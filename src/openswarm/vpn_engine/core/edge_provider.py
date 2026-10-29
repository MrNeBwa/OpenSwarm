"""Turn native edge ``manifest``/``install`` responses into public models."""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import os
import socket
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from ..schema import (
    EdgePathMode,
    Protocol,
    Security,
    Server,
    Settings,
    Transport,
    default_config_dir,
)
from .builtin_edge import builtin_servers as _builtin_servers
from .builtin_edge import load_builtin_config, load_builtin_payload
from .edge_client import (
    EdgeClient,
    EdgeError,
    EdgePathUnavailable,
    EdgePinMismatch,
)
from .edge_config import (
    MAX_EDGE_FILE_BYTES,
    EdgeConfig,
    EdgeConfigError,
    EdgePath,
    EdgePathType,
    decode_edge_container,
    load_edge_config,
    parse_edge_config,
)
from .identity import DeviceIdentity, load_identity
from .tunnel import TunnelProfile
from .wireguard import build_tunnel_profile

EDGE_PROVIDER = "ninja-edge"
EDGE_PORT = 443


@dataclass(frozen=True)
class EdgeInstall:
    response: Mapping[str, Any]
    path: EdgePath
    edge_ip: str
    wg_private_key: str = field(repr=False)
    profile: TunnelProfile = field(repr=False)


