"""Админ-кнопки на TG-уведомлении о заявке на вывод реферальных рублей.

Заявки создаются из кабинета сайта (POST /cabinet/referral/withdrawals),
сумма уже зарезервирована. «Выплачено» фиксирует выплату, «Отклонить»
возвращает деньги на баланс. Пользователь получает кабинет-уведомление
(работает и для веб-юзеров без Telegram); при наличии telegram_id — ещё и DM.
"""

import logging

from aiogram import Dispatcher, F, types

from app.config import settings
from app.database.crud.user import get_user_by_id
from app.database.crud.withdrawal import get_withdrawal_by_id
from app.services import withdrawal_service

logger = logging.getLogger(__name__)


async def admin_paid_withdrawal(callback: types.CallbackQuery):
    if not settings.is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет прав", show_alert=True)
        return

    from app.database.database import AsyncSessionLocal

    withdrawal_id = int(callback.data.rsplit("_", 1)[-1])
    async with AsyncSessionLocal() as db:
        withdrawal = await get_withdrawal_by_id(db, withdrawal_id)
        if withdrawal is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        if not await withdrawal_service.mark_paid(
            db, withdrawal, admin_tg_id=callback.from_user.id,
        ):
            await callback.answer("Заявка уже обработана", show_alert=True)
            return
        wd_user = await get_user_by_id(db, withdrawal.user_id)

    try:
        await callback.message.edit_text(
            callback.message.html_text
            + f"\n\n💸 <b>Выплачено</b> (админ @{callback.from_user.username or callback.from_user.id})",
            parse_mode="HTML",
        )
    except Exception:
        pass

    if wd_user and wd_user.telegram_id:
        try:
            await callback.bot.send_message(
                wd_user.telegram_id,
                f"💸 Заявка №{withdrawal.id} на вывод {withdrawal.amount_kopeks / 100:.2f} ₽ выплачена.",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя о выплате №{withdrawal.id}: {e}")

    await callback.answer("Заявка отмечена выплаченной")


async def admin_reject_withdrawal(callback: types.CallbackQuery):
    if not settings.is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет прав", show_alert=True)
        return

    from app.database.database import AsyncSessionLocal

    withdrawal_id = int(callback.data.rsplit("_", 1)[-1])
    async with AsyncSessionLocal() as db:
        withdrawal = await get_withdrawal_by_id(db, withdrawal_id)
        if withdrawal is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        wd_user = await get_user_by_id(db, withdrawal.user_id)
        if wd_user is None:
            await callback.answer("Пользователь заявки не найден", show_alert=True)
            return
        if not await withdrawal_service.reject(
            db, withdrawal, wd_user, admin_tg_id=callback.from_user.id,
        ):
            await callback.answer("Заявка уже обработана", show_alert=True)
            return

    try:
        await callback.message.edit_text(
            callback.message.html_text
            + f"\n\n❌ <b>Отклонено, средства возвращены</b> (админ @{callback.from_user.username or callback.from_user.id})",
            parse_mode="HTML",
        )
    except Exception:
        pass

    if wd_user.telegram_id:
        try:
            await callback.bot.send_message(
                wd_user.telegram_id,
                f"❌ Заявка №{withdrawal.id} на вывод отклонена. "
                f"{withdrawal.amount_kopeks / 100:.2f} ₽ возвращены на баланс.",
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя об отклонении №{withdrawal.id}: {e}")

    await callback.answer("Заявка отклонена, средства возвращены")


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(admin_paid_withdrawal, F.data.startswith("admin_wd_paid_"))
    dp.callback_query.register(admin_reject_withdrawal, F.data.startswith("admin_wd_reject_"))
