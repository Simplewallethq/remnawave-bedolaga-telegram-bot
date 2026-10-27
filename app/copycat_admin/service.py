"""Админка копикетов: проверка токена, картинка и заведение витрины.

Витрина заводится так же, как `python -m scripts.copycats add`: группа
default, Telegram id, юзернейм и название сразу из getMe (без названия запись
до синхронизации считалась бы зеркалом Leto). Имя бренда не задаётся — оно
берётся из названия бота в Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.copycat import get_copycat_bot_by_token, get_copycat_group_by_name
from app.database.models import CopycatBot
from app.services import owner_platform_service as platform
from app.services.copycat_service import DEFAULT_GROUP_NAME

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_MAX_PICTURE_BYTES = 10 * 1024 * 1024
# i.ibb.co временами отвечает 200 с PNG-заглушкой «service unavailable» ровно такого размера.
_IMGBB_PLACEHOLDER_BYTES = 1115


class AddError(Exception):
    """Витрину не завести: текст — для админа."""


@dataclass(frozen=True)
class Candidate:
    token: str
    bot_id: int
    username: str
    title: str
    foreign_webhook: Optional[str]


async def check_token(db: AsyncSession, token: str) -> Candidate:
    row = await get_copycat_bot_by_token(db, token)
    if row is not None:
        raise AddError(f"Этот токен уже заведён: #{row.id} @{row.username or '?'}")
    # Прод рвёт соединения, простаивающие в транзакции дольше секунды, —
    # закрываем её до запроса в Telegram.
    await db.commit()
    me, webhook = await _probe(token)
    if me is None:
        raise AddError("Токен не работает: Telegram его не принял (опечатка, отозван или аккаунт заблокирован)")
    if platform.is_reserved_bot_id(me.id):
        raise AddError("Это служебный бот — витриной его подключить нельзя")
    existing = (
        await db.execute(select(CopycatBot).where(CopycatBot.telegram_bot_id == me.id))
    ).scalar_one_or_none()
    if existing is not None:
        state = "включена" if existing.enabled else "выключена"
        raise AddError(f"Бот @{me.username} уже есть: #{existing.id} ({state}), токен в базе другой")
    from app.services.copycat_service import mirror_webhook_url

    foreign = webhook if webhook and webhook != mirror_webhook_url(me.id) else None
    return Candidate(token=token, bot_id=me.id, username=me.username or "", title=me.full_name or "", foreign_webhook=foreign)


async def _probe(token: str):
    """(getMe, url вебхука) по токену; (None, None) — токен не работает."""
    bot = Bot(token=token)
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=_TIMEOUT)
        try:
            webhook = (await asyncio.wait_for(bot.get_webhook_info(), timeout=_TIMEOUT)).url or None
        except Exception:  # noqa: BLE001 - вебхук только для предупреждения
            webhook = None
        return me, webhook
    except Exception as error:  # noqa: BLE001 - любой отказ = токен не подходит
        logger.info("Админка копикетов: токен не прошёл getMe: %s", error)
        return None, None
    finally:
        await bot.session.close()


def store_picture(data: bytes, bot_id: int) -> str:
    """Картинка приветствия в OWNER_BOT_IMAGES_DIR (смонтированный data/); путь для `logo`."""
    if len(data) == _IMGBB_PLACEHOLDER_BYTES:
        raise AddError("imgbb отдал заглушку «service unavailable» — пришлите ссылку ещё раз или файлом")
    try:
        return platform.save_picture_bytes(data, bot_id)
    except ValueError as error:
        raise AddError("Это не картинка, или Telegram её не примет (слишком вытянутая)") from error


async def download(url: str) -> bytes:
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                if response.status != 200:
                    raise AddError(f"Картинка не скачалась: HTTP {response.status}. Пришлите её файлом")
                data = b""
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data += chunk
                    if len(data) > _MAX_PICTURE_BYTES:
                        break
    except AddError:
        raise
    except Exception as error:  # noqa: BLE001
        raise AddError(f"Картинка не скачалась ({type(error).__name__}). Пришлите её файлом") from error
    if len(data) > _MAX_PICTURE_BYTES:
        raise AddError("Картинка больше 10 МБ")
    return data


async def avatar_bytes(token: str, bot_id: int) -> bytes:
    """Аватарка самого бота — когда отдельной картинки для приветствия нет."""
    bot = Bot(token=token)
    try:
        photos = await asyncio.wait_for(bot.get_user_profile_photos(bot_id, limit=1), timeout=_TIMEOUT)
        if not photos.photos:
            raise AddError("У бота нет аватарки — пришлите картинку")
        file = await bot.get_file(photos.photos[0][-1].file_id)
        buffer = await bot.download_file(file.file_path)
        return buffer.read()
    except AddError:
        raise
    except Exception as error:  # noqa: BLE001
        raise AddError(f"Аватарку не получить ({type(error).__name__}) — пришлите картинку") from error
    finally:
        await bot.session.close()


async def create(db: AsyncSession, candidate: Candidate, *, logo: str, image_url: Optional[str]) -> CopycatBot:
    group = await get_copycat_group_by_name(db, DEFAULT_GROUP_NAME)
    row = CopycatBot(
        token=candidate.token,
        telegram_bot_id=candidate.bot_id,
        username=candidate.username,
        title=candidate.title,
        group_id=group.id if group else None,
        enabled=True,
        is_copycat=True,
        logo=logo,
        image_url=image_url,
    )
    db.add(row)
    await db.commit()
    return row


@dataclass(frozen=True)
class Health:
    ok: bool
    text: str


async def health(row: CopycatBot) -> Health:
    """Жив ли токен и смотрит ли вебхук на нас."""
    from app.config import settings
    from app.services.copycat_service import mirror_webhook_url

    bot = Bot(token=row.token)
    try:
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=_TIMEOUT)
        except Exception as error:  # noqa: BLE001
            return Health(False, f"токен не работает: {error}")
        if settings.get_bot_run_mode() == "polling" or not settings.WEBHOOK_URL:
            return Health(True, f"токен жив (@{me.username}), бот на polling")
        try:
            info = await asyncio.wait_for(bot.get_webhook_info(), timeout=_TIMEOUT)
        except Exception as error:  # noqa: BLE001
            return Health(False, f"токен жив, вебхук не прочитать: {error}")
        if (info.url or "") != mirror_webhook_url(me.id):
            return Health(False, f"токен жив, но вебхук не наш: {info.url or 'пусто'}")
        pending = f", в очереди {info.pending_update_count}" if info.pending_update_count else ""
        error = f", последняя ошибка: {info.last_error_message}" if info.last_error_message else ""
        return Health(True, f"токен жив, вебхук наш{pending}{error}")
    finally:
        await bot.session.close()
