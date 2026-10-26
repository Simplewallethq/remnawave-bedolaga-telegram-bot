"""API мини-аппа конструктора: всё управление ботами владельца.

Авторизация — `Authorization: tma <initData>`, подпись проверяется токеном
бота-конструктора. Владелец — его аккаунт в скоупе платформы (как в боте),
правила — функции платформы владельцев: те же, что во встроенных экранах.

Ошибка — HTTP 4xx с `{"detail": {"error": текст, "field": поле, "code": …}}`.
"""

from __future__ import annotations

import logging
import zlib
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import get_db
from app.database.models import CopycatBot, OwnerPayout, User
from app.localization.texts import get_texts
from app.services import owner_platform_service as platform
from app.utils.telegram_webapp import TelegramWebAppAuthError, parse_webapp_init_data
from app.utils.tron import is_trc20_address

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["constructor"])

MAX_UPLOAD = 10 * 1024 * 1024
PLAN_DEVICES = {"solo": 1, "plus": 3, "pro": 10}


def fail(error: str, field: Optional[str] = None, status: int = 400, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": error, "field": field, **extra})


# ---------------------------------------------------------------- авторизация


def _init_data_shape(raw: str) -> dict:
    """Что пришло в initData — без значений: для разбора отказов в логе."""
    import time
    from urllib.parse import parse_qsl

    try:
        fields = dict(parse_qsl(raw, keep_blank_values=True))
    except ValueError:
        return {"len": len(raw), "parse_error": True}
    auth_date = fields.get("auth_date", "")
    return {
        "len": len(raw),
        "keys": sorted(fields),
        "age_s": int(time.time()) - int(auth_date) if auth_date.isdigit() else None,
    }


async def tg_user(authorization: str = Header(default="")) -> service.TgUser:
    if not authorization.startswith("tma "):
        logger.warning("Конструктор: запрос без initData")
        raise fail("Откройте приложение из Telegram", status=401)
    try:
        # Telegram Desktop кэширует initData и подолгу отдаёт старый auth_date
        # (tdesktop#28303, «not planned»), поэтому срок — из настройки, а не сутки.
        data = parse_webapp_init_data(
            authorization[4:],
            settings.CONSTRUCTOR_BOT_TOKEN.strip(),
            max_age_seconds=int(settings.CONSTRUCTOR_INIT_DATA_MAX_AGE_SECONDS),
        )
    except (TelegramWebAppAuthError, ValueError) as error:
        logger.warning(
            "Конструктор: initData не принят (%s), initData %s", error, _init_data_shape(authorization[4:])
        )
        raise fail("Откройте приложение из Telegram", status=401) from None
    user = data.get("user")
    if not isinstance(user, dict) or not user.get("id"):
        raise fail("Откройте приложение из Telegram", status=401)
    return service.TgUser(
        id=int(user["id"]),
        username=user.get("username"),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
        language_code=user.get("language_code"),
    )


async def _account(db: AsyncSession, tg: service.TgUser) -> User:
    try:
        return await service.ensure_owner_account(db, tg)
    except service.ConnectError as error:
        raise fail(error.message, status=403) from None


async def _owned_bot(db: AsyncSession, tg: service.TgUser, row_id: int) -> tuple[User, CopycatBot]:
    user = await service.find_owner_account(db, tg.id)
    row = await platform.get_owner_bot(db, user.id, row_id) if user else None
    if row is None or not row.enabled:
        raise fail("Бот не найден", status=404)
    return user, row


# ---------------------------------------------------------------- сериализация


def _rub(kopeks: int) -> float:
    return round(int(kopeks) / 100, 2)


def _cover_version(row: CopycatBot) -> str:
    return format(zlib.crc32((row.logo or service.DEFAULT_COVER).encode()), "x")


def bot_short(row: CopycatBot) -> dict:
    return {
        "id": row.id,
        "username": row.username,
        "title": row.title or row.username,
        "link": service.bot_link(row),
        "cover_version": _cover_version(row),
    }


async def bot_detail(db: AsyncSession, row: CopycatBot) -> dict:
    stats = await platform.bot_stats(db, row)
    support = (row.support or "").strip().lstrip("@") or None
    return {
        **bot_short(row),
        "support": support,
        "custom_cover": bool(row.logo) and row.logo != service.DEFAULT_COVER,
        "created_at": row.created_at.isoformat() if getattr(row, "created_at", None) else None,
        "prices": {
            "monthly": platform.current_monthly_prices(row),
            "periods": platform.current_period_prices(row),
        },
        "stats": {
            "users_total": stats.users_total,
            "users_yesterday": stats.users_yesterday,
            "turnover": _rub(stats.turnover_total),
        },
    }


def _payout_json(payout: OwnerPayout) -> dict:
    amount, fee = Decimal(payout.amount_usdt), Decimal(payout.fee_usdt)
    return {
        "id": payout.id,
        "amount_usdt": str(amount),
        "fee_usdt": str(fee),
        "send_usdt": str(amount - fee),
        "amount_rub": _rub(payout.amount_rub_kopeks),
        "wallet": payout.trc20_address,
        "status": payout.status,
        "created_at": payout.created_at.isoformat() if payout.created_at else None,
    }


def _price_errors(monthly: dict[str, int]) -> dict[str, str]:
    texts = get_texts("ru")
    return {
        code: texts.t(key, platform.PRICE_ERROR_DEFAULTS[key]).format(**values)
        for code, (key, values) in platform.monthly_prices_errors(monthly).items()
    }


def _parse_prices(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise fail("Укажите цены Solo, Plus и Pro", "prices")
    monthly = {}
    for code, title in platform.PLAN_TITLES.items():
        value = raw.get(code)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 100_000:
            raise fail(f"{title}: введите целое число рублей", f"prices.{code}")
        monthly[code] = value
    return monthly


# ---------------------------------------------------------------- общее


@router.get("/config")
async def get_config() -> dict:
    return {
        "plans": {
            code: {"title": title, "devices": PLAN_DEVICES[code], "floor": platform.floor_monthly_rub(code)}
            for code, title in platform.PLAN_TITLES.items()
        },
        "periods": [30, *sorted(settings.get_owner_price_period_multipliers())],
        "revshare_default": int(settings.OWNER_DEFAULT_REVSHARE_PERCENT),
        "hold_days": int(settings.OWNER_PAYOUT_HOLD_DAYS),
        "payout_fee_usdt": str(platform.payout_fee()),
        "max_bots": int(settings.OWNER_MAX_BOTS),
        "example_url": (settings.CONSTRUCTOR_EXAMPLE_BOT_URL or "").strip() or None,
    }


@router.get("/state")
async def get_state(tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)) -> dict:
    user = await service.find_owner_account(db, tg.id)
    owner = await platform.get_owner(db, user.id) if user else None
    bots = await platform.list_owner_bots(db, user.id, enabled_only=True) if user else []
    stats = await platform.owner_stats(db, user.id) if user else platform.OwnerStats(0, 0, 0)
    earnings = await platform.owner_earnings(db, owner) if owner else None
    return {
        "user": {"id": tg.id, "username": tg.username, "first_name": tg.first_name},
        "revshare": platform.revshare_percent(owner),
        "wallet": owner.trc20_address if owner else None,
        "stats": {
            "active_bots": stats.active_bots,
            "users_total": stats.users_total,
            "users_yesterday": stats.users_yesterday,
        },
        "available": _rub(earnings.available_kopeks) if earnings else 0,
        "bots": [bot_short(row) for row in bots],
    }


# ---------------------------------------------------------------- создание


class TokenBody(BaseModel):
    text: str = ""


@router.post("/token")
async def check_token(
    body: TokenBody, tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Токен из любого текста (чистый или весь ответ BotFather) → можно ли подключить."""
    token = service.find_token(body.text)
    if not token:
        raise fail(service.ERRORS["invalid"], "token")
    user = await service.find_owner_account(db, tg.id)
    try:
        info = await service.check_token(db, user.id if user else None, token)
    except service.ConnectError as error:
        raise fail(error.message, "token", code=error.code) from None
    return {
        "username": info.username,
        "title": info.title,
        "restore": info.restore_row_id is not None,
        "foreign_webhook": info.foreign_webhook,
    }


async def _read_image(image: UploadFile) -> bytes:
    data = await image.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise fail("Картинка больше 10 МБ", "image")
    return data


@router.post("/bots")
async def create_bot(
    token: str = Form(...),
    support: str = Form(""),
    force: bool = Form(False),
    image: Optional[UploadFile] = File(None),
    tg: service.TgUser = Depends(tg_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Запуск бота: токен, поддержка, своя обложка (необязательно)."""
    found = service.find_token(token)
    if not found:
        raise fail(service.ERRORS["invalid"], "token")
    user = await _account(db, tg)
    try:
        info = await service.check_token(db, user.id, found)
    except service.ConnectError as error:
        raise fail(error.message, "token", code=error.code) from None

    logo = None
    if info.restore_row_id is None:
        if info.foreign_webhook and not force:
            raise fail(
                "Этот бот уже подключён к другому сервису. Если продолжить, там он перестанет работать.",
                "token",
                409,
                code="webhook",
                username=info.username,
            )
        support_name = platform.normalize_support_username(support)
        if support_name is None:
            raise fail("Нужен username вида @support_name: латиница, цифры и _ (5–32 символа)", "support")
        if image is not None:
            try:
                logo = platform.save_picture_bytes(await _read_image(image), info.bot_id)
            except ValueError:
                raise fail("Эту картинку Telegram не покажет — выберите JPG или PNG обычных пропорций", "image") from None
    try:
        row, restored = await service.launch_bot(db, user, info, support=support, logo=logo)
    except service.ConnectError as error:
        raise fail(error.message, "token", code=error.code) from None
    return {"bot": await bot_detail(db, row), "restored": restored}


# ---------------------------------------------------------------- бот


@router.get("/bots/{row_id}")
async def get_bot(row_id: int, tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)) -> dict:
    _, row = await _owned_bot(db, tg, row_id)
    return {"bot": await bot_detail(db, row)}


@router.get("/bots/{row_id}/status")
async def get_bot_status(row_id: int, tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Токен жив и вебхук смотрит на нас — отдельно: это запросы к Telegram."""
    from app.services.copycat_service import copycat_runtime

    _, row = await _owned_bot(db, tg, row_id)
    state = copycat_runtime.connection_state(row)
    if state == "pending" and copycat_runtime.can_change_live_set:
        # Только что подключён: фоновая синхронизация ещё не подняла бота.
        # В polling-режиме новые боты поднимаются только после рестарта — там
        # «запускается» висело бы вечно, поэтому честно проверяем как есть.
        return {"working": False, "starting": True}
    return {"working": await platform.bot_is_working(row), "starting": False}


class BotPatch(BaseModel):
    support: Optional[str] = None
    prices: Optional[dict[str, Any]] = None


@router.patch("/bots/{row_id}")
async def patch_bot(
    row_id: int, body: BotPatch, tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)
) -> dict:
    _, row = await _owned_bot(db, tg, row_id)
    if body.support is not None:
        support = platform.normalize_support_username(body.support)
        if support is None:
            raise fail("Нужен username вида @support_name: латиница, цифры и _ (5–32 символа)", "support")
        row.support = f"@{support}"
        await db.commit()
        service.refresh_runtime_in_background()
    if body.prices is not None:
        monthly = _parse_prices(body.prices)
        errors = _price_errors(monthly)
        if errors:
            code = next(iter(errors))
            raise fail(errors[code], f"prices.{code}", errors=errors)
        await platform.save_owner_prices(db, row, monthly)
    return {"bot": await bot_detail(db, row)}


class PricesBody(BaseModel):
    prices: dict[str, Any]


@router.post("/prices/preview")
async def preview_prices(body: PricesBody, tg: service.TgUser = Depends(tg_user)) -> dict:
    """Ошибки и цены по периодам для набора месячных цен — до сохранения."""
    monthly = _parse_prices(body.prices)
    return {"errors": _price_errors(monthly), "periods": platform.preview_period_prices(monthly)}


@router.post("/bots/{row_id}/cover")
async def upload_cover(
    row_id: int,
    image: UploadFile = File(...),
    tg: service.TgUser = Depends(tg_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    _, row = await _owned_bot(db, tg, row_id)
    try:
        row.logo = platform.save_picture_bytes(await _read_image(image), row.telegram_bot_id or row.id)
    except ValueError:
        raise fail("Эту картинку Telegram не покажет — выберите JPG или PNG обычных пропорций", "image") from None
    await db.commit()
    service.refresh_runtime_in_background()
    return {"bot": await bot_detail(db, row)}


@router.delete("/bots/{row_id}/cover")
async def reset_cover(row_id: int, tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)) -> dict:
    _, row = await _owned_bot(db, tg, row_id)
    row.logo = service.DEFAULT_COVER
    await db.commit()
    service.refresh_runtime_in_background()
    return {"bot": await bot_detail(db, row)}


@router.get("/bots/{row_id}/cover")
async def get_cover(row_id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    """Обложка бота владельца — без авторизации: <img> не шлёт заголовки,
    а обложку и так видят все клиенты бота."""
    row = await db.get(CopycatBot, row_id)
    if row is None or row.owner_user_id is None:
        raise fail("Нет обложки", status=404)
    path = Path(settings.resolve_bot_logo(row.logo or service.DEFAULT_COVER))
    if not path.is_file():
        path = Path(service.DEFAULT_COVER)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@router.delete("/bots/{row_id}")
async def delete_bot(row_id: int, tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Удаление — выключение: подписчики и настройки остаются за ботом,
    подключение тем же владельцем вернёт всё."""
    _, row = await _owned_bot(db, tg, row_id)
    await platform.disable_bot(db, row)
    service.refresh_runtime_in_background()
    return {"deleted": row.id}


# ---------------------------------------------------------------- заработок


async def _earnings_json(db: AsyncSession, user: Optional[User]) -> dict:
    owner = await platform.get_owner(db, user.id) if user else None
    earnings = await platform.owner_earnings(db, owner) if owner else None
    rate = await platform.get_payout_rate()
    available = earnings.available_kopeks if earnings else 0
    payouts = await platform.list_payouts(db, owner.id) if owner else []
    return {
        "revshare": platform.revshare_percent(owner),
        "turnover_total": _rub(earnings.turnover_total) if earnings else 0,
        "turnover_yesterday": _rub(earnings.turnover_yesterday) if earnings else 0,
        "profit_total": _rub(earnings.profit_total) if earnings else 0,
        "profit_yesterday": _rub(earnings.profit_yesterday) if earnings else 0,
        "paid_out": _rub(earnings.payouts_kopeks) if earnings else 0,
        "available": _rub(available),
        "rate": str(rate.rate.quantize(Decimal("0.01"))) if rate else None,
        "available_usdt": str(platform.rub_to_usdt(available, rate.rate)) if rate else None,
        "fee_usdt": str(platform.payout_fee()),
        "hold_days": int(settings.OWNER_PAYOUT_HOLD_DAYS),
        "wallet": owner.trc20_address if owner else None,
        "payouts": [_payout_json(payout) for payout in payouts],
    }


@router.get("/earnings")
async def get_earnings(tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)) -> dict:
    return await _earnings_json(db, await service.find_owner_account(db, tg.id))


class WalletBody(BaseModel):
    address: str


@router.put("/wallet")
async def set_wallet(body: WalletBody, tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)) -> dict:
    address = body.address.strip()
    if not is_trc20_address(address):
        raise fail("Это не адрес TRON (TRC-20). Проверьте, что копируете адрес для USDT в сети TRON.", "wallet")
    user = await _account(db, tg)
    owner = await platform.get_or_create_owner(db, user.id)
    owner.trc20_address = address
    await db.commit()
    return {"wallet": address}


