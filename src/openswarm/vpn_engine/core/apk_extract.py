"""Extract the static server material from a decompiled Ninja VPN APK.

The APK does not ship a country catalog: the only static endpoints live in
``assets/edge.bin`` (four edge paths plus one VLESS/Reality ``mask`` profile).
The live ``manifest`` response is *not* reachable offline and is intentionally
out of scope here.

Credentials stay in memory.  :func:`catalog_dict` omits them unless the caller
explicitly asks for a private dump, and nothing in this module logs raw payload
bytes (same rule as :mod:`ninjavpn_client.core.edge_config`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..schema import Server
from .builtin_edge import builtin_servers
from .edge_config import (
    MAX_EDGE_FILE_BYTES,
    EdgeConfig,
    EdgeConfigError,
    EdgePath,
    EdgePathType,
    parse_edge_config,
)

#: Files larger than this are skipped by the optional string scan.
MAX_SCAN_FILE_BYTES = 4 * 1024 * 1024

#: Deterministic device id used when the caller does not supply one.  RAND and
#: MASK hosts are derived from it, so the catalog is repeatable.
DEFAULT_DEVICE_ID = "00000000-0000-4000-8000-000000000000"

CATALOG_SCHEMA_VERSION = 1
CATALOG_GENERATOR = "ninjactl apk extract"
DEFAULT_EDGE_PORT = 443

_DECOMPILE_MARKERS = (
    "assets",
    "resources",
    "sources",
    "src",
    "res",
    "AndroidManifest.xml",
)
_SCAN_ROOTS = (("sources", "app", "ninjavpn"), ("src", "app"))
_BLOB_LAYOUTS = (("assets", "edge.bin"), ("resources", "assets", "edge.bin"))

_HOST_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:[A-Za-z]{2,24})"
    r"(?![A-Za-z0-9_.-])"
)
_PACKAGE_RE = re.compile(r'\bpackage="([^"]+)"')
_VERSION_NAME_RE = re.compile(r'\bandroid:versionName="([^"]*)"')
_VERSION_CODE_RE = re.compile(r'\bandroid:versionCode="(0x[0-9A-Fa-f]+|\d+)"')

#: Third-party namespaces that show up in every jadx output and are never
#: Ninja VPN endpoints.
_NOISE_PREFIXES = (
    "java.",
    "javax.",
    "android.",
    "androidx.",
    "com.google.",
    "io.netty.",
    "kotlin.",
    "okhttp3",
    "jadx.",
)
_NOISE_HOSTS = frozenset({"type.googleapis.com"})


class ApkSourceError(Exception):
    """The decompiled APK tree cannot be used as a source.

    ``code`` is ``APK_SOURCE_MISSING``, ``APK_SOURCE_INVALID`` or
    ``APK_SOURCE_UNSAFE``.
    """

    def __init__(self, message: str, *, code: str = "APK_SOURCE_INVALID") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ApkManifestInfo:
    """Metadata read from the decoded ``AndroidManifest.xml``."""

    package: str = ""
    version_name: str | None = None
    version_code: int | None = None


@dataclass(frozen=True)
class EdgeEndpoint:
    """Public description of one edge path (never contains pins or keys)."""

    path: str
    host: str
    port: int
    tls_host: str
    pin_count: int
    derived: bool


@dataclass(frozen=True)
class HostCandidate:
    """Heuristic hostname hit from the application's own source files."""

    host: str
    file: str
    line: int


@dataclass(frozen=True)
class ApkExtraction:
    """Everything the APK yields statically, plus diagnostics."""

    manifest: ApkManifestInfo
    edge_bin_sha256: str | None
    servers: list[Server]
    endpoints: list[EdgeEndpoint]
    candidates: list[HostCandidate]
    warnings: list[str]
    source: str = ""


# ---------------------------------------------------------------------------
# source tree
# ---------------------------------------------------------------------------


