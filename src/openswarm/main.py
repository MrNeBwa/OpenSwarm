"""OpenSwarm entry point: TUI by default, --headless for scripting, --doctor for diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
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
    p.add_argument("--mode", default=None, help="chat|auto|storm|best|cowork|research|write|review|wf:<workflow id>")
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


async def run_headless(cfg: SwarmConfig, repo: Path, prompt: str, mode: str, cont: bool = False, json_out: bool = False) -> int:
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


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "doc":  # `openswarm doc ...`: file tool used by agents, no config needed
        from .docs import docs_main

        sys.exit(docs_main(argv[1:]))
    if argv and argv[0] == "skills":  # `openswarm skills list|install [--project] [--force]`
        from .skillpack import BUNDLE, discover_skills, install_skills

        sub = argv[1] if len(argv) > 1 else "list"
        if sub == "install":
            names = install_skills(Path.cwd() if "--project" in argv else None, "--force" in argv)
            print("installed: " + (", ".join(names) if names else "nothing new (use --force to overwrite)"))
        else:
            for sk in discover_skills(Path.cwd()):
                print(f"{sk['scope']:8} {sk['name']:28} {sk['description'][:70]}")
            print(f"\nbundled with OpenSwarm: {', '.join(BUNDLE)}  ->  `openswarm skills install`")
        sys.exit(0)
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

    if args.headless:
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            sys.exit("--headless needs a prompt")
        if cfg.opencode.discover_models:
            reconcile_models(cfg, asyncio.run(discover_models(cfg)))
        try:
            sys.exit(asyncio.run(run_headless(cfg, repo, prompt, mode, args.cont, args.json)))
        except GitError as exc:
            sys.exit(str(exc))

    from .tui import SwarmApp  # imported lazily so --doctor/--headless work without a terminal

    SwarmApp(cfg, repo, mode, restored).run()


if __name__ == "__main__":
    main()
