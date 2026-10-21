"""Textual TUI for OpenSwarm: parallel chat tabs, Storm/Swarm/Cowork runs and a live message transcript."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    SelectionList,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.selection_list import Selection as ListItem

from .broker import Broker
from .config import SwarmConfig, discover_models, reconcile_models
from .git_manager import GitError, GitManager
from .servers import ServerPool
from .state import save_state, snapshot
from .orchestrator import DONE, FAILED, IDLE, MERGING, RUNNING, Hooks, Orchestrator, Selection

STATUS_STYLE = {
    IDLE: ("○", "grey62"),
    RUNNING: ("●", "yellow"),
    MERGING: ("◐", "cyan"),
    DONE: ("✔", "green"),
    FAILED: ("✘", "red"),
}
MODES = [
    ("Chat - parallel sessions, any directory", "chat"),
    ("Auto - picks the best mode", "auto"),
    ("Best of N - candidates, judge, merge winner (git)", "best"),
    ("Storm - planner + parallel workers", "storm"),
    ("Cowork - architect → coder → reviewer", "cowork"),
    ("Research - parallel researchers + report", "research"),
    ("Write - outline, parallel drafts, editor", "write"),
    ("Review - parallel review lenses", "review"),
]
SWARM_KEY = "swarm"


@dataclass
class Chat:
    """One independent opencode conversation (= one tab). Several can run at the same time."""

    id: str
    n: int
    workdir: Path
    turns: int = 0
    session_id: str = ""
    busy: bool = False

    @property
    def title(self) -> str:
        return f"Chat {self.n} · {self.workdir.name or '/'}"


class SwarmApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "OpenSwarm"
    BINDINGS = [
        Binding("ctrl+n", "new_chat", "New chat"),
        Binding("alt+left", "tab(-1)", "Prev tab", show=False),
        Binding("alt+right", "tab(1)", "Next tab", show=False),
        Binding("ctrl+x", "cancel_run", "Cancel"),
        Binding("ctrl+r", "refresh_models", "Refresh models"),
    ]

    def __init__(self, cfg: SwarmConfig, repo_dir: Path, initial_mode: str | None = None, restored: bool = False) -> None:
        super().__init__()
        self._sw_restored = restored
        self._sw_pool = ServerPool(cfg)
        self._sw_ready = False
        self.cfg = cfg
        self.repo_dir = repo_dir
        self.swarm_mode = initial_mode or cfg.default_mode
        self.git: GitManager | None = None
        self.broker: Broker | None = None
        self._sw_runs: dict[str, object] = {}  # chat id or SWARM_KEY -> running Worker
        self._sw_chats: dict[str, Chat] = {}
        self._sw_chat_counter = 0
        self._sw_current_chat: str | None = None
        self._sw_agent_logs: dict[str, RichLog] = {}
        self._sw_agent_nodes: dict[str, tuple[object, str, str]] = {}
        self._sw_first_agent_of_run = True
        self._sw_msg_run = ""
        self._sw_msg_after = 0

    # ----------------------------------------------------------------- layout
    def _model_options(self, current: str) -> list[tuple[str, str]]:
        opts = [(m.display(), m.id) for m in self.cfg.models]
        if current and current not in {v for _, v in opts}:
            opts.insert(0, (current, current))
        return opts

    def compose(self) -> ComposeResult:
        r = self.cfg.roles
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with VerticalScroll(id="sidebar"):
                yield Label("Working directory (any dir)", classes="section")
                yield Input(value=str(self.repo_dir), id="workdir")
                yield Label("Mode", classes="section")
                yield Select(MODES + [(f"Workflow: {w.label}", f"wf:{w.id}") for w in self.cfg.workflows], value=self.swarm_mode, allow_blank=False, id="mode")
                yield Label("Planner / Architect model", classes="section")
                yield Select(self._model_options(r.planner), value=r.planner, allow_blank=False, id="planner-model")
                yield Label("Worker / Coder / Chat model", classes="section")
                yield Select(self._model_options(r.coder), value=r.coder, allow_blank=False, id="coder-model")
                yield Label("Reviewer model", classes="section")
                yield Select(self._model_options(r.reviewer), value=r.reviewer, allow_blank=False, id="reviewer-model")
                yield Label("Skills & flags", classes="section")
                yield SelectionList[str](*[ListItem(s.label, s.id, s.default) for s in self.cfg.skills], id="skills")
                yield Label("Active workers", classes="section")
                yield Tree("Agents", id="agents")
            with Vertical(id="main"):
                with TabbedContent(id="tabs", initial="pane-swarm"):
                    with TabPane("Swarm", id="pane-swarm"):
                        yield RichLog(id="log-swarm", wrap=True, markup=False, highlight=False, max_lines=5000)
                    with TabPane("💬 Messages", id="pane-messages"):
                        yield RichLog(id="log-messages", wrap=True, markup=False, highlight=False, max_lines=5000)
        with Horizontal(id="launchbar"):
            yield Input(placeholder="Message the active chat / describe the swarm task (use @file.docx to attach), then Enter…", id="prompt")
            yield Button("Send", id="launch", variant="primary")
            yield Button("Cancel", id="cancel", variant="error", disabled=True)
        yield Footer()

    async def on_mount(self) -> None:
        self._update_mode_ui()
        self.query_one("#agents", Tree).root.expand()
        self.query_one("#prompt", Input).focus()
        await self._setup_workspace(self.repo_dir)
        if self._sw_restored:
            self._swarm_log("Restored your last mode, models and skills.")
        self._sw_ready = True
        self.set_interval(1.5, self._sw_poll_messages)
        if self.cfg.opencode.discover_models:
            self.run_worker(self._refresh_models(), name="models", exclusive=True)

    async def on_unmount(self) -> None:
        await self._sw_pool.close()

    # --------------------------------------------------------------- workspace
    async def _setup_workspace(self, path: Path) -> None:
        path = path.expanduser().resolve()
        if not path.is_dir():
            self._swarm_log(f"ERROR: {path} is not a directory")
            return
        try:
            git = await GitManager.discover(path, self.cfg.opencode.inherit_paths, self.cfg.opencode.link_mode)
            await git.ensure_ready()
            broker = Broker(git.swarm_dir / "swarm.db")
        except (GitError, OSError) as exc:
            self._swarm_log(f"ERROR: {exc}")
            return
        if self.broker:
            self.broker.close()
        self.git, self.broker, self.repo_dir = git, broker, path
        self._sw_msg_run, self._sw_msg_after = "", 0
        try:
            self.query_one("#log-messages", RichLog).clear()
        except Exception:
            pass
        if git.is_git:
            self._swarm_log(f"Workdir: {path} (git repo, branch {await git.current_branch()}) - all modes available")
        else:
            self._swarm_log(f"Workdir: {path} - {git.note}")
        self._swarm_log("New chats start in this directory; existing chats keep theirs. Ctrl+N = new chat.")

    @on(Input.Submitted, "#workdir")
    def _workdir_submitted(self, event: Input.Submitted) -> None:
        if SWARM_KEY in self._sw_runs:
            self.notify("Wait for the running swarm to finish (or Ctrl+X).", severity="warning")
            return
        self.run_worker(self._setup_workspace(Path(event.value.strip() or ".")), name="workdir", exclusive=True)

    # ------------------------------------------------------------ model refresh
    async def _refresh_models(self) -> None:
        self._swarm_log("Querying `opencode models`…")
        found = await discover_models(self.cfg)
        if not found:
            self._swarm_log("Could not list models from opencode; using swarm_config.yaml entries.")
            return
        reconcile_models(self.cfg, found)
        for sel_id, role in (("planner-model", "planner"), ("coder-model", "coder"), ("reviewer-model", "reviewer")):
            sel = self.query_one(f"#{sel_id}", Select)
            current = getattr(self.cfg.roles, role)
            sel.set_options(self._model_options(current))
            sel.value = current
        self._swarm_log(f"{len(found)} models available from opencode.")

    def action_refresh_models(self) -> None:
        self.run_worker(self._refresh_models(), name="models", exclusive=True)

    # -------------------------------------------------------------- chats/tabs
    async def _create_chat(self) -> Chat:
        self._sw_chat_counter += 1
        chat = Chat(id="chat-" + uuid.uuid4().hex[:6], n=self._sw_chat_counter, workdir=self.repo_dir)
        self._sw_chats[chat.id] = chat
        await self._h_agent(chat.id, chat.title, "chat")
        self.query_one("#tabs", TabbedContent).active = f"pane-{chat.id}"
        self._sw_current_chat = chat.id
        return chat

    async def action_new_chat(self) -> None:
        chat = await self._create_chat()
        self.query_one("#mode", Select).value = "chat"
        self._swarm_log(f"{chat.title}: new chat in {chat.workdir}")
        self.query_one("#prompt", Input).focus()

    def _active_chat(self) -> Chat | None:
        active = self.query_one("#tabs", TabbedContent).active or ""
        chat = self._sw_chats.get(active.removeprefix("pane-"))
        return chat or (self._sw_chats.get(self._sw_current_chat or ""))

    def action_tab(self, delta: int) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        ids = [p.id for p in tabs.query(TabPane) if p.id]
        if ids and tabs.active in ids:
            tabs.active = ids[(ids.index(tabs.active) + delta) % len(ids)]

    # -------------------------------------------------------------- UI events
    def _update_mode_ui(self) -> None:
        self.sub_title = f"Mode: {self.swarm_mode.capitalize()}"
        self.query_one("#launch", Button).label = "Send" if self.swarm_mode == "chat" else "Launch Swarm"

    @on(Select.Changed, "#mode")
    def _mode_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.swarm_mode = event.value
            self._update_mode_ui()

    # ------------------------------------------------------------------ memory
    def _remember(self) -> None:
        """Persist mode/models/skills so the next launch starts exactly like this one."""
        if not self._sw_ready:
            return
        sel = self._build_selection()
        if not (sel.planner_model and sel.coder_model and sel.reviewer_model):
            return  # a Select is mid-update
        r = self.cfg.roles
        r.planner = r.architect = sel.planner_model
        r.coder, r.reviewer = sel.coder_model, sel.reviewer_model
        save_state(snapshot(self.swarm_mode, sel.planner_model, sel.coder_model, sel.reviewer_model, sel.skills))

    @on(Select.Changed)
    def _any_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in {"mode", "planner-model", "coder-model", "reviewer-model"}:
            self._remember()

    @on(SelectionList.SelectedChanged)
    def _skills_changed(self) -> None:
        self._remember()

    @on(Button.Pressed, "#launch")
    async def _launch_pressed(self) -> None:
        await self._launch()

    @on(Input.Submitted, "#prompt")
    async def _prompt_submitted(self) -> None:
        await self._launch()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel_run()

    def _build_selection(self) -> Selection:
        def val(i: str) -> str:
            v = self.query_one(i, Select).value
            return v if isinstance(v, str) else ""

        return Selection(
            mode=self.swarm_mode,
            planner_model=val("#planner-model"),
            coder_model=val("#coder-model"),
            reviewer_model=val("#reviewer-model"),
            skills=list(self.query_one("#skills", SelectionList).selected),
        )

    def _refresh_buttons(self) -> None:
        self.query_one("#cancel", Button).disabled = not self._sw_runs

    async def _launch(self) -> None:
        if not (self.git and self.broker):
            self.notify("Pick a valid working directory first.", severity="error")
            return
        box = self.query_one("#prompt", Input)
        prompt = box.value.strip()
        if not prompt:
            self.notify("Type something first.", severity="warning")
            return
        sel = self._build_selection()
        chat: Chat | None = None
        if self.swarm_mode == "chat":
            chat = self._active_chat() or await self._create_chat()
            if chat.busy:
                self.notify(f"{chat.title} is busy - press Ctrl+N to open another chat.", severity="warning")
                return
            sel.chat_id, sel.chat_title, sel.workdir = chat.id, chat.title, chat.workdir
            sel.session_id = chat.session_id
            sel.continue_session = chat.turns > 0 and not chat.session_id
            if sel.continue_session and any(
                c.id != chat.id and c.workdir == chat.workdir and c.turns > 0 for c in self._sw_chats.values()
            ):
                self._swarm_log(
                    f"WARNING: {chat.title} has no known session id and shares {chat.workdir} with another chat; "
                    "--continue may pick the other chat's session. Use different directories."
                )
            chat.busy, chat.turns = True, chat.turns + 1
            key = chat.id
        else:
            if SWARM_KEY in self._sw_runs:
                self.notify("A swarm is already running (Ctrl+X to cancel).", severity="warning")
                return
            self._sw_first_agent_of_run = True
            key = SWARM_KEY
        box.value = ""
        hooks = Hooks(on_agent=self._h_agent, on_status=self._h_status, on_line=self._h_line, on_log=self._swarm_log)
        orch = Orchestrator(self.cfg, self.git, self.broker, sel, hooks, self._sw_pool)
        self._swarm_log(f"> {f'[{chat.title}] ' if chat else ''}{prompt}")
        self._sw_runs[key] = self.run_worker(self._run_swarm(key, orch, prompt, chat), name=key, exclusive=False)
        self._refresh_buttons()

    async def _run_swarm(self, key: str, orch: Orchestrator, prompt: str, chat: Chat | None) -> None:
        try:
            summary = await orch.run(prompt)
            if chat is not None:
                if summary.session_id and not chat.session_id:
                    chat.session_id = summary.session_id
                    self._swarm_log(f"{chat.title}: session {summary.session_id} captured (parallel-safe).")
                elif not chat.session_id and chat.turns == 1 and summary.ok:
                    self._swarm_log(
                        f"{chat.title}: no session id found in output; follow-ups will use --continue "
                        "(see opencode.session_id_pattern in swarm_config.yaml)."
                    )
                if not summary.ok:
                    self.notify(f"{chat.title} failed", severity="warning")
            else:
                self.notify(
                    f"Swarm finished: {len(summary.merged)} merged, {len(summary.failed)} failed",
                    severity="information" if summary.ok else "warning",
                )
        except Exception as exc:  # surface anything unexpected instead of dying silently
            self._swarm_log(f"{key} crashed: {exc!r}")
            self.notify(f"Crashed: {exc}", severity="error")
        finally:
            self._sw_runs.pop(key, None)
            if chat is not None:
                chat.busy = False
            self._refresh_buttons()

    def action_cancel_run(self) -> None:
        """Cancel the active chat if it is running, otherwise everything that is running."""
        chat = self._active_chat()
        targets = [chat.id] if chat and chat.id in self._sw_runs else list(self._sw_runs)
        for key in targets:
            self._sw_runs[key].cancel()  # type: ignore[attr-defined]
        if targets:
            self._swarm_log(f"Cancelling: {', '.join(targets)}")

    # ------------------------------------------------------------------ hooks
    def _swarm_log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.query_one("#log-swarm", RichLog).write(Text(f"[{stamp}] {msg}"))

    @staticmethod
    def _short_agent(agent_id: str, run_id: str) -> str:
        prefix = run_id + "-"
        return agent_id[len(prefix):] if run_id and agent_id.startswith(prefix) else agent_id

    def _sw_poll_messages(self) -> None:
        """Best-effort live transcript of the newest run, without consuming unread messages."""
        if not self.broker:
            return
        try:
            run = self.broker.latest_run_id()
            if not run:
                return
            log = self.query_one("#log-messages", RichLog)
            if run != self._sw_msg_run:
                self._sw_msg_run, self._sw_msg_after = run, 0
                log.clear()
                log.write(Text(f"run {run}", style="bold"))
            for m in self.broker.messages_since(run, self._sw_msg_after):
                self._sw_msg_after = int(m["id"])
                sender = self._short_agent(str(m["sender"]), run)
                recipient = self._short_agent(str(m["recipient"]), run)
                log.write(Text(f"#{m['id']}  {sender} → {recipient}  [{m['kind']}]\n{str(m['body']).strip()}"))
        except Exception:  # the poller must never take the UI down
            return

    @staticmethod
    def _label(title: str, role: str, status: str) -> Text:
        icon, style = STATUS_STYLE.get(status, ("?", "white"))
        return Text.assemble((icon + " ", style), f"{title} ", (f"({role}) ", "dim"), (status, style))

    async def _h_agent(self, agent_id: str, title: str, role: str) -> None:
        if agent_id in self._sw_agent_logs:  # chat tabs are created up front and re-announced every turn
            return
        log = RichLog(id=f"log-{agent_id}", wrap=True, markup=False, highlight=False, max_lines=5000)
        tabs = self.query_one("#tabs", TabbedContent)
        await tabs.add_pane(TabPane(title, log, id=f"pane-{agent_id}"))
        self._sw_agent_logs[agent_id] = log
        node = self.query_one("#agents", Tree).root.add_leaf(self._label(title, role, IDLE), data=agent_id)
        self._sw_agent_nodes[agent_id] = (node, title, role)
        if self._sw_first_agent_of_run and role != "chat":
            self._sw_first_agent_of_run = False
            tabs.active = f"pane-{agent_id}"

    def _h_status(self, agent_id: str, status: str) -> None:
        entry = self._sw_agent_nodes.get(agent_id)
        if entry:
            node, title, role = entry
            node.set_label(self._label(title, role, status))  # type: ignore[attr-defined]

    def _h_line(self, agent_id: str, line: str, is_err: bool) -> None:
        log = self._sw_agent_logs.get(agent_id)
        if log is None:
            return
        text = Text.from_ansi(line)
        if is_err:
            text.stylize("red")
        log.write(text)
