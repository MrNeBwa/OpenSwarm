"""Build Xray-core JSON from :class:`schema.Settings` and a selected server.

The output follows ``docs/CONFIG_SCHEMA.md`` §6 and the Android projection in
``docs/CONFIG_MODEL.md``.  No secret is ever placed in a tag or file name.
"""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlsplit

from ..schema import (
    LogLevel,
    Protocol,
    RoutingMode,
    Security,
    Server,
    Settings,
    Transport,
    ValidationError,
)

_LOGLEVELS = {
    LogLevel.NONE: "error",
    LogLevel.ERROR: "error",
    LogLevel.WARNING: "warning",
    LogLevel.INFO: "info",
    LogLevel.DEBUG: "debug",
    LogLevel.TRACE: "debug",
}


class ConfigBuildError(ValueError):
    """The settings/server pair cannot be projected to a valid Xray config."""


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def build_xray_config(
    settings: Settings,
    server: Server | None,
    *,
    raw_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fresh, JSON-serializable Xray configuration.

    ``raw_config`` is used for manually imported full Xray JSON configs; it is
    copied and validated but otherwise left untouched.
    """

    if not isinstance(settings, Settings):
        raise ConfigBuildError("settings must be a Settings instance")
    settings.validate()

    if raw_config is not None:
        if not isinstance(raw_config, dict):
            raise ConfigBuildError("imported config must be a JSON object")
        config = copy.deepcopy(raw_config)
        validate_xray_config(config)
        return config

    if server is None:
        raise ConfigBuildError("no server selected")
    if not isinstance(server, Server):
        raise ConfigBuildError("server must be a Server instance")
    try:
        server.validate()
    except ValidationError as exc:
        # ``params`` may carry structured values (e.g. a fragment object)
        # that the flat schema validator cannot represent.  Only reject
        # issues outside those extension mappings.
        blocking = [
            issue
            for issue in exc.issues
            if not issue.path.startswith(("params.", "credentials."))
        ]
        if blocking:
            raise ConfigBuildError(
                "invalid server: " + "; ".join(str(issue) for issue in blocking)
            ) from exc
    except Exception as exc:  # pragma: no cover - defensive
        raise ConfigBuildError(f"invalid server: {exc}") from exc

    config: dict[str, Any] = {
        "log": {
            "loglevel": _LOGLEVELS[settings.log_level],
            "access": "none",
            "error": "",
        },
        "inbounds": _build_inbounds(settings),
        "outbounds": [
            build_outbound(server, settings),
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": _build_routing(settings),
        "dns": _build_dns(settings),
        "stats": {},
    }
    validate_xray_config(config)
    return config


def build_outbound(server: Server, settings: Settings) -> dict[str, Any]:
    """Project one server to the primary Xray outbound."""

    port = settings.server_port or server.port
    params = dict(server.params)
    credentials = dict(server.credentials)
    protocol = server.protocol

    outbound: dict[str, Any] = {"tag": "proxy"}

    if protocol is Protocol.VLESS:
        user: dict[str, Any] = {
            "id": credentials.get("uuid", ""),
            "encryption": str(params.get("encryption", "none")),
            "level": _as_int(params.get("level"), 0),
        }
        flow = str(params.get("flow", "")).strip()
        if flow:
            user["flow"] = flow
        outbound.update(
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": server.host,
                            "port": port,
                            "users": [user],
                        }
                    ]
                },
            }
        )
    elif protocol is Protocol.VMESS:
        user = {
            "id": credentials.get("uuid", ""),
            "alterId": _as_int(params.get("alter_id"), 0),
            "security": str(params.get("encryption", "auto")),
            "level": _as_int(params.get("level"), 0),
        }
        outbound.update(
            {
                "protocol": "vmess",
                "settings": {
                    "vnext": [
                        {"address": server.host, "port": port, "users": [user]}
                    ]
                },
            }
        )
    elif protocol is Protocol.TROJAN:
        outbound.update(
            {
                "protocol": "trojan",
                "settings": {
                    "servers": [
                        {
                            "address": server.host,
                            "port": port,
                            "password": credentials.get("password", ""),
                            "level": _as_int(params.get("level"), 0),
                        }
                    ]
                },
            }
        )
    elif protocol is Protocol.SHADOWSOCKS:
        outbound.update(
            {
                "protocol": "shadowsocks",
                "settings": {
                    "servers": [
                        {
                            "address": server.host,
                            "port": port,
                            "method": str(params.get("method", "aes-256-gcm")),
                            "password": credentials.get("password", ""),
                            "level": _as_int(params.get("level"), 0),
                        }
                    ]
                },
            }
        )
    elif protocol is Protocol.WIREGUARD:
        address = _split_list(params.get("address"))
        allowed = _split_list(params.get("allowed_ips")) or ["0.0.0.0/0", "::/0"]
        peer: dict[str, Any] = {
            "publicKey": str(params.get("public_key", "")),
            "endpoint": str(params.get("endpoint", f"{server.host}:{port}")),
            "allowedIPs": allowed,
        }
        keepalive = _as_int(params.get("keepalive"), 0)
        if keepalive:
            peer["keepAlive"] = keepalive
        wg_settings: dict[str, Any] = {
            "secretKey": credentials.get("private_key", ""),
            "peers": [peer],
            "mtu": _as_int(params.get("mtu"), settings.mtu),
        }
        if address:
            wg_settings["address"] = address
        if credentials.get("pre_shared_key"):
            peer["preSharedKey"] = credentials["pre_shared_key"]
        outbound.update({"protocol": "wireguard", "settings": wg_settings})
    else:  # pragma: no cover - enum is closed
        raise ConfigBuildError(f"unsupported protocol: {protocol}")

    outbound["streamSettings"] = _build_stream_settings(server, settings)
    mux = _build_mux(params)
    if mux is not None:
        outbound["mux"] = mux
    return outbound


def _build_inbounds(settings: Settings) -> list[dict[str, Any]]:
    inbounds: list[dict[str, Any]] = []
    if settings.socks_enabled:
        inbounds.append(
            {
                "tag": "socks-in",
                "listen": "127.0.0.1",
                "port": settings.socks_port,
                "protocol": "socks",
                "settings": {"auth": "noauth", "udp": True},
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                    "routeOnly": True,
                },
            }
        )
    if settings.http_enabled:
        inbounds.append(
            {
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": settings.http_port,
                "protocol": "http",
                "settings": {"allowTransparent": False},
            }
        )
    if not inbounds:  # Settings.validate catches this; keep the invariant local.
        raise ConfigBuildError("at least one inbound must be enabled")
    return inbounds


def _build_stream_settings(server: Server, settings: Settings) -> dict[str, Any]:
    params = server.params
    transport = server.transport
    network = "tcp" if transport in (Transport.AUTO, Transport.RAW) else transport.value
    stream: dict[str, Any] = {"network": network}
    sni = str(params.get("sni") or params.get("server_name") or server.host)
    alpn = _split_list(params.get("alpn"))

    if server.security is Security.TLS:
        tls: dict[str, Any] = {"serverName": sni}
        if alpn:
            tls["alpn"] = alpn
        if _as_bool(params.get("allow_insecure")):
            tls["allowInsecure"] = True
        if params.get("fingerprint"):
            tls["fingerprint"] = str(params["fingerprint"])
        stream["security"] = "tls"
        stream["tlsSettings"] = tls
    elif server.security is Security.REALITY:
        reality: dict[str, Any] = {
            "serverName": sni,
            "fingerprint": str(params.get("fingerprint", "chrome")),
            "publicKey": server.credentials.get("public_key", str(params.get("public_key", ""))),
            "shortId": server.credentials.get("short_id", str(params.get("short_id", ""))),
        }
        if alpn:
            reality["alpn"] = alpn
        if params.get("spider_x"):
            reality["spiderX"] = str(params["spider_x"])
        if _as_bool(params.get("allow_insecure")):
            reality["allowInsecure"] = True
        stream["security"] = "reality"
        stream["realitySettings"] = reality
        tls = {"serverName": sni}
        if alpn:
            tls["alpn"] = alpn
        stream["tlsSettings"] = tls
    else:
        stream["security"] = "none"

    if transport is Transport.WS:
        ws: dict[str, Any] = {"path": str(params.get("path", "/"))}
        host = params.get("host") or sni
        if host:
            ws["headers"] = {"Host": str(host)}
        stream["wsSettings"] = ws
    elif transport is Transport.GRPC:
        stream["grpcSettings"] = {
            "serviceName": str(params.get("service_name", "")),
            "multiMode": _as_bool(params.get("multi_mode")),
        }
    elif transport is Transport.HTTPUPGRADE:
        upgrade: dict[str, Any] = {"path": str(params.get("path", "/"))}
        if params.get("host"):
            upgrade["host"] = str(params["host"])
        stream["httpupgradeSettings"] = upgrade
    elif transport is Transport.XHTTP:
        xhttp: dict[str, Any] = {
            "path": str(params.get("path", "/")),
            "mode": str(params.get("mode", "auto")),
        }
        if params.get("host"):
            xhttp["host"] = str(params["host"])
        stream["xhttpSettings"] = xhttp
    elif transport is Transport.KCP:
        kcp: dict[str, Any] = {"header": {"type": str(params.get("header_type", "none"))}}
        if params.get("seed"):
            kcp["seed"] = str(params["seed"])
        stream["kcpSettings"] = kcp
    elif transport is Transport.HTTP:
        stream["network"] = "tcp"
        stream["tcpSettings"] = {
            "header": {
                "type": "http",
                "request": {"path": [str(params.get("path", "/"))]},
            }
        }
    elif network == "tcp":
        stream["tcpSettings"] = {"header": {"type": str(params.get("header_type", "none"))}}

    # Xray's REALITY transport only supports raw, xhttp and grpc.  Android
    # profiles exported by Ninja VPN occasionally carry a stale ws/http
    # transport next to REALITY; emitting it verbatim makes xray-core refuse to
    # start.  Fall back to raw and drop the now-unused transport settings.
    if server.security is Security.REALITY and stream.get("network") not in {
        "tcp",
        "raw",
        "xhttp",
        "grpc",
    }:
        stream["network"] = "raw"
        for key in (
            "wsSettings",
            "grpcSettings",
            "httpupgradeSettings",
            "kcpSettings",
            "tcpSettings",
            "xhttpSettings",
        ):
            stream.pop(key, None)
        stream["tcpSettings"] = {"header": {"type": "none"}}

    fragment = params.get("fragment")
    if isinstance(fragment, dict):
        allowed = {
            key: str(value)
            for key, value in fragment.items()
            if key in {"packets", "length", "interval"} and value is not None
        }
        if allowed:
            stream["fragment"] = allowed
    return stream


def _build_mux(params: dict[str, Any]) -> dict[str, Any] | None:
    if not _as_bool(params.get("mux")):
        return None
    return {
        "enabled": True,
        "concurrency": max(1, _as_int(params.get("mux_concurrency"), 8)),
    }


def _build_dns(settings: Settings) -> dict[str, Any]:
    servers: list[str] = []
    for entry in settings.dns_servers:
        text = str(entry).strip()
        if not text:
            continue
        if "://" in text:
            parts = urlsplit(text)
            if parts.scheme not in {"https", "tls", "quic", "tcp", "udp"} or not parts.netloc:
                raise ConfigBuildError(f"unsupported DNS server: {parts.scheme}")
            servers.append(text)
        else:
            servers.append(text)
    if not servers:
        raise ConfigBuildError("at least one DNS server is required")
    dns: dict[str, Any] = {"servers": servers, "hosts": {}}
    if settings.dns_leak_protection:
        dns["queryStrategy"] = "UseIP"
    return dns


_PRIVATE_CIDRS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "255.255.255.255/32",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)


def _build_routing(settings: Settings) -> dict[str, Any]:
    mode = settings.routing_mode
    rules: list[dict[str, Any]] = [
        {"type": "field", "ip": list(_PRIVATE_CIDRS), "outboundTag": "direct"}
    ]
    if mode is RoutingMode.EDGE:
        rules.append({"type": "field", "port": "443", "network": "tcp", "outboundTag": "direct"})
    if settings.dns_leak_protection:
        rules.append({"type": "field", "port": "53", "outboundTag": "proxy"})
    if mode is RoutingMode.STEALTH:
        rules.append({"type": "field", "port": "443", "network": "udp", "outboundTag": "block"})
    rules.append({"type": "field", "network": "tcp,udp", "outboundTag": "proxy"})
    return {
        "domainStrategy": "AsIs" if mode in (RoutingMode.PROXY, RoutingMode.STEALTH) else "IPIfNonMatch",
        "rules": rules,
    }


def validate_xray_config(config: dict[str, Any]) -> None:
    """Structural validation before handing a config to Xray."""

    if not isinstance(config, dict):
        raise ConfigBuildError("config must be a JSON object")
    inbounds = config.get("inbounds")
    outbounds = config.get("outbounds")
    if not isinstance(inbounds, list) or not inbounds:
        raise ConfigBuildError("config.inbounds must be a non-empty array")
    if not isinstance(outbounds, list) or not outbounds:
        raise ConfigBuildError("config.outbounds must be a non-empty array")

    inbound_tags: set[str] = set()
    for index, inbound in enumerate(inbounds):
        if not isinstance(inbound, dict):
            raise ConfigBuildError(f"inbounds[{index}] must be an object")
        if not isinstance(inbound.get("protocol"), str) or not inbound["protocol"]:
            raise ConfigBuildError(f"inbounds[{index}].protocol is required")
        if not isinstance(inbound.get("port"), int) or not 1 <= inbound["port"] <= 65535:
            raise ConfigBuildError(f"inbounds[{index}].port must be 1..65535")
        if inbound.get("listen") != "127.0.0.1":
            raise ConfigBuildError(f"inbounds[{index}] must listen on loopback")
        tag = inbound.get("tag")
        if tag is not None:
            if not isinstance(tag, str) or not tag:
                raise ConfigBuildError(f"inbounds[{index}].tag is invalid")
            if tag in inbound_tags:
                raise ConfigBuildError(f"duplicate inbound tag: {tag}")
            inbound_tags.add(tag)

    tags: set[str] = set()
    for index, outbound in enumerate(outbounds):
        if not isinstance(outbound, dict):
            raise ConfigBuildError(f"outbounds[{index}] must be an object")
        if not isinstance(outbound.get("protocol"), str) or not outbound["protocol"]:
            raise ConfigBuildError(f"outbounds[{index}].protocol is required")
        tag = outbound.get("tag")
        if tag is not None:
            if not isinstance(tag, str) or not tag:
                raise ConfigBuildError(f"outbounds[{index}].tag is invalid")
            if tag in tags:
                raise ConfigBuildError(f"duplicate outbound tag: {tag}")
            tags.add(tag)

    routing = config.get("routing")
    if routing is not None:
        if not isinstance(routing, dict):
            raise ConfigBuildError("config.routing must be an object")
        rules = routing.get("rules", [])
        if not isinstance(rules, list):
            raise ConfigBuildError("routing.rules must be an array")
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise ConfigBuildError(f"routing.rules[{index}] must be an object")
            target = rule.get("outboundTag")
            if target is not None and tags and target not in tags:
                raise ConfigBuildError(f"routing rule points to unknown outbound: {target}")


__all__ = [
    "ConfigBuildError",
    "build_outbound",
    "build_xray_config",
    "validate_xray_config",
]
