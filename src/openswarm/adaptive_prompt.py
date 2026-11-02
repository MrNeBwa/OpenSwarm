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

    lines: list[str] = ["\n## Адаптивный фокус ассистента:"]

    if stack["summary"]:
        lines.append(f"- **Контекст рабочего проекта**: {stack['summary']}. Используй идиоматические паттерны и соглашения этого стека.")

    if intent == "debug":
        lines.extend([
            "- **Режим отладки (Debug & Fix)**:",
            "  1. Сначала четко сформулируй корневую причину (Root Cause), почему возникла проблема.",
            "  2. Предложи минимальное, точечное исправление без регрессий.",
            "  3. Приведи понятный diff или готовый фрагмент кода.",
            "  4. Напиши, как проверить решение (команда запуска тестов или сценарий проверки).",
        ])
    elif intent == "code":
        lines.extend([
            "- **Режим разработки кода (Clean Code)**:",
            "  1. Пиши надежный, готовый к продакшену код с обработкой краевых случаев (edge cases).",
            "  2. Обязательно указывай язык в блоках кода (```python, ```typescript, ```bash и т.д.).",
            "  3. При создании артефактов или крупных файлов выделяй их в чистые самодостаточные блоки.",
        ])
    elif intent == "architecture":
        lines.extend([
            "- **Архитектурный анализ (Architecture & System Design)**:",
            "  1. Структурируй сравнение вариантов (Pros/Cons, производительность, простота поддержки).",
            "  2. Приводи наглядные схемы связей компонентов (например, текстовые диаграммы или Mermaid).",
            "  3. Опиши стратегию миграции или поэтапного внедрения.",
        ])
    elif intent == "review":
        lines.extend([
            "- **Аудит и ревью (Code Review & Security)**:",
            "  1. Раздели замечания по критичности: Critical, Warning, Suggestion.",
            "  2. Обрати особое внимание на безопасность (инъекции, утечки памяти, гонки потоков, невалидные входные данные).",
            "  3. Дай конкретные примеры исправлений.",
        ])
    elif intent == "docs":
        lines.extend([
            "- **Документация и руководство (Clear Documentation)**:",
            "  1. Пиши четким, профессиональным стилем с логической иерархией заголовков (H2/H3).",
            "  2. Включай примеры использования, описание параметров и ссылки на компоненты.",
        ])
    elif intent == "vision":
        lines.extend([
            "- **Анализ визуальных материалов (Vision Analysis)**:",
            "  1. Внимательно изучи прикрепленные изображения или скриншоты.",
            "  2. Опиши структуру интерфейса, графические элементы, схемы данных, тексты и ошибки на экране.",
            "  3. Сопоставь увиденное с кодовой базой и предложи конкретные шаги реализации или исправления.",
        ])

    # Inter-agent communication instructions
    lines.extend([
        "- **Межагентное взаимодействие, вопросы и артефакты**:",
        "  - Если для задачи нужен совет другого специалиста (архитектора, ревьюера, кодера), укажи прямое обращение: `@coder: <сообщение>` или `@architect: <вопрос>`.",
        "  - Если в требованиях пользователя есть критическая неоднозначность, задай вопрос: `<question title=\"...\">Текст вопроса с вариантами выбора</question>`.",
        "  - Для создания интерактивных визуальных компонентов, веб-приложений, SVG-схем или отдельных документов используй тег артефакта: `<artifact identifier=\"id\" title=\"Название\" type=\"html|svg|markdown|code\">содержимое</artifact>`.",
    ])

    return "\n".join(lines) + "\n"
