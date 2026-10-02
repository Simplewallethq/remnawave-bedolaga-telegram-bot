"""Повторная регистрация удалённого юзера удаляет подписку вместе с хвостами."""

from __future__ import annotations

from datetime import datetime, timedelta
import sys

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.database.crud.user import create_web_user
from app.database.models import Base, DeviceLink, SentNotification, Subscription, User
from app.handlers import start


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен")
async def test_reregistration_deletes_subscription_with_sent_notifications() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()

    user = await create_web_user(db, email="old@example.com", password_hash="x", auth_source="app")
    subscription = Subscription(
        user_id=user.id,
        status="expired",
        is_trial=True,
        end_date=datetime.utcnow() - timedelta(days=100),
    )
    db.add(subscription)
    await db.flush()
    db.add_all(
        [
            SentNotification(user_id=user.id, subscription_id=subscription.id,
                             notification_type="trial_inactive_1h"),
            DeviceLink(subscription_id=subscription.id, device_id="device-1"),
        ]
    )
    await db.commit()
    db.expunge_all()

    user = (
        await db.execute(
            select(User).options(selectinload(User.subscription)).where(User.id == user.id)
        )
    ).scalar_one()

    await start._delete_subscription_for_reregistration(db, user)
    await db.commit()

    assert user.subscription is None
    assert (await db.execute(select(Subscription))).scalars().all() == []
    assert (await db.execute(select(SentNotification))).scalars().all() == []
    assert (await db.execute(select(DeviceLink))).scalars().all() == []

    db.add(Subscription(user_id=user.id, status="active", is_trial=True,
                        end_date=datetime.utcnow() + timedelta(days=3)))
    await db.commit()

    await db.close()
    await engine.dispose()
