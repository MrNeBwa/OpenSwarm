"""The APK-provided ``edge.bin`` projected into one built-in server.

The blob is a shared data-plane profile, not a country catalog: it only yields
the fixed ``mask`` VLESS/Reality endpoint.  Credentials stay in memory and are
never written to the public server cache.

The bundled blob is optional.  It may be missing on installations that choose
not to ship it, and a broken blob must never prevent the client from starting.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import os
import re
from importlib import resources
from pathlib import Path

from ..schema import Server
from .edge_config import (
    EdgeConfig,
    EdgeConfigError,
    EdgePathType,
    load_edge_config,
    parse_edge_config,
)
from .servers import server_from_outbound

LOGGER = logging.getLogger(__name__)

BUILTIN_SOURCE = "apk"
BUILTIN_SERVER_ID = "edge-apk-mask"
BUILTIN_SERVER_NAME = "Ninja VPN (APK)"
BUILTIN_PROVIDER = "apk-edge"
ENV_EDGE_BIN = "NINJAVPN_EDGE_BIN"

_PLACEHOLDER = re.compile(r"%[A-Za-z0-9_]+%")


def bundled_edge_path() -> Path:
    """Path of the packaged ``data/edge.bin`` (may not exist)."""

    resource = resources.files("ninjavpn_client").joinpath("data", "edge.bin")
    return Path(str(resource))


def _read_bundled_bytes() -> bytes:
    try:
        resource = resources.files("ninjavpn_client").joinpath("data", "edge.bin")
        return resource.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise EdgeConfigError(
            f"bundled edge config not found: {bundled_edge_path()}",
            code="EDGE_CONFIG_MISSING",
        ) from exc


def load_builtin_payload() -> bytes:
    """Raw bytes of the built-in blob (or the explicit ``NINJAVPN_EDGE_BIN``).

    Packaged files are usually owned by root in a system installation, so this
    reader deliberately skips the user-ownership check that applies to files a
    user asks the CLI to import explicitly.
    """

    value = os.environ.get(ENV_EDGE_BIN, "").strip()
    if value:
        try:
            return Path(value).expanduser().read_bytes()
        except OSError as exc:
            raise EdgeConfigError(
                f"bundled edge config not found: {value}",
                code="EDGE_CONFIG_MISSING",
            ) from exc
    return _read_bundled_bytes()


def load_builtin_config(device_id: str, *, path: Path | None = None) -> EdgeConfig:
    """Load the bundled edge config without writing anything to disk.

    ``path`` (or ``NINJAVPN_EDGE_BIN``) overrides the packaged blob for tests
    and local development.
    """

    override = path
    if override is None:
        value = os.environ.get(ENV_EDGE_BIN, "").strip()
        if value:
            override = Path(value).expanduser()
    if override is not None:
        return load_edge_config(override, device_id=device_id)
    return parse_edge_config(load_builtin_payload(), device_id=device_id)


def _first_address(addrs: tuple[str, ...]) -> str:
    for addr in addrs:
        try:
            if ipaddress.ip_address(addr).version == 4:
                return addr
        except ValueError:
            continue
    return addrs[0] if addrs else ""


def builtin_servers(config: EdgeConfig) -> list[Server]:
    """Return the single MASK server, or ``[]`` when it cannot be built."""

    path = config.paths.get(EdgePathType.MASK)
    if path is None or path.xray is None:
        LOGGER.warning("bundled edge config has no usable mask path")
        return []

    address = _first_address(path.addrs)
    host = path.sni or path.host
    if not address or not host:
        LOGGER.warning("bundled edge mask path has no address or host")
        return []

    template = json.dumps(copy.deepcopy(dict(path.xray)), ensure_ascii=False)
    template = template.replace("%addr%", address).replace("%host%", host)
    if _PLACEHOLDER.search(template):
        LOGGER.warning("bundled edge mask template has unresolved placeholders")
        return []
    try:
        outbound = json.loads(template)
    except json.JSONDecodeError:
        LOGGER.warning("bundled edge mask template is not valid JSON")
        return []
    if not isinstance(outbound, dict):
        LOGGER.warning("bundled edge mask template is not an object")
        return []

    server = server_from_outbound(outbound, fallback_name=BUILTIN_SERVER_NAME)
    if server is None or not server.host or not server.port:
        LOGGER.warning("bundled edge mask template is not a supported proxy")
        return []

    server.id = BUILTIN_SERVER_ID
    server.name = BUILTIN_SERVER_NAME
    server.tags = ["builtin", "apk", "reality"]
    server.params["provider"] = BUILTIN_PROVIDER
    server.params["edge_path"] = EdgePathType.MASK.value
    server.params["source"] = BUILTIN_SOURCE
    server.country = ""
    server.country_code = ""
    server.city = ""
    try:
        server.validate()
    except Exception as exc:
        LOGGER.warning("bundled edge server is invalid: %s", exc.__class__.__name__)
        return []
    return [server]


__all__ = [
    "BUILTIN_PROVIDER",
    "BUILTIN_SERVER_ID",
    "BUILTIN_SERVER_NAME",
    "BUILTIN_SOURCE",
    "ENV_EDGE_BIN",
    "builtin_servers",
    "bundled_edge_path",
    "load_builtin_config",
    "load_builtin_payload",
]
