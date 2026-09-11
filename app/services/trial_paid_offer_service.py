"""A/B «доступ за 1 ₽ вместо триала» (только Telegram-бот).

Новый пользователь при регистрации в боте с вероятностью TRIAL_PAID_OFFER_PERCENT
попадает в вариант `paid_trial` (иначе `control`); вариант липкий и хранится в
`users.trial_offer_variant`. Вариант `paid_trial` вместо бесплатного триала видит
оффер: TRIAL_PAID_OFFER_ACCESS_DAYS дней тарифа TRIAL_PAID_OFFER_PLAN_CODE за
TRIAL_PAID_OFFER_PRICE_KOPEKS по СБП через 1Payment. Счёт выставляется с
subscribe=1, так что успешная оплата приносит токен рекуррента; подписка
создаётся на срок доступа, но с `plan_period_days` =
TRIAL_PAID_OFFER_RENEWAL_PERIOD_DAYS и `is_paid_trial=True` — дальше монитор
списывает месячную цену тарифа за TRIAL_PAID_OFFER_RECURRING_HOURS_BEFORE часов
до конца доступа и продлевает на месяц (`finalize_tariff_renewal` снимает флаг).

Неоплатившим по TRIAL_PAID_OFFER_FALLBACK_TRIAL_MINUTES (0 — выключено) через
N минут выдаётся обычный бесплатный триал.

Кабинет, миниапп и web-API не знают об эксперименте и ведут себя как раньше.
"""

from __future__ import annotations

import html
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Subscription, SubscriptionPlan, User, UserStatus
from app.localization.texts import get_texts
from app.utils.formatters import format_days_declension
from app.services.plan_pricing_service import (
    get_plan_by_code,
    get_plan_by_id,
    get_plan_price,
    resolve_pricing_cohort,
)

logger = logging.getLogger(__name__)

VARIANT_CONTROL = "control"
VARIANT_PAID = "paid_trial"

SNAPSHOT_KEY = "paid_trial"
SNAPSHOT_KIND = "paid_trial"
SNAPSHOT_VERSION = 1
PURCHASE_SOURCE = "paid_trial_offer"

PAY_IMAGE_PATH = os.path.join("images", "pay.webp")
CONNECTION_IMAGE_PATH = os.path.join("images", "connection.webp")


