"""Конструктор: общее для бота и мини-аппа.

Аккаунт владельца — тот же, что в боте-платформе (OWNER_PLATFORM_BOT_ID):
ищется и заводится в его скоупе. Поэтому владельцы, их боты, кошельки и
заявки на вывод одни и те же в конструкторе и во встроенной платформе.

Подключение бота — через функции платформы (owner_platform_service), правила
те же: занятость, повторное подключение своего выключенного бота, лимит ботов.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from aiogram import Bot
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.copycat import get_copycat_bot_by_token
from app.database.crud.user import create_user, get_user_by_telegram_id
from app.database.models import CopycatBot, User, UserStatus
from app.services import owner_platform_service as platform
from app.services.blacklist_service import blacklist_service

logger = logging.getLogger(__name__)

# Обложка бота, если владелец не загрузил свою: без неё бот показал бы
# картинку основного бренда.
DEFAULT_COVER = "assets/constructor/demo.png"

# Токен внутри любого текста: чистый токен, скопированный или пересланный ответ BotFather.
_TOKEN_FIND_RE = re.compile(r"(?<![A-Za-z0-9_:-])(\d{5,}:[A-Za-z0-9_-]{30,})(?![A-Za-z0-9_-])")
_PROBE_TIMEOUT = 10

ERRORS = {
    "invalid": "Токен не подошёл. Скопируйте его из @BotFather целиком.",
    "active": "Этот бот уже подключён к сервису — выберите другого бота.",
    "foreign": "Этот бот уже подключён к сервису другим владельцем.",
    "reserved": "Этого бота подключить нельзя — выберите другого.",
    "limit": "У вас уже {max} ботов — это максимум.",
    "blocked": "Создание ботов для этого аккаунта недоступно.",
}


class ConnectError(Exception):
    """Бота подключить нельзя. code — ключ ERRORS."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def message(self) -> str:
        return ERRORS[self.code].format(max=settings.OWNER_MAX_BOTS)


@dataclass(frozen=True)
class TgUser:
    """Кто открыл конструктор: из апдейта бота или initData мини-аппа."""

    id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language_code: Optional[str] = None


@dataclass(frozen=True)
class TokenInfo:
    token: str
    bot_id: int
    username: str
    title: str
    # Выключенный бот этого же владельца: подключение вернёт его со всем прежним.
    restore_row_id: Optional[int]
    # У бота вебхук другого сервиса: после подключения тот перестанет получать апдейты.
    foreign_webhook: bool


def find_token(text: Optional[str]) -> Optional[str]:
    match = _TOKEN_FIND_RE.search(text or "")
    return match.group(1) if match else None


_refresh_tasks: set[asyncio.Task] = set()


def refresh_runtime_in_background() -> None:
    """Поднять/обновить витрины, не задерживая ответ владельцу.

    Синхронизация проходит все витрины под общей блокировкой и при сбоях сети
    до Telegram может занять минуту — ждать её в запросе мини-аппа или в шаге
    мастера нельзя. Бот поднимается через секунды, статус на его странице
    показывает «Запускается», пока синхронизация не закончится.
    """
    task = asyncio.create_task(platform.refresh_runtime())
    _refresh_tasks.add(task)  # держим ссылку: иначе задачу может собрать GC
    task.add_done_callback(_refresh_tasks.discard)


# ---------------------------------------------------------------- аккаунт


async def find_owner_account(db: AsyncSession, telegram_id: int) -> Optional[User]:
    user = await get_user_by_telegram_id(db, telegram_id, bot_id=settings.OWNER_PLATFORM_BOT_ID)
    if user is None or user.status == UserStatus.DELETED.value:
        return None
    return user


async def ensure_owner_account(db: AsyncSession, tg: TgUser) -> User:
    """Аккаунт владельца в скоупе платформы; новый — без триала и онбординга VPN."""
    user = await get_user_by_telegram_id(db, tg.id, bot_id=settings.OWNER_PLATFORM_BOT_ID)
    if user is not None and user.status != UserStatus.DELETED.value:
        return user

    is_blacklisted, _ = await blacklist_service.is_user_blacklisted(tg.id, tg.username)
    if is_blacklisted:
        raise ConnectError("blocked")

    if user is not None:
        user.status = UserStatus.ACTIVE.value
        user.username = tg.username
        user.first_name = tg.first_name
        user.last_name = tg.last_name
        user.last_activity = datetime.utcnow()
        await db.commit()
        return user
    return await create_user(
        db,
        telegram_id=tg.id,
        username=tg.username,
        first_name=tg.first_name,
        last_name=tg.last_name,
        language="ru",
        language_code=tg.language_code,
        bot_id=settings.OWNER_PLATFORM_BOT_ID,
    )


