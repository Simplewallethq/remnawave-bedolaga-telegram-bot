"""Раздать платный внутренний сквад панели существующим не-триальным подпискам.

Background
----------
Бот добавляет сквад ``PAID_INTERNAL_SQUAD_UUID`` в ``activeInternalSquads`` только в
момент отправки пользователя в панель (см. ``app/utils/panel_squads.py``). Старые
платные пользователи в панель не переотправляются, пока с ними что-то не случится
(продление, правка админом), поэтому их надо догнать этим скриптом один раз.

Базовый список сквадов берётся **из панели**, а не из ``connected_squads`` бота:
у части пользователей в панели есть сквады, которых в БД бота нет (kaz1, ab1…),
и терять их нельзя.

Usage
-----
    # dry-run (по умолчанию): посчитать и показать, кого бы обновили
    python -m scripts.backfill_paid_squad

    # применить: только активные платные (status=active, end_date > now)
    python -m scripts.backfill_paid_squad --apply

    # все не-триальные, включая истёкшие и отключённые
    python -m scripts.backfill_paid_squad --apply --all

    # пробный прогон на первых N подписках
    python -m scripts.backfill_paid_squad --apply --limit 20
"""

import argparse
import asyncio
import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus, User
from app.external.remnawave_api import RemnaWaveAPIError
from app.services.remnawave_service import RemnaWaveService
from app.utils.panel_squads import build_panel_squads, get_paid_squad_uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_paid_squad")

BATCH_SIZE = 500
CONCURRENCY = 5


def _panel_squad_uuids(remnawave_user) -> list:
    uuids = []
    for squad in remnawave_user.active_internal_squads or []:
        if isinstance(squad, dict):
            uuid = squad.get("uuid")
            if uuid:
                uuids.append(uuid)
        elif isinstance(squad, str):
            uuids.append(squad)
    return uuids


def _conditions(include_all: bool):
    conditions = [Subscription.is_trial.is_(False), User.remnawave_uuid.isnot(None)]
    if not include_all:
        conditions += [
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.end_date > datetime.utcnow(),
        ]
    return conditions


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="выполнить запись в панель (без флага — dry-run)")
    parser.add_argument("--all", action="store_true", help="все не-триальные подписки, а не только активные")
    parser.add_argument("--limit", type=int, default=0, help="обработать не больше N подписок (0 = все)")
    args = parser.parse_args()

    paid_uuid = get_paid_squad_uuid()
    if not paid_uuid:
        parser.error("PAID_INTERNAL_SQUAD_UUID не задан — нечего раздавать")

    mode = "APPLY" if args.apply else "DRY-RUN"
    scope = "все не-триальные" if args.all else "активные платные"
    logger.info("Режим: %s, выборка: %s, сквад: %s", mode, scope, paid_uuid)

    stats = {"checked": 0, "updated": 0, "already": 0, "missing": 0, "errors": 0}
    errors: list = []
    semaphore = asyncio.Semaphore(CONCURRENCY)

    service = RemnaWaveService()

    async with AsyncSessionLocal() as db:
        base_query = select(Subscription).join(User, User.id == Subscription.user_id).where(*_conditions(args.all))
        total = (await db.execute(select(func.count()).select_from(base_query.subquery()))).scalar_one()
        logger.info("Подписок в выборке: %s", total)

        async with service.get_api_client() as api:

            async def process(subscription: Subscription) -> None:
                user = subscription.user
                async with semaphore:
                    try:
                        remnawave_user = await api.get_user_by_uuid(user.remnawave_uuid)
                        if remnawave_user is None:
                            stats["missing"] += 1
                            logger.warning("Нет в панели: user_id=%s uuid=%s", user.id, user.remnawave_uuid)
                            return
                        current = _panel_squad_uuids(remnawave_user)
                        if paid_uuid in current:
                            stats["already"] += 1
                            return
                        target = build_panel_squads(subscription, current)
                        if not args.apply:
                            logger.info(
                                "[dry-run] user_id=%s %s: %s -> %s",
                                user.id,
                                user.telegram_id or user.email or remnawave_user.username,
                                current,
                                target,
                            )
                            stats["updated"] += 1
                            return
                        await api.update_user(uuid=user.remnawave_uuid, active_internal_squads=target)
                        stats["updated"] += 1
                    except RemnaWaveAPIError as error:
                        stats["errors"] += 1
                        errors.append((user.id, user.remnawave_uuid, str(error)))
                    except Exception as error:  # noqa: BLE001 — одиночная ошибка не должна ронять прогон
                        stats["errors"] += 1
                        errors.append((user.id, user.remnawave_uuid, repr(error)))

            offset = 0
            while True:
                if args.limit and stats["checked"] >= args.limit:
                    break
                limit = BATCH_SIZE
                if args.limit:
                    limit = min(limit, args.limit - stats["checked"])
                batch = (
                    await db.execute(
                        base_query.options(selectinload(Subscription.user))
                        .order_by(Subscription.id)
                        .offset(offset)
                        .limit(limit)
                    )
                ).scalars().all()
                if not batch:
                    break
                stats["checked"] += len(batch)
                await asyncio.gather(*(process(subscription) for subscription in batch))
                offset += len(batch)
                logger.info(
                    "Прогресс: %s/%s, обновлено %s, уже были %s, нет в панели %s, ошибок %s",
                    stats["checked"], total, stats["updated"], stats["already"], stats["missing"], stats["errors"],
                )

    for user_id, uuid, error in errors[:50]:
        logger.error("Ошибка user_id=%s uuid=%s: %s", user_id, uuid, error)
    if len(errors) > 50:
        logger.error("… и ещё %s ошибок", len(errors) - 50)

    logger.info(
        "Итог (%s): проверено %s, %s %s, уже в скваде %s, нет в панели %s, ошибок %s",
        mode,
        stats["checked"],
        "обновлено" if args.apply else "было бы обновлено",
        stats["updated"],
        stats["already"],
        stats["missing"],
        stats["errors"],
    )
    if not args.apply:
        logger.info("Dry-run: в панель ничего не записано. Повторите с --apply.")


if __name__ == "__main__":
    asyncio.run(main())
