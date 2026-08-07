"""Enable auto-pay for every existing subscription and set the charge window.

Background
----------
``autopay_enabled`` / ``autopay_days_before`` are per-subscription columns written
once at creation time, so flipping ``DEFAULT_AUTOPAY_ENABLED`` /
``DEFAULT_AUTOPAY_DAYS_BEFORE`` only affects NEW subscriptions. This one-off
script backfills the existing rows.

Partner subscriptions (``is_partner``) are left untouched: they are granted, not
sold, and the renewal pricing pipeline has nothing sensible to charge for them.

Trials are updated too. The monitoring loop never charges a trial
(``is_trial == False`` is part of its filter), but carrying the flag through the
trial → paid conversion is what makes auto-pay stick for converted users.

Usage
-----
    # dry-run (default): report the current state and what WOULD change
    python -m scripts.enable_autopay_for_all

    # apply: autopay_enabled = true, autopay_days_before = 1
    python -m scripts.enable_autopay_for_all --apply

    # another charge window
    python -m scripts.enable_autopay_for_all --apply --days 3

    # only one of the two columns
    python -m scripts.enable_autopay_for_all --apply --skip-days
    python -m scripts.enable_autopay_for_all --apply --skip-enable
"""

import argparse
import asyncio
import logging

from sqlalchemy import func, select, update

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("enable_autopay_for_all")

# Партнерские подписки выдаются, а не продаются — автосписание для них не имеет смысла.
NOT_PARTNER = Subscription.is_partner.is_(False)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="выполнить запись (без флага — dry-run)")
    parser.add_argument("--days", type=int, default=1, help="за сколько дней до окончания списывать (по умолчанию 1)")
    parser.add_argument("--skip-enable", action="store_true", help="не трогать autopay_enabled")
    parser.add_argument("--skip-days", action="store_true", help="не трогать autopay_days_before")
    args = parser.parse_args()

    if args.days < 0:
        parser.error("--days не может быть отрицательным")
    if args.skip_enable and args.skip_days:
        parser.error("--skip-enable вместе с --skip-days ничего не оставляет")

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("Режим: %s, целевое окно списания: %s дн.", mode, args.days)

    async with AsyncSessionLocal() as db:
        total = (await db.execute(select(func.count()).select_from(Subscription))).scalar_one()
        partners = (
            await db.execute(
                select(func.count()).select_from(Subscription).where(Subscription.is_partner.is_(True))
            )
        ).scalar_one()
        logger.info("Всего подписок: %s (из них партнерских, будут пропущены: %s)", total, partners)

        distribution = (
            await db.execute(
                select(Subscription.autopay_enabled, Subscription.autopay_days_before, func.count())
                .group_by(Subscription.autopay_enabled, Subscription.autopay_days_before)
                .order_by(Subscription.autopay_enabled, Subscription.autopay_days_before)
            )
        ).all()
        logger.info("Текущее состояние (enabled / days_before / шт.):")
        for enabled, days_before, count in distribution:
            logger.info("  %s / %s / %s", enabled, days_before, count)

        enable_conditions = [NOT_PARTNER, Subscription.autopay_enabled.is_distinct_from(True)]
        days_conditions = [NOT_PARTNER, Subscription.autopay_days_before.is_distinct_from(args.days)]

        to_enable = 0
        if not args.skip_enable:
            to_enable = (
                await db.execute(select(func.count()).select_from(Subscription).where(*enable_conditions))
            ).scalar_one()
            logger.info("Будет включен автоплатеж у подписок: %s", to_enable)

        to_set_days = 0
        if not args.skip_days:
            to_set_days = (
                await db.execute(select(func.count()).select_from(Subscription).where(*days_conditions))
            ).scalar_one()
            logger.info("Будет изменено окно списания у подписок: %s", to_set_days)

        if not args.apply:
            logger.info("Dry-run: ничего не записано. Повторите с --apply.")
            return

        if not args.skip_enable and to_enable:
            result = await db.execute(
                update(Subscription).where(*enable_conditions).values(autopay_enabled=True)
            )
            logger.info("autopay_enabled=true: обновлено строк %s", result.rowcount)

        if not args.skip_days and to_set_days:
            result = await db.execute(
                update(Subscription).where(*days_conditions).values(autopay_days_before=args.days)
            )
            logger.info("autopay_days_before=%s: обновлено строк %s", args.days, result.rowcount)

        await db.commit()

    logger.info("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