def ipv6_available() -> bool:
    """Best-effort check that the host can open an IPv6 socket."""

    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    except OSError:
        return False
    try:
        sock.bind(("::1", 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _generate_wireguard_keypair() -> tuple[str, str]:
    private = x25519.X25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return (
        base64.b64encode(private_raw).decode("ascii"),
        base64.b64encode(public_raw).decode("ascii"),
    )


def _clean_edge(value: object) -> str | None:
    text = str(value or "").strip().strip("[]")
    if "/" in text:
        text = text.split("/", 1)[0]
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return None
    return text


class EdgeProvider:
    def __init__(
        self,
        settings: Settings,
        identity: DeviceIdentity | None = None,
        *,
        client_factory: Callable[[EdgeConfig], EdgeClient] | None = None,
    ) -> None:
        self.settings = settings
        self.identity = identity or load_identity()
        self._client_factory = client_factory
        self._client: EdgeClient | None = None
        self.last_servers: list[Server] = []
        self.last_config: EdgeConfig | None = None
        self.last_config_source = ""

    # -- configuration -----------------------------------------------------
    def config_path(self) -> Path:
        configured = self.settings.edge_config_path.strip()
        if configured:
            return Path(configured).expanduser()
        return default_config_dir() / "edge.json"

    def load_config(self, *, fallback_builtin: bool = False) -> EdgeConfig:
        path = self.config_path()
        try:
            config = load_edge_config(path, device_id=self.identity.hard)
        except EdgeConfigError as exc:
            if not fallback_builtin:
                raise EdgeError(str(exc), code=exc.code) from exc
            try:
                config = load_builtin_config(self.identity.hard)
            except EdgeConfigError as builtin_exc:
                raise EdgeError(str(builtin_exc), code=builtin_exc.code) from builtin_exc
            except Exception as builtin_exc:
                raise EdgeError(
                    "edge config cannot be loaded",
                    code="EDGE_CONFIG_INVALID",
                ) from builtin_exc
            self.last_config_source = "bundled"
        except EdgeError:
            raise
        except Exception as exc:
            raise EdgeError(
                "edge config cannot be loaded", code="EDGE_CONFIG_INVALID"
            ) from exc
        else:
            self.last_config_source = "file"
        self.last_config = config
        return config

    def builtin_servers(self) -> list[Server]:
        """The server extracted from the packaged APK blob (may be empty)."""

        try:
            config = load_builtin_config(self.identity.hard)
        except EdgeConfigError as exc:
            raise EdgeError(str(exc), code=exc.code) from exc
        return _builtin_servers(config)

    def _read_source(self, source: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(source, flags)
        except FileNotFoundError as exc:
            raise EdgeConfigError(
                f"edge config not found: {source}", code="EDGE_CONFIG_MISSING"
            ) from exc
        except OSError as exc:
            raise EdgeConfigError("cannot open edge config", code="EDGE_CONFIG_INVALID") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise EdgeConfigError("edge config must be a regular file")
            if info.st_uid != os.getuid():
                raise EdgeConfigError("edge config must be owned by the current user")
            if info.st_size > MAX_EDGE_FILE_BYTES:
                raise EdgeConfigError("edge file is larger than 1 MiB")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                payload = handle.read(MAX_EDGE_FILE_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(payload) > MAX_EDGE_FILE_BYTES:
            raise EdgeConfigError("edge file is larger than 1 MiB")
        return payload

    @staticmethod
    def _write_private(path: Path, payload: bytes) -> None:
        parent = path.parent
        try:
            info = parent.lstat()
        except FileNotFoundError:
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise EdgeConfigError("edge config directory is unsafe", code="EDGE_CONFIG_INVALID")
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    @staticmethod
    def _normalise_payload(payload: bytes) -> bytes:
        """Return decrypted, pretty-printed JSON without reordering fields.

        Keeping the original path records byte-for-byte (after JSON parsing)
        preserves the deterministic RAND/MASK host derivation, which hashes
        the canonical record.
        """

        stripped = payload.lstrip()
        raw = payload if stripped.startswith(b"{") else decode_edge_container(payload)
        try:
            parsed = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgeConfigError("edge config is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise EdgeConfigError("edge config root must be an object")
        return (
            json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    def import_config(
        self, source: str | Path, *, force: bool = False, trusted: bool = False
    ) -> EdgeConfig:
        """Validate ``edge.bin``/``edge.json`` and store decrypted JSON privately.

        The destination is always plain JSON with mode ``0600``.  The source
        container itself is never copied; its AES key provides no protection
        and would only obscure the stored pin set.

        ``trusted`` is reserved for the packaged blob (usually root-owned on a
        system installation), which must not be subjected to the
        current-user-ownership check for user-supplied files.
        """

        source_path = Path(source).expanduser()
        payload = (
            load_builtin_payload() if trusted else self._read_source(source_path)
        )
        config = parse_edge_config(payload, device_id=self.identity.hard)
        destination = self.config_path()
        try:
            same_file = destination.exists() and os.path.samefile(source_path, destination)
        except OSError:
            same_file = False
        if not same_file:
            if destination.exists() and not force:
                raise EdgeConfigError(
                    f"edge config already exists: {destination}",
                    code="EDGE_CONFIG_EXISTS",
                )
            if destination.is_symlink():
                raise EdgeConfigError(
                    "refusing to replace symlinked edge config",
                    code="EDGE_CONFIG_INVALID",
                )
            try:
                self._write_private(destination, self._normalise_payload(payload))
            except OSError as exc:
                raise EdgeConfigError(
                    "cannot write edge config", code="EDGE_CONFIG_INVALID"
                ) from exc
        self.last_config = config
        self.last_config_source = "file"
        return config

    def _make_client(self, config: EdgeConfig) -> EdgeClient:
        if self._client_factory is not None:
            return self._client_factory(config)
        return EdgeClient(
            config,
            self.identity,
            mask_enabled=self.settings.edge_path is EdgePathMode.MASK,
        )

    def _preferred(self) -> EdgePathType | None:
        mode = self.settings.edge_path
        if mode is EdgePathMode.AUTO:
            return None
        try:
            return EdgePathType(mode.value)
        except ValueError:
            return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- refresh -----------------------------------------------------------
    async def refresh(self) -> list[Server]:
        config = self.load_config(fallback_builtin=True)
        client = self._make_client(config)
        self._client = client
        preferred = self._preferred()
        try:
            if preferred is None:
                available = await client.ping()
                if not available:
                    raise EdgePathUnavailable("edge ping failed on every path")
            else:
                await client.ping(preferred)
            # Stay on the path that answered the ping when auto-selection had
            # to skip an unavailable (for example pinned-out) main path.
            manifest_path = preferred
            if (
                preferred is None
                and client.last_path is not None
                and client.last_path.type is not EdgePathType.MAIN
            ):
                manifest_path = client.last_path.type
            payload = await client.manifest(path=manifest_path)
        except EdgePinMismatch as exc:
            raise EdgeError(
                "edge config is stale: TLS pin mismatch; "
                "import a fresh edge.bin or use a subscription",
                code="EDGE_TLS_PIN_MISMATCH",
            ) from exc
        except EdgeError:
            raise
        path = client.last_path
        if path is None:
            path = next(iter(config.paths.values()))
        servers = self.servers_from_manifest(payload, path=path)
        self.last_servers = servers
        return servers

    # -- catalog -----------------------------------------------------------
    def servers_from_manifest(
        self, payload: Mapping[str, Any], *, path: EdgePath
    ) -> list[Server]:
        if not isinstance(payload, Mapping):
            raise EdgeError("manifest must be an object", code="EDGE_HTTP_ERROR")
        datacenters = payload.get("datacenters", [])
        if not isinstance(datacenters, list):
            raise EdgeError(
                "manifest.datacenters must be a list", code="EDGE_HTTP_ERROR"
            )

        best: dict[str, tuple[int, int, bool, bool, int]] = {}
        for item in datacenters:
            if not isinstance(item, Mapping):
                continue
            country = str(item.get("country", "")).strip().upper()
            if len(country) != 2 or not (country.isascii() and country.isalpha()):
                continue
            latency = item.get("latency")
            if not isinstance(latency, Mapping):
                latency = {}
            pref = self._latency(latency.get("pref"))
            real = self._latency(latency.get("real"))
            premium = bool(item.get("premium", False))
            expensive = bool(item.get("expensive", False))
            candidate = (1 if premium else 0, pref, real, premium, expensive)
            existing = best.get(country)
            if existing is None:
                best[country] = (candidate[0], candidate[1], premium, expensive, real)
                continue
            # Existing tuple layout: premium_rank, pref, premium, expensive, real.
            if candidate[0] < existing[0] or (
                candidate[0] == existing[0] and candidate[1] < existing[1]
            ):
                best[country] = (candidate[0], candidate[1], premium, expensive, real)

        servers: list[Server] = []
        for country in sorted(best, key=lambda code: (best[code][0], best[code][1], code)):
            _premium_rank, pref, premium, expensive, real = best[country]
            tags = ["edge"]
            if premium:
                tags.append("premium")
            if expensive:
                tags.append("expensive")
            servers.append(
                Server(
                    id=f"edge-{country.lower()}",
                    name=country,
                    host=path.tls_host,
                    port=EDGE_PORT,
                    protocol=Protocol.WIREGUARD,
                    transport=Transport.TCP,
                    security=Security.TLS,
                    country=country,
                    country_code=country,
                    premium=premium,
                    latency_ms=pref or None,
                    tags=tags,
                    params={
                        "provider": EDGE_PROVIDER,
                        "edge_path": path.type.value,
                        "latency_pref": pref,
                        "latency_real": real,
                        "expensive": expensive,
                    },
                )
            )
        return servers

    @staticmethod
    def _latency(value: object) -> int:
        try:
            number = int(str(value))
        except (TypeError, ValueError):
            return 0
        return max(0, number)

    # -- install -----------------------------------------------------------
    async def install(self, server: Server) -> EdgeInstall:
        if server.params.get("provider") != EDGE_PROVIDER:
            raise EdgeError(
                "server is not provided by the native edge layer",
                code="SERVER_NOT_FOUND",
            )
        country = (server.country_code or "").strip().upper()
        if not country:
            country = str(server.params.get("country", "")).strip().upper()
        if len(country) != 2 or not (country.isascii() and country.isalpha()):
            raise EdgeError("server has no country code", code="INVALID_PARAMS")

        private_key, public_key = _generate_wireguard_keypair()
        client = self._client
        if client is None:
            config = self.load_config()
            client = self._make_client(config)
            self._client = client
        preferred = self._preferred()
        try:
            response = await client.install(country, public_key, path=preferred)
        except EdgeError:
            raise

        ips: list[tuple[int, str]] = []
        edges = response.get("edges")
        if not isinstance(edges, (list, tuple)):
            raise EdgeError(
                "install response has no edges", code="EDGE_PATH_UNAVAILABLE"
            )
        for item in edges:
            cleaned = _clean_edge(item)
            if cleaned is None:
                continue
            ips.append((6 if ":" in cleaned else 4, cleaned))
        if not ips:
            raise EdgeError(
                "install response has no usable edge address",
                code="EDGE_PATH_UNAVAILABLE",
            )

        has_v6 = ipv6_available()
        if has_v6:
            edge_ip = next((addr for family, addr in ips if family == 6), ips[0][1])
        else:
            edge_ip = next((addr for family, addr in ips if family == 4), "")
        if not edge_ip:
            raise EdgeError(
                "install response offers IPv6 only, but IPv6 is unavailable",
                code="EDGE_PATH_UNAVAILABLE",
            )

        config = response.get("config")
        if not isinstance(config, Mapping):
            raise EdgeError("install response has no config", code="EDGE_CONFIG_INVALID")
        if not config.get("peer"):
            raise EdgeError("install response has no peer", code="EDGE_CONFIG_INVALID")
        try:
            port = int(config.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            raise EdgeError("install response has an invalid port", code="EDGE_CONFIG_INVALID")
        if not has_v6 and edge_ip and ":" in edge_ip:
            raise EdgeError("IPv6 edge is unavailable", code="EDGE_PATH_UNAVAILABLE")

        profile = build_tunnel_profile(
            response,
            self.settings,
            private_key=private_key,
            ipv6_available=has_v6,
        )
        # The provider knows which IP it selected; keep it in the endpoint even
        # if the install response lists a different first address.
        endpoint_host = f"[{edge_ip}]" if ":" in edge_ip else edge_ip
        profile = TunnelProfile(
            interface=profile.interface,
            private_key=profile.private_key,
            addresses=profile.addresses,
            dns=profile.dns,
            mtu=profile.mtu,
            peer_public_key=profile.peer_public_key,
            endpoint=f"{endpoint_host}:{port}",
            allowed_ips=profile.allowed_ips,
            persistent_keepalive=profile.persistent_keepalive,
        )
        path = client.last_path
        if path is None:
            path = next(iter(client.config.paths.values()))
        return EdgeInstall(
            response=response,
            path=path,
            edge_ip=edge_ip,
            wg_private_key=private_key,
            profile=profile,
        )


__all__ = [
    "EDGE_PROVIDER",
    "EdgeInstall",
    "EdgeProvider",
    "ipv6_available",
]