def find_decompile_root(path: Path) -> Path:
    """Validate and normalise a decompiled APK directory."""

    target = Path(path).expanduser()
    try:
        info = target.stat()
    except OSError as exc:
        raise ApkSourceError(
            f"decompiled APK directory not found: {target}",
            code="APK_SOURCE_MISSING",
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ApkSourceError(
            f"decompiled APK path is not a directory: {target}",
            code="APK_SOURCE_MISSING",
        )
    try:
        entries = list(target.iterdir())
    except OSError as exc:
        raise ApkSourceError(
            f"decompiled APK directory cannot be read: {target}",
            code="APK_SOURCE_INVALID",
        ) from exc
    if not entries:
        raise ApkSourceError(
            f"decompiled APK directory is empty: {target}",
            code="APK_SOURCE_MISSING",
        )
    if not any((target / marker).exists() for marker in _DECOMPILE_MARKERS):
        raise ApkSourceError(
            f"directory does not look like an APK decompilation: {target}",
            code="APK_SOURCE_INVALID",
        )
    return target.resolve()


def _manifest_file(root: Path) -> Path | None:
    candidates = (root / "AndroidManifest.xml", root / "resources" / "AndroidManifest.xml")
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_EDGE_FILE_BYTES:
            return candidate
    return None


def read_manifest_info(root: Path) -> ApkManifestInfo:
    """Read package/version fields; missing version fields are not an error."""

    target = _manifest_file(Path(root).expanduser())
    if target is None:
        return ApkManifestInfo()
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_EDGE_FILE_BYTES)
    except OSError:
        return ApkManifestInfo()

    package_match = _PACKAGE_RE.search(text)
    name_match = _VERSION_NAME_RE.search(text)
    code_match = _VERSION_CODE_RE.search(text)
    version_code: int | None = None
    if code_match is not None:
        try:
            version_code = int(code_match.group(1), 0)
        except ValueError:
            version_code = None
    return ApkManifestInfo(
        package=package_match.group(1).strip() if package_match else "",
        version_name=name_match.group(1) if name_match else None,
        version_code=version_code,
    )


def _find_edge_blob(root: Path) -> Path | None:
    for parts in _BLOB_LAYOUTS:
        candidate = root.joinpath(*parts)
        # ``lexists`` keeps a broken symlink visible so it is rejected as
        # unsafe instead of being reported as merely missing.
        if os.path.lexists(candidate):
            return candidate
    return None


def _read_edge_blob(blob: Path) -> bytes:
    try:
        info = blob.lstat()
    except OSError as exc:
        raise ApkSourceError(f"cannot stat edge blob: {blob}", code="APK_SOURCE_MISSING") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ApkSourceError("edge blob must be a regular file", code="APK_SOURCE_UNSAFE")
    if info.st_size > MAX_EDGE_FILE_BYTES:
        raise ApkSourceError(
            f"edge blob is larger than {MAX_EDGE_FILE_BYTES} bytes",
            code="APK_SOURCE_UNSAFE",
        )
    try:
        with blob.open("rb") as handle:
            payload = handle.read(MAX_EDGE_FILE_BYTES + 1)
    except OSError as exc:
        raise ApkSourceError("cannot read edge blob", code="APK_SOURCE_UNSAFE") from exc
    if len(payload) > MAX_EDGE_FILE_BYTES:
        raise ApkSourceError(
            f"edge blob is larger than {MAX_EDGE_FILE_BYTES} bytes",
            code="APK_SOURCE_UNSAFE",
        )
    return payload


# ---------------------------------------------------------------------------
# projection
# ---------------------------------------------------------------------------


def _path_port(path: EdgePath) -> int:
    """MASK carries its port in the Xray template; edge paths default to 443."""

    xray = path.xray
    if isinstance(xray, Mapping):
        settings = xray.get("settings")
        if isinstance(settings, Mapping):
            vnext = settings.get("vnext")
            if isinstance(vnext, (list, tuple)) and vnext and isinstance(vnext[0], Mapping):
                port = vnext[0].get("port")
                if isinstance(port, int) and not isinstance(port, bool) and 0 < port <= 65535:
                    return port
    return DEFAULT_EDGE_PORT


def _build_endpoints(config: EdgeConfig) -> list[EdgeEndpoint]:
    endpoints: list[EdgeEndpoint] = []
    for kind in EdgePathType:
        path = config.paths.get(kind)
        if path is None:
            continue
        endpoints.append(
            EdgeEndpoint(
                path=kind.value,
                host=path.host,
                port=_path_port(path),
                tls_host=path.tls_host,
                pin_count=len(path.pins),
                # RAND/MASK names are generated from the device id, so the
                # catalog host is canonical rather than a constant from the APK.
                derived=kind in (EdgePathType.RAND, EdgePathType.MASK),
            )
        )
    return endpoints


