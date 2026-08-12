"""Отзыв привязки устройства на реальной SQLite-БД.

Строка device_links при удалении устройства обязана уцелеть — по ней видно,
выдавался ли этому устройству триал. Вместо DELETE проставляется revoked_at,
поэтому проверять это имеет смысл только на живой БД.
"""

import importlib
import sys
import types
from datetime import datetime

import pytest

# conftest подменяет aiosqlite заглушкой, чтобы модули импортировались без драйвера.
# Здесь нужен настоящий драйвер — возвращаем его, а если его нет, тест пропускаем.
if isinstance(sys.modules.get("aiosqlite"), types.ModuleType) and not hasattr(
    sys.modules.get("aiosqlite"), "connect"
):
    sys.modules.pop("aiosqlite", None)
    try:
        importlib.import_module("aiosqlite")
    except ImportError:  # pragma: no cover
        pass

aiosqlite = pytest.importorskip("aiosqlite", reason="нужен настоящий драйвер aiosqlite")
if not hasattr(aiosqlite, "connect"):  # pragma: no cover
    pytest.skip("aiosqlite подменён заглушкой", allow_module_level=True)

from contextlib import asynccontextmanager  # noqa: E402

from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.crud.device_link import (  # noqa: E402
    count_device_links,
    create_device_link,
    get_device_link,
    reactivate_device_link,
    rebind_device_link,
    revoke_all_device_links,
    revoke_device_links,
)
from app.database.models import Base, DeviceLink  # noqa: E402


@asynccontextmanager
async def _db():
    """Свой движок на тест: conftest запускает async-тесты собственным хуком,
    async-фикстуры он не поддерживает."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()


async def _link(db: AsyncSession, device_id: str, subscription_id: int = 1) -> DeviceLink:
    return await create_device_link(db, subscription_id, device_id)


async def test_new_link_is_active():
    async with _db() as db:
        link = await _link(db, "dev-1")
        assert link.revoked_at is None
        assert link.is_active


async def test_revoke_marks_without_deleting():
    async with _db() as db:
        await _link(db, "dev-1")

        assert await revoke_device_links(db, ["dev-1"]) == 1

        link = await get_device_link(db, "dev-1")
        # Ключевое: строка на месте, иначе устройство сможет получить второй триал.
        assert link is not None
        assert isinstance(link.revoked_at, datetime)
        assert not link.is_active


async def test_revoke_is_idempotent():
    async with _db() as db:
        await _link(db, "dev-1")
        await revoke_device_links(db, ["dev-1"])
        first = (await get_device_link(db, "dev-1")).revoked_at

        assert await revoke_device_links(db, ["dev-1"]) == 0
        assert (await get_device_link(db, "dev-1")).revoked_at == first


async def test_revoke_touches_only_named_devices():
    async with _db() as db:
        await _link(db, "dev-1")
        await _link(db, "dev-2")

        await revoke_device_links(db, ["dev-1"])

        assert not (await get_device_link(db, "dev-1")).is_active
        assert (await get_device_link(db, "dev-2")).is_active


async def test_revoke_empty_list_is_a_noop():
    async with _db() as db:
        await _link(db, "dev-1")

        assert await revoke_device_links(db, []) == 0
        assert (await get_device_link(db, "dev-1")).is_active


async def test_reset_revokes_only_that_subscription():
    async with _db() as db:
        await _link(db, "dev-1", subscription_id=1)
        await _link(db, "dev-2", subscription_id=1)
        await _link(db, "other", subscription_id=2)

        assert await revoke_all_device_links(db, 1) == 2

        assert not (await get_device_link(db, "dev-1")).is_active
        assert not (await get_device_link(db, "dev-2")).is_active
        assert (await get_device_link(db, "other")).is_active


async def test_reconnect_clears_the_revocation():
    """Без этого отозванное однажды устройство осталось бы отозванным навсегда."""
    async with _db() as db:
        await _link(db, "dev-1")
        await revoke_device_links(db, ["dev-1"])

        await reactivate_device_link(db, await get_device_link(db, "dev-1"))

        assert (await get_device_link(db, "dev-1")).is_active


async def test_rebind_to_another_subscription_clears_the_revocation():
    async with _db() as db:
        await _link(db, "dev-1", subscription_id=1)
        await revoke_device_links(db, ["dev-1"])

        await rebind_device_link(db, await get_device_link(db, "dev-1"), 2)

        fresh = await get_device_link(db, "dev-1")
        assert fresh.subscription_id == 2
        assert fresh.is_active


async def test_counting_is_unchanged_by_revocation():
    """Шаг 1 инертен: счётчик лимита пока считает и отозванные тоже.

    Менять его — отдельный шаг, иначе лимит поедет вместе с этой миграцией.
    """
    async with _db() as db:
        await _link(db, "dev-1")
        await _link(db, "dev-2")
        await revoke_device_links(db, ["dev-1"])

        assert await count_device_links(db, 1) == 2
