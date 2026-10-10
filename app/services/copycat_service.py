"""Копикеты из БД: сборка бренда, импорт mirror_bots.yaml и живой реестр.

Источник правды — таблицы copycat_bots и copycat_groups. Поле бренда берётся
у бота, пустое — у его группы, дальше — COPYCAT_* настройки и основной бот
(см. app/branding/profile.py). Веса платёжного роутера: свои у бота, иначе у
группы, иначе глобальные PAYMENT_ROUTER_WEIGHT_*.

Бот перечитывает таблицы раз в COPYCAT_SYNC_INTERVAL_SECONDS: новая витрина
поднимается и получает вебхук, выключенная гасится, правки бренда и весов
подхватываются на лету. Писать в таблицы может кто угодно — CLI
(`python -m scripts.copycats`), будущий бот для владельцев витрин.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from aiogram import Bot

from app.branding.profile import _as_bool, clean_brand_name
from app.config import settings
from app.database.models import CopycatBot, CopycatGroup
from app.utils import bot_registry

logger = logging.getLogger(__name__)

# Группа, в которую попадают витрины при импорте из mirror_bots.yaml.
DEFAULT_GROUP_NAME = "default"

# Поля бренда, которые задаёт группа как значения по умолчанию для своих ботов.
GROUP_BRAND_FIELDS = (
    "support",
    "subscription_domain",
    "happ_redirect_template",
    "privacy_url",
    "terms_url",
    "referral_terms_url",
)

# Поля бренда бота (перебивают группу).
BOT_BRAND_FIELDS = (
    "name",
    "privacy_url",
    "terms_url",
    "channel_link",
    "channel_id",
    "support",
    "subscription_domain",
    "happ_redirect_template",
    "referral_terms_url",
    "has_own_app",
    "rays_enabled",
)

# Ключи записи mirror_bots.yaml, которые переносятся в copycat_bots как есть.
_LEGACY_IMPORT_FIELDS = BOT_BRAND_FIELDS + ("logo", "image_url")

_CONNECT_CONCURRENCY = 16


# ---------------------------------------------------------------- бренд и веса


def _filled(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def brand_config_for(
    row: CopycatBot, group: Optional[CopycatGroup], *, title: Optional[str] = None
) -> dict[str, Any]:
    """Запись витрины в формате, который понимает mirror_profile_from_config."""
    config: dict[str, Any] = {
        "token": row.token,
        "logo": settings.resolve_bot_logo(row.logo or settings.LOGO_FILE),
        "copycat": bool(row.is_copycat),
    }
    if group is not None:
        for field in GROUP_BRAND_FIELDS:
            value = getattr(group, field, None)
            if _filled(value):
                config[field] = value
    for field in BOT_BRAND_FIELDS:
        value = getattr(row, field, None)
        if _filled(value):
            config[field] = value
    if not _filled(config.get("name")):
        # Имя витрины — как бот назван в Telegram: руками 100+ названий не ведём.
        name = clean_brand_name(title or row.title)
        if name:
            config["name"] = name
    return config


def normalize_gateway_weights(raw: Any) -> Optional[dict[str, int]]:
    """Словарь весов роутера: известные шлюзы, целые ≥ 0. None — не задано."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        logger.warning("Веса роутера копикета не словарь, игнорируем: %r", raw)
        return None
    from app.services.payment_gateway_router import ROUTED_GATEWAYS

    weights: dict[str, int] = {}
    for gateway, value in raw.items():
        gateway = str(gateway).strip().lower()
        if gateway not in ROUTED_GATEWAYS:
            logger.warning("Неизвестный шлюз в весах копикета: %s", gateway)
            continue
        try:
            weights[gateway] = max(0, int(value))
        except (TypeError, ValueError):
            logger.warning("Некорректный вес %s=%r у копикета", gateway, value)
    return weights


def effective_gateway_weights(
    row: CopycatBot, group: Optional[CopycatGroup]
) -> Optional[tuple[dict[str, int], str]]:
    """Веса роутера витрины и откуда они: "bot" | "group:<имя>". None — глобальные."""
    own = normalize_gateway_weights(row.payment_gateway_weights)
    if own is not None:
        return own, "bot"
    if group is not None:
        from_group = normalize_gateway_weights(group.payment_gateway_weights)
        if from_group is not None:
            return from_group, f"group:{group.name}"
    return None


