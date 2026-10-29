"""Typed settings and runtime models for the Ninja VPN Linux client.

The module intentionally depends on the standard library only.  It is the
single source of truth for the JSON persisted in
``~/.config/ninjavpn-client/settings.json`` and for the public wire shapes used
by :mod:`ninjavpn_client.ipc`.

Security notes
--------------
* Settings are written atomically (temporary file in the same directory +
  ``os.replace``) with mode ``0600``.
* Symbolic links are never followed while reading or replacing settings.
* Proxy credentials are kept in :class:`Server.credentials`, are excluded from
  ``repr`` and from :meth:`Server.to_public_dict`; they must never be written to
  logs or rendered by the GUI.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

APP_NAME = "ninjavpn-client"
SCHEMA_VERSION = 1
MAX_SETTINGS_BYTES = 1024 * 1024
MAX_PATH_LENGTH = 4096


class SchemaError(ValueError):
    """Base class for schema/loading errors."""


class SettingsError(SchemaError):
    """Raised when settings cannot be read or written."""


class SettingsVersionError(SettingsError):
    """Raised when settings use a schema version this client cannot handle."""


@dataclass(frozen=True)
class ValidationIssue:
    """One validation problem, addressed by a JSON-style path."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class ValidationError(SchemaError):
    """Raised when a model does not satisfy its invariants."""

    def __init__(self, issues: Sequence[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues))


class _StrEnum(StrEnum):
    """String enum that serializes as its value."""


class Protocol(_StrEnum):
    AUTO = "auto"
    VLESS = "vless"
    VMESS = "vmess"
    TROJAN = "trojan"
    SHADOWSOCKS = "shadowsocks"
    WIREGUARD = "wireguard"


class Transport(_StrEnum):
    AUTO = "auto"
    TCP = "tcp"
    RAW = "raw"
    WS = "ws"
    GRPC = "grpc"
    HTTPUPGRADE = "httpupgrade"
    XHTTP = "xhttp"
    KCP = "kcp"
    HTTP = "http"


class Security(_StrEnum):
    NONE = "none"
    TLS = "tls"
    REALITY = "reality"


class RoutingMode(_StrEnum):
    GLOBAL = "global"
    PROXY = "proxy"
    # Compatibility modes understood by the Android client and kept for
    # imported configs/subscriptions.  They are valid values in the same field.
    EDGE = "edge"
    STEALTH = "stealth"


