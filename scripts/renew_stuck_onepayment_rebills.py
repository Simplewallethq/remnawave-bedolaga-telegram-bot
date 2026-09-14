"""Провести продление по успешным рекуррентам 1Payment, где оно упало.

Background
----------
До фикса `_apply_onepayment_recurring_renewal` блокировал подписку голым
FOR UPDATE, а `Subscription.plan` грузится LEFT JOIN'ом — Postgres отвечал
«FOR UPDATE cannot be applied to the nullable side of an outer join». Деньги
по токену списывались и ложились на баланс, а подписка не продлевалась.
Монитор это не подбирает: подписка к тому времени EXPIRED, а баланс-автоплатёж
пропускается из-за активной привязки.

Скрипт берёт оплаченные рекуррентные платежи без `renewal_applied` и прогоняет
то же продление, что и колбек. Проверки внутри те же: подписка не продлена
вручную с момента списания, статус ACTIVE/EXPIRED, баланс покрывает цену —
иначе платёж пропускается и деньги остаются на балансе. Повторно не списывает.

Usage
-----
    # dry-run (по умолчанию): показать, что будет продлено
    python -m scripts.renew_stuck_onepayment_rebills

    # провести продление и уведомить пользователей
    python -m scripts.renew_stuck_onepayment_rebills --apply

    # только конкретные платежи
    python -m scripts.renew_stuck_onepayment_rebills --apply --payment-id 1722 --payment-id 1724
"""

import argparse
import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select

from app.config import settings
from app.database.crud.onepayment import get_onepayment_binding_by_id
from app.database.crud.user import get_user_by_id
from app.database.database import AsyncSessionLocal
from app.database.models import OnePaymentPayment, Subscription
from app.services.payment.onepayment import RENEWAL_METADATA_KEY, _parse_period_end
from app.services.payment_service import PaymentService
from app.utils import bot_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("renew_stuck_onepayment_rebills")


async def find_stuck_payments(
    payment_ids: Optional[List[int]], since: Optional[datetime]
) -> List[int]:
    async with AsyncSessionLocal() as db:
        query = select(OnePaymentPayment).where(
            OnePaymentPayment.is_recurring == True,  # noqa: E712
            OnePaymentPayment.is_paid == True,  # noqa: E712
            OnePaymentPayment.status == OnePaymentPayment.STATUS_SUCCESS,
        )
        if payment_ids:
            query = query.where(OnePaymentPayment.id.in_(payment_ids))
        if since is not None:
            query = query.where(OnePaymentPayment.created_at >= since)
        result = await db.execute(query.order_by(OnePaymentPayment.created_at))
        return [
            p.id
            for p in result.scalars().all()
            if isinstance((p.metadata_json or {}).get(RENEWAL_METADATA_KEY), dict)
            and not (p.metadata_json or {}).get("renewal_applied")
        ]


def build_bots() -> Bot:
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)
    primary = Bot(token=settings.BOT_TOKEN, default=default)
    # Пользователям зеркал пишет только их зеркало; id бота — префикс токена.
    for mirror_cfg in settings.get_mirror_bots():
        try:
            bot_id = int(str(mirror_cfg["token"]).split(":", 1)[0])
        except (KeyError, ValueError):
            continue
        bot_registry.register_bot(bot_id, settings.LOGO_FILE, Bot(token=mirror_cfg["token"], default=default))
    return primary


async def describe(payment_id: int) -> str:
    async with AsyncSessionLocal() as db:
        payment = await db.get(OnePaymentPayment, payment_id)
        user = await get_user_by_id(db, payment.user_id)
        snapshot = payment.metadata_json[RENEWAL_METADATA_KEY]
        subscription = await db.get(Subscription, int(snapshot["subscription_id"]))
        period_end = _parse_period_end(snapshot.get("period_end"))
        current_end = subscription.end_date.replace(microsecond=0) if subscription and subscription.end_date else None
        verdict = "renew"
        if subscription is None or subscription.user_id != user.id:
            verdict = "skip: no subscription"
        elif period_end is not None and current_end != period_end:
            verdict = "skip: renewed since charge"
        elif user.balance_kopeks < int(snapshot["price_kopeks"]):
            verdict = "skip: balance below price"
        return (
            f"payment #{payment.id} {payment.user_data} user={user.id} tg={user.telegram_id} "
            f"balance={user.balance_kopeks} price={snapshot['price_kopeks']} days={snapshot['period_days']} "
            f"sub_end={current_end} sub_status={subscription.status if subscription else None} -> {verdict}"
        )


async def renew(service: PaymentService, payment_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        payment = await db.get(OnePaymentPayment, payment_id)
        user = await get_user_by_id(db, payment.user_id)
        binding = await get_onepayment_binding_by_id(db, payment.binding_id) if payment.binding_id else None
        try:
            return await service._apply_onepayment_recurring_renewal(db, payment, user, binding)
        except Exception:
            logger.exception("payment #%s: renewal failed", payment_id)
            await db.rollback()
            return False


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="провести продление (иначе dry-run)")
    parser.add_argument("--payment-id", type=int, action="append", help="id платежа onepayment_payments")
    parser.add_argument("--since", type=datetime.fromisoformat, help="только платежи с created_at >= (UTC)")
    args = parser.parse_args()

    payment_ids = await find_stuck_payments(args.payment_id, args.since)
    logger.info("stuck recurring payments: %s", len(payment_ids))
    for payment_id in payment_ids:
        logger.info(await describe(payment_id))

    if not args.apply or not payment_ids:
        logger.info("dry-run, nothing changed" if not args.apply else "nothing to do")
        return

    bot = build_bots()
    service = PaymentService(bot)
    renewed = 0
    try:
        for payment_id in payment_ids:
            if await renew(service, payment_id):
                renewed += 1
                logger.info("payment #%s: renewed", payment_id)
            else:
                logger.info("payment #%s: skipped, money stays on balance", payment_id)
    finally:
        await bot.session.close()
        for instance in bot_registry._instances.values():
            await instance.session.close()
    logger.info("renewed %s of %s", renewed, len(payment_ids))


if __name__ == "__main__":
    asyncio.run(main())