def effective_owner_share_percent(
    row: CopycatBot, group: Optional[CopycatGroup]
) -> Optional[int]:
    """Доля владельца витрины в выручке, %; остальное — наше."""
    if row.owner_share_percent is not None:
        return int(row.owner_share_percent)
    if group is not None and group.owner_share_percent is not None:
        return int(group.owner_share_percent)
    return None


# ------------------------------------------------------------- импорт из yaml


@dataclass
class LegacyImportResult:
    added: int = 0
    skipped: int = 0


async def import_legacy_mirrors(
    db,
    items: Optional[Iterable[Mapping[str, Any]]] = None,
    *,
    group_name: str = DEFAULT_GROUP_NAME,
) -> LegacyImportResult:
    """Переносит записи mirror_bots.yaml в copycat_bots.

    Уже заведённые токены пропускаются — повторный импорт безопасен. Витрины
    попадают в группу `group_name`, зеркала основного бренда (`copycat: false`)
    — без группы. Коммит — на вызывающем.
    """
    from app.database.crud.copycat import existing_tokens, get_or_create_copycat_group

    items = list(items if items is not None else settings.get_legacy_mirror_items())
    result = LegacyImportResult()
    if not items:
        return result

    tokens = [str(item.get("token") or "").strip() for item in items]
    known = await existing_tokens(db, tokens)
    group: Optional[CopycatGroup] = None

    for item, token in zip(items, tokens):
        if not token or token in known:
            result.skipped += 1
            continue
        known.add(token)
        is_copycat = _as_bool(item.get("copycat"), True)
        row = CopycatBot(token=token, is_copycat=is_copycat, enabled=True)
        for field in _LEGACY_IMPORT_FIELDS:
            value = item.get(field)
            if not _filled(value):
                continue
            if field in ("has_own_app", "rays_enabled"):
                value = _as_bool(value, False)
            else:
                value = str(value).strip()
            setattr(row, field, value)
        if is_copycat:
            if group is None:
                group = await get_or_create_copycat_group(db, group_name)
            row.group_id = group.id
        db.add(row)
        result.added += 1

    await db.flush()
    return result


# ------------------------------------------------------------- живой реестр


