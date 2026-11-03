"""Desktop App Automation & Computer Use Agent for OpenSwarm.

Enables OpenSwarm to interact with desktop applications on the user's behalf:
- Discover open windows, titles, classes, and geometry (Hyprland, Sway, X11/wmctrl)
- Focus any application window
- Type/paste text and code directly into target applications (wl-copy/xclip + sendshortcut/wtype/xdotool)
- Send keyboard shortcuts (Ctrl+S, Ctrl+V, Return, etc.)
- Capture screenshots of specific application windows or full screen (grim/import)
- Launch desktop applications or URLs (xdg-open, gtk-launch, hyprctl)
- Confirmation queue (`require_confirmation=True` by default) so the user stays in full control.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("openswarm.desktop_agent")


@dataclass
class DesktopAction:
    id: str
    action: str  # focus_window | type_text | send_shortcut | clipboard_set | launch_app | move_cursor | capture_window
    target: str = ""  # window class, address, title, or app name
    value: str = ""  # text to type, shortcut keys (e.g. "CTRL,S"), coordinates "x,y", or URL
    description: str = ""
    status: str = "pending_confirmation"  # pending_confirmation | executed | failed | rejected
    created_at: float = field(default_factory=time.time)
    result: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.action
        d["target_window"] = self.target
        d["text"] = self.value
        return d


class DesktopAgentManager:
    """Manages desktop application inspection and confirmation-gated automation actions."""

    def __init__(self, workdir: Path | None = None) -> None:
        self.workdir = workdir or Path.cwd()
        self.require_confirmation: bool = True
        self._pending: dict[str, DesktopAction] = {}
        self._history: list[dict[str, Any]] = []

    def capabilities(self) -> dict[str, Any]:
        """Detects available window manager and input automation utilities."""
        return {
            "hyprctl": bool(shutil.which("hyprctl")),
            "swaymsg": bool(shutil.which("swaymsg")),
            "wmctrl": bool(shutil.which("wmctrl")),
            "xdotool": bool(shutil.which("xdotool")),
            "wtype": bool(shutil.which("wtype")),
            "ydotool": bool(shutil.which("ydotool")),
            "wl_copy": bool(shutil.which("wl-copy")),
            "wl_paste": bool(shutil.which("wl-paste")),
            "xclip": bool(shutil.which("xclip")),
            "grim": bool(shutil.which("grim")),
            "xdg_open": bool(shutil.which("xdg-open")),
        }

    def list_windows(self) -> list[dict[str, Any]]:
        """Lists open desktop application windows across Hyprland, Sway, or X11."""
        windows: list[dict[str, Any]] = []
        if shutil.which("hyprctl"):
            try:
                r = subprocess.run(
                    ["hyprctl", "clients", "-j"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
                if r.returncode == 0 and r.stdout.strip():
                    clients = json.loads(r.stdout)
                    for c in clients:
                        title = c.get("title") or ""
                        cls = c.get("class") or c.get("initialClass") or ""
                        if not title and not cls:
                            continue
                        at = c.get("at") or [0, 0]
                        sz = c.get("size") or [0, 0]
                        ws = (c.get("workspace") or {}).get("name") or str((c.get("workspace") or {}).get("id", ""))
                        windows.append({
                            "id": c.get("address", ""),
                            "address": c.get("address", ""),
                            "class": cls,
                            "app_class": cls,
                            "title": title,
                            "workspace": ws,
                            "pid": c.get("pid", 0),
                            "floating": bool(c.get("floating", False)),
                            "pinned": bool(c.get("pinned", False)),
                            "geometry": {"x": at[0], "y": at[1], "width": sz[0], "height": sz[1]},
                        })
                    return windows
            except Exception as exc:
                logger.debug("hyprctl clients failed: %s", exc)

        if shutil.which("wmctrl"):
            try:
                r = subprocess.run(["wmctrl", "-l", "-G"], capture_output=True, text=True, timeout=2.0)
                if r.returncode == 0:
                    for line in r.stdout.splitlines():
                        parts = line.split(None, 7)
                        if len(parts) >= 8:
                            windows.append({
                                "id": parts[0],
                                "class": parts[6],
                                "title": parts[7],
                                "workspace": parts[1],
                                "geometry": {
                                    "x": int(parts[2]),
                                    "y": int(parts[3]),
                                    "width": int(parts[4]),
                                    "height": int(parts[5]),
                                },
                            })
                    return windows
            except Exception:
                pass

        return windows

    def get_active_window(self) -> dict[str, Any] | None:
        """Returns the currently focused desktop window."""
        if shutil.which("hyprctl"):
            try:
                r = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=1.5)
                if r.returncode == 0 and r.stdout.strip():
                    c = json.loads(r.stdout)
                    if c and (c.get("address") or c.get("class")):
                        return {
                            "id": c.get("address", ""),
                            "address": c.get("address", ""),
                            "class": c.get("class", ""),
                            "app_class": c.get("class", ""),
                            "title": c.get("title", ""),
                            "pid": c.get("pid", 0),
                        }
            except Exception:
                pass
        return None

    def get_clipboard(self) -> str:
        """Reads text from the system clipboard."""
        if shutil.which("wl-paste"):
            try:
                r = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=1.5)
                if r.returncode == 0:
                    return r.stdout[:10000]
            except Exception:
                pass
        if shutil.which("xclip"):
            try:
                r = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=1.5)
                if r.returncode == 0:
                    return r.stdout[:10000]
            except Exception:
                pass
        return ""

    def set_clipboard(self, text: str) -> bool:
        """Writes text to the system clipboard."""
        if shutil.which("wl-copy"):
            try:
                r = subprocess.run(["wl-copy"], input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        if shutil.which("xclip"):
            try:
                r = subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    def queue_action(
        self,
        action: str,
        target: str = "",
        value: str = "",
        description: str = "",
    ) -> DesktopAction:
        act_id = f"desk_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        desc = description or self._describe_action(action, target, value)
        item = DesktopAction(
            id=act_id,
            action=action,
            target=target,
            value=value,
            description=desc,
            status="pending_confirmation",
        )
        self._pending[act_id] = item
        return item

    def _describe_action(self, action: str, target: str, value: str) -> str:
        if action == "focus_window":
            return f"Переключить фокус на окно «{target}»"
        if action == "type_text":
            preview = (value[:60] + "…") if len(value) > 60 else value
            return f"Ввести/вставить текст «{preview}» в «{target or 'активное окно'}»"
        if action == "send_shortcut":
            return f"Отправить горячую клавишу [{value}] в «{target or 'активное окно'}»"
        if action == "clipboard_set":
            preview = (value[:60] + "…") if len(value) > 60 else value
            return f"Скопировать в буфер обмена: «{preview}»"
        if action == "launch_app":
            return f"Запустить приложение / открыть: «{target or value}»"
        if action == "move_cursor":
            return f"Переместить курсор в координаты ({value})"
        if action == "capture_window":
            return f"Сделать снимок окна «{target or 'весь экран'}»"
        return f"{action}: {target} {value}".strip()

    def list_pending(self) -> list[dict[str, Any]]:
        return [a.to_dict() for a in sorted(self._pending.values(), key=lambda x: x.created_at, reverse=True)]

    def list_history(self, limit: int = 25) -> list[dict[str, Any]]:
        return self._history[-limit:][::-1]

    def reject_action(self, action_id: str) -> dict[str, Any]:
        act = self._pending.pop(action_id, None)
        if not act:
            return {"ok": False, "error": "Действие не найдено"}
        act.status = "rejected"
        self._history.append(act.to_dict())
        return {"ok": True, "action": act.to_dict()}

    def _normalize_action_kind(self, raw_kind: str) -> str:
        mapping = {
            "paste_text": "type_text",
            "copy_clipboard": "clipboard_set",
            "screenshot_window": "capture_window",
        }
        return mapping.get(raw_kind, raw_kind)

    def execute_action(
        self,
        action: str = "",
        target: str = "",
        value: str = "",
        confirmed: bool = False,
        description: str = "",
        *,
        kind: str = "",
        target_window: str = "",
        text: str = "",
        shortcut: str = "",
    ) -> dict[str, Any]:
        """Executes a desktop action if confirmed (or if require_confirmation is False); otherwise queues it."""
        eff_action = self._normalize_action_kind(action or kind or "type_text")
        eff_target = target or target_window
        eff_value = value or shortcut or text
        if self.require_confirmation and not confirmed:
            queued = self.queue_action(action=eff_action, target=eff_target, value=eff_value, description=description)
            return {
                "ok": False,
                "executed": False,
                "requires_confirmation": True,
                "action": queued.to_dict(),
                "error": "Действие ожидает подтверждения пользователя",
            }

        item = DesktopAction(
            id=f"desk_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            action=eff_action,
            target=eff_target,
            value=eff_value,
            description=description or self._describe_action(eff_action, eff_target, eff_value),
            status="executed",
        )
        ok, msg = self._run_primitive(eff_action, eff_target, eff_value)
        item.status = "executed" if ok else "failed"
        item.result = msg
        self._history.append(item.to_dict())
        return {"ok": ok, "executed": ok, "action": item.to_dict(), "message": msg, "path": msg if eff_action == "capture_window" else ""}

    def approve_action(self, action_id: str) -> dict[str, Any]:
        item = self._pending.pop(action_id, None)
        if not item:
            raise ValueError(f"Действие {action_id} не найдено в очереди ожидания")
        ok, msg = self._run_primitive(item.action, item.target, item.value)
        item.status = "executed" if ok else "failed"
        item.result = msg
        self._history.append(item.to_dict())
        return {"ok": ok, "executed": ok, "action": item.to_dict(), "message": msg}

    def _resolve_hypr_selector(self, target: str) -> str:
        t = (target or "").strip()
        if not t:
            return ""
        if t.startswith(("address:", "class:", "title:", "pid:")):
            return t
        if t.startswith("0x"):
            return f"address:{t}"
        # Check if matches a known window class
        for w in self.list_windows():
            if w.get("id") == t:
                return f"address:{t}"
            if w.get("class") and w["class"].lower() == t.lower():
                return f"class:^{re.escape(w['class'])}$"
            if w.get("title") and t.lower() in w["title"].lower():
                return f"address:{w['id']}" if w.get("id") else f"title:{t}"
        return f"class:{t}"

    def _run_primitive(self, action: str, target: str, value: str) -> tuple[bool, str]:
        try:
            if action == "clipboard_set":
                ok = self.set_clipboard(value)
                return (ok, "Текст скопирован в системный буфер обмена" if ok else "Не удалось записать в буфер обмена")

            if action == "focus_window":
                if shutil.which("hyprctl"):
                    sel = self._resolve_hypr_selector(target)
                    r = subprocess.run(["hyprctl", "dispatch", "focuswindow", sel], capture_output=True, text=True, timeout=2.0)
                    return (r.returncode == 0, f"Фокус переключён на {sel}")
                if shutil.which("wmctrl"):
                    r = subprocess.run(["wmctrl", "-a", target], capture_output=True, text=True, timeout=2.0)
                    return (r.returncode == 0, f"Фокус переключён на {target}")
                return (False, "Оконный менеджер не поддерживает переключение фокуса")

            if action == "type_text":
                # Focus target window first if specified
                if target:
                    self._run_primitive("focus_window", target, "")
                    time.sleep(0.15)
                # Copy text to clipboard and paste, or use wtype/xdotool
                if self.set_clipboard(value):
                    if shutil.which("hyprctl"):
                        sel = self._resolve_hypr_selector(target) if target else ""
                        shortcut_arg = f"CTRL, V, {sel}" if sel else "CTRL, V,"
                        r = subprocess.run(["hyprctl", "dispatch", "sendshortcut", shortcut_arg], capture_output=True, text=True, timeout=2.0)
                        if r.returncode == 0:
                            return (True, "Текст скопирован в буфер и вставлен в окно (Ctrl+V)")
                    return (True, "Текст помещён в буфер обмена (нажмите Ctrl+V в целевом окне)")
                if shutil.which("wtype"):
                    r = subprocess.run(["wtype", value], capture_output=True, text=True, timeout=3.0)
                    return (r.returncode == 0, "Текст введён через wtype")
                if shutil.which("xdotool"):
                    r = subprocess.run(["xdotool", "type", "--clearmodifiers", value], capture_output=True, text=True, timeout=3.0)
                    return (r.returncode == 0, "Текст введён через xdotool")
                return (False, "Не найдены утилиты ввода (wl-copy/wtype/xdotool)")

            if action == "send_shortcut":
                # Format: e.g. "CTRL+S" or "CTRL, S"
                raw = value.replace("+", ",").upper()
                parts = [p.strip() for p in raw.split(",") if p.strip()]
                mod = parts[0] if len(parts) > 1 else ""
                key = parts[-1] if parts else "RETURN"
                if shutil.which("hyprctl"):
                    sel = self._resolve_hypr_selector(target) if target else ""
                    arg = f"{mod}, {key}, {sel}" if sel else f"{mod}, {key},"
                    r = subprocess.run(["hyprctl", "dispatch", "sendshortcut", arg], capture_output=True, text=True, timeout=2.0)
                    return (r.returncode == 0, f"Отправлено сочетание клавиш {mod}+{key}")
                if shutil.which("xdotool"):
                    xkey = value.replace(",", "+")
                    r = subprocess.run(["xdotool", "key", xkey], capture_output=True, text=True, timeout=2.0)
                    return (r.returncode == 0, f"Отправлено сочетание клавиш {xkey}")
                return (False, "Не удалось отправить сочетание клавиш")

            if action == "move_cursor":
                coords = [c.strip() for c in value.replace(";", ",").split(",") if c.strip()]
                if len(coords) >= 2 and shutil.which("hyprctl"):
                    x, y = int(float(coords[0])), int(float(coords[1]))
                    r = subprocess.run(["hyprctl", "dispatch", "movecursor", str(x), str(y)], capture_output=True, text=True, timeout=2.0)
                    return (r.returncode == 0, f"Курсор перемещён в ({x}, {y})")
                return (False, "Укажите координаты X, Y")

            if action == "launch_app":
                app_target = (target or value).strip()
                if not app_target:
                    return (False, "Не указано приложение или URL")
                if app_target.startswith(("http://", "https://")) and shutil.which("xdg-open"):
                    subprocess.Popen(["xdg-open", app_target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return (True, f"Открыта ссылка {app_target}")
                if shutil.which("hyprctl"):
                    r = subprocess.run(["hyprctl", "dispatch", "exec", app_target], capture_output=True, text=True, timeout=2.0)
                    return (r.returncode == 0, f"Запущено приложение: {app_target}")
                if shutil.which("gtk-launch"):
                    subprocess.Popen(["gtk-launch", app_target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return (True, f"Запущено приложение: {app_target}")
                return (False, f"Не удалось запустить {app_target}")

            if action == "capture_window":
                sdir = self.workdir / ".swarm" / "screenshots"
                sdir.mkdir(parents=True, exist_ok=True)
                spath = sdir / f"win_{int(time.time())}.png"
                geom = None
                if target:
                    for w in self.list_windows():
                        if w.get("id") == target or (w.get("class") and w["class"].lower() == target.lower()):
                            g = w.get("geometry") or {}
                            if g.get("width", 0) > 0 and g.get("height", 0) > 0:
                                geom = f"{g['x']},{g['y']} {g['width']}x{g['height']}"
                            break
                if shutil.which("grim"):
                    cmd = ["grim", "-g", geom, str(spath)] if geom else ["grim", str(spath)]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3.0)
                    if r.returncode == 0 and spath.is_file():
                        rel = str(spath.relative_to(self.workdir)) if spath.is_relative_to(self.workdir) else str(spath)
                        return (True, rel)
                return (False, "Не удалось сделать снимок окна")

            return (False, f"Неизвестное действие: {action}")
        except Exception as exc:
            return (False, f"Ошибка выполнения: {exc}")

    async def plan_actions_for_goal(self, goal: str, runner_fn: Any = None, target_window: str = "") -> list[dict[str, Any]]:
        """Plans a safe sequence of desktop actions for a user's natural language goal and queues them."""
        wins = self.list_windows()
        win_summary = "\n".join(
            f"- id={w['id']} class={w['class']} title={w['title']!r}" for w in wins[:25]
        ) or "(нет открытых окон)"

        prompt = (
            "Ты — Desktop Automation Agent в системе OpenSwarm.\n"
            f"Открытые окна пользователя:\n{win_summary}\n\n"
            f"Задача пользователя: {goal}\n"
            + (f"Целевое окно: {target_window}\n\n" if target_window else "\n")
            + "Составь список шагов (1–4 действия) в формате JSON-массива объектов:\n"
            '[{"action": "focus_window"|"type_text"|"send_shortcut"|"clipboard_set"|"launch_app", '
            '"target": "class или address окна", "value": "текст или клавиши", "description": "понятное описание на русском"}]\n'
            "Выведи ТОЛЬКО валидный JSON-массив."
        )

        planned: list[dict[str, Any]] = []
        if runner_fn:
            try:
                raw = await runner_fn(prompt)
                m = re.search(r"\[[\s\S]*\]", raw)
                if m:
                    arr = json.loads(m.group(0))
                    if isinstance(arr, list):
                        for item in arr[:5]:
                            if isinstance(item, dict) and item.get("action"):
                                act = self.queue_action(
                                    action=self._normalize_action_kind(str(item.get("action"))),
                                    target=str(item.get("target") or target_window or ""),
                                    value=str(item.get("value") or ""),
                                    description=str(item.get("description") or ""),
                                )
                                planned.append(act.to_dict())
            except Exception as exc:
                logger.warning("Failed to parse AI desktop plan: %s", exc)

        if not planned:
            # Deterministic fallback plan based on goal keywords
            target_win = target_window or (wins[0]["class"] if wins else "")
            if not target_window:
                for w in wins:
                    if w["class"] and w["class"].lower() in goal.lower():
                        target_win = w["class"]
                        break
            if target_win:
                f_act = self.queue_action(
                    action="focus_window",
                    target=target_win,
                    value="",
                    description=f"Переключить фокус на окно «{target_win}»",
                )
                planned.append(f_act.to_dict())
            # Extract quoted text if present
            qm = re.search(r'["«](.+?)["»]', goal)
            txt_val = qm.group(1) if qm else goal
            act = self.queue_action(
                action="type_text" if target_win else "clipboard_set",
                target=target_win,
                value=txt_val,
                description=f"Вставить текст в «{target_win or 'буфер обмена'}»: «{txt_val[:60]}»",
            )
            planned.append(act.to_dict())
            if any(k in goal.lower() for k in ("сохрани", "save", "ctrl+s")):
                s_act = self.queue_action(
                    action="send_shortcut",
                    target=target_win,
                    value="CTRL+S",
                    description="Сохранить файл (Ctrl+S)",
                )
                planned.append(s_act.to_dict())

        return planned

    def status(self) -> dict[str, Any]:
        pend = self.list_pending()
        return {
            "ok": True,
            "require_confirmation": self.require_confirmation,
            "capabilities": self.capabilities(),
            "active_window": self.get_active_window(),
            "windows": self.list_windows(),
            "pending": pend,
            "pending_actions": pend,
            "history": self.list_history(),
        }


_GLOBAL_DESKTOP_AGENT: DesktopAgentManager | None = None


def get_desktop_agent(workdir: Path | None = None) -> DesktopAgentManager:
    global _GLOBAL_DESKTOP_AGENT
    if _GLOBAL_DESKTOP_AGENT is None:
        _GLOBAL_DESKTOP_AGENT = DesktopAgentManager(workdir=workdir)
    elif workdir is not None:
        _GLOBAL_DESKTOP_AGENT.workdir = workdir
    return _GLOBAL_DESKTOP_AGENT
