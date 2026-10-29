"""Self-Healing & Error Analysis Engine for OpenSwarm.

Provides:
1. `analyze_error(text)` — root-cause classifier for agent, model, network, VPN, git, and sandbox errors.
2. `heal_git_workspace(workdir)` — cleans stale `.git/index.lock` files and prunes broken git worktrees.
3. `diagnose_and_heal(cfg, workdir, pool, auto_fix)` — full system doctor + automatic self-repair across
   `opencode`, model routing, per-app VPN, git worktrees, skills/plugins, and recent session failure logs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import ROLES, SwarmConfig, discover_models, reconcile_models
from .router import get_router
from .skillpack import BUNDLE, discover_skills, install_skills
from .state import state_path

ERROR_PATTERNS: list[tuple[str, str, str, str]] = [
    (
        "model_broken",
        r"ModelNotFoundError|model[^\n]{0,40}not found|invalid model|unknown model|no endpoints found|model_not_found|does not exist or you do not have access|ProviderModelNotFoundError",
        "Model is unavailable, deprecated, or not supported by the provider.",
        "Mark model as unhealthy and automatically switch to the highest-ranked working model.",
    ),
    (
        "rate_limit",
        r"FreeUsageLimitError|rate.?limit|\b429\b|too many requests|quota exceeded|usage limit|limit (?:reached|exceeded)|free (?:tier|usage)|overloaded",
        "Provider rate limit or free-tier usage quota reached (HTTP 429).",
        "Swap VPN exit server (IPv4) / rotate OpenRouter key, or failover to an alternative free model.",
    ),
    (
        "auth_error",
        r"\b401\b|\b403\b|unauthorized|invalid[^\n]{0,30}api.?key|authentication failed|missing api key|insufficient_quota|no credits",
        "Authentication failed or API key has no remaining credits.",
        "Rotate API key or switch role to a working key-free opencode/* model.",
    ),
    (
        "vpn_tunnel",
        r"proxyconnect tcp|socks connect|tunnel via [^\n]+ does not pass traffic|port \d+ is already in use|xray exited|xray did not open",
        "Per-app VPN proxy (xray) failed to pass traffic or had a port conflict.",
        "Reap stale xray process, rotate to a fresh IPv4 WireGuard exit server, and reset warm server pool.",
    ),
    (
        "daemon_connection",
        r"ECONNREFUSED|fetch failed|unable to connect|failed to connect|socket hang up|connection reset by peer|HTTP API unavailable",
        "Warm `opencode serve` daemon crashed or closed its connection.",
        "Reset warm server pool and retry with isolated `--standalone` execution.",
    ),
    (
        "git_worktree",
        r"index\.lock|already exists|is already checked out|worktree[^\n]{0,50}locked|cannot lock ref",
        "Git repository has a stale lock file or orphaned worktree registration.",
        "Remove stale `.git/index.lock` and run `git worktree prune`.",
    ),
    (
        "sandbox_error",
        r"bwrap:|Creating new namespace failed|Operation not permitted|bubblewrap",
        "Bubblewrap (bwrap) user-namespace sandbox is blocked by kernel policy.",
        "Disable sandbox for this run or enable unprivileged user namespaces.",
    ),
    (
        "cli_flag",
        r"unrecognized flag|unknown flag|unknown option|unrecognized option|flag provided but not defined",
        "The installed opencode CLI version rejected a command-line flag.",
        "Disable the unsupported flag in capability cache and retry automatically.",
    ),
    (
        "variant_unavailable",
        r"variant unavailable",
        "The selected model does not support reasoning effort or variants.",
        "Disable reasoning variant/effort for this model and retry execution.",
    ),
]


def analyze_error(text: str) -> dict[str, str]:
    """Classify raw stderr/stdout into a root cause, explanation, and remediation."""
    clean = (text or "").strip()
    if not clean:
        return {
            "kind": "unknown",
            "title": "Unknown / Empty Output",
            "diagnosis": "The agent exited without producing output or a recognizable error signature.",
            "remediation": "Retry with a healthier model or inspect the full agent log.",
            "snippet": "",
        }
    for kind, pat, diag, fix in ERROR_PATTERNS:
        m = re.search(pat, clean, re.I)
        if m:
            line = next((ln.strip() for ln in clean.splitlines() if m.group(0).lower() in ln.lower()), m.group(0))
            return {
                "kind": kind,
                "title": kind.replace("_", " ").title(),
                "diagnosis": diag,
                "remediation": fix,
                "snippet": line[:240],
            }
    tail = [ln.strip() for ln in clean.splitlines() if ln.strip()][-2:]
    return {
        "kind": "runtime_error",
        "title": "Agent / Command Error",
        "diagnosis": "The command or agent exited with a non-zero status.",
        "remediation": "Run Self-Heal mode (`heal`) to inspect the stack trace and repair the failure.",
        "snippet": " | ".join(tail)[:240],
    }


def heal_git_workspace(workdir: Path) -> list[str]:
    """Clean stale git locks and prune dead worktrees in `workdir`. Returns list of actions taken."""
    actions: list[str] = []
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return actions
        root = Path(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return actions

    lock = root / ".git" / "index.lock"
    if lock.exists():
        try:
            age = time.time() - lock.stat().st_mtime
            if age > 3.0:
                lock.unlink(missing_ok=True)
                actions.append(f"Removed stale {lock} (age {age:.0f}s)")
        except OSError:
            pass

    try:
        subprocess.run(["git", "worktree", "prune"], cwd=str(root), capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return actions


def scan_recent_session_issues(limit_sessions: int = 8) -> list[dict[str, Any]]:
    """Inspect recent session logs for failed runs or agent errors and classify their root causes."""
    sdir = state_path().parent / "sessions"
    if not sdir.is_dir():
        return []
    issues: list[dict[str, Any]] = []
    files = sorted(sdir.glob("*.jsonl"), key=lambda p: -p.stat().st_mtime)[:limit_sessions]
    for f in files:
        sid = f.stem
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        except OSError:
            continue
        err_buf: list[str] = []
        failed_done = False
        for ln in reversed(lines):
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "done":
                if not ev.get("ok"):
                    failed_done = True
                else:
                    break  # most recent run in this session succeeded
            elif t == "line" and ev.get("err") and len(err_buf) < 6:
                err_buf.append(str(ev.get("text", "")))
        if failed_done or err_buf:
            combined = "\n".join(reversed(err_buf))
            diag = analyze_error(combined)
            issues.append({
                "session": sid,
                "kind": diag["kind"],
                "title": diag["title"],
                "diagnosis": diag["diagnosis"],
                "remediation": diag["remediation"],
                "snippet": diag["snippet"],
            })
    return issues[:6]


async def diagnose_and_heal(
    cfg: SwarmConfig,
    workdir: Path,
    pool: Any = None,
    auto_fix: bool = True,
) -> dict[str, Any]:
    """Comprehensive system health check + self-repair."""
    checks: list[dict[str, Any]] = []
    fixes: list[str] = []
    router = get_router()

    # 1. OpenCode binary & capabilities
    oc_bin = shutil.which(cfg.opencode.binary)
    if oc_bin:
        try:
            proc = await asyncio.create_subprocess_exec(
                oc_bin, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            ver = out.decode(errors="replace").strip()
            checks.append({"id": "opencode", "status": "ok", "title": "OpenCode CLI", "detail": f"{oc_bin} ({ver})"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"id": "opencode", "status": "error", "title": "OpenCode CLI", "detail": str(exc)[:160]})
    else:
        checks.append({"id": "opencode", "status": "error", "title": "OpenCode CLI", "detail": f"'{cfg.opencode.binary}' not found on PATH"})

    # 2. Model catalogue & role assignment health
    if cfg.opencode.discover_models and oc_bin:
        discovered = await discover_models(cfg)
        if discovered:
            reconcile_models(cfg, discovered)
    known_ids = set(cfg.model_ids())
    unhealthy_roles: list[str] = []
    for role in ROLES:
        mid = getattr(cfg.roles, role, "")
        hlth = router.health_info(mid)
        if not mid or (known_ids and mid not in known_ids) or not hlth["healthy"]:
            unhealthy_roles.append(f"{role} ({mid or 'unset'})")
    if unhealthy_roles:
        if auto_fix:
            router.clear_cooldowns()
            rec = router.recommend_roles(cfg, getattr(cfg, "priority", "balanced"))
            for r, new_m in rec.items():
                if hasattr(cfg.roles, r) and new_m:
                    setattr(cfg.roles, r, new_m)
            fixes.append(f"Healed role models -> planner={cfg.roles.planner}, coder={cfg.roles.coder}, reviewer={cfg.roles.reviewer}")
            checks.append({"id": "models", "status": "healed", "title": "Models & Role Routing", "detail": f"Auto-repaired roles: {', '.join(unhealthy_roles)}"})
        else:
            checks.append({"id": "models", "status": "warn", "title": "Models & Role Routing", "detail": f"Unhealthy/missing roles: {', '.join(unhealthy_roles)}"})
    else:
        checks.append({
            "id": "models",
            "status": "ok",
            "title": "Models & Role Routing",
            "detail": f"{len(cfg.models)} models ready · priority={getattr(cfg, 'priority', 'balanced')} · coder={cfg.roles.coder}",
        })

    # 3. Per-App VPN health
    from .vpn import get_vpn

    vpn = get_vpn(cfg)
    if vpn.enabled:
        if vpn.running():
            checks.append({"id": "vpn", "status": "ok", "title": "Per-App VPN (NinjaVPN + Xray)", "detail": f"Active via {vpn.current} (127.0.0.1:{vpn.s.http_port}, IPv4-forced)"})
        elif auto_fix:
            ok = await asyncio.to_thread(vpn.ensure)
            if ok:
                if pool is not None:
                    await pool.close()
                fixes.append(f"Restarted per-app VPN tunnel via {vpn.current} (127.0.0.1:{vpn.s.http_port})")
                checks.append({"id": "vpn", "status": "healed", "title": "Per-App VPN (NinjaVPN + Xray)", "detail": f"Healed & connected via {vpn.current}"})
            else:
                checks.append({"id": "vpn", "status": "warn", "title": "Per-App VPN (NinjaVPN + Xray)", "detail": f"Enabled but could not start: {vpn.last_error}"})
        else:
            checks.append({"id": "vpn", "status": "warn", "title": "Per-App VPN (NinjaVPN + Xray)", "detail": "Enabled in state, but xray tunnel is not running"})
    else:
        checks.append({"id": "vpn", "status": "ok", "title": "Per-App VPN (NinjaVPN + Xray)", "detail": "Disabled (agents connect directly)"})

    # 4. Git & Worktree health
    git_actions = await asyncio.to_thread(heal_git_workspace, workdir) if auto_fix else []
    if git_actions:
        fixes.extend(git_actions)
        checks.append({"id": "git", "status": "healed", "title": "Git & Worktrees", "detail": "; ".join(git_actions)})
    else:
        checks.append({"id": "git", "status": "ok", "title": "Git & Worktrees", "detail": f"Clean workspace at {workdir}"})

    # 5. Warm server pool health
    if pool is not None and getattr(pool, "api_bad", False) and auto_fix:
        await pool.close()
        pool.api_bad = False
        fixes.append("Reset warm opencode server pool and cleared api_bad flag")
        checks.append({"id": "servers", "status": "healed", "title": "Warm Server Pool", "detail": "Cleared stale server state"})
    else:
        checks.append({"id": "servers", "status": "ok", "title": "Warm Server Pool", "detail": "Ready (--standalone isolation active for workers)"})

    # 6. Bundled Native Skills
    native = discover_skills(workdir)
    if len(native) < len(BUNDLE) and auto_fix:
        written = await asyncio.to_thread(install_skills, None, False)
        if written:
            fixes.append(f"Installed {len(written)} bundled OpenSwarm native skills")
        native = discover_skills(workdir)
        checks.append({"id": "skills", "status": "healed" if written else "ok", "title": "Skills & Plugins", "detail": f"{len(native)} native skills active"})
    else:
        checks.append({"id": "skills", "status": "ok", "title": "Skills & Plugins", "detail": f"{len(native)} native skills active"})

    # 7. Recent session issues
    recent_issues = await asyncio.to_thread(scan_recent_session_issues)

    overall_ok = all(c["status"] in ("ok", "healed") for c in checks)
    return {
        "ok": overall_ok,
        "checks": checks,
        "fixes": fixes,
        "recent_issues": recent_issues,
        "recommended_roles": router.recommend_roles(cfg, getattr(cfg, "priority", "balanced")),
    }