class CopycatRuntime:
    """Держит живых ботов-витрин в соответствии с таблицей copycat_bots."""

    def __init__(self) -> None:
        self._default_props = None
        self._primary_bot_id: Optional[int] = None
        # id записи copycat_bots → живой бот
        self._live: dict[int, Bot] = {}
        # id записи → Telegram id, под которым бренд лежит в bot_registry
        self._known: dict[int, int] = {}
        self._lock = asyncio.Lock()
        self._initialized = False
        self._hub = None
        self._allowed_updates: Optional[list[str]] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._failed_tokens: set[str] = set()
        self._restart_noted: set[int] = set()

    # ---------------------------------------------------------- состояние

    def live_bots(self) -> list[Bot]:
        return list(self._live.values())

    @property
    def can_change_live_set(self) -> bool:
        """После старта ботов можно добавлять и снимать только в webhook-режиме:
        polling запущен с фиксированным списком."""
        return not self._initialized or self._hub is not None

    # ---------------------------------------------------------- старт

    async def start(self, default_props, *, primary_bot_id: Optional[int]) -> list[Bot]:
        self._default_props = default_props
        self._primary_bot_id = primary_bot_id
        try:
            await self._import_legacy_if_empty()
        except Exception as error:
            logger.error("Импорт mirror_bots.yaml в БД не удался: %s", error)
        await self.sync()
        self._initialized = True
        return self.live_bots()

    async def _import_legacy_if_empty(self) -> None:
        from app.database.crud.copycat import count_copycat_bots
        from app.database.database import AsyncSessionLocal

        legacy = settings.get_legacy_mirror_items()
        async with AsyncSessionLocal() as db:
            if await count_copycat_bots(db):
                if legacy:
                    logger.warning(
                        "Копикеты живут в БД (copycat_bots); %s записей из %s игнорируются. "
                        "Добавить новые: python -m scripts.copycats import-yaml",
                        len(legacy),
                        settings.MIRROR_BOTS_FILE,
                    )
                return
            if not legacy:
                return
            result = await import_legacy_mirrors(db, legacy)
            await db.commit()
            logger.info(
                "Копикеты перенесены из %s в БД: добавлено %s, пропущено %s",
                settings.MIRROR_BOTS_FILE,
                result.added,
                result.skipped,
            )

    async def attach_webhooks(self, hub, allowed_updates: Optional[list[str]]) -> None:
        """Webhook-режим: вешает вебхуки живым витринам и включает горячее
        добавление новых."""
        async with self._lock:
            self._hub = hub
            self._allowed_updates = allowed_updates
            for bot in self._live.values():
                await self._attach_webhook(bot)

    def start_sync_loop(self) -> None:
        interval = int(settings.COPYCAT_SYNC_INTERVAL_SECONDS or 0)
        if interval <= 0 or (self._sync_task and not self._sync_task.done()):
            return
        self._sync_task = asyncio.create_task(self._sync_loop(interval), name="copycat-sync")

    async def _sync_loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("Синхронизация копикетов упала: %s", error)

    async def stop(self) -> None:
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        self._sync_task = None

    # ---------------------------------------------------------- синхронизация

    async def sync(self) -> None:
        from app.database.crud.copycat import list_copycat_bots
        from app.database.database import AsyncSessionLocal

        async with self._lock:
            async with AsyncSessionLocal() as db:
                rows = await list_copycat_bots(db)
                rows_by_id = {row.id: row for row in rows}

                # Удалённые из таблицы.
                for row_id in list(self._known):
                    if row_id not in rows_by_id:
                        if not await self._bring_down(row_id, reason="удалена из БД"):
                            continue
                        tg_id = self._known.pop(row_id)
                        bot_registry.forget_bot(tg_id)

                # Выключенные и сменившие токен.
                for row in rows:
                    live = self._live.get(row.id)
                    if live is None:
                        continue
                    if not row.enabled:
                        await self._bring_down(row.id, reason="выключена")
                    elif live.token != row.token:
                        await self._bring_down(row.id, reason="сменился токен")

                # Новые и включённые обратно.
                to_connect = [
                    row for row in rows
                    if row.enabled and row.id not in self._live and self._may_bring_up(row)
                ]
                connected = await self._connect_all(to_connect)
                dirty = False
                for row, bot, me in connected:
                    dirty |= self._remember_identity(row, me, rows)
                    self._live[row.id] = bot
                    if self._hub is not None:
                        await self._attach_webhook(bot)
                    logger.info(
                        "Mirror bot registered: @%s id=%s logo=%s copycat=%s group=%s",
                        me.username,
                        me.id,
                        row.logo,
                        row.is_copycat,
                        row.group.name if row.group else "-",
                    )

                # Бренд и веса — всем, у кого известен Telegram id, в том
                # числе выключенным: их пользователи остаются аудиторией витрины.
                for row in rows:
                    self._apply_brand(row)

                if dirty:
                    await db.commit()

    def _may_bring_up(self, row: CopycatBot) -> bool:
        if self.can_change_live_set:
            return True
        if row.id not in self._restart_noted:
            self._restart_noted.add(row.id)
            logger.warning(
                "Витрина #%s (@%s) включена, но бот работает в polling — "
                "поднимется после рестарта",
                row.id,
                row.username or "?",
            )
        return False

    async def _connect_all(self, rows: list[CopycatBot]):
        semaphore = asyncio.Semaphore(_CONNECT_CONCURRENCY)

        async def connect(row: CopycatBot):
            async with semaphore:
                bot = Bot(token=row.token, default=self._default_props)
                try:
                    me = await bot.get_me()
                except Exception as error:
                    await bot.session.close()
                    log = logger.debug if row.token in self._failed_tokens else logger.error
                    self._failed_tokens.add(row.token)
                    log("Failed to init mirror bot #%s (token %s...): %s", row.id, row.token[:10], error)
                    return None
                self._failed_tokens.discard(row.token)
                if self._primary_bot_id is not None and me.id == self._primary_bot_id:
                    await bot.session.close()
                    logger.error("Витрина #%s — это токен основного бота, пропускаем", row.id)
                    return None
                return row, bot, me

        results = await asyncio.gather(*(connect(row) for row in rows))
        return [item for item in results if item is not None]

    def _remember_identity(self, row: CopycatBot, me, rows: list[CopycatBot]) -> bool:
        """Сохраняет Telegram id, username и название бота в запись."""
        changed = False
        if row.telegram_bot_id != me.id:
            # Владелец перевыпустил токен и витрину завели заново: старая
            # запись с мёртвым токеном уступает Telegram id новой.
            for other in rows:
                if other.id != row.id and other.telegram_bot_id == me.id:
                    logger.warning(
                        "Витрина #%s повторяет бота @%s из #%s — старая запись выключена",
                        row.id,
                        me.username,
                        other.id,
                    )
                    other.telegram_bot_id = None
                    other.enabled = False
                    self._known.pop(other.id, None)
            row.telegram_bot_id = me.id
            changed = True
        if row.username != me.username:
            row.username = me.username
            changed = True
        if row.title != me.full_name:
            row.title = me.full_name
            changed = True
        return changed

    def _apply_brand(self, row: CopycatBot) -> None:
        live = self._live.get(row.id)
        tg_id = live.id if live is not None else row.telegram_bot_id
        if tg_id is None:
            return
        previous = self._known.get(row.id)
        if previous is not None and previous != tg_id:
            bot_registry.forget_bot(previous)
        self._known[row.id] = tg_id

        config = brand_config_for(row, row.group)
        bot_registry.register_bot(tg_id, Path(config["logo"]), live, brand_config=config)
        weights = effective_gateway_weights(row, row.group)
        if weights is None:
            bot_registry.set_payment_weights(tg_id, None)
        else:
            bot_registry.set_payment_weights(tg_id, weights[0], weights[1])

    async def _bring_down(self, row_id: int, *, reason: str) -> bool:
        bot = self._live.get(row_id)
        if bot is None:
            return True
        if not self.can_change_live_set:
            if row_id not in self._restart_noted:
                self._restart_noted.add(row_id)
                logger.warning(
                    "Витрина #%s %s, но бот работает в polling — снимется после рестарта",
                    row_id,
                    reason,
                )
            return False
        self._live.pop(row_id)
        bot_registry.unregister_instance(bot.id)
        if self._hub is not None:
            await self._hub.remove(bot.id)
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception as error:
                logger.warning("Не удалось снять вебхук витрины %s: %s", bot.id, error)
        try:
            await bot.session.close()
        except Exception:  # pragma: no cover - закрытие сессии не критично
            pass
        logger.info("Mirror bot stopped: id=%s (%s)", bot.id, reason)
        return True

    async def _attach_webhook(self, bot: Bot) -> None:
        await self._hub.add(bot)
        url = mirror_webhook_url(bot.id)
        try:
            await bot.set_webhook(
                url=url,
                secret_token=settings.WEBHOOK_SECRET_TOKEN,
                drop_pending_updates=settings.WEBHOOK_DROP_PENDING_UPDATES,
                allowed_updates=self._allowed_updates,
            )
            logger.info("Mirror webhook установлен: %s", url)
        except Exception as error:
            logger.error("Ошибка установки webhook для витрины %s: %s", bot.id, error)


def mirror_webhook_path(bot_id: int) -> str:
    """Путь вебхука витрины — по Telegram id, а не по номеру в списке:
    номер сдвигался при удалении записи, и вебхук попадал в чужого бота."""
    return f"{settings.get_telegram_webhook_path()}/mirror/{bot_id}"


def mirror_webhook_url(bot_id: int) -> str:
    return f"{(settings.WEBHOOK_URL or '').rstrip('/')}{mirror_webhook_path(bot_id)}"


copycat_runtime = CopycatRuntime()