# ---------------------------------------------------------------------------
# optional string scan (report only)
# ---------------------------------------------------------------------------


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _is_noise(host: str) -> bool:
    lowered = host.lower()
    if lowered in _NOISE_HOSTS:
        return True
    return any(lowered.startswith(prefix) for prefix in _NOISE_PREFIXES)


def _scan_candidates(root: Path, warnings: list[str]) -> list[HostCandidate]:
    found: dict[tuple[str, str, int], HostCandidate] = {}
    for parts in _SCAN_ROOTS:
        base = root.joinpath(*parts)
        if not base.is_dir():
            continue
        for file in sorted(base.rglob("*.java")):
            try:
                info = file.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            relative = _relative(root, file)
            if info.st_size > MAX_SCAN_FILE_BYTES:
                warnings.append(
                    f"SCAN_SKIPPED: {relative} is larger than {MAX_SCAN_FILE_BYTES} bytes"
                )
                continue
            try:
                with file.open("r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError as exc:
                warnings.append(
                    f"SCAN_SKIPPED: {relative} cannot be read ({exc.__class__.__name__})"
                )
                continue
            for number, line in enumerate(text.splitlines(), 1):
                for match in _HOST_RE.finditer(line):
                    host = match.group(0)
                    if _is_noise(host):
                        continue
                    found[(host, relative, number)] = HostCandidate(
                        host=host, file=relative, line=number
                    )
    return sorted(found.values(), key=lambda item: (item.host, item.file, item.line))


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def extract_servers(
    root: Path,
    *,
    device_id: str = DEFAULT_DEVICE_ID,
    scan_strings: bool = False,
) -> ApkExtraction:
    """Extract every static server/endpoint from a decompiled APK tree.

    A broken container is reported through ``warnings`` with an empty server
    list (the caller decides whether that is a partial result); structural
    problems (missing/unsafe source) raise :class:`ApkSourceError`.
    """

    root_path = find_decompile_root(root)
    manifest = read_manifest_info(root_path)
    warnings: list[str] = []

    blob = _find_edge_blob(root_path)
    if blob is None:
        raise ApkSourceError(
            f"edge.bin not found under {root_path}", code="APK_SOURCE_MISSING"
        )
    raw = _read_edge_blob(blob)
    digest = hashlib.sha256(raw).hexdigest()

    servers: list[Server] = []
    endpoints: list[EdgeEndpoint] = []
    try:
        config = parse_edge_config(raw, device_id=device_id)
    except EdgeConfigError as exc:
        warnings.append(f"{exc.code}: {exc}")
    else:
        # Reuse the single projection point so the catalog always matches the
        # server the client actually connects to.
        servers = builtin_servers(config)
        endpoints = _build_endpoints(config)
        if not servers:
            warnings.append("EDGE_PATH_UNAVAILABLE: edge.bin has no usable mask profile")

    candidates = _scan_candidates(root_path, warnings) if scan_strings else []
    return ApkExtraction(
        manifest=manifest,
        edge_bin_sha256=digest,
        servers=servers,
        endpoints=endpoints,
        candidates=candidates,
        warnings=warnings,
        source=str(Path(root).expanduser()),
    )


def catalog_dict(extraction: ApkExtraction, *, include_secrets: bool = False) -> dict:
    """Machine-readable catalog, importable by ``ninjactl import`` as-is."""

    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_by": CATALOG_GENERATOR,
        "apk": {
            "package": extraction.manifest.package,
            "version_name": extraction.manifest.version_name,
            "version_code": extraction.manifest.version_code,
            "source": extraction.source,
            "edge_bin_sha256": extraction.edge_bin_sha256,
        },
        "servers": [
            server.to_dict(include_credentials=include_secrets)
            for server in extraction.servers
        ],
        "endpoints": [
            {
                "path": endpoint.path,
                "host": endpoint.host,
                "port": endpoint.port,
                "tls_host": endpoint.tls_host,
                "pin_count": endpoint.pin_count,
                "derived": endpoint.derived,
            }
            for endpoint in extraction.endpoints
        ],
        "host_candidates": [
            {"host": candidate.host, "file": candidate.file, "line": candidate.line}
            for candidate in extraction.candidates
        ],
        "warnings": list(extraction.warnings),
    }


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|")


