"""Safety net: bubblewrap sandbox, allowed roots, audit log, snapshots/undo and git diffs."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

from .config import SecuritySettings, SwarmConfig
from .state import state_path

EXCLUDE_DIRS = {".git", ".swarm", "node_modules", "__pycache__", ".venv", ".cache", "target", "dist", "build"}


class SandboxError(RuntimeError):
    pass


# --------------------------------------------------------------------- sandbox
def bwrap_command(cmd: list[str], cwd: Path, sec: SecuritySettings, extra_rw: list[Path] | None = None) -> list[str]:
    """Wrap `cmd` in bubblewrap: whole system read-only, only the workdir (+opencode state) writable, secrets hidden."""
    exe = shutil.which("bwrap")
    if not exe:
        raise SandboxError("sandbox is on but bubblewrap is missing: sudo pacman -S bubblewrap (or set security.sandbox: off)")
    home = Path.home()
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")
    xdg_cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    xdg_state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state")
    xdg_cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    rw: dict[str, None] = {}
    for p in [cwd, *(extra_rw or []), xdg_data / "opencode", xdg_cfg / "opencode", xdg_state / "opencode",
              xdg_cache / "opencode", *(Path(x).expanduser() for x in sec.sandbox_extra_rw)]:
        if "opencode" in str(p):
            p.mkdir(parents=True, exist_ok=True)
        if p.exists():
            rw[str(p.resolve())] = None
    args = [exe, "--die-with-parent", "--unshare-pid", "--new-session", "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for p in rw:  # binds come after --tmpfs /tmp so a workdir under /tmp stays visible
        args += ["--bind", p, p]
    for h in sec.sandbox_hide:
        t = home / h
        if t.is_dir():
            args += ["--tmpfs", str(t)]
        elif t.exists():
            args += ["--ro-bind", "/dev/null", str(t)]
    if not sec.sandbox_network:
        args.append("--unshare-net")
    return [*args, "--chdir", str(cwd), "--", *cmd]


def maybe_sandbox(cmd: list[str], cwd: Path, cfg: SwarmConfig, enabled: bool | None = None, extra_rw: list[Path] | None = None) -> list[str]:
    on = (cfg.security.sandbox == "bwrap") if enabled is None else enabled
    return bwrap_command(cmd, cwd, cfg.security, extra_rw) if on else cmd


# -------------------------------------------------------------- allowed roots
def path_allowed(cfg: SwarmConfig, path: Path) -> bool:
    roots = [Path(r).expanduser().resolve() for r in cfg.security.allowed_roots]
    if not roots:
        return True
    p = path.resolve()
    return any(p == r or r in p.parents for r in roots)


# ------------------------------------------------------------------- audit log
def audit_path() -> Path:
    return state_path().parent / "audit.log"


def audit(cfg: SwarmConfig, event: str, **data: Any) -> None:
    if not cfg.security.audit:
        return
    path = audit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 10_000_000:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": round(time.time(), 2), "event": event, **data}, default=str) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------- snapshots
def _snap_dir(workdir: Path) -> Path:
    import hashlib

    return state_path().parent / "snapshots" / hashlib.sha1(str(workdir.resolve()).encode()).hexdigest()[:12]


def _walk(workdir: Path):
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            yield Path(root) / f


def create_snapshot(workdir: Path, label: str, limit_mb: int, keep: int) -> str | None:
    """tar.gz of the directory (skips heavy/VCS dirs). Returns None if it would exceed the size limit."""
    total = 0
    for f in _walk(workdir):
        try:
            total += f.lstat().st_size
        except OSError:
            continue
        if total > limit_mb * 1024 * 1024:
            return None
    sd = _snap_dir(workdir)
    sd.mkdir(parents=True, exist_ok=True)
    sid = time.strftime("%Y%m%d-%H%M%S") + "-" + "".join(c for c in label.lower() if c.isalnum())[:12]
    with tarfile.open(sd / f"{sid}.tar.gz", "w:gz") as tf:
        for f in _walk(workdir):
            tf.add(f, arcname=str(f.relative_to(workdir)), recursive=False)
    for old in sorted(sd.glob("*.tar.gz"))[:-keep]:
        old.unlink(missing_ok=True)
    return sid


def list_snapshots(workdir: Path) -> list[str]:
    return sorted((p.name[:-7] for p in _snap_dir(workdir).glob("*.tar.gz")), reverse=True)


def restore_snapshot(workdir: Path, sid: str) -> dict[str, int]:
    """Make the directory match the snapshot: restore changed/deleted files, delete files created since."""
    tpath = _snap_dir(workdir) / f"{sid}.tar.gz"
    if not tpath.is_file():
        raise FileNotFoundError(sid)
    wd = workdir.resolve()
    keep: set[str] = set()
    restored = removed = 0
    with tarfile.open(tpath) as tf:
        for m in tf.getmembers():
            target = (wd / m.name).resolve()
            if not (target == wd or wd in target.parents):  # never write outside the workdir
                continue
            keep.add(m.name)
            if m.isfile() or m.issym():
                cur = wd / m.name
                if m.isfile() and cur.is_file() and cur.stat().st_size == m.size and int(cur.stat().st_mtime) == int(m.mtime):
                    continue
                tf.extract(m, wd, filter="data") if hasattr(tarfile, "data_filter") else tf.extract(m, wd)
                restored += 1
    for f in list(_walk(wd)):
        if str(f.relative_to(wd)) not in keep:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return {"restored": restored, "removed": removed}


# --------------------------------------------------------------------- git diff
def git_diff_summary(workdir: Path, max_chars: int = 60_000) -> dict[str, Any]:
    """Changes in a git working tree (tracked + untracked) as stat + patch; empty dict if not a repo."""
    def run(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=workdir, capture_output=True, text=True, timeout=30).stdout

    try:
        if run("rev-parse", "--is-inside-work-tree").strip() != "true":
            return {}
        untracked = [x for x in run("ls-files", "--others", "--exclude-standard").splitlines() if x][:200]
        stat = run("diff", "--stat", "HEAD")
        patch = run("diff", "HEAD")
        if untracked:
            stat += "\nuntracked:\n" + "\n".join(f"  {u}" for u in untracked)
        return {"stat": stat.strip(), "patch": patch[:max_chars], "truncated": len(patch) > max_chars, "files": len(untracked) + stat.count("|")}
    except (OSError, subprocess.SubprocessError):
        return {}
