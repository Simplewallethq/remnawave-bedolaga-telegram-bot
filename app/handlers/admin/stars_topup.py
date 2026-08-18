from __future__ import annotations

import html
import logging
import secrets
from dataclasses import dataclass
from decimal import Decimal

from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id
from app.database.models import BotStarsTopup, User
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = logging.getLogger(__name__)

ADMIN_STARS_PAYLOAD_PREFIX = "admin_stars_topup"
MIN_STARS_AMOUNT = 5
MAX_STARS_AMOUNT = 100_000
PRESET_AMOUNTS = (100, 500, 1_000, 5_000)


@dataclass(frozen=True)
class AdminStarsPayload:
    bot_id: int
    admin_id: int
    stars_amount: int
    nonce: str


def build_admin_stars_payload(*, bot_id: int, admin_id: int, stars_amount: int) -> str:
    if not MIN_STARS_AMOUNT <= stars_amount <= MAX_STARS_AMOUNT:
        raise ValueError("Stars amount is outside Telegram limits")

    nonce = secrets.token_hex(8)
    payload = (
        f"{ADMIN_STARS_PAYLOAD_PREFIX}:{bot_id}:{admin_id}:{stars_amount}:{nonce}"
    )
    if len(payload.encode("utf-8")) > 128:
        raise ValueError("Invoice payload is too long")
    return payload


def parse_admin_stars_payload(payload: str | None) -> AdminStarsPayload | None:
    if not payload:
        return None

    parts = payload.split(":")
    if len(parts) != 5 or parts[0] != ADMIN_STARS_PAYLOAD_PREFIX:
        return None

    try:
        bot_id = int(parts[1])
        admin_id = int(parts[2])
        stars_amount = int(parts[3])
    except (TypeError, ValueError):
        return None

    nonce = parts[4]
    if (
        bot_id <= 0
        or admin_id <= 0
        or not MIN_STARS_AMOUNT <= stars_amount <= MAX_STARS_AMOUNT
        or len(nonce) != 16
        or any(char not in "0123456789abcdef" for char in nonce)
    ):
        return None

    return AdminStarsPayload(
        bot_id=bot_id,
        admin_id=admin_id,
        stars_amount=stars_amount,
        nonce=nonce,
    )


def _format_star_amount(value) -> str:
    amount = int(getattr(value, "amount", 0) or 0)
    nanostar_amount = int(getattr(value, "nanostar_amount", 0) or 0)
    total = Decimal(amount) + (Decimal(nanostar_amount) / Decimal(1_000_000_000))
    rendered = format(total, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


async def _get_bot_stars_balance(bot: Bot) -> str | None:
    try:
        return _format_star_amount(await bot.get_my_star_balance())
    except Exception as error:
        logger.warning("Не удалось получить Stars-баланс бота %s: %s", bot.id, error)
        return None


def _topup_keyboard(*, manual_mode: bool = False) -> InlineKeyboardMarkup:
    if manual_mode:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data="admin_stars_topup",
                    )
                ]
            ]
        )

    rows = [
        [
            InlineKeyboardButton(
                text=f"{PRESET_AMOUNTS[0]} ⭐",
                callback_data=f"admin_stars_topup_amount:{PRESET_AMOUNTS[0]}",
            ),
            InlineKeyboardButton(
                text=f"{PRESET_AMOUNTS[1]} ⭐",
                callback_data=f"admin_stars_topup_amount:{PRESET_AMOUNTS[1]}",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{PRESET_AMOUNTS[2]} ⭐",
                callback_data=f"admin_stars_topup_amount:{PRESET_AMOUNTS[2]}",
            ),
            InlineKeyboardButton(
                text=f"{PRESET_AMOUNTS[3]} ⭐",
                callback_data=f"admin_stars_topup_amount:{PRESET_AMOUNTS[3]}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="✍️ Другая сумма",
                callback_data="admin_stars_topup_custom",
            )
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_topup_menu(callback: types.CallbackQuery) -> None:
    balance = await _get_bot_stars_balance(callback.bot)
    balance_text = f"{balance} ⭐" if balance is not None else "не удалось получить"
    bot_user = await callback.bot.get_me()
    bot_name = f"@{bot_user.username}" if bot_user.username else str(bot_user.id)

    await callback.message.edit_text(
        "⭐ <b>Пополнение Stars-баланса бота</b>\n\n"
        f"Бот: {bot_name}\n"
        f"Текущий баланс: <b>{balance_text}</b>\n\n"
        "Выберите количество Stars. После этого бот выставит вам счёт в Telegram. "
        "Рублёвый баланс пользователя при такой оплате не изменяется.",
        reply_markup=_topup_keyboard(),
        parse_mode="HTML",
    )


async def _send_topup_invoice(message: types.Message, *, admin_id: int, amount: int) -> None:
    payload = build_admin_stars_payload(
        bot_id=message.bot.id,
        admin_id=admin_id,
        stars_amount=amount,
    )
    nonce = payload.rsplit(":", 1)[-1]

    await message.answer_invoice(
        title="Пополнение Stars-баланса",
        description=(
            f"Перевод {amount} Telegram Stars на баланс этого бота "
            "для оплаты его служебных функций."
        ),
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label="Пополнение бота", amount=amount)],
        provider_token="",
        start_parameter=f"admin_stars_{nonce}",
        protect_content=True,
    )