class TrialPaidOfferService:
    """Назначение варианта, оффер, активация по колбеку и фолбэк-триал."""

    # ------------------------------------------------------------ варианты

    def assign_variant(self, user: Any) -> Optional[str]:
        """Назначает вариант новому пользователю бота. None — тест выключен.

        Только устанавливает поле, коммит — на вызывающем.
        """
        if not settings.is_trial_paid_offer_enabled():
            return None
        percent = settings.get_trial_paid_offer_percent()
        variant = VARIANT_PAID if random.random() * 100 < percent else VARIANT_CONTROL
        user.trial_offer_variant = variant
        logger.info(
            "🧪 A/B платного триала: пользователь %s → %s (percent=%s)",
            getattr(user, "telegram_id", None),
            variant,
            percent,
        )
        return variant

    @staticmethod
    def is_paid_variant(user: Any) -> bool:
        return getattr(user, "trial_offer_variant", None) == VARIANT_PAID

    def is_offer_available(self, user: Any) -> bool:
        """Показывать ли оффер вместо триала: тест включён, вариант paid_trial,
        подписки ещё не было и платной истории нет."""
        if not user or not settings.is_trial_paid_offer_enabled():
            return False
        if not self.is_paid_variant(user):
            return False
        if getattr(user, "subscription", None) is not None:
            return False
        if getattr(user, "has_had_paid_subscription", False):
            return False
        return True

    # --------------------------------------------------------------- тариф

    async def resolve_plan(
        self, db: AsyncSession, user: Any
    ) -> Optional[Tuple[SubscriptionPlan, int]]:
        """(план, цена продления) или None, если оффер нечем обслужить.

        Без цены на период продления рекуррент не сможет посчитать списание —
        тогда оффер не показываем вовсе.
        """
        plan = await get_plan_by_code(db, settings.get_trial_paid_offer_plan_code())
        if plan is None or not plan.is_active:
            return None
        renewal_price = await get_plan_price(
            db,
            plan.id,
            settings.get_trial_paid_offer_renewal_period_days(),
            cohort=resolve_pricing_cohort(user),
        )
        if renewal_price is None or renewal_price <= 0:
            return None
        return plan, int(renewal_price)

    @staticmethod
    def build_snapshot(user: Any, plan: SubscriptionPlan, renewal_price_kopeks: int) -> Dict[str, Any]:
        return {
            "v": SNAPSHOT_VERSION,
            "kind": SNAPSHOT_KIND,
            "user_id": int(user.id),
            "plan_id": int(plan.id),
            "plan_code": plan.code,
            "access_days": settings.get_trial_paid_offer_access_days(),
            "renewal_period_days": settings.get_trial_paid_offer_renewal_period_days(),
            "renewal_price_kopeks": int(renewal_price_kopeks),
            "price_kopeks": settings.get_trial_paid_offer_price_kopeks(),
            "issued_at": datetime.utcnow().isoformat(),
        }

    @staticmethod
    def extract_snapshot(metadata: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(metadata, dict):
            return None
        snapshot = metadata.get(SNAPSHOT_KEY)
        if not isinstance(snapshot, dict) or snapshot.get("kind") != SNAPSHOT_KIND:
            return None
        return snapshot

    # ---------------------------------------------------------------- счёт

    async def create_offer_invoice(
        self, db: AsyncSession, bot: Any, user: Any
    ) -> Optional[Dict[str, Any]]:
        """Счёт 1Payment на цену оффера. Мимо роутера: тот режет по общему
        минимуму всех шлюзов, а нам нужен 1 ₽."""
        from app.services.payment_service import PaymentService

        resolved = await self.resolve_plan(db, user)
        if resolved is None:
            logger.warning(
                "🧪 Платный триал: тариф %s недоступен или без цены на %s дн.",
                settings.get_trial_paid_offer_plan_code(),
                settings.get_trial_paid_offer_renewal_period_days(),
            )
            return None
        plan, renewal_price = resolved
        snapshot = self.build_snapshot(user, plan, renewal_price)

        texts = get_texts(getattr(user, "language", None) or settings.DEFAULT_LANGUAGE)
        description = texts.t(
            "PAID_TRIAL_OFFER_INVOICE_DESCRIPTION",
            "Доступ {name} на {days} дн.",
        ).format(name=plan.display_name, days=snapshot["access_days"])

        result = await PaymentService(bot).create_onepayment_payment(
            db,
            user_id=user.id,
            amount_kopeks=snapshot["price_kopeks"],
            description=description,
            language=getattr(user, "language", None),
            metadata={SNAPSHOT_KEY: snapshot, "purpose": PURCHASE_SOURCE},
            allow_below_min=True,
        )
        if result:
            result["snapshot"] = snapshot
            result["plan"] = plan
        return result

    # ----------------------------------------------------------- активация

    async def activate_from_payment(
        self,
        db: AsyncSession,
        user: Any,
        snapshot: Dict[str, Any],
        *,
        bot: Any = None,
    ) -> bool:
        """Активация после зачисления оплаты на баланс (из колбека 1Payment).

        True — подписка создана и пользователь уведомлён. False — активировать
        нечего/нельзя: деньги остаются на балансе, вызывающий шлёт обычное
        уведомление о пополнении.
        """
        from app.database.crud.subscription_event import record_subscription_purchase_event
        from app.database.crud.user import get_user_by_id
        from app.handlers.subscription.tariffs import (
            _resolve_active_subscription,
            finalize_tariff_purchase,
        )

        try:
            plan_id = int(snapshot.get("plan_id") or 0)
            access_days = int(snapshot.get("access_days") or 0)
            renewal_period_days = int(snapshot.get("renewal_period_days") or 0)
            price_kopeks = int(snapshot.get("price_kopeks") or 0)
        except (TypeError, ValueError):
            plan_id = access_days = renewal_period_days = price_kopeks = 0
        if plan_id <= 0 or access_days <= 0 or renewal_period_days <= 0 or price_kopeks <= 0:
            logger.warning("🧪 Платный триал: некорректный снимок у пользователя %s: %s", user.id, snapshot)
            return False

        fresh_user = await get_user_by_id(db, user.id)
        if fresh_user is None:
            return False
        user = fresh_user

        plan = await get_plan_by_id(db, plan_id)
        if plan is None or not plan.is_active:
            logger.warning("🧪 Платный триал: тариф %s недоступен", plan_id)
            return False

        active_sub = await _resolve_active_subscription(user)
        if active_sub and not active_sub.is_trial:
            logger.info(
                "🧪 Платный триал: у пользователя %s уже активная платная подписка, пропуск",
                user.telegram_id,
            )
            return False

        if user.balance_kopeks < price_kopeks:
            logger.info(
                "🧪 Платный триал: недостаточно средств у %s (%s < %s)",
                user.telegram_id,
                user.balance_kopeks,
                price_kopeks,
            )
            return False

        texts = get_texts(getattr(user, "language", None) or settings.DEFAULT_LANGUAGE)
        description = texts.t(
            "PAID_TRIAL_OFFER_INVOICE_DESCRIPTION",
            "Доступ {name} на {days} дн.",
        ).format(name=plan.display_name, days=access_days)

        try:
            result = await finalize_tariff_purchase(
                db,
                user,
                plan,
                renewal_period_days,
                price_kopeks,
                bot=bot,
                access_days=access_days,
                description=description,
            )
        except Exception as error:  # pragma: no cover - defensive logging
            logger.error(
                "❌ Платный триал: ошибка оформления для пользователя %s: %s",
                user.telegram_id,
                error,
                exc_info=True,
            )
            return False
        if result is None:
            return False
        subscription, transaction, was_trial_conversion = result

        try:
            await record_subscription_purchase_event(
                db,
                user_id=user.id,
                subscription_id=subscription.id,
                transaction_id=transaction.id,
                amount_kopeks=transaction.amount_kopeks,
                occurred_at=transaction.completed_at or transaction.created_at,
                period_days=access_days,
                was_trial_conversion=was_trial_conversion,
                payment_method=transaction.payment_method,
                source=PURCHASE_SOURCE,
                starts_at=subscription.start_date,
                ends_at=subscription.end_date,
                extra={
                    "renewal_period_days": renewal_period_days,
                    "renewal_price_kopeks": snapshot.get("renewal_price_kopeks"),
                    "plan_code": plan.code,
                },
            )
        except Exception as error:
            logger.warning("🧪 Платный триал: не удалось записать событие покупки: %s", error)

        if bot is not None:
            try:
                from app.services.admin_notification_service import AdminNotificationService

                await AdminNotificationService(bot).send_subscription_purchase_notification(
                    db,
                    user,
                    subscription,
                    transaction,
                    access_days,
                    was_trial_conversion,
                    record_event=False,
                )
            except Exception as error:
                logger.error("🧪 Платный триал: не удалось уведомить админов (%s): %s", user.telegram_id, error)

            await self.notify_activated(db, bot, user, subscription, plan, snapshot)

        logger.info(
            "✅ Платный триал: %s/%s дн. оформлен для пользователя %s (цена %s, продление %s дн.)",
            plan.code,
            access_days,
            user.telegram_id,
            price_kopeks,
            renewal_period_days,
        )
        return True

    async def notify_activated(
        self,
        db: AsyncSession,
        bot: Any,
        user: Any,
        subscription: Subscription,
        plan: SubscriptionPlan,
        snapshot: Dict[str, Any],
    ) -> None:
        """Онбординг с ключом доступа + предупреждение о списании через сутки."""
        from app.keyboards.inline import get_onboarding_welcome_keyboard
        from app.utils.subscription_utils import get_raw_subscription_link

        if not bot or not getattr(user, "telegram_id", None):
            return
        try:
            await db.refresh(subscription)
        except Exception:
            pass

        lang = getattr(user, "language", None) or settings.DEFAULT_LANGUAGE
        texts = get_texts(lang)
        link = get_raw_subscription_link(subscription)
        parts = [
            texts.t(
                "PAID_TRIAL_OFFER_ACTIVATED_TEXT",
                "✅ <b>Готово! Доступ на {days} активирован</b>",
            ).format(days=format_days_declension(int(snapshot.get("access_days") or 0), lang)),
        ]
        if link:
            parts.append(
                "▎ "
                + texts.t("ONBOARDING_ACCESS_KEY_LABEL", "Ключ-ссылка доступа (для Happ, Incy)")
                + "\n"
                + f"<pre><code>{html.escape(link, quote=True)}</code></pre>"
            )
        text = "\n\n".join(parts)
        keyboard = get_onboarding_welcome_keyboard(lang)
        try:
            if os.path.exists(CONNECTION_IMAGE_PATH):
                from aiogram.types import FSInputFile

                await bot.send_photo(
                    user.telegram_id,
                    FSInputFile(CONNECTION_IMAGE_PATH),
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(user.telegram_id, text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as error:
            logger.error("🧪 Платный триал: не удалось уведомить пользователя %s: %s", user.telegram_id, error)

    # -------------------------------------------------------------- фолбэк

    FALLBACK_BATCH_LIMIT = 50

    async def list_fallback_candidates(
        self,
        db: AsyncSession,
        *,
        before: datetime,
        not_before: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[User]:
        """Вариант paid_trial без какой-либо подписки, зарегистрированные до `before`
        (и не раньше `not_before`). Свежие — первыми, чтобы новым триал приходил
        вовремя, а бэклог дотягивался следом."""
        query = (
            select(User)
            .outerjoin(Subscription, Subscription.user_id == User.id)
            .where(
                User.trial_offer_variant == VARIANT_PAID,
                User.status == UserStatus.ACTIVE.value,
                User.has_had_paid_subscription.is_(False),
                User.paid_trial_fallback_at.is_(None),
                User.created_at < before,
                Subscription.id.is_(None),
            )
            .options(selectinload(User.subscription))
            .order_by(User.created_at.desc())
        )
        if not_before is not None:
            query = query.where(User.created_at >= not_before)
        if limit:
            query = query.limit(limit)
        result = await db.execute(query)
        return list(result.scalars().unique().all())

    async def grant_fallback_trials(self, db: AsyncSession, bot: Any) -> int:
        """Через TRIAL_PAID_OFFER_FALLBACK_TRIAL_MINUTES без оплаты выдаёт обычный триал.

        Идемпотентно: после выдачи у пользователя есть подписка, и в выборку он
        больше не попадает. При 0 минут ничего не делает. Момент выдачи пишется в
        users.paid_trial_fallback_at — по нему считается когорта в статистике.
        """
        minutes = settings.get_trial_paid_offer_fallback_trial_minutes()
        if minutes <= 0 or not settings.is_trial_paid_offer_enabled():
            return 0

        from app.handlers.start import activate_trial_for_user

        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=minutes)
        oldest = now - timedelta(hours=settings.get_trial_paid_offer_fallback_max_age_hours())
        candidates = await self.list_fallback_candidates(
            db, before=cutoff, not_before=oldest, limit=self.FALLBACK_BATCH_LIMIT
        )
        granted = 0
        for user in candidates:
            try:
                ok = await activate_trial_for_user(
                    bot, db, user, description="Фолбэк-триал после неоплаченного оффера за 1 ₽"
                )
            except Exception as error:
                logger.error("🧪 Фолбэк-триал: ошибка выдачи пользователю %s: %s", user.telegram_id, error)
                try:
                    await db.rollback()
                except Exception:  # pragma: no cover
                    pass
                continue
            if not ok:
                continue
            granted += 1
            try:
                user.paid_trial_fallback_at = datetime.utcnow()
                await db.commit()
            except Exception as error:
                logger.error("🧪 Фолбэк-триал: не удалось отметить выдачу пользователю %s: %s", user.telegram_id, error)
                try:
                    await db.rollback()
                except Exception:  # pragma: no cover
                    pass
            await self._notify_fallback_trial(db, bot, user)
        if granted:
            logger.info("🧪 Фолбэк-триал: выдано %s пользователям без оплаты оффера", granted)
        return granted

    async def _notify_fallback_trial(self, db: AsyncSession, bot: Any, user: Any) -> None:
        from app.keyboards.inline import get_onboarding_welcome_keyboard
        from app.utils.subscription_utils import get_raw_subscription_link

        if not bot or not getattr(user, "telegram_id", None):
            return
        try:
            await db.refresh(user, ["subscription"])
        except Exception:
            pass
        lang = getattr(user, "language", None) or settings.DEFAULT_LANGUAGE
        texts = get_texts(lang)
        link = get_raw_subscription_link(getattr(user, "subscription", None))
        text = texts.t(
            "PAID_TRIAL_FALLBACK_TRIAL_TEXT",
            "🎁 Дарим тебе бесплатную подписку на {days} дн.! Установи приложение и пользуйся.",
        ).format(days=settings.TRIAL_DURATION_DAYS)
        if link:
            text += (
                "\n\n"
                + texts.t("ONBOARDING_ACCESS_KEY_LABEL", "Ключ-ссылка доступа (для Happ, Incy)")
                + "\n"
                + f"<pre><code>{html.escape(link, quote=True)}</code></pre>"
            )
        keyboard = get_onboarding_welcome_keyboard(lang)
        try:
            if os.path.exists(CONNECTION_IMAGE_PATH):
                from aiogram.types import FSInputFile

                await bot.send_photo(
                    user.telegram_id,
                    FSInputFile(CONNECTION_IMAGE_PATH),
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(user.telegram_id, text, reply_markup=keyboard, parse_mode="HTML")
        except Exception as error:
            logger.error("🧪 Фолбэк-триал: не удалось уведомить пользователя %s: %s", user.telegram_id, error)


trial_paid_offer_service = TrialPaidOfferService()
