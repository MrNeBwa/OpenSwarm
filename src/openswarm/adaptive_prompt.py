"""Dynamic & Adaptive Instruction Engine for OpenSwarm.

Automatically inspects workspace files to detect technology stacks, classifies user intent,
and generates contextual, high-precision guidance so models perform better without requiring
the user to do manual prompt engineering.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("openswarm.adaptive_prompt")


def detect_workspace_tech_stack(workdir: Path | str | None) -> dict[str, Any]:
    """Inspects files in the given directory to detect primary languages, frameworks and build tools."""
    if not workdir:
        return {"languages": [], "frameworks": [], "tools": [], "summary": ""}

    p = Path(workdir)
    if not p.is_dir():
        return {"languages": [], "frameworks": [], "tools": [], "summary": ""}

    languages: list[str] = []
    frameworks: list[str] = []
    tools: list[str] = []

    try:
        # Python
        if (p / "pyproject.toml").is_file() or (p / "requirements.txt").is_file() or (p / "setup.py").is_file():
            languages.append("Python")
            content = ""
            for fname in ("pyproject.toml", "requirements.txt"):
                f = p / fname
                if f.is_file():
                    try:
                        content += f.read_text(encoding="utf-8", errors="ignore")[:4000] + "\n"
                    except OSError:
                        pass
            if re.search(r"fastapi", content, re.I):
                frameworks.append("FastAPI")
            if re.search(r"django", content, re.I):
                frameworks.append("Django")
            if re.search(r"flask", content, re.I):
                frameworks.append("Flask")
            if re.search(r"pytest", content, re.I):
                tools.append("pytest")

        # Node / JS / TS
        pkg_json = p / "package.json"
        if pkg_json.is_file():
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8", errors="ignore")[:8000])
                all_deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                if (p / "tsconfig.json").is_file() or "typescript" in all_deps:
                    languages.append("TypeScript")
                else:
                    languages.append("JavaScript")

                if "react" in all_deps or "react-dom" in all_deps:
                    frameworks.append("React")
                if "vue" in all_deps:
                    frameworks.append("Vue")
                if "next" in all_deps:
                    frameworks.append("Next.js")
                if "svelte" in all_deps:
                    frameworks.append("Svelte")
                if "express" in all_deps:
                    frameworks.append("Express")
                if "vite" in all_deps:
                    tools.append("Vite")
                if "vitest" in all_deps or "jest" in all_deps:
                    tools.append("Jest/Vitest")
            except Exception:
                languages.append("JavaScript")

        # Rust
        if (p / "Cargo.toml").is_file():
            languages.append("Rust")
            tools.append("Cargo")

        # Go
        if (p / "go.mod").is_file():
            languages.append("Go")

        # .NET / C#
        if list(p.glob("*.csproj")) or list(p.glob("*.sln")):
            languages.append("C# (.NET)")
            frameworks.append(".NET 8/9")

        # Git & Containers
        if (p / ".git").is_dir():
            tools.append("Git")
        if (p / "Dockerfile").is_file() or (p / "docker-compose.yml").is_file():
            tools.append("Docker")

    except Exception as exc:
        logger.debug("Error analyzing workspace stack: %s", exc)

    summary_parts = []
    if languages:
        summary_parts.append(f"Языки: {', '.join(languages)}")
    if frameworks:
        summary_parts.append(f"Фреймворки: {', '.join(frameworks)}")
    if tools:
        summary_parts.append(f"Инструменты: {', '.join(tools)}")

    return {
        "languages": languages,
        "frameworks": frameworks,
        "tools": tools,
        "summary": " · ".join(summary_parts),
    }


def classify_user_intent(prompt: str, has_images: bool = False) -> str:
    """Classifies user query into a primary intent category."""
    if has_images:
        return "vision"

    p = prompt.lower()

    if re.search(r"\b(image|screenshot|photo|picture|look at|diagram|картинк|скриншот|изображен|рисунок|схем)\b", p):
        return "vision"

    if re.search(r"\b(error|exception|fail|traceback|crash|bug|fix|not working|broken|debug|ошибк|баг|упал|почини|исправь|не работает|исключени|сбой)\b", p):
        return "debug"

    if re.search(r"\b(architecture|design|db|database|schema|compare|trade-offs?|pros and cons|архитектур|спроектируй|баз[аы] данных|сравни|плюсы и минусы)\b", p):
        return "architecture"

    if re.search(r"\b(review|audit|vulnerability|security|lint|ревью|проверь|аудит|уязвимост|код-ревью)\b", p):
        return "review"

    if re.search(r"\b(docs|document|readme|spec|article|explain|writeup|guide|документаци|инструкци|описани|стать|руководств|поясни)\b", p):
        return "docs"

    if re.search(r"\b(def |function|class|code|script|component|api|endpoint|sql|implement|add|create|route|напиши код|создай функцию|реализуй|скрипт|компонент)\b", p):
        return "code"

    return "chat"


def get_adaptive_instructions(
    arg1: Path | str,
    arg2: Path | str | None = None,
    focus_override: str = "auto",
    has_images: bool = False,
    role: str = "coder",
    focus: str | None = None,
) -> str:
    """Generates concise, context-aware prompt guidance based on intent and tech stack.
    Accepts either (prompt, workdir) or (workdir, prompt) for convenience."""
    if isinstance(arg1, Path) or (isinstance(arg1, str) and (Path(arg1).is_dir() or ("/" in arg1 and " " not in arg1))):
        workdir = arg1
        prompt = str(arg2 or "")
    else:
        prompt = str(arg1 or "")
        workdir = arg2

    actual_focus = focus or focus_override
    intent = actual_focus if actual_focus and actual_focus != "auto" else classify_user_intent(prompt, has_images)
    stack = detect_workspace_tech_stack(workdir)

    if role == "chat" and intent == "chat":
        # Ultra-lightweight for simple conversational turns so models don't produce huge walls of text
        if stack["summary"]:
            return f"\n[Контекст проекта: {stack['summary']}. Отвечай кратко, по делу и без вывода внутренних рассуждений.]\n"
        return "\n[Отвечай кратко, чётко и по делу, без лишней воды и без вывода внутренних рассуждений.]\n"

    lines: list[str] = ["\n## Адаптивный фокус:"]

    if stack["summary"]:
        lines.append(f"- **Стек проекта**: {stack['summary']}.")

    if intent == "debug":
        lines.append("- **Отладка**: Укажи корневую причину (Root Cause), дай минимальное точечное исправление и команду проверки.")
    elif intent == "code":
        lines.append("- **Код**: Пиши чистый рабочий код в блоках с указанием языка (```python, ```bash и т.д.) без лишней воды.")
    elif intent == "architecture":
        lines.append("- **Архитектура**: Кратко сравни варианты (плюсы/минусы) и предложи оптимальную схему компонентов.")
    elif intent == "review":
        lines.append("- **Ревью**: Выдели только реальные проблемы по критичности (Critical / Warning) с примером исправления.")
    elif intent == "docs":
        lines.append("- **Документация**: Пиши структурированно и лаконично с примерами использования.")
    elif intent == "vision":
        lines.append("- **Визуальный анализ**: Опиши ключевые элементы/ошибки на изображении и предложи решение.")

    if role != "chat":
        lines.append(
            "- **Координация**: Для вопроса коллеге пиши `@coder: ...` / `@architect: ...`; для интерактивных виджетов используй `<artifact identifier=\"id\" title=\"...\" type=\"html\">...</artifact>`."
        )

    return "\n".join(lines) + "\n"
