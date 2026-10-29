"""Connection state machine for proxy (Xray) and TUN (WireGuard) data planes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..ipc import (
    ConnectParams,
    ConnectResult,
    DisconnectParams,
    DisconnectResult,
    StatusResult,
)
from ..schema import (
    ConnectionState,
    ConnectionStats,
    ConnectionStatus,
    DataPlane,
    Protocol,
    RoutingMode,
    Security,
    Server,
    Settings,
    Transport,
    default_state_dir,
)
from .config_builder import ConfigBuildError, build_xray_config
from .edge_client import EdgeError
from .edge_provider import EDGE_PROVIDER, EdgeProvider
from .identity import DeviceIdentity
from .privileges import KillSwitch
from .servers import ServerRepository, SubscriptionError
from .settings_store import SettingsStore
from .stats import StatsCollector
from .tunnel import TunnelManager, TunnelStatus
from .xray import XrayError, XrayNotFound, XrayProcess, resolve_xray


class ConnectionError(RuntimeError):
    """Connection failure carrying a stable IPC error code."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


async def _default_health(port: int, timeout: float = 3.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=timeout
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader
    return True


class ConnectionManager:
    """Own one active connection and publish state/progress/stats events."""

    def __init__(
        self,
        store: SettingsStore,
        repository: ServerRepository,
        *,
        process_factory: Callable[..., Any] | None = None,
        health_check: Callable[..., Any] | None = None,
        external_ip_checker: Callable[[], Any] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        on_log: Callable[..., None] | None = None,
        stats_interval: float = 1.0,
        startup_timeout: float = 10.0,
        config_dir: str | Path | None = None,
        edge_provider: EdgeProvider | None = None,
        tunnel_manager: TunnelManager | None = None,
        identity: DeviceIdentity | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.store = store
        self.repository = repository
        self.settings = store
        self._process_factory = process_factory or XrayProcess
        self._health_check = health_check or _default_health
        self._external_ip_checker = external_ip_checker
        self._on_event = on_event
        self._on_log = on_log
        self.stats_interval = max(0.01, float(stats_interval))
        self.startup_timeout = max(0.1, float(startup_timeout))
        self.state = ConnectionState()
        self._lock = asyncio.Lock()
        self._process: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._stats_task: asyncio.Task[None] | None = None
        self._server: Server | None = None
        self._stats = ConnectionStats()
        self._data_plane = DataPlane.PROXY
        self._tunnel_status: TunnelStatus | None = None
        self._edge_provider = edge_provider
        self._tunnel_manager = tunnel_manager
        self._identity = identity
        self._kill_switch = kill_switch
        self._config_path = (
            Path(config_dir).expanduser() / "xray-config.json"
            if config_dir is not None
            else default_state_dir() / "xray-config.json"
        )
        self._config_dir = self._config_path.parent
        self._connected_at: float | None = None
        self._external_ip = ""
        self._tun_collector: StatsCollector | None = None

    # -- properties ---------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def server(self) -> Server | None:
        return self._server

    @property
    def data_plane(self) -> DataPlane:
        return self._data_plane

    def _settings(self) -> Settings:
        return self.store.get()

    # -- event helpers ------------------------------------------------------
    def _emit(self, name: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(name, data)

    def _log(self, level: str, message: str, source: str = "core") -> None:
        if self._on_log is not None:
            try:
                self._on_log(level, message, source)
            except TypeError:
                with contextlib.suppress(Exception):
                    self._on_log(level, message)
            except Exception:
                pass

    def _progress(
        self,
        stage: str,
        percent: float,
        message: str = "",
        server_id: str | None = None,
    ) -> None:
        self._emit(
            "progress",
            {
                "stage": stage,
                "percent": percent,
                "message": message,
                "server_id": server_id or (self._server.id if self._server else None),
            },
        )

    def _set_state(
        self,
        status: ConnectionStatus,
        *,
        server: Server | None = None,
        error: str | None = None,
        error_code: str | None = None,
        attempt: int = 0,
    ) -> ConnectionState:
        if server is not None:
            self._server = server
        self.state = ConnectionState(
            status=status,
            since=time.time(),
            server_id=(self._server.id if self._server else None),
            server_name=(self._server.name if self._server else None),
            error=error,
            error_code=error_code,
            attempt=attempt,
        )
        self._emit("state", self.state.to_dict())
        return self.state

    # -- public API ---------------------------------------------------------
    async def connect(self, params: ConnectParams) -> ConnectResult:
        if not isinstance(params, ConnectParams):
            params = ConnectParams.from_dict(params)  # type: ignore[arg-type]
        async with self._lock:
            if self.state.status in (ConnectionStatus.CONNECTED, ConnectionStatus.CONNECTING):
                if not params.force:
                    raise ConnectionError("ALREADY_CONNECTED", "already connected")
                await self._stop_current(force=True)
            try:
                server = self.repository.select(self._settings(), params.server_id)
            except SubscriptionError as exc:
                raise ConnectionError("SERVER_NOT_FOUND", str(exc)) from exc
            except Exception as exc:
                raise ConnectionError("SERVER_NOT_FOUND", str(exc)) from exc

            settings = self._settings()
            data_plane = params.data_plane or settings.data_plane
            routing_mode = params.routing_mode or settings.routing_mode
            if data_plane is DataPlane.TUN and routing_mode is not RoutingMode.GLOBAL:
                raise ConnectionError(
                    "INVALID_PARAMS", "TUN requires routing_mode=global"
                )
            self._data_plane = data_plane
            self._server = server
            self._stats = ConnectionStats(server_id=server.id)
            self._set_state(
                ConnectionStatus.CONNECTING, server=server, attempt=1
            )
            self._log("info", f"connecting to {server.id}")
            self._task = asyncio.create_task(
                self._connect_flow(server, data_plane, routing_mode),
                name="ninjavpn-connect",
            )
            return ConnectResult(accepted=True, state=self.state, server=server)

    async def _connect_flow(
        self,
        server: Server,
        data_plane: DataPlane,
        routing_mode: RoutingMode,
    ) -> None:
        try:
            if data_plane is DataPlane.TUN:
                await self._connect_tunnel(server)
            else:
                await self._connect_proxy(server)
        except asyncio.CancelledError:
            raise
        except ConnectionError as exc:
            await self._fail(exc.code, exc.message)
        except XrayNotFound as exc:
            await self._fail("XRAY_NOT_FOUND", str(exc))
        except ConfigBuildError as exc:
            await self._fail("CONFIG_INVALID", str(exc))
        except (XrayError, EdgeError) as exc:
            code = getattr(exc, "code", None) or "XRAY_START_FAILED"
            await self._fail(code, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            await self._fail("XRAY_START_FAILED", f"{exc.__class__.__name__}: {exc}")

    async def _edge_proxy_server(self, server: Server) -> Server:
        """Turn an edge catalog entry into a userspace WireGuard outbound.

        Rootless proxy mode has no system TUN, so the WireGuard peer is
        projected into a userspace Xray outbound.  The install response is
        short-lived and the private key never leaves the user account.
        """

        provider = self._edge_provider
        if provider is None:
            provider = EdgeProvider(self._settings(), self._identity)
            self._edge_provider = provider
        self._progress("handshake", 10, "Получение WireGuard-конфига", server.id)
        install = await provider.install(server)
        profile = install.profile
        host, separator, port_text = profile.endpoint.rpartition(":")
        if not separator or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ConfigBuildError("edge install returned an invalid endpoint")
        return Server(
            id=server.id,
            name=server.name,
            host=host.strip("[]"),
            port=int(port_text),
            protocol=Protocol.WIREGUARD,
            transport=Transport.TCP,
            security=Security.NONE,
            country=server.country,
            country_code=server.country_code,
            city=server.city,
            premium=server.premium,
            latency_ms=server.latency_ms,
            tags=[*server.tags, "edge-native"],
            params={
                "provider": EDGE_PROVIDER,
                "endpoint": profile.endpoint,
                "public_key": profile.peer_public_key,
                "address": list(profile.addresses),
                "allowed_ips": list(profile.allowed_ips),
                "keepalive": profile.persistent_keepalive,
                "mtu": profile.mtu,
            },
            credentials={"private_key": profile.private_key},
        )

    async def _connect_proxy(self, server: Server) -> None:
        if server.params.get("provider") == EDGE_PROVIDER:
            server = await self._edge_proxy_server(server)
        self._progress("generating_config", 20, "Сборка конфигурации Xray")
        raw = None
        if (
            self.repository.raw_config is not None
            and self.repository.imported_server_id == server.id
        ):
            raw = self.repository.raw_config
        try:
            config = build_xray_config(self._settings(), server, raw_config=raw)
        except ConfigBuildError:
            raise
        try:
            binary = resolve_xray(self._settings(), verify=False)
        except XrayNotFound:
            raise
        self._write_config(config)
        self._progress("starting_xray", 60, "Запуск Xray")
        process = self._process_factory(
            binary.path,
            self._config_path,
            socks_port=self._settings().socks_port,
            startup_timeout=self.startup_timeout,
            on_log=lambda level, message: self._log(level, message, "xray"),
            on_exit=self._on_process_exit,
        )
        self._process = process
        try:
            await process.start()
        except Exception as exc:
            with contextlib.suppress(Exception):
                await process.stop(force=True)
            raise ConnectionError("XRAY_START_FAILED", str(exc)) from exc

        if not await self._wait_healthy():
            with contextlib.suppress(Exception):
                await process.stop(force=True)
            self._process = None
            raise ConnectionError("XRAY_START_FAILED", "Xray health-check failed")

        self._mark_connected()
        self._stats_task = asyncio.create_task(self._stats_loop(), name="ninjavpn-stats")

    async def _connect_tunnel(self, server: Server) -> None:
        settings = self._settings()
        provider = self._edge_provider
        if provider is None:
            provider = EdgeProvider(settings, self._identity)
            self._edge_provider = provider
        manager = self._tunnel_manager
        if manager is None:
            manager = TunnelManager()
            self._tunnel_manager = manager

        self._progress("handshake", 10, "Получение WireGuard-конфига", server.id)
        install = await provider.install(server)
        self._progress("kill_switch", 40, "Поднятие TUN", server.id)
        result = await manager.up(
            install.profile,
            kill_switch=settings.kill_switch,
            dns_leak_protection=settings.dns_leak_protection,
        )
        if not result.ok:
            raise ConnectionError(result.code or "TUN_START_FAILED", result.message)
        self._tun_collector = StatsCollector(interface=install.profile.interface)
        self._progress("ready", 100, "Подключено", server.id)
        self._mark_connected()
        self._stats_task = asyncio.create_task(self._stats_loop(), name="ninjavpn-stats")

    def _mark_connected(self) -> None:
        self._connected_at = time.time()
        self._set_state(ConnectionStatus.CONNECTED, server=self._server)
        self._progress("ready", 100, "Подключено", self._server.id if self._server else None)
        self._log("info", f"connected to {self._server.id if self._server else '?'}")

    async def _wait_healthy(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        port = self._settings().socks_port
        while True:
            process = self._process
            # Fake processes in tests may expose a static flag; real ones are
            # checked after the first probe below.
            if (
                process is not None
                and hasattr(process, "is_running")
                and not process.is_running
                and getattr(process, "_process", None) is not None
            ):
                return False
            with contextlib.suppress(Exception):
                result = self._health_check(port, min(2.0, self.startup_timeout))
                if asyncio.iscoroutine(result):
                    result = await result
                if result:
                    return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)

    def _write_config(self, config: dict[str, Any]) -> None:
        self._config_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        payload = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self._config_path.name}.", suffix=".tmp", dir=str(self._config_dir)
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._config_path)
            os.chmod(self._config_path, 0o600)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    async def _fail(self, code: str, message: str) -> None:
        self._process = None
        self._connected_at = None
        self._set_state(
            ConnectionStatus.ERROR,
            server=self._server,
            error=message,
            error_code=code,
        )
        self._log("error", f"{code}: {message}")

    async def _stats_loop(self) -> None:
        try:
            first = True
            while self.state.status is ConnectionStatus.CONNECTED:
                stats = await self._collect_stats()
                self._stats = stats
                self._emit("stats", stats.to_dict())
                if first and self._external_ip_checker is not None:
                    first = False
                    with contextlib.suppress(Exception):
                        value = self._external_ip_checker()
                        if asyncio.iscoroutine(value):
                            value = await value
                        if isinstance(value, str):
                            self._external_ip = value
                await asyncio.sleep(self.stats_interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _collect_stats(self) -> ConnectionStats:
        now = time.time()
        uptime = (now - self._connected_at) if self._connected_at else 0.0
        stats = ConnectionStats(
            up_bytes=self._stats.up_bytes,
            down_bytes=self._stats.down_bytes,
            uptime_seconds=max(0.0, uptime),
            external_ip=self._external_ip,
            server_id=self._server.id if self._server else None,
            updated_at=now,
        )
        if self._data_plane is DataPlane.TUN and self._tun_collector is not None:
            sample = await asyncio.to_thread(self._tun_collector.sample)
            stats.down_bytes = sample.rx_bytes
            stats.up_bytes = sample.tx_bytes
            self._tunnel_status = TunnelStatus(
                active=True,
                interface=sample.interface,
                endpoint=self._tunnel_status.endpoint if self._tunnel_status else "",
                latest_handshake=sample.latest_handshake,
                rx_bytes=sample.rx_bytes,
                tx_bytes=sample.tx_bytes,
            )
        return stats

    async def _on_process_exit(self, code: int | None) -> None:
        if self.state.status is ConnectionStatus.CONNECTED:
            await self._fail("XRAY_START_FAILED", f"Xray exited (code {code})")

    async def disconnect(self, params: DisconnectParams | None = None) -> DisconnectResult:
        if params is None:
            params = DisconnectParams()
        if not isinstance(params, DisconnectParams):
            params = DisconnectParams.from_dict(params)  # type: ignore[arg-type]
        async with self._lock:
            await self._stop_current(force=params.force)
            self._server = None
            self._stats = ConnectionStats()
            self._tunnel_status = None
            self._set_state(ConnectionStatus.DISCONNECTED)
            self._log("info", "disconnected")
            return DisconnectResult(accepted=True, state=self.state)

    async def _stop_current(self, *, force: bool = False) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._stats_task is not None:
            self._stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stats_task
            self._stats_task = None
        process = self._process
        self._process = None
        if process is not None:
            with contextlib.suppress(Exception):
                await process.stop(force=force)
        if self._tunnel_manager is not None and self._tunnel_manager.active:
            with contextlib.suppress(Exception):
                await self._tunnel_manager.down()
        self._connected_at = None

    async def wait_for_state(
        self, status: ConnectionStatus, *, timeout: float = 10.0
    ) -> ConnectionState:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state.status is status:
                return self.state
            await asyncio.sleep(0.01)
        if self.state.status is status:
            return self.state
        raise TimeoutError(f"state did not become {status.value} within {timeout}s")

    async def status(self) -> StatusResult:
        stats = self._stats
        if self.state.status is ConnectionStatus.CONNECTED:
            stats = await self._collect_stats()
            self._stats = stats
        tunnel_interface = None
        tunnel_handshake = None
        if self._data_plane is DataPlane.TUN:
            if self._tunnel_manager is not None:
                with contextlib.suppress(Exception):
                    self._tunnel_status = await self._tunnel_manager.status()
            if self._tunnel_status is not None:
                tunnel_interface = self._tunnel_status.interface
                tunnel_handshake = self._tunnel_status.latest_handshake
        process = self._process
        return StatusResult(
            state=self.state,
            stats=stats,
            server=self._server,
            routing_mode=self._settings().routing_mode,
            xray_pid=getattr(process, "pid", None) if process is not None else None,
            xray_version=None,
            kill_switch_active=bool(
                self._kill_switch is not None and self._kill_switch.active
            ),
            data_plane=self._data_plane,
            tunnel_interface=tunnel_interface,
            tunnel_handshake=tunnel_handshake,
        )

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.disconnect(DisconnectParams(force=True))
        provider = self._edge_provider
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.close()


__all__ = ["ConnectionError", "ConnectionManager"]
