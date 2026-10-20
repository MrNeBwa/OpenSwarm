"""`openswarm selftest` (bundled test-suite on an offline fake opencode) and `openswarm doctor --live`
(checks the assumptions OpenSwarm makes about YOUR real opencode, sandbox and helper tools)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from .config import SwarmConfig, discover_models


def run_selftest(verbose: bool = False) -> int:
    here = Path(__file__).parent
    suite = unittest.defaultTestLoader.discover(str(here / "tests"), top_level_dir=str(here.parent))
    res = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    return 0 if res.wasSuccessful() else 1


def _run(cmd: list[str], timeout: int = 20, cwd: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, (r.stdout + r.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


async def live_checks(cfg: SwarmConfig, prompt: bool = False) -> list[tuple[str, str]]:
    from .apiclient import Http, run_chat_api
    from .safety import SandboxError, bwrap_command
    from .servers import ServerPool

    out: list[tuple[str, str]] = []
    add = lambda lvl, msg: out.append((lvl, msg))  # noqa: E731
    oc = cfg.opencode
    binary = shutil.which(oc.binary)
    if not binary:
        add("FAIL", f"opencode binary '{oc.binary}' not found on PATH")
        return out
    add("ok", f"opencode: {binary} ({_run([binary, '--version'])[1].strip() or 'version unknown'})")

    _, run_help = _run([binary, *oc.subcommand, "--help"])
    for flag, need in (("--model", True), ("--session", True), ("--title", True), ("--attach", False), ("--file", False), ("--agent", False), ("--continue", False)):
        if flag in run_help:
            add("ok", f"`run` supports {flag}")
        else:
            add("FAIL" if need else "warn", f"`run --help` does not mention {flag}" + ("" if need else " (related feature degrades gracefully)"))
    _, serve_help = _run([binary, *oc.serve_command, "--help"])
    for flag in ("--port", "--hostname"):
        add("ok" if flag in serve_help else "FAIL", f"`serve` {'supports' if flag in serve_help else 'does not mention'} {flag}")

    models = await discover_models(cfg)
    add("ok" if models else "warn", f"`opencode models`: {len(models)} model(s)" + ("" if models else " (not logged in? run `opencode auth login`)"))
    free = [m for m in models if "free" in m]
    if models and not any(m in models for m in (cfg.roles.planner, cfg.roles.coder)):
        add("warn", f"configured role models are not in `opencode models` (example available: {models[0]}); pick models in the UI")
    rc, sl = _run([binary, "session", "list", "--format", "json", "-n", "1"])
    add("ok" if rc == 0 else "warn", "`session list --format json` works" if rc == 0 else f"`session list --format json` failed: {sl.strip()[:80]}")

    pool = ServerPool(cfg)
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        try:
            import os

            ep = await pool.endpoint(wd, dict(os.environ))
            if not ep:
                add("FAIL", "could not start `opencode serve` (chat falls back to plain `run`; speed/permissions UI unavailable)")
            else:
                url, pw = ep
                add("ok", f"`opencode serve` started on {url}")
                code, _ = await Http(url, "").request("GET", "/session")
                add("ok" if code == 401 else "FAIL", "server rejects requests without the password" if code == 401 else f"server did NOT reject an unauthenticated request (HTTP {code}) - do not use warm servers")
                http = Http(url, pw)
                code, sess = await http.request("POST", "/session", {"title": "openswarm-doctor"})
                if code < 300 and isinstance(sess, dict) and sess.get("id"):
                    add("ok", "HTTP API: POST /session works")
                    await http.request("DELETE", f"/session/{sess['id']}")
                else:
                    add("FAIL", f"HTTP API: POST /session -> {code} {str(sess)[:80]}")
                kinds: list[str] = []
                try:
                    async for ev in http.sse("/event"):
                        kinds.append(str(ev.get("type")))
                        break
                except Exception as exc:  # noqa: BLE001
                    add("FAIL", f"SSE /event failed: {exc}")
                add("ok" if kinds[:1] == ["server.connected"] else "warn", f"SSE first event: {kinds[:1]}")
                if prompt:
                    seen: list[str] = []
                    r = await run_chat_api(http, text="Reply with the single word: ok", model=cfg.roles.coder, on_event=lambda k, p: seen.append(k),
                                           on_permission=None, timeout=120)
                    add("ok" if r.ok and r.text else "FAIL", f"live prompt via API -> {r.text[:60]!r} (events: {sorted(set(seen))}, tokens: {r.tokens}, cost: {r.cost}) {r.error}")
        except Exception as exc:  # noqa: BLE001
            add("FAIL", f"live server check crashed: {exc!r}")
        finally:
            await pool.close()

        if shutil.which("bwrap"):
            try:
                probe = Path.home() / ".openswarm_sandbox_probe"
                cmd = bwrap_command(["sh", "-c", f"echo ok > {wd}/w; echo $? ; (echo x > {probe}) 2>/dev/null; echo $?"], wd, cfg.security)
                rc, o = _run(cmd)
                lines = o.split()
                writable, blocked = (lines[0] == "0"), (len(lines) > 1 and lines[1] != "0")
                probe.unlink(missing_ok=True)
                add("ok" if writable and blocked else "FAIL", "bubblewrap sandbox: workdir writable, home read-only" if writable and blocked else f"bubblewrap sandbox misbehaves: {o.strip()[:120]}")
            except SandboxError as exc:
                add("warn", str(exc))
        else:
            add("warn", "bubblewrap not installed: sudo pacman -S bubblewrap (needed only for the Sandbox option)")
    for tool, why in (("soffice", "office -> PDF export"), ("pandoc", "document conversion"), ("pdftotext", "reading PDFs"), ("tesseract", "OCR"), ("bwrap", "sandbox")):
        if not shutil.which(tool):
            add("warn", f"{tool} not found ({why})")
    return out


def print_live(cfg: SwarmConfig, prompt: bool = False) -> int:
    res = asyncio.run(live_checks(cfg, prompt))
    for lvl, msg in res:
        print(f"[{lvl:4}] {msg}")
    return 1 if any(l == "FAIL" for l, _ in res) else 0