class ConnectionStatus(_StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class DataPlane(_StrEnum):
    """How user traffic is captured.

    ``proxy`` exposes local SOCKS/HTTP inbounds; ``tun`` routes all system
    traffic through a WireGuard interface (requires the privileged helper).
    """

    PROXY = "proxy"
    TUN = "tun"


class EdgePathMode(_StrEnum):
    """Which native edge path the client may use."""

    AUTO = "auto"
    MAIN = "main"
    RAND = "rand"
    MASK = "mask"
    TEST = "test"


class Language(_StrEnum):
    RU = "ru"
    EN = "en"


class Theme(_StrEnum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


class LogLevel(_StrEnum):
    NONE = "none"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    DEBUG = "debug"
    TRACE = "trace"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@{}()+-]{0,255}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
_CREDENTIAL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
_MAX_SECRET_LENGTH = 8192


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce_enum(instance: Any, name: str, enum_cls: type[Enum]) -> None:
    value = getattr(instance, name)
    if isinstance(value, str) and not isinstance(value, enum_cls):
        lowered = value.strip().lower()
        # Leave the raw value in place if unknown; validate() reports it.
        with contextlib.suppress(ValueError):
            setattr(instance, name, enum_cls(lowered))


def _string_issue(
    issues: list[ValidationIssue],
    path: str,
    value: Any,
    *,
    allow_empty: bool = True,
    max_length: int = MAX_PATH_LENGTH,
) -> bool:
    if not isinstance(value, str):
        issues.append(ValidationIssue(path, "must be a string"))
        return False
    if "\x00" in value:
        issues.append(ValidationIssue(path, "must not contain NUL"))
        return False
    if not allow_empty and not value.strip():
        issues.append(ValidationIssue(path, "must not be empty"))
        return False
    if len(value) > max_length:
        issues.append(ValidationIssue(path, f"must be at most {max_length} characters"))
        return False
    return True


def _path_issue(
    issues: list[ValidationIssue],
    path: str,
    value: Any,
    *,
    allow_empty: bool = True,
    allow_url: bool = False,
) -> bool:
    if not _string_issue(issues, path, value, allow_empty=allow_empty):
        return False
    text = value.strip()
    if not text:
        return True
    if _CONTROL_RE.search(text):
        issues.append(ValidationIssue(path, "must not contain control characters"))
        return False
    if allow_url and text.startswith(("http://", "https://")):
        try:
            parts = urlsplit(text)
        except ValueError as exc:
            issues.append(ValidationIssue(path, f"invalid URL: {exc}"))
            return False
        if not parts.netloc:
            issues.append(ValidationIssue(path, "URL has no host"))
            return False
        return True
    if not os.path.isabs(os.path.expanduser(text)):
        issues.append(ValidationIssue(path, "must be an absolute path or start with ~/"))
        return False
    return True


def _enum_issue(
    issues: list[ValidationIssue],
    path: str,
    value: Any,
    enum_cls: type[Enum],
) -> bool:
    if not isinstance(value, enum_cls):
        allowed = ", ".join(sorted(str(item.value) for item in enum_cls))
        issues.append(ValidationIssue(path, f"must be one of: {allowed}"))
        return False
    return True


def _int_issue(
    issues: list[ValidationIssue],
    path: str,
    value: Any,
    minimum: int,
    maximum: int,
) -> bool:
    if not _is_int(value):
        issues.append(ValidationIssue(path, "must be an integer"))
        return False
    if not minimum <= value <= maximum:
        issues.append(ValidationIssue(path, f"must be in range {minimum}..{maximum}"))
        return False
    return True


def _bool_issue(issues: list[ValidationIssue], path: str, value: Any) -> bool:
    if not isinstance(value, bool):
        issues.append(ValidationIssue(path, "must be a boolean"))
        return False
    return True


def _check_dns_servers(issues: list[ValidationIssue], path: str, value: Any) -> None:
    if not isinstance(value, (list, tuple)):
        issues.append(ValidationIssue(path, "must be a list of strings"))
        return
    if not 1 <= len(value) <= 8:
        issues.append(ValidationIssue(path, "must contain 1..8 DNS servers"))
        return
    for index, entry in enumerate(value):
        item_path = f"{path}[{index}]"
        if not _string_issue(issues, item_path, entry, allow_empty=False, max_length=512):
            continue
        text = entry.strip()
        try:
            ipaddress.ip_address(text)
            continue
        except ValueError:
            pass
        try:
            parts = urlsplit(text)
        except ValueError as exc:
            issues.append(ValidationIssue(item_path, f"invalid DNS URL: {exc}"))
            continue
        if parts.scheme not in {"https", "tls", "quic", "tcp", "udp"} or not parts.netloc:
            issues.append(
                ValidationIssue(
                    item_path,
                    "must be an IP address or a DNS URL (https://, tls://, quic://, tcp://, udp://)",
                )
            )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_DEFAULT_DNS = ["1.1.1.1", "8.8.8.8"]


@dataclass
class Settings:
    """All user-visible client settings.

    Field names are also the JSON keys.  ``schema_version`` allows future
    migrations; readers must reject versions newer than :data:`SCHEMA_VERSION`.
    """

    schema_version: int = SCHEMA_VERSION

    # binaries and files
    xray_path: str = ""
    wireguard_path: str = ""
    config_dir: str = ""

    # subscription / manual import
    subscription_url: str = ""
    subscription_auto_update: bool = True
    subscription_update_interval_hours: int = 24
    config_import_path: str = ""

    # server selection
    selected_server_id: str = ""
    selected_country: str = ""

    # protocol / transport
    protocol: Protocol = Protocol.AUTO
    transport: Transport = Transport.AUTO
    server_port: int = 0  # 0 = use the port advertised by the server

    # local proxy and tunnel
    mtu: int = 1500
    socks_enabled: bool = True
    socks_port: int = 1080
    http_enabled: bool = True
    http_port: int = 8080

    # DNS and routing
    dns_servers: list[str] = field(default_factory=lambda: list(_DEFAULT_DNS))
    dns_leak_protection: bool = True
    routing_mode: RoutingMode = RoutingMode.GLOBAL

    # data plane / native edge servers
    data_plane: DataPlane = DataPlane.PROXY
    edge_enabled: bool = False
    edge_config_path: str = ""  # empty = ~/.config/ninjavpn-client/edge.json
    edge_path: EdgePathMode = EdgePathMode.AUTO
    tun_allow_lan: bool = False

    # desktop integration / behaviour
    kill_switch: bool = False
    autostart: bool = False
    auto_connect: bool = False
    start_minimized: bool = False
    minimize_to_tray: bool = True

    # resilience
    retry_count: int = 3
    retry_delay_seconds: float = 2.0
    keep_alive_interval_seconds: int = 30

    # UI
    language: Language = Language.RU
    theme: Theme = Theme.SYSTEM
    log_level: LogLevel = LogLevel.INFO

    def __post_init__(self) -> None:
        for name, enum_cls in (
            ("protocol", Protocol),
            ("transport", Transport),
            ("routing_mode", RoutingMode),
            ("data_plane", DataPlane),
            ("edge_path", EdgePathMode),
            ("language", Language),
            ("theme", Theme),
            ("log_level", LogLevel),
        ):
            _coerce_enum(self, name, enum_cls)
        if isinstance(self.dns_servers, tuple):
            self.dns_servers = list(self.dns_servers)
        if isinstance(self.selected_country, str):
            self.selected_country = self.selected_country.strip().upper()

    # -- validation ---------------------------------------------------------

    def validate(self) -> None:
        """Validate every field, raising :class:`ValidationError` on failure."""

        issues: list[ValidationIssue] = []
        _int_issue(issues, "schema_version", self.schema_version, SCHEMA_VERSION, SCHEMA_VERSION)

        _path_issue(issues, "xray_path", self.xray_path)
        _path_issue(issues, "wireguard_path", self.wireguard_path)
        _path_issue(issues, "config_dir", self.config_dir)

        _string_issue(issues, "subscription_url", self.subscription_url, max_length=8192)
        if self.subscription_url.strip():
            try:
                parts = urlsplit(self.subscription_url.strip())
            except ValueError as exc:
                issues.append(ValidationIssue("subscription_url", f"invalid URL: {exc}"))
                parts = None
            if parts is not None and (parts.scheme not in {"http", "https"} or not parts.netloc):
                issues.append(
                    ValidationIssue("subscription_url", "must be an http(s) URL with a host")
                )
            elif (
                parts is not None
                and parts.scheme == "http"
                and parts.hostname
                not in {
                    "localhost",
                    "127.0.0.1",
                    "::1",
                }
            ):
                issues.append(
                    ValidationIssue("subscription_url", "plain http is allowed only for localhost")
                )
            elif parts is not None and (parts.username or parts.password):
                issues.append(
                    ValidationIssue(
                        "subscription_url",
                        "must not embed credentials; use the token in the path",
                    )
                )

        _bool_issue(issues, "subscription_auto_update", self.subscription_auto_update)
        _int_issue(
            issues,
            "subscription_update_interval_hours",
            self.subscription_update_interval_hours,
            1,
            720,
        )
        _path_issue(issues, "config_import_path", self.config_import_path, allow_url=True)

        _string_issue(issues, "selected_server_id", self.selected_server_id, max_length=256)
        if not isinstance(self.selected_country, str):
            issues.append(ValidationIssue("selected_country", "must be a string"))
        elif self.selected_country and not _COUNTRY_RE.match(self.selected_country):
            issues.append(ValidationIssue("selected_country", "must be an ISO-3166 alpha-2 code"))

        _enum_issue(issues, "protocol", self.protocol, Protocol)
        _enum_issue(issues, "transport", self.transport, Transport)
        _int_issue(issues, "server_port", self.server_port, 0, 65535)

        _int_issue(issues, "mtu", self.mtu, 576, 9000)
        _bool_issue(issues, "socks_enabled", self.socks_enabled)
        _int_issue(issues, "socks_port", self.socks_port, 1024, 65535)
        _bool_issue(issues, "http_enabled", self.http_enabled)
        _int_issue(issues, "http_port", self.http_port, 1024, 65535)
        if self.socks_enabled is False and self.http_enabled is False:
            issues.append(
                ValidationIssue(
                    "socks_enabled", "at least one local proxy (SOCKS or HTTP) must be enabled"
                )
            )
        if self.socks_port == self.http_port and self.socks_enabled and self.http_enabled:
            issues.append(ValidationIssue("http_port", "SOCKS and HTTP ports must be different"))

        _check_dns_servers(issues, "dns_servers", self.dns_servers)
        _bool_issue(issues, "dns_leak_protection", self.dns_leak_protection)
        _enum_issue(issues, "routing_mode", self.routing_mode, RoutingMode)
        _enum_issue(issues, "data_plane", self.data_plane, DataPlane)
        _bool_issue(issues, "edge_enabled", self.edge_enabled)
        _path_issue(issues, "edge_config_path", self.edge_config_path)
        if isinstance(self.edge_config_path, str) and self.edge_config_path.strip().startswith(
            ("http://", "https://")
        ):
            issues.append(
                ValidationIssue("edge_config_path", "must be a local file, not a URL")
            )
        _enum_issue(issues, "edge_path", self.edge_path, EdgePathMode)
        _bool_issue(issues, "tun_allow_lan", self.tun_allow_lan)
        if self.data_plane is DataPlane.TUN and self.routing_mode is not RoutingMode.GLOBAL:
            issues.append(
                ValidationIssue("data_plane", "TUN requires routing_mode=global")
            )
        if self.tun_allow_lan and self.data_plane is not DataPlane.TUN:
            issues.append(
                ValidationIssue("tun_allow_lan", "is allowed only with data_plane=tun")
            )
        if self.edge_path is EdgePathMode.TEST and not self.edge_enabled:
            issues.append(
                ValidationIssue("edge_path", "test path requires edge_enabled=true")
            )

        for name in (
            "kill_switch",
            "autostart",
            "auto_connect",
            "start_minimized",
            "minimize_to_tray",
        ):
            _bool_issue(issues, name, getattr(self, name))

        _int_issue(issues, "retry_count", self.retry_count, 0, 20)
        if not _is_number(self.retry_delay_seconds):
            issues.append(ValidationIssue("retry_delay_seconds", "must be a number"))
        elif not 0.1 <= float(self.retry_delay_seconds) <= 300.0:
            issues.append(ValidationIssue("retry_delay_seconds", "must be in range 0.1..300.0"))
        _int_issue(issues, "keep_alive_interval_seconds", self.keep_alive_interval_seconds, 0, 3600)

        _enum_issue(issues, "language", self.language, Language)
        _enum_issue(issues, "theme", self.theme, Theme)
        _enum_issue(issues, "log_level", self.log_level, LogLevel)

        if issues:
            raise ValidationError(issues)

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, Enum):
                result[item.name] = value.value
            elif isinstance(value, list):
                result[item.name] = list(value)
            else:
                result[item.name] = value
        return result

    def replace(self, **changes: Any) -> Settings:
        """Return a validated copy with ``changes`` applied."""

        merged = self.to_dict()
        merged.update(changes)
        return Settings.from_dict(merged, strict=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, strict: bool = False) -> Settings:
        if not isinstance(data, Mapping):
            raise ValidationError([ValidationIssue("$", "settings must be a JSON object")])

        migrated = migrate_settings_dict(data)
        known = {item.name for item in fields(cls)}
        issues: list[ValidationIssue] = []
        if strict:
            for key in sorted(set(migrated) - known):
                issues.append(ValidationIssue(key, "unknown setting"))

        values = {key: migrated[key] for key in known if key in migrated}
        try:
            settings = cls(**values)
        except TypeError as exc:  # pragma: no cover - guarded by the filter above
            raise ValidationError([ValidationIssue("$", str(exc))]) from exc
        if issues:
            settings.validate()
            raise ValidationError(issues)
        settings.validate()
        return settings

    def config_dir_path(self, *, env: Mapping[str, str] | None = None) -> Path:
        return Path(self.config_dir).expanduser() if self.config_dir else default_config_dir(env)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@dataclass
