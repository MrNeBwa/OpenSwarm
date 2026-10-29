"""Core daemon: the single owner of VPN state and the IPC server.

The daemon runs one asyncio event loop.  It serves newline-delimited JSON over
a private Unix socket (see :mod:`ninjavpn_client.ipc`), keeps the settings and
server cache, supervises the connection state machine and publishes events to
every connected client.

Running this module directly starts the user daemon::

    python -m ninjavpn_client.core.daemon

A single instance is enforced with ``flock`` on
``$XDG_STATE_HOME/ninjavpn-client/daemon.lock``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import socket
import struct
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from .. import __version__
from ..ipc import (
    PROTOCOL_VERSION,
    ConnectParams,
    ConnectResult,
    DisconnectParams,
    EdgeImportParams,
    EdgePingParams,
    EdgeStatusResult,
    ErrorCode,
    EventMessage,
    EventName,
    IPCError,
    IPCProtocolError,
    LogTailParams,
    Method,
    PingResult,
    ProtocolVersionError,
    Request,
    Response,
    ServerPingParams,
    ServersPingResult,
    ServersResult,
    SettingsResult,
    SettingsSetParams,
    SubscribeParams,
    SubscribeResult,
    decode_message,
    default_socket_path,
    encode_message,
    prepare_socket_path,
    remove_stale_socket,
)
from ..schema import (
    ConnectionStatus,
    SettingsError,
    ValidationError,
    default_log_dir,
    default_state_dir,
)
from .builtin_edge import builtin_servers, bundled_edge_path, load_builtin_config
from .connection import ConnectionError, ConnectionManager
from .edge_client import EdgeError
from .edge_config import EdgeConfig, EdgeConfigError
from .edge_provider import EdgeProvider
from .identity import DeviceIdentity, load_identity
from .logs import LogBuffer
from .servers import ServerRepository, SubscriptionError
from .settings_store import SettingsStore

Publisher = Callable[[str, dict[str, Any]], EventMessage | None]


class ServiceError(Exception):
    """A request failed with a stable IPC error code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


def _issues(exc: ValidationError) -> list[dict[str, str]]:
    return [{"path": issue.path, "message": issue.message} for issue in exc.issues]


def _edge_status_result(
    config: EdgeConfig, path: Path, *, enabled: bool = True, source: str = "file"
) -> EdgeStatusResult:
    digest = hashlib.sha256(config.cert_der).hexdigest()
    pins = {pin for edge in config.paths.values() for pin in edge.pins}
    return EdgeStatusResult(
        enabled=bool(enabled),
        config_path=str(path),
        paths=sorted(path_type.value for path_type in config.paths),
        cert_sha256=digest,
        pins_count=len(pins),
        time=config.time,
        error=None,
        source=source,
    )


