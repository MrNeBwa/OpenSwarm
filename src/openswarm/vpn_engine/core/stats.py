"""Traffic statistics helpers (Xray counters and WireGuard ``wg show``)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .privileges import CommandRunner, SubprocessRunner


@dataclass(frozen=True)
class WgPeerStats:
    interface: str
    public_key: str = ""
    endpoint: str = ""
    allowed_ips: str = ""
    latest_handshake: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    persistent_keepalive: str = "off"


@dataclass(frozen=True)
class TunnelStats:
    interface: str
    rx_bytes: int = 0
    tx_bytes: int = 0
    latest_handshake: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "interface": self.interface,
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
            "latest_handshake": self.latest_handshake,
        }


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def parse_wg_transfer(text: str) -> dict[str, tuple[int, int]]:
    """Parse ``wg show all transfer`` (``iface\\tpeer\\trx\\ttx``)."""

    result: dict[str, tuple[int, int]] = {}
    for line in str(text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        interface = parts[0].strip()
        if not interface:
            continue
        rx, tx = _int(parts[2]), _int(parts[3])
        old_rx, old_tx = result.get(interface, (0, 0))
        result[interface] = (old_rx + rx, old_tx + tx)
    return result


def parse_wg_latest_handshakes(text: str) -> dict[str, int]:
    """Parse ``wg show all latest-handshakes``."""

    result: dict[str, int] = {}
    for line in str(text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        interface = parts[0].strip()
        if not interface:
            continue
        value = _int(parts[2])
        result[interface] = max(result.get(interface, 0), value)
    return result


def parse_wg_dump(text: str) -> list[WgPeerStats]:
    """Parse ``wg show all dump`` (interface, private key, port, fwmark, peers)."""

    rows: list[WgPeerStats] = []
    for line in str(text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        rows.append(
            WgPeerStats(
                interface=parts[0],
                public_key=parts[1],
                endpoint=parts[3],
                allowed_ips=parts[4].replace(",", ", "),
                latest_handshake=_int(parts[5]),
                rx_bytes=_int(parts[6]),
                tx_bytes=_int(parts[7]),
                persistent_keepalive=parts[8],
            )
        )
    return rows


class StatsCollector:
    """Read interface counters without root.

    ``wg show`` usually needs no privileges for interfaces owned by the user
    session (WireGuard exposes counters through netlink).  Failures degrade to
    zeroes and never raise into the connection loop.
    """

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        interface: str = "ninjavpn0",
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.interface = interface
        self._last_rx = 0
        self._last_tx = 0
        self._last_at = time.monotonic()

    def sample(self) -> TunnelStats:
        # ``wg show <if> transfer`` omits the interface column; using the
        # ``all`` form keeps one parser for the documented 4/3-column layout.
        result = self.runner.run(["wg", "show", "all", "transfer"], timeout=3.0)
        if result.returncode != 0:
            return TunnelStats(self.interface)
        values = parse_wg_transfer(result.stdout)
        rx, tx = values.get(self.interface, (0, 0))
        handshake_result = self.runner.run(
            ["wg", "show", "all", "latest-handshakes"], timeout=3.0
        )
        handshakes = (
            parse_wg_latest_handshakes(handshake_result.stdout)
            if handshake_result.returncode == 0
            else {}
        )
        self._last_rx, self._last_tx = rx, tx
        self._last_at = time.monotonic()
        return TunnelStats(
            interface=self.interface,
            rx_bytes=rx,
            tx_bytes=tx,
            latest_handshake=handshakes.get(self.interface, 0),
        )

    def reset(self) -> None:
        self._last_rx = 0
        self._last_tx = 0
        self._last_at = time.monotonic()


__all__ = [
    "StatsCollector",
    "TunnelStats",
    "WgPeerStats",
    "parse_wg_dump",
    "parse_wg_latest_handshakes",
    "parse_wg_transfer",
]
