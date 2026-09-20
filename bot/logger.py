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
from config import log_group_id_candidates
from core.models import FilterDecision, UniqueGift
from core.runtime import BUILD, LiveFilters

LOGGER = logging.getLogger("tg_gifts.logger")
COMMUNITY = "https://t.me/GGsel_deal"
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\ufeff]")
_URL_OK = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%\-]+$")
_SOURCE_NAME = {
    "tg_market": "Telegram",
    "telegram_resale": "Telegram",
    "dialog": "диалог",
    "recent_gift_peer": "гифт",
    "live_gift_received": "гифт",
    "live_gift_action": "гифт",
    "nft_chat": "чат",
    "contact": "контакт",
    "seed_username": "seed",
    "seed_id": "seed",
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


def _gift_name(gift: UniqueGift) -> str:
    title = (gift.title or "").strip()
    if not title:
        title = (gift.slug or "").replace("-", " ").strip() or "подарок"
    number = f" #{gift.number}" if gift.number is not None else ""
    return f"{title}{number}"


def _gift_block(gift: UniqueGift) -> str:
    name = _gift_name(gift)
    link = gift.nft_link
    heading = _http_link(link, name) if link.startswith("https://") else _esc(name)
    lines = [f"{e('gift')} <b>{heading}</b>"]
    traits: list[str] = []
    if gift.model:
        traits.append(f"модель {_esc(gift.model)}")
    if gift.backdrop:
        traits.append(f"фон {_esc(gift.backdrop)}")
    if gift.symbol:
        traits.append(f"узор {_esc(gift.symbol)}")
    if traits:
        lines.append(" · ".join(traits))
    source = gift.market_source or "telegram"
    lines.append(f"{e('ton')} <code>{_ton(gift.best_floor_ton)} TON</code> · {_esc(source)}")
    return "\n".join(lines)


def _gifts_for_card(decision: FilterDecision) -> list[UniqueGift]:
    snap = decision.snapshot
    gifts = list(snap.cheap_gifts or [])
    if not gifts and decision.cheapest_gift is not None:
        gifts = [decision.cheapest_gift]
    if not gifts:
        gifts = list(snap.unique_gifts or [])
    return gifts[:3]


def _plain(decision: FilterDecision, live: LiveFilters) -> str:
    snap = decision.snapshot
    m = snap.metrics
    gifts = _gifts_for_card(decision)
    gift = gifts[0] if gifts else None
    title = _gift_name(gift) if gift else "лот"
    return (
        f"Маркет {snap.source} | {_ton(snap.min_floor_ton)} TON\n"
        f"Продавец {m.display_name} id {m.user_id}\n"
        f"Рейтинг ур.{m.stars_rating_level} | NFT {len(snap.unique_gifts)}\n"
        f"{title}\n"
        f"{gift.nft_link if gift else ''}\n"
        f"{live.community_url or COMMUNITY}\n"
        f"сборка {BUILD}"
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
        gifts = _gifts_for_card(decision)
        gift_block = "\n\n".join(_gift_block(gift) for gift in gifts) or "—"
        extra = f"\n... ещё {len(snapshot.cheap_gifts) - 3}" if len(snapshot.cheap_gifts) > 3 else ""
        getgems = gifts[0].getgems_link if gifts else "https://getgems.io"
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
            f"{gift_block}{extra}\n"
            f"{e('cart')} <b>Маркет</b> <code>{_esc(_SOURCE_NAME.get(snapshot.source, snapshot.source))}</code>\n"
            f"{e('user')} <b>Продавец</b> {profile_html} "
            f"({_esc(handle)})\n"
            f"{e('id')} ID <code>{metrics.user_id}</code>\n"
            f"{e('star')} <b>Stars-рейтинг:</b> <code>{_esc(rating)}</code>\n"
            f"{e('spark')} <b>Premium:</b> <code>{'да' if metrics.is_premium else 'нет'}</code>\n"
            f"{e('gift')} <b>NFT в профиле:</b> "
            f"<code>{len(snapshot.unique_gifts)}</code> / макс <code>{self.live.max_unique_gifts}</code>\n"
            f"{e('chart')} <b>Регистрация</b> {_esc(_age_label(metrics.approx_registered_at, metrics.account_age_days))}\n"
            f"{e('link')} {_http_link(getgems, 'Getgems')} · {e('chat')} {_http_link(community, 'гарант')}\n"
            f"{e('bell')} <code>{now}</code> · сборка <code>{_esc(BUILD)}</code>\n"
            f"<i>{_esc('; '.join(decision.reasons))}</i>"
        )

    def render_caption(self, decision: FilterDecision) -> str:
        snapshot = decision.snapshot
        metrics = snapshot.metrics
        handle = f"@{metrics.username}" if metrics.username else "без username"
        gifts = _gifts_for_card(decision)
        gift_block = "\n".join(_gift_block(gift) for gift in gifts) or "—"
        rating = f"ур. {metrics.stars_rating_level}" if metrics.stars_rating_level is not None else "н/д"
        profile = metrics.telegram_link
        profile_html = (
            _http_link(profile, metrics.display_name)
            if profile.startswith("https://")
            else _esc(metrics.display_name)
        )
        text = (
            f"{gift_block}\n"
            f"{e('user')} {profile_html} ({_esc(handle)})\n"
            f"{e('id')} <code>{metrics.user_id}</code> · {e('star')} {_esc(rating)} · "
            f"NFT <code>{len(snapshot.unique_gifts)}</code>\n"
            f"{e('cart')} {_esc(_SOURCE_NAME.get(snapshot.source, snapshot.source))}"
        )
        if len(text) > 1024:
            text = strip(text)
        return text[:1024]

    def _remember(self, decision: FilterDecision) -> str:
        snap = decision.snapshot
        gift = snap.cheap_gifts[0] if snap.cheap_gifts else (snap.unique_gifts[0] if snap.unique_gifts else None)
        token = new_token()
        title = _gift_name(gift) if gift else "лот"
        if gift and gift.model:
            title = f"{title} · {gift.model}"
        self.claims.put(
            ClaimLot(
                token=token,
                title=title[:80],
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
        gifts = _gifts_for_card(decision)
        nft = gifts[0].nft_link if gifts else ""
        html_text = self.render(decision)
        caption = self.render_caption(decision)
        photo = gifts[0].image_url if gifts else ""
        plain = _plain(decision, self.live)
        chat = self.live.community_url or COMMUNITY
        last_error = None
        for attempt in range(3):
            markup = lot_keyboard(token, nft, chat)
            try:
                await self._deliver(html_text, markup, html=True, photo_url=photo, caption=caption, nft_link=nft)
                return True
            except TelegramRetryAfter as exc:
                wait = min(int(getattr(exc, "retry_after", 3) or 3) + 1, 6)
                LOGGER.warning("группа flood, жду %sс (попытка %s)", wait, attempt + 1)
                await asyncio.sleep(wait)
                last_error = exc
                continue
            except TelegramBadRequest as exc:
                last_error = exc
                text_l = str(exc).lower()
                if "chat not found" in text_l:
                    if await self._try_other_chat():
                        continue
                    LOGGER.error(
                        "бот не видит лог-группу %s. Добавь ЭТОГО бота в НОВУЮ группу и проверь LOG_GROUP_ID на хосте",
                        self.log_group_id,
                    )
                    return False
                LOGGER.warning("карточка отклонена (%s), упрощает", exc)
                disable()
                html_text = strip(html_text)
                use_html = "too long" not in text_l and "message is too long" not in text_l
                try:
                    await self._deliver(
                        html_text if use_html else plain,
                        lot_keyboard(token, nft, chat),
                        html=use_html,
                        photo_url=photo,
                        caption=caption,
                        nft_link=nft,
                    )
                    return True
                except TelegramRetryAfter as exc2:
                    wait = min(int(getattr(exc2, "retry_after", 3) or 3) + 1, 6)
                    LOGGER.warning("группа flood после fallback, жду %sс", wait)
                    await asyncio.sleep(wait)
                    continue
                except TelegramAPIError:
                    try:
                        await self._deliver(
                            plain,
                            lot_keyboard(token, nft, chat),
                            html=False,
                            photo_url=photo,
                            caption=strip(caption)[:1024],
                            nft_link=nft,
                        )
                        return True
                    except TelegramRetryAfter as exc3:
                        await asyncio.sleep(min(int(getattr(exc3, "retry_after", 5) or 5) + 1, 20))
                        continue
                    except TelegramAPIError as exc3:
                        LOGGER.error("Не удалось отправить лог: %s", exc3)
                        return False
            except TelegramAPIError as exc:
                last_error = exc
                LOGGER.warning("отправка: %s", exc)
                continue
            except asyncio.TimeoutError as exc:
                last_error = exc
                LOGGER.warning("отправка зависла, пробую ещё")
                continue
        LOGGER.error("карточка не ушла после ретраев: %s", last_error)
        return False

    async def _try_other_chat(self) -> bool:
        for chat_id in log_group_id_candidates(self.log_group_id):
            if chat_id == self.log_group_id:
                continue
            try:
                await asyncio.wait_for(self.bot.get_chat(chat_id), timeout=8)
            except Exception:
                LOGGER.warning("группа %s тоже не видна", chat_id)
                continue
            LOGGER.warning("лог-группа %s не найдена — пишу в %s", self.log_group_id, chat_id)
            self.log_group_id = chat_id
            return True
        return False

    async def probe(self) -> None:
        last = None
        for chat_id in log_group_id_candidates(self.log_group_id):
            try:
                chat = await asyncio.wait_for(self.bot.get_chat(chat_id), timeout=8)
            except Exception as exc:
                last = exc
                LOGGER.warning("проверка группы %s: %s", chat_id, exc)
                continue
            title = getattr(chat, "title", None) or getattr(chat, "username", "") or chat_id
            self.log_group_id = chat_id
            LOGGER.info("лог-группа ок: %s (%s)", chat_id, title)
            return
        LOGGER.error(
            "лог-группа недоступна (%s). Добавь бота парсера в новую группу, LOG_GROUP_ID=%s",
            last,
            self.log_group_id,
        )

    async def announce_build(self, build: str, admin_id: int) -> None:
        text = (
            f"{e('lightning')} <b>сборка</b> <code>{_esc(build)}</code> запущена\n"
            f"лохи = новички с промахом цены, не те кто шарит рынок"
        )
        sent = 0
        seen: set[int] = set()
        for chat_id in (self.log_group_id, admin_id):
            if not chat_id or int(chat_id) in seen:
                continue
            seen.add(int(chat_id))
            try:
                await asyncio.wait_for(
                    self.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        disable_web_page_preview=True,
                        parse_mode=ParseMode.HTML,
                    ),
                    timeout=12,
                )
                sent += 1
            except Exception as exc:
                LOGGER.warning("пинг сборки chat=%s: %s", chat_id, exc)
        if sent:
            LOGGER.info("пинг сборки %s ушёл в %s чат(ов)", build, sent)
        else:
            LOGGER.error("пинг сборки %s никуда не ушёл — группа/админ не видят бота", build)

    async def announce_pass(self, build: str, stats: str) -> None:
        LOGGER.info("круг %s | %s", build, stats)

    async def _deliver(
        self,
        text: str,
        markup,
        *,
        html: bool,
        photo_url: str = "",
        caption: str = "",
        nft_link: str = "",
    ) -> None:
        parse_mode = ParseMode.HTML if html else None
        if photo_url:
            try:
                await asyncio.wait_for(
                    self.bot.send_photo(
                        chat_id=self.log_group_id,
                        photo=photo_url,
                        caption=caption or text[:1024],
                        reply_markup=markup,
                        parse_mode=parse_mode,
                    ),
                    timeout=8,
                )
                return
            except (TelegramBadRequest, asyncio.TimeoutError) as exc:
                LOGGER.warning("фото гифта не ушло (%s)", exc)
        preview = f"{nft_link}\n{text}" if nft_link and nft_link not in text else text
        await asyncio.wait_for(
            self.bot.send_message(
                chat_id=self.log_group_id,
                text=preview,
                reply_markup=markup,
                disable_web_page_preview=False,
                parse_mode=parse_mode,
            ),
            timeout=8,
        )
