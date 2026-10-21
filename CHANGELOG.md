# Changelog

## 0.5.0

### Added

- **Non-blocking agent messages** — `openswarm msg ask` sends a question and returns immediately,
  `msg check` collects replies without waiting, and `msg reply <id>` answers the original sender.
  Agents are prompted to check the bus at explicit checkpoints (before shared assumptions, after a
  major step, before finalizing) instead of blocking or busy-polling.
- **Live message viewers** — the TUI has a **💬 Messages** tab and the web UI has a **💬 Messages**
  drawer; both poll the active run and show sender, recipient, kind, reply link and body without
  consuming unread messages. A `run_started` event carries the run id, and
  `GET /api/sessions/<id>/messages?run=&after=` exposes the transcript.

### Fixed

- OpenCode v2 worktree isolation now exports `PWD` (not only `cwd`) so agents run in their worktree.
- Planner JSON with raw newlines inside string values no longer discards the whole plan.
- Late peer replies are collected during a short grace window instead of being reported as missing.

### Tests

- Broker/viewer tests for message lookup, `messages_since`, ask/check/reply CLI flow and concurrent
  senders; web API test proving message viewing does not acknowledge messages. 52 offline tests pass
  (`openswarm selftest`).
