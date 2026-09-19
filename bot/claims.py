"""Кнопка «Занять лот»: карточка уходит в личку нажавшему."""

from __future__ import annotations

import html
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.emoji import e, kb_icon

LOGGER = logging.getLogger("tg_gifts.claims")
TTL_SEC = 6 * 3600


@dataclass
class ClaimLot:
    token: str
    title: str
    slug: str
    number: Optional[int]
    price_ton: Optional[float]
    source: str
    seller_id: int
    seller_name: str
    seller_username: Optional[str]
    nft_link: str
    getgems_link: str
    rating: Optional[int]
    created: float


class ClaimStore:
    def __init__(self) -> None:
        self._items: dict[str, ClaimLot] = {}

    def put(self, lot: ClaimLot) -> str:
        self._purge()
        self._items[lot.token] = lot
        return lot.token

    def get(self, token: str) -> Optional[ClaimLot]:
        self._purge()
        return self._items.get(token)

    def take(self, token: str) -> Optional[ClaimLot]:
        self._purge()
        return self._items.pop(token, None)

    def _purge(self) -> None:
        now = time.time()
        dead = [key for key, item in self._items.items() if now - item.created > TTL_SEC]
        for key in dead:
            self._items.pop(key, None)


def new_token() -> str:
    return secrets.token_hex(8)


def lot_keyboard(token: str, nft_link: str, community_url: str = "") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Занять лот", callback_data=f"claim:{token}", **kb_icon("check"))]]
    url = _http_url(nft_link)
    if url:
        rows.append([InlineKeyboardButton(text="Открыть NFT", url=url, **kb_icon("link"))])
    chat = _http_url(community_url) or "https://t.me/GGsel_deal"
    rows.append(
        [InlineKeyboardButton(text="Гарант GGsel_deal", url=chat, **kb_icon("chat"))]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _http_url(url: str) -> str:
    text = (url or "").strip()
    if re.fullmatch(r"https://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%\-]+", text):
        return text
    return ""


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def format_lot_dm(lot: ClaimLot) -> str:
    handle = f"@{lot.seller_username}" if lot.seller_username else "без username"
    num = f" #{lot.number}" if lot.number is not None and f"#{lot.number}" not in lot.title else ""
    price = f"{lot.price_ton:.2f}" if lot.price_ton is not None else "n/a"
    rating = f"ур. {lot.rating}" if lot.rating is not None else "н/д"
    nft = _esc(lot.nft_link) if lot.nft_link else "—"
    return (
        f"{e('check')} <b>Лот занят</b>\n"
        f"{e('gift')} <b>{_esc(lot.title)}{num}</b>\n"
        f"{e('ton')} Цена: <code>{price} TON</code> · {_esc(lot.source)}\n"
        f"{e('star')} Рейтинг продавца: <code>{_esc(rating)}</code>\n"
        f"{e('user')} Продавец: {_esc(lot.seller_name)} ({_esc(handle)})\n"
        f"{e('id')} ID <code>{lot.seller_id}</code>\n"
        f"{e('link')} NFT: {nft}\n"
        f"{e('chat')} Гарант: https://t.me/GGsel_deal"
    )


def _claimer_label(user) -> str:
    name = (getattr(user, "full_name", None) or "").strip() or "кто-то"
    username = getattr(user, "username", None)
    if username:
        return f"{name} (@{username})"
    return f"{name} · id {user.id}"


def claimed_notice(claimed_by, lot: Optional[ClaimLot] = None) -> str:
    who = _esc(_claimer_label(claimed_by)) if claimed_by is not None else "уже занят"
    lines = [f"{e('check')} <b>Лот занят</b>", f"{e('user')} {who}"]
    if lot is not None:
        num = f" #{lot.number}" if lot.number is not None and f"#{lot.number}" not in lot.title else ""
        title = f"{_esc(lot.title)}{num}".strip()
        if title:
            lines.insert(1, f"{e('gift')} {title}")
    return "\n".join(lines)


async def _mark_group_claimed(call: CallbackQuery, lot: ClaimLot, claimed_by) -> None:
    """Карточка MATCH пропадает, остаётся только кто занял."""
    message = call.message
    if not isinstance(message, Message):
        return
    text = claimed_notice(claimed_by, lot)
    try:
        await message.edit_text(text, reply_markup=None, disable_web_page_preview=True)
        return
    except TelegramBadRequest as exc:
        LOGGER.warning("не обновил карточку (%s)", exc)
    try:
        await message.reply(text, disable_web_page_preview=True)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        LOGGER.warning("не написал что лот занят: %s", exc)


async def _mark_already_taken(call: CallbackQuery) -> None:
    message = call.message
    if not isinstance(message, Message):
        return
    text = f"{e('check')} <b>Лот занят</b>"
    try:
        await message.edit_text(text, reply_markup=None, disable_web_page_preview=True)
    except (TelegramBadRequest, TelegramForbiddenError):
        try:
            await message.edit_reply_markup(reply_markup=None)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


def setup_claims(store: ClaimStore) -> Router:
    router = Router(name="claims")

    @router.callback_query(F.data.startswith("claim:"))
    async def on_claim(call: CallbackQuery) -> None:
        token = (call.data or "").split(":", 1)[-1]
        lot = store.take(token)
        if lot is None:
            await call.answer("Лот уже занят или устарел", show_alert=True)
            await _mark_already_taken(call)
            return
        user = call.from_user
        if user is None:
            store.put(lot)
            await call.answer("Нет пользователя", show_alert=True)
            return
        text = format_lot_dm(lot)
        try:
            await call.bot.send_message(
                chat_id=user.id,
                text=text,
                disable_web_page_preview=True,
            )
        except TelegramForbiddenError:
            store.put(lot)
            await call.answer(
                "Сначала напишите боту /start в личке, потом нажмите ещё раз",
                show_alert=True,
            )
            return
        except TelegramBadRequest as exc:
            LOGGER.warning("HTML DM отклонён (%s), шлём plain", exc)
            try:
                await call.bot.send_message(
                    chat_id=user.id,
                    text=(
                        f"Лот занят\n{lot.title}\n"
                        f"{lot.price_ton} TON · {lot.source}\n"
                        f"{lot.nft_link}\n"
                        f"Продавец {lot.seller_name} id {lot.seller_id}"
                    ),
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                store.put(lot)
                await call.answer("Напишите боту /start в личке и повторите", show_alert=True)
                return
        await call.answer("Лот отправлен вам в личку")
        await _mark_group_claimed(call, lot, user)

    return router
