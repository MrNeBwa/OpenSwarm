"""Schedules and file triggers (run while `openswarm web` is running)."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .web import WebApp


def parse_every(spec: str) -> tuple[str, float, int, int]:
    """'30m' | '2h' | '45s' -> ('interval', seconds, 0, 0); 'daily 09:30' -> ('daily', 0, 9, 30)."""
    spec = spec.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhd])", spec)
    if m:
        return "interval", int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)], 0, 0
    m = re.fullmatch(r"daily\s+(\d{1,2}):(\d{2})", spec)
    if m:
        return "daily", 0, int(m.group(1)), int(m.group(2))
    raise ValueError(f"cannot parse schedule {spec!r} (use 30m, 2h or 'daily 09:30')")


class Automation:
    def __init__(self, app: "WebApp") -> None:
        self.app = app
        self.last: dict[str, float] = {}
        self.day: dict[str, str] = {}
        self.seen: dict[str, float] = {}
        self.baseline = False

    def _body(self, preset_id: str, mode: str, prompt: str) -> dict:
        cfg = self.app.cfg
        preset = next((p for p in cfg.presets if p.id == preset_id), None)
        return {
            "prompt": prompt, "mode": preset.mode if preset else mode,
            "skills": preset.skills if preset else [s.id for s in cfg.skills if s.default],
            "models": {"planner": cfg.roles.planner, "coder": cfg.roles.coder, "reviewer": cfg.roles.reviewer},
        }

    async def fire(self, key: str, title: str, workdir: Path, mode: str, preset: str, prompt: str) -> bool:
        sess = next((s for s in self.app.sessions.values() if s.title == title and str(s.workdir) == str(workdir)), None)
        if sess is None:
            sess = self.app.new_session(workdir, mode, title)
        if sess.busy:
            return False
        try:
            await self.app.start_run(sess, self._body(preset, mode, prompt))
        except Exception as exc:  # noqa: BLE001 - automation must never crash the server
            sess.emit("log", text=f"automation '{key}' failed to start: {exc}")
            return False
        return True

    async def tick(self) -> None:
        now = time.time()
        lt = time.localtime(now)
        for sc in self.app.cfg.schedules:
            try:
                kind, secs, hh, mm = parse_every(sc.every)
            except ValueError:
                continue
            wd = Path(sc.workdir).expanduser().resolve()
            if not wd.is_dir():
                continue
            if kind == "interval":
                first = sc.id not in self.last
                self.last.setdefault(sc.id, now)
                due = not first and now - self.last[sc.id] >= secs
            else:
                today = time.strftime("%Y-%m-%d", lt)
                due = (lt.tm_hour, lt.tm_min) >= (hh, mm) and self.day.get(sc.id) != today
            if due and await self.fire(f"schedule:{sc.id}", f"⏰ {sc.id}", wd, sc.mode, sc.preset, sc.prompt):
                self.last[sc.id] = now
                self.day[sc.id] = time.strftime("%Y-%m-%d", lt)
        for tr in self.app.cfg.triggers:
            wd = Path(tr.watch).expanduser().resolve()
            if not wd.is_dir():
                continue
            for f in wd.glob(tr.glob):
                if not f.is_file():
                    continue
                key = str(f)
                mt = f.stat().st_mtime
                if not self.baseline:
                    self.seen[key] = mt  # files that already existed at start-up never trigger
                elif self.seen.get(key) != mt and now - mt >= tr.debounce_s:
                    self.seen[key] = mt
                    await self.fire(f"trigger:{tr.id}", f"👁 {tr.id}", wd, tr.mode, tr.preset, tr.prompt.replace("{file}", str(f)))
        self.baseline = True

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(15)