def render_report(extraction: ApkExtraction) -> str:
    """Render the human-readable ``analysis/APK_SERVERS.md`` report."""

    manifest = extraction.manifest
    lines = [
        "<!-- generated by ninjactl apk extract -->",
        "# Сервера из декомпиляции APK",
        "",
        "Статический материал из `assets/edge.bin` и исходников приложения.",
        "Каталог стран в APK не хранится: его отдаёт только живой `manifest`.",
        "",
        "## Карточка APK",
        "",
        "| Параметр | Значение |",
        "| --- | --- |",
        f"| Package | `{_cell(manifest.package or '—')}` |",
        f"| versionName | `{_cell(manifest.version_name or '—')}` |",
        f"| versionCode | `{_cell(manifest.version_code if manifest.version_code is not None else '—')}` |",
        f"| Источник | `{_cell(extraction.source or '—')}` |",
        f"| SHA-256 `edge.bin` | `{_cell(extraction.edge_bin_sha256 or '—')}` |",
        "",
        "## Edge-пути",
        "",
        "| Путь | Host | Port | TLS host | Пинов | derived |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for endpoint in extraction.endpoints:
        lines.append(
            f"| `{endpoint.path}` | `{_cell(endpoint.host)}` | {endpoint.port} | "
            f"`{_cell(endpoint.tls_host)}` | {endpoint.pin_count} | "
            f"{'да' if endpoint.derived else 'нет'} |"
        )
    if not extraction.endpoints:
        lines.append("| — | — | — | — | — | — |")

    lines.extend(["", "## Сервера каталога", ""])
    if extraction.servers:
        for server in extraction.servers:
            protocol = getattr(server.protocol, "value", server.protocol)
            security = getattr(server.security, "value", server.security)
            lines.append(
                f"- `{server.id}` — {server.name}: `{protocol}` / "
                f"`{security}` на `{server.host}:{server.port}`"
            )
            sni = server.params.get("sni")
            if sni:
                lines.append(f"  - SNI: `{sni}`")
            if server.tags:
                lines.append(f"  - Теги: {', '.join(server.tags)}")
    else:
        lines.append("Серверов не найдено (см. предупреждения ниже).")
    lines.extend(
        [
            "",
            "Учётные данные (`uuid`, `public_key`, `short_id`) в отчёт не попадают:",
            "они остаются только в памяти и в отдельном файле `--include-secrets`.",
            "",
            "## Хосты-кандидаты (эвристика)",
            "",
            "Скан строковых литералов собственного пакета. Это не сервера: список",
            "используется только для отчёта и никогда не попадает в каталог.",
            "",
        ]
    )
    if extraction.candidates:
        lines.extend(["| Host | Файл | Строка |", "| --- | --- | --- |"])
        for candidate in extraction.candidates:
            lines.append(
                f"| `{_cell(candidate.host)}` | `{_cell(candidate.file)}` | {candidate.line} |"
            )
    else:
        lines.append("Совпадений нет (скан выключен или литералов не найдено).")

    lines.extend(
        [
            "",
            "## Чего в APK нет",
            "",
            "- Каталога стран/городов: он приходит только из живого `manifest`",
            "  (`ninjactl edge refresh`), поэтому офлайн его получить нельзя.",
            "- Серверов в `assets/edge.json` и `assets/stealth.json`: это",
            "  inbound/routing-шаблоны Xray без outbound-профилей.",
            "- Актуальных имён `rand`/`mask`: они выводятся из `device_id`",
            "  (UUIDv3), поэтому в каталоге помечены `derived: true`.",
            "",
            "## Предупреждения",
            "",
        ]
    )
    if extraction.warnings:
        lines.extend(f"- `{warning}`" for warning in extraction.warnings)
    else:
        lines.append("Нет.")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "DEFAULT_DEVICE_ID",
    "MAX_SCAN_FILE_BYTES",
    "ApkExtraction",
    "ApkManifestInfo",
    "ApkSourceError",
    "EdgeEndpoint",
    "HostCandidate",
    "catalog_dict",
    "extract_servers",
    "find_decompile_root",
    "read_manifest_info",
    "render_report",
]
