"""Сборка Aiogram Dispatcher: логгер + админка."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.admin import setup_admin
from bot.claims import ClaimStore, setup_claims
from bot.emoji import PremiumEmojiFallbackMiddleware
from config import Settings
from core.runtime import LiveFilters


def build_bot(settings: Settings) -> Bot:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.session.middleware(PremiumEmojiFallbackMiddleware())
    return bot


def build_dispatcher(live: LiveFilters, claims: ClaimStore) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(setup_claims(claims))
    dispatcher.include_router(setup_admin(live))
    return dispatcher
