"""Вывод реферальных рублей из кабинета сайта.

Заявочная схема: сумма списывается с баланса в момент создания заявки
(дебет + Transaction + строка заявки — один коммит), админ обрабатывает
из TG-уведомления кнопками «Выплачено» / «Отклонить»; отклонение
возвращает деньги на баланс. Автовыплат нет.

Доступно к выводу = min(баланс, заработано рефералкой − выплачено − в ожидании):
кап по заработанному не даёт выводить обычные пополнения, кап по балансу —
уходить в минус, если часть денег уже потрачена на подписку.
"""

import html
import logging
import re
from datetime import datetime
from typing import Any, Dict, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.referral import get_referral_earnings_sum
from app.database.crud.withdrawal import (
    create_withdrawal_request,
    get_user_withdrawal_sums,
    mark_withdrawal_paid,
    mark_withdrawal_rejected,
)
from app.database.models import (
    PaymentMethod,
    Transaction,
    TransactionType,
    User,
    WithdrawalRequest,
    WithdrawalStatus,
)

logger = logging.getLogger(__name__)

# TG-юзернейм (с @ или без) либо телефон — свободная строка реквизитов
# намеренно узкая: выплата ручная, админу нужен только контакт.
DETAILS_RE = re.compile(r"^@?[a-zA-Z0-9_+\-. ]{4,64}$")


