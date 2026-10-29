"""OpenSwarm entry point: TUI by default, --headless for scripting, --doctor for diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .broker import Broker
from .config import (
    CONFIG_NAME,
    ConfigError,
    SwarmConfig,
    discover_models,
    find_config_path,
    load_config,
    reconcile_models,
)
from .git_manager import GitError, GitManager
from .orchestrator import Hooks, Orchestrator, Selection
from .servers import ServerPool
from .state import apply_state, forget, load_state, state_path


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openswarm", description="Multi-agent orchestrator for the opencode CLI")
    p.add_argument("prompt", nargs="*", help="master task (required with --headless)")
    p.add_argument("--dir", "--repo", dest="repo", type=Path, default=Path.cwd(), help="working directory - any directory, git repo or not (default: cwd)")
    p.add_argument("--config", type=Path, default=None, help=f"path to {CONFIG_NAME} (default: ./{CONFIG_NAME} if present, else ~/.config/openswarm/)")
    p.add_argument("--mode", default=None, help="chat|auto|heal|architect|storm|best|cowork|debate|research|write|review|wf:<workflow id>")
    p.add_argument("--priority", default=None, choices=["balanced", "quality", "fast", "free", "local"], help="work priority profile for smart model selection")
    p.add_argument("--effort", default=None, choices=["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"], help="reasoning effort / model variant")
    p.add_argument("--agents", "--parallel", dest="agents", type=int, default=None, help="number of parallel agent workers to spawn (1..64)")
    p.add_argument("--json", action="store_true", help="headless: print the result as JSON on stdout (logs go to stderr)")
    p.add_argument("--live", action="store_true", help="with --doctor: check the real opencode/server/sandbox (use --live-prompt to also send one tiny prompt)")
    p.add_argument("--live-prompt", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("-c", "--continue", dest="cont", action="store_true", help="headless chat: continue the previous session")
    p.add_argument("--headless", action="store_true", help="run without the TUI and stream logs to stdout")
    p.add_argument("--list-models", action="store_true", help="print models reported by `opencode models`")
    p.add_argument("--doctor", action="store_true", help="check git/opencode/config and exit")
    p.add_argument("--no-memory", action="store_true", help="ignore remembered models/mode/skills this run")
    p.add_argument("--reset-memory", action="store_true", help="forget remembered selections and exit")
    p.add_argument("--version", action="version", version=f"openswarm {__version__}")
    return p


def doctor(cfg: SwarmConfig, repo: Path, cfg_path: Path) -> int:
    ok = True

    def line(good: bool, msg: str, required: bool = True) -> None:
        nonlocal ok
        if required:
            ok &= good
        print(("[ok]   " if good else ("[FAIL] " if required else "[warn] ")) + msg)

    line(shutil.which("git") is not None, "git on PATH")
    oc = shutil.which(cfg.opencode.binary)
    line(oc is not None, f"opencode binary: {oc or cfg.opencode.binary + ' (not found)'}")
    if oc:
        try:
            ver = subprocess.run([oc, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
            line(True, f"opencode version: {ver or 'unknown'}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            line(False, f"opencode --version failed: {exc}")
    gm = asyncio.run(GitManager.discover(repo))
    print(f"[info] workdir {gm.start}: " + (f"git repo at {gm.root}" if gm.is_git else gm.note))
    line(cfg_path.exists(), f"config: {cfg_path}")
    line(True, f"memory file: {state_path()}")
    line(shutil.which("openswarm") is not None, "`openswarm doc` reachable on PATH (agents call it)", False)
    line(shutil.which("pdftotext") is not None, "pdftotext (PDF reading): sudo pacman -S poppler", False)
    return 0 if ok else 1


async def run_headless(cfg: SwarmConfig, repo: Path, prompt: str, mode: str, cont: bool = False, json_out: bool = False, target_agents: int | None = None) -> int:
    git = await GitManager.discover(repo, cfg.opencode.inherit_paths, cfg.opencode.link_mode)
    await git.ensure_ready()
    broker = Broker(git.swarm_dir / "swarm.db")
    sel = Selection(
        mode=mode,
        planner_model=cfg.roles.planner,
        coder_model=cfg.roles.coder,
        reviewer_model=cfg.roles.reviewer,
        skills=[s.id for s in cfg.skills if s.default],
        continue_session=cont,
        target_agents=target_agents,
    )

    log = (lambda *x: print(*x, file=sys.stderr, flush=True)) if json_out else (lambda *x: print(*x, flush=True))
    result: list[str] = []
    hooks = Hooks(
        on_status=lambda a, s: log(f"[{a}] -> {s}"),
        on_line=lambda a, t, e: log(f"[{a}] {'! ' if e else ''}{t}"),
        on_log=lambda m: log(f"[swarm] {m}"),
        on_result=lambda a, t, k: result.append(t),
    )
    summary = await Orchestrator(cfg, git, broker, sel, hooks, ServerPool(cfg)).run(prompt)
    broker.close()
    if json_out:
        print(json.dumps({"ok": summary.ok, "mode": summary.mode, "result": summary.result or (result[-1] if result else ""),
                          "merged": summary.merged, "failed": summary.failed, "session_id": summary.session_id,
                          "snapshot": summary.snapshot, "usage": summary.usage}, ensure_ascii=False))
    return 0 if summary.ok else 1


def bus_main(argv: list[str]) -> int:
    """Inter-agent & user communication bus (`openswarm msg ...`)."""
    import os
    import time

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", type=Path, default=Path.cwd())
    common.add_argument("--db", type=Path, default=None)
    common.add_argument("--run", default=os.environ.get("OPENSWARM_RUN_ID", ""))
    common.add_argument("--from", dest="sender", default=os.environ.get("OPENSWARM_AGENT") or os.environ.get("OPENSWARM_ROLE") or "agent")
    p = argparse.ArgumentParser(prog="openswarm msg", description="Inter-agent & non-blocking question bus", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="post a message to another agent or the whole swarm", parents=[common])
    s.add_argument("text")
    s.add_argument("--to", default="all", help="recipient role/agent id or 'all' (default: all)")
    s.add_argument("--kind", default="chat", choices=["chat", "question", "answer", "handoff"])

    a = sub.add_parser("ask", help="ask a question (non-blocking by default, or --wait for a reply)", parents=[common])
    a.add_argument("question")
    a.add_argument("--to", default="user", help="target ('user', 'all', or an agent/role id)")
    a.add_argument("--wait", type=float, default=0.0, help="seconds to wait for a reply before continuing (0 = non-blocking)")
    a.add_argument("--default", default="", help="fallback assumption if no reply arrives in time")

    r = sub.add_parser("reply", help="reply to a question by message id", parents=[common])
    r.add_argument("id", type=int)
    r.add_argument("text")

    rd = sub.add_parser("read", help="read messages in the current run", parents=[common])
    rd.add_argument("--for", dest="recipient", default=None)
    rd.add_argument("--since", type=int, default=0)
    rd.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    db_path = args.db or Path(os.environ.get("OPENSWARM_DB", "")) if os.environ.get("OPENSWARM_DB") else None
    if not db_path:
        gm = asyncio.run(GitManager.discover(args.dir.resolve()))
        asyncio.run(gm.ensure_ready())
        db_path = gm.swarm_dir / "swarm.db"
    broker = Broker(db_path)
    try:
        if args.cmd == "send":
            mid = broker.post_message(args.run, args.sender, args.to, args.text, kind=args.kind)
            print(f"[msg #{mid}] {args.sender} -> @{args.to}: {args.text}")
        elif args.cmd == "ask":
            qtext = args.question + (f" (Default if unanswered: {args.default})" if args.default else "")
            mid = broker.post_message(args.run, args.sender, args.to, qtext, kind="question")
            if args.wait <= 0:
                print(f"[question #{mid} posted to @{args.to} (non-blocking)]" + (f" Proceeding with default: {args.default}" if args.default else ""))
                return 0
            deadline = time.time() + args.wait
            while time.time() < deadline:
                rep = broker.find_reply(mid)
                if rep:
                    print(rep["content"])
                    return 0
                time.sleep(0.3)
            if args.default:
                print(args.default)
                return 0
            print(f"(no reply within {args.wait:g}s; proceeding autonomously)")
        elif args.cmd == "reply":
            mid = broker.post_message(args.run, args.sender, "all", args.text, kind="answer", reply_to=args.id)
            print(f"[reply #{mid} to #{args.id}] {args.text}")
        elif args.cmd == "read":
            msgs = broker.get_messages(args.run, recipient=args.recipient, since_id=args.since)
            if args.json:
                print(json.dumps(msgs, ensure_ascii=False))
            elif not msgs:
                print("(no messages)")
            else:
                for m in msgs:
                    tag = f" [{m['kind']}]" if m["kind"] != "chat" else ""
                    rep = f" (re #{m['reply_to']})" if m.get("reply_to") else ""
                    print(f"#{m['id']} {m['sender']} -> @{m['recipient']}{tag}{rep}: {m['content']}")
    finally:
        broker.close()
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("msg", "bus"):  # `openswarm msg send|ask|reply|read`
        sys.exit(bus_main(argv[1:]))
    if argv and argv[0] == "doc":  # `openswarm doc ...`: file tool used by agents, no config needed
        from .docs import docs_main

        sys.exit(docs_main(argv[1:]))
    if argv and argv[0] == "vpn":  # `openswarm vpn status|list|on|off|rotate|use|test|reset`
        from .vpn import vpn_main

        cfg = load_config((find_config_path(Path.cwd())).resolve())
        sys.exit(vpn_main(cfg, argv[1:]))
    if argv and argv[0] == "heal":  # `openswarm heal [--check]`
        from .healer import diagnose_and_heal

        cfg = load_config((find_config_path(Path.cwd())).resolve())
        apply_state(cfg, load_state())
        rep = asyncio.run(diagnose_and_heal(cfg, Path.cwd().resolve(), auto_fix="--check" not in argv))
        for c in rep["checks"]:
            tag = {"ok": "[ok]   ", "healed": "[HEAL] ", "warn": "[warn] ", "error": "[FAIL] "}.get(c["status"], "[info] ")
            print(f"{tag}{c['title']}: {c['detail']}")
        for fx in rep.get("fixes") or []:
            print(f"  -> fixed: {fx}")
        for iss in rep.get("recent_issues") or []:
            print(f"  [issue in {iss['session']}] {iss['title']}: {iss['diagnosis']} -> {iss['remediation']}")
        sys.exit(0 if rep["ok"] else 1)
    if argv and argv[0] == "plugins":  # `openswarm plugins list|mcp-enable|mcp-disable|hook|sync-skills`
        from .plugins import plugins_main

        sys.exit(plugins_main(Path.cwd().resolve(), argv[1:]))
    if argv and argv[0] == "priority":  # `openswarm priority [balanced|quality|fast|free|local]`
        from .router import VALID_PRIORITIES, get_router, priority_list
        from .state import save_state

        cfg = load_config((find_config_path(Path.cwd())).resolve())
        apply_state(cfg, load_state())
        if cfg.opencode.discover_models:
            reconcile_models(cfg, asyncio.run(discover_models(cfg)))
        router = get_router()
        if len(argv) > 1 and argv[1] in VALID_PRIORITIES:
            cfg.priority = argv[1]
            rec = router.recommend_roles(cfg, cfg.priority)
            for r, m in rec.items():
                if hasattr(cfg.roles, r) and m:
                    setattr(cfg.roles, r, m)
            st = load_state()
            st["priority"] = cfg.priority
            st["roles"] = rec
            save_state(st)
            print(f"Priority set to '{cfg.priority}' -> planner={cfg.roles.planner}, coder={cfg.roles.coder}, reviewer={cfg.roles.reviewer}")
        else:
            for p in priority_list():
                mark = "*" if p["id"] == cfg.priority else " "
                print(f"{mark} {p['icon']} {p['id']:<10} {p['label']:<16} — {p['desc']}")
        sys.exit(0)
    if argv and argv[0] == "skills":  # `openswarm skills list|install|show [--project] [--force]`
        from .skillpack import BUNDLE, discover_skills, install_skills

        sub = argv[1] if len(argv) > 1 else "list"
        if sub == "install":
            names = install_skills(Path.cwd() if "--project" in argv else None, "--force" in argv)
            print("installed: " + (", ".join(names) if names else "nothing new (use --force to overwrite)"))
        elif sub == "show" and len(argv) > 2:
            name = argv[2]
            if name in BUNDLE:
                desc, body = BUNDLE[name]
                print(f"# {name} — {desc}\n\n{body.strip()}")
            else:
                hit = next((s for s in discover_skills(Path.cwd()) if s["name"] == name), None)
                if hit:
                    print(Path(hit["path"]).read_text(encoding="utf-8", errors="replace"))
                else:
                    sys.exit(f"unknown skill '{name}'")
        else:
            for sk in discover_skills(Path.cwd()):
                print(f"{sk['scope']:8} {sk['name']:28} {sk['description'][:70]}")
            print(f"\nbundled with OpenSwarm ({len(BUNDLE)}): {', '.join(BUNDLE)}  ->  `openswarm skills install`")
        sys.exit(0)
    if argv and argv[0] == "presets":  # `openswarm presets`
        cfg = load_config((find_config_path(Path.cwd())).resolve())
        for pr in cfg.presets:
            print(f"{pr.id:14} [{pr.mode:8}] {pr.label:24} — {pr.description}")
        sys.exit(0)
    if argv and argv[0] == "workflows":  # `openswarm workflows`
        cfg = load_config((find_config_path(Path.cwd())).resolve())
        for wf in cfg.workflows:
            steps = " -> ".join(f"{s.id}({s.kind})" for s in wf.steps)
            print(f"wf:{wf.id:16} {wf.label:42} [{steps}]")
        sys.exit(0)
    if argv and argv[0] == "schedule":  # `openswarm schedule [list|add|run|cancel]`
        from .scheduler import TaskScheduler

        sched = TaskScheduler()
        sub = argv[1] if len(argv) > 1 else "list"
        if sub == "list":
            tasks = sched.list_tasks(include_finished=True)
            if not tasks:
                print("No scheduled tasks found.")
            else:
                for t in tasks:
                    status_emoji = {"pending": "⏳", "running": "●", "completed": "✔", "failed": "✘", "cancelled": "🚫"}.get(t["status"], "•")
                    print(f"{status_emoji} {t['id']:10} [{t['status']:9}] run_at: {t.get('run_at_iso','')} mode: {t.get('mode','chat')} prompt: {t.get('prompt','')[:60]}")
            sys.exit(0)
        elif sub == "add":
            prompt = argv[2] if len(argv) > 2 else ""
            if not prompt:
                sys.exit("Usage: openswarm schedule add \"prompt\" [--delay <seconds>] [--mode <mode>] [--agents <N>]")
            delay = 3600.0
            mode = "chat"
            agents = 1
            i = 3
            while i < len(argv):
                if argv[i] == "--delay" and i + 1 < len(argv):
                    delay = float(argv[i + 1])
                    i += 2
                elif argv[i] == "--mode" and i + 1 < len(argv):
                    mode = argv[i + 1]
                    i += 2
                elif argv[i] in ("--agents", "--parallel") and i + 1 < len(argv):
                    agents = int(argv[i + 1])
                    i += 2
                else:
                    i += 1
            t = sched.schedule(prompt=prompt, delay_seconds=delay, mode=mode, target_agents=agents)
            print(f"Scheduled task {t.id} to run in {int(delay)}s ({t.run_at_iso})")
            sys.exit(0)
        elif sub == "cancel":
            tid = argv[2] if len(argv) > 2 else ""
            if not tid:
                sys.exit("Usage: openswarm schedule cancel <task_id>")
            ok = sched.cancel(tid)
            print(f"Task {tid} {'cancelled' if ok else 'not found or already done'}")
            sys.exit(0 if ok else 1)
        elif sub == "run":
            tid = argv[2] if len(argv) > 2 else ""
            task = sched.tasks.get(tid)
            if not task:
                sys.exit(f"Task {tid} not found")
            cfg = load_config((find_config_path(Path.cwd())).resolve())
            rc = asyncio.run(run_headless(cfg, Path.cwd(), task.prompt, task.mode, target_agents=task.target_agents))
            task.status = "completed" if rc == 0 else "failed"
            sched._save()
            sys.exit(rc)
        else:
            sys.exit("Usage: openswarm schedule [list|add|run|cancel]")

    if argv and argv[0] == "selftest":
        from .selftest import run_selftest

        sys.exit(run_selftest("-v" in argv))
    if argv and argv[0] == "ask":  # `openswarm ask "prompt" --mode research --json`  (scripting)
        argv = ["--headless", *argv[1:]]
    if argv and argv[0] == "audit":
        from .safety import audit_path

        try:
            print("\n".join(audit_path().read_text(encoding="utf-8").splitlines()[-int(argv[1]) if len(argv) > 1 else -50:]))
        except OSError:
            print("(no audit log yet)")
        sys.exit(0)
    if argv and argv[0] == "snapshots":  # `openswarm snapshots [list|restore ID] [--dir D]`
        from .safety import list_snapshots, restore_snapshot

        d = Path(argv[argv.index("--dir") + 1] if "--dir" in argv else Path.cwd()).resolve()
        if len(argv) > 2 and argv[1] == "restore":
            print(restore_snapshot(d, argv[2]))
        else:
            print("\n".join(list_snapshots(d)) or "(no snapshots for this directory)")
        sys.exit(0)
    if argv and argv[0] == "gui":  # native window (GTK4/WebKitGTK -> Chromium app -> browser)
        from .gui import gui_main

        sys.exit(gui_main(argv[1:]))
    if argv and argv[0] == "desktop-install":  # icon + application-menu entry
        from .gui import install_desktop

        print("\n".join(str(p) for p in install_desktop()))
        sys.exit(0)
    if argv and argv[0] == "web":  # `openswarm web [--port N] [--dir D] [--app] [--no-open]`
        from .web import web_main

        sys.exit(web_main(argv[1:]))
    args = _parser().parse_args(argv)
    if args.reset_memory:
        print(f"memory cleared ({state_path()})" if forget() else "nothing to forget")
        sys.exit(0)
    repo = args.repo.resolve()
    cfg_path = (args.config or find_config_path(repo)).resolve()
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        sys.exit(str(exc))

    restored = False if args.no_memory else apply_state(cfg, load_state())

    if args.doctor:
        rc = doctor(cfg, repo, cfg_path)
        if args.live or args.live_prompt:
            from .selftest import print_live

            print()
            rc = print_live(cfg, args.live_prompt) or rc
        sys.exit(rc)

    if args.list_models:
        found = asyncio.run(discover_models(cfg))
        print("\n".join(found) if found else "No models reported (is opencode installed and logged in?)")
        sys.exit(0 if found else 1)

    mode = args.mode or cfg.default_mode
    if args.effort:
        cfg.effort = args.effort
    if args.agents:
        cfg.max_parallel = max(1, min(64, args.agents))
    # Determine target agent count: CLI flag > environment variable > default (0 = auto)
    target_agents = None
    if args.agents is not None:
        target_agents = args.agents
    elif env_agents := os.environ.get("OPENSWARM_TARGET_AGENTS"):
        target_agents = int(env_agents)
    if target_agents is not None:
        target_agents = max(1, min(64, target_agents))
    if args.priority:
        from .router import get_router

        cfg.priority = args.priority
        for r, m in get_router().recommend_roles(cfg, cfg.priority).items():
            if hasattr(cfg.roles, r) and m:
                setattr(cfg.roles, r, m)

    if args.headless:
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            sys.exit("--headless needs a prompt")
        if cfg.opencode.discover_models:
            reconcile_models(cfg, asyncio.run(discover_models(cfg)))
        try:
            sys.exit(asyncio.run(run_headless(cfg, repo, prompt, mode, args.cont, args.json, target_agents)))
        except GitError as exc:
            sys.exit(str(exc))

    from .tui import SwarmApp  # imported lazily so --doctor/--headless work without a terminal

    SwarmApp(cfg, repo, mode, restored, target_agents).run()


if __name__ == "__main__":
    main()
