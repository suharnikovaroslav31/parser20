"""HTML-карточка в группу + кнопка «Занять лот»."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter

from bot.claims import ClaimLot, ClaimStore, lot_keyboard, new_token
from bot.emoji import disable, e, strip
from core.models import FilterDecision, UniqueGift
from core.runtime import LiveFilters

LOGGER = logging.getLogger("tg_gifts.logger")
COMMUNITY = "https://t.me/BYRMALDAEVO"
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\ufeff]")
_URL_OK = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%\-]+$")
_SOURCE_NAME = {
    "tg_market": "Telegram",
    "telegram_resale": "Telegram",
    "mrkt": "MRKT",
    "tonnel": "Tonnel",
    "portal": "Portals",
    "getgems": "Getgems",
}


def _esc(value: object) -> str:
    return html.escape(_CTRL.sub("", "" if value is None else str(value)), quote=True)


def _ton(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def _age_label(registered_at: Optional[datetime], days: Optional[int]) -> str:
    if registered_at is None:
        return "н/д"
    stamp = registered_at.strftime("%Y-%m")
    if days is None:
        return f"~ {stamp}"
    return f"~ {stamp} ({days}д)"


def _http_link(url: str, label: str) -> str:
    text = (url or "").strip()
    if not _URL_OK.match(text):
        return _esc(label)
    return f'<a href="{html.escape(text, quote=True)}">{_esc(label)}</a>'


def _gift_line(gift: UniqueGift) -> str:
    floor = _ton(gift.best_floor_ton)
    source = gift.market_source or "telegram"
    number = f" #{gift.number}" if gift.number is not None else ""
    model = f" · {gift.model}" if gift.model else ""
    title = f"{gift.title}{number}"
    link = gift.nft_link
    if link.startswith("https://"):
        return (
            f"{e('gift')} {_http_link(link, title)}{_esc(model)} — "
            f"{e('ton')} <code>{floor} TON</code> ({_esc(source)})"
        )
    return (
        f"{e('gift')} {_esc(title)}{_esc(model)} — "
        f"{e('ton')} <code>{floor} TON</code> ({_esc(source)})"
    )


def _plain(decision: FilterDecision, live: LiveFilters) -> str:
    snap = decision.snapshot
    m = snap.metrics
    gift = snap.cheap_gifts[0] if snap.cheap_gifts else None
    title = f"{gift.title} #{gift.number}" if gift and gift.number is not None else (gift.title if gift else "лот")
    return (
        f"Маркет {snap.source} | {_ton(snap.min_floor_ton)} TON\n"
        f"Продавец {m.display_name} id {m.user_id}\n"
        f"Рейтинг ур.{m.stars_rating_level} | NFT {len(snap.unique_gifts)}\n"
        f"{title}\n"
        f"{gift.nft_link if gift else ''}\n"
        f"{live.community_url or COMMUNITY}"
    )


class GiftLogger:
    def __init__(self, bot: Bot, log_group_id: int, live: LiveFilters, claims: ClaimStore) -> None:
        self.bot = bot
        self.log_group_id = log_group_id
        self.live = live
        self.claims = claims

    def render(self, decision: FilterDecision) -> str:
        snapshot = decision.snapshot
        metrics = snapshot.metrics
        handle = f"@{metrics.username}" if metrics.username else "без username"
        cheap_block = "\n".join(_gift_line(gift) for gift in snapshot.cheap_gifts[:8]) or "—"
        extra = f"\n... ещё {len(snapshot.cheap_gifts) - 8}" if len(snapshot.cheap_gifts) > 8 else ""
        getgems = snapshot.unique_gifts[0].getgems_link if snapshot.unique_gifts else "https://getgems.io"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        rating = f"ур. {metrics.stars_rating_level}" if metrics.stars_rating_level is not None else "н/д"
        community = self.live.community_url or COMMUNITY
        profile = metrics.telegram_link
        profile_html = (
            _http_link(profile, metrics.display_name)
            if profile.startswith("https://")
            else _esc(metrics.display_name)
        )
        return (
            f"{e('cart')} <b>Маркет</b> <code>{_esc(_SOURCE_NAME.get(snapshot.source, snapshot.source))}</code> · "
            f"{e('ton')} <code>{_ton(snapshot.min_floor_ton)} TON</code>\n"
            f"{e('user')} <b>Продавец</b> {profile_html} "
            f"({_esc(handle)})\n"
            f"{e('id')} ID <code>{metrics.user_id}</code>\n"
            f"{e('star')} <b>Stars-рейтинг:</b> <code>{_esc(rating)}</code>\n"
            f"{e('spark')} <b>Premium:</b> <code>{'да' if metrics.is_premium else 'нет'}</code>\n"
            f"{e('gift')} <b>NFT в профиле:</b> "
            f"<code>{len(snapshot.unique_gifts)}</code> / макс <code>{self.live.max_unique_gifts}</code>\n"
            f"{e('chart')} <b>Регистрация</b> {_esc(_age_label(metrics.approx_registered_at, metrics.account_age_days))}\n"
            f"{e('money')} <b>Лот</b>\n{cheap_block}{extra}\n"
            f"{e('link')} {_http_link(getgems, 'Getgems')} · {e('chat')} {_http_link(community, 'чат BYRMALDAEVO')}\n"
            f"{e('bell')} <code>{now}</code>\n"
            f"<i>{_esc('; '.join(decision.reasons))}</i>"
        )

    def _remember(self, decision: FilterDecision) -> str:
        snap = decision.snapshot
        gift = snap.cheap_gifts[0] if snap.cheap_gifts else None
        token = new_token()
        self.claims.put(
            ClaimLot(
                token=token,
                title=(gift.title if gift else "лот")[:80],
                slug=(gift.slug if gift else "")[:80],
                number=gift.number if gift else None,
                price_ton=snap.min_floor_ton,
                source=snap.source,
                seller_id=snap.metrics.user_id,
                seller_name=snap.metrics.display_name[:80],
                seller_username=snap.metrics.username,
                nft_link=gift.nft_link if gift else "",
                getgems_link=gift.getgems_link if gift else "",
                rating=snap.metrics.stars_rating_level,
                created=time.time(),
            )
        )
        return token

    async def send(self, decision: FilterDecision) -> bool:
        token = self._remember(decision)
        nft = decision.snapshot.cheap_gifts[0].nft_link if decision.snapshot.cheap_gifts else ""
        html_text = self.render(decision)
        plain = _plain(decision, self.live)
        last_error = None
        for attempt in range(8):
            markup = lot_keyboard(token, nft)
            try:
                await self._deliver(html_text, markup, html=True)
                return True
            except TelegramRetryAfter as exc:
                wait = min(int(exc.retry_after) + 1, 60)
                LOGGER.warning("группа flood, жду %sс (попытка %s)", wait, attempt + 1)
                await asyncio.sleep(wait)
                last_error = exc
                continue
            except TelegramBadRequest as exc:
                last_error = exc
                LOGGER.warning("карточка отклонена (%s), упрощает", exc)
                disable()
                html_text = strip(html_text)
                text_l = str(exc).lower()
                use_html = "too long" not in text_l and "message is too long" not in text_l
                try:
                    await self._deliver(html_text if use_html else plain, lot_keyboard(token, nft), html=use_html)
                    return True
                except TelegramRetryAfter as exc2:
                    wait = min(int(exc2.retry_after) + 1, 60)
                    LOGGER.warning("группа flood после fallback, жду %sс", wait)
                    await asyncio.sleep(wait)
                    continue
                except TelegramAPIError:
                    try:
                        await self._deliver(plain, lot_keyboard(token, nft), html=False)
                        return True
                    except TelegramRetryAfter as exc3:
                        await asyncio.sleep(min(int(exc3.retry_after) + 1, 60))
                        continue
                    except TelegramAPIError as exc3:
                        LOGGER.error("Не удалось отправить лог: %s", exc3)
                        return False
            except TelegramAPIError as exc:
                LOGGER.error("Не удалось отправить лог: %s", exc)
                return False
        LOGGER.error("карточка не ушла после ретраев: %s", last_error)
        return False

    async def _deliver(self, text: str, markup, *, html: bool) -> None:
        await self.bot.send_message(
            chat_id=self.log_group_id,
            text=text,
            reply_markup=markup,
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML if html else None,
        )
