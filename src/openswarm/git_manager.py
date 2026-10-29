"""Async git worktree isolation: create / inject skills / diff / merge / cleanup."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

SWARM_DIR = ".swarm"


class GitError(RuntimeError):
    pass


@dataclass
class Worktree:
    task_id: str
    branch: str
    path: Path
    base: str


async def run_git(cwd: Path, *args: str, check: bool = True) -> tuple[int, str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "LC_ALL": "C"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found") from exc
    out, err = await proc.communicate()
    rc = proc.returncode or 0
    o, e = out.decode(errors="replace"), err.decode(errors="replace")
    if check and rc != 0:
        raise GitError(f"git {' '.join(args)} failed ({rc}): {(e or o).strip()}")
    return rc, o, e


class GitManager:
    def __init__(
        self,
        root: Path,
        inherit_paths: list[str] | None = None,
        link_mode: str = "symlink",
        *,
        is_git: bool = True,
        start: Path | None = None,
        state_dir: Path | None = None,
        note: str = "",
    ) -> None:
        self.root = root  # git toplevel, or the chosen directory when not a git repo
        self.start = start or root  # directory the user actually chose (chat runs here)
        self.workdir = root
        self.is_git = is_git
        self.note = note
        self.inherit_paths = inherit_paths or []
        self.link_mode = link_mode
        self.swarm_dir = root / SWARM_DIR if is_git else (state_dir or root / SWARM_DIR)
        self.worktrees_dir = self.swarm_dir / "worktrees"
        self._merge_lock = asyncio.Lock()
        self._identity: list[str] | None = None

    # ------------------------------------------------------------------ setup
    @classmethod
    async def discover(
        cls, start: Path, inherit_paths: list[str] | None = None, link_mode: str = "symlink"
    ) -> "GitManager":
        start = start.resolve()
        note = ""
        try:
            _, out, _ = await run_git(start, "rev-parse", "--show-toplevel")
            root = Path(out.strip())
            rc, _, _ = await run_git(root, "rev-parse", "--verify", "HEAD", check=False)
            if rc == 0:
                return cls(root, inherit_paths, link_mode, start=start)
            note = "Git repository has no commits yet"
        except GitError:
            note = "Not a git repository"
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        state = base / "openswarm" / "state" / hashlib.sha1(str(start).encode()).hexdigest()[:12]
        return cls(
            start, inherit_paths, link_mode, is_git=False, start=start, state_dir=state,
            note=f"{note}: Storm/Cowork run in place, one agent at a time (no isolation, no diff/merge).",
        )

    async def ensure_ready(self) -> None:
        self.swarm_dir.mkdir(parents=True, exist_ok=True)
        if not self.is_git:
            return
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        await self._exclude(f"/{SWARM_DIR}/")

    async def _exclude(self, pattern: str) -> None:
        """Add a pattern to .git/info/exclude (shared by every worktree, never committed)."""
        _, out, _ = await run_git(self.root, "rev-parse", "--git-path", "info/exclude")
        path = Path(out.strip())
        if not path.is_absolute():
            path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if pattern not in existing:
            with path.open("a", encoding="utf-8") as fh:
                if existing and existing[-1] != "":
                    fh.write("\n")
                fh.write(pattern + "\n")

    async def _identity_args(self) -> list[str]:
        if self._identity is None:
            rc, out, _ = await run_git(self.root, "config", "user.name", check=False)
            rc2, out2, _ = await run_git(self.root, "config", "user.email", check=False)
            if rc == 0 and out.strip() and rc2 == 0 and out2.strip():
                self._identity = []
            else:
                self._identity = ["-c", "user.name=OpenSwarm", "-c", "user.email=openswarm@localhost"]
        return self._identity

    # --------------------------------------------------------------- queries
    async def head(self) -> str:
        if not self.is_git:
            return ""
        _, out, _ = await run_git(self.root, "rev-parse", "HEAD")
        return out.strip()

    async def current_branch(self) -> str:
        if not self.is_git:
            return "(no git)"
        _, out, _ = await run_git(self.root, "rev-parse", "--abbrev-ref", "HEAD")
        return out.strip()

    async def status(self, cwd: Path | None = None, untracked: bool = True) -> str:
        if not self.is_git:
            return ""
        args = ["status", "--porcelain"] + ([] if untracked else ["--untracked-files=no"])
        _, out, _ = await run_git(cwd or self.root, *args)
        return out

    # ------------------------------------------------------------- worktrees
    async def create_worktree(self, task_id: str) -> Worktree:
        if not self.is_git:  # in-place: the "worktree" is the directory itself
            return Worktree(task_id, "", self.workdir, "")
        branch = f"swarm/{task_id}"
        path = self.worktrees_dir / task_id
        base = await self.head()
        if path.exists() or (await run_git(self.root, "rev-parse", "--verify", branch, check=False))[0] == 0:
            await self.cleanup(Worktree(task_id, branch, path, base))  # stale leftovers
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        await run_git(self.root, "worktree", "add", "-b", branch, str(path), "HEAD")
        wt = Worktree(task_id, branch, path, base)
        await self.inject_configs(wt)
        return wt

    async def inject_configs(self, wt: Worktree) -> list[str]:
        """Make .opencode/, opencode.json, MCP + env files visible inside the worktree."""
        injected: list[str] = []
        for name in self.inherit_paths:
            src, dst = self.root / name, wt.path / name
            if not os.path.lexists(src) or os.path.lexists(dst):
                continue  # missing in root, or already provided by the checkout (tracked)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if self.link_mode == "symlink":
                dst.symlink_to(src.resolve(), target_is_directory=src.is_dir())
            elif src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
            await self._exclude(f"/{name}")  # keep it out of `git add -A`
            injected.append(name)
        return injected

    async def commit_all(self, wt: Worktree, message: str) -> bool:
        """Commit everything the agent produced. Returns True if a commit was made."""
        if not self.is_git:
            return False
        await run_git(wt.path, "add", "-A")
        rc, _, _ = await run_git(wt.path, "diff", "--cached", "--quiet", check=False)
        if rc == 0:
            return False
        ident = await self._identity_args()
        # --no-verify: this is an intermediate agent snapshot; hooks run on the real merge.
        await run_git(wt.path, *ident, "commit", "--no-verify", "-q", "-m", message)
        return True

    async def diff(self, wt: Worktree, max_chars: int = 30_000) -> str:
        if not self.is_git:
            return "(not a git repository: no diff available - inspect the files directly)"
        _, stat, _ = await run_git(wt.path, "diff", "--stat", wt.base, "HEAD")
        _, patch, _ = await run_git(wt.path, "diff", wt.base, "HEAD")
        text = f"{stat}\n{patch}"
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [diff truncated; run `git diff` for the rest] ..."
        return text

    async def has_changes(self, wt: Worktree) -> bool:
        if not self.is_git:
            return True  # cannot tell; let the reviewer decide
        _, out, _ = await run_git(wt.path, "rev-list", "--count", f"{wt.base}..HEAD")
        return int(out.strip() or 0) > 0

    async def merge(self, wt: Worktree) -> tuple[bool, str]:
        """Merge swarm/<task_id> into the root repo's current branch (serialised)."""
        if not self.is_git:
            return True, "applied in place"
        async with self._merge_lock:
            if not await self.has_changes(wt):
                return True, "no changes to merge"
            ident = await self._identity_args()
            rc, out, err = await run_git(
                self.root, *ident, "merge", "--no-ff", "--no-edit", "-m", f"swarm: merge {wt.task_id}", wt.branch,
                check=False,
            )
            if rc == 0:
                return True, out.strip()
            await run_git(self.root, "merge", "--abort", check=False)
            return False, (err or out).strip()

    async def cleanup(self, wt: Worktree, delete_branch: bool = True) -> None:
        if not self.is_git:
            return
        rc, _, _ = await run_git(self.root, "worktree", "remove", "--force", str(wt.path), check=False)
        if rc != 0 and wt.path.exists():
            shutil.rmtree(wt.path, ignore_errors=True)
        await run_git(self.root, "worktree", "prune", check=False)
        if delete_branch:
            await run_git(self.root, "branch", "-D", wt.branch, check=False)