@admin_required
@error_handler
async def show_stars_topup(
    callback: types.CallbackQuery,
    state: FSMContext,
    **kwargs,
) -> None:
    await state.clear()
    await _render_topup_menu(callback)
    await callback.answer()


@admin_required
@error_handler
async def select_stars_topup_amount(
    callback: types.CallbackQuery,
    state: FSMContext,
    **kwargs,
) -> None:
    try:
        amount = int(callback.data.rsplit(":", 1)[-1])
        if not MIN_STARS_AMOUNT <= amount <= MAX_STARS_AMOUNT:
            raise ValueError
    except (TypeError, ValueError):
        await callback.answer("Некорректное количество Stars", show_alert=True)
        return

    await state.clear()
    await _send_topup_invoice(
        callback.message,
        admin_id=callback.from_user.id,
        amount=amount,
    )
    await callback.answer("Счёт создан")


@admin_required
@error_handler
async def request_custom_stars_amount(
    callback: types.CallbackQuery,
    state: FSMContext,
    **kwargs,
) -> None:
    await state.set_state(AdminStates.waiting_for_stars_topup_amount)
    await callback.message.edit_text(
        "✍️ <b>Введите количество Stars</b>\n\n"
        f"Допустимое значение: от {MIN_STARS_AMOUNT} до {MAX_STARS_AMOUNT}.",
        reply_markup=_topup_keyboard(manual_mode=True),
        parse_mode="HTML",
    )
    await callback.answer()


@admin_required
@error_handler
async def process_custom_stars_amount(
    message: types.Message,
    state: FSMContext,
    **kwargs,
) -> None:
    try:
        amount = int((message.text or "").strip())
    except (TypeError, ValueError):
        amount = 0

    if not MIN_STARS_AMOUNT <= amount <= MAX_STARS_AMOUNT:
        await message.answer(
            f"❌ Введите целое число от {MIN_STARS_AMOUNT} до {MAX_STARS_AMOUNT}."
        )
        return

    await state.clear()
    await _send_topup_invoice(message, admin_id=message.from_user.id, amount=amount)


def _payment_matches_payload(
    parsed: AdminStarsPayload | None,
    *,
    payer_id: int,
    bot_id: int,
    currency: str,
    total_amount: int,
) -> bool:
    return bool(
        parsed
        and parsed.admin_id == payer_id
        and parsed.bot_id == bot_id
        and parsed.stars_amount == total_amount
        and currency == "XTR"
    )


async def handle_admin_stars_pre_checkout(query: types.PreCheckoutQuery) -> None:
    parsed = parse_admin_stars_payload(query.invoice_payload)
    valid = _payment_matches_payload(
        parsed,
        payer_id=query.from_user.id,
        bot_id=query.bot.id,
        currency=query.currency,
        total_amount=query.total_amount,
    )
    if not valid or not settings.is_admin(query.from_user.id):
        logger.warning(
            "Отклонён admin Stars pre-checkout: payer=%s bot=%s amount=%s",
            query.from_user.id,
            query.bot.id,
            query.total_amount,
        )
        await query.answer(
            ok=False,
            error_message="Этот счёт может оплатить только администратор, которому он выставлен.",
        )
        return

    await query.answer(ok=True)


