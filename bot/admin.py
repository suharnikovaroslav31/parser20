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

from bot.emoji import e, kb_icon
from config import get_settings
from core.runtime import BUILD, LiveFilters

LOGGER = logging.getLogger("tg_gifts.admin")
router = Router(name="admin")


def _admin_id() -> int:
    try:
        return int(get_settings().admin_id)
    except Exception:
        return 8810737152


def _community(live: LiveFilters) -> str:
    return (live.community_url or "https://t.me/GGsel_deal").strip()


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return user is not None and user.id == _admin_id()


class EditFilter(StatesGroup):
    waiting_value = State()


def _btn(text: str, callback: str, key: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback, **kb_icon(key))


def _kb(live: LiveFilters) -> InlineKeyboardMarkup:
    on = "ON" if live.scanner_enabled else "OFF"
    age = "выкл" if not live.filter_seller_age or not live.max_account_age_days else str(live.max_account_age_days)
    prem = {True: "да", False: "нет", None: "любой"}[live.require_premium]
    rating_req = "да" if live.require_stars_rating else "нет"
    rows = [
        [_btn(f"Сканер {on}", "tgl:scanner", "lightning" if live.scanner_enabled else "no")],
        [
            _btn(f"Цена мин {live.floor_min_ton:g}", "set:floor_min_ton", "ton"),
            _btn(f"Цена макс {live.floor_max_ton:g}", "set:floor_max_ton", "ton"),
        ],
        [
            _btn(f"NFT мин {live.min_unique_gifts}", "set:min_unique_gifts", "gift"),
            _btn(f"NFT макс {live.max_unique_gifts}", "set:max_unique_gifts", "gift"),
        ],
        [
            _btn(f"Рейтинг мин {live.stars_rating_min}", "set:stars_rating_min", "star"),
            _btn(f"Рейтинг макс {live.stars_rating_max}", "set:stars_rating_max", "star"),
        ],
        [_btn(f"Рейтинг обязателен: {rating_req}", "tgl:rating", "star")],
        [_btn(f"Лох-профиль: {'да' if live.require_noob_profile else 'нет'}", "tgl:noob", "user")],
        [_btn(f"Только русские: {'да' if live.require_russian else 'нет'}", "tgl:russian", "user")],
        [
            _btn(f"Возраст дн. {age}", "set:max_account_age_days", "chart"),
            _btn(f"Premium {prem}", "cycle:premium", "spark"),
        ],
        [
            _btn(f"Активность ≥ {live.min_activity_score}", "set:min_activity_score", "chart"),
            _btn(
                f"Живость {'выкл' if live.max_activity_score <= 0 else f'≤ {live.max_activity_score}'}",
                "set:max_activity_score",
                "chart",
            ),
        ],
        [
            _btn(
                f"Обычных гифтов {'любое' if live.max_regular_gifts <= 0 else f'≤ {live.max_regular_gifts}'}",
                "set:max_regular_gifts",
                "gift",
            ),
            _btn(f"Круг {live.market_poll_sec}с", "set:market_poll_sec", "lightning"),
        ],
        [_btn(f"Антидубль {live.alert_cooldown_hours}ч", "set:alert_cooldown_hours", "bell")],
        [_btn("Обновить", "menu:refresh", "lightning")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _menu_text(live: LiveFilters) -> str:
    lines = "\n".join(f"{e('list')} {row}" for row in live.summary_lines())
    return (
        f"{e('gear')} <b>Админ-панель TG-Gifts</b>\n"
        f"{e('spark')} Сборка <code>{BUILD}</code>. Настройки видны только вам.\n\n"
        f"{lines}\n\n"
        f"{e('chat')} Гарант: <a href=\"{_community(live)}\">GGsel_deal</a>\n"
        f"{e('warn')} Лох = новичок: ур.1, бедный профиль, 1–2 NFT, цена явно мимо флора. Кто шарит / у флора / флиппер — skip.\n"
        f"Нажмите кнопку, чтобы сменить значение."
    )


def _prompts() -> dict[str, str]:
    return {
        "floor_min_ton": "Мин. цена лота в TON (например 0 или 1.5)",
        "floor_max_ton": "Макс. цена лота в TON (например 10)",
        "min_unique_gifts": "Минимум unique NFT в профиле (целое)",
        "max_unique_gifts": "Максимум unique NFT в профиле (целое, сейчас 2)",
        "stars_rating_min": "Мин. уровень Stars-рейтинга (1 = новичок)",
        "stars_rating_max": "Макс. уровень Stars-рейтинга (1 = только первый уровень)",
        "max_account_age_days": "Макс. возраст аккаунта в днях (0 = выключить фильтр)",
        "min_activity_score": "Мин. эвристика активности 0–100 (0 = выкл)",
        "max_activity_score": "Макс. живость профиля 0–100 (0 = не режем по живости)",
        "max_regular_gifts": "Макс. обычных гифтов на витрине (0 = не режем)",
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
    plain = "Админ-панель TG-Gifts\n" + "\n".join(live.summary_lines()) + f"\n{_community(live)}"
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
            f"{e('check')} Бот запущен.\n"
            f"Теперь в группе можно нажать «Занять лот» — карточка придёт сюда в личку.\n"
            f"{e('chat')} Гарант: <a href=\"https://t.me/GGsel_deal\">t.me/GGsel_deal</a>",
            disable_web_page_preview=True,
        )

    @router.message(Command("admin"), IsAdmin())
    async def cmd_admin(message: Message, state: FSMContext) -> None:
        await state.clear()
        await _send_menu(message, live)

    @router.message(Command("build"), IsAdmin())
    async def cmd_build(message: Message) -> None:
        await message.answer(f"сборка <code>{BUILD}</code>")

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

    @router.callback_query(F.data == "tgl:noob", IsAdmin())
    async def toggle_noob(call: CallbackQuery) -> None:
        live.update(require_noob_profile=not live.require_noob_profile)
        await call.answer("Лох-профиль " + ("вкл" if live.require_noob_profile else "выкл"))
        if isinstance(call.message, Message):
            await _send_menu(call.message, live, edit=True)

    @router.callback_query(F.data == "tgl:russian", IsAdmin())
    async def toggle_russian(call: CallbackQuery) -> None:
        live.update(require_russian=not live.require_russian)
        await call.answer("Только русские " + ("вкл" if live.require_russian else "выкл"))
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
        "max_activity_score",
        "max_regular_gifts",
        "market_poll_sec",
        "alert_cooldown_hours",
    }:
        number = int(float(raw))
        if field == "market_poll_sec" and not 15 <= number <= 600:
            raise ValueError("poll 15–600")
        if field in {"min_activity_score", "max_activity_score"} and not 0 <= number <= 100:
            raise ValueError("активность 0–100")
        if number < 0:
            raise ValueError("нужно ≥ 0")
        return number
    raise KeyError(field)
