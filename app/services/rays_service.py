import logging
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.rays import add_user_rays
from app.database.crud.user import get_user_by_id
from app.branding.context import use_brand_for_user
from app.localization.texts import get_texts
from app.utils.bot_registry import bot_for_user
from app.utils.user_utils import is_partner_account, is_rays_program_available_for
from app.database.models import RayTransactionType, User
from app.services.referral_service import send_referral_notification

logger = logging.getLogger(__name__)


async def award_rays_for_referral_purchase(
    db: AsyncSession,
    buyer: User,
    period_days: int,
    source_transaction_id: int,
    bot: Bot = None,
) -> bool:
    """Начисляет лучи рефереру за квалифицирующую покупку реферала.

    Правило: первый платёж реферала с периодом ≥ минимального тира выставляет
    лучи по тарифу и лочит их навсегда (buyer.rays_credited=True). Период меньше
    минимального тира (например, месяц) не квалифицирует — 0 лучей, флаг не
    лочится, чтобы более длинная покупка позже всё ещё могла начислить лучи.
    Всё после квалификации (апгрейды, продления, ребиллы) лучей не даёт.
    """
    try:
        if not settings.is_rays_program_enabled():
            return False

        # Лучи применимы только к рефералам (пришедшим по реферальной ссылке).
        if not buyer.referred_by_id:
            return False

        # Уже квалифицирован — повторные/последующие покупки лучей не дают.
        if buyer.rays_credited:
            return False

        rays = settings.get_rays_for_period(period_days)
        if rays <= 0:
            # Месяц не квалифицирует: 0 лучей, флаг НЕ лочим.
            logger.info(
                "✨ Покупка реферала %s на %s дн. не квалифицирует на лучи",
                buyer.id, period_days,
            )
            return False

        referrer = await get_user_by_id(db, buyer.referred_by_id)
        if not referrer:
            logger.error("Реферер %s не найден для начисления лучей", buyer.referred_by_id)
            return False

        # Партнёр живёт на повышенной комиссии и вне программы лучей. Флаг
        # квалификации у реферала не лочим: он к лучам партнёра отношения не имеет.
        if is_partner_account(referrer):
            logger.info(
                "☀️ Реферер %s — партнёрский аккаунт, лучи не начисляются",
                referrer.id,
            )
            return False

        # Витрина реферера без программы лучей (копикет): начислять нечего.
        if not is_rays_program_available_for(referrer):
            logger.info(
                "☀️ Реферер %s — витрина без программы лучей, лучи не начисляются",
                referrer.id,
            )
            return False

        # Лочим реферала и начисляем лучи рефереру в одной транзакции.
        buyer.rays_credited = True
        ray_tx = await add_user_rays(
            db,
            referrer,
            rays,
            type=RayTransactionType.REFERRAL_PURCHASE,
            referral_id=buyer.id,
            source_transaction_id=source_transaction_id,
            period_days=period_days,
            description=f"Лучи за покупку реферала {buyer.full_name} на {period_days} дн.",
        )

        if ray_tx is None:
            # Идемпотентный повтор (тот же платёж) или ошибка — лучи не начислены.
            return False

        logger.info(
            "✨ Начислено %s лучей рефереру %s за покупку реферала %s на %s дн.",
            rays, referrer.telegram_id, buyer.telegram_id, period_days,
        )

        if bot and settings.is_referral_notifications_enabled() and referrer.telegram_id:
            with use_brand_for_user(referrer):
                notification = get_texts(getattr(referrer, "language", None)).t(
                    "RAYS_AWARDED_NOTIFICATION",
                    "✨ <b>Вам начислены лучи!</b>\n\n"
                    "Ваш реферал <b>{buyer_name}</b> оформил подписку на длительный срок.\n\n"
                    "🎁 Начислено: <b>{rays}</b> лучей\n"
                    "💎 Всего лучей на балансе: <b>{balance}</b>",
                ).format(buyer_name=buyer.full_name, rays=rays, balance=referrer.rays_balance)
                await send_referral_notification(
                    bot_for_user(referrer, bot), referrer.telegram_id, notification,
                )

        return True

    except Exception as e:
        logger.error(f"Ошибка начисления лучей за покупку реферала {buyer.id}: {e}")
        return False
