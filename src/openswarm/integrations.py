"""Integrations Hub for OpenSwarm.

Supports:
1. Telegram Bot (notifications and reports)
2. Discord Webhooks (rich embeds and status alerts)
3. Email (SMTP notifications and HTML reports)
4. Calendar (.ics event generation, reminders, and Google Calendar links)
"""

from __future__ import annotations

import asyncio
import email.mime.multipart
import email.mime.text
import json
import logging
import smtplib
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib import request

logger = logging.getLogger("openswarm.integrations")


@dataclass
class IntegrationSettings:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False
    telegram_require_confirmation: bool = True

    discord_webhook_url: str = ""
    discord_enabled: bool = False

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_tls: bool = True
    email_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_bot_token": self.telegram_bot_token,
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_enabled": self.telegram_enabled,
            "telegram_require_confirmation": self.telegram_require_confirmation,
            "notify_telegram": self.telegram_enabled,
            "discord_webhook_url": self.discord_webhook_url,
            "discord_enabled": self.discord_enabled,
            "notify_discord": self.discord_enabled,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_user": self.smtp_user,
            "smtp_from": self.smtp_from,
            "smtp_to": self.smtp_to,
            "smtp_tls": self.smtp_tls,
            "smtp_use_tls": self.smtp_tls,
            "email_enabled": self.email_enabled,
            "notify_email": self.email_enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IntegrationSettings":
        return cls(
            telegram_bot_token=str(data.get("telegram_bot_token", "")),
            telegram_chat_id=str(data.get("telegram_chat_id", "")),
            telegram_enabled=bool(data.get("telegram_enabled", data.get("notify_telegram", False))),
            telegram_require_confirmation=bool(data.get("telegram_require_confirmation", True)),
            discord_webhook_url=str(data.get("discord_webhook_url", "")),
            discord_enabled=bool(data.get("discord_enabled", data.get("notify_discord", False))),
            smtp_host=str(data.get("smtp_host", "")),
            smtp_port=int(data.get("smtp_port", 587)),
            smtp_user=str(data.get("smtp_user", "")),
            smtp_password=str(data.get("smtp_password", "")),
            smtp_from=str(data.get("smtp_from", "")),
            smtp_to=str(data.get("smtp_to", "")),
            smtp_tls=bool(data.get("smtp_tls", data.get("smtp_use_tls", True))),
            email_enabled=bool(data.get("email_enabled", data.get("notify_email", False))),
        )


async def send_telegram(token: str, chat_id: str, text: str, parse_mode: str = "HTML", confirmed: bool = True) -> bool:
    """Send a notification message to Telegram (only if confirmed=True)."""
    if not token or not chat_id or not confirmed:
        return False

    def _sync():
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with request.urlopen(req, timeout=10) as resp:
            return resp.status == 200

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


async def send_discord(webhook_url: str, title: str, description: str, color: int = 0x5865F2, fields: list[dict[str, Any]] | None = None) -> bool:
    """Send a formatted notification to Discord via webhook."""
    if not webhook_url:
        return False

    def _sync():
        embed = {
            "title": title[:256],
            "description": description[:2048],
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "OpenSwarm 1.0 Autonomous Agent"},
        }
        if fields:
            embed["fields"] = fields[:25]
        payload = {
            "username": "OpenSwarm",
            "avatar_url": "https://raw.githubusercontent.com/opencode-ai/opencode/main/assets/logo.png",
            "embeds": [embed],
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(webhook_url, data=data, headers={"Content-Type": "application/json", "User-Agent": "OpenSwarm/1.0"})
        with request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        logger.warning("Discord send failed: %s", exc)
        return False


async def send_email(settings: IntegrationSettings, subject: str, body: str, html: str | None = None) -> bool:
    """Send an email notification via SMTP."""
    if not settings.smtp_host or not settings.smtp_to:
        return False

    def _sync():
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from or settings.smtp_user or "openswarm@localhost"
        msg["To"] = settings.smtp_to
        msg["Date"] = email.utils.formatdate(localtime=True)

        part_text = email.mime.text.MIMEText(body, "plain", "utf-8")
        msg.attach(part_text)
        if html:
            part_html = email.mime.text.MIMEText(html, "html", "utf-8")
            msg.attach(part_html)

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            if settings.smtp_tls:
                server.starttls()
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(msg["From"], [settings.smtp_to], msg.as_string())
        return True

    try:
        return await asyncio.to_thread(_sync)
    except Exception as exc:
        logger.warning("Email send failed: %s", exc)
        return False


def generate_ics(title: str, description: str, start: datetime, duration_minutes: int = 60, location: str = "OpenSwarm Workspace") -> str:
    """Generate a standard iCalendar (.ics) string for calendar reminders and scheduling."""
    end = start + timedelta(minutes=duration_minutes)
    now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_str = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end_str = end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = f"openswarm-{int(start.timestamp())}-{hash(title) & 0xFFFFFFFF}@openswarm"

    clean_desc = description.replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    clean_title = title.replace("\n", " ").replace(",", "\\,").replace(";", "\\;")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//OpenSwarm//Agent Task Scheduler//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_str}",
        f"DTSTART:{start_str}",
        f"DTEND:{end_str}",
        f"SUMMARY:{clean_title}",
        f"DESCRIPTION:{clean_desc}",
        f"LOCATION:{location}",
        "STATUS:CONFIRMED",
        "BEGIN:VALARM",
        "TRIGGER:-PT15M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:Reminder: {clean_title}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines)


def google_calendar_link(title: str, description: str, start: datetime, duration_minutes: int = 60) -> str:
    """Generate a direct web link to create an event in Google Calendar."""
    end = start + timedelta(minutes=duration_minutes)
    fmt = "%Y%m%dT%H%M%SZ"
    dates = f"{start.astimezone(timezone.utc).strftime(fmt)}/{end.astimezone(timezone.utc).strftime(fmt)}"
    params = {
        "action": "TEMPLATE",
        "text": title,
        "details": description,
        "dates": dates,
        "location": "OpenSwarm",
    }
    return f"https://calendar.google.com/calendar/render?{urllib.parse.urlencode(params)}"
