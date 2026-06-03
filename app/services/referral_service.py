import logging
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram import Bot

from app.config import settings
from app.database.crud.user import add_user_balance, get_user_by_id
from app.database.crud.referral import create_referral_earning
from app.utils.user_utils import get_effective_referral_commission_percent

logger = logging.getLogger(__name__)

# Порог суммарных пополнений реферала, после превышения которого он засчитывается
# в счётчик "качественных" рефералов партнёра (1000₽). Используется для будущей
# партнёрской программы и системы достижений.
REFERRAL_QUALIFIED_TOPUP_THRESHOLD_KOPEKS = 100_000


async def send_referral_notification(
    bot: Bot,
    user_id: int,
    message: str
):
    try:
        await bot.send_message(user_id, message, parse_mode="HTML")
        logger.info(f"✅ Уведомление отправлено пользователю {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки уведомления пользователю {user_id}: {e}")


async def process_referral_registration(
    db: AsyncSession,
    new_user_id: int,
    referrer_id: int,
    bot: Bot = None
):
    try:
        new_user = await get_user_by_id(db, new_user_id)
        referrer = await get_user_by_id(db, referrer_id)

        if not new_user or not referrer:
            logger.error(f"Пользователи не найдены: new_user_id={new_user_id}, referrer_id={referrer_id}")
            return False

        if new_user.referred_by_id != referrer_id:
            logger.error(f"Пользователь {new_user_id} не привязан к рефереру {referrer_id}")
            return False

        try:
            from app.services.referral_contest_service import referral_contest_service

            await referral_contest_service.on_referral_registration(db, new_user_id)
        except Exception as exc:
            logger.debug("Не удалось записать конкурсную регистрацию: %s", exc)

        if bot:
            commission_percent = get_effective_referral_commission_percent(referrer)

            referral_notification = (
                f"🎉 <b>Добро пожаловать!</b>\n\n"
                f"Вы перешли по реферальной ссылке пользователя <b>{referrer.full_name}</b>."
            )
            await send_referral_notification(bot, new_user.telegram_id, referral_notification)

            inviter_notification = (
                f"👥 <b>Новый реферал!</b>\n\n"
                f"По вашей ссылке зарегистрировался пользователь <b>{new_user.full_name}</b>!\n\n"
                f"💰 Вы будете получать <b>{commission_percent}%</b> со всех его платежей."
            )
            await send_referral_notification(bot, referrer.telegram_id, inviter_notification)

        logger.info(f"✅ Зарегистрирован реферал {new_user_id} для {referrer_id}.")
        return True

    except Exception as e:
        logger.error(f"Ошибка обработки реферальной регистрации: {e}")
        return False


async def process_referral_topup(
    db: AsyncSession,
    user_id: int,
    topup_amount_kopeks: int,
    bot: Bot = None
):
    try:
        user = await get_user_by_id(db, user_id)
        if not user or not user.referred_by_id:
            logger.info(f"Пользователь {user_id} не является рефералом")
            return True

        referrer = await get_user_by_id(db, user.referred_by_id)
        if not referrer:
            logger.error(f"Реферер {user.referred_by_id} не найден")
            return False

        # Накопительная сумма пополнений реферала и счётчик "качественных" рефералов
        # партнёра (внёсших суммарно более 1000₽). Данные сохраняются для будущей
        # партнёрской программы и системы достижений (пока нигде не отображаются).
        previous_total = user.referral_total_topup_kopeks or 0
        new_total = previous_total + topup_amount_kopeks
        user.referral_total_topup_kopeks = new_total

        newly_qualified = (
            previous_total <= REFERRAL_QUALIFIED_TOPUP_THRESHOLD_KOPEKS < new_total
        )
        if newly_qualified:
            referrer.qualified_referrals_count = (referrer.qualified_referrals_count or 0) + 1

        if not user.has_made_first_topup:
            user.has_made_first_topup = True

        await db.commit()

        if newly_qualified:
            logger.info(
                "⭐ Реферал %s превысил порог %s₽ по сумме пополнений; счётчик партнёра %s = %s",
                user.telegram_id,
                REFERRAL_QUALIFIED_TOPUP_THRESHOLD_KOPEKS / 100,
                referrer.telegram_id,
                referrer.qualified_referrals_count,
            )

        commission_percent = get_effective_referral_commission_percent(referrer)
        commission_amount = int(topup_amount_kopeks * commission_percent / 100)

        if commission_amount <= 0:
            return True

        await add_user_balance(
            db,
            referrer,
            commission_amount,
            f"Комиссия {commission_percent}% с пополнения {user.full_name}",
            bot=bot,
        )

        await create_referral_earning(
            db=db,
            user_id=referrer.id,
            referral_id=user.id,
            amount_kopeks=commission_amount,
            reason="referral_commission",
        )

        logger.info(
            "💰 Реферальная комиссия: %s получил %s₽ с пополнения реферала %s",
            referrer.telegram_id,
            commission_amount / 100,
            user.telegram_id,
        )

        if bot:
            commission_notification = (
                f"💰 <b>Реферальная комиссия!</b>\n\n"
                f"Ваш реферал <b>{user.full_name}</b> пополнил баланс на "
                f"{settings.format_price(topup_amount_kopeks)}\n\n"
                f"🎁 Ваша комиссия ({commission_percent}%): "
                f"{settings.format_price(commission_amount)}\n\n"
                f"💎 Средства зачислены на ваш баланс."
            )
            await send_referral_notification(bot, referrer.telegram_id, commission_notification)

        return True

    except Exception as e:
        logger.error(f"Ошибка обработки пополнения реферала: {e}")
        return False
