"""Telegram User Account Integration for OpenSwarm.

Allows connecting a user's personal Telegram account via Telethon so OpenSwarm
can read recent messages, understand incoming conversations, and draft/send
intelligent contextual replies as a personal AI assistant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("openswarm.telegram_user")

# Built-in fallback Telegram Desktop credentials (standard public API credentials)
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"


def _session_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / "openswarm" / "telegram"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_file() -> str:
    return str(_session_dir() / "user_session")


@dataclass
class TelegramUserProfile:
    id: int
    first_name: str
    last_name: str = ""
    username: str = ""
    phone: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TelegramUserManager:
    """Manages an active Telethon client session for a personal Telegram account."""

    def __init__(self) -> None:
        self.client: Any = None
        self._phone_code_hash: str = ""
        self._phone: str = ""
        self._api_id: int = DEFAULT_API_ID
        self._api_hash: str = DEFAULT_API_HASH
        self._lock = asyncio.Lock()

    async def _ensure_client(self, api_id: int | None = None, api_hash: str | None = None) -> Any:
        try:
            from telethon import TelegramClient
        except ImportError:
            raise RuntimeError("Библиотека 'telethon' не установлена. Запустите: pip install telethon")

        cur_id = api_id or self._api_id or DEFAULT_API_ID
        cur_hash = api_hash or self._api_hash or DEFAULT_API_HASH
        self._api_id = cur_id
        self._api_hash = cur_hash

        if self.client is None:
            self.client = TelegramClient(_session_file(), cur_id, cur_hash)
            await self.client.connect()
        elif not self.client.is_connected():
            await self.client.connect()

        return self.client

    async def get_status(self) -> dict[str, Any]:
        """Returns whether user account is authorized and basic user profile."""
        try:
            from telethon import TelegramClient
        except ImportError:
            return {"installed": False, "connected": False, "authorized": False, "error": "telethon_missing"}

        sess_path = Path(_session_file() + ".session")
        has_session = sess_path.exists()

        if not has_session and (self.client is None or not self.client.is_connected()):
            return {
                "installed": True,
                "connected": False,
                "authorized": False,
                "active": False,
                "status": "disconnected",
                "phone": self._phone,
                "user": None,
                "profile": None,
            }

        async with self._lock:
            try:
                client = await self._ensure_client()
                is_auth = await client.is_user_authorized()
                if not is_auth:
                    return {
                        "installed": True,
                        "connected": True,
                        "authorized": False,
                        "active": False,
                        "status": "unauthorized",
                        "phone": self._phone,
                        "user": None,
                        "profile": None,
                    }
                me = await client.get_me()
                profile = TelegramUserProfile(
                    id=getattr(me, "id", 0),
                    first_name=getattr(me, "first_name", "") or "",
                    last_name=getattr(me, "last_name", "") or "",
                    username=getattr(me, "username", "") or "",
                    phone=getattr(me, "phone", "") or "",
                )
                p_dict = profile.to_dict()
                return {
                    "installed": True,
                    "connected": True,
                    "authorized": True,
                    "active": True,
                    "status": "connected",
                    "phone": profile.phone or self._phone,
                    "user": p_dict,
                    "profile": p_dict,
                }
            except Exception as exc:
                logger.warning("Error getting Telegram user status: %s", exc)
                return {
                    "installed": True,
                    "connected": False,
                    "authorized": False,
                    "active": False,
                    "status": "error",
                    "error": str(exc),
                    "phone": self._phone,
                    "user": None,
                    "profile": None,
                }

    async def send_code(self, phone: str, api_id: int | None = None, api_hash: str | None = None) -> dict[str, Any]:
        """Sends an authentication code via Telegram/SMS."""
        phone = phone.strip().replace(" ", "").replace("-", "")
        if not phone.startswith("+"):
            phone = "+" + phone

        self._phone = phone
        async with self._lock:
            client = await self._ensure_client(api_id, api_hash)
            res = await client.send_code_request(phone)
            self._phone_code_hash = res.phone_code_hash
            return {
                "ok": True,
                "phone": phone,
                "phone_code_hash": self._phone_code_hash,
                "is_code_via_app": getattr(res, "type", None).__class__.__name__ == "SentCodeTypeApp",
            }

    async def sign_in(self, code: str, password: str | None = None, phone_code_hash: str | None = None) -> dict[str, Any]:
        """Completes Telegram sign in using received code and optional 2FA password."""
        async with self._lock:
            client = await self._ensure_client()
            hash_val = phone_code_hash or self._phone_code_hash
            try:
                await client.sign_in(self._phone, code.strip(), phone_code_hash=hash_val)
            except Exception as exc:
                name = exc.__class__.__name__
                if "SessionPasswordNeeded" in name:
                    if not password:
                        return {"ok": False, "needs_password": True, "error": "2FA пароль обязателен"}
                    await client.sign_in(password=password.strip())
                else:
                    raise exc

            me = await client.get_me()
            return {
                "ok": True,
                "authorized": True,
                "profile": TelegramUserProfile(
                    id=getattr(me, "id", 0),
                    first_name=getattr(me, "first_name", "") or "",
                    last_name=getattr(me, "last_name", "") or "",
                    username=getattr(me, "username", "") or "",
                    phone=getattr(me, "phone", "") or "",
                ).to_dict(),
            }

    async def get_dialogs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Retrieves user's recent dialogs and unread message counters."""
        async with self._lock:
            client = await self._ensure_client()
            if not await client.is_user_authorized():
                raise RuntimeError("Пользователь Telegram не авторизован")

            dialogs = await client.get_dialogs(limit=limit)
            out: list[dict[str, Any]] = []
            for d in dialogs:
                last_msg = ""
                if d.message and getattr(d.message, "message", None):
                    last_msg = d.message.message[:120]
                out.append({
                    "id": d.id,
                    "title": d.title or d.name or str(d.id),
                    "is_user": d.is_user,
                    "is_group": d.is_group,
                    "is_channel": d.is_channel,
                    "unread_count": d.unread_count,
                    "last_message": last_msg,
                    "date": d.date.isoformat() if d.date else "",
                })
            return out

    async def get_messages(self, chat_id: int | str, limit: int = 15) -> list[dict[str, Any]]:
        """Fetches recent conversation history with contact or group."""
        async with self._lock:
            client = await self._ensure_client()
            if not await client.is_user_authorized():
                raise RuntimeError("Пользователь Telegram не авторизован")

            entity = await client.get_entity(int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id)
            messages = await client.get_messages(entity, limit=limit)
            out: list[dict[str, Any]] = []
            for m in reversed(messages):
                text = getattr(m, "message", "") or ""
                if not text:
                    continue
                out.append({
                    "id": m.id,
                    "sender_id": getattr(m, "sender_id", 0),
                    "out": bool(getattr(m, "out", False)),
                    "text": text,
                    "date": m.date.isoformat() if m.date else "",
                })
            return out

    async def send_message(self, chat_id: int | str, text: str) -> dict[str, Any]:
        """Sends a text message to chat."""
        async with self._lock:
            client = await self._ensure_client()
            if not await client.is_user_authorized():
                raise RuntimeError("Пользователь Telegram не авторизован")

            entity = await client.get_entity(int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id)
            msg = await client.send_message(entity, text.strip())
            return {"ok": True, "message_id": msg.id}

    async def suggest_reply(self, chat_id: int | str, prompt_hint: str = "", runner_fn: Any = None) -> list[str]:
        """Reads recent conversation and drafts 2-3 contextual replies."""
        msgs = await self.get_messages(chat_id, limit=8)
        if not msgs:
            return ["Привет! Чем могу помочь?", "Здравствуйте! Да, на связи.", "Спасибо, принято!"]

        transcript = []
        for m in msgs:
            who = "Я" if m["out"] else "Собеседник"
            transcript.append(f"{who}: {m['text']}")
        context_str = "\n".join(transcript)

        prompt = (
            f"Ниже приведён недавний диалог в Telegram:\n\n{context_str}\n\n"
            f"Дополнительное указание пользователя: {prompt_hint or 'Предложи естественные, вежливые и уместные ответы'}\n\n"
            "Сгенерируй ровно 3 разных варианта ответа (краткий, развёрнутый и вежливый/деловой). "
            "Выведи только варианты ответов в формате JSON-массива строк, например: [\"Вариант 1\", \"Вариант 2\", \"Вариант 3\"]. "
            "Никакого лишнего текста, только валидный JSON."
        )

        if runner_fn:
            raw = await runner_fn(prompt)
            # Try parsing JSON array
            try:
                match = re.search(r"\[\s*\".*?\"\s*\]", raw, re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
                    if isinstance(parsed, list) and parsed:
                        return [str(x) for x in parsed[:3]]
            except Exception:
                pass
            # Fallback: split lines
            lines = [l.strip().lstrip("123.-*• ") for l in raw.splitlines() if l.strip()]
            if lines:
                return lines[:3]

        return [
            "Здравствуйте! Ознакомился с сообщением, скоро дам подробный ответ.",
            "Привет! Да, всё в порядке, продолжаю работу.",
            "Спасибо за информацию! Если есть вопросы — пишите.",
        ]

    async def logout(self) -> bool:
        """Disconnects and deletes session files."""
        async with self._lock:
            if self.client is not None:
                try:
                    if self.client.is_connected() and await self.client.is_user_authorized():
                        await self.client.log_out()
                    else:
                        await self.client.disconnect()
                except Exception:
                    pass
                self.client = None

            # Remove session files
            sess_dir = _session_dir()
            for p in sess_dir.glob("user_session*"):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
            return True


_GLOBAL_USER_MANAGER: TelegramUserManager | None = None


def get_telegram_user_manager() -> TelegramUserManager:
    global _GLOBAL_USER_MANAGER
    if _GLOBAL_USER_MANAGER is None:
        _GLOBAL_USER_MANAGER = TelegramUserManager()
    return _GLOBAL_USER_MANAGER
