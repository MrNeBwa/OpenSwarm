"""Turnstone Command Line Interface (CLI).

Interactive terminal researcher, one-shot deep query runner, and Brain manager.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from .brain import TurnstoneBrain
from .engine import TurnstoneEngine


def print_banner() -> None:
    print("\033[1;32m" + "🪨🦅 Turnstone — Autonomous Research Teammate (Powered by OpenCode)" + "\033[0m")
    print("\033[2m" + "Type your query or topic. Type 'exit' to quit. Prefix with 'deep: ' for Deep Research.\n" + "\033[0m")


async def run_single_inquiry(prompt: str, deep: bool = False, model: str = "") -> None:
    engine = TurnstoneEngine(model=model)
    print(f"\033[1;36m[Turnstone]\033[0m Investigating: \033[1m{prompt}\033[0m\n")

    async for ev in engine.run(prompt, deep_research=deep, model=model):
        if ev.type == "thought":
            print(f"\033[2m🧠 {ev.content}\033[0m")
        elif ev.type == "tool_start":
            print(f"\033[33m🔧 {ev.content}\033[0m")
        elif ev.type == "tool_end":
            print(f"\033[32m✔ {ev.content}\033[0m")
        elif ev.type == "research_plan":
            print(f"\033[1;33m📋 {ev.content}\033[0m")
        elif ev.type == "message":
            print(f"\n\033[1;37m{ev.content}\033[0m\n")
        elif ev.type == "error":
            print(f"\033[1;31m❌ {ev.content}\033[0m")


async def interactive_chat(model: str = "") -> None:
    print_banner()
    engine = TurnstoneEngine(model=model)
    history: list[dict[str, str]] = []

    while True:
        try:
            prompt = input("\033[1;32mTurnstone ❯ \033[0m").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break

        if not prompt:
            continue
        if prompt.lower() in ("exit", "quit", "q"):
            print("Goodbye!")
            break

        deep_mode = False
        if prompt.lower().startswith("deep:"):
            deep_mode = True
            prompt = prompt[5:].strip()

        print()
        response_text = ""
        async for ev in engine.run(prompt, history=history, deep_research=deep_mode, model=model):
            if ev.type == "thought":
                print(f"\033[2m🧠 {ev.content}\033[0m")
            elif ev.type == "tool_start":
                print(f"\033[33m🔧 {ev.content}\033[0m")
            elif ev.type == "tool_end":
                print(f"\033[32m✔ {ev.content}\033[0m")
            elif ev.type == "research_plan":
                print(f"\033[1;33m📋 {ev.content}\033[0m")
            elif ev.type == "message":
                response_text = ev.content
                print(f"\n\033[1;37m{ev.content}\033[0m\n")
            elif ev.type == "error":
                print(f"\033[1;31m❌ {ev.content}\033[0m")

        if response_text:
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": response_text})


def manage_brain(args: list[str]) -> int:
    brain = TurnstoneBrain()
    cmd = args[0] if args else "list"

    if cmd == "list":
        items = brain.list_memories()
        print(f"🧠 Turnstone Brain ({len(items)} memories stored):\n")
        for m in items:
            tags = " ".join(f"#{t}" for t in m.get("tags", []))
            print(f"[{m.get('id')}] \033[1;32m{m.get('category')}\033[0m: {m.get('content')} \033[2m{tags}\033[0m")
        return 0

    if cmd == "remember" and len(args) > 1:
        fact = " ".join(args[1:])
        item = brain.remember(fact)
        print(f"✔ Saved to Brain: [{item.id}] {item.content}")
        return 0

    if cmd == "forget" and len(args) > 1:
        m_id = args[1]
        ok = brain.forget(m_id)
        print(f"{'✔ Forgotten' if ok else '❌ Not found'}: {m_id}")
        return 0 if ok else 1

    if cmd in ("recall", "search") and len(args) > 1:
        q = " ".join(args[1:])
        recalled = brain.recall(q)
        print(f"Recalled {len(recalled)} memories for '{q}':\n")
        for m in recalled:
            print(f"• [{m.get('category')}] {m.get('content')}")
        return 0

    print("Usage: turnstone brain [list | remember <text> | forget <id> | recall <query>]")
    return 1
