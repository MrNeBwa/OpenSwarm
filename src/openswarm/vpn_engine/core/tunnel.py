"""System-wide WireGuard TUN lifecycle through the privileged helper."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..schema import default_state_dir
from .privileges import (
    CommandRunner,
    PrivilegedHelperClient,
    SubprocessRunner,
)
from .wireguard import WireGuardError, build_wireguard_config

INTERFACE_NAME = "ninjavpn0"
TUNNEL_MARKER = "# ninjavpn tunnel"
HANDSHAKE_TIMEOUT = 15.0
_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")


class TunnelError(RuntimeError):
    code = "TUN_START_FAILED"


@dataclass(frozen=True)
class TunnelProfile:
    interface: str = INTERFACE_NAME
    private_key: str = field(default="", repr=False)
    addresses: tuple[str, ...] = ()
    dns: tuple[str, ...] = ()
    mtu: int = 1420
    peer_public_key: str = ""
    endpoint: str = ""
    allowed_ips: tuple[str, ...] = ("0.0.0.0/0", "::/0")
    persistent_keepalive: int = 25

    def __post_init__(self) -> None:
        if not _INTERFACE_RE.match(self.interface):
            raise TunnelError("invalid tunnel interface name")
        if not self.private_key or "\n" in self.private_key:
            raise TunnelError("invalid WireGuard private key")
        if not self.peer_public_key or "\n" in self.peer_public_key:
            raise TunnelError("invalid WireGuard peer key")
        if not self.endpoint or "\n" in self.endpoint:
            raise TunnelError("invalid WireGuard endpoint")
        host, separator, port = self.endpoint.rpartition(":")
        if (
            not separator
            or not port.isdigit()
            or not 1 <= int(port) <= 65535
            or not host.strip("[]")
            or any(character.isspace() for character in host)
        ):
            raise TunnelError("invalid WireGuard endpoint")
        if not 576 <= int(self.mtu) <= 9000:
            raise TunnelError("invalid tunnel MTU")

    def as_profile(self) -> dict[str, object]:
        return {
            "interface": {
                "private_key": self.private_key,
                "addresses": list(self.addresses),
                "dns": list(self.dns),
                "mtu": int(self.mtu),
            },
            "peer": {
                "public_key": self.peer_public_key,
                "endpoint": self.endpoint,
                "allowed_ips": list(self.allowed_ips),
                "persistent_keepalive": int(self.persistent_keepalive),
            },
        }

    def render(self) -> str:
        try:
            body = build_wireguard_config(self.as_profile())
        except WireGuardError as exc:
            raise TunnelError(str(exc)) from exc
        return TUNNEL_MARKER + "\n" + body


@dataclass(frozen=True)
class TunnelResult:
    ok: bool
    active: bool
    message: str = ""
    code: str = ""


@dataclass(frozen=True)
class TunnelStatus:
    active: bool
    interface: str
    endpoint: str = ""
    latest_handshake: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
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


def _result_data(result: object) -> dict[str, object]:
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return dict(data)
    text = getattr(result, "stdout", "") or ""
    text = text.strip()
    if not text:
        return {}
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


class TunnelManager:
    """Own the WireGuard interface and its private state file."""

    def __init__(
        self,
        *,
        helper: PrivilegedHelperClient | None = None,
        runner: CommandRunner | None = None,
        search=None,
        interface: str = INTERFACE_NAME,
        state_dir: str | Path | None = None,
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        poll_interval: float = 0.2,
    ) -> None:
        self._search = search or shutil.which
        self.runner = runner or SubprocessRunner()
        self.helper = helper or PrivilegedHelperClient(
            runner=self.runner,
            search=self._search,
        )
        if not _INTERFACE_RE.match(interface):
            raise ValueError("invalid interface name")
        self.interface = interface
        self.state_dir = (
            Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        )
        self.config_path = self.state_dir / "tunnel.conf"
        self.state_path = self.state_dir / "tunnel.state.json"
        self.handshake_timeout = float(handshake_timeout)
        self.poll_interval = max(0.01, float(poll_interval))
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def state_exists(self) -> bool:
        """True when a previous ``up`` left an owned state file behind."""

        return self.state_path.exists()

    def interface_exists(self) -> bool:
        """True when the WireGuard interface currently exists on this host."""

        return Path(f"/sys/class/net/{self.interface}").exists()

    def _tool(self, name: str) -> str | None:
        try:
            return self._search(name)
        except OSError:
            return None

    def require_tools(self) -> str:
        wg_quick = self._tool("wg-quick")
        if not wg_quick:
            raise TunnelError("wg-quick не найден (установите wireguard-tools)")
        return wg_quick

    def _dns_tool(self) -> str | None:
        for name in ("resolvectl", "resolvconf"):
            if self._tool(name):
                return name
        return None

    def _load_state(self) -> dict[str, object] | None:
        try:
            info = self.state_path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_state(self, profile: TunnelProfile) -> None:
        _atomic_write(
            self.state_path,
            json.dumps(
                {
                    "version": 1,
                    "interface": profile.interface,
                    "endpoint": profile.endpoint,
                    "created_at": time.time(),
                },
                indent=2,
            )
            + "\n",
        )

    def _remove_state(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.state_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            self.config_path.unlink()

    async def up(
        self,
        profile: TunnelProfile,
        *,
        kill_switch: bool,
        dns_leak_protection: bool,
    ) -> TunnelResult:
        if self._active:
            return TunnelResult(True, True, "tunnel is already active")
        if not isinstance(profile, TunnelProfile):
            return TunnelResult(False, False, "invalid tunnel profile", "TUN_START_FAILED")
        try:
            self.require_tools()
        except TunnelError as exc:
            return TunnelResult(False, False, str(exc), "TUN_TOOLS_MISSING")

        if dns_leak_protection:
            if not profile.dns:
                return TunnelResult(
                    False,
                    False,
                    "в профиле нет DNS-серверов: DNS-защита невозможна",
                    "DNS_UNSUPPORTED",
                )
            for server in profile.dns:
                try:
                    ipaddress.ip_address(str(server).strip().strip("[]"))
                except ValueError:
                    return TunnelResult(
                        False,
                        False,
                        "DNS для TUN должен быть IP-адресом",
                        "DNS_UNSUPPORTED",
                    )
        dns_tool = self._dns_tool()
        if dns_leak_protection and dns_tool is None:
            return TunnelResult(
                False,
                False,
                "resolvectl/resolvconf не найден: DNS-защита невозможна",
                "DNS_UNAVAILABLE",
            )

        old_state = self._load_state()
        if old_state is not None:
            self._active = True
            await self.down()

        try:
            text = profile.render()
            _atomic_write(self.config_path, text)
        except (OSError, TunnelError) as exc:
            return TunnelResult(
                False, False, f"не удалось записать tunnel.conf: {exc}", "TUN_START_FAILED"
            )

        try:
            result = self.helper.tunnel_up(self.config_path, strict=bool(kill_switch))
        except Exception as exc:  # helper/runner must never leak keys
            self._remove_state()
            return TunnelResult(
                False, False, f"helper не запустился: {exc.__class__.__name__}", "TUN_PERMISSION_DENIED"
            )
        if not result.ok:
            self._remove_state()
            message = (result.message or "tunnel up failed").strip()
            lowered = message.lower()
            if "pkexec" in lowered or "not authorized" in lowered or "authentication" in lowered:
                code = "TUN_PERMISSION_DENIED"
            elif "wg-quick" in lowered or "not found" in lowered:
                code = "TUN_TOOLS_MISSING"
            else:
                code = "TUN_START_FAILED"
            return TunnelResult(False, False, message, code)

        self._active = True
        with contextlib.suppress(OSError):
            self._write_state(profile)

        if await self._wait_handshake():
            return TunnelResult(True, True, "tunnel is up")
        await self.down()
        return TunnelResult(
            False,
            False,
            "WireGuard handshake не появился за отведённое время",
            "TUN_HANDSHAKE_TIMEOUT",
        )

    async def _wait_handshake(self) -> bool:
        deadline = time.monotonic() + max(0.0, self.handshake_timeout)
        while True:
            status = await self.status()
            if status.latest_handshake > 0:
                return True
            if time.monotonic() >= deadline:
                return False
            import asyncio

            await asyncio.sleep(self.poll_interval)

    async def down(self) -> TunnelResult:
        if not self._active and self._load_state() is None:
            return TunnelResult(True, False, "tunnel is not active")
        try:
            result = self.helper.tunnel_down()
        except Exception as exc:
            return TunnelResult(
                False, self._active, f"helper не запустился: {exc.__class__.__name__}", "TUN_START_FAILED"
            )
        if result.ok:
            self._active = False
            self._remove_state()
            return TunnelResult(True, False, result.message or "tunnel is down")
        return TunnelResult(False, self._active, result.message or "tunnel down failed", "TUN_START_FAILED")

    async def status(self) -> TunnelStatus:
        state = self._load_state() or {}
        endpoint = str(state.get("endpoint", ""))
        try:
            result = self.helper.tunnel_status()
        except Exception:
            result = None
        data = _result_data(result) if result is not None else {}
        active = bool(data.get("active", self._active))
        interface = str(data.get("interface") or self.interface)
        handshake = _int(data.get("latest_handshake", data.get("handshake", 0)))
        rx = _int(data.get("rx_bytes", data.get("rx", 0)))
        tx = _int(data.get("tx_bytes", data.get("tx", 0)))
        if result is not None:
            text = getattr(result, "stdout", "") or ""
            for line in text.splitlines():
                parts = line.split("\t")
                if len(parts) >= 8 and parts[0] == interface:
                    handshake = max(handshake, _int(parts[4]))
                    rx = max(rx, _int(parts[5]))
                    tx = max(tx, _int(parts[6]))
        return TunnelStatus(
            active=active or self._active,
            interface=interface,
            endpoint=endpoint or str(data.get("endpoint", "")),
            latest_handshake=handshake,
            rx_bytes=rx,
            tx_bytes=tx,
        )

    async def cleanup(self) -> TunnelResult:
        state = self._load_state()
        try:
            result = self.helper.tunnel_cleanup()
        except Exception as exc:
            return TunnelResult(False, self._active, f"helper не запустился: {exc.__class__.__name__}", "TUN_STALE")
        self._remove_state()
        self._active = False
        if result.ok:
            return TunnelResult(True, False, result.message or "tunnel cleaned up")
        if state is None and "no state" in (result.message or "").lower():
            return TunnelResult(True, False, result.message)
        return TunnelResult(False, False, result.message or "tunnel cleanup failed", "TUN_STALE")


__all__ = [
    "HANDSHAKE_TIMEOUT",
    "INTERFACE_NAME",
    "TUNNEL_MARKER",
    "TunnelError",
    "TunnelManager",
    "TunnelProfile",
    "TunnelResult",
    "TunnelStatus",
]