class CoreService:
    """Request dispatcher implementing the IPC method catalogue."""

    def __init__(
        self,
        *,
        store: SettingsStore | None = None,
        repository: ServerRepository | None = None,
        connection: ConnectionManager | None = None,
        logs: LogBuffer | None = None,
    ) -> None:
        self.store = store
        self.repository = repository
        self.logs = logs
        self.connections = connection
        self.publisher: Publisher | None = None
        self.started_at = time.time()
        self._identity: DeviceIdentity | None = None
        self._edge_provider: EdgeProvider | None = None
        self._closed = False
        self._shutdown = asyncio.Event()
        self._background: set[asyncio.Task[Any]] = set()

    # -- lifecycle ----------------------------------------------------------
    async def setup(self) -> None:
        if self.store is None:
            self.store = SettingsStore()
        settings = self.store.load()

        if self.repository is None:
            self.repository = ServerRepository()
        try:
            self.repository.load_cache()
        except SubscriptionError as exc:
            self.logs = self.logs or LogBuffer()
            self.logs.add("warning", f"server cache ignored: {exc}", "core")

        if self.logs is None:
            self.logs = LogBuffer(file_path=default_log_dir() / "core.log")

        # A fresh installation has no cached servers.  Seed the catalog from
        # the built-in edge.bin so the client can connect without a
        # subscription; a missing or broken blob only disables that server.
        if not self.repository.servers:
            path = self.repository.edge_bin_path
            try:
                imported = self.repository.import_builtin(path)
                self._log(
                    "info",
                    f"built-in servers imported: {imported.count} ({path})",
                    "core",
                )
            except (SubscriptionError, EdgeError, OSError) as exc:
                self._log(
                    "warning",
                    f"built-in server import failed: {exc.__class__.__name__}: {exc}",
                    "core",
                )


        # The public cache strips credentials, so the built-in APK server is
        # re-seeded on every start.  A missing or broken blob must never stop
        # the daemon: it only disables this one server.
        try:
            identity = self._identity or load_identity()
            self._identity = identity
            added = self.repository.merge_servers(
                builtin_servers(load_builtin_config(identity.hard))
            )
            if added:
                self._log("info", f"APK edge server added: {added}", "core")
        except EdgeConfigError as exc:
            self._log("warning", f"APK edge config unavailable: {exc}", "core")
        except Exception as exc:
            self._log(
                "warning", f"APK edge seed failed: {exc.__class__.__name__}", "core"
            )

        if self.connections is None:
            self.connections = ConnectionManager(
                self.store,
                self.repository,
                on_event=self._on_event,
                on_log=self._log,
            )

        # Recover from a daemon that was killed while a tunnel was active.
        # Never touch an interface without our own state file: it may belong
        # to another tool or to a different user.
        try:
            from .tunnel import TunnelManager

            manager = TunnelManager()
            if manager.state_exists:
                result = await manager.cleanup()
                if result.ok:
                    self.logs.add("warning", "removed stale TUN state", "core")
                else:
                    self.logs.add(
                        "error",
                        f"could not clean stale TUN state: {result.code or result.message}",
                        "core",
                    )
            elif manager.interface_exists():
                self.logs.add(
                    "warning",
                    f"TUN interface {manager.interface} exists without state (TUN_STALE)",
                    "core",
                )
        except Exception as exc:
            self.logs.add(
                "warning",
                f"TUN cleanup check failed: {exc.__class__.__name__}",
                "core",
            )

        self.logs.add("info", f"core {__version__} started", "core")
        if settings.data_plane.value == "tun" and not settings.edge_enabled:
            self.logs.add(
                "warning",
                "data_plane=tun without edge config; imported WireGuard profiles may be used",
                "core",
            )

    async def close(self) -> None:
        self._closed = True
        for task in list(self._background):
            task.cancel()
        for task in list(self._background):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._background.clear()
        if self.connections is not None:
            with contextlib.suppress(Exception):
                await self.connections.close()
        if self._edge_provider is not None:
            with contextlib.suppress(Exception):
                await self._edge_provider.close()
            self._edge_provider = None
        self._shutdown.set()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    # -- helpers ------------------------------------------------------------
    def _settings(self):
        assert self.store is not None
        return self.store.get()

    def _log(self, level: str, message: str, source: str = "core") -> None:
        if self.logs is None:
            return
        entry = self.logs.add(level, message, source)
        self._publish(EventName.LOG, entry.to_dict())

    def _publish(self, name: str, data: dict[str, Any]) -> EventMessage | None:
        publisher = self.publisher
        if publisher is None:
            return None
        try:
            return publisher(name, data)
        except Exception:
            return None

    def _on_event(self, name: str, data: dict[str, Any]) -> None:
        if name == EventName.STATE and data.get("status") == ConnectionStatus.CONNECTED.value:
            server_id = data.get("server_id")
            if isinstance(server_id, str) and server_id:
                with contextlib.suppress(Exception):
                    if self._settings().selected_server_id != server_id:
                        self.store.update({"selected_server_id": server_id})
        self._publish(name, data)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _require(self, method: str) -> None:
        if self._closed:
            raise ServiceError(ErrorCode.SHUTTING_DOWN, "core is shutting down")

    def _edge(self) -> EdgeProvider:
        settings = self._settings()
        if self._edge_provider is None:
            if self._identity is None:
                self._identity = load_identity()
            self._edge_provider = EdgeProvider(settings, self._identity)
        else:
            self._edge_provider.settings = settings
        return self._edge_provider

    def _emit_servers(self) -> None:
        result = self._servers_result()
        self._publish(EventName.SERVERS, result.to_dict())

    def _servers_result(self) -> ServersResult:
        assert self.repository is not None
        return ServersResult(
            servers=list(self.repository.servers),
            updated_at=self.repository.updated_at,
            source=self.repository.source,
        )

    # -- dispatch -----------------------------------------------------------
    async def dispatch(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._require(method)
        if not isinstance(method, str) or not method:
            raise ServiceError(ErrorCode.INVALID_REQUEST, "method must be a non-empty string")
        data = dict(params or {})
        if not isinstance(params, Mapping):
            raise ServiceError(ErrorCode.INVALID_PARAMS, "params must be an object")

        if method == Method.PING:
            return PingResult(
                daemon_version=__version__,
                protocol_version=PROTOCOL_VERSION,
                pid=os.getpid(),
                uptime_seconds=max(0.0, time.time() - self.started_at),
            ).to_dict()

        if method == Method.STATUS:
            assert self.connections is not None
            return (await self.connections.status()).to_dict()

        if method == Method.CONNECT:
            return await self._connect(data)

        if method == Method.DISCONNECT:
            assert self.connections is not None
            try:
                request = DisconnectParams.from_dict(data)
            except IPCProtocolError as exc:
                raise ServiceError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
            try:
                result = await self.connections.disconnect(request)
            except ConnectionError as exc:
                raise ServiceError(exc.code, exc.message, exc.details) from exc
            self._log("info", "disconnected", "core")
            return result.to_dict()

        if method == Method.SERVERS:
            return self._servers_result().to_dict()

        if method == Method.SERVERS_PING:
            return await self._servers_ping(data)

        if method == Method.SUBSCRIBE:
            return await self._subscribe(data)

        if method == Method.SETTINGS_GET:
            return SettingsResult(self._settings()).to_dict()

        if method == Method.SETTINGS_SET:
            return await self._settings_set(data)

        if method == Method.LOG_TAIL:
            return self._log_tail(data)

        if method == Method.SHUTDOWN:
            self.request_shutdown()
            return {"stopping": True}

        if method == Method.IMPORT:
            return await self._import(data)

        if method == Method.EDGE_IMPORT:
            return await self._edge_import(data)

        if method == Method.EDGE_STATUS:
            return self._edge_status()

        if method == Method.EDGE_REFRESH:
            return await self._edge_refresh(data)

        if method == Method.EDGE_PING:
            return await self._edge_ping(data)

        if method == Method.TUNNEL_STATUS:
            return await self._tunnel_status()

        if method == Method.TUNNEL_CLEANUP:
            return await self._tunnel_cleanup()

        raise ServiceError(ErrorCode.UNKNOWN_METHOD, f"unknown method {method}")

    # -- methods ------------------------------------------------------------
    async def _connect(self, params: Mapping[str, Any]) -> dict[str, Any]:
        assert self.connections is not None
        try:
            request = ConnectParams.from_dict(params)
        except IPCProtocolError as exc:
            raise ServiceError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        try:
            result: ConnectResult = await self.connections.connect(request)
        except ConnectionError as exc:
            raise ServiceError(exc.code, exc.message, exc.details) from exc
        except (ValidationError, ValueError) as exc:
            raise ServiceError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        return result.to_dict()

    async def _servers_ping(self, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = ServerPingParams.from_dict(params)
        except IPCProtocolError as exc:
            raise ServiceError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        assert self.repository is not None
        try:
            results = await asyncio.to_thread(
                self.repository.ping,
                request.ids,
                timeout=request.timeout_seconds,
            )
        except Exception as exc:
            raise ServiceError(ErrorCode.INTERNAL, f"ping failed: {exc}") from exc
        self._emit_servers()
        return ServersPingResult.from_dict({"results": results}).to_dict()

    async def _subscribe(self, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = SubscribeParams.from_dict(params)
        except IPCProtocolError as exc:
            raise ServiceError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        url = (request.url or self._settings().subscription_url).strip()
        if not url:
            raise ServiceError(
                ErrorCode.INVALID_PARAMS, "subscription url is not configured"
            )
        assert self.repository is not None
        try:
            result = await asyncio.to_thread(
                self.repository.import_subscription,
                url,
                force=request.force,
                timeout=request.timeout_seconds,
            )
        except SubscriptionError as exc:
            raise ServiceError(ErrorCode.SUBSCRIPTION_FAILED, str(exc)) from exc
        except Exception as exc:
            raise ServiceError(ErrorCode.SUBSCRIPTION_FAILED, str(exc)) from exc

        if request.url and request.url != self._settings().subscription_url:
            with contextlib.suppress(Exception):
                self.store.update({"subscription_url": request.url})
                self._publish(
                    EventName.SETTINGS, {"settings": self._settings().to_dict()}
                )
        self._emit_servers()
        message = (
            f"Подписка обновлена: {result.count} серверов"
            if result.updated
            else f"Кэш актуален: {result.count} серверов"
        )
        return SubscribeResult(
            updated=bool(result.updated),
            count=result.count,
            fetched_at=result.fetched_at or time.time(),
            source=result.source,
            message=message,
        ).to_dict()

    async def _settings_set(self, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = SettingsSetParams.from_dict(params)
        except IPCProtocolError as exc:
            raise ServiceError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        try:
            updated = self.store.update(request.patch, merge=request.merge)
        except ValidationError as exc:
            raise ServiceError(
                ErrorCode.INVALID_PARAMS,
                "invalid settings",
                {"issues": _issues(exc)},
            ) from exc
        except (SettingsError, OSError) as exc:
            raise ServiceError(ErrorCode.IO_ERROR, str(exc)) from exc
        result = SettingsResult(updated).to_dict()
        self._publish(EventName.SETTINGS, result)
        return result

    def _log_tail(self, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = LogTailParams.from_dict(params)
        except IPCProtocolError as exc:
            raise ServiceError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        assert self.logs is not None
        return self.logs.tail(
            lines=request.lines,
            level=request.level,
            since=request.since,
            source=request.source,
        ).to_dict()

    async def _import(self, params: Mapping[str, Any]) -> dict[str, Any]:
        source = params.get("source", params.get("path"))
        if not isinstance(source, str) or not source.strip():
            raise ServiceError(ErrorCode.INVALID_PARAMS, "source must be a non-empty string")
        assert self.repository is not None
        try:
            if source.startswith(("http://", "https://")):
                result = await asyncio.to_thread(self.repository.import_url, source)
            else:
                result = await asyncio.to_thread(self.repository.import_file, source)
        except SubscriptionError as exc:
            raise ServiceError(ErrorCode.SUBSCRIPTION_FAILED, str(exc)) from exc
        except Exception as exc:
            raise ServiceError(ErrorCode.SUBSCRIPTION_FAILED, str(exc)) from exc
        self._emit_servers()
        self._log("info", f"imported {result.count} server(s)", "core")
        return {
            "count": result.count,
            "servers": [server.to_public_dict() for server in result.servers],
            "source": result.source,
            "updated_at": result.fetched_at,
        }

    # -- native edge --------------------------------------------------------
    async def _edge_import(self, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = EdgeImportParams.from_dict(params)
        except IPCProtocolError as exc:
            raise ServiceError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        provider = self._edge()
        source = Path(request.path).expanduser()
        try:
            trusted = source.exists() and os.path.samefile(source, bundled_edge_path())
        except OSError:
            trusted = False
        try:
            config = await asyncio.to_thread(
                provider.import_config,
                request.path,
                force=request.force,
                trusted=trusted,
            )
        except EdgeConfigError as exc:
            raise ServiceError(exc.code, str(exc)) from exc
        except EdgeError as exc:
            raise ServiceError(exc.code, str(exc)) from exc
        path = provider.config_path()
        with contextlib.suppress(Exception):
            self.store.update({"edge_enabled": True})
        self._log("info", f"edge config imported: {path}", "core")
        self._publish(
            EventName.SETTINGS, {"settings": self._settings().to_dict()}
        )
        return _edge_status_result(config, path).to_dict()

    def _edge_status(self) -> dict[str, Any]:
        provider = self._edge()
        path = provider.config_path()
        try:
            config = provider.load_config(fallback_builtin=True)
        except EdgeError as exc:
            return EdgeStatusResult(
                enabled=False,
                config_path=str(path),
                error=str(exc),
            ).to_dict()
        return _edge_status_result(
            config,
            path,
            enabled=self._settings().edge_enabled,
            source=provider.last_config_source or "file",
        ).to_dict()

    async def _edge_refresh(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if params.get("path") is not None:
            path_value = params.get("path")
            if not isinstance(path_value, str):
                raise ServiceError(ErrorCode.INVALID_PARAMS, "path must be a string")
        provider = self._edge()
        try:
            servers = await provider.refresh()
        except EdgeConfigError as exc:
            raise ServiceError(exc.code, str(exc)) from exc
        except EdgeError as exc:
            raise ServiceError(exc.code, str(exc)) from exc
        assert self.repository is not None
        merged = [
            server
            for server in self.repository.servers
            if server.params.get("provider") != "ninja-edge"
        ]
        merged.extend(servers)
        try:
            self.repository.set_servers(merged, source="edge")
        except SubscriptionError as exc:
            raise ServiceError(ErrorCode.SUBSCRIPTION_FAILED, str(exc)) from exc
        self._emit_servers()
        self._log("info", f"edge manifest refreshed: {len(servers)} server(s)", "core")
        return {
            "servers": [server.to_public_dict() for server in servers],
            "count": len(servers),
            "source": "edge",
        }

    async def _edge_ping(self, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = EdgePingParams.from_dict(params)
        except IPCProtocolError as exc:
            raise ServiceError(ErrorCode.INVALID_PARAMS, str(exc)) from exc
        provider = self._edge()
        try:
            config = provider.load_config()
            client = provider._make_client(config)
            provider._client = client
            try:
                ok = await client.ping(request.path)
            finally:
                await client.close()
                provider._client = None
        except EdgeConfigError as exc:
            raise ServiceError(exc.code, str(exc)) from exc
        except EdgeError as exc:
            raise ServiceError(exc.code, str(exc)) from exc
        return {"ok": bool(ok), "path": request.path}

    # -- tunnel -------------------------------------------------------------
    def _tunnel(self):
        assert self.connections is not None
        manager = self.connections._tunnel_manager
        if manager is None:
            from .tunnel import TunnelManager

            manager = TunnelManager()
            self.connections._tunnel_manager = manager
        return manager

    async def _tunnel_status(self) -> dict[str, Any]:
        status = await self._tunnel().status()
        return {
            "active": status.active,
            "interface": status.interface,
            "endpoint": status.endpoint,
            "latest_handshake": status.latest_handshake,
            "rx_bytes": status.rx_bytes,
            "tx_bytes": status.tx_bytes,
        }

    async def _tunnel_cleanup(self) -> dict[str, Any]:
        result = await self._tunnel().cleanup()
        return {
            "ok": result.ok,
            "active": result.active,
            "message": result.message,
            "code": result.code,
        }


# ---------------------------------------------------------------------------
# IPC server
# ---------------------------------------------------------------------------


class _Client:
    __slots__ = ("lock", "sent_seq", "writer")

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.lock = asyncio.Lock()
        self.sent_seq = 0


class IPCServer:
    """Asyncio Unix-socket server with same-UID peer checks and broadcasts."""

    def __init__(
        self,
        service: Any,
        path: str | os.PathLike[str] | None = None,
        *,
        socket_mode: int = 0o600,
    ) -> None:
        self.service = service
        self.path = (
            Path(path).expanduser() if path is not None else default_socket_path()
        )
        self.socket_mode = socket_mode
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[_Client] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._seq = 0
        self._closed = False
        self._started_at = 0.0
        # A short replay window closes the race where an event is published
        # between accept() and the handler registering its writer.
        self._recent: deque[EventMessage] = deque(maxlen=256)

    @property
    def socket_path(self) -> Path:
        return self.path

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        target = prepare_socket_path(self.path)
        directory = target.parent
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        remove_stale_socket(target)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(target),
                limit=1024 * 1024 + 2,
            )
        except OSError as exc:
            raise ServiceError(
                ErrorCode.IO_ERROR, f"cannot listen on {target}: {exc}"
            ) from exc
        try:
            os.chmod(target, self.socket_mode)
        except OSError:
            with contextlib.suppress(Exception):
                self._server.close()
                await self._server.wait_closed()
            raise
        self._started_at = time.time()
        self._closed = False
        publisher = getattr(self.service, "publisher", None)
        if publisher is None and hasattr(self.service, "publisher"):
            self.service.publisher = self.publish

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for client in list(self._clients):
            self._clients.discard(client)
            with contextlib.suppress(Exception):
                client.writer.close()
                await client.writer.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        with contextlib.suppress(OSError):
            self.path.unlink()

    # -- broadcast ----------------------------------------------------------
    def publish(self, event: str, data: Mapping[str, Any] | None = None) -> EventMessage:
        self._seq += 1
        frame = EventMessage(
            event=event,
            data=dict(data or {}),
            seq=self._seq,
            ts=time.time(),
        )
        self._recent.append(frame)
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon(self._broadcast, frame)
        else:  # pragma: no cover - publish before start/after stop
            self._broadcast(frame)
        return frame

    def _broadcast(self, frame: EventMessage) -> None:
        if self._closed or not self._clients:
            return
        for client in list(self._clients):
            self._spawn(self._send(client, frame))

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- clients ------------------------------------------------------------
    def _peer_allowed(self, writer: asyncio.StreamWriter) -> bool:
        sock = writer.get_extra_info("socket")
        if sock is None or not hasattr(socket, "SO_PEERCRED"):
            return True
        try:
            raw = sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", raw)
        except (OSError, AttributeError, ValueError):
            return True
        return uid == os.getuid()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not self._peer_allowed(writer):
            with contextlib.suppress(Exception):
                writer.close()
            return
        client = _Client(writer)
        self._clients.add(client)
        now = time.time()
        for frame in list(self._recent):
            if now - frame.ts <= 2.0:
                self._spawn(self._send(client, frame))
        try:
            while not self._closed:
                try:
                    line = await reader.readline()
                except ValueError:
                    await self._send_error(
                        client, "invalid", ErrorCode.INVALID_REQUEST, "frame is too large"
                    )
                    break
                if not line:
                    break
                await self._handle_frame(client, line)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            pass
        finally:
            self._clients.discard(client)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _handle_frame(self, client: _Client, line: bytes) -> None:
        request_id = "invalid"
        try:
            message = decode_message(line, expected_version=None)
        except ProtocolVersionError as exc:
            with contextlib.suppress(Exception):
                data = json.loads(line.decode("utf-8", "replace"))
                request_id = str(data.get("id") or "invalid")
            await self._send_error(
                client,
                request_id,
                ErrorCode.UNSUPPORTED_VERSION,
                str(exc),
                {"supported": [PROTOCOL_VERSION]},
            )
            return
        except (IPCProtocolError, UnicodeDecodeError, ValueError) as exc:
            await self._send_error(
                client, request_id, ErrorCode.INVALID_REQUEST, str(exc)
            )
            return

        if not isinstance(message, Request):
            await self._send_error(
                client,
                getattr(message, "id", "invalid"),
                ErrorCode.INVALID_REQUEST,
                "expected a request envelope",
            )
            return
        if message.version != PROTOCOL_VERSION:
            await self._send_error(
                client,
                message.id,
                ErrorCode.UNSUPPORTED_VERSION,
                f"unsupported protocol version {message.version}",
                {"supported": [PROTOCOL_VERSION]},
            )
            return

        task = asyncio.create_task(self._dispatch(client, message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch(self, client: _Client, request: Request) -> None:
        try:
            result = await self.service.dispatch(request.method, request.params)
        except ServiceError as exc:
            await self._send_error(client, request.id, exc.code, exc.message, exc.details)
            return
        except IPCProtocolError as exc:
            await self._send_error(
                client, request.id, ErrorCode.INVALID_PARAMS, str(exc)
            )
            return
        except ValidationError as exc:
            await self._send_error(
                client,
                request.id,
                ErrorCode.INVALID_PARAMS,
                "invalid parameters",
                {"issues": _issues(exc)},
            )
            return
        except Exception as exc:  # defensive: never leak a traceback to clients
            code = getattr(exc, "code", None) or ErrorCode.INTERNAL
            await self._send_error(client, request.id, str(code), str(exc))
            return

        response = Response(
            id=request.id,
            ok=True,
            result=dict(result or {}),
            version=PROTOCOL_VERSION,
        )
        await self._send(client, response)
        if request.method == Method.SHUTDOWN:
            shutdown = getattr(self.service, "request_shutdown", None)
            if callable(shutdown):
                with contextlib.suppress(Exception):
                    shutdown()
            self._spawn(self.stop())

    # -- writing ------------------------------------------------------------
    async def _send(self, client: _Client, message: Any) -> None:
        if isinstance(message, EventMessage):
            if message.seq and message.seq <= client.sent_seq:
                return
            client.sent_seq = max(client.sent_seq, message.seq)
        try:
            payload = encode_message(message)
        except IPCProtocolError:
            return
        try:
            async with client.lock:
                client.writer.write(payload)
                await client.writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            self._clients.discard(client)

    async def _send_error(
        self,
        client: _Client,
        request_id: str,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if not request_id:
            request_id = "invalid"
        await self._send(
            client,
            Response(
                id=request_id,
                ok=False,
                error=IPCError(code, message, dict(details or {})),
                version=PROTOCOL_VERSION,
            ),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _lock_path() -> Path:
    return default_state_dir() / "daemon.lock"


def _acquire_lock(path: Path) -> int | None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    return fd


async def _serve(socket_path: Path | None = None) -> int:
    service = CoreService()
    await service.setup()
    server = IPCServer(service, socket_path)
    await server.start()
    stop = asyncio.Event()

    def _request_stop(*_args: Any) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, _request_stop)

    async def _watch() -> None:
        await service._shutdown.wait()
        stop.set()

    watcher = asyncio.create_task(_watch())
    try:
        await stop.wait()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await server.stop()
        await service.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ninjavpn-clientd",
        description="Ninja VPN core daemon (user session).",
    )
    parser.add_argument("--socket", type=Path, default=None, help="Unix socket path")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0

    lock_fd = _acquire_lock(_lock_path())
    if lock_fd is None:
        print("ninjavpn-client daemon is already running", file=sys.stderr)
        return 0
    try:
        return asyncio.run(_serve(args.socket))
    except KeyboardInterrupt:  # pragma: no cover - signal handler normally wins
        return 0
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(lock_fd)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["CoreService", "IPCServer", "ServiceError", "main"]