class WithdrawalError(Exception):
    """Ошибка бизнес-валидации заявки. code — машиночитаемый detail для фронта."""

    def __init__(self, code: str, http_status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


async def get_withdrawal_summary(
    db: AsyncSession,
    user: User,
    *,
    earned_kopeks: Optional[int] = None,
) -> Dict[str, Any]:
    """Денежная сводка вывода. Единый источник для /referral и валидации заявки."""
    if earned_kopeks is None:
        earned_kopeks = await get_referral_earnings_sum(db, user.id) or 0

    sums = await get_user_withdrawal_sums(db, user.id)
    pending = sums["pending_kopeks"]
    withdrawn = sums["paid_kopeks"]

    available = max(0, min(user.balance_kopeks, earned_kopeks - withdrawn - pending))

    return {
        "available_kopeks": available,
        "available_rub": round(available / 100, 2),
        "pending_rub": round(pending / 100, 2),
        "withdrawn_rub": round(withdrawn / 100, 2),
        "min_rub": settings.REFERRAL_WITHDRAWAL_MIN_RUBLES,
        "enabled": bool(settings.REFERRAL_WITHDRAWAL_ENABLED),
    }


def build_withdrawal(wd: WithdrawalRequest) -> Dict[str, Any]:
    return {
        "id": f"wd_{wd.id}",
        "amount": round(wd.amount_kopeks / 100, 2),
        "status": wd.status,
        "details": wd.details,
        "createdAt": wd.created_at.isoformat() if wd.created_at else None,
        "processedAt": wd.processed_at.isoformat() if wd.processed_at else None,
    }


async def create_withdrawal(
    db: AsyncSession,
    user: User,
    *,
    amount_kopeks: int,
    details: str,
) -> WithdrawalRequest:
    """Создаёт заявку и резервирует сумму. Бросает WithdrawalError при отказе."""
    if not settings.REFERRAL_WITHDRAWAL_ENABLED:
        raise WithdrawalError("withdrawal_disabled", 403)

    details = (details or "").strip()
    if not DETAILS_RE.match(details):
        raise WithdrawalError("invalid_details", 422)

    if amount_kopeks < settings.REFERRAL_WITHDRAWAL_MIN_RUBLES * 100:
        raise WithdrawalError("below_minimum", 422)

    # Блокируем строку пользователя: два параллельных POST не должны оба
    # пройти проверку available. Пересчёт — внутри блокировки.
    locked_user = (
        await db.execute(
            select(User).where(User.id == user.id).with_for_update()
        )
    ).scalar_one()

    summary = await get_withdrawal_summary(db, locked_user)
    if amount_kopeks > summary["available_kopeks"]:
        raise WithdrawalError("insufficient_funds", 402)

    try:
        locked_user.balance_kopeks -= amount_kopeks
        locked_user.updated_at = datetime.utcnow()

        debit_tx = Transaction(
            user_id=locked_user.id,
            type=TransactionType.WITHDRAWAL.value,
            amount_kopeks=amount_kopeks,
            description="Заявка на вывод реферальных средств",
            payment_method=PaymentMethod.MANUAL.value,
            is_completed=True,
            completed_at=datetime.utcnow(),
        )
        db.add(debit_tx)
        await db.flush()

        withdrawal = await create_withdrawal_request(
            db,
            user_id=locked_user.id,
            amount_kopeks=amount_kopeks,
            details=details,
            debit_transaction_id=debit_tx.id,
            commit=False,
        )
        await db.commit()
    except WithdrawalError:
        raise
    except Exception:
        await db.rollback()
        raise

    await db.refresh(withdrawal)
    await db.refresh(user)

    logger.info(
        "💸 Заявка на вывод №%s: %s₽, пользователь %s",
        withdrawal.id, amount_kopeks / 100, locked_user.id,
    )

    await _notify_admins_new_withdrawal(locked_user, withdrawal)
    return withdrawal


async def mark_paid(
    db: AsyncSession,
    withdrawal: WithdrawalRequest,
    *,
    admin_tg_id: Optional[int] = None,
) -> bool:
    """Помечает заявку выплаченной. False — уже обработана (идемпотентность)."""
    if withdrawal.status != WithdrawalStatus.PENDING.value:
        return False

    await mark_withdrawal_paid(db, withdrawal, processed_by=admin_tg_id)
    logger.info("✅ Заявка на вывод №%s выплачена (админ %s)", withdrawal.id, admin_tg_id)

    await _notify_user(db, withdrawal, paid=True)
    return True


async def reject(
    db: AsyncSession,
    withdrawal: WithdrawalRequest,
    user: User,
    *,
    admin_tg_id: Optional[int] = None,
) -> bool:
    """Отклоняет заявку и возвращает деньги на баланс. False — уже обработана."""
    if withdrawal.status != WithdrawalStatus.PENDING.value:
        return False

    try:
        user.balance_kopeks += withdrawal.amount_kopeks
        user.updated_at = datetime.utcnow()

        refund_tx = Transaction(
            user_id=user.id,
            type=TransactionType.REFUND.value,
            amount_kopeks=withdrawal.amount_kopeks,
            description=f"Возврат: заявка на вывод №{withdrawal.id} отклонена",
            payment_method=PaymentMethod.MANUAL.value,
            is_completed=True,
            completed_at=datetime.utcnow(),
        )
        db.add(refund_tx)
        await db.flush()

        await mark_withdrawal_rejected(
            db, withdrawal,
            processed_by=admin_tg_id,
            refund_transaction_id=refund_tx.id,
            commit=False,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    await db.refresh(withdrawal)
    await db.refresh(user)

    logger.info(
        "↩️ Заявка на вывод №%s отклонена (админ %s), возвращено %s₽",
        withdrawal.id, admin_tg_id, withdrawal.amount_kopeks / 100,
    )

    await _notify_user(db, withdrawal, paid=False)
    return True


async def _notify_user(db: AsyncSession, withdrawal: WithdrawalRequest, *, paid: bool) -> None:
    """Кабинет-уведомление о статусе заявки — работает и для веб-юзеров без TG."""
    try:
        from app.services.cabinet_notification_service import (
            CabinetNotificationType,
            notify,
        )

        await notify(
            db,
            user_id=withdrawal.user_id,
            type=(
                CabinetNotificationType.WITHDRAWAL_PAID
                if paid else CabinetNotificationType.WITHDRAWAL_REJECTED
            ),
            payload={
                "withdrawalId": withdrawal.id,
                "amountKopeks": withdrawal.amount_kopeks,
            },
        )
    except Exception as exc:
        logger.warning(
            "Не удалось создать кабинет-уведомление по заявке №%s: %s",
            withdrawal.id, exc,
        )


async def _notify_admins_new_withdrawal(user: User, withdrawal: WithdrawalRequest) -> None:
    """TG-уведомление админ-чату с кнопками «Выплачено» / «Отклонить».

    Best-effort: сбой Telegram не валит заявку — деньги уже зарезервированы,
    заявка видна в БД и в истории кабинета.
    """
    if not settings.BOT_TOKEN:
        return

    bot: Optional[Bot] = None
    try:
        from app.services.admin_notification_service import AdminNotificationService

        username = f"@{user.username}" if user.username else "—"
        text = (
            "💸 <b>НОВАЯ ЗАЯВКА НА ВЫВОД</b>\n\n"
            f"💰 Сумма: <b>{withdrawal.amount_kopeks / 100:.2f} ₽</b>\n"
            f"🧾 Заявка: <b>№{withdrawal.id}</b>\n"
            f"📮 Реквизиты: <code>{html.escape(withdrawal.details)}</code>\n\n"
            f"👤 Пользователь: {html.escape(user.full_name or '')}\n"
            f"🆔 Telegram ID: <code>{user.telegram_id}</code>\n"
            f"📱 Username: {html.escape(username)}\n"
            f"📧 Email: {html.escape(user.email or '—')}\n\n"
            "Сумма уже зарезервирована (списана с баланса)."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="💸 Выплачено",
                callback_data=f"admin_wd_paid_{withdrawal.id}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить (вернуть деньги)",
                callback_data=f"admin_wd_reject_{withdrawal.id}",
            ),
        ]])

        bot = Bot(token=settings.BOT_TOKEN)
        await AdminNotificationService(bot)._send_message(text, reply_markup=keyboard)
    except Exception as exc:
        logger.error("Не удалось уведомить админов о заявке на вывод №%s: %s", withdrawal.id, exc)
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                pass
