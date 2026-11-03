"""OpenSwarm Web: a local app (asyncio HTTP + Server-Sent Events, standard library only).

    openswarm web            # prints a private URL (token), opens your browser
    openswarm web --app      # opens it as a chromeless "app" window when Chromium/Chrome/Brave is installed

Security model: binds to 127.0.0.1 only, every API call needs the random token printed at start-up, and the
Host/Origin headers are checked (blocks DNS-rebinding and drive-by requests from other web pages).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import __version__
from .broker import Broker
from .config import ConfigError, ModelSpec, SwarmConfig, discover_models, find_config_path, load_config, reconcile_models
from .docs import DocError, read_file
from .git_manager import GitError, GitManager
from .automation import Automation
from .orchestrator import Hooks, Orchestrator, Selection
from .keys import configured_keys, get_pool, key_id, key_sources, load_stored_keys, mask, parse_keys, save_stored_keys
from .safety import audit_path, git_diff_summary, list_snapshots, path_allowed, restore_snapshot, audit
from .servers import ServerPool
from .skillpack import discover_skills
from .state import MODES, apply_state, load_state, save_state, snapshot, state_path

MAX_BODY = 64 * 1024 * 1024
REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict", 413: "Payload Too Large", 500: "Internal Server Error"}
HIDDEN = {".git", ".swarm", "__pycache__", "node_modules", ".venv"}


class HttpError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.status, self.msg = status, msg


def sessions_dir() -> Path:
    return state_path().parent / "sessions"


class Session:
    """One conversation/run history (a browser tab). Several can be running at the same time."""

    def __init__(self, sid: str, workdir: Path, mode: str, title: str = "New session") -> None:
        self.id, self.workdir, self.mode, self.title = sid, workdir, mode, title
        self.created = self.updated = time.time()
        self.oc_session = ""  # opencode session id (chat continuity)
        self.turns = 0
        self.busy = False
        self.task: asyncio.Task | None = None
        self.events: list[dict[str, Any]] = []
        self.loaded = True
        self.listeners: set[asyncio.Queue] = set()
        self.perms: dict[str, asyncio.Future] = {}
        self.snapshot = ""
        self.orch: Any = None

    # ---------------------------------------------------------------- storage
    @property
    def meta_path(self) -> Path:
        return sessions_dir() / f"{self.id}.json"

    @property
    def log_path(self) -> Path:
        return sessions_dir() / f"{self.id}.jsonl"

    def meta(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "workdir": str(self.workdir), "mode": self.mode,
                "created": self.created, "updated": self.updated, "oc_session": self.oc_session,
                "turns": self.turns, "busy": self.busy}

    def save_meta(self) -> None:
        try:
            sessions_dir().mkdir(parents=True, exist_ok=True)
            self.meta_path.write_text(json.dumps(self.meta()), encoding="utf-8")
        except OSError:
            pass

    @classmethod
    def from_meta(cls, m: dict[str, Any]) -> "Session":
        s = cls(m["id"], Path(m["workdir"]), m.get("mode", "chat"), m.get("title", "Session"))
        s.created, s.updated = m.get("created", time.time()), m.get("updated", time.time())
        s.oc_session, s.turns = m.get("oc_session", ""), m.get("turns", 0)
        s.loaded = False
        return s

    def load_events(self) -> None:
        if self.loaded:
            return
        self.loaded = True
        try:
            with self.log_path.open(encoding="utf-8") as fh:
                self.events = [json.loads(ln) for ln in fh if ln.strip()][-50_000:]
        except (OSError, ValueError):
            self.events = []

    def build_conversation_history(self, max_turns: int = 12, max_chars: int = 14_000) -> str:
        self.load_events()
        blocks: list[str] = []
        user_msg = ""
        for ev in self.events:
            ev_type = ev.get("type")
            if ev_type == "user":
                user_msg = str(ev.get("text") or "").strip()
            elif ev_type in ("result", "done"):
                res_text = str(ev.get("text") or "").strip()
                if not res_text and ev_type == "done":
                    continue
                if user_msg:
                    blocks.append(f"User: {user_msg}\nAssistant: {res_text[:2500]}")
                    user_msg = ""
        if user_msg:
            blocks.append(f"User: {user_msg}")
        recent = blocks[-max_turns:]
        history = "\n\n".join(recent)
        if len(history) > max_chars:
            history = history[-max_chars:]
        return history

    # ----------------------------------------------------------------- events
    def emit(self, type_: str, **data: Any) -> dict[str, Any]:
        self.load_events()
        data = {k: v for k, v in data.items() if k not in ("id", "t", "type")}  # reserved
        ev = {"id": (self.events[-1]["id"] + 1) if self.events else 1, "t": round(time.time(), 2), "type": type_, **data}
        self.events.append(ev)
        self.updated = ev["t"]
        try:
            sessions_dir().mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev) + "\n")
        except OSError:
            pass
        for q in list(self.listeners):
            q.put_nowait(ev)
        return ev


class WebApp:
    def __init__(self, cfg: SwarmConfig, workdir: Path, port: int, token: str) -> None:
        self.cfg, self.workdir, self.port, self.token = cfg, workdir, port, token
        self.pool = ServerPool(cfg)
        self.automation = Automation(self)
        self.sessions: dict[str, Session] = {}
        self._ws: dict[str, tuple[GitManager, Broker]] = {}
        from .integrations import IntegrationSettings
        from .scheduler import TaskScheduler

        self.integrations_path = state_path().parent / "integrations.json"
        self.integrations = self._load_integrations()
        self.scheduler = TaskScheduler(dispatch_fn=self._dispatch_scheduled_task)
        self.scheduler.start()
        try:
            for mp in sorted(sessions_dir().glob("*.json")):
                m = json.loads(mp.read_text(encoding="utf-8"))
                self.sessions[m["id"]] = Session.from_meta(m)
        except (OSError, ValueError, KeyError):
            pass

    def _load_integrations(self):
        from .integrations import IntegrationSettings
        if self.integrations_path.exists():
            try:
                data = json.loads(self.integrations_path.read_text(encoding="utf-8"))
                return IntegrationSettings.from_dict(data)
            except Exception:
                pass
        return IntegrationSettings()

    def _save_integrations(self) -> None:
        try:
            self.integrations_path.parent.mkdir(parents=True, exist_ok=True)
            self.integrations_path.write_text(json.dumps(self.integrations.to_dict(), indent=2), encoding="utf-8")
        except Exception:
            pass

    async def _dispatch_scheduled_task(self, task) -> None:
        sess = self.sessions.get(task.session_id)
        if not sess:
            sess = self.new_session(self.workdir, task.mode or "chat", f"Scheduled: {task.prompt[:30]}")
        body = {
            "prompt": task.prompt,
            "mode": task.mode,
            "priority": task.priority,
            "effort": task.effort,
            "target_agents": task.target_agents,
        }
        await self.start_run(sess, body)

    async def _notify_integrations(self, s: Session, prompt: str, summary) -> None:
        from .integrations import send_telegram, send_discord, send_email
        integ = self.integrations
        status_emoji = "✅" if summary.ok else "❌"
        status_text = "Completed successfully" if summary.ok else "Encountered errors"
        title = f"OpenSwarm: {s.title}"
        summary_text = (
            f"<b>{title}</b>\n"
            f"Status: {status_emoji} {status_text}\n"
            f"Mode: <code>{summary.mode}</code>\n"
            f"Task: {prompt[:300]}\n"
        )
        if summary.merged:
            summary_text += f"Merged: {', '.join(summary.merged)}\n"
        if summary.failed:
            summary_text += f"Failed: {', '.join(summary.failed)}\n"

        if integ.telegram_enabled and integ.telegram_bot_token and integ.telegram_chat_id and not getattr(integ, "telegram_require_confirmation", True):
            asyncio.create_task(send_telegram(integ.telegram_bot_token, integ.telegram_chat_id, summary_text, confirmed=True))
        if integ.discord_enabled and integ.discord_webhook_url:
            fields = [
                {"name": "Mode", "value": summary.mode, "inline": True},
                {"name": "Status", "value": status_text, "inline": True},
            ]
            asyncio.create_task(send_discord(
                integ.discord_webhook_url,
                title,
                f"Task: {prompt[:500]}\n\n" + (summary.result[:1000] if summary.result else ""),
                color=0x4E9F6E if summary.ok else 0xD95858,
                fields=fields,
            ))
        if integ.email_enabled and integ.smtp_host and integ.smtp_to:
            body = f"OpenSwarm Task Report: {s.title}\nStatus: {status_text}\nMode: {summary.mode}\n\nTask:\n{prompt}\n\nResult:\n{summary.result}\n"
            asyncio.create_task(send_email(integ, f"[{status_emoji}] OpenSwarm: {s.title}", body))

    # -------------------------------------------------------------- workspace
    async def workspace(self, path: Path) -> tuple[GitManager, Broker]:
        key = str(path)
        if key not in self._ws:
            git = await GitManager.discover(path, self.cfg.opencode.inherit_paths, self.cfg.opencode.link_mode)
            await git.ensure_ready()
            self._ws[key] = (git, Broker(git.swarm_dir / "swarm.db"))
        return self._ws[key]

    # ------------------------------------------------------------------ state
    def mode_list(self) -> list[dict[str, str]]:
        core = [("auto", "✨", "Auto", "Intelligently auto-selects the optimal workflow, models & effort"),
                ("chat", "💬", "Chat", "One agent, continuous session, in any directory"),
                ("cowork", "🤝", "Cowork", "Architect → coder → reviewer loop"),
                ("heal", "🩺", "Heal & Fix", "System self-repair + root-cause diagnosis → coder fix → verify"),
                ("architect", "📐", "Architect", "Parallel System Architect + Risk/Test Strategist → Blueprint"),
                ("storm", "⚡", "Storm", "Planner splits the task → parallel workers (git worktrees) → merge"),
                ("best", "🏆", "Best of N", "N independent attempts → verify → judge → merge the winner (git)"),
                ("debate", "⚖️", "Debate", "Multi-round adversarial debate (Advocate / Skeptic / Pragmatist) → Moderator verdict"),
                ("research", "🔎", "Research", "Parallel researchers → synthesized, cited report"),
                ("write", "✍️", "Write", "Outline → parallel section drafts → editor"),
                ("review", "🛡️", "Review", "Parallel review lenses → prioritized report")]
        out = [{"id": i, "icon": ic, "label": lb, "desc": d, "group": "Modes"} for i, ic, lb, d in core]
        out += [{"id": f"wf:{w.id}", "icon": "🧩", "label": w.label, "desc": w.description, "group": "Workflows"} for w in self.cfg.workflows]
        return out

    def valid_mode(self, m: Any) -> bool:
        return isinstance(m, str) and m in {x["id"] for x in self.mode_list()}

    def state(self) -> dict[str, Any]:
        from .dsh import dsh_status
        from .kilo import kilo_status
        from .router import get_router, priority_list
        from .optimizer import get_optimizer_stats

        c = self.cfg
        router = get_router()
        return {
            "version": __version__, "workdir": str(self.workdir), "home": str(Path.home()),
            "modes": self.mode_list(), "mode": c.default_mode, "parallel": c.max_parallel,
            "priority": c.priority, "priorities": priority_list(),
            "effort": getattr(c, "effort", "medium"),
            "efforts": ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
            "recommended_roles": router.recommend_roles(c, c.priority),
            "dsh": dsh_status(),
            "kilo": kilo_status(),
            "optimizer": get_optimizer_stats(),
            "sandbox": c.security.sandbox == "bwrap", "bwrap": bool(shutil.which("bwrap")),
            "allowed_roots": c.security.allowed_roots,
            "native_picker": bool(self.native_picker_cmd()),
            "automation": {"schedules": [x.id for x in c.schedules], "triggers": [x.id for x in c.triggers]},
            "integrations": self.integrations.to_dict(),
            "scheduled_count": len([t for t in self.scheduler.tasks.values() if t.status == "pending"]),
            "models": [{"id": m.id, "label": m.display()} for m in c.models],
            "roles": {"planner": c.roles.planner, "coder": c.roles.coder, "reviewer": c.roles.reviewer},
            "skills": [{"id": s.id, "label": s.label, "category": s.category, "default": s.default, "hint": s.prompt_hint} for s in c.skills],
            "presets": [p.model_dump() for p in c.presets],
            "native_skills": discover_skills(self.workdir),
            "sessions": sorted((s.meta() for s in self.sessions.values()), key=lambda m: -m["updated"]),
        }

    # ------------------------------------------------------------------- runs
    def new_session(self, workdir: Path, mode: str, title: str) -> Session:
        s = Session(uuid.uuid4().hex[:8], workdir, mode, title or "New session")
        self.sessions[s.id] = s
        s.save_meta()
        return s

    def fork_session(self, parent_id: str) -> Session:
        parent = self.sessions.get(parent_id)
        if not parent:
            raise HttpError(404, "parent session not found")
        parent.load_events()
        new_id = uuid.uuid4().hex[:8]
        new_s = Session(new_id, parent.workdir, parent.mode, f"{parent.title} (fork)")
        new_s.oc_session = parent.oc_session
        new_s.turns = parent.turns
        new_s.save_meta()
        for ev in parent.events:
            new_s.events.append(dict(ev))
        try:
            sessions_dir().mkdir(parents=True, exist_ok=True)
            with new_s.log_path.open("w", encoding="utf-8") as fh:
                for ev in new_s.events:
                    fh.write(json.dumps(ev) + "\n")
        except OSError:
            pass
        self.sessions[new_id] = new_s
        return new_s

    async def start_run(self, s: Session, body: dict[str, Any]) -> None:
        if s.busy:
            raise HttpError(409, "This session is busy - open another session or cancel the run.")
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            raise HttpError(400, "empty prompt")
        mode = body.get("mode") if self.valid_mode(body.get("mode")) else s.mode
        models = body.get("models") or {}
        skills = [x for x in body.get("skills", []) if isinstance(x, str)]
        parallel = body.get("parallel")
        priority = str(body.get("priority") or self.cfg.priority)
        effort = str(body.get("effort") or getattr(self.cfg, "effort", "medium"))
        cfg = copy.copy(self.cfg)  # per-run overrides must not leak into other sessions
        if isinstance(parallel, int) and 0 <= parallel <= 64:
            cfg.max_parallel = parallel
        if priority in ("balanced", "quality", "fast", "free", "local"):
            cfg.priority = priority
            self.cfg.priority = priority
        if effort in ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max"):
            cfg.effort = effort
            self.cfg.effort = effort
        history_context = s.build_conversation_history()
        target_agents = int(body.get("target_agents") or body.get("agents") or parallel or 0)
        files = [str(f) for f in (body.get("files") or []) if isinstance(f, str)]
        images = [str(img) for img in (body.get("images") or []) if isinstance(img, str)]
        focus = str(body.get("focus") or "auto")
        sel = Selection(
            mode=mode, planner_model=str(models.get("planner", "")), coder_model=str(models.get("coder", "")),
            reviewer_model=str(models.get("reviewer", "")), skills=skills, chat_id=f"chat-{s.id}",
            chat_title=s.title, workdir=s.workdir, session_id=s.oc_session,
            continue_session=bool(s.turns > 0 and not s.oc_session and mode == "chat"),
            sandbox=body.get("sandbox") if isinstance(body.get("sandbox"), bool) else None,
            candidate_models=[m for m in (body.get("candidates") or []) if isinstance(m, str)],
            priority=cfg.priority,
            effort=cfg.effort,
            history_context=history_context,
            target_agents=target_agents,
            files=files,
            images=images,
            focus=focus,
        )
        try:
            git, broker = await self.workspace(s.workdir)
        except (GitError, OSError) as exc:
            raise HttpError(400, str(exc))
        s.mode = mode
        if s.title in ("New session", "") and s.turns == 0:
            s.title = prompt.replace("\n", " ")[:48]
        s.busy = True
        s.save_meta()
        st = snapshot(mode, sel.planner_model or cfg.roles.planner, sel.coder_model or cfg.roles.coder,
                      sel.reviewer_model or cfg.roles.reviewer, skills, cfg.priority, cfg.effort)
        st["parallel"] = cfg.max_parallel
        save_state(st)
        s.emit("user", text=prompt, mode=mode, images=images, files=files, focus=focus)
        hooks = Hooks(
            on_agent=lambda a, t, r: s.emit("agent", agent=a, title=t, role=r),
            on_status=lambda a, st_: s.emit("status", agent=a, status=st_),
            on_line=lambda a, t, e: s.emit("line", agent=a, text=t[:3000], err=bool(e)),
            on_log=lambda m: s.emit("log", text=m),
            on_result=lambda a, t, k: s.emit("result", agent=a, text=t, kind=k),
            on_event=lambda a, k, p: self._on_event(s, a, k, p),
            ask_permission=lambda a, req: self._ask(s, a, req),
        )
        orch = Orchestrator(cfg, git, broker, sel, hooks, self.pool)
        s.orch = orch
        s.task = asyncio.create_task(self._run(s, orch, prompt, mode))

    def _on_event(self, s: Session, agent: str, kind: str, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        if "id" in payload:  # never let a payload field shadow the event's own sequence number "id"
            payload["tid"] = payload.pop("id")
        if kind == "snapshot":
            s.snapshot = payload.get("tid", "")
        if kind == "permission":
            s.emit("permission", agent=agent, req={**payload, "id": payload.get("tid")})
        elif kind == "permission_resolved":
            s.emit("permission_resolved", pid=payload.get("tid"), decision=payload.get("decision"))
        elif kind == "bus":
            s.emit("bus", mid=payload.get("tid"), sender=agent, recipient=payload.get("recipient", "all"),
                   kind=payload.get("kind", "chat"), content=payload.get("content", ""), reply_to=payload.get("reply_to", 0))
        else:
            s.emit(kind, agent=agent, **payload)

    async def _ask(self, s: Session, agent: str, req: dict[str, Any]) -> str:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        s.perms[str(req.get("id"))] = fut
        try:
            return await fut
        finally:
            s.perms.pop(str(req.get("id")), None)

    async def _run(self, s: Session, orch: Orchestrator, prompt: str, mode: str) -> None:
        try:
            summary = await orch.run(prompt)
            s.turns += 1
            if summary.session_id:
                s.oc_session = summary.session_id
                s.emit("log", text=f"opencode session {summary.session_id} captured")
            s.emit("done", ok=summary.ok, merged=summary.merged, failed=summary.failed, snapshot=summary.snapshot, usage=summary.usage, mode=mode)
            asyncio.create_task(self._notify_integrations(s, prompt, summary))
        except asyncio.CancelledError:
            s.emit("done", ok=False, merged=[], failed=["cancelled"])
        except Exception as exc:  # never let one run take the server down
            s.emit("log", text=f"run crashed: {exc!r}")
            s.emit("done", ok=False, merged=[], failed=["crashed"])
        finally:
            for fut in s.perms.values():
                if not fut.done():
                    fut.set_result("reject")
            s.busy, s.task = False, None
            s.save_meta()

    # ---------------------------------------------------------------- project files
    def agents_md(self, s: Session) -> str:
        p = s.workdir / "AGENTS.md"
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

    async def shutdown(self) -> None:
        self.scheduler.stop()
        for s in self.sessions.values():
            if s.task:
                s.task.cancel()
        await asyncio.sleep(0)
        await self.pool.close()
        for _, broker in self._ws.values():
            broker.close()

    # ------------------------------------------------------------------ files
    @staticmethod
    def _inside(root: Path, rel: str) -> Path:
        p = (root / rel).resolve()
        if p != root.resolve() and root.resolve() not in p.parents:
            raise HttpError(403, "path escapes the session directory")
        return p

    def list_dir(self, s: Session, rel: str) -> dict[str, Any]:
        p = self._inside(s.workdir, rel)
        if not p.is_dir():
            raise HttpError(404, "not a directory")
        items = []
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if e.name in HIDDEN:
                continue
            try:
                items.append({"name": e.name, "dir": e.is_dir(), "size": 0 if e.is_dir() else e.stat().st_size})
            except OSError:
                continue
        return {"path": str(p.relative_to(s.workdir.resolve())) if p != s.workdir.resolve() else "", "items": items[:500]}

    def read(self, s: Session, rel: str) -> dict[str, Any]:
        p = self._inside(s.workdir, rel)
        if not p.is_file():
            raise HttpError(404, "no such file")
        try:
            text = read_file(p)
        except (DocError, OSError, Exception) as exc:  # noqa: BLE001 - report any parser failure to the UI
            return {"name": p.name, "text": f"(cannot display: {exc})", "truncated": False}
        return {"name": p.name, "text": text[:200_000], "truncated": len(text) > 200_000}

    def export_session_md(self, s: Session) -> str:
        """Render the full session conversation, logs and results as a portable Markdown transcript."""
        s.load_events()
        lines = [
            f"# {s.title}",
            f"- **Session ID:** `{s.id}`",
            f"- **Mode:** `{s.mode}`",
            f"- **Working directory:** `{s.workdir}`",
            "",
        ]
        for ev in s.events:
            t = ev.get("type")
            if t == "user":
                lines += ["---", f"## 🧑 User ({ev.get('mode', s.mode)})", "", str(ev.get("text", "")), ""]
            elif t == "log":
                lines.append(f"> ℹ️ {ev.get('text', '')}")
            elif t == "result":
                lines += [f"## 🤖 Result ({ev.get('kind') or ev.get('agent') or 'agent'})", "", str(ev.get("text", "")), ""]
            elif t == "done":
                st = "✔ OK" if ev.get("ok") else ("✘ Failed: " + ", ".join(ev.get("failed") or []))
                u = ev.get("usage") or {}
                tok = u.get("tokens") or {}
                u_str = f" · {tok.get('input', 0)}→{tok.get('output', 0)} tokens" if tok else ""
                if u.get("cost"):
                    u_str += f" · ${float(u['cost']):.4f}"
                lines += ["", f"*Run finished: {st}{u_str}*", ""]
        return "\n".join(lines).strip() + "\n"

    def _shortcuts(self) -> list[dict[str, str]]:
        """Quick places for the folder picker (only ones that exist and are allowed)."""
        home = Path.home()
        cands: list[tuple[str, Path]] = [("Домашняя", home), ("Рабочий стол", home / "Desktop"), ("Документы", home / "Documents"),
                                         ("Загрузки", home / "Downloads")]
        for n in ("Projects", "projects", "dev", "code", "src", "work", "repos", "git"):
            cands.append((f"~/{n}", home / n))
        cands += [("Текущая папка OpenSwarm", self.workdir), ("Корень /", Path("/")), ("/mnt", Path("/mnt")), ("/tmp", Path("/tmp"))]
        media = Path("/run/media") / (os.environ.get("USER") or "")
        if os.environ.get("USER") and media.is_dir():
            cands.append(("Диски", media))
        out, seen = [], set()
        for name, p in cands:
            try:
                rp = p.resolve()
                if str(rp) in seen or not rp.is_dir() or not path_allowed(self.cfg, rp):
                    continue
            except OSError:
                continue
            seen.add(str(rp))
            out.append({"name": name, "path": str(rp)})
        return out

    def _recent_dirs(self, limit: int = 8) -> list[str]:
        seen: list[str] = []
        for s in sorted(self.sessions.values(), key=lambda x: -x.meta().get("updated", 0)):
            w = str(s.workdir)
            if w not in seen and Path(w).is_dir() and path_allowed(self.cfg, Path(w)):
                seen.append(w)
            if len(seen) >= limit:
                break
        return seen

    def list_dirs(self, path: str, hidden: bool = False) -> dict[str, Any]:
        raw = (path or "").strip()
        try:
            p = Path(raw or Path.home()).expanduser().resolve()
        except (OSError, RuntimeError):
            p = Path.home()
        prefix = ""
        if not p.is_dir():  # typed half-way ("/home/me/pro"): show the nearest existing folder, filtered by the rest
            if p.parent.is_dir():
                prefix, p = p.name, p.parent
            else:
                while not p.is_dir() and p != p.parent:
                    p = p.parent
        if not path_allowed(self.cfg, p):
            roots = self.cfg.security.allowed_roots
            p = Path(roots[0]).expanduser().resolve()
            prefix = ""
        if not p.is_dir():
            raise HttpError(404, "not a directory")
        subs: list[dict[str, Any]] = []
        denied = False
        try:
            for e in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                try:
                    if not e.is_dir():
                        continue
                except OSError:
                    continue
                if e.name.startswith(".") and not hidden:
                    continue
                subs.append({"name": e.name, "git": (e / ".git").exists(), "hidden": e.name.startswith(".")})
        except PermissionError:
            denied = True
        except OSError:
            pass
        crumbs, cur = [], p
        while True:
            crumbs.append({"name": cur.name or "/", "path": str(cur)})
            if cur.parent == cur:
                break
            cur = cur.parent
        crumbs.reverse()
        return {
            "path": str(p), "parent": str(p.parent) if p.parent != p and path_allowed(self.cfg, p.parent) else "",
            "dirs": [d["name"] for d in subs[:800]], "items": subs[:800], "prefix": prefix, "crumbs": crumbs,
            "is_git": (p / ".git").exists(), "writable": os.access(p, os.W_OK), "denied": denied,
            "shortcuts": self._shortcuts(), "recent": self._recent_dirs(), "home": str(Path.home()),
        }

    def make_dir(self, parent: str, name: str) -> dict[str, Any]:
        name = (name or "").strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
            raise HttpError(400, "некорректное имя папки")
        base = Path(parent or Path.home()).expanduser().resolve()
        if not base.is_dir():
            raise HttpError(404, "not a directory")
        target = base / name
        if not path_allowed(self.cfg, target):
            raise HttpError(403, f"{target} is outside security.allowed_roots")
        try:
            target.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            raise HttpError(409, "папка с таким именем уже существует") from None
        except OSError as exc:
            raise HttpError(400, f"не удалось создать папку: {exc.strerror or exc}") from None
        audit(self.cfg, "mkdir", path=str(target))
        return self.list_dirs(str(target))

    @staticmethod
    def native_picker_cmd() -> str:
        return "zenity" if shutil.which("zenity") else ("kdialog" if shutil.which("kdialog") else "")

    def pick_dir_native(self, start: str) -> dict[str, Any]:
        """System folder dialog (zenity / kdialog) - the server runs on the same machine as the window."""
        tool = self.native_picker_cmd()
        if not tool:
            raise HttpError(400, "нет zenity/kdialog: sudo pacman -S zenity")
        try:
            base = Path(start or Path.home()).expanduser()
            while not base.is_dir() and base != base.parent:
                base = base.parent
        except (OSError, RuntimeError):
            base = Path.home()
        start_s = str(base).rstrip("/") + "/"
        cmd = (["zenity", "--file-selection", "--directory", "--title=Выберите рабочую папку", f"--filename={start_s}"]
               if tool == "zenity" else ["kdialog", "--title", "Выберите рабочую папку", "--getexistingdirectory", start_s])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HttpError(500, f"диалог не открылся: {exc}") from None
        chosen = r.stdout.strip() if r.returncode == 0 else ""
        if chosen and not path_allowed(self.cfg, Path(chosen)):
            raise HttpError(403, f"{chosen} is outside security.allowed_roots")
        return {"path": chosen}

    # ------------------------------------------------------------------- http
    def _check(self, method: str, headers: dict[str, str], q: dict[str, list[str]], path: str) -> None:
        host = headers.get("host", "")
        valid_host = (
            host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")
            or bool(re.fullmatch(rf"\d{{1,3}}(?:\.\d{{1,3}}){{3}}:{self.port}", host))
        )
        if not valid_host:
            raise HttpError(403, "bad Host header")
        origin = headers.get("origin")
        if origin and origin not in (f"http://{host}", f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}", "null"):
            raise HttpError(403, "bad Origin")
        if path.startswith("/api/"):
            tok = headers.get("x-token") or (q.get("token") or [""])[0]
            if not secrets.compare_digest(tok, self.token):
                raise HttpError(403, "bad token")

    async def route(self, method: str, path: str, q: dict[str, list[str]], headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter) -> None:
        self._check(method, headers, q, path)
        if method == "GET" and path == "/":
            if (q.get("token") or [""])[0] != self.token:
                raise HttpError(403, "Open the URL printed by `openswarm web` (it contains the access token).")
            html = Path(__file__).with_name("web_ui.html").read_text(encoding="utf-8").replace("__TOKEN__", self.token)
            return await _send(writer, 200, html.encode(), "text/html; charset=utf-8", csp=True)
        if method == "GET" and path == "/oauth/callback":
            from .dsh import complete_pkce_signin
            code = (q.get("code") or [""])[0]
            state_tok = (q.get("state") or [""])[0]
            res = await asyncio.to_thread(complete_pkce_signin, code, state_tok)
            if res.get("ok"):
                await self.pool.close()
                ok_html = (
                    "<!doctype html><html><head><meta charset='utf-8'><title>DeepSeek Harness Connected</title>"
                    "<style>body{background:#0b0d13;color:#eef1f8;font:15px system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
                    ".card{background:#12151f;border:1px solid #262b3d;border-radius:14px;padding:28px 32px;text-align:center;max-width:420px}"
                    "h2{margin:0 0 10px;color:#34d399}</style></head><body><div class='card'>"
                    "<h2>✅ DeepSeek Harness подключён!</h2><p>Аккаунт DeepSeek успешно авторизован (1M контекст активен). Это окно можно закрыть.</p>"
                    "<script>try{if(window.opener){window.opener.postMessage({type:'dsh_oauth_done',ok:true},'*');setTimeout(()=>window.close(),700);}}catch(e){}</script>"
                    "</div></body></html>"
                )
                return await _send(writer, 200, ok_html.encode("utf-8"), "text/html; charset=utf-8")
            err_msg = str(res.get("error") or "OAuth exchange failed")
            err_html = (
                f"<!doctype html><html><head><meta charset='utf-8'><title>DeepSeek Auth Error</title>"
                f"<style>body{{background:#0b0d13;color:#eef1f8;font:15px system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}"
                f".card{{background:#12151f;border:1px solid #ef4444;border-radius:14px;padding:28px 32px;text-align:center;max-width:440px}}"
                f"h2{{margin:0 0 10px;color:#f87171}}</style></head><body><div class='card'>"
                f"<h2>❌ Ошибка авторизации</h2><p>{err_msg}</p></div></body></html>"
            )
            return await _send(writer, 400, err_html.encode("utf-8"), "text/html; charset=utf-8")
        if not path.startswith("/api/"):
            raise HttpError(404, "not found")
        js = json.loads(body.decode() or "{}") if body and "json" in headers.get("content-type", "") else {}
        parts = [unquote(x) for x in path.split("/")[2:]]
        if parts == ["state"] and method == "GET":
            return await _json(writer, self.state())
        if parts == ["audit"] and method == "GET":
            try:
                lines = audit_path().read_text(encoding="utf-8").splitlines()[-int((q.get("n") or ["100"])[0]):]
            except OSError:
                lines = []
            return await _json(writer, {"lines": [json.loads(x) for x in lines if x.startswith("{")]})
        if parts and parts[0] == "keys":
            return await self.keys_route(method, parts[1:], js, writer)
        if parts and parts[0] == "dsh":
            return await self.dsh_route(method, parts[1:], headers, js, writer)
        if parts and parts[0] == "kilo":
            return await self.kilo_route(method, parts[1:], js, writer)
        if parts and parts[0] == "vpn":
            return await self.vpn_route(method, parts[1:], js, writer)
        if parts and parts[0] == "heal":
            return await self.heal_route(method, parts[1:], q, js, writer)
        if parts and parts[0] in ("router", "priority"):
            return await self.router_route(method, parts, q, js, writer)
        if parts and parts[0] == "plugins":
            return await self.plugins_route(method, parts[1:], q, js, writer)
        if parts and parts[0] == "integrations":
            return await self.integrations_route(method, parts[1:], js, writer)
        if parts and parts[0] == "telegram":
            return await self.telegram_user_route(method, parts[1:], q, js, writer)
        if parts and parts[0] == "desktop":
            return await self.desktop_route(method, parts[1:], q, js, writer)
        if parts and parts[0] == "optimizer":
            return await self.optimizer_route(method, parts[1:], js, writer)
        if parts and parts[0] == "free-models":
            return await _json(writer, self.free_models_catalog())
        if parts and parts[0] == "scheduled":
            return await self.scheduled_route(method, parts[1:], js, writer)
        if parts == ["window", "raise"] and method == "POST":
            raised = False
            cmds = [
                ["hyprctl", "dispatch", "focuswindow", "class:dev.openswarm.app"],
                ["swaymsg", '[app_id="dev.openswarm.app"] focus'],
                ["wmctrl", "-a", "OpenSwarm"],
                ["xdotool", "search", "--name", "OpenSwarm", "windowactivate"],
            ]
            for cmd in cmds:
                if shutil.which(cmd[0]):
                    try:
                        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0)
                        if r.returncode == 0:
                            raised = True
                            break
                    except Exception:
                        pass
            return await _json(writer, {"ok": True, "raised": raised})
        if parts == ["window", "pin"] and method == "POST":
            pinned = False
            enabled = bool(js.get("pinned", True))
            val = "1" if enabled else "0"
            if shutil.which("hyprctl"):
                try:
                    subprocess.run(["hyprctl", "setprop", "class:dev.openswarm.app", "pinned", val], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0)
                    subprocess.run(["hyprctl", "dispatch", "pin", "class:dev.openswarm.app"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0)
                    pinned = True
                except Exception:
                    pass
            if shutil.which("swaymsg"):
                try:
                    cmd = "sticky enable" if enabled else "sticky disable"
                    subprocess.run(["swaymsg", f'[app_id="dev.openswarm.app"] {cmd}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0)
                    pinned = True
                except Exception:
                    pass
            if shutil.which("wmctrl"):
                try:
                    flag = "add,above,sticky" if enabled else "remove,above,sticky"
                    subprocess.run(["wmctrl", "-r", "OpenSwarm", "-b", flag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.0)
                    pinned = True
                except Exception:
                    pass
            return await _json(writer, {"ok": True, "pinned": pinned})
        if parts == ["screen", "capture"] and method == "POST":
            sdir = self.workdir / ".swarm" / "screenshots"
            sdir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            spath = sdir / f"screen_{ts}.png"
            captured = False
            if shutil.which("grim"):
                try:
                    r = subprocess.run(["grim", str(spath)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0)
                    if r.returncode == 0 and spath.is_file():
                        captured = True
                except Exception:
                    pass
            if not captured and shutil.which("grimblast"):
                try:
                    r = subprocess.run(["grimblast", "save", "screen", str(spath)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0)
                    if r.returncode == 0 and spath.is_file():
                        captured = True
                except Exception:
                    pass
            if not captured and shutil.which("import"):
                try:
                    r = subprocess.run(["import", "-window", "root", str(spath)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0)
                    if r.returncode == 0 and spath.is_file():
                        captured = True
                except Exception:
                    pass
            if not captured:
                for tool in ("maim", "scrot"):
                    if shutil.which(tool):
                        try:
                            r = subprocess.run([tool, str(spath)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3.0)
                            if r.returncode == 0 and spath.is_file():
                                captured = True
                                break
                        except Exception:
                            pass
            if not captured:
                return await _json(writer, {"ok": False, "error": "No screenshot utility found (grim/import/maim/scrot)"})
            rel = str(spath.relative_to(self.workdir)) if spath.is_relative_to(self.workdir) else str(spath)
            return await _json(writer, {"ok": True, "path": rel, "filename": spath.name})
        if parts and parts[0] == "mobile":
            return await self.mobile_route(method, parts[1:], writer)
        if parts == ["dirs"] and method == "GET":
            return await _json(writer, self.list_dirs((q.get("path") or [""])[0], (q.get("hidden") or ["0"])[0] == "1"))
        if parts == ["mkdir"] and method == "POST":
            return await _json(writer, self.make_dir(str(js.get("parent", "")), str(js.get("name", ""))))
        if parts == ["pick-dir"] and method == "POST":
            return await _json(writer, await asyncio.to_thread(self.pick_dir_native, str(js.get("start", ""))))
        if parts == ["sessions"] and method == "POST":
            wd = Path(str(js.get("workdir") or self.workdir)).expanduser().resolve()
            if not wd.is_dir():
                raise HttpError(400, f"{wd} is not a directory")
            if not path_allowed(self.cfg, wd):
                raise HttpError(403, f"{wd} is outside security.allowed_roots")
            mode = js.get("mode") if self.valid_mode(js.get("mode")) else self.cfg.default_mode
            return await _json(writer, self.new_session(wd, mode, str(js.get("title", ""))).meta())
        if len(parts) >= 2 and parts[0] == "sessions":
            s = self.sessions.get(parts[1])
            if not s:
                raise HttpError(404, "no such session")
            rest = parts[2:]
            if not rest and method == "GET":
                s.load_events()
                return await _json(writer, {**s.meta(), "events": s.events[-20000:]})
            if not rest and method in ("PATCH", "PUT"):
                changed = False
                if "title" in js:
                    title = str(js.get("title", "")).strip()
                    if not title:
                        raise HttpError(400, "title must not be empty")
                    s.title = title[:80]
                    changed = True
                if "workdir" in js and js.get("workdir"):
                    new_wd = Path(str(js.get("workdir"))).expanduser().resolve()
                    if not new_wd.is_dir():
                        raise HttpError(400, f"{new_wd} is not a directory")
                    if not path_allowed(self.cfg, new_wd):
                        raise HttpError(403, f"{new_wd} is outside security.allowed_roots")
                    s.workdir = new_wd
                    changed = True
                if "mode" in js and js.get("mode"):
                    new_mode = str(js.get("mode"))
                    if self.valid_mode(new_mode):
                        s.mode = new_mode
                        changed = True
                if not changed:
                    raise HttpError(400, "title, workdir, or mode must be provided")
                s.save_meta()
                return await _json(writer, s.meta())
            if rest == ["export"] and method == "GET":
                md_text = self.export_session_md(s)
                fmt = (q.get("format") or ["md"])[0].lower()
                if fmt == "json":
                    s.load_events()
                    return await _json(writer, {**s.meta(), "events": s.events})
                return await _json(writer, {"title": s.title, "markdown": md_text})
            if not rest and method == "DELETE":
                if s.task:
                    s.task.cancel()
                self.sessions.pop(s.id, None)
                for pth in (s.meta_path, s.log_path):
                    pth.unlink(missing_ok=True)
                return await _json(writer, {"ok": True})
            if rest == ["fork"] and method == "POST":
                forked = self.fork_session(s.id)
                return await _json(writer, forked.meta())
            if rest == ["send"] and method == "POST":
                await self.start_run(s, js)
                return await _json(writer, {"ok": True})
            if rest == ["bus"] and method == "GET":
                _, broker = await self.workspace(s.workdir)
                return await _json(writer, {"messages": broker.get_messages(str((q.get("run") or [""])[0]))})
            if rest == ["bus"] and method == "POST":
                _, broker = await self.workspace(s.workdir)
                text = str(js.get("text", "")).strip()
                if not text:
                    raise HttpError(400, "empty message")
                sender = str(js.get("sender") or js.get("from") or "user").strip() or "user"
                to = str(js.get("to") or "all").strip() or "all"
                kind = str(js.get("kind") or ("answer" if js.get("reply_to") else "chat"))
                reply_to = int(js.get("reply_to") or 0)
                mid = broker.post_message(str(js.get("run") or ""), sender, to, text, kind=kind, reply_to=reply_to)
                msg = {"mid": mid, "sender": sender, "recipient": to, "kind": kind, "content": text, "reply_to": reply_to}
                if not s.busy:  # when idle, emit immediately; when busy, _pump_bus or emit delivers it
                    s.emit("bus", **msg)
                return await _json(writer, {"ok": True, "id": mid, **msg})
            if rest == ["permission"] and method == "POST":
                fut = s.perms.get(str(js.get("id")))
                if not fut or fut.done():
                    raise HttpError(404, "no such pending permission (the run may have ended)")
                fut.set_result(js.get("response") if js.get("response") in ("once", "always", "reject") else "reject")
                return await _json(writer, {"ok": True})
            if rest == ["diff"] and method == "GET":
                return await _json(writer, await asyncio.to_thread(git_diff_summary, s.workdir))
            if rest == ["snapshots"] and method == "GET":
                return await _json(writer, {"snapshots": list_snapshots(s.workdir)})
            if rest == ["undo"] and method == "POST":
                if s.busy:
                    raise HttpError(409, "cannot undo while a run is active")
                sid = str(js.get("snapshot") or (list_snapshots(s.workdir) or [""])[0])
                try:
                    res = await asyncio.to_thread(restore_snapshot, s.workdir, sid)
                except FileNotFoundError:
                    raise HttpError(404, "no such snapshot")
                audit(self.cfg, "undo", session=s.id, snapshot=sid, **res)
                s.emit("log", text=f"↩ restored snapshot {sid}: {res['restored']} file(s) restored, {res['removed']} removed")
                return await _json(writer, res)
            if rest == ["agents-md"] and method == "GET":
                return await _json(writer, {"text": self.agents_md(s)})
            if rest == ["agents-md"] and method == "PUT":
                (s.workdir / "AGENTS.md").write_text(str(js.get("text", "")), encoding="utf-8")
                audit(self.cfg, "agents_md_write", session=s.id)
                return await _json(writer, {"ok": True})
            if rest == ["result"] and method == "GET":
                s.load_events()
                res = next((e for e in reversed(s.events) if e["type"] == "result"), None)
                done = next((e for e in reversed(s.events) if e["type"] == "done"), None)
                return await _json(writer, {"busy": s.busy, "result": res["text"] if res else "", "done": done})
            if rest == ["cancel"] and method == "POST":
                if s.task:
                    s.task.cancel()
                return await _json(writer, {"ok": True})
            if rest == ["events"] and method == "GET":
                return await self._sse(writer, s, int((q.get("after") or ["0"])[0] or 0))
            if rest == ["files"] and method == "GET":
                return await _json(writer, self.list_dir(s, (q.get("path") or [""])[0]))
            if rest == ["file"] and method == "GET":
                return await _json(writer, self.read(s, (q.get("path") or [""])[0]))
            if rest == ["upload"] and method == "POST":
                name = re.sub(r"[^\w.\- ]", "_", Path((q.get("name") or ["upload.bin"])[0]).name) or "upload.bin"
                dest = self._inside(s.workdir, (q.get("dir") or [""])[0]) / name
                n = 1
                while dest.exists():
                    dest = dest.with_name(f"{Path(name).stem}-{n}{Path(name).suffix}")
                    n += 1
                dest.write_bytes(body)
                return await _json(writer, {"path": str(dest.relative_to(s.workdir.resolve()))})
            if rest == ["raw"] and method == "GET":
                rel = (q.get("path") or [""])[0]
                p = self._inside(s.workdir, rel)
                if not p.is_file():
                    raise HttpError(404, "no such file")
                mime, _ = mimetypes.guess_type(str(p))
                mime = mime or "application/octet-stream"
                try:
                    content = p.read_bytes()
                except OSError as exc:
                    raise HttpError(500, f"cannot read file: {exc}")
                return await _send(writer, 200, content, mime)
            if rest == ["upload-image"] and method == "POST":
                img_dir = s.workdir / ".openswarm_images"
                img_dir.mkdir(parents=True, exist_ok=True)
                if "json" in headers.get("content-type", "") and js.get("image"):
                    raw_b64 = str(js["image"])
                    fname = str(js.get("name") or "image.png")
                    if "," in raw_b64:
                        raw_b64 = raw_b64.split(",", 1)[1]
                    data = base64.b64decode(raw_b64)
                else:
                    fname = (q.get("name") or ["image.png"])[0]
                    data = body
                fname = re.sub(r"[^\w.\-]", "_", Path(fname).name) or "image.png"
                if not Path(fname).suffix:
                    fname += ".png"
                dest = img_dir / f"{int(time.time()*1000)}_{fname}"
                dest.write_bytes(data)
                rel = str(dest.relative_to(s.workdir.resolve()))
                return await _json(writer, {
                    "ok": True,
                    "path": rel,
                    "name": dest.name,
                    "url": f"/api/sessions/{s.id}/raw?path={quote(rel)}",
                })
            if rest == ["clarify"] and method == "POST":
                qid = str(js.get("qid") or js.get("id") or "").strip()
                answer = str(js.get("answer") or js.get("text") or "").strip()
                resolved = False
                if getattr(s, "orch", None):
                    resolved = s.orch.resolve_clarification(qid, answer)
                return await _json(writer, {"ok": resolved, "qid": qid})
        raise HttpError(404, "unknown endpoint")

    async def dsh_route(self, method: str, rest: list[str], headers: dict[str, str], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .dsh import (
            clear_dsh_credentials,
            dsh_status,
            save_dsh_credentials,
            start_pkce_signin,
        )

        if not rest and method == "GET":
            return await _json(writer, dsh_status())
        if rest in (["token"], []) and method == "POST":
            tok = js.get("token")
            kind = str(js.get("kind") or "auto")
            mode = js.get("mode")
            st = await asyncio.to_thread(
                save_dsh_credentials,
                str(tok) if tok is not None else None,
                kind=kind,
                mode=str(mode) if mode is not None else None,
                source="web-ui",
            )
            await self.pool.close()
            return await _json(writer, st)
        if rest == ["mode"] and method == "POST":
            mode = str(js.get("mode") or "auto")
            st = await asyncio.to_thread(save_dsh_credentials, None, mode=mode)
            await self.pool.close()
            return await _json(writer, st)
        if rest == ["oauth-start"] and method == "POST":
            host = headers.get("host") or f"127.0.0.1:{self.port}"
            origin = str(js.get("origin") or f"http://{host}")
            res = await asyncio.to_thread(start_pkce_signin, origin)
            return await _json(writer, res)
        if rest == ["activate"] and method == "POST":
            target_model = str(js.get("model") or "deepseek-harness/deepseek-flash")
            all_roles = bool(js.get("all_roles", False))
            self.cfg.roles.coder = target_model
            self.cfg.roles.chat = target_model
            if all_roles:
                self.cfg.roles.planner = target_model
                self.cfg.roles.architect = target_model
                self.cfg.roles.reviewer = target_model
            st = load_state()
            st["roles"] = {
                "planner": self.cfg.roles.planner,
                "architect": self.cfg.roles.architect,
                "coder": self.cfg.roles.coder,
                "reviewer": self.cfg.roles.reviewer,
            }
            save_state(st)
            return await _json(
                writer,
                {
                    **dsh_status(),
                    "activated_model": target_model,
                    "roles": {
                        "planner": self.cfg.roles.planner,
                        "coder": self.cfg.roles.coder,
                        "reviewer": self.cfg.roles.reviewer,
                    },
                },
            )
        if (not rest and method == "DELETE") or (rest == ["logout"] and method == "POST"):
            st = await asyncio.to_thread(clear_dsh_credentials)
            await self.pool.close()
            return await _json(writer, st)
        raise HttpError(404, "unknown dsh endpoint")

    async def kilo_route(self, method: str, rest: list[str], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .kilo import (
            KILO_DEFAULT_MODEL,
            clear_kilo_credentials,
            fetch_kilo_free_models,
            kilo_status,
            save_kilo_credentials,
            test_kilo_completion,
        )

        if not rest and method == "GET":
            return await _json(writer, kilo_status())
        if rest == ["models"] and method == "GET":
            models = await asyncio.to_thread(fetch_kilo_free_models)
            return await _json(writer, {"models": models})
        if rest in (["token"], []) and method == "POST":
            tok = js.get("token")
            mode = js.get("mode")
            st = await asyncio.to_thread(
                save_kilo_credentials,
                str(tok) if tok is not None else None,
                mode=str(mode) if mode is not None else None,
            )
            await self.pool.close()
            return await _json(writer, st)
        if rest == ["test"] and method == "POST":
            mdl = str(js.get("model") or KILO_DEFAULT_MODEL)
            res = await asyncio.to_thread(test_kilo_completion, mdl)
            return await _json(writer, res)
        if rest == ["activate"] and method == "POST":
            target_model = str(js.get("model") or KILO_DEFAULT_MODEL)
            all_roles = bool(js.get("all_roles", False))
            self.cfg.roles.coder = target_model
            self.cfg.roles.chat = target_model
            if all_roles:
                self.cfg.roles.planner = target_model
                self.cfg.roles.architect = target_model
                self.cfg.roles.reviewer = target_model
            st = load_state()
            st["roles"] = {
                "planner": self.cfg.roles.planner,
                "architect": self.cfg.roles.architect,
                "coder": self.cfg.roles.coder,
                "reviewer": self.cfg.roles.reviewer,
            }
            save_state(st)
            return await _json(
                writer,
                {
                    **kilo_status(),
                    "activated_model": target_model,
                    "roles": {
                        "planner": self.cfg.roles.planner,
                        "coder": self.cfg.roles.coder,
                        "reviewer": self.cfg.roles.reviewer,
                    },
                },
            )
        if (not rest and method == "DELETE") or (rest == ["logout"] and method == "POST"):
            st = await asyncio.to_thread(clear_kilo_credentials)
            await self.pool.close()
            return await _json(writer, st)
        raise HttpError(404, "unknown kilo endpoint")

    async def vpn_route(self, method: str, rest: list[str], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .vpn import get_vpn
        m = get_vpn(self.cfg)
        if not rest and method == "GET":
            return await _json(writer, m.status(with_servers=True))
        if rest == ["toggle"] and method == "POST":
            turn_on = bool(js.get("enabled", not m.enabled))
            ok = await asyncio.to_thread(m.set_enabled, turn_on)
            await self.pool.close()
            return await _json(writer, {**m.status(), "ok": ok})
        if rest == ["autoswitch"] and method == "POST":
            turn_on = bool(js.get("auto_switch", not m.auto_switch_on_limits))
            m.set_auto_switch(turn_on)
            return await _json(writer, {**m.status(), "ok": True})
        if rest == ["rotate"] and method == "POST":
            m.enabled = True
            ok = await asyncio.to_thread(m.rotate, "manual (web)", False)
            if ok and self.pool:
                await self.pool.close()
                self.pool.api_bad = False
            return await _json(writer, {**m.status(), "ok": ok, "check": getattr(m, "last_check", {})})
        if rest == ["use"] and method == "POST":
            sid = str(js.get("id", ""))
            ok = await asyncio.to_thread(m.use, sid)
            if ok and self.pool:
                await self.pool.close()
                self.pool.api_bad = False
            return await _json(writer, {**m.status(), "ok": ok, "check": getattr(m, "last_check", {})})
        if rest == ["verify"] and method == "POST":
            target = str(js.get("target", ""))
            chk = await asyncio.to_thread(m.verify, target)
            return await _json(writer, {"ok": chk.get("ok", False), "check": chk, "status": m.status()})
        if rest == ["reset"] and method == "POST":
            m.cooldown.clear()
            m._save()
            return await _json(writer, m.status(with_servers=True))
        raise HttpError(404, "unknown endpoint")

    def _target_workdir(self, q: dict[str, list[str]], js: dict[str, Any] | None = None) -> Path:
        sid = str((js or {}).get("session") or (q.get("session") or [""])[0]).strip()
        if sid and sid in self.sessions:
            return self.sessions[sid].workdir
        return self.workdir

    async def heal_route(self, method: str, rest: list[str], q: dict[str, list[str]], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .healer import analyze_error, diagnose_and_heal

        wd = self._target_workdir(q, js)
        if not rest and method == "GET":
            return await _json(writer, await diagnose_and_heal(self.cfg, wd, self.pool, auto_fix=False))
        if not rest and method == "POST":
            auto_fix = bool(js.get("auto_fix", True))
            rep = await diagnose_and_heal(self.cfg, wd, self.pool, auto_fix=auto_fix)
            return await _json(writer, rep)
        if rest == ["analyze"] and method == "POST":
            return await _json(writer, analyze_error(str(js.get("text", ""))))
        raise HttpError(404, "unknown endpoint")

    async def router_route(self, method: str, parts: list[str], q: dict[str, list[str]], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .router import VALID_PRIORITIES, get_router, priority_list

        router = get_router()
        if parts == ["router"] and method == "GET":
            role = str((q.get("role") or ["coder"])[0])
            prio = str((q.get("priority") or [self.cfg.priority])[0])
            return await _json(writer, {
                "priority": self.cfg.priority,
                "priorities": priority_list(),
                "recommended_roles": router.recommend_roles(self.cfg, prio),
                "rankings": router.rank_models(self.cfg, role=role, priority=prio)[:25],
            })
        if parts == ["router", "reset"] and method == "POST":
            router.clear_cooldowns()
            return await _json(writer, {"ok": True, "recommended_roles": router.recommend_roles(self.cfg, self.cfg.priority)})
        if parts in (["priority"], ["router", "priority"]) and method == "POST":
            prio = str(js.get("priority") or "balanced").lower()
            if prio not in VALID_PRIORITIES:
                raise HttpError(400, f"unknown priority: {prio}")
            self.cfg.priority = prio
            rec = router.recommend_roles(self.cfg, prio)
            if js.get("auto_pick", True):
                for r, m in rec.items():
                    if hasattr(self.cfg.roles, r) and m:
                        setattr(self.cfg.roles, r, m)
            st = load_state()
            st["priority"] = prio
            st["roles"] = {"planner": self.cfg.roles.planner, "architect": self.cfg.roles.architect,
                           "coder": self.cfg.roles.coder, "reviewer": self.cfg.roles.reviewer}
            save_state(st)
            return await _json(writer, {
                "ok": True,
                "priority": prio,
                "roles": {"planner": self.cfg.roles.planner, "coder": self.cfg.roles.coder, "reviewer": self.cfg.roles.reviewer},
                "recommended_roles": rec,
            })
        raise HttpError(404, "unknown endpoint")

    async def plugins_route(self, method: str, rest: list[str], q: dict[str, list[str]], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .plugins import (
            create_custom_skill,
            import_external_skill,
            plugins_overview,
            set_hook,
            set_mcp_server,
            toggle_catalog_mcp,
        )
        from .skillpack import install_skills

        wd = self._target_workdir(q, js)
        if not rest and method == "GET":
            return await _json(writer, plugins_overview(wd))
        if rest == ["mcp"] and method == "POST":
            sid = str(js.get("id", "")).strip()
            if not sid:
                raise HttpError(400, "missing id")
            cmd = js.get("command")
            try:
                if isinstance(cmd, list) and cmd:
                    set_mcp_server(wd, sid, [str(x) for x in cmd], js.get("env") or {}, bool(js.get("enabled", True)))
                else:
                    toggle_catalog_mcp(wd, sid, js.get("enabled"))
            except ValueError as exc:
                raise HttpError(400, str(exc))
            await self.pool.close()
            return await _json(writer, plugins_overview(wd))
        if rest == ["hook"] and method == "POST":
            hid = str(js.get("id", "")).strip()
            if not hid:
                raise HttpError(400, "missing hook id")
            set_hook(hid, bool(js.get("enabled", True)))
            return await _json(writer, plugins_overview(wd))
        if rest == ["import-skill"] and method == "POST":
            try:
                name = await asyncio.to_thread(import_external_skill, str(js.get("path", "")), wd if js.get("project") else None)
            except ValueError as exc:
                raise HttpError(400, str(exc))
            return await _json(writer, {**plugins_overview(wd), "imported": name})
        if rest == ["create-skill"] and method == "POST":
            try:
                name = await asyncio.to_thread(
                    create_custom_skill,
                    str(js.get("name", "")),
                    str(js.get("description", "")),
                    str(js.get("instructions", "")),
                    wd if js.get("project") else None,
                )
            except ValueError as exc:
                raise HttpError(400, str(exc))
            return await _json(writer, {**plugins_overview(wd), "created": name})
        if rest == ["sync-skills"] and method == "POST":
            written = await asyncio.to_thread(install_skills, None, True)
            return await _json(writer, {**plugins_overview(wd), "synced": len(written)})
        raise HttpError(404, "unknown endpoint")

    # ------------------------------------------------------------ OpenRouter keys
    def keys_view(self) -> dict[str, Any]:
        pool = get_pool(self.cfg)
        rows = []
        for k, src in key_sources(self.cfg):
            rows.append({"id": key_id(k), "masked": mask(k), "source": src, **(pool.info(k) if pool else {"state": "ready", "wait_seconds": 0, "uses": 0, "reason": ""})})
        return {"keys": rows, "strategy": self.cfg.openrouter.strategy, "prefix": self.cfg.openrouter.model_prefix, "env_var": self.cfg.openrouter.env_var}

    async def refresh_openrouter_models(self) -> dict[str, Any]:
        """Models from `opencode models` (with a key in its env) plus OpenRouter's public catalogue."""
        cfg, error = self.cfg, ""
        if cfg.opencode.discover_models:
            reconcile_models(cfg, await discover_models(cfg))
        ids, error = await asyncio.to_thread(fetch_openrouter_catalogue)
        pre = cfg.openrouter.model_prefix
        have = set(cfg.model_ids())
        new = [ModelSpec(id=f"{pre}{i}", label=f"{n} (OpenRouter)", free=i.endswith(":free")) for i, n in ids if f"{pre}{i}" not in have]
        cfg.models += sorted(new, key=lambda m: (not m.free, m.id))
        total = sum(1 for m in cfg.models if m.id.startswith(pre))
        return {"total": total, "added": len(new), "error": error if not total else ""}

    async def keys_route(self, method: str, rest: list[str], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        cfg = self.cfg
        if not rest and method == "GET":
            return await _json(writer, self.keys_view())
        if not rest and method == "POST":  # add keys (any separators: lines, commas, spaces)
            new = parse_keys(str(js.get("text", "")))
            if not new:
                raise HttpError(400, "no keys found in the text")
            bad = [mask(k) for k in new if len(k) < 16]
            if bad:
                raise HttpError(400, f"does not look like an API key: {', '.join(bad)}")
            had = bool(configured_keys(cfg))
            known = set(configured_keys(cfg))
            stored = load_stored_keys()
            added = [k for k in new if k not in known]
            save_stored_keys(stored + added)
            audit(cfg, "openrouter_keys_added", count=len(added))
            if added and not had:  # first key: openrouter/* models become visible
                await self.refresh_openrouter_models()
            return await _json(writer, {**self.keys_view(), "added": len(added), "duplicates": len(new) - len(added)})
        if rest == ["models"] and method == "POST":  # (re)load the openrouter/* model list
            return await _json(writer, await self.refresh_openrouter_models())
        if len(rest) >= 1:
            key = next((k for k, _ in key_sources(cfg) if key_id(k) == rest[0]), None)
            if key is None:
                raise HttpError(404, "no such key")
            if len(rest) == 1 and method == "DELETE":
                stored = load_stored_keys()
                if key not in stored:
                    raise HttpError(409, "this key comes from swarm_config.yaml or the environment - remove it there")
                save_stored_keys([k for k in stored if k != key])
                audit(cfg, "openrouter_key_removed")
                return await _json(writer, self.keys_view())
            if rest[1:] == ["reset"] and method == "POST":
                pool = get_pool(cfg)
                if pool:
                    pool.revive(key)
                return await _json(writer, self.keys_view())
            if rest[1:] == ["check"] and method == "POST":
                return await _json(writer, await asyncio.to_thread(check_openrouter_key, key))
        raise HttpError(404, "unknown endpoint")

    async def integrations_route(self, method: str, rest: list[str], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .integrations import send_telegram, send_discord, send_email, generate_ics, google_calendar_link, IntegrationSettings
        if not rest and method == "GET":
            return await _json(writer, self.integrations.to_dict())
        if not rest and method == "POST":
            self.integrations = IntegrationSettings.from_dict(js)
            self._save_integrations()
            return await _json(writer, {"ok": True, "integrations": self.integrations.to_dict()})
        if rest == ["test"] and method == "POST":
            target = str(js.get("target", ""))
            ok = False
            if target == "telegram":
                tok = str(js.get("token") or self.integrations.telegram_bot_token)
                chat = str(js.get("chat_id") or self.integrations.telegram_chat_id)
                ok = await send_telegram(tok, chat, "🔔 <b>OpenSwarm Test Alert</b>\nTelegram integration is working perfectly!")
            elif target == "discord":
                wh = str(js.get("webhook_url") or self.integrations.discord_webhook_url)
                ok = await send_discord(wh, "OpenSwarm Test Alert", "Discord webhook integration is working perfectly!", color=0x5865F2)
            elif target == "email":
                test_settings = IntegrationSettings.from_dict(js) if js.get("smtp_host") else self.integrations
                ok = await send_email(test_settings, "OpenSwarm Test Notification", "This is a test notification from OpenSwarm 1.0.")
            return await _json(writer, {"ok": ok, "target": target})
        if rest == ["calendar"] and method in ("GET", "POST"):
            from datetime import datetime, timezone
            title = str(js.get("title") or "OpenSwarm Task")
            desc = str(js.get("description") or "Autonomous agent session task")
            start = datetime.now(timezone.utc)
            ics = generate_ics(title, desc, start)
            glink = google_calendar_link(title, desc, start)
            return await _json(writer, {"ics": ics, "google_link": glink})
        raise HttpError(404, "unknown endpoint")

    async def scheduled_route(self, method: str, rest: list[str], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        if not rest and method == "GET":
            return await _json(writer, {"tasks": self.scheduler.list_tasks()})
        if not rest and method == "POST":
            prompt = str(js.get("prompt", "")).strip()
            if not prompt:
                raise HttpError(400, "prompt cannot be empty")
            delay = float(js.get("delay_seconds", 0))
            run_at = float(js.get("run_at")) if js.get("run_at") else None
            task = self.scheduler.schedule(
                prompt=prompt,
                delay_seconds=delay,
                run_at=run_at,
                mode=str(js.get("mode") or "chat"),
                priority=str(js.get("priority") or self.cfg.priority),
                effort=str(js.get("effort") or getattr(self.cfg, "effort", "medium")),
                session_id=str(js.get("session_id") or ""),
                target_agents=int(js.get("target_agents") or js.get("agents") or 1),
            )
            return await _json(writer, {"ok": True, "task": task.to_dict()})
        if len(rest) == 1 and method == "DELETE":
            ok = self.scheduler.cancel(rest[0])
            return await _json(writer, {"ok": ok})
        if len(rest) == 2 and rest[1] == "run" and method == "POST":
            task = self.scheduler.tasks.get(rest[0])
            if task and task.status == "pending":
                task.status = "running"
                asyncio.create_task(self.scheduler._execute_task(task))
                return await _json(writer, {"ok": True})
            raise HttpError(404, "task not found or not pending")
        raise HttpError(404, "unknown endpoint")

    async def mobile_route(self, method: str, rest: list[str], writer: asyncio.StreamWriter) -> None:
        import socket
        root_dir = Path(__file__).resolve().parent.parent.parent
        apk_candidates = [
            root_dir / "dist" / "OpenSwarm-Mobile.apk",
            Path(__file__).resolve().parent / "data" / "OpenSwarm-Mobile.apk",
        ]
        apk_path = next((p for p in apk_candidates if p.is_file()), apk_candidates[0])
        if (not rest or rest == ["info"]) and method == "GET":
            lan_ip = "127.0.0.1"
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("8.8.8.8", 80))
                    lan_ip = s.getsockname()[0]
            except OSError:
                pass
            return await _json(writer, {
                "lan_ip": lan_ip,
                "port": self.port,
                "lan_url": f"http://{lan_ip}:{self.port}/?token={self.token}",
                "local_url": f"http://127.0.0.1:{self.port}/?token={self.token}",
                "apk_download_url": f"/api/mobile/apk?token={self.token}",
                "apk_exists": apk_path.is_file(),
                "apk_size": apk_path.stat().st_size if apk_path.is_file() else 0,
                "apk_path": str(apk_path),
            })
        if rest == ["apk"] and method == "GET":
            if not apk_path.is_file():
                builder = root_dir / "scripts" / "build_apk.py"
                if builder.is_file():
                    await asyncio.to_thread(subprocess.run, ["python3", str(builder)], check=True)
            if not apk_path.is_file():
                raise HttpError(404, "APK file not found")
            body = apk_path.read_bytes()
            hdr = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/vnd.android.package-archive\r\n"
                'Content-Disposition: attachment; filename="OpenSwarm-Mobile.apk"\r\n'
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(hdr.encode("latin-1") + body)
            await writer.drain()
            return
        raise HttpError(404, "unknown endpoint")

    async def optimizer_route(self, method: str, rest: list[str], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .optimizer import (
            build_repo_skeleton,
            get_optimizer_stats,
            optimize_prompt_for_model,
            reset_optimizer_stats,
            update_optimizer_settings,
        )
        if (not rest or rest == ["stats"]) and method == "GET":
            return await _json(writer, get_optimizer_stats())
        if rest == ["settings"] and method == "POST":
            return await _json(writer, update_optimizer_settings(js))
        if rest == ["reset"] and method == "POST":
            reset_optimizer_stats()
            return await _json(writer, get_optimizer_stats())
        if rest == ["repomap"] and method in ("GET", "POST"):
            wd_str = str(js.get("workdir") or self.workdir)
            wd = Path(wd_str).expanduser().resolve()
            skeleton = build_repo_skeleton(wd if wd.is_dir() else self.workdir)
            return await _json(writer, {"ok": True, "skeleton": skeleton, "chars": len(skeleton), "stats": get_optimizer_stats()})
        if rest == ["compress"] and method == "POST":
            raw = str(js.get("text", ""))
            comp = optimize_prompt_for_model(raw, role=str(js.get("role", "chat")))
            return await _json(writer, {
                "ok": True,
                "original_chars": len(raw),
                "compressed_chars": len(comp),
                "saved_chars": max(0, len(raw) - len(comp)),
                "compressed": comp,
                "stats": get_optimizer_stats(),
            })
        raise HttpError(404, "unknown endpoint in optimizer api")

    async def desktop_route(self, method: str, rest: list[str], q: dict[str, list[str]], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .desktop_agent import get_desktop_agent
        agent = get_desktop_agent(self.workdir)
        if (not rest or rest == ["status"]) and method == "GET":
            return await _json(writer, agent.status())
        if rest == ["windows"] and method == "GET":
            return await _json(writer, {"ok": True, "windows": agent.list_windows(), "active": agent.get_active_window()})
        if rest == ["settings"] and method == "POST":
            if "require_confirmation" in js:
                agent.require_confirmation = bool(js["require_confirmation"])
            return await _json(writer, agent.status())
        if rest == ["action"] and method == "POST":
            act = str(js.get("action") or js.get("kind") or "").strip()
            if not act:
                raise HttpError(400, "Укажите тип действия (action)")
            target = str(js.get("target") or js.get("target_window") or "").strip()
            value = str(js.get("value") or js.get("shortcut") or js.get("text") or "")
            confirmed = bool(js.get("confirmed", False))
            desc = str(js.get("description", "")).strip()
            res = agent.execute_action(action=act, target=target, value=value, confirmed=confirmed, description=desc)
            return await _json(writer, res)
        if rest == ["approve"] and method == "POST":
            aid = str(js.get("id") or js.get("action_id") or "").strip()
            if not aid:
                raise HttpError(400, "Укажите id действия")
            try:
                return await _json(writer, agent.approve_action(aid))
            except Exception as exc:
                raise HttpError(400, str(exc))
        if rest == ["reject"] and method == "POST":
            aid = str(js.get("id") or js.get("action_id") or "").strip()
            rej = agent.reject_action(aid)
            return await _json(writer, {"ok": rej.get("ok", False), "pending": agent.list_pending()})
        if rest == ["assist"] and method == "POST":
            goal = str(js.get("goal", "")).strip()
            target_win = str(js.get("target_window") or js.get("target") or "").strip()
            if not goal:
                raise HttpError(400, "Опишите задачу для выполнения в приложении")

            async def _run_llm(p: str) -> str:
                try:
                    from .runner import OpenCodeRunner
                    runner = OpenCodeRunner(self.cfg, self.workdir, "desktop_assist", "chat", self.cfg.roles.coder)
                    res = await runner.run(p)
                    return res.clean_output
                except Exception:
                    return ""

            planned = await agent.plan_actions_for_goal(goal, runner_fn=_run_llm, target_window=target_win)
            return await _json(writer, {"ok": True, "planned": planned, "queued_actions": planned, "pending": agent.list_pending()})
        raise HttpError(404, "unknown endpoint in desktop api")

    def free_models_catalog(self) -> dict[str, Any]:
        return {
            "categories": [
                {
                    "title": "OpenCode Встроенные (100% бесплатно, без ключей)",
                    "desc": "Работают сразу из коробки через нативный движок OpenCode",
                    "models": [
                        {"id": "opencode/deepseek-v4.1-flash-free", "name": "DeepSeek V4.1 Flash", "role": "Кодер / Чат", "badge": "Рекомендуется для кода", "speed": "Быстро", "context": "128k"},
                        {"id": "opencode/beam-free", "name": "Beam Free (1M)", "role": "Архитектор / План", "badge": "Глубокие рассуждения", "speed": "Средне", "context": "1M"},
                        {"id": "opencode/nemotron-3.5-lightning-free", "name": "Nemotron 3.5 Lightning", "role": "Быстрый ассистент", "badge": "Ультра-скорость", "speed": "Мгновенно", "context": "1M"},
                        {"id": "opencode/nemotron-3.5-super-vl-free", "name": "Nemotron 3.5 Super VL", "role": "Ревьюер / Зрение", "badge": "Мультимодальный", "speed": "Быстро", "context": "1M"},
                        {"id": "opencode/glm-5.3-flash-free", "name": "GLM 5.3 Flash", "role": "Общий ассистент", "badge": "Быстрый ответ", "speed": "Быстро", "context": "128k"},
                    ]
                },
                {
                    "title": "Kilo AI Gateway (Бесплатно, без API ключей)",
                    "desc": "Анонимный доступ к пулу моделей через Kilo Gateway (~200 запросов/час на IP)",
                    "models": [
                        {"id": "kilo/poolside/laguna-s-2.1:free", "name": "Laguna S 2.1 Free", "role": "Кодер / Аналитик", "badge": "Kilo Free", "speed": "Быстро", "context": "64k"},
                        {"id": "kilo/cohere/north-mini-code:free", "name": "North Mini Code Free", "role": "Кодинг / Скрипты", "badge": "Kilo Free", "speed": "Очень быстро", "context": "32k"},
                        {"id": "kilo/kilo-auto/free", "name": "Kilo Auto Router", "role": "Авто-маршрутизация", "badge": "Умный выбор", "speed": "Быстро", "context": "64k"},
                    ]
                },
                {
                    "title": "Внешние бесплатные провайдеры (Нужен бесплатный ключ)",
                    "desc": "Провайдеры с щедрыми бесплатными тарифами без привязки карты",
                    "models": [
                        {"id": "deepseek-harness/deepseek-flash", "name": "DeepSeek Harness Flash", "provider": "DeepSeek Harness", "badge": "1M контекст", "info": "Встроенный безлимитный режим"},
                        {"id": "google/gemini-2.0-flash-exp:free", "name": "Google Gemini 2.0 Flash", "provider": "Google AI Studio", "badge": "15 RPM бесплатно", "info": "Получить ключ бесплатно на aistudio.google.com"},
                        {"id": "groq/llama-3.3-70b-versatile", "name": "Groq Llama 3.3 70B", "provider": "Groq Cloud", "badge": "300+ токенов/сек", "info": "Бесплатный ключ на console.groq.com"},
                    ]
                },
                {
                    "title": "Локальные нейросети (100% оффлайн, без интернета)",
                    "desc": "Запуск прямо на вашем компьютере через Ollama или LM Studio",
                    "models": [
                        {"id": "ollama/qwen2.5-coder:7b", "name": "Qwen 2.5 Coder 7B", "role": "Оффлайн-кодер", "badge": "Ollama", "info": "Команда: ollama run qwen2.5-coder:7b"},
                        {"id": "ollama/deepseek-r1:8b", "name": "DeepSeek R1 8B", "role": "Оффлайн-рассуждения", "badge": "Ollama", "info": "Команда: ollama run deepseek-r1:8b"},
                    ]
                }
            ]
        }

    async def telegram_user_route(self, method: str, rest: list[str], q: dict[str, list[str]], js: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        from .telegram_user import get_telegram_user_manager
        mgr = get_telegram_user_manager()

        if rest == ["user", "status"] and method == "GET":
            return await _json(writer, await mgr.get_status())

        if (rest == ["user", "auth", "send_code"] or rest == ["user", "code"]) and method == "POST":
            phone = str(js.get("phone", "")).strip()
            api_id = int(js.get("api_id", 0)) if js.get("api_id") else None
            api_hash = str(js.get("api_hash", "")).strip() or None
            if not phone:
                raise HttpError(400, "Укажите номер телефона")
            try:
                res = await mgr.send_code(phone, api_id, api_hash)
                return await _json(writer, res)
            except Exception as exc:
                raise HttpError(400, f"Ошибка отправки кода: {exc}")

        if (rest == ["user", "auth", "sign_in"] or rest == ["user", "signin"]) and method == "POST":
            code = str(js.get("code", "")).strip()
            password = str(js.get("password", "")).strip() or None
            phone_code_hash = str(js.get("phone_code_hash", "")).strip() or None
            if not code:
                raise HttpError(400, "Укажите код подтверждения")
            try:
                res = await mgr.sign_in(code, password, phone_code_hash)
                return await _json(writer, res)
            except Exception as exc:
                raise HttpError(400, f"Ошибка авторизации: {exc}")

        if rest == ["user", "dialogs"] and method == "GET":
            limit = int((q.get("limit") or ["20"])[0])
            try:
                res = await mgr.get_dialogs(limit=min(50, max(1, limit)))
                return await _json(writer, {"ok": True, "dialogs": res})
            except Exception as exc:
                raise HttpError(400, f"Ошибка загрузки чатов: {exc}")

        if rest == ["user", "messages"] and method == "GET":
            chat_id = (q.get("chat_id") or [""])[0]
            if not chat_id:
                raise HttpError(400, "Укажите chat_id")
            limit = int((q.get("limit") or ["15"])[0])
            try:
                res = await mgr.get_messages(chat_id, limit=min(50, max(1, limit)))
                return await _json(writer, {"ok": True, "messages": res})
            except Exception as exc:
                raise HttpError(400, f"Ошибка загрузки сообщений: {exc}")

        if rest == ["user", "suggest"] and method == "POST":
            chat_id = js.get("chat_id")
            if not chat_id:
                raise HttpError(400, "Укажите chat_id")
            hint = str(js.get("hint", "")).strip()

            async def _run_llm(p: str) -> str:
                try:
                    from .runner import OpenCodeRunner
                    runner = OpenCodeRunner(self.cfg, self.workdir, "tg_suggest", "chat", self.cfg.roles.coder)
                    res = await runner.run(p)
                    return res.clean_output
                except Exception:
                    return ""

            try:
                replies = await mgr.suggest_reply(chat_id, hint, runner_fn=_run_llm)
                return await _json(writer, {"ok": True, "suggestions": replies})
            except Exception as exc:
                raise HttpError(400, f"Ошибка генерации ответов: {exc}")

        if rest == ["user", "send"] and method == "POST":
            chat_id = js.get("chat_id")
            text = str(js.get("text", "")).strip()
            confirmed = bool(js.get("confirmed", False))
            chat_title = str(js.get("chat_title", "")).strip()
            if not chat_id or not text:
                raise HttpError(400, "chat_id и text обязательны")
            try:
                res = await mgr.send_message(chat_id, text, confirmed=confirmed, chat_title=chat_title)
                return await _json(writer, res)
            except Exception as exc:
                raise HttpError(400, f"Ошибка отправки сообщения: {exc}")

        if rest == ["user", "drafts"] and method == "GET":
            return await _json(writer, {"ok": True, "drafts": mgr.list_drafts(), "require_confirmation": mgr.require_confirmation})

        if rest == ["user", "drafts", "approve"] and method == "POST":
            did = str(js.get("id") or js.get("draft_id") or "").strip()
            if not did:
                raise HttpError(400, "Укажите id черновика")
            try:
                res = await mgr.approve_draft(did)
                return await _json(writer, res)
            except Exception as exc:
                raise HttpError(400, f"Ошибка подтверждения черновика: {exc}")

        if rest == ["user", "drafts", "reject"] and method == "POST":
            did = str(js.get("id") or js.get("draft_id") or "").strip()
            ok = mgr.reject_draft(did)
            return await _json(writer, {"ok": ok, "drafts": mgr.list_drafts()})

        if rest == ["user", "logout"] and method == "POST":
            ok = await mgr.logout()
            return await _json(writer, {"ok": ok})

        raise HttpError(404, "unknown endpoint in telegram user api")

    async def _sse(self, writer: asyncio.StreamWriter, s: Session, after: int) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: keep-alive\r\nX-Accel-Buffering: no\r\n\r\n")
        s.load_events()
        q: asyncio.Queue = asyncio.Queue()
        s.listeners.add(q)
        last = after
        try:
            for ev in [e for e in s.events if e["id"] > after]:
                writer.write(f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n".encode())
                last = ev["id"]
            await writer.drain()
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    if ev["id"] <= last:
                        continue
                    last = ev["id"]
                    writer.write(f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n".encode())
                except asyncio.TimeoutError:
                    writer.write(b": ping\n\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            s.listeners.discard(q)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Lazily start the scheduler tick loop on first request (safe: we're inside an event loop now)
        await self.scheduler.ensure_started()
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            n = int(headers.get("content-length", "0") or 0)
            if n > MAX_BODY:
                raise HttpError(413, "body too large")
            body = await reader.readexactly(n) if n else b""
            u = urlparse(target)
            await self.route(method, u.path, parse_qs(u.query), headers, body, writer)
        except asyncio.CancelledError:
            pass
        except HttpError as exc:
            await _json(writer, {"error": exc.msg}, exc.status)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError, ConnectionError, ValueError):
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                await _json(writer, {"error": f"internal error: {exc!r}"}, 500)
            except ConnectionError:
                pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


def fetch_openrouter_catalogue() -> tuple[list[tuple[str, str]], str]:
    """[(model id, name)] from OpenRouter's public model list (no key needed) and an error text ('' when fine)."""
    import urllib.request

    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/models", headers={"User-Agent": "openswarm"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = (json.loads(r.read().decode()) or {}).get("data") or []
        return [(str(m["id"]), str(m.get("name") or m["id"])) for m in data if isinstance(m, dict) and m.get("id")], ""
    except (OSError, ValueError) as exc:
        return [], f"cannot load the model list from openrouter.ai: {str(exc)[:100]}"


def check_openrouter_key(key: str) -> dict[str, Any]:
    """Ask OpenRouter about one key (label, usage, remaining credit). Never raises; the key is only sent to openrouter.ai."""
    import urllib.error
    import urllib.request

    last = ""
    for url in ("https://openrouter.ai/api/v1/key", "https://openrouter.ai/api/v1/auth/key"):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "openswarm"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = (json.loads(r.read().decode() or "{}") or {}).get("data") or {}
            return {"ok": True, "label": d.get("label") or "", "usage": d.get("usage"), "limit": d.get("limit"),
                    "remaining": d.get("limit_remaining"), "free_tier": d.get("is_free_tier")}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            return {"ok": False, "error": "invalid or revoked key" if exc.code in (401, 403) else f"HTTP {exc.code}"}
        except (OSError, ValueError) as exc:
            last = str(exc)[:120]
    return {"ok": False, "error": last or "no answer from openrouter.ai"}


async def _send(writer: asyncio.StreamWriter, status: int, body: bytes, ctype: str, csp: bool = False) -> None:
    extra = ("Content-Security-Policy: default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'\r\n") if csp else ""
    writer.write(
        f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
        f"Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n{extra}"
        f"Connection: close\r\n\r\n".encode() + body
    )
    await writer.drain()


async def _json(writer: asyncio.StreamWriter, data: Any, status: int = 200) -> None:
    await _send(writer, status, json.dumps(data).encode(), "application/json")


def _open_app_window(url: str, size: tuple[int, int] | None = None) -> subprocess.Popen | None:
    """Open a chromeless window with its own profile, so the process lives exactly as long as the window."""
    profile = state_path().parent / "app-profile"
    for exe in ("chromium", "google-chrome-stable", "google-chrome", "brave", "chrome", "microsoft-edge-stable"):
        path = shutil.which(exe)
        if path:
            cmd = [path, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--new-window"]
            if size:
                cmd.append(f"--window-size={size[0]},{size[1]}")
            return subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
    return None


async def _watch_window(proc: subprocess.Popen, server: asyncio.AbstractServer) -> None:
    """Stop the server when the app window is closed (ignore a launcher that exits immediately)."""
    started = time.time()
    while proc.poll() is None:
        await asyncio.sleep(1)
    if time.time() - started > 3:
        print("app window closed - stopping OpenSwarm", flush=True)
        server.close()
        await server.wait_closed()
        raise asyncio.CancelledError


async def _serve(app: WebApp, host: str, url_open: bool, as_window: bool, ready: asyncio.Future | None = None) -> None:
    server = await asyncio.start_server(app.handle, host, app.port, limit=2**20)
    app.port = server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{app.port}/?token={app.token}"
    print(f"OpenSwarm Web {__version__}\n  {url}\n  (private to this machine; Ctrl+C to stop)", flush=True)
    if ready and not ready.done():
        ready.set_result(url)
    await app.scheduler.ensure_started()
    auto_task = asyncio.create_task(app.automation.run()) if (app.cfg.schedules or app.cfg.triggers) else None
    watcher: asyncio.Task | None = None
    if url_open:
        proc = _open_app_window(url) if as_window else None
        if proc:
            serving = asyncio.current_task()

            async def _guard() -> None:
                try:
                    await _watch_window(proc, server)
                except asyncio.CancelledError:
                    if serving:
                        serving.cancel()

            watcher = asyncio.create_task(_guard())
        else:
            webbrowser.open(url)
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        if watcher:
            watcher.cancel()
        if auto_task:
            auto_task.cancel()
        await app.shutdown()


def web_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="openswarm web", description="OpenSwarm as a local web app")
    p.add_argument("--port", type=int, default=8787, help="0 = pick a free port")
    p.add_argument("--dir", "--repo", dest="dir", type=Path, default=Path.cwd(), help="default working directory")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--app", action="store_true", help="open as a chromeless app window (Chromium/Chrome/Brave)")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.add_argument("--no-memory", action="store_true")
    p.add_argument("--token", default=os.environ.get("OPENSWARM_TOKEN", ""), help="fixed access token for scripts (default: random; env OPENSWARM_TOKEN)")
    a = p.parse_args(argv)
    workdir = a.dir.resolve()
    try:
        cfg = load_config((a.config or find_config_path(workdir)).resolve())
    except ConfigError as exc:
        print(exc)
        return 2
    if not a.no_memory:
        apply_state(cfg, load_state())
    if cfg.opencode.discover_models:
        reconcile_models(cfg, asyncio.run(discover_models(cfg)))
    app = WebApp(cfg, workdir, a.port, a.token or secrets.token_urlsafe(18))
    try:
        asyncio.run(_serve(app, "127.0.0.1", not a.no_open, a.app))
    except OSError as exc:
        if a.port:
            print(f"port {a.port} unavailable ({exc}); picking a free one")
            app.port = 0
            asyncio.run(_serve(app, "127.0.0.1", not a.no_open, a.app))
        else:
            raise
    except KeyboardInterrupt:
        pass
    return 0
