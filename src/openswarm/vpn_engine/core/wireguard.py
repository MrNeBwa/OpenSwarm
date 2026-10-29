"""WireGuard projection and optional ``wg-quick`` system mode."""

from __future__ import annotations

import contextlib
import ipaddress
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..schema import Server, Settings
from .privileges import CommandRunner, SubprocessRunner

if TYPE_CHECKING:
    from .tunnel import TunnelProfile


class WireGuardError(RuntimeError):
    """WireGuard config/CLI failure."""


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _first_edge(edges: Iterable[Any]) -> str | None:
    for item in edges:
        text = str(item).strip()
        if not text:
            continue
        if "/" in text:
            text = text.split("/", 1)[0]
        try:
            ipaddress.ip_address(text.strip("[]"))
        except ValueError:
            continue
        return text.strip("[]")
    return None


def _format_endpoint(addr: str, port: int) -> str:
    text = addr.strip()
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    elif "/" in text:
        text = text.split("/", 1)[0]
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError as exc:
        raise WireGuardError("endpoint address must be an IP literal") from exc
    if isinstance(parsed, ipaddress.IPv6Address):
        return f"[{parsed}]:{int(port)}"
    return f"{parsed}:{int(port)}"


def _is_ip(value: Any) -> bool:
    try:
        ipaddress.ip_address(str(value).strip().strip("[]").split("/", 1)[0])
    except ValueError:
        return False
    return True


def _clamp_mtu(value: Any, fallback: int = 1420) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        number = int(fallback or 1420)
    return max(576, min(9000, number))


