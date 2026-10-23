"""Платформа владельцев: заработок, кошелёк TRC-20 и заявки на вывод."""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.owner import (
    back_keyboard,
    cancel_keyboard,
    earnings_keyboard,
    home_keyboard,
    payout_confirm_keyboard,
    wallet_saved_keyboard,
)
from app.services import owner_platform_service as platform
from app.states import OwnerStates
from app.utils.tron import is_trc20_address, mask_address

from .common import answer_screen, language_of, t

logger = logging.getLogger(__name__)


def _rub(kopeks: int) -> str:
    rubles = Decimal(kopeks) / Decimal(100)
    text = f"{rubles:,.2f}".replace(",", " ")
    return text[:-3] if text.endswith(".00") else text


async def show_earnings(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    await state.clear()
    await callback.answer()
    language = language_of(db_user)
    owner = await platform.get_or_create_owner(db, db_user.id)
    await db.commit()
    earnings = await platform.owner_earnings(db, owner)
    text = t(
        language,
        "OWNER_EARNINGS_TEXT",
        "Оборот всего: <b>{turnover_total}₽</b>\n"
        "Оборот за вчера: <b>{turnover_yesterday}₽</b>\n"
        "Прибыль всего ({percent}% от оборота): <b>{profit_total}₽</b>\n"
        "Прибыль за вчера: <b>{profit_yesterday}₽</b>\n\n"
        "Доступный баланс: <b>{available}₽</b> (продажи старше {hold} дней, за вычетом выплаченного)",
        turnover_total=_rub(earnings.turnover_total),
        turnover_yesterday=_rub(earnings.turnover_yesterday),
        percent=earnings.revshare_percent,
        profit_total=_rub(earnings.profit_total),
        profit_yesterday=_rub(earnings.profit_yesterday),
        available=_rub(earnings.available_kopeks),
        hold=settings.OWNER_PAYOUT_HOLD_DAYS,
    )
    await answer_screen(callback, text, earnings_keyboard(language))


# ---------------------------------------------------------------- кошелёк


async def show_wallet(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    await callback.answer()
    language = language_of(db_user)
    owner = await platform.get_or_create_owner(db, db_user.id)
    await db.commit()
    if owner.trc20_address:
        text = t(
            language,
            "OWNER_WALLET_CURRENT",
            "Текущий кошелёк: {wallet} (TRON TRC-20). Чтобы заменить, пришли новый адрес.",
            wallet=mask_address(owner.trc20_address),
        )
    else:
        text = t(
            language,
            "OWNER_WALLET_PROMPT",
            "Введи кошелек для вывода заработка. Это должен быть кошелек в сети TRON TRC-20.",
        )
    await state.set_state(OwnerStates.waiting_for_wallet)
    await answer_screen(callback, text, back_keyboard(language, "wl_earn"))


async def handle_wallet(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    address = (message.text or "").strip()
    if not is_trc20_address(address):
        await message.answer(
            t(
                language,
                "OWNER_WALLET_INVALID",
                "Это не адрес TRON (TRC-20). Проверь, что копируешь адрес для USDT в сети TRON.",
            ),
            reply_markup=back_keyboard(language, "wl_wallet"),
        )
        return
    owner = await platform.get_or_create_owner(db, db_user.id)
    owner.trc20_address = address
    await db.commit()
    await state.clear()
    await message.answer(
        t(
            language,
            "OWNER_WALLET_SAVED",
            "Кошелек {wallet} в сети TRON TRC-20 добавлен в твой профиль.",
            wallet=mask_address(address),
        ),
        reply_markup=wallet_saved_keyboard(language),
    )


# ---------------------------------------------------------------- вывод


async def start_payout(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    owner = await platform.get_or_create_owner(db, db_user.id)
    await db.commit()
    if not owner.trc20_address:
        await show_wallet(callback, state, db, db_user)
        return
    rate = await platform.get_payout_rate()
    if rate is None:
        await callback.answer(
            t(language, "OWNER_RATE_UNAVAILABLE", "Курс ЦБ сейчас недоступен, попробуй позже."),
            show_alert=True,
        )
        return
    await callback.answer()
    earnings = await platform.owner_earnings(db, owner)
    available_usdt = platform.rub_to_usdt(earnings.available_kopeks, rate.rate)
    await state.set_state(OwnerStates.waiting_for_payout_amount)
    await state.set_data({"wl_cbr_rate": str(rate.cbr_rate), "wl_rate": str(rate.rate)})
    text = t(
        language,
        "OWNER_PAYOUT_PROMPT",
        "Доступно к выводу: <b>{usdt} USDT</b> — {rub}₽ по курсу вывода {rate}. Введи сумму в USDT для вывода.",
        usdt=available_usdt,
        rub=_rub(earnings.available_kopeks),
        rate=rate.rate.quantize(Decimal("0.01")),
    )
    await answer_screen(callback, text, cancel_keyboard(language, "wl_earn"))


def _rate_from_state(data: dict) -> platform.PayoutRate | None:
    try:
        return platform.PayoutRate(cbr_rate=Decimal(data["wl_cbr_rate"]), rate=Decimal(data["wl_rate"]))
    except (KeyError, ArithmeticError, ValueError):
        return None


async def handle_payout_amount(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    rate = _rate_from_state(await state.get_data())
    owner = await platform.get_or_create_owner(db, db_user.id)
    if rate is None or not owner.trc20_address:
        await state.clear()
        await message.answer(
            t(language, "OWNER_PAYOUT_RESTART", "Начни вывод заново."), reply_markup=back_keyboard(language, "wl_earn")
        )
        return
    amount = platform.parse_usdt(message.text or "")
    if amount is None:
        await message.answer(
            t(language, "OWNER_PAYOUT_NOT_NUMBER", "Ошибка. Введи сумму числом, например 50."),
            reply_markup=cancel_keyboard(language, "wl_earn"),
        )
        return
    earnings = await platform.owner_earnings(db, owner)
    error = platform.payout_amount_error(amount, platform.rub_to_usdt(earnings.available_kopeks, rate.rate))
    if error == "OWNER_PAYOUT_TOO_MUCH":
        await message.answer(
            t(language, error, "Ошибка. Введенная сумма больше доступной к выводу."),
            reply_markup=back_keyboard(language, "wl_payout"),
        )
        return
    if error == "OWNER_PAYOUT_BELOW_FEE":
        await message.answer(
            t(
                language,
                error,
                "Ошибка. Сумма должна быть больше комиссии ({fee} USDT).",
                fee=platform.payout_fee().normalize(),
            ),
            reply_markup=cancel_keyboard(language, "wl_earn"),
        )
        return

    fee = platform.payout_fee()
    await state.update_data(wl_amount=str(amount))
    await message.answer(
        t(
            language,
            "OWNER_PAYOUT_CONFIRM",
            "Будет отправлено <b>{send} USDT</b> ({amount} − комиссия {fee} USDT) на {wallet} (TRC-20). "
            "Действие нельзя отменить.\n\nСрок вывода: 48ч с момента оформления заявки.",
            send=amount - fee,
            amount=amount,
            fee=fee.normalize(),
            wallet=mask_address(owner.trc20_address),
        ),
        reply_markup=payout_confirm_keyboard(language),
        parse_mode="HTML",
    )


async def confirm_payout(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    data = await state.get_data()
    rate = _rate_from_state(data)
    amount = platform.parse_usdt(data.get("wl_amount") or "")
    await state.clear()
    if rate is None or amount is None:
        await callback.answer(t(language, "OWNER_PAYOUT_RESTART", "Начни вывод заново."), show_alert=True)
        return
    owner = await platform.get_or_create_owner(db, db_user.id)
    await db.commit()
    payout = await platform.create_payout(db, owner.id, amount, rate)
    if payout is None:
        await callback.answer(
            t(language, "OWNER_PAYOUT_TOO_MUCH", "Ошибка. Введенная сумма больше доступной к выводу."),
            show_alert=True,
        )
        return
    await callback.answer()
    await platform.notify_admins_new_payout(db_user, payout)
    await answer_screen(
        callback,
        t(language, "OWNER_PAYOUT_ACCEPTED", "✅ Заявка принята. Срок вывода: 48ч с момента оформления заявки."),
        home_keyboard(language),
    )


# ---------------------------------------------------------------- админ


async def admin_process_payout(callback: types.CallbackQuery):
    """Кнопки в админ-чате (приходят в основной бот): выплачено / отклонено."""
    if not settings.is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет прав", show_alert=True)
        return
    from app.database.database import AsyncSessionLocal
    from app.utils.bot_registry import get_bot_instance

    paid = callback.data.startswith("admin_owner_payout_paid_")
    payout_id = int(callback.data.rsplit("_", 1)[-1])
    async with AsyncSessionLocal() as db:
        payout = await platform.process_payout(db, payout_id, paid=paid, admin_tg_id=callback.from_user.id)
        if payout is None:
            await callback.answer("Заявка уже обработана или не найдена", show_alert=True)
            return
        recipient = await platform.payout_recipient(db, payout)

    admin = f"@{callback.from_user.username}" if callback.from_user.username else str(callback.from_user.id)
    mark = "💸 <b>Выплачено</b>" if paid else "❌ <b>Отклонено, сумма вернулась в доступный баланс</b>"
    try:
        await callback.message.edit_text(
            callback.message.html_text + f"\n\n{mark} (админ {admin})", parse_mode="HTML"
        )
    except Exception:  # noqa: BLE001
        pass

    # Владелец узнаёт о выплате в конструкторе; не открывал его — в боте-платформе.
    from app.constructor import constructor_runtime

    senders = [constructor_runtime.bot, get_bot_instance(settings.OWNER_PLATFORM_BOT_ID)]
    if recipient is not None:
        to_send = Decimal(payout.amount_usdt) - Decimal(payout.fee_usdt)
        text = (
            f"💸 Заявка №{payout.id} на вывод {to_send} USDT выплачена."
            if paid
            else f"❌ Заявка №{payout.id} на вывод отклонена, сумма вернулась в доступный баланс."
        )
        for sender in filter(None, senders):
            try:
                await sender.send_message(recipient.telegram_id, text)
                break
            except Exception as error:  # noqa: BLE001
                logger.warning("Не удалось уведомить владельца о заявке №%s из бота %s: %s", payout.id, sender.id, error)
    await callback.answer("Готово")