class PayoutBody(BaseModel):
    amount: str


@router.post("/payouts")
async def create_payout(body: PayoutBody, tg: service.TgUser = Depends(tg_user), db: AsyncSession = Depends(get_db)) -> dict:
    user = await service.find_owner_account(db, tg.id)
    owner = await platform.get_owner(db, user.id) if user else None
    if owner is None or not owner.trc20_address:
        raise fail("Сначала укажите кошелёк для вывода", "wallet")
    amount = platform.parse_usdt(body.amount)
    if amount is None:
        raise fail("Введите сумму числом, например 50", "amount")
    rate = await platform.get_payout_rate()
    if rate is None:
        raise fail("Курс ЦБ сейчас недоступен, попробуйте позже", "amount", 503)

    earnings = await platform.owner_earnings(db, owner)
    error = platform.payout_amount_error(amount, platform.rub_to_usdt(earnings.available_kopeks, rate.rate))
    if error == "OWNER_PAYOUT_TOO_MUCH":
        raise fail("Сумма больше доступной к выводу", "amount")
    if error == "OWNER_PAYOUT_BELOW_FEE":
        raise fail(f"Сумма должна быть больше комиссии ({platform.payout_fee().normalize()} USDT)", "amount")

    payout = await platform.create_payout(db, owner.id, amount, rate)
    if payout is None:
        raise fail("Сумма больше доступной к выводу", "amount")
    await platform.notify_admins_new_payout(user, payout)
    return {"payout": _payout_json(payout), "earnings": await _earnings_json(db, user)}