async def _record_successful_topup(
    db: AsyncSession,
    *,
    admin_user_id: int | None,
    admin_telegram_id: int,
    bot_id: int,
    stars_amount: int,
    invoice_payload: str,
    telegram_payment_charge_id: str,
    provider_payment_charge_id: str | None,
) -> tuple[BotStarsTopup, bool]:
    result = await db.execute(
        select(BotStarsTopup).where(
            BotStarsTopup.telegram_payment_charge_id == telegram_payment_charge_id
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing, False

    record = BotStarsTopup(
        admin_user_id=admin_user_id,
        admin_telegram_id=admin_telegram_id,
        bot_id=bot_id,
        stars_amount=stars_amount,
        invoice_payload=invoice_payload,
        telegram_payment_charge_id=telegram_payment_charge_id,
        provider_payment_charge_id=provider_payment_charge_id or None,
    )
    db.add(record)
    try:
        await db.commit()
        await db.refresh(record)
        return record, True
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(BotStarsTopup).where(
                BotStarsTopup.telegram_payment_charge_id == telegram_payment_charge_id
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing, False
        raise


async def handle_admin_stars_successful_payment(
    message: types.Message,
    db: AsyncSession,
    db_user: User | None = None,
    **kwargs,
) -> None:
    payment = message.successful_payment
    parsed = parse_admin_stars_payload(payment.invoice_payload)
    valid = _payment_matches_payload(
        parsed,
        payer_id=message.from_user.id,
        bot_id=message.bot.id,
        currency=payment.currency,
        total_amount=payment.total_amount,
    )
    if db_user is None:
        db_user = await get_user_by_telegram_id(db, message.from_user.id)

    if not valid:
        record, created = await _record_successful_topup(
            db,
            admin_user_id=db_user.id if db_user else None,
            admin_telegram_id=message.from_user.id,
            bot_id=message.bot.id,
            stars_amount=payment.total_amount,
            invoice_payload=payment.invoice_payload,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
            provider_payment_charge_id=getattr(
                payment, "provider_payment_charge_id", None
            ),
        )
        logger.error(
            "Получен Stars-платёж с некорректным admin payload: payer=%s charge=%s created=%s",
            message.from_user.id,
            record.telegram_payment_charge_id,
            created,
        )
        await message.answer(
            "⚠️ Stars поступили боту, но данные платежа не прошли проверку. "
            "Сохраните чек и обратитесь к разработчику."
        )
        return

    record, created = await _record_successful_topup(
        db,
        admin_user_id=db_user.id if db_user else None,
        admin_telegram_id=message.from_user.id,
        bot_id=message.bot.id,
        stars_amount=payment.total_amount,
        invoice_payload=payment.invoice_payload,
        telegram_payment_charge_id=payment.telegram_payment_charge_id,
        provider_payment_charge_id=getattr(
            payment, "provider_payment_charge_id", None
        ),
    )

    if not created:
        logger.info(
            "Повторный successful_payment для admin Stars topup %s",
            record.telegram_payment_charge_id,
        )
        await message.answer("ℹ️ Это пополнение уже было учтено ранее.")
        return

    balance = await _get_bot_stars_balance(message.bot)
    balance_line = (
        f"\nТекущий баланс бота: <b>{balance} ⭐</b>" if balance is not None else ""
    )
    admin_warning = "" if settings.is_admin(message.from_user.id) else (
        "\n\n⚠️ Права администратора изменились после выставления счёта."
    )
    await message.answer(
        "✅ <b>Stars-баланс бота пополнен</b>\n\n"
        f"Зачислено: <b>{payment.total_amount} ⭐</b>"
        f"{balance_line}\n"
        f"ID платежа: <code>{html.escape(payment.telegram_payment_charge_id)}</code>"
        f"{admin_warning}",
        parse_mode="HTML",
    )
    logger.info(
        "Admin %s пополнил Stars-баланс бота %s на %s; charge=%s",
        message.from_user.id,
        message.bot.id,
        payment.total_amount,
        payment.telegram_payment_charge_id,
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_stars_topup, F.data == "admin_stars_topup")
    dp.callback_query.register(
        select_stars_topup_amount,
        F.data.startswith("admin_stars_topup_amount:"),
    )
    dp.callback_query.register(
        request_custom_stars_amount,
        F.data == "admin_stars_topup_custom",
    )
    dp.message.register(
        process_custom_stars_amount,
        AdminStates.waiting_for_stars_topup_amount,
    )
    dp.pre_checkout_query.register(
        handle_admin_stars_pre_checkout,
        F.invoice_payload.startswith(f"{ADMIN_STARS_PAYLOAD_PREFIX}:"),
    )
    dp.message.register(
        handle_admin_stars_successful_payment,
        F.successful_payment.invoice_payload.startswith(
            f"{ADMIN_STARS_PAYLOAD_PREFIX}:"
        ),
    )
