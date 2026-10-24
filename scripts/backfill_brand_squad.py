"""Раздать сквад основного бренда всем существующим пользователям панели, кроме копикетов.

Background
----------
Бот добавляет ``PRIMARY_BRAND_INTERNAL_SQUAD_UUID`` в ``activeInternalSquads`` только в
момент отправки пользователя в панель (см. ``app/utils/panel_squads.py``). Старых
пользователей надо догнать один раз, причём не только бот-аккаунты: проход идёт
**по всей панели**, чтобы заодно покрыть юзеров teleVpn (``ltg_*``, ``televpnFree*``)
и ручные аккаунты без префикса.

Кто пропускается:

* пользователи копикетов — их ``remnawave_uuid`` из БД бота (бот регистрации —
  витрина по ``copycat_bots``; реестр брендов в скрипте пуст, поэтому из БД);
* партнёры и служебные аккаунты по префиксу username (``SKIP_PREFIXES``);
* ``user_*`` / ``web_*``, которых нет в БД бота (сироты: подписки в боте нет).

Базовый список сквадов берётся **из панели**. Запись — ``POST /api/users/bulk/update-squads``:
он заменяет список целиком, поэтому пользователи страницы группируются по текущему
набору сквадов и каждая группа уходит сразу после чтения страницы.

Usage
-----
    # dry-run (по умолчанию): разбивка по категориям, в панель ничего не пишется
    python -m scripts.backfill_brand_squad

    # применить
    python -m scripts.backfill_brand_squad --apply

    # пробный прогон: обновить не больше N пользователей
    python -m scripts.backfill_brand_squad --apply --limit 20

    # по очереди: ручные аккаунты, потом активные, потом все остальные
    python -m scripts.backfill_brand_squad --apply --only manual
    python -m scripts.backfill_brand_squad --apply --only active
    python -m scripts.backfill_brand_squad --apply
"""

import argparse
import asyncio
import logging
from collections import Counter, defaultdict

from sqlalchemy import select

from app.branding.profile import is_copycat_config
from app.config import settings
from app.database.crud.copycat import list_copycat_bots
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.services.copycat_service import brand_config_for
from app.services.remnawave_service import RemnaWaveService
from app.utils.panel_squads import get_primary_brand_squad_uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_brand_squad")

PAGE_SIZE = 500
SKIP_PREFIXES = ("wintech_", "letoapp", "bridge")
BOT_PREFIXES = ("user_", "web_")


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


def _category(username: str) -> str:
    for prefix in ("user_", "web_", "ltg_", "televpnFree", "televpn"):
        if username.startswith(prefix):
            return prefix.rstrip("_")
    return "manual"


