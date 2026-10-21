"""Agent-to-agent message bus over the run's SQLite broker.

Storm workers run in separate git worktrees, but they share
the run database in ``<repo>/.swarm/swarm.db``. This CLI exposes that database as a tiny message bus:

    openswarm msg agents
    openswarm msg ask --to <agent> --body "Which contract for parse()?"   # non-blocking
    openswarm msg check                                                  # non-blocking inbox
    openswarm msg reply <message-id> --body "Use the tuple form"         # answer a peer
    openswarm msg history --limit 20

``ask`` returns immediately and prints the message id, so an agent can keep working and collect
the answer at its next checkpoint with ``check``. Use ``inbox --wait N`` only for a hard dependency.
The orchestrator exports ``OPENSWARM_BROKER_DB``, ``OPENSWARM_RUN_ID`` and ``OPENSWARM_AGENT``
into every worker, so the commands work from any worktree without extra flags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .broker import Broker

EXIT_NO_RUN = 3


def _print(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openswarm msg", description="Message bus for agents of the current run")
    p.add_argument("--db", type=Path, default=None, help="broker SQLite file (default: $OPENSWARM_BROKER_DB or ./.swarm/swarm.db)")
    p.add_argument("--run", default="", help="run id (default: $OPENSWARM_RUN_ID or the latest run)")
    p.add_argument("--agent", default="", help="your agent id (default: $OPENSWARM_AGENT)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="send a message to an agent, a comma-separated list, or *")
    s.add_argument("--to", required=True, help="recipient agent id, comma list, or * for broadcast")
    s.add_argument("--body", "-m", default="", help="message text ('-' reads stdin)")
    s.add_argument("--kind", default="note", help="note|question|answer|advice|warning|result")
    s.add_argument("--reply-to", type=int, default=None, help="id of the message being answered")
    s.add_argument("--from", dest="sender", default="", help="sender id (default: your agent id)")

    i = sub.add_parser("inbox", help="read and acknowledge incoming messages (blocks only with --wait)")
    i.add_argument("--limit", type=int, default=50)
    i.add_argument("--peek", action="store_true", help="show messages without marking them read")
    i.add_argument("--wait", type=float, default=0.0, help="seconds to wait for the first message (0 = return at once)")
    i.add_argument("--grace", type=float, default=1.5, help="after the first message, keep collecting for this many seconds")
    i.add_argument("--json", action="store_true", help="accepted for compatibility; output is always JSON")

    a = sub.add_parser("ask", help="send a question and return immediately (do not block; collect the reply later)")
    a.add_argument("--to", required=True, help="recipient agent id, comma list, or * for broadcast")
    a.add_argument("--body", "-m", default="", help="question text ('-' reads stdin)")
    a.add_argument("--kind", default="question", help="usually question (note|question|advice)")
    a.add_argument("--from", dest="sender", default="", help="sender id (default: your agent id)")

    c = sub.add_parser("check", help="non-blocking inbox check: show and acknowledge replies that have arrived")
    c.add_argument("--limit", type=int, default=50)

    r = sub.add_parser("reply", help="answer a message by id (goes to its sender)")
    r.add_argument("message_id", type=int, help="id shown by `ask`, `send`, `check` or `inbox`")
    r.add_argument("--body", "-m", default="", help="answer text ('-' reads stdin)")
    r.add_argument("--from", dest="sender", default="", help="sender id (default: your agent id)")

    h = sub.add_parser("history", help="recent messages in this run")
    h.add_argument("--limit", type=int, default=50)
    h.add_argument("--json", action="store_true", help="accepted for compatibility; output is always JSON")

    sub.add_parser("agents", help="agents taking part in this run")
    return p


def _resolve(args: argparse.Namespace) -> tuple[Broker, str, str]:
    path = args.db or os.environ.get("OPENSWARM_BROKER_DB")
    if not path:
        path = Path.cwd() / ".swarm" / "swarm.db"
    path = Path(path)
    if not path.exists():
        raise SystemExit(
            f"no broker database at {path} - run this from an OpenSwarm run or set OPENSWARM_BROKER_DB"
        )
    broker = Broker(path)
    run_id = args.run or os.environ.get("OPENSWARM_RUN_ID") or broker.latest_run_id()
    agent = args.agent or os.environ.get("OPENSWARM_AGENT") or "agent"
    if not run_id:
        broker.close()
        raise SystemExit("no run id: set OPENSWARM_RUN_ID or pass --run")
    return broker, run_id, agent


def _body(raw: str) -> str:
    return sys.stdin.read() if raw == "-" else (raw or "")


def _collect(broker: Broker, run_id: str, agent: str, limit: int, wait: float, grace: float, peek: bool) -> list[dict]:
    deadline = time.time() + max(0.0, wait)
    messages = broker.inbox(run_id, agent, limit=limit, peek=True)
    while not messages and time.time() < deadline:
        time.sleep(0.25)
        messages = broker.inbox(run_id, agent, limit=limit, peek=True)
    if messages and grace > 0:
        # Peers often answer a few hundred ms apart; collect a short grace window so a
        # second reply is not mistaken for "no answer".
        grace_end = time.time() + min(max(grace, 0.0), 10.0)
        while time.time() < grace_end:
            time.sleep(0.25)
    if not messages:
        return []
    return broker.inbox(run_id, agent, limit=limit, peek=peek)


def comm_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    broker, run_id, agent = _resolve(args)
    try:
        if args.cmd == "send":
            body = _body(args.body)
            if not body.strip():
                print("message body is empty", file=sys.stderr)
                return 2
            sender = args.sender or agent
            mid = broker.send_message(run_id, sender, args.to, body, args.kind or "note", args.reply_to)
            _print({"ok": True, "id": mid, "run_id": run_id, "from": sender, "to": args.to, "kind": args.kind})
            return 0

        if args.cmd == "ask":
            body = _body(args.body)
            if not body.strip():
                print("question body is empty", file=sys.stderr)
                return 2
            sender = args.sender or agent
            mid = broker.send_message(run_id, sender, args.to, body, args.kind or "question")
            _print({
                "ok": True, "id": mid, "run_id": run_id, "from": sender, "to": args.to,
                "kind": args.kind or "question", "non_blocking": True,
                "hint": f"keep working; later run `openswarm msg check` (replies carry reply_to={mid})",
            })
            return 0

        if args.cmd == "reply":
            original = broker.get_message(run_id, args.message_id)
            if not original:
                print(f"no message {args.message_id} in run {run_id}", file=sys.stderr)
                return 2
            body = _body(args.body)
            if not body.strip():
                print("answer body is empty", file=sys.stderr)
                return 2
            sender = args.sender or agent
            mid = broker.send_message(
                run_id, sender, str(original["sender"]), body, "answer", int(original["id"])
            )
            _print({
                "ok": True, "id": mid, "reply_to": original["id"], "run_id": run_id,
                "from": sender, "to": original["sender"], "kind": "answer",
            })
            return 0

        if args.cmd == "check":
            messages = broker.inbox(run_id, agent, limit=args.limit, peek=False)
            _print({"ok": True, "run_id": run_id, "agent": agent, "count": len(messages),
                    "messages": messages, "non_blocking": True})
            return 0

        if args.cmd == "inbox":
            messages = _collect(broker, run_id, agent, args.limit, args.wait, args.grace, args.peek)
            _print({"ok": True, "run_id": run_id, "agent": agent, "count": len(messages), "messages": messages})
            return 0

        if args.cmd == "history":
            _print({"ok": True, "run_id": run_id, "messages": broker.message_history(run_id, args.limit)})
            return 0

        if args.cmd == "agents":
            _print({"ok": True, "run_id": run_id, "you": agent, "agents": broker.participants(run_id)})
            return 0
    finally:
        broker.close()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(comm_main(sys.argv[1:]))