class Server:
    """One remote endpoint.

    ``credentials`` holds protocol secrets (UUID, password, private key, ...).
    It is deliberately absent from :meth:`to_public_dict` and from ``repr``.
    ``params`` holds non-secret connection parameters (SNI, path, ALPN, ...).
    """

    id: str
    name: str
    host: str
    port: int
    protocol: Protocol
    transport: Transport = Transport.TCP
    security: Security = Security.NONE
    country: str = ""
    country_code: str = ""
    city: str = ""
    premium: bool = False
    latency_ms: int | None = None
    updated_at: float | None = None
    tags: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict, repr=False)
    credentials: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for name, enum_cls in (
            ("protocol", Protocol),
            ("transport", Transport),
            ("security", Security),
        ):
            _coerce_enum(self, name, enum_cls)
        if isinstance(self.tags, tuple):
            self.tags = list(self.tags)
        if isinstance(self.country_code, str):
            self.country_code = self.country_code.strip().upper()

    def validate(self) -> None:
        issues: list[ValidationIssue] = []
        if not isinstance(self.id, str) or not _SERVER_ID_RE.match(self.id):
            issues.append(
                ValidationIssue(
                    "id", "must start with an alphanumeric character and be 1..256 safe chars"
                )
            )
        _string_issue(issues, "name", self.name, allow_empty=False, max_length=256)
        if not isinstance(self.host, str):
            issues.append(ValidationIssue("host", "must be a string"))
        else:
            host = self.host.strip()
            try:
                ipaddress.ip_address(host.strip("[]"))
            except ValueError:
                if not _HOST_RE.match(host):
                    issues.append(
                        ValidationIssue("host", "must be a hostname or IP address without scheme")
                    )
        _int_issue(issues, "port", self.port, 1, 65535)
        _enum_issue(issues, "protocol", self.protocol, Protocol)
        _enum_issue(issues, "transport", self.transport, Transport)
        _enum_issue(issues, "security", self.security, Security)
        _string_issue(issues, "country", self.country, max_length=128)
        if not isinstance(self.country_code, str):
            issues.append(ValidationIssue("country_code", "must be a string"))
        elif self.country_code and not _COUNTRY_RE.match(self.country_code):
            issues.append(ValidationIssue("country_code", "must be an ISO-3166 alpha-2 code"))
        _string_issue(issues, "city", self.city, max_length=128)
        _bool_issue(issues, "premium", self.premium)
        if self.latency_ms is not None:
            _int_issue(issues, "latency_ms", self.latency_ms, 0, 600_000)
        if self.updated_at is not None and not _is_number(self.updated_at):
            issues.append(ValidationIssue("updated_at", "must be a Unix timestamp"))
        if not isinstance(self.tags, (list, tuple)):
            issues.append(ValidationIssue("tags", "must be a list of strings"))
        else:
            for index, tag in enumerate(self.tags):
                _string_issue(issues, f"tags[{index}]", tag, max_length=64)
        self._validate_mapping(issues, "params", self.params, secret=False)
        self._validate_mapping(issues, "credentials", self.credentials, secret=True)
        if issues:
            raise ValidationError(issues)

    def _validate_mapping(
        self,
        issues: list[ValidationIssue],
        path: str,
        value: Any,
        *,
        secret: bool,
    ) -> None:
        if not isinstance(value, Mapping):
            issues.append(ValidationIssue(path, "must be a JSON object"))
            return
        if len(value) > 64:
            issues.append(ValidationIssue(path, "must contain at most 64 entries"))
        for key, item in value.items():
            key_path = f"{path}.{key}"
            if not isinstance(key, str) or not _CREDENTIAL_KEY_RE.match(key):
                issues.append(ValidationIssue(key_path, "invalid parameter name"))
                continue
            if secret:
                if not isinstance(item, str):
                    issues.append(ValidationIssue(key_path, "secret values must be strings"))
                elif len(item) > _MAX_SECRET_LENGTH:
                    issues.append(ValidationIssue(key_path, "secret value is too long"))
                elif "\x00" in item:
                    issues.append(ValidationIssue(key_path, "must not contain NUL"))
            elif not isinstance(item, (str, int, float, bool)) and item is not None:
                issues.append(
                    ValidationIssue(key_path, "must be a string, number, boolean or null")
                )
            elif isinstance(item, str) and len(item) > _MAX_SECRET_LENGTH:
                issues.append(ValidationIssue(key_path, "value is too long"))

    def to_dict(self, *, include_credentials: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "protocol": self.protocol.value,
            "transport": self.transport.value,
            "security": self.security.value,
            "country": self.country,
            "country_code": self.country_code,
            "city": self.city,
            "premium": self.premium,
            "latency_ms": self.latency_ms,
            "updated_at": self.updated_at,
            "tags": list(self.tags),
            "params": dict(self.params),
        }
        if include_credentials:
            result["credentials"] = dict(self.credentials)
        return result

    def to_public_dict(self) -> dict[str, Any]:
        """Wire shape safe for IPC/GUI/logs (never contains secrets)."""

        return self.to_dict(include_credentials=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, strict: bool = False) -> Server:
        if not isinstance(data, Mapping):
            raise ValidationError([ValidationIssue("$", "server must be a JSON object")])
        known = {item.name for item in fields(cls)}
        issues = (
            [ValidationIssue(key, "unknown server field") for key in sorted(set(data) - known)]
            if strict
            else []
        )
        values = {key: data[key] for key in known if key in data}
        try:
            server = cls(**values)
        except TypeError as exc:  # pragma: no cover
            raise ValidationError([ValidationIssue("$", str(exc))]) from exc
        if issues:
            server.validate()
            raise ValidationError(issues)
        server.validate()
        return server


