"""SQLite-backed shared context broker: run/task state + notes shared between agents."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id TEXT PRIMARY KEY, mode TEXT, prompt TEXT, status TEXT, created REAL, finished REAL);
CREATE TABLE IF NOT EXISTS tasks(
    id TEXT PRIMARY KEY, run_id TEXT, title TEXT, prompt TEXT, role TEXT, status TEXT,
    branch TEXT, worktree TEXT, deps TEXT, result TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT, author TEXT,
    topic TEXT, content TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, sender TEXT, recipient TEXT,
    kind TEXT, body TEXT, reply_to INTEGER, created REAL, read_at REAL, read_by TEXT);
CREATE INDEX IF NOT EXISTS idx_messages_inbox ON messages(run_id, recipient, id);
"""

MAX_MESSAGE_BYTES = 64 * 1024


class Broker:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path.resolve()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ runs
    def create_run(self, mode: str, prompt: str) -> str:
        run_id = "r" + uuid.uuid4().hex[:6]
        self.db.execute(
            "INSERT INTO runs(id, mode, prompt, status, created) VALUES (?,?,?,?,?)",
            (run_id, mode, prompt, "running", time.time()),
        )
        self.db.commit()
        return run_id

    def finish_run(self, run_id: str, status: str) -> None:
        self.db.execute("UPDATE runs SET status=?, finished=? WHERE id=?", (status, time.time(), run_id))
        self.db.commit()

    # ----------------------------------------------------------------- tasks
    def add_task(
        self, run_id: str, task_id: str, title: str, prompt: str, role: str, deps: list[str] | None = None
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tasks(id, run_id, title, prompt, role, status, deps, updated) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (task_id, run_id, title, prompt, role, "idle", json.dumps(deps or []), time.time()),
        )
        self.db.commit()

    def update_task(self, task_id: str, **fields: str) -> None:
        allowed = {"status", "branch", "worktree", "result"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown task fields: {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(
            f"UPDATE tasks SET {cols}, updated=? WHERE id=?", (*fields.values(), time.time(), task_id)
        )
        self.db.commit()

    def get_tasks(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM tasks WHERE run_id=? ORDER BY updated", (run_id,)))

    # ----------------------------------------------------------------- notes
    def add_note(self, run_id: str, author: str, topic: str, content: str, task_id: str = "") -> None:
        self.db.execute(
            "INSERT INTO notes(run_id, task_id, author, topic, content, ts) VALUES (?,?,?,?,?,?)",
            (run_id, task_id, author, topic, content, time.time()),
        )
        self.db.commit()

    def get_notes(self, run_id: str, topic: str | None = None) -> list[sqlite3.Row]:
        if topic:
            q, args = "SELECT * FROM notes WHERE run_id=? AND topic=? ORDER BY id", (run_id, topic)
        else:
            q, args = "SELECT * FROM notes WHERE run_id=? ORDER BY id", (run_id,)
        return list(self.db.execute(q, args))

    def latest_note(self, run_id: str, topic: str) -> str:
        row = self.db.execute(
            "SELECT content FROM notes WHERE run_id=? AND topic=? ORDER BY id DESC LIMIT 1", (run_id, topic)
        ).fetchone()
        return row["content"] if row else ""

    def context_block(self, run_id: str, topics: tuple[str, ...] = ("spec", "result"), limit: int = 6000) -> str:
        """Render shared notes as a prompt section (most recent notes win when truncating)."""
        parts: list[str] = []
        for n in self.get_notes(run_id):
            if n["topic"] in topics:
                parts.append(f"### {n['topic']} from {n['author']}\n{n['content'].strip()}")
        text = "\n\n".join(parts)
        if len(text) > limit:
            text = "...[older notes truncated]...\n" + text[-limit:]
        return text

    # --------------------------------------------------------------- messages
    def send_message(
        self, run_id: str, sender: str, recipient: str, body: str, kind: str = "note", reply_to: int | None = None
    ) -> int:
        """Send a message to one agent, a comma-separated list, or ``*`` (broadcast)."""
        body = (body or "").strip()
        if not body:
            raise ValueError("message body is empty")
        if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError(f"message body exceeds {MAX_MESSAGE_BYTES} bytes")
        recipients = [r.strip() for r in recipient.split(",") if r.strip()]
        if not recipients:
            recipients = ["*"]
        if recipients == ["*"]:
            # Expand broadcast at send time so each participant gets an independent unread row.
            peers = [str(p["id"]) for p in self.participants(run_id) if str(p["id"]) != sender]
            if peers:
                recipients = peers
        last = 0
        for to in recipients:
            cur = self.db.execute(
                "INSERT INTO messages(run_id, sender, recipient, kind, body, reply_to, created) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, sender, to, kind or "note", body, reply_to, time.time()),
            )
            last = int(cur.lastrowid or 0)
        self.db.commit()
        return last

    def inbox(self, run_id: str, agent: str, limit: int = 50, peek: bool = False) -> list[dict]:
        """Atomically claim unread messages addressed to ``agent`` (or to ``*``)."""
        limit = max(1, min(int(limit), 200))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute(
                "SELECT * FROM messages WHERE run_id=? AND (recipient=? OR recipient='*') "
                "AND sender<>? AND read_at IS NULL ORDER BY id LIMIT ?",
                (run_id, agent, agent, limit),
            ).fetchall()
            if rows and not peek:
                ids = [int(r["id"]) for r in rows]
                marks = ",".join("?" * len(ids))
                self.db.execute(
                    f"UPDATE messages SET read_at=?, read_by=? WHERE id IN ({marks})",
                    (time.time(), agent, *ids),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return [dict(r) for r in rows]

    def get_message(self, run_id: str, message_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM messages WHERE run_id=? AND id=?", (run_id, int(message_id))
        ).fetchone()
        return dict(row) if row else None

    def messages_since(self, run_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        """All messages in a run newer than ``after`` (read-only; never acknowledges anything)."""
        limit = max(1, min(int(limit), 1000))
        rows = self.db.execute(
            "SELECT * FROM messages WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
            (run_id, max(0, int(after)), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def message_history(self, run_id: str, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        rows = self.db.execute(
            "SELECT * FROM messages WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def participants(self, run_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, title, status FROM tasks WHERE run_id=? ORDER BY updated", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_run_id(self) -> str:
        row = self.db.execute("SELECT id FROM runs ORDER BY created DESC LIMIT 1").fetchone()
        return str(row["id"]) if row else ""

