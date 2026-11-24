"""Наполнить пул «выделенный IP» слотами из JSON (Trello #141).

Слоты заводятся в панели заранее (на адрес — инбаунд вход=выход, хост «🔒 Мой IP»,
сквад с одним этим инбаундом). Таблицу слотов выгружает репозиторий нод
(tstNode: ``_panel/dedicated-ip/slots.json``). Формат — список объектов::

    [{"slot_no": 1, "ip": "45.136.136.230", "squad_uuid": "…", "host_uuid": "…"}, …]

Скрипт только добавляет: новые слоты встают в пул свободными, у существующих
обновляется ``host_uuid``; несовпадение ip/сквада у существующего номера — ошибка.
Ничего не удаляет и не трогает выданные адреса. Пул расширяют тем же скриптом:
дописали новые слоты в JSON — прогнали ещё раз.

Usage
-----
    python -m scripts.seed_dedicated_ips slots.json            # dry-run
    python -m scripts.seed_dedicated_ips slots.json --apply
"""

import argparse
import asyncio
import json
import logging

from sqlalchemy import select

from app.database.database import AsyncSessionLocal
from app.database.models import DedicatedIpSlot
from app.services.dedicated_ip_service import pool_stats, seed_slots

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("seed_dedicated_ips")


def _load(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise SystemExit("ожидается JSON-список слотов")
    seen_no, seen_ip, seen_squad = set(), set(), set()
    for item in data:
        for key in ("slot_no", "ip", "squad_uuid"):
            if not item.get(key):
                raise SystemExit(f"у слота нет поля {key}: {item}")
        for value, seen, name in (
            (int(item["slot_no"]), seen_no, "slot_no"),
            (item["ip"], seen_ip, "ip"),
            (item["squad_uuid"], seen_squad, "squad_uuid"),
        ):
            if value in seen:
                raise SystemExit(f"дубль {name}: {value}")
            seen.add(value)
    return data


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    slots = _load(args.path)
    async with AsyncSessionLocal() as db:
        existing = {
            slot.slot_no: slot
            for slot in (await db.execute(select(DedicatedIpSlot))).scalars().all()
        }
        new = [item for item in slots if int(item["slot_no"]) not in existing]
        logger.info("В файле %s слотов, в базе %s, новых %s", len(slots), len(existing), len(new))
        for item in new:
            logger.info("  + #%s %s сквад %s", item["slot_no"], item["ip"], item["squad_uuid"])
        if not args.apply:
            logger.info("dry-run: ничего не записано (добавьте --apply)")
            return
        result = await seed_slots(db, slots)
        logger.info("Готово: добавлено %s, обновлено %s; пул: %s", result["created"], result["updated"], await pool_stats(db))
        logger.info("Перезапустите бот или дождитесь цикла мониторинга — он перечитает сквады пула")


if __name__ == "__main__":
    asyncio.run(main())