# ---------------------------------------------------------------- токен


async def _probe(token: str):
    """getMe и адрес вебхука бота; (None, None) — токен не работает."""
    if not platform.looks_like_token(token):
        return None, None
    bot = Bot(token=token.strip())
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=_PROBE_TIMEOUT)
        try:
            webhook = (await asyncio.wait_for(bot.get_webhook_info(), timeout=_PROBE_TIMEOUT)).url or None
        except Exception:  # noqa: BLE001 - без вебхука подключить можно, это лишь предупреждение
            webhook = None
        return me, webhook
    except Exception as error:  # noqa: BLE001 - любой отказ = токен не подходит
        logger.info("Токен бота из конструктора не прошёл getMe: %s", error)
        return None, None
    finally:
        await bot.session.close()


async def check_token(db: AsyncSession, owner_user_id: Optional[int], token: str) -> TokenInfo:
    """Можно ли подключить бота по токену. ConnectError — нельзя.

    owner_user_id None — аккаунта ещё нет: своих выключенных ботов у него нет.
    """
    from app.services.copycat_service import mirror_webhook_url

    token = token.strip()
    me, webhook = await _probe(token)
    if me is None:
        raise ConnectError("invalid")
    if platform.is_reserved_bot_id(me.id):
        raise ConnectError("reserved")

    user_id = owner_user_id or 0
    occupancy = await platform.occupancy_by_bot_id(db, me.id, user_id)
    if occupancy.status == "free":
        # Токен уже в базе у записи, которая ещё не узнала Telegram id бота.
        token_row = await get_copycat_bot_by_token(db, token)
        if token_row is not None:
            occupancy = platform.classify(token_row, user_id)
    if occupancy.status in ("active", "foreign"):
        raise ConnectError(occupancy.status)

    return TokenInfo(
        token=token,
        bot_id=me.id,
        username=me.username or "",
        title=me.full_name or me.username or "",
        restore_row_id=occupancy.row.id if occupancy.status == "restore" else None,
        foreign_webhook=bool(webhook) and webhook != mirror_webhook_url(me.id),
    )


# ---------------------------------------------------------------- запуск


async def launch_bot(
    db: AsyncSession,
    user: User,
    info: TokenInfo,
    *,
    support: Optional[str],
    logo: Optional[str],
) -> tuple[CopycatBot, bool]:
    """Подключает бота владельцу и запускает подъём в фоне. (запись, вернули ли
    выключенного бота). Вернувшийся бот сохраняет прежние саппорт, обложку и цены."""
    await platform.get_or_create_owner(db, user.id)
    await db.commit()

    if info.restore_row_id is not None:
        row = await platform.get_owner_bot(db, user.id, info.restore_row_id)
        if row is None or row.enabled:
            raise ConnectError("active")
        await platform.restore_bot(db, row, token=info.token, username=info.username, title=info.title)
        refresh_runtime_in_background()
        return row, True

    active = await platform.list_owner_bots(db, user.id, enabled_only=True)
    if len(active) >= int(settings.OWNER_MAX_BOTS):
        raise ConnectError("limit")
    try:
        row = await platform.connect_bot(
            db,
            owner_user_id=user.id,
            token=info.token,
            bot_id=info.bot_id,
            username=info.username,
            title=info.title,
            logo=logo or DEFAULT_COVER,
            support=platform.normalize_support_username(support or ""),
        )
    except IntegrityError:
        # Пока владелец заполнял мастер, этого бота успели подключить.
        await db.rollback()
        raise ConnectError("active") from None
    refresh_runtime_in_background()
    return row, False


def bot_link(row: CopycatBot) -> str:
    return f"https://t.me/{row.username}" if row.username else ""