async def _load_bot_accounts() -> tuple[set, set]:
    """(uuid аккаунтов копикетов, uuid всех аккаунтов бота). Сессию не держим поверх панели."""
    async with AsyncSessionLocal() as db:
        copycat_ids = set()
        for row in await list_copycat_bots(db):
            if row.telegram_bot_id and is_copycat_config(brand_config_for(row, row.group)):
                copycat_ids.add(row.telegram_bot_id)
        rows = (
            await db.execute(select(User.remnawave_uuid, User.bot_id).where(User.remnawave_uuid.isnot(None)))
        ).all()
        await db.commit()

    primary_id = None
    try:
        primary_id = int(settings.BOT_TOKEN.split(":", 1)[0])
    except (AttributeError, ValueError):
        pass
    copycat_ids.discard(primary_id)

    copycat_uuids = {uuid for uuid, bot_id in rows if bot_id in copycat_ids}
    bot_uuids = {uuid for uuid, _ in rows}
    logger.info(
        "Копикетов: %s, аккаунтов бота с панелью: %s, из них копикетных: %s",
        len(copycat_ids),
        len(bot_uuids),
        len(copycat_uuids),
    )
    return copycat_uuids, bot_uuids


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="выполнить запись в панель (без флага — dry-run)")
    parser.add_argument("--limit", type=int, default=0, help="обновить не больше N пользователей (0 = все)")
    parser.add_argument(
        "--only",
        choices=("manual", "active"),
        help="manual — только аккаунты без префикса, active — только со статусом ACTIVE",
    )
    args = parser.parse_args()

    squad_uuid = get_primary_brand_squad_uuid()
    if not squad_uuid:
        parser.error("PRIMARY_BRAND_INTERNAL_SQUAD_UUID не задан — нечего раздавать")

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("Режим: %s, сквад: %s, выборка: %s", mode, squad_uuid, args.only or "все")

    copycat_uuids, bot_uuids = await _load_bot_accounts()

    stats = Counter()
    to_update = Counter()
    errors: list = []
    service = RemnaWaveService()

    async with service.get_api_client() as api:
        start = 0
        total = None
        while total is None or start < total:
            if args.limit and stats["updated"] >= args.limit:
                break
            page = await api.get_all_users(start=start, size=PAGE_SIZE)
            total = page["total"]
            users = page["users"]
            if not users:
                break
            start += len(users)

            groups: dict = defaultdict(list)
            for panel_user in users:
                stats["checked"] += 1
                username = panel_user.username or ""
                if panel_user.uuid in copycat_uuids:
                    stats["skip_copycat"] += 1
                    continue
                if username.startswith(SKIP_PREFIXES):
                    stats["skip_service"] += 1
                    continue
                if username.startswith(BOT_PREFIXES) and panel_user.uuid not in bot_uuids:
                    stats["skip_orphan"] += 1
                    continue
                if args.only == "manual" and _category(username) != "manual":
                    continue
                if args.only == "active" and getattr(panel_user.status, "value", panel_user.status) != "ACTIVE":
                    continue
                current = _panel_squad_uuids(panel_user)
                if squad_uuid in current:
                    stats["already"] += 1
                    continue
                if args.limit and stats["updated"] + sum(len(v) for v in groups.values()) >= args.limit:
                    continue
                groups[tuple(current)].append(panel_user.uuid)
                to_update[_category(username)] += 1

            for current, uuids in groups.items():
                target = list(current) + [squad_uuid]
                if not args.apply:
                    stats["updated"] += len(uuids)
                    continue
                try:
                    await api._make_request(
                        "POST",
                        "/api/users/bulk/update-squads",
                        {"uuids": uuids, "activeInternalSquads": target},
                    )
                    stats["updated"] += len(uuids)
                except Exception as error:  # noqa: BLE001 — одна пачка не должна ронять прогон
                    stats["errors"] += len(uuids)
                    errors.append((list(current), len(uuids), repr(error)))

            logger.info(
                "Прогресс: %s/%s, %s %s, уже в скваде %s, копикеты %s, служебные %s, сироты %s, ошибок %s",
                stats["checked"],
                total,
                "обновлено" if args.apply else "к обновлению",
                stats["updated"],
                stats["already"],
                stats["skip_copycat"],
                stats["skip_service"],
                stats["skip_orphan"],
                stats["errors"],
            )

    for current, count, error in errors[:50]:
        logger.error("Ошибка пачки %s (%s юзеров): %s", current, count, error)

    logger.info("По категориям (%s): %s", mode, dict(to_update))
    logger.info(
        "Итог (%s): проверено %s, %s %s, уже в скваде %s, пропущено: копикеты %s, служебные %s, сироты %s; ошибок %s",
        mode,
        stats["checked"],
        "обновлено" if args.apply else "было бы обновлено",
        stats["updated"],
        stats["already"],
        stats["skip_copycat"],
        stats["skip_service"],
        stats["skip_orphan"],
        stats["errors"],
    )
    if not args.apply:
        logger.info("Dry-run: в панель ничего не записано. Повторите с --apply.")


if __name__ == "__main__":
    asyncio.run(main())
