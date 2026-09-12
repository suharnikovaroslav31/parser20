"""Премиум custom emoji — те же ID, что в Lolz Deals.

В HTML: <tg-emoji emoji-id="...">fallback</tg-emoji>.
На кнопках: icon_custom_emoji_id. Если Telegram отклонит пак,
middleware выключает премиум до перезапуска и остаются unicode.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

LOGGER = logging.getLogger("tg_gifts.emoji")

# Те же ID и фолбэки, что в Lolz Deals: ключ = смысл строки.
IDS: dict[str, str] = {
    "cart": "5778672437122045013",
    "user": "6032949275732742941",
    "star": "5463289097336405244",
    "spark": "5325547803936572038",
    "gift": "5203996991054432397",
    "chart": "5244837092042750681",
    "money": "5893473283696759404",
    "ton": "5235630047959727475",
    "link": "5271604874419647061",
    "chat": "5443038326535759644",
    "bell": "5458603043203327669",
    "check": "5206607081334906820",
    "warn": "5274099962655816924",
    "gear": "5902016123972358349",
    "search": "5467538555158943525",
    "list": "5778299625370817409",
    "lightning": "5456140674028019486",
    "no": "5210952531676504517",
    "ok": "5206607081334906820",
    "plus": "5361847815255372871",
    "back": "5895507195524550741",
    "people": "6032609071373226027",
    "card": "5902056028513505203",
    "pen": "5395444784611480792",
    "shield": "5902016123972358349",
    "crown": "6039802097916974085",
    "id": "5467538555158943525",
    "clock": "5458603043203327669",
    "gem": "5235630047959727475",
    "fire": "5456140674028019486",
}

FALLBACK: dict[str, str] = {
    "cart": "🛒",
    "user": "👤",
    "star": "⭐",
    "spark": "✨",
    "gift": "🎁",
    "chart": "📈",
    "money": "💰",
    "ton": "💎",
    "link": "🔗",
    "chat": "💬",
    "bell": "🔔",
    "check": "✅",
    "warn": "❕",
    "gear": "🛡",
    "search": "💭",
    "list": "📝",
    "lightning": "⚡️",
    "no": "❌",
    "ok": "✅",
    "plus": "➕",
    "back": "⬅️",
    "people": "👥",
    "card": "💳",
    "pen": "✍️",
    "shield": "🛡",
    "crown": "🔱",
    "id": "💭",
    "clock": "🔔",
    "gem": "💎",
    "fire": "⚡️",
}

_TAG = re.compile(r"</?tg-emoji(?: emoji-id=\"\d+\")?>")
_FAIL_MARKS = (
    "tg-emoji",
    "custom emoji",
    "custom_emoji",
    "emoji_invalid",
    "icon_custom",
    "inline keyboard",
    "can't parse entities",
    "cant parse entities",
    "can't find end tag",
)
_enabled = True


def enabled() -> bool:
    return _enabled


def disable() -> None:
    global _enabled
    _enabled = False


def e(name: str) -> str:
    fallback = FALLBACK.get(name, "•")
    emoji_id = IDS.get(name, "")
    if not _enabled or not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def icon(name: str) -> Optional[str]:
    if not _enabled:
        return None
    return IDS.get(name) or None


def kb_icon(name: str) -> dict[str, str]:
    emoji_id = icon(name)
    if not emoji_id:
        return {}
    return {"icon_custom_emoji_id": emoji_id}


def strip(text: str) -> str:
    return _TAG.sub("", text or "")


def looks_like_emoji_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(mark in text for mark in _FAIL_MARKS)


def drop_button_icons(method: Any) -> bool:
    markup = getattr(method, "reply_markup", None)
    if not isinstance(markup, InlineKeyboardMarkup):
        return False
    changed = False
    for row in markup.inline_keyboard:
        for button in row:
            if getattr(button, "icon_custom_emoji_id", None):
                button.icon_custom_emoji_id = None
                changed = True
    return changed


class PremiumEmojiFallbackMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as exc:
            if not looks_like_emoji_error(exc):
                raise
            LOGGER.warning("Премиум-эмодзи отключены: %s", exc)
            disable()
            changed = drop_button_icons(method)
            for field in ("text", "caption"):
                value = getattr(method, field, None)
                if isinstance(value, str) and "<tg-emoji" in value:
                    setattr(method, field, strip(value))
                    changed = True
            if not changed:
                raise
            return await make_request(bot, method)
