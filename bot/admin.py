"""
Админ-панель Aiogram. Видна только ADMIN_ID.
"""

from __future__ import annotations

import logging
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.emoji import e
from core.runtime import LiveFilters

LOGGER = logging.getLogger("tg_gifts.admin")
ADMIN_ID = 8927983640

router = Router(name="admin")


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return user is not None and user.id == ADMIN_ID


class EditFilter(StatesGroup):
    waiting_value = State()


def _kb(live: LiveFilters) -> InlineKeyboardMarkup:
    on = "ON" if live.scanner_enabled else "OFF"
    age = "выкл" if not live.max_account_age_days else str(live.max_account_age_days)
    prem = {True: "да", False: "нет", None: "любой"}[live.require_premium]
    rating_req = "да" if live.require_stars_rating else "нет"
    rows = [
        [InlineKeyboardButton(text=f"{'■' if live.scanner_enabled else '□'} Сканер {on}", callback_data="tgl:scanner")],
        [
            InlineKeyboardButton(text=f"Цена мин {live.floor_min_ton:g}", callback_data="set:floor_min_ton"),
            InlineKeyboardButton(text=f"Цена макс {live.floor_max_ton:g}", callback_data="set:floor_max_ton"),
        ],
        [
            InlineKeyboardButton(text=f"NFT мин {live.min_unique_gifts}", callback_data="set:min_unique_gifts"),
            InlineKeyboardButton(text=f"NFT макс {live.max_unique_gifts}", callback_data="set:max_unique_gifts"),
        ],
        [
            InlineKeyboardButton(text=f"Рейтинг мин {live.stars_rating_min}", callback_data="set:stars_rating_min"),
            InlineKeyboardButton(text=f"Рейтинг макс {live.stars_rating_max}", callback_data="set:stars_rating_max"),
        ],
        [InlineKeyboardButton(text=f"Рейтинг обязателен: {rating_req}", callback_data="tgl:rating")],
        [
            InlineKeyboardButton(text=f"Возраст дн. {age}", callback_data="set:max_account_age_days"),
            InlineKeyboardButton(text=f"Premium {prem}", callback_data="cycle:premium"),
        ],
        [
            InlineKeyboardButton(text=f"Активность ≥ {live.min_activity_score}", callback_data="set:min_activity_score"),
            InlineKeyboardButton(text=f"Круг {live.market_poll_sec}с", callback_data="set:market_poll_sec"),
        ],
        [InlineKeyboardButton(text=f"Антидубль {live.alert_cooldown_hours}ч", callback_data="set:alert_cooldown_hours")],
        [InlineKeyboardButton(text="Обновить", callback_data="menu:refresh")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _menu_text(live: LiveFilters) -> str:
    lines = "\n".join(f"• {row}" for row in live.summary_lines())
    return (
        f"{e('gear')} <b>Админ-панель TG-Gifts</b>\n"
        f"{e('spark')} Настройки видны только вам.\n\n"
        f"{lines}\n\n"
        f"{e('chat')} Чат: <a href=\"https://t.me/BYRMALDAEVO\">BYRMALDAEVO</a>\n"
        f"{e('warn')} Ищем дешёвые лоты у новичков: мало NFT, рейтинг 0–2 или без рейтинга.\n"
        f"Нажмите кнопку, чтобы сменить значение."
    )


def _prompts() -> dict[str, str]:
    return {
        "floor_min_ton": "Мин. цена лота в TON (например 0 или 1.5)",
        "floor_max_ton": "Макс. цена лота в TON (например 10)",
        "min_unique_gifts": "Минимум unique NFT в профиле (целое)",
        "max_unique_gifts": "Максимум unique NFT в профиле (целое)",
        "stars_rating_min": "Мин. уровень Stars-рейтинга (0 = без рейтинга тоже ок)",
        "stars_rating_max": "Макс. уровень Stars-рейтинга (2 = новички, без коллекционеров)",
        "max_account_age_days": "Макс. возраст аккаунта в днях (0 = выключить фильтр)",
        "min_activity_score": "Мин. эвристика активности 0–100 (0 = выкл)",
        "market_poll_sec": "Пауза между кругами маркета, секунды (15–600)",
        "alert_cooldown_hours": "Часы антидубля алертов",
    }


async def _send_menu(target: Message, live: LiveFilters, *, edit: bool = False) -> None:
    html_text = _menu_text(live)
    kb = _kb(live)
    try:
        if edit:
            await target.edit_text(html_text, reply_markup=kb, disable_web_page_preview=True)
        else:
            await target.answer(html_text, reply_markup=kb, disable_web_page_preview=True)
        return
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        LOGGER.warning("HTML меню отклонено (%s), шлём plain", exc)
    plain = "Админ-панель TG-Gifts\n" + "\n".join(live.summary_lines()) + "\nhttps://t.me/BYRMALDAEVO"
    if edit:
        await target.edit_text(plain, reply_markup=kb, parse_mode=None, disable_web_page_preview=True)
    else:
        await target.answer(plain, reply_markup=kb, parse_mode=None, disable_web_page_preview=True)


def setup_admin(live: LiveFilters) -> Router:
    """Привязывает LiveFilters к роутеру (один экземпляр на процесс)."""

    @router.message(CommandStart(), IsAdmin())
    async def start_admin(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _send_menu(message, live)

    @router.message(CommandStart())
    async def start_public(message: Message) -> None:
        await message.answer(
            f"{e('gem')} Бот запущен.\n"
            f"Теперь в группе можно нажать «Занять лот» — карточка придёт сюда в личку.\n"
            f"{e('chat')} Чат: <a href=\"https://t.me/BYRMALDAEVO\">t.me/BYRMALDAEVO</a>",
            disable_web_page_preview=True,
        )

    @router.message(Command("admin"), IsAdmin())
    async def cmd_admin(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _send_menu(message, live)

    @router.message(Command("admin"))
    async def cmd_admin_denied(message: Message) -> None:
        await message.answer("Нет доступа.")

    @router.callback_query(F.data == "menu:refresh", IsAdmin())
    async def refresh(call: CallbackQuery) -> None:
        await call.answer()
        if isinstance(call.message, Message):
            await _send_menu(call.message, live, edit=True)

    @router.callback_query(F.data == "tgl:scanner", IsAdmin())
    async def toggle_scanner(call: CallbackQuery) -> None:
        live.update(scanner_enabled=not live.scanner_enabled)
        await call.answer("Сканер " + ("включён" if live.scanner_enabled else "выключен"))
        if isinstance(call.message, Message):
            await _send_menu(call.message, live, edit=True)

    @router.callback_query(F.data == "tgl:rating", IsAdmin())
    async def toggle_rating(call: CallbackQuery) -> None:
        live.update(require_stars_rating=not live.require_stars_rating)
        await call.answer()
        if isinstance(call.message, Message):
            await _send_menu(call.message, live, edit=True)

    @router.callback_query(F.data == "cycle:premium", IsAdmin())
    async def cycle_premium(call: CallbackQuery) -> None:
        order: list[Optional[bool]] = [None, True, False]
        idx = order.index(live.require_premium) if live.require_premium in order else 0
        live.update(require_premium=order[(idx + 1) % 3])
        await call.answer()
        if isinstance(call.message, Message):
            await _send_menu(call.message, live, edit=True)

    @router.callback_query(F.data.startswith("set:"), IsAdmin())
    async def ask_value(call: CallbackQuery, state: FSMContext) -> None:
        field = call.data.split(":", 1)[1]  # type: ignore[union-attr]
        await state.set_state(EditFilter.waiting_value)
        await state.update_data(field=field)
        prompt = _prompts().get(field, "Новое значение")
        await call.answer()
        await call.message.answer(f"{e('search')} {prompt}")  # type: ignore[union-attr]

    @router.message(EditFilter.waiting_value, IsAdmin())
    async def apply_value(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        field = data.get("field")
        raw = (message.text or "").strip().replace(",", ".")
        await state.clear()
        if not field:
            await message.answer("Сбой состояния, нажмите /admin")
            return
        try:
            parsed = _parse_field(field, raw)
            live.update(**{field: parsed})
        except (ValueError, KeyError) as exc:
            await message.answer(f"{e('warn')} Не принял значение: {exc}\n/admin")
            return
        await _send_menu(message, live)

    return router


def _parse_field(field: str, raw: str) -> object:
    if field in {"floor_min_ton", "floor_max_ton"}:
        value = float(raw)
        if value < 0:
            raise ValueError("цена не может быть отрицательной")
        return value
    if field == "max_account_age_days":
        days = int(float(raw))
        return None if days <= 0 else days
    if field in {
        "min_unique_gifts",
        "max_unique_gifts",
        "stars_rating_min",
        "stars_rating_max",
        "min_activity_score",
        "market_poll_sec",
        "alert_cooldown_hours",
    }:
        number = int(float(raw))
        if field == "market_poll_sec" and not 15 <= number <= 600:
            raise ValueError("poll 15–600")
        if field == "min_activity_score" and not 0 <= number <= 100:
            raise ValueError("активность 0–100")
        if number < 0:
            raise ValueError("нужно ≥ 0")
        return number
    raise KeyError(field)
