"""Управление копикетами в БД: витрины, группы, веса роутера.

Бот перечитывает таблицы раз в COPYCAT_SYNC_INTERVAL_SECONDS (60 с): новая
витрина поднимается и получает вебхук, выключенная гасится, правки бренда и
весов подхватываются без рестарта. Бот указывается id записи, Telegram id,
@username или токеном; группа — именем.

Usage
-----
    python -m scripts.copycats list [--group NAME]
    python -m scripts.copycats show BOT
    python -m scripts.copycats add TOKEN [--group NAME] [--mirror] [key=value ...]
    python -m scripts.copycats set BOT key=value ...        # пустое значение = NULL
    python -m scripts.copycats enable BOT | disable BOT | delete BOT

    python -m scripts.copycats groups
    python -m scripts.copycats group-add NAME [key=value ...]
    python -m scripts.copycats group-set NAME key=value ...
    python -m scripts.copycats group-delete NAME            # боты остаются без группы

    # веса роутера: свои у бота или у группы; --clear — вернуть наследование
    python -m scripts.copycats weights BOT platega=10 wata=1
    python -m scripts.copycats weights group:NAME wata=1
    python -m scripts.copycats weights BOT --clear

    # дозалить новые записи из mirror_bots.yaml (существующие токены пропускаются)
    python -m scripts.copycats import-yaml [--group NAME]

    # ценовые схемы: триал, устройства и цены тарифов (в рублях) по периодам;
    # цепляются к боту или группе: set BOT price_scheme=NAME, group-set G price_scheme=NAME
    python -m scripts.copycats schemes
    python -m scripts.copycats scheme-show NAME
    python -m scripts.copycats scheme-add NAME [trial_days=N] [enforce_floor=true]
    python -m scripts.copycats scheme-set NAME [trial_days=N] [enforce_floor=true] [name=NEW]
    python -m scripts.copycats scheme-tariff NAME solo [devices=1] [30=320 90=570 360=1560 720=2160]
    python -m scripts.copycats scheme-clone SRC DST       # SRC=- — цены основного бота
    python -m scripts.copycats scheme-delete NAME         # боты и группы вернутся к ценам по умолчанию

    # владельцы ботов с платформы (OWNER_PLATFORM_BOT_ID)
    python -m scripts.copycats owners
    python -m scripts.copycats owner-set OWNER revshare=70   # OWNER: Telegram id или user:<id>

Поля бота: name, logo, image_url, privacy_url, terms_url, channel_link,
channel_id, support, subscription_domain, happ_redirect_template,
referral_terms_url, has_own_app, rays_enabled, is_copycat, enabled, group,
owner_user_id, owner_share_percent, price_scheme, aurapay_shop (касса из
AURAPAY_EXTRA_SHOPS; пусто — основная).
Поля группы: name, description, support, subscription_domain,
happ_redirect_template, privacy_url, terms_url, referral_terms_url,
owner_share_percent, price_scheme.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Optional

from app.config import settings
from app.database.crud.copycat import (
    find_copycat_bot,
    get_copycat_bot_by_token,
    get_copycat_group_by_name,
    group_bot_counts,
    list_copycat_bots,
    list_copycat_groups,
)
from app.database.database import AsyncSessionLocal, engine
from app.database.models import (
    Base,
    BotOwner,
    CopycatBot,
    CopycatGroup,
    OwnerPayout,
    PriceScheme,
    PriceSchemeTariff,
    User,
)
from app.services.copycat_service import (
    BOT_BRAND_FIELDS,
    DEFAULT_GROUP_NAME,
    GROUP_BRAND_FIELDS,
    effective_gateway_weights,
    effective_owner_share_percent,
    effective_price_scheme_id,
    import_legacy_mirrors,
)
from app.services.price_scheme_service import (
    clone_scheme,
    get_scheme_by_name,
    set_tariff,
    snapshot_from_row,
)
from app.services.aurapay_service import normalize_shop
from app.services.payment_gateway_router import ROUTED_GATEWAYS

BOT_FIELDS = BOT_BRAND_FIELDS + (
    "logo",
    "image_url",
    "is_copycat",
    "enabled",
    "group",
    "owner_user_id",
    "owner_share_percent",
    "price_scheme",
    "aurapay_shop",
)
GROUP_FIELDS = ("name", "description", "owner_share_percent", "price_scheme") + GROUP_BRAND_FIELDS
SCHEME_FIELDS = ("name", "trial_days", "enforce_floor")
OWNER_FIELDS = ("revshare",)
BOOL_FIELDS = {"has_own_app", "rays_enabled", "is_copycat", "enabled", "enforce_floor"}
INT_FIELDS = {"owner_user_id", "owner_share_percent", "trial_days", "revshare"}
PLAN_CODES = ("solo", "plus", "pro")

# id схемы → имя, для вывода list/show/groups.
_scheme_names: dict[int, str] = {}


class UsageError(Exception):
    pass


def _parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on", "да"):
        return True
    if lowered in ("0", "false", "no", "off", "нет"):
        return False
    raise UsageError(f"ожидалось true/false, получено {value!r}")


def _parse_assignments(pairs: list[str], allowed: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise UsageError(f"ожидалось key=value, получено {pair!r}")
        key, value = pair.split("=", 1)
        key = key.strip()
        if key not in allowed:
            raise UsageError(f"неизвестное поле {key!r}; доступны: {', '.join(allowed)}")
        value = value.strip()
        if value == "":
            result[key] = None
        elif key in BOOL_FIELDS:
            result[key] = _parse_bool(value)
        elif key in INT_FIELDS:
            try:
                result[key] = int(value)
            except ValueError as error:
                raise UsageError(f"{key}: ожидалось целое, получено {value!r}") from error
        else:
            result[key] = value
    for key in ("owner_share_percent", "revshare"):
        share = result.get(key)
        if share is not None and not 0 <= share <= 100:
            raise UsageError(f"{key} должен быть от 0 до 100")
    if "enabled" in result and result["enabled"] is None:
        raise UsageError("enabled не может быть пустым")
    if "is_copycat" in result and result["is_copycat"] is None:
        raise UsageError("is_copycat не может быть пустым")
    return result


async def _require_bot(db, ref: str) -> CopycatBot:
    row = await find_copycat_bot(db, ref)
    if row is None:
        raise UsageError(f"витрина {ref!r} не найдена")
    return row


async def _require_group(db, name: str) -> CopycatGroup:
    group = await get_copycat_group_by_name(db, name)
    if group is None:
        raise UsageError(f"группа {name!r} не найдена")
    return group


async def _require_scheme(db, name: str) -> PriceScheme:
    scheme = await get_scheme_by_name(db, name)
    if scheme is None:
        raise UsageError(f"ценовая схема {name!r} не найдена")
    return scheme


async def _apply_bot_fields(db, row: Any, values: dict[str, Any]) -> None:
    """Поля бота или группы: group и price_scheme задаются именем."""
    for key, value in values.items():
        if key == "group":
            row.group_id = None if value is None else (await _require_group(db, value)).id
        elif key == "price_scheme":
            row.price_scheme_id = None if value is None else (await _require_scheme(db, value)).id
        elif key == "aurapay_shop":
            row.aurapay_shop = _require_aurapay_shop(value)
        else:
            setattr(row, key, value)


def _require_aurapay_shop(value: Optional[str]) -> Optional[str]:
    shop = normalize_shop(value)
    if shop is None:
        return None
    shops = settings.get_aurapay_extra_shops()
    if shop not in shops:
        known = ", ".join(sorted(shops)) or "нет"
        raise UsageError(f"касса AuraPay {shop!r} не описана в AURAPAY_EXTRA_SHOPS; есть: {known}")
    return shop


def _scheme_name(scheme_id: Optional[int]) -> str:
    if scheme_id is None:
        return "—"
    return _scheme_names.get(scheme_id, f"#{scheme_id}")


def _parse_rubles(value: str) -> int:
    try:
        rubles = int(value)
    except ValueError as error:
        raise UsageError(f"цена {value!r} не целое число рублей") from error
    if rubles <= 0:
        raise UsageError("цена должна быть больше нуля")
    return rubles * 100


def _show_scheme(scheme: PriceScheme) -> None:
    trial = f"{scheme.trial_days} дн." if scheme.trial_days else "глобальный"
    floor = " пол=да" if scheme.enforce_floor else ""
    print(f"#{scheme.id:<3} {scheme.name:<24} триал={trial}{floor}")
    snapshot = snapshot_from_row(scheme)
    for code, tariff in snapshot.tariffs.items():
        devices = tariff.device_limit if tariff.device_limit is not None else "как у тарифа"
        prices = ", ".join(f"{days}д={price // 100}₽" for days, price in sorted(tariff.prices.items()))
        print(f"  {code:<5} устройств={devices}  {prices or 'цен нет'}")


async def _require_owner(db, ref: str) -> BotOwner:
    from sqlalchemy import select

    ref = (ref or "").strip()
    query = select(BotOwner).join(User, User.id == BotOwner.user_id)
    if ref.startswith("user:"):
        query = query.where(BotOwner.user_id == int(ref.split(":", 1)[1]))
    elif ref.isdigit():
        query = query.where(User.telegram_id == int(ref))
    else:
        raise UsageError("владелец указывается Telegram id или user:<id>")
    owners = list((await db.execute(query)).scalars().all())
    if not owners:
        raise UsageError(f"владелец {ref!r} не найден")
    if len(owners) > 1:
        raise UsageError(f"у Telegram id {ref} несколько аккаунтов-владельцев, укажите user:<id>")
    return owners[0]


def _fmt_weights(weights: Optional[dict]) -> str:
    if weights is None:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in weights.items()) or "{} (всё выключено)"


def _bot_line(row: CopycatBot) -> str:
    status = "on " if row.enabled else "off"
    kind = "" if row.is_copycat else " [зеркало]"
    group = row.group.name if row.group else "-"
    weights = effective_gateway_weights(row, row.group)
    routing = f" роутер={weights[1]}" if weights else ""
    share = effective_owner_share_percent(row, row.group)
    share_text = f" доля={share}%" if share is not None else ""
    scheme_id = effective_price_scheme_id(row, row.group)
    scheme_text = f" цены={_scheme_name(scheme_id)}" if scheme_id is not None else ""
    owner_text = f" владелец=user:{row.owner_user_id}" if row.owner_user_id else ""
    shop_text = f" aurapay={row.aurapay_shop}" if row.aurapay_shop else ""
    return (
        f"#{row.id:<4} {status} @{row.username or '?':<28} tg={row.telegram_bot_id or '-':<12} "
        f"группа={group}{kind}{routing}{share_text}{scheme_text}{owner_text}{shop_text}"
    )


def _show_bot(row: CopycatBot) -> None:
    group = row.group
    print(_bot_line(row))
    print(f"  title: {row.title or '—'}")
    print(f"  token: {row.token[:12]}…")
    for field in ("logo", "image_url") + BOT_BRAND_FIELDS:
        own = getattr(row, field, None)
        inherited = getattr(group, field, None) if group and field in GROUP_BRAND_FIELDS else None
        if own is not None and own != "":
            print(f"  {field}: {own}")
        elif inherited:
            print(f"  {field}: {inherited}  (группа)")
    weights = effective_gateway_weights(row, group)
    if weights:
        print(f"  веса роутера ({weights[1]}): {_fmt_weights(weights[0])}")
    else:
        print("  веса роутера: глобальные")
    share = effective_owner_share_percent(row, group)
    print(f"  доля владельца: {share if share is not None else '—'}%  owner_user_id: {row.owner_user_id or '—'}")
    own = " (своя)" if row.price_scheme_id is not None else " (группы)"
    scheme_id = effective_price_scheme_id(row, group)
    print(f"  ценовая схема: {_scheme_name(scheme_id)}{own if scheme_id is not None else ' (цены основного бота)'}")
    print(f"  касса AuraPay: {row.aurapay_shop or 'основная'}")


def _show_group(group: CopycatGroup, count: int) -> None:
    share = f"{group.owner_share_percent}%" if group.owner_share_percent is not None else "—"
    print(f"#{group.id:<3} {group.name:<20} ботов={count:<4} доля владельца={share}")
    if group.description:
        print(f"  {group.description}")
    for field in GROUP_BRAND_FIELDS:
        value = getattr(group, field)
        if value:
            print(f"  {field}: {value}")
    if group.payment_gateway_weights is not None:
        print(f"  веса роутера: {_fmt_weights(group.payment_gateway_weights)}")
    if group.price_scheme_id is not None:
        print(f"  ценовая схема: {_scheme_name(group.price_scheme_id)}")


async def run(args: argparse.Namespace) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    PriceScheme.__table__,
                    PriceSchemeTariff.__table__,
                    CopycatGroup.__table__,
                    CopycatBot.__table__,
                    BotOwner.__table__,
                    OwnerPayout.__table__,
                ],
            )
        )

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        command = args.command
        _scheme_names.clear()
        _scheme_names.update(
            {scheme_id: name for scheme_id, name in (await db.execute(select(PriceScheme.id, PriceScheme.name))).all()}
        )

        if command == "list":
            group_id = (await _require_group(db, args.group)).id if args.group else None
            rows = await list_copycat_bots(db, group_id=group_id)
            for row in rows:
                print(_bot_line(row))
            enabled = sum(1 for row in rows if row.enabled)
            print(f"Всего: {len(rows)}, включено: {enabled}")
            return

        if command == "show":
            _show_bot(await _require_bot(db, args.bot))
            return

        if command == "add":
            token = args.token.strip()
            if await get_copycat_bot_by_token(db, token):
                raise UsageError("такой токен уже заведён")
            row = CopycatBot(token=token, enabled=True, is_copycat=not args.mirror)
            values = _parse_assignments(args.fields, BOT_FIELDS)
            if args.group:
                values["group"] = args.group
            elif not args.mirror and "group" not in values:
                if await get_copycat_group_by_name(db, DEFAULT_GROUP_NAME):
                    values["group"] = DEFAULT_GROUP_NAME
            await _apply_bot_fields(db, row, values)
            db.add(row)
            await db.commit()
            print(f"Добавлена витрина #{row.id}; бот поднимет её при следующей синхронизации")
            return

        if command == "set":
            row = await _require_bot(db, args.bot)
            values = _parse_assignments(args.fields, BOT_FIELDS)
            if not values:
                raise UsageError("нечего менять")
            await _apply_bot_fields(db, row, values)
            await db.commit()
            print(f"Витрина #{row.id} обновлена")
            return

        if command in ("enable", "disable"):
            row = await _require_bot(db, args.bot)
            row.enabled = command == "enable"
            await db.commit()
            print(f"Витрина #{row.id} {'включена' if row.enabled else 'выключена'}")
            return

        if command == "delete":
            row = await _require_bot(db, args.bot)
            await db.delete(row)
            await db.commit()
            print(f"Витрина #{row.id} удалена (её пользователи станут пользователями основного бренда)")
            return

        if command == "groups":
            counts = await group_bot_counts(db)
            for group in await list_copycat_groups(db):
                _show_group(group, counts.get(group.id, 0))
            if counts.get(None):
                print(f"Без группы: {counts[None]}")
            return

        if command == "group-add":
            name = args.name.strip()
            if await get_copycat_group_by_name(db, name):
                raise UsageError(f"группа {name!r} уже есть")
            group = CopycatGroup(name=name)
            await _apply_bot_fields(db, group, _parse_assignments(args.fields, GROUP_FIELDS))
            db.add(group)
            await db.commit()
            print(f"Группа #{group.id} {group.name} создана")
            return

        if command == "group-set":
            group = await _require_group(db, args.name)
            values = _parse_assignments(args.fields, GROUP_FIELDS)
            if not values:
                raise UsageError("нечего менять")
            if "name" in values and not values["name"]:
                raise UsageError("имя группы не может быть пустым")
            await _apply_bot_fields(db, group, values)
            await db.commit()
            print(f"Группа {group.name} обновлена")
            return

        if command == "group-delete":
            group = await _require_group(db, args.name)
            for row in await list_copycat_bots(db, group_id=group.id):
                row.group_id = None
            await db.delete(group)
            await db.commit()
            print(f"Группа {args.name} удалена")
            return

        if command == "weights":
            target = args.target
            if target.startswith("group:"):
                holder: Any = await _require_group(db, target.split(":", 1)[1])
            else:
                holder = await _require_bot(db, target)
            if args.clear:
                holder.payment_gateway_weights = None
            else:
                if not args.pairs:
                    raise UsageError("укажите веса gateway=N или --clear")
                weights: dict[str, int] = {}
                for pair in args.pairs:
                    gateway, _, value = pair.partition("=")
                    gateway = gateway.strip().lower()
                    if gateway not in ROUTED_GATEWAYS:
                        raise UsageError(f"шлюз {gateway!r} не из роутера: {', '.join(ROUTED_GATEWAYS)}")
                    try:
                        weights[gateway] = max(0, int(value))
                    except ValueError as error:
                        raise UsageError(f"вес {pair!r} не число") from error
                holder.payment_gateway_weights = weights
            await db.commit()
            print(f"Веса {target}: {_fmt_weights(holder.payment_gateway_weights)}")
            return

        if command == "import-yaml":
            result = await import_legacy_mirrors(db, group_name=args.group or DEFAULT_GROUP_NAME)
            await db.commit()
            print(f"Импорт: добавлено {result.added}, пропущено {result.skipped}")
            return

        if command == "schemes":
            schemes = (
                await db.execute(select(PriceScheme).order_by(PriceScheme.id))
            ).scalars().all()
            for scheme in schemes:
                await db.refresh(scheme, ["tariffs"])
                _show_scheme(scheme)
            if not schemes:
                print("Схем нет: все боты продают по ценам основного бота")
            return

        if command == "scheme-show":
            _show_scheme(await _require_scheme(db, args.name))
            return

        if command == "scheme-add":
            name = args.name.strip()
            if await get_scheme_by_name(db, name):
                raise UsageError(f"схема {name!r} уже есть")
            values = _parse_assignments(args.fields, SCHEME_FIELDS)
            scheme = PriceScheme(name=name, enforce_floor=bool(values.pop("enforce_floor", False)))
            for key, value in values.items():
                setattr(scheme, key, value)
            db.add(scheme)
            await db.commit()
            print(f"Схема #{scheme.id} {scheme.name} создана; цены — scheme-tariff {scheme.name} solo 30=…")
            return

        if command == "scheme-set":
            scheme = await _require_scheme(db, args.name)
            values = _parse_assignments(args.fields, SCHEME_FIELDS)
            if not values:
                raise UsageError("нечего менять")
            if "name" in values and not values["name"]:
                raise UsageError("имя схемы не может быть пустым")
            if "enforce_floor" in values and values["enforce_floor"] is None:
                values["enforce_floor"] = False
            for key, value in values.items():
                setattr(scheme, key, value)
            await db.commit()
            print(f"Схема {scheme.name} обновлена")
            return

        if command == "scheme-tariff":
            scheme = await _require_scheme(db, args.name)
            plan_code = args.plan.strip().lower()
            if plan_code not in PLAN_CODES:
                raise UsageError(f"тариф {plan_code!r}; доступны: {', '.join(PLAN_CODES)}")
            prices: dict[int, int] = {}
            device_limit: Any = ...
            for pair in args.pairs:
                key, sep, value = pair.partition("=")
                if not sep:
                    raise UsageError(f"ожидалось key=value, получено {pair!r}")
                key = key.strip()
                if key == "devices":
                    device_limit = int(value) if value.strip() else None
                elif key.isdigit():
                    prices[int(key)] = _parse_rubles(value.strip())
                else:
                    raise UsageError(f"неизвестный ключ {key!r}: devices или число дней")
            if not prices and device_limit is ...:
                raise UsageError("укажите devices=N и/или цены ДНЕЙ=РУБЛЕЙ")
            set_tariff(scheme, plan_code, prices=prices, device_limit=device_limit)
            await db.commit()
            await db.refresh(scheme, ["tariffs"])
            _show_scheme(scheme)
            return

        if command == "scheme-clone":
            dst = args.dst.strip()
            if await get_scheme_by_name(db, dst):
                raise UsageError(f"схема {dst!r} уже есть")
            from app.services.price_scheme_service import load_schemes

            await load_schemes(db)
            source = None
            if args.src != "-":
                source = snapshot_from_row(await _require_scheme(db, args.src))
            scheme = clone_scheme(source, dst, enforce_floor=bool(source and source.enforce_floor))
            db.add(scheme)
            await db.commit()
            await db.refresh(scheme, ["tariffs"])
            _show_scheme(scheme)
            return

        if command == "scheme-delete":
            scheme = await _require_scheme(db, args.name)
            await db.delete(scheme)
            await db.commit()
            print(f"Схема {args.name} удалена")
            return

        if command == "owners":
            from sqlalchemy import func

            rows = (
                await db.execute(
                    select(BotOwner, User.telegram_id, User.username, func.count(CopycatBot.id))
                    .join(User, User.id == BotOwner.user_id)
                    .outerjoin(CopycatBot, CopycatBot.owner_user_id == BotOwner.user_id)
                    .group_by(BotOwner.id, User.telegram_id, User.username)
                    .order_by(BotOwner.id)
                )
            ).all()
            from app.config import settings

            # Строки режима VPN без ботов — просто выбор «попробовать VPN».
            rows = [row for row in rows if row[0].mode == "owner" or row[3]]
            for owner, telegram_id, username, bots in rows:
                share = owner.revshare_percent
                share_text = f"{share}%" if share is not None else f"{settings.OWNER_DEFAULT_REVSHARE_PERCENT}% (по умолчанию)"
                print(
                    f"user:{owner.user_id:<8} tg={telegram_id:<12} @{username or '-':<24} "
                    f"ботов={bots:<3} ревшара={share_text} режим={owner.mode}"
                )
            print(f"Всего владельцев: {len(rows)}")
            return

        if command == "owner-set":
            owner = await _require_owner(db, args.owner)
            values = _parse_assignments(args.fields, OWNER_FIELDS)
            if "revshare" in values:
                owner.revshare_percent = values["revshare"]
            await db.commit()
            print(f"Владелец user:{owner.user_id} обновлён")
            return

        raise UsageError(f"неизвестная команда {command!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.copycats",
        description="Копикеты в БД",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list")
    p.add_argument("--group")
    sub.add_parser("show").add_argument("bot")

    p = sub.add_parser("add")
    p.add_argument("token")
    p.add_argument("--group")
    p.add_argument("--mirror", action="store_true", help="зеркало под основным брендом")
    p.add_argument("fields", nargs="*")

    p = sub.add_parser("set")
    p.add_argument("bot")
    p.add_argument("fields", nargs="+")

    for name in ("enable", "disable", "delete"):
        sub.add_parser(name).add_argument("bot")

    sub.add_parser("groups")
    p = sub.add_parser("group-add")
    p.add_argument("name")
    p.add_argument("fields", nargs="*")
    p = sub.add_parser("group-set")
    p.add_argument("name")
    p.add_argument("fields", nargs="+")
    sub.add_parser("group-delete").add_argument("name")

    p = sub.add_parser("weights")
    p.add_argument("target", help="BOT или group:NAME")
    p.add_argument("pairs", nargs="*")
    p.add_argument("--clear", action="store_true")

    p = sub.add_parser("import-yaml")
    p.add_argument("--group")

    sub.add_parser("schemes")
    sub.add_parser("scheme-show").add_argument("name")
    for name, required in (("scheme-add", "*"), ("scheme-set", "+")):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("fields", nargs=required)
    p = sub.add_parser("scheme-tariff")
    p.add_argument("name")
    p.add_argument("plan")
    p.add_argument("pairs", nargs="+")
    p = sub.add_parser("scheme-clone")
    p.add_argument("src")
    p.add_argument("dst")
    sub.add_parser("scheme-delete").add_argument("name")

    sub.add_parser("owners")
    p = sub.add_parser("owner-set")
    p.add_argument("owner")
    p.add_argument("fields", nargs="+")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except UsageError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
