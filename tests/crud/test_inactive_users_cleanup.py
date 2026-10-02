"""Чистка неактивных не трогает платных; кабинет обновляет last_activity."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.crud.user import create_web_user, get_inactive_users  # noqa: E402
from app.database.models import Base  # noqa: E402
from app.webapi import cabinet_dependencies  # noqa: E402


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _make_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return maker()


@pytest.mark.anyio
@pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен")
async def test_inactive_cleanup_skips_paid_users() -> None:
    db = await _make_session()
    old = datetime.utcnow() - timedelta(days=200)

    free = await create_web_user(db, email="free@example.com", password_hash="x", auth_source="app")
    paid = await create_web_user(db, email="paid@example.com", password_hash="x", auth_source="app")
    free.last_activity = old
    paid.last_activity = old
    paid.has_had_paid_subscription = True
    await db.commit()

    users = await get_inactive_users(db, months=3)

    assert [u.id for u in users] == [free.id]
    await db.close()


def _user(last_activity):
    user = MagicMock()
    user.id = 1
    user.last_activity = last_activity
    return user


@pytest.mark.anyio
async def test_cabinet_touches_stale_last_activity() -> None:
    db = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
    user = _user(datetime.utcnow() - timedelta(days=100))

    await cabinet_dependencies._touch_last_activity(db, user)

    assert datetime.utcnow() - user.last_activity < timedelta(minutes=1)
    db.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_cabinet_skips_fresh_last_activity() -> None:
    db = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
    fresh = datetime.utcnow() - timedelta(minutes=5)
    user = _user(fresh)

    await cabinet_dependencies._touch_last_activity(db, user)

    assert user.last_activity == fresh
    db.commit.assert_not_awaited()
