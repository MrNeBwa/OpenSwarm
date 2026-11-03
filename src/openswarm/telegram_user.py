"""Telegram User Account Integration for OpenSwarm.

Allows connecting a user's personal Telegram account via Telethon so OpenSwarm
can read recent messages, understand incoming conversations, and draft
contextual replies as a personal AI assistant.

SAFETY GUARANTEE:
All outgoing messages are strictly gated by `require_confirmation=True`.
No message is ever transmitted to Telegram without explicit user confirmation
(`confirmed=True` or explicit approval of a queued draft).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
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
    """Manages an active Telethon client session with strict confirmation-gated sending."""

    def __init__(self) -> None:
        self.client: Any = None
        self._phone_code_hash: str = ""
        self._phone: str = ""
        self._api_id: int = DEFAULT_API_ID
        self._api_hash: str = DEFAULT_API_HASH
        self._lock = asyncio.Lock()
        # Mandatory confirmation guard: never send without explicit user approval
        self.require_confirmation: bool = True
        self._pending_drafts: dict[str, dict[str, Any]] = {}

    def create_draft(self, chat_id: int | str, text: str, chat_title: str = "") -> dict[str, Any]:
        """Stores a message in the local pending queue awaiting explicit user confirmation."""
        draft_id = f"tg_draft_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        draft = {
            "id": draft_id,
            "chat_id": chat_id,
            "chat_title": chat_title or str(chat_id),
            "text": text.strip(),
            "status": "pending_confirmation",
            "created_at": time.time(),
        }
        self._pending_drafts[draft_id] = draft
        return draft

    def list_drafts(self) -> list[dict[str, Any]]:
        """Returns all drafts waiting for user approval."""
        return sorted(self._pending_drafts.values(), key=lambda x: x["created_at"], reverse=True)

    def reject_draft(self, draft_id: str) -> bool:
        """Discards a pending draft without sending."""
        return self._pending_drafts.pop(draft_id, None) is not None

    async def approve_draft(self, draft_id: str) -> dict[str, Any]:
        """Explicitly approves and sends a pending draft."""
        draft = self._pending_drafts.get(draft_id)
        if not draft:
            raise ValueError(f"Черновик {draft_id} не найден или уже удалён")
        res = await self.send_message(draft["chat_id"], draft["text"], confirmed=True, chat_title=draft.get("chat_title", ""))
        if res.get("ok"):
            self._pending_drafts.pop(draft_id, None)
        return res

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
        """Returns whether user account is authorized, profile, and confirmation guard state."""
        base_meta = {
            "require_confirmation": self.require_confirmation,
            "pending_drafts": self.list_drafts(),
        }
        try:
            from telethon import TelegramClient  # noqa: F401
        except ImportError:
            return {
                **base_meta,
                "installed": False,
                "connected": False,
                "authorized": False,
                "active": False,
                "status": "disconnected",
                "error": "telethon_missing",
            }

        sess_path = Path(_session_file() + ".session")
        has_session = sess_path.exists()

        if not has_session and (self.client is None or not self.client.is_connected()):
            return {
                **base_meta,
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
                        **base_meta,
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
                    **base_meta,
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
                    **base_meta,
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
            chat_title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(chat_id)
            messages = await client.get_messages(entity, limit=limit)
            out: list[dict[str, Any]] = []
            for m in reversed(messages):
                text = (getattr(m, "message", "") or "").strip()
                if not text:
                    if getattr(m, "photo", None):
                        text = "[Фото]"
                    elif getattr(m, "voice", None):
                        text = "[Голосовое сообщение]"
                    elif getattr(m, "sticker", None):
                        text = "[Стикер]"
                    elif getattr(m, "video", None) or getattr(m, "video_note", None):
                        text = "[Видео]"
                    elif getattr(m, "document", None):
                        text = "[Файл]"
                    else:
                        continue
                is_out = bool(getattr(m, "out", False))
                sender_name = "Вы" if is_out else str(chat_title)
                snd = getattr(m, "sender", None)
                if not is_out and snd is not None:
                    fn = (getattr(snd, "first_name", "") or "").strip()
                    ln = (getattr(snd, "last_name", "") or "").strip()
                    tl = (getattr(snd, "title", "") or "").strip()
                    un = (getattr(snd, "username", "") or "").strip()
                    full = f"{fn} {ln}".strip()
                    sender_name = full or tl or (f"@{un}" if un else sender_name)
                reply_to_id = getattr(m, "reply_to_msg_id", None)
                if reply_to_id is None and getattr(m, "reply_to", None) is not None:
                    reply_to_id = getattr(m.reply_to, "reply_to_msg_id", None)
                out.append({
                    "id": m.id,
                    "sender_id": getattr(m, "sender_id", 0),
                    "sender": sender_name,
                    "out": is_out,
                    "text": text,
                    "reply_to_msg_id": reply_to_id,
                    "date": m.date.isoformat() if m.date else "",
                })
            return out

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        confirmed: bool = False,
        chat_title: str = "",
    ) -> dict[str, Any]:
        """Sends a text message to chat ONLY if `confirmed=True` (when confirmation is required).

        If `confirmed=False`, queues the message as a pending draft and blocks network transmission.
        """
        clean_text = (text or "").strip()
        if not clean_text:
            raise ValueError("Текст сообщения не может быть пустым")

        if self.require_confirmation and not confirmed:
            draft = self.create_draft(chat_id, clean_text, chat_title=chat_title)
            logger.info("Blocked unconfirmed Telegram message to %s; created draft %s", chat_id, draft["id"])
            return {
                "ok": False,
                "sent": False,
                "requires_confirmation": True,
                "draft": draft,
                "error": "Отправка без подтверждения запрещена. Подтвердите отправку сообщения.",
            }

        async with self._lock:
            client = await self._ensure_client()
            if not await client.is_user_authorized():
                raise RuntimeError("Пользователь Telegram не авторизован")

            entity = await client.get_entity(int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id)
            msg = await client.send_message(entity, clean_text)
            return {"ok": True, "sent": True, "message_id": msg.id}

    @staticmethod
    def _clean_llm_output(raw: str) -> str:
        """Removes CLI banners (`> agent · model`), Thinking traces, and ANSI sequences."""
        if not raw:
            return ""
        clean = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", str(raw))
        lines: list[str] = []
        for ln in clean.splitlines():
            s = ln.strip()
            if not s:
                continue
            if s.startswith("> ") and "·" in s:
                continue
            if s.startswith("Thinking:") or s.startswith("$ "):
                continue
            lines.append(ln)
        return "\n".join(lines).strip()

    @staticmethod
    def _parse_llm_suggestion_payload(raw: str) -> dict[str, Any]:
        """Parses structured JSON or plain-text reply variants from model output."""
        cleaned = TelegramUserManager._clean_llm_output(raw)
        if not cleaned:
            return {"need_more_context": False, "replies": [], "reason": "", "summary": ""}

        # 1. Try parsing JSON object {"need_more_context": ..., "replies": [...]}
        obj_matches = list(re.finditer(r"\{[\s\S]*\}", cleaned))
        for m in reversed(obj_matches):
            candidate = m.group(0)
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    need_more = bool(data.get("need_more_context", False))
                    raw_replies = data.get("replies") or data.get("suggestions") or []
                    replies = [
                        str(x).strip()
                        for x in (raw_replies if isinstance(raw_replies, list) else [])
                        if str(x).strip()
                    ]
                    return {
                        "need_more_context": need_more,
                        "replies": replies[:3],
                        "reason": str(data.get("reason") or "").strip(),
                        "summary": str(data.get("context_summary") or data.get("summary") or "").strip(),
                    }
            except Exception:
                pass

        if "NEED_MORE_CONTEXT" in cleaned.upper() and not re.search(r'"need_more_context"\s*:\s*false', cleaned, re.I):
            reason_m = re.search(r"NEED_MORE_CONTEXT\s*[:\-]?\s*(.*)", cleaned, re.I)
            return {
                "need_more_context": True,
                "replies": [],
                "reason": (reason_m.group(1).strip() if reason_m else "Недостаточно контекста в последних сообщениях"),
                "summary": "",
            }

        # 2. Try parsing JSON array ["Вариант 1", "Вариант 2", "Вариант 3"]
        arr_match = re.search(r"\[\s*\"[\s\S]*?\"\s*\]", cleaned)
        if arr_match:
            try:
                parsed = json.loads(arr_match.group(0))
                if isinstance(parsed, list):
                    replies = [str(x).strip() for x in parsed if str(x).strip()]
                    if replies:
                        return {"need_more_context": False, "replies": replies[:3], "reason": "", "summary": ""}
            except Exception:
                pass

        # 3. Fallback line-by-line extraction (ignoring markdown fences and meta-lines)
        lines: list[str] = []
        for ln in cleaned.splitlines():
            s = ln.strip()
            if not s or s.startswith("```") or s.startswith("{") or s.startswith("}") or s.startswith("[") or s.startswith("]"):
                continue
            s = re.sub(r"^(?:вариант\s*\d+\s*[:\-–]|ответ\s*\d+\s*[:\-–]|\d+[\.\)]\s*|[-*•]\s*)", "", s, flags=re.I).strip()
            s = s.strip("\"'«» ")
            if len(s) >= 2 and not s.lower().startswith(("ниже привед", "вот три", "конечно", "json")):
                lines.append(s)
        return {"need_more_context": False, "replies": lines[:3], "reason": "", "summary": ""}

    @staticmethod
    def _needs_auto_expansion(msgs: list[dict[str, Any]]) -> tuple[bool, str]:
        """Detects when the current message window is structurally incomplete."""
        if not msgs:
            return False, ""
        msg_ids = {m.get("id") for m in msgs if m.get("id") is not None}
        for m in msgs:
            rep_id = m.get("reply_to_msg_id")
            if rep_id and rep_id not in msg_ids:
                return True, f"Сообщение ссылается на более раннее сообщение #{rep_id}"
        meaningful_texts = [
            re.sub(r"\[.*?\]", "", str(m.get("text") or "")).strip()
            for m in msgs
        ]
        non_empty = [t for t in meaningful_texts if t]
        total_words = sum(len(t.split()) for t in non_empty)
        if total_words < 6:
            return True, "Последние сообщения слишком короткие для понимания сути разговора"
        return False, ""

    @staticmethod
    def _smart_fallback_replies(msgs: list[dict[str, Any]], prompt_hint: str = "") -> list[str]:
        """Generates natural, context-aware replies when the LLM runner is unavailable."""
        if not msgs:
            return ["Привет! Как дела?", "Привет! На связи, что нового?", "Здравствуйте! Чем могу помочь?"]

        all_text = " ".join(str(m.get("text") or "") for m in msgs).lower()
        is_formal = any(w in all_text for w in ("здравствуйте", "добрый день", "коллеги", "уважаем", "подскажите, пожалуйста"))
        incoming = [m for m in msgs if not m.get("out")]
        last_msg = (incoming[-1] if incoming else msgs[-1]).get("text", "").strip()
        last_low = last_msg.lower()

        if prompt_hint:
            hint_clean = prompt_hint.strip().rstrip(".")
            if is_formal:
                return [
                    f"Да, {hint_clean}.",
                    f"Здравствуйте! По этому вопросу: {hint_clean}.",
                    f"Принято, спасибо! {hint_clean[:1].upper() + hint_clean[1:]}.",
                ]
            return [
                f"{hint_clean[:1].upper() + hint_clean[1:]}",
                f"Давай так: {hint_clean}",
                f"Ок, понял! {hint_clean[:1].upper() + hint_clean[1:]}",
            ]

        if "?" in last_msg or any(w in last_low for w in ("когда", "где", "как", "почему", "сможешь", "будешь", "получилось", "что думаешь")):
            if is_formal:
                return [
                    "Да, конечно, сейчас проверю и уточню детали.",
                    "Планирую в ближайшее время, сразу напишу по результату.",
                    "Пока уточняю этот момент, вернусь с ответом чуть позже.",
                ]
            return [
                "Да, давай! Сейчас гляну и напишу.",
                "Скоро буду знать точно, сразу маякну 👍",
                "Пока в процессе, чуть позже смогу сказать точнее.",
            ]

        if any(w in last_low for w in ("привет", "ку", "здаров", "доброе утро", "добрый вечер", "хай")):
            if is_formal:
                return [
                    "Здравствуйте! Рад слышать, как ваши дела?",
                    "Добрый день! На связи, чем могу помочь?",
                    "Здравствуйте! Всё отлично, спасибо.",
                ]
            return [
                "Привет! Всё супер, ты как?",
                "Привет! На связи, что нового?",
                "Хай! Как дела?",
            ]

        if is_formal:
            return [
                "Спасибо, принял к сведению!",
                "Понял вас, спасибо за информацию. Буду на связи.",
                "Отлично, договорились!",
            ]
        return [
            "Понял, спасибо! 👍",
            "Супер, давай так и сделаем!",
            "Ок, принято, на связи!",
        ]

    async def suggest_reply(
        self,
        chat_id: int | str,
        prompt_hint: str = "",
        runner_fn: Any = None,
        initial_limit: int = 5,
        max_limit: int = 40,
        return_meta: bool = False,
    ) -> list[str] | dict[str, Any]:
        """Reads recent conversation starting from `initial_limit` (5) messages and progressively
        doubles context (`5 -> 10 -> 20 -> 40`) if the conversation window is insufficient.
        Never sends any message automatically.
        """
        limit = max(1, int(initial_limit or 5))
        cap = max(limit, int(max_limit or 40))
        steps_tried: list[int] = []
        expansion_reasons: list[str] = []
        msgs: list[dict[str, Any]] = []
        final_replies: list[str] = []
        summary: str = ""

        while True:
            msgs = await self.get_messages(chat_id, limit=limit)
            steps_tried.append(len(msgs))
            if not msgs:
                final_replies = ["Привет! Как дела?", "Привет! На связи.", "Здравствуйте! Чем могу помочь?"]
                break

            reached_beginning = len(msgs) < limit
            can_expand = (not reached_beginning) and (limit < cap)

            # Check if structural context is obviously incomplete (e.g. reply to older msg or 1-word fragments)
            auto_expand, auto_reason = self._needs_auto_expansion(msgs)
            if auto_expand and can_expand and len(steps_tried) == 1:
                expansion_reasons.append(auto_reason)
                limit = min(cap, limit * 2)
                continue

            if not runner_fn:
                final_replies = self._smart_fallback_replies(msgs, prompt_hint=prompt_hint)
                break

            msg_by_id = {m.get("id"): m for m in msgs if m.get("id") is not None}
            transcript_lines: list[str] = []
            for idx, m in enumerate(msgs, 1):
                who = "Вы (пользователь)" if m.get("out") else f"Собеседник ({m.get('sender') or 'Контакт'})"
                t_str = (m.get("date") or "")[11:16]
                time_part = f" [{t_str}]" if t_str else ""
                rep_id = m.get("reply_to_msg_id")
                rep_part = ""
                if rep_id:
                    if rep_id in msg_by_id:
                        snippet = str(msg_by_id[rep_id].get("text") or "")[:40]
                        rep_part = f" (в ответ на: «{snippet}»)"
                    else:
                        rep_part = f" (в ответ на старое сообщение #{rep_id} вне текущего окна)"
                transcript_lines.append(f"{idx}.{time_part} {who}{rep_part}: {m.get('text', '')}")

            context_str = "\n".join(transcript_lines)
            last_speaker = "Вы (пользователь)" if msgs[-1].get("out") else "Собеседник"

            expand_instruction = (
                f"Сейчас тебе показаны последние {len(msgs)} сообщений диалога (шаг окна: {limit}).\n"
                "ВАЖНО: Оцени, достаточно ли этих сообщений, чтобы точно понять тему, тон общения и смысл последнего сообщения.\n"
                "- Если в этих сообщениях не хватает начала мысли, непонятно о чём речь, есть отсылки к более ранним сообщениям или это только короткие обрывки — "
                "НЕ выдумывай странный ответ наугад, а запроси расширение истории в 2 раза, вернув строго JSON:\n"
                '{"need_more_context": true, "reason": "кратко чего не хватает"}\n'
                "- Если контекста ДОСТАТОЧНО для адекватного ответа, верни строго JSON:\n"
                '{"need_more_context": false, "context_summary": "суть диалога в 5-8 словах", "replies": ["Вариант 1", "Вариант 2", "Вариант 3"]}\n'
            ) if can_expand else (
                f"Показаны последние {len(msgs)} сообщений диалога (максимальное доступное окно).\n"
                "Верни строго JSON с 3 адекватными вариантами ответа:\n"
                '{"need_more_context": false, "context_summary": "суть диалога в 5-8 словах", "replies": ["Вариант 1", "Вариант 2", "Вариант 3"]}\n'
            )

            prompt = (
                "Ты — умный помощник для личной переписки в Telegram. Твоя задача — предложить 3 живых, естественных и уместных ответа от лица пользователя («Вы»).\n\n"
                f"История последних сообщений ({len(msgs)} шт.):\n"
                f"{context_str}\n\n"
                f"Последнее сообщение написал: {last_speaker}.\n"
                f"Пожелание пользователя к ответу: {prompt_hint or 'Ответить адекватно, естественно и в том же стиле/тоне, в котором ведётся переписка'}.\n\n"
                "Правила хорошего ответа:\n"
                "1. Строго соблюдай тон переписки: если общение неформальное на «ты» и без официоза — пиши просто, живо и естественно (без канцеляризмов вроде «Ознакомился с сообщением»). Если деловое на «Вы» — пиши вежливо и по делу.\n"
                "2. Отвечай конкретно на суть последних сообщений собеседника (или дополни свою мысль, если последнее сообщение от пользователя).\n"
                "3. Предложи 3 разных по оттенку варианта: (1) краткий/естественный, (2) более подробный/конструктивный, (3) мягкий/уточняющий или альтернативный.\n\n"
                f"{expand_instruction}"
                "Выведи ТОЛЬКО валидный JSON без пояснений и без markdown-обёрток."
            )

            raw = await runner_fn(prompt)
            parsed = self._parse_llm_suggestion_payload(raw)

            if parsed.get("need_more_context") and can_expand:
                reason = parsed.get("reason") or f"Модель запросила больше контекста после {len(msgs)} сообщений"
                expansion_reasons.append(reason)
                limit = min(cap, limit * 2)
                continue

            if parsed.get("replies"):
                final_replies = parsed["replies"][:3]
                summary = parsed.get("summary") or ""
                break

            # If model returned nothing parseable and we can still expand once, try expanding or fallback
            final_replies = self._smart_fallback_replies(msgs, prompt_hint=prompt_hint)
            break

        if not final_replies:
            final_replies = self._smart_fallback_replies(msgs, prompt_hint=prompt_hint)

        if return_meta:
            return {
                "replies": final_replies,
                "context_used": len(msgs),
                "initial_limit": initial_limit,
                "steps": steps_tried,
                "expanded": len(steps_tried) > 1,
                "reasons": expansion_reasons,
                "summary": summary,
            }
        return final_replies

    async def logout(self) -> bool:
        """Disconnects and deletes session files."""
        async with self._lock:
            self._pending_drafts.clear()
            if self.client is not None:
                try:
                    if self.client.is_connected() and await self.client.is_user_authorized():
                        await self.client.log_out()
                    else:
                        await self.client.disconnect()
                except Exception:
                    pass
                self.client = None

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
