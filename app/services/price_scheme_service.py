"""Ценовые схемы витрин: триал и цены Solo/Plus/Pro по периодам.

Схема цепляется к боту или к группе копикетов (бот перебивает группу) — это
делает CopycatRuntime, кладя id схемы в bot_registry. Цены читаются из
снимка в памяти: его перегружает синхронизация копикетов и правка цен
владельцем, поэтому синхронные помощники plan_pricing_service работают без
запросов в БД.

Схема в ценах — это когорта `scheme:<id>` (см. plan_pricing_service).
Тариф или период, которого в схеме нет, берётся из цен основного бота для
новых пользователей. У схем с `enforce_floor` цена не опускается ниже
минимальных для покупателей: схема OWNER_PRICE_FLOOR_SCHEME, а без неё —
те же цены основного бота для новых.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import PriceScheme, PriceSchemeTariff, SubscriptionPlan

logger = logging.getLogger(__name__)

SCHEME_COHORT_PREFIX = "scheme:"
# Когорта цен основного бота, на которую опираются схемы.
BASE_COHORT = "new"
OWNER_PLAN_CODES = ("solo", "plus", "pro")


@dataclass(frozen=True)
class SchemeTariff:
    device_limit: Optional[int]
    prices: Mapping[int, int]


@dataclass(frozen=True)
class SchemeSnapshot:
    id: int
    name: str
    trial_days: Optional[int]
    enforce_floor: bool
    tariffs: Mapping[str, SchemeTariff] = field(default_factory=dict)


_schemes: dict[int, SchemeSnapshot] = {}
_plan_codes: dict[int, str] = {}
# Цены основного бота для новых: {plan_code: {дней: копейки}}.
_base_prices: dict[str, dict[int, int]] = {}


# ---------------------------------------------------------------- когорта


def scheme_cohort(scheme_id: int) -> str:
    return f"{SCHEME_COHORT_PREFIX}{scheme_id}"


def scheme_id_from_cohort(cohort: Optional[str]) -> Optional[int]:
    if not cohort or not str(cohort).startswith(SCHEME_COHORT_PREFIX):
        return None
    try:
        return int(str(cohort)[len(SCHEME_COHORT_PREFIX):])
    except ValueError:
        return None


def get_scheme(scheme_id: Optional[int]) -> Optional[SchemeSnapshot]:
    if scheme_id is None:
        return None
    return _schemes.get(scheme_id)


def scheme_for_bot(bot_id: Optional[int]) -> Optional[SchemeSnapshot]:
    from app.utils import bot_registry

    return get_scheme(bot_registry.get_price_scheme_id(bot_id))


def interaction_bot_id(user: object) -> Optional[int]:
    """Бот, в котором пользователь сейчас покупает: бот апдейта, вне бота — бот регистрации."""
    from app.branding.context import current_bot_id

    bot_id = current_bot_id()
    if bot_id is not None:
        return bot_id
    return getattr(user, "bot_id", None)


def scheme_for_user(user: object) -> Optional[SchemeSnapshot]:
    return scheme_for_bot(interaction_bot_id(user))


# ---------------------------------------------------------------- цены


def _floor_prices(plan_code: str) -> dict[int, int]:
    floor_name = (settings.OWNER_PRICE_FLOOR_SCHEME or "").strip().lower()
    if floor_name:
        for snapshot in _schemes.values():
            if snapshot.name.lower() == floor_name:
                tariff = snapshot.tariffs.get(plan_code)
                return dict(tariff.prices) if tariff else {}
    return dict(_base_prices.get(plan_code, {}))


def floor_prices(plan_code: str) -> dict[int, int]:
    """Минимальные цены для покупателей ботов владельцев {дней: копейки}."""
    return _floor_prices(plan_code)


def base_prices(plan_code: str) -> dict[int, int]:
    return dict(_base_prices.get(plan_code, {}))


def scheme_prices_for_plan(
    scheme: SchemeSnapshot, plan_code: Optional[str], fallback: Optional[Mapping[int, int]] = None
) -> dict[int, int]:
    """Цены тарифа в схеме {дней: копейки}: недостающие — из цен основного бота,
    у схем с полом — не ниже минимальных."""
    if not plan_code:
        return dict(fallback or {})
    prices = dict(fallback if fallback is not None else _base_prices.get(plan_code, {}))
    tariff = scheme.tariffs.get(plan_code)
    if tariff is not None:
        prices.update(tariff.prices)
    if scheme.enforce_floor:
        for period, floor in _floor_prices(plan_code).items():
            if period in prices and prices[period] < floor:
                prices[period] = floor
    # Схема продаёт только периоды, которые бот умеет покупать.
    from app.services.plan_pricing_service import SUPPORTED_PERIOD_DAYS

    return {period: price for period, price in prices.items() if period in SUPPORTED_PERIOD_DAYS}


def plan_code_for_id(plan_id: int) -> Optional[str]:
    return _plan_codes.get(plan_id)


def scheme_device_limit(scheme: Optional[SchemeSnapshot], plan_code: Optional[str]) -> Optional[int]:
    if scheme is None or not plan_code:
        return None
    tariff = scheme.tariffs.get(plan_code)
    return tariff.device_limit if tariff else None


def trial_days_for_user(user: object) -> int:
    """Длительность триала: из схемы бота, иначе глобальная."""
    scheme = scheme_for_user(user)
    if scheme is not None and scheme.trial_days:
        return int(scheme.trial_days)
    return int(settings.TRIAL_DURATION_DAYS)


def derive_period_prices(monthly_rub: int, plan_code: str) -> dict[int, int]:
    """Цены владельца {дней: копейки} из месячной: множители из настройки,
    округление вверх до рубля, не ниже минимальных для покупателей."""
    floor = _floor_prices(plan_code)
    prices = {30: int(monthly_rub) * 100}
    for days, factor in settings.get_owner_price_period_multipliers().items():
        prices[int(days)] = int(math.ceil(monthly_rub * factor)) * 100
    for period, value in list(prices.items()):
        minimum = floor.get(period)
        if minimum is not None and value < minimum:
            prices[period] = int(minimum)
    return prices


# ---------------------------------------------------------------- снимок


def _parse_prices(raw: Any) -> dict[int, int]:
    result: dict[int, int] = {}
    if not isinstance(raw, Mapping):
        return result
    for period, value in raw.items():
        try:
            result[int(period)] = int(value)
        except (TypeError, ValueError):
            logger.warning("Некорректная цена в схеме: %r=%r", period, value)
    return result


def snapshot_from_row(row: PriceScheme) -> SchemeSnapshot:
    return SchemeSnapshot(
        id=row.id,
        name=row.name,
        trial_days=row.trial_days,
        enforce_floor=bool(row.enforce_floor),
        tariffs={
            tariff.plan_code: SchemeTariff(
                device_limit=tariff.device_limit,
                prices=_parse_prices(tariff.period_prices),
            )
            for tariff in row.tariffs
        },
    )


async def load_schemes(db: AsyncSession) -> None:
    """Перечитывает схемы и цены основного бота в память."""
    from app.services.plan_pricing_service import select_prices_for_audience

    schemes = (
        await db.execute(select(PriceScheme).options(selectinload(PriceScheme.tariffs)))
    ).scalars().all()
    plans = (
        await db.execute(select(SubscriptionPlan).options(selectinload(SubscriptionPlan.prices)))
    ).scalars().all()

    _schemes.clear()
    _schemes.update({row.id: snapshot_from_row(row) for row in schemes})
    _plan_codes.clear()
    _plan_codes.update({plan.id: plan.code for plan in plans})
    _base_prices.clear()
    _base_prices.update({plan.code: select_prices_for_audience(plan, BASE_COHORT) for plan in plans})


async def reload_schemes() -> None:
    from app.database.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await load_schemes(db)
        await db.commit()


def set_snapshot_for_tests(
    schemes: list[SchemeSnapshot],
    *,
    plan_codes: Optional[Mapping[int, str]] = None,
    base: Optional[Mapping[str, Mapping[int, int]]] = None,
) -> None:
    _schemes.clear()
    _schemes.update({s.id: s for s in schemes})
    _plan_codes.clear()
    _plan_codes.update(plan_codes or {})
    _base_prices.clear()
    _base_prices.update({code: dict(prices) for code, prices in (base or {}).items()})


# ---------------------------------------------------------------- запись


async def get_scheme_by_name(db: AsyncSession, name: str) -> Optional[PriceScheme]:
    from sqlalchemy import func

    result = await db.execute(
        select(PriceScheme)
        .options(selectinload(PriceScheme.tariffs))
        .where(func.lower(PriceScheme.name) == (name or "").strip().lower())
    )
    return result.scalar_one_or_none()


async def get_scheme_row(db: AsyncSession, scheme_id: int) -> Optional[PriceScheme]:
    result = await db.execute(
        select(PriceScheme).options(selectinload(PriceScheme.tariffs)).where(PriceScheme.id == scheme_id)
    )
    return result.scalar_one_or_none()


def set_tariff(
    scheme: PriceScheme,
    plan_code: str,
    *,
    prices: Optional[Mapping[int, int]] = None,
    device_limit: Any = ...,
) -> PriceSchemeTariff:
    """Задаёт цены (сливая с уже заданными) и устройства тарифа схемы."""
    tariff = next((t for t in scheme.tariffs if t.plan_code == plan_code), None)
    if tariff is None:
        tariff = PriceSchemeTariff(plan_code=plan_code, period_prices={})
        scheme.tariffs.append(tariff)
    if prices:
        merged = {str(k): int(v) for k, v in (tariff.period_prices or {}).items()}
        merged.update({str(int(k)): int(v) for k, v in prices.items()})
        tariff.period_prices = merged
    if device_limit is not ...:
        tariff.device_limit = device_limit
    return tariff


def clone_scheme(source: Optional[SchemeSnapshot], name: str, *, enforce_floor: bool) -> PriceScheme:
    """Новая схема (не добавлена в сессию) — копия `source` или цен основного бота."""
    scheme = PriceScheme(
        name=name,
        trial_days=source.trial_days if source else None,
        enforce_floor=enforce_floor,
    )
    codes = set(OWNER_PLAN_CODES) | set(source.tariffs if source else ())
    for code in sorted(codes):
        if source is not None:
            prices = scheme_prices_for_plan(source, code)
            device_limit = scheme_device_limit(source, code)
        else:
            prices = base_prices(code)
            device_limit = None
        if not prices and device_limit is None:
            continue
        set_tariff(scheme, code, prices=prices, device_limit=device_limit)
    return scheme
