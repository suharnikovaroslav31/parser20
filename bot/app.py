"""Сборка Aiogram Dispatcher: логгер + админка."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.admin import setup_admin
from bot.claims import ClaimStore, setup_claims
from config import Settings
from core.runtime import LiveFilters


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher(live: LiveFilters, claims: ClaimStore) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(setup_claims(claims))
    dispatcher.include_router(setup_admin(live))
    return dispatcher