# ---------------------------------------------------------------------------
# Runtime models
# ---------------------------------------------------------------------------


@dataclass
class ConnectionState:
    status: ConnectionStatus = ConnectionStatus.DISCONNECTED
    since: float = field(default_factory=time.time)
    server_id: str | None = None
    server_name: str | None = None
    error: str | None = None
    error_code: str | None = None
    attempt: int = 0

    def __post_init__(self) -> None:
        _coerce_enum(self, "status", ConnectionStatus)

    @property
    def is_connected(self) -> bool:
        return self.status is ConnectionStatus.CONNECTED

    def validate(self) -> None:
        issues: list[ValidationIssue] = []
        _enum_issue(issues, "status", self.status, ConnectionStatus)
        if not _is_number(self.since):
            issues.append(ValidationIssue("since", "must be a Unix timestamp"))
        for name in ("server_id", "server_name", "error", "error_code"):
            value = getattr(self, name)
            if value is not None:
                _string_issue(issues, name, value, max_length=1024)
        _int_issue(issues, "attempt", self.attempt, 0, 1000)
        if issues:
            raise ValidationError(issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "since": self.since,
            "server_id": self.server_id,
            "server_name": self.server_name,
            "error": self.error,
            "error_code": self.error_code,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConnectionState:
        if not isinstance(data, Mapping):
            raise ValidationError([ValidationIssue("$", "state must be a JSON object")])
        state = cls(
            status=data.get("status", ConnectionStatus.DISCONNECTED),
            since=data.get("since", time.time()),
            server_id=data.get("server_id"),
            server_name=data.get("server_name"),
            error=data.get("error"),
            error_code=data.get("error_code"),
            attempt=data.get("attempt", 0),
        )
        state.validate()
        return state


@dataclass
class ConnectionStats:
    up_bytes: int = 0
    down_bytes: int = 0
    uptime_seconds: float = 0.0
    external_ip: str = ""
    server_id: str | None = None
    up_speed_bps: float = 0.0
    down_speed_bps: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def validate(self) -> None:
        issues: list[ValidationIssue] = []
        _int_issue(issues, "up_bytes", self.up_bytes, 0, 1 << 62)
        _int_issue(issues, "down_bytes", self.down_bytes, 0, 1 << 62)
        if not _is_number(self.uptime_seconds) or self.uptime_seconds < 0:
            issues.append(ValidationIssue("uptime_seconds", "must be a non-negative number"))
        if not isinstance(self.external_ip, str):
            issues.append(ValidationIssue("external_ip", "must be a string"))
        elif self.external_ip:
            try:
                ipaddress.ip_address(self.external_ip)
            except ValueError:
                issues.append(ValidationIssue("external_ip", "must be an IP address or empty"))
        if self.server_id is not None:
            _string_issue(issues, "server_id", self.server_id, max_length=256)
        for name in ("up_speed_bps", "down_speed_bps"):
            value = getattr(self, name)
            if not _is_number(value) or value < 0:
                issues.append(ValidationIssue(name, "must be a non-negative number"))
        if not _is_number(self.updated_at):
            issues.append(ValidationIssue("updated_at", "must be a Unix timestamp"))
        if issues:
            raise ValidationError(issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "up_bytes": self.up_bytes,
            "down_bytes": self.down_bytes,
            "uptime_seconds": self.uptime_seconds,
            "external_ip": self.external_ip,
            "server_id": self.server_id,
            "up_speed_bps": self.up_speed_bps,
            "down_speed_bps": self.down_speed_bps,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConnectionStats:
        if not isinstance(data, Mapping):
            raise ValidationError([ValidationIssue("$", "stats must be a JSON object")])
        stats = cls(
            up_bytes=data.get("up_bytes", 0),
            down_bytes=data.get("down_bytes", 0),
            uptime_seconds=data.get("uptime_seconds", 0.0),
            external_ip=data.get("external_ip", ""),
            server_id=data.get("server_id"),
            up_speed_bps=data.get("up_speed_bps", 0.0),
            down_speed_bps=data.get("down_speed_bps", 0.0),
            updated_at=data.get("updated_at", time.time()),
        )
        stats.validate()
        return stats


@dataclass
class Event:
    """A domain event carried inside an IPC ``event`` envelope."""

    event: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def validate(self) -> None:
        issues: list[ValidationIssue] = []
        if not isinstance(self.event, str) or not _EVENT_RE.match(self.event):
            issues.append(ValidationIssue("event", "must be a lowercase dotted event name"))
        if not isinstance(self.data, Mapping):
            issues.append(ValidationIssue("data", "must be a JSON object"))
        if not _is_number(self.ts):
            issues.append(ValidationIssue("ts", "must be a Unix timestamp"))
        if issues:
            raise ValidationError(issues)

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "data": dict(self.data), "ts": self.ts}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Event:
        if not isinstance(data, Mapping):
            raise ValidationError([ValidationIssue("$", "event must be a JSON object")])
        event = cls(
            event=data.get("event", ""),
            data=data.get("data", {}),
            ts=data.get("ts", time.time()),
        )
        event.validate()
        return event


# ---------------------------------------------------------------------------
# Paths, migration and persistence
# ---------------------------------------------------------------------------


def _xdg_dir(env: Mapping[str, str] | None, variable: str, fallback: str) -> Path:
    source = os.environ if env is None else env
    value = source.get(variable)
    if value:
        return Path(value).expanduser()
    return Path.home() / fallback


ENV_EDGE_BIN = "NINJAVPN_EDGE_BIN"
SYSTEM_EDGE_BIN_PATH = Path("/usr/share") / APP_NAME / "edge.bin"
MANUAL_EDGE_BIN_PATH = Path(".local/share") / APP_NAME / "edge.bin"


def default_config_dir(env: Mapping[str, str] | None = None) -> Path:
    return _xdg_dir(env, "XDG_CONFIG_HOME", ".config") / APP_NAME


def default_edge_bin_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the ``edge.bin`` blob to use.

    Priority: the ``$NINJAVPN_EDGE_BIN`` override, the source tree
    (``./assets/edge.bin``), the packaged location under ``/usr/share``, then a
    per-user manual install.  The first existing candidate wins; when none
    exists the manual-install path is returned so callers can report it.
    """

    source = os.environ if env is None else env
    override = source.get(ENV_EDGE_BIN, "").strip()
    if override:
        return Path(override).expanduser()
    home = Path(source.get("HOME", "") or Path.home()).expanduser()
    candidates = (
        Path.cwd() / "assets" / "edge.bin",
        SYSTEM_EDGE_BIN_PATH,
        home / MANUAL_EDGE_BIN_PATH,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return home / MANUAL_EDGE_BIN_PATH


def default_settings_path(env: Mapping[str, str] | None = None) -> Path:
    return default_config_dir(env) / "settings.json"


def default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    return _xdg_dir(env, "XDG_STATE_HOME", ".local/state") / APP_NAME


def default_log_dir(env: Mapping[str, str] | None = None) -> Path:
    return default_state_dir(env) / "logs"


def migrate_settings_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of raw settings migrated to :data:`SCHEMA_VERSION`.

    Version 1 is the initial schema.  The hook exists so the core can add
    migrations without changing callers.
    """

    if not isinstance(data, Mapping):
        raise ValidationError([ValidationIssue("$", "settings must be a JSON object")])
    migrated = dict(data)
    version = migrated.get("schema_version", SCHEMA_VERSION)
    if not _is_int(version):
        raise ValidationError([ValidationIssue("schema_version", "must be an integer")])
    if version > SCHEMA_VERSION:
        raise SettingsVersionError(
            f"settings schema version {version} is newer than supported {SCHEMA_VERSION}"
        )
    if version < 1:
        raise SettingsVersionError(f"unsupported settings schema version {version}")
    migrated["schema_version"] = SCHEMA_VERSION
    return migrated


def _ensure_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise SettingsError(f"refusing to use symlinked directory: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise SettingsError(f"not a directory: {path}")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    _ensure_directory(path.parent)
    try:
        if path.is_symlink():
            raise SettingsError(f"refusing to replace symlink: {path}")
    except OSError as exc:
        raise SettingsError(f"cannot inspect {path}: {exc}") from exc

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load and validate settings.

    Raises :class:`SettingsError` when the file is missing, too large, not
    owned by the current user, or is a symbolic link.
    """

    target = Path(path).expanduser() if path is not None else default_settings_path()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except FileNotFoundError as exc:
        raise SettingsError(f"settings file not found: {target}") from exc
    except OSError as exc:
        raise SettingsError(f"cannot open settings {target}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SettingsError(f"settings path is not a regular file: {target}")
        if info.st_uid != os.getuid():
            raise SettingsError(f"settings file is not owned by the current user: {target}")
        if info.st_mode & 0o077:
            # Best effort: tighten permissions, but do not fail startup if the
            # filesystem is read-only.
            with contextlib.suppress(OSError):
                os.fchmod(fd, 0o600)
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_SETTINGS_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)

    if len(payload) > MAX_SETTINGS_BYTES:
        raise SettingsError(f"settings file is larger than {MAX_SETTINGS_BYTES} bytes")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise SettingsError("settings file is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise SettingsError(f"settings file is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise SettingsError("settings root must be a JSON object")
    try:
        return Settings.from_dict(raw)
    except SchemaError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise SettingsError(f"invalid settings: {exc}") from exc


def save_settings(
    settings: Settings,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Validate and atomically persist ``settings`` with mode ``0600``."""

    if not isinstance(settings, Settings):
        raise TypeError("settings must be a Settings instance")
    settings.validate()
    target = Path(path).expanduser() if path is not None else default_settings_path()
    payload = (
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    if len(payload) > MAX_SETTINGS_BYTES:
        raise SettingsError(f"settings are larger than {MAX_SETTINGS_BYTES} bytes")
    try:
        _atomic_write_bytes(target, payload)
    except SettingsError:
        raise
    except OSError as exc:
        raise SettingsError(f"cannot write settings {target}: {exc}") from exc
    return target


__all__ = [
    "APP_NAME",
    "SCHEMA_VERSION",
    "ConnectionState",
    "ConnectionStats",
    "ConnectionStatus",
    "DataPlane",
    "EdgePathMode",
    "Event",
    "Language",
    "LogLevel",
    "Protocol",
    "RoutingMode",
    "SchemaError",
    "Security",
    "Server",
    "Settings",
    "SettingsError",
    "SettingsVersionError",
    "Theme",
    "Transport",
    "ValidationError",
    "ValidationIssue",
    "default_config_dir",
    "default_edge_bin_path",
    "default_log_dir",
    "default_settings_path",
    "default_state_dir",
    "load_settings",
    "migrate_settings_dict",
    "save_settings",
]