def _clean_address(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "/" in text:
            ipaddress.ip_interface(text)
        else:
            ipaddress.ip_address(text)
    except ValueError:
        return None
    return text


def _clean_route(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None
    return text


def _install_addresses(config: Mapping[str, Any], data: Mapping[str, Any]) -> list[str]:
    addresses: list[str] = []
    addr = config.get("addr")
    if isinstance(addr, Mapping):
        for key in ("ipv4", "ipv6"):
            cleaned = _clean_address(addr.get(key))
            if cleaned:
                addresses.append(cleaned)
    if not addresses:
        fallback = data.get("addresses")
        if isinstance(fallback, Mapping):
            for key in ("ipv4", "ipv6"):
                cleaned = _clean_address(fallback.get(key))
                if cleaned:
                    addresses.append(cleaned)
    return addresses


def profile_from_install(
    data: Mapping[str, Any],
    *,
    private_key: str,
    country: str = "",
    dns_servers: Sequence[str] | None = None,
    settings: Settings | None = None,
    ipv6_available: bool = True,
    full_tunnel: bool = True,
) -> dict[str, Any]:
    """Project the Android ``install`` response into a wg-quick profile."""

    config = data.get("config")
    if not isinstance(config, Mapping) or not config.get("peer"):
        raise WireGuardError("install response has no WireGuard peer")
    edge = _first_edge(data.get("edges", []))
    if edge is None:
        raise WireGuardError("install response has no edge address")
    if not ipv6_available and ":" in edge:
        edge = None
        for candidate in data.get("edges", []):
            try:
                if ipaddress.ip_address(str(candidate).strip().strip("[]").split("/", 1)[0]).version == 4:
                    edge = str(candidate).strip().strip("[]").split("/", 1)[0]
                    break
            except ValueError:
                continue
        if edge is None:
            raise WireGuardError("no IPv4 edge address is available")
    port = int(config.get("port") or 0)
    if not 1 <= port <= 65535:
        raise WireGuardError("install response has an invalid WireGuard port")

    addresses = _install_addresses(config, data)
    if not ipv6_available:
        addresses = [item for item in addresses if ":" not in item]
    if not addresses:
        addresses = ["10.66.0.2/32"]

    allowed: list[str] = []
    routes = data.get("routes")
    if isinstance(routes, (list, tuple)):
        for item in routes:
            if isinstance(item, Mapping):
                cleaned = _clean_route(item.get("addr"))
            else:
                cleaned = _clean_route(item)
            if cleaned:
                allowed.append(cleaned)
    if not allowed:
        allowed = ["0.0.0.0/0", "::/0"]
    if full_tunnel:
        for required in ("0.0.0.0/0", "::/0"):
            if required not in allowed:
                allowed.append(required)
    if not ipv6_available:
        allowed = [item for item in allowed if ":" not in item]

    dns = [
        str(item).strip()
        for item in (dns_servers or (settings.dns_servers if settings else []))
        if _is_ip(item)
    ]
    mtu = _clamp_mtu(config.get("mtu"), settings.mtu if settings else 1420)
    return {
        "country": country,
        "interface": {
            "private_key": private_key,
            "addresses": addresses,
            "dns": dns,
            "mtu": mtu,
        },
        "peer": {
            "public_key": str(config.get("peer")),
            "endpoint": _format_endpoint(edge, port),
            "allowed_ips": allowed,
            "persistent_keepalive": 25,
        },
    }


def build_tunnel_profile(
    data: Mapping[str, Any],
    settings: Settings,
    *,
    private_key: str,
    ipv6_available: bool,
) -> TunnelProfile:
    """Build a :class:`TunnelProfile` from an ``install`` response."""

    from .tunnel import TunnelProfile

    profile = profile_from_install(
        data,
        private_key=private_key,
        country=str(data.get("country", "")),
        dns_servers=settings.dns_servers,
        settings=settings,
        ipv6_available=ipv6_available,
        full_tunnel=True,
    )
    interface = profile["interface"]
    peer = profile["peer"]
    return TunnelProfile(
        private_key=str(interface["private_key"]),
        addresses=tuple(interface["addresses"]),
        dns=tuple(interface["dns"]),
        mtu=int(interface["mtu"]),
        peer_public_key=str(peer["public_key"]),
        endpoint=str(peer["endpoint"]),
        allowed_ips=tuple(peer["allowed_ips"]),
        persistent_keepalive=int(peer.get("persistent_keepalive", 25)),
    )


def build_wireguard_config(profile: Mapping[str, Any]) -> str:
    interface = profile.get("interface")
    peer = profile.get("peer")
    if not isinstance(interface, Mapping) or not isinstance(peer, Mapping):
        raise WireGuardError("profile must contain interface and peer")
    private_key = str(interface.get("private_key", "")).strip()
    public_key = str(peer.get("public_key", "")).strip()
    endpoint = str(peer.get("endpoint", "")).strip()
    if not private_key or not public_key or not endpoint:
        raise WireGuardError("private key, public key and endpoint are required")
    if "\n" in private_key or "\n" in public_key or "\n" in endpoint:
        raise WireGuardError("WireGuard values must be single-line")

    lines = ["[Interface]", f"PrivateKey = {private_key}"]
    addresses = _list(interface.get("addresses"))
    if addresses:
        lines.append("Address = " + ", ".join(addresses))
    dns = _list(interface.get("dns"))
    if dns:
        lines.append("DNS = " + ", ".join(dns))
    mtu = interface.get("mtu")
    try:
        if int(mtu or 0) > 0:
            lines.append(f"MTU = {int(mtu)}")
    except (TypeError, ValueError):
        pass

    lines.extend(["[Peer]", f"PublicKey = {public_key}", f"Endpoint = {endpoint}"])
    allowed = _list(peer.get("allowed_ips"))
    if allowed:
        lines.append("AllowedIPs = " + ", ".join(allowed))
    keepalive = peer.get("persistent_keepalive")
    try:
        if int(keepalive or 0) > 0:
            lines.append(f"PersistentKeepalive = {int(keepalive)}")
    except (TypeError, ValueError):
        pass
    if peer.get("preshared_key"):
        lines.append(f"PresharedKey = {peer['preshared_key']}")
    return "\n".join(lines) + "\n"


@dataclass
class WireGuardResult:
    ok: bool
    active: bool
    command: list[str] = field(default_factory=list)
    message: str = ""


class WireGuardManager:
    """Optional system WireGuard mode.  Always requires explicit user action."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        wg_quick_path: str | None = None,
        prefix: Sequence[str] | None = None,
        search=None,
        interface: str = "ninjavpn0",
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self._search = search or shutil.which
        self.wg_quick_path = (
            wg_quick_path if wg_quick_path is not None else self._search("wg-quick")
        )
        self.prefix = list(prefix or [])
        self.interface = interface
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def require_tools(self) -> str:
        if not self.wg_quick_path:
            raise WireGuardError(
                "wg-quick не найден: установите wireguard-tools "
                "(pacman -S wireguard-tools)"
            )
        return self.wg_quick_path

    def _write_config(self, text: str, config_dir: Path | None) -> Path:
        directory = config_dir or Path(tempfile.mkdtemp(prefix="ninjavpn-wg-"))
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = directory / f"{self.interface}.conf"
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(directory))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
        return path

    def up(
        self,
        text: str,
        *,
        explicit: bool = False,
        config_dir: str | Path | None = None,
    ) -> WireGuardResult:
        if not explicit:
            return WireGuardResult(
                False, self._active, [], "WireGuard-режим требует явного действия пользователя"
            )
        wg_quick = self.require_tools()
        try:
            path = self._write_config(
                text, Path(config_dir).expanduser() if config_dir is not None else None
            )
        except OSError as exc:
            return WireGuardResult(False, False, [], f"не удалось записать конфиг: {exc}")
        argv = [*self.prefix, wg_quick, "up", str(path)]
        outcome = self.runner.run(argv)
        if outcome.returncode != 0:
            return WireGuardResult(
                False, False, argv, (outcome.stderr or outcome.stdout or "wg-quick up failed").strip()
            )
        self._active = True
        return WireGuardResult(True, True, argv, "WireGuard поднят")

    def down(self, *, explicit: bool = False, config_dir: str | Path | None = None) -> WireGuardResult:
        if not explicit:
            return WireGuardResult(
                False, self._active, [], "снятие WireGuard требует явного действия пользователя"
            )
        if not self._active:
            return WireGuardResult(True, False, [], "WireGuard не был активен")
        wg_quick = self.require_tools()
        argv = [*self.prefix, wg_quick, "down", self.interface]
        outcome = self.runner.run(argv)
        if outcome.returncode != 0:
            return WireGuardResult(
                False, True, argv, (outcome.stderr or outcome.stdout or "wg-quick down failed").strip()
            )
        self._active = False
        return WireGuardResult(True, False, argv, "WireGuard опущен")


def profile_for_server(
    server: Server,
    settings: Settings,
    *,
    private_key: str,
    endpoint: str | None = None,
    allowed_ips: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a profile from a :class:`Server` (used by CLI/import)."""

    addresses = _list(server.params.get("address")) or ["10.66.0.2/32"]
    return {
        "interface": {
            "private_key": private_key,
            "addresses": addresses,
            "dns": list(settings.dns_servers),
            "mtu": settings.mtu,
        },
        "peer": {
            "public_key": str(server.params.get("public_key", "")),
            "endpoint": endpoint or f"{server.host}:{server.port}",
            "allowed_ips": list(allowed_ips or _list(server.params.get("allowed_ips")) or ["0.0.0.0/0", "::/0"]),
            "persistent_keepalive": int(server.params.get("keepalive", 25) or 0),
        },
    }


__all__ = [
    "WireGuardError",
    "WireGuardManager",
    "WireGuardResult",
    "build_wireguard_config",
    "profile_for_server",
    "profile_from_install",
]
