"""Xray binary discovery, verified download and process supervision."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..schema import Settings, default_state_dir

DEFAULT_XRAY_VERSION = "25.6.8"
DEFAULT_XRAY_URL = (
    "https://github.com/XTLS/Xray-core/releases/download/"
    f"v{DEFAULT_XRAY_VERSION}/Xray-linux-64.zip"
)
# SHA-256 published for Xray-linux-64.zip v25.6.8.
DEFAULT_XRAY_SHA256 = "51bcd3304fdbd64b58048b056da005fbaa6c83577fc351cae34024760e111e4b"
MIN_XRAY_VERSION = (1, 8, 0)
KNOWN_XRAY_PATHS = (
    Path("/usr/bin/xray"),
    Path("/usr/local/bin/xray"),
    Path("~/.local/share/ninjavpn-client/xray/xray").expanduser(),
)


class XrayError(RuntimeError):
    """Base class for Xray-related failures."""


class XrayNotFound(XrayError):
    """No usable xray binary was found."""

    INSTRUCTIONS = (
        "xray-core не найден. Установите его одним из способов:\n"
        "  • AUR: yay -S xray-bin (или paru -S xray-bin)\n"
        "  • официальный релиз: https://github.com/XTLS/Xray-core/releases\n"
        "    (Xray-linux-64.zip, проверка sha256 обязательна)\n"
        "  • укажите путь в настройке xray_path или выполните\n"
        "    ninjactl settings set xray_path=/полный/путь/к/xray"
    )


class DownloadError(XrayError):
    """The optional binary download failed or was not consented to."""


@dataclass(frozen=True)
class XrayBinary:
    path: Path
    version: str
    source: str


def _version_tuple(version: str) -> tuple[int, ...]:
    digits: list[int] = []
    for part in version.split():
        if part and part[0].isdigit():
            for piece in part.replace("-", ".").split("."):
                if piece.isdigit():
                    digits.append(int(piece))
                else:
                    break
            break
    return tuple(digits)


def xray_version(path: str | Path, *, timeout: float = 3.0) -> str:
    """Run ``xray version`` with a short timeout and return its first line."""

    target = Path(path).expanduser()
    try:
        completed = subprocess.run(
            [str(target), "version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"), "HOME": os.environ.get("HOME", "")},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise XrayNotFound(f"не удалось запустить {target}: {exc.__class__.__name__}") from exc
    if completed.returncode != 0:
        raise XrayNotFound(f"{target} version завершился с кодом {completed.returncode}")
    line = (completed.stdout or completed.stderr).splitlines()
    if not line or "Xray" not in line[0]:
        raise XrayNotFound(f"{target} не похож на xray-core")
    return line[0].strip()


def _validate_binary(path: Path, *, source: str, verify: bool) -> XrayBinary:
    try:
        info = path.lstat()
    except OSError as exc:
        raise XrayNotFound(f"xray не найден по пути {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        # Follow the link for the mode check, but reject links into
        # world-writable directories.
        try:
            info = path.stat()
        except OSError as exc:
            raise XrayNotFound(f"не удалось прочитать symlink {path}") from exc
        parent = path.resolve().parent
        try:
            parent_mode = parent.stat().st_mode
        except OSError:
            parent_mode = 0
        if parent_mode & 0o002:
            raise XrayNotFound(f"xray указывает в общедоступный каталог: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise XrayNotFound(f"xray не является обычным файлом: {path}")
    if not os.access(path, os.X_OK):
        raise XrayNotFound(f"xray не исполняемый: {path}")
    version = xray_version(path) if verify else ""
    if verify and _version_tuple(version) and _version_tuple(version) < MIN_XRAY_VERSION:
        raise XrayNotFound(
            f"xray {version} слишком старый, требуется >= {'.'.join(map(str, MIN_XRAY_VERSION))}"
        )
    return XrayBinary(path=path, version=version, source=source)


def resolve_xray(
    settings: Settings | None = None,
    *,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    known_paths: Iterable[str | Path] | None = None,
    verify: bool = True,
) -> XrayBinary:
    """Resolve xray in the order: setting → PATH → known locations."""

    env = os.environ if env is None else env
    which = which or shutil.which
    configured = (settings.xray_path if settings is not None else "").strip()
    if configured:
        return _validate_binary(Path(configured).expanduser(), source="settings", verify=verify)

    found = which("xray")
    if found:
        return _validate_binary(Path(found), source="path", verify=verify)

    candidates = list(known_paths) if known_paths is not None else list(KNOWN_XRAY_PATHS)
    state_candidate = default_state_dir(env) / "xray" / "xray"
    if state_candidate not in candidates:
        candidates.append(state_candidate)
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.exists():
            try:
                return _validate_binary(path, source="known", verify=verify)
            except XrayNotFound:
                continue
    raise XrayNotFound(XrayNotFound.INSTRUCTIONS)


def download_xray(
    dest_dir: str | Path | None = None,
    *,
    consent: bool = False,
    version: str = DEFAULT_XRAY_VERSION,
    sha256: str = DEFAULT_XRAY_SHA256,
    url: str | None = None,
    opener: Callable[[str, float], object] | None = None,
    timeout: float = 60.0,
) -> Path:
    """Download and verify the official linux-amd64 archive.

    ``consent`` must be set explicitly by the user-facing command; this
    function is never called from ``connect``.
    """

    if not consent:
        raise DownloadError(
            "скачивание xray возможно только с явного согласия пользователя "
            "(ninjactl xray install --yes)"
        )
    target_dir = (
        Path(dest_dir).expanduser()
        if dest_dir is not None
        else default_state_dir() / "xray"
    )
    if target_dir.exists() and target_dir.is_symlink():
        raise DownloadError("каталог установки не должен быть symlink")
    target_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(target_dir, 0o700)

    source_url = url or (
        f"https://github.com/XTLS/Xray-core/releases/download/v{version}/Xray-linux-64.zip"
    )
    if not source_url.startswith("https://"):
        raise DownloadError("xray можно скачивать только по https")

    opener = opener or (lambda link, limit: urllib.request.urlopen(link, timeout=limit))
    try:
        with opener(source_url, timeout) as response:
            payload = response.read(200 * 1024 * 1024 + 1)
    except DownloadError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DownloadError(f"не удалось скачать xray: {exc.__class__.__name__}") from exc
    if len(payload) > 200 * 1024 * 1024:
        raise DownloadError("архив xray слишком большой")

    digest = hashlib.sha256(payload).hexdigest()
    if not sha256 or digest.lower() != sha256.lower():
        raise DownloadError(
            f"sha256 не совпал: ожидался {sha256}, получен {digest}; установка отменена"
        )

    with tempfile.TemporaryDirectory(prefix="ninjavpn-xray-", dir=str(target_dir)) as temporary:
        archive_path = Path(temporary) / "xray.zip"
        archive_path.write_bytes(payload)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                member = next(
                    (
                        item
                        for item in archive.infolist()
                        if not item.is_dir() and item.filename.rstrip("/").endswith("xray")
                    ),
                    None,
                )
                if member is None:
                    raise DownloadError("в архиве нет файла xray")
                destination = (Path(temporary) / "xray").resolve()
                member_target = (Path(temporary) / member.filename).resolve()
                if destination.parent != Path(temporary).resolve() or not str(
                    member_target
                ).startswith(str(Path(temporary).resolve()) + os.sep):
                    raise DownloadError("небезопасный путь внутри архива")
                with archive.open(member) as source, open(destination, "wb") as handle:
                    shutil.copyfileobj(source, handle, length=1024 * 1024)
        except zipfile.BadZipFile as exc:
            raise DownloadError("скачанный файл не является zip-архивом") from exc

        os.chmod(destination, 0o755)
        final_path = target_dir / "xray"
        os.replace(destination, final_path)
        os.chmod(final_path, 0o755)
    return final_path


class XrayProcess:
    """Supervise one unprivileged ``xray run`` process."""

    def __init__(
        self,
        binary: str | Path,
        config_path: str | Path,
        *,
        socks_port: int | None = None,
        startup_timeout: float = 10.0,
        stop_timeout: float = 5.0,
        health_check: Callable[[int, float], object] | None = None,
        on_log: Callable[[str, str], None] | None = None,
        on_exit: Callable[[int | None], None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.binary = Path(binary)
        self.config_path = Path(config_path)
        self.socks_port = socks_port
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self._health_check = health_check
        self.on_log = on_log or (lambda level, message: None)
        self.on_exit = on_exit
        self.env = dict(env) if env is not None else _minimal_env()
        self._process: asyncio.subprocess.Process | None = None
        self._log_tasks: list[asyncio.Task[None]] = []
        self._wait_task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _run_test(self) -> None:
        process = await asyncio.create_subprocess_exec(
            str(self.binary),
            "run",
            "-test",
            "-c",
            str(self.config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.startup_timeout)
        if process.returncode != 0:
            message = (stderr or stdout).decode("utf-8", "replace").strip()[-2000:]
            raise XrayError(f"xray -test завершился с кодом {process.returncode}: {message}")
        self.on_log("debug", "xray config test: OK")

    async def start(self) -> None:
        if self.is_running:
            return
        await self._run_test()
        self._stopping = False
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(self.binary),
                "run",
                "-c",
                str(self.config_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env=self.env,
            )
        except OSError as exc:
            raise XrayError(f"не удалось запустить xray: {exc}") from exc
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._log_tasks = [
            asyncio.create_task(self._pump(self._process.stdout, "info"), name="xray-stdout"),
            asyncio.create_task(self._pump(self._process.stderr, "error"), name="xray-stderr"),
        ]
        self._wait_task = asyncio.create_task(self._wait(), name="xray-wait")
        try:
            await self._wait_until_healthy()
        except BaseException:
            await self.stop(force=True)
            raise

    async def _pump(self, stream: asyncio.StreamReader, level: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            message = line.decode("utf-8", "replace").rstrip()
            if message:
                self.on_log(level, message)

    async def _wait(self) -> None:
        process = self._process
        if process is None:
            return
        code = await process.wait()
        for task in self._log_tasks:
            task.cancel()
        if not self._stopping and self.on_exit is not None:
            result = self.on_exit(code)
            # ``on_exit`` may be an async callback (e.g. the connection
            # manager); awaiting it here makes sure a crashed process is
            # reported instead of silently leaving a stale ``connected`` state.
            if asyncio.iscoroutine(result):
                with contextlib.suppress(Exception):
                    await result

    async def _wait_until_healthy(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if not self.is_running:
                code = self._process.returncode if self._process is not None else None
                raise XrayError(f"xray завершился при запуске (код {code})")
            healthy = False
            if self._health_check is None:
                if self.socks_port is None:
                    await asyncio.sleep(0.05)
                    return
                healthy = await _tcp_probe("127.0.0.1", self.socks_port, timeout=0.5)
            else:
                result = self._health_check(self.socks_port or 0, 0.5)
                if asyncio.iscoroutine(result):
                    result = await result
                healthy = bool(result)
            if healthy:
                # A foreign listener on the port can satisfy the probe while
                # our own process is already dead. Re-check after a short
                # grace so a failed startup (e.g. "address already in use")
                # is not mistaken for a healthy connection.
                await asyncio.sleep(0.1)
                if not self.is_running:
                    code = self._process.returncode if self._process is not None else None
                    raise XrayError(f"xray завершился при запуске (код {code})")
                return
            await asyncio.sleep(0.05)
        raise XrayError("xray не прошёл health-check за отведённое время")

    async def stop(self, *, force: bool = False) -> None:
        self._stopping = True
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                if force:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=0.2 if force else self.stop_timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=2)
        if self._wait_task is not None:
            self._wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wait_task
            self._wait_task = None
        self._process = None
        self.on_log("debug", "xray stopped")

    async def restart(self) -> None:
        await self.stop()
        await self.start()


async def _tcp_probe(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    del reader
    return True


def _minimal_env() -> dict[str, str]:
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


__all__ = [
    "DEFAULT_XRAY_SHA256",
    "DEFAULT_XRAY_VERSION",
    "DownloadError",
    "XrayBinary",
    "XrayError",
    "XrayNotFound",
    "XrayProcess",
    "download_xray",
    "resolve_xray",
    "xray_version",
]
