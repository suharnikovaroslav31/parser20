"""Монохромные символы в тексте. Custom <tg-emoji> боту недоступны без своего стикерпака."""

from __future__ import annotations

_ICONS = {
    "user": "○",
    "star": "★",
    "gift": "◇",
    "chart": "▣",
    "link": "↗",
    "clock": "◷",
    "crown": "♔",
    "gem": "◆",
    "cart": "▢",
    "gear": "⚙",
    "check": "✓",
    "fire": "▲",
    "search": "⌕",
    "warn": "!",
    "chat": "✉",
    "spark": "✧",
    "id": "#",
    "ton": "◈",
}


def e(name: str) -> str:
    return _ICONS.get(name, "•")
