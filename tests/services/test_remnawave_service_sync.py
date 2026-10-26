from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.remnawave_service import RemnaWaveService


def _create_service() -> RemnaWaveService:
    service = RemnaWaveService.__new__(RemnaWaveService)
    service._panel_timezone = ZoneInfo("UTC")
    service._utc_timezone = ZoneInfo("UTC")
    return service


def _make_panel_user(telegram_id: int, expire_at: str, status: str = "ACTIVE") -> dict:
    return {
        "telegramId": telegram_id,
        "expireAt": expire_at,
        "status": status,
    }


def test_deduplicate_prefers_latest_expire_date():
    service = _create_service()

    telegram_id = 100
    older = _make_panel_user(telegram_id, datetime(2025, 1, 1, 0, 0, 0).isoformat())
    newer = _make_panel_user(telegram_id, datetime(2025, 2, 1, 0, 0, 0).isoformat())

    deduplicated = service._deduplicate_panel_users_by_telegram_id([older, newer])

    assert deduplicated[telegram_id] is newer


def test_deduplicate_prefers_active_status_on_same_expire():
    service = _create_service()

    telegram_id = 200
    expire = datetime(2025, 1, 1, 0, 0, 0).isoformat()
    disabled = _make_panel_user(telegram_id, expire, status="DISABLED")
    active = _make_panel_user(telegram_id, expire, status="ACTIVE")

    deduplicated = service._deduplicate_panel_users_by_telegram_id([disabled, active])

    assert deduplicated[telegram_id] is active


def test_deduplicate_ignores_records_without_expire_date():
    service = _create_service()

    telegram_id = 300
    missing_expire = _make_panel_user(telegram_id, "")
    valid = _make_panel_user(telegram_id, datetime(2025, 3, 1, 0, 0, 0).isoformat())

    deduplicated = service._deduplicate_panel_users_by_telegram_id([missing_expire, valid])

    assert deduplicated[telegram_id] is valid


async def test_get_or_create_user_handles_unique_violation(monkeypatch):
    service = _create_service()
    db = AsyncMock()

    panel_user = {"telegramId": 555, "username": "existing"}
    existing_user = object()

    create_user_mock = AsyncMock(
        side_effect=IntegrityError("stmt", "params", Exception("unique"))
    )
    get_user_mock = AsyncMock(return_value=existing_user)
    rollback_mock = AsyncMock()

    db.rollback = rollback_mock

    monkeypatch.setattr("app.services.remnawave_service.create_user_no_commit", create_user_mock)
    monkeypatch.setattr(
        "app.services.remnawave_service.get_user_by_telegram_id",
        get_user_mock,
    )

    user, created = await service._get_or_create_bot_user_from_panel(db, panel_user)

    assert user is existing_user
    assert created is False
    create_user_mock.assert_awaited_once()
    get_user_mock.assert_awaited_once_with(db, 555)
    rollback_mock.assert_awaited()


async def test_get_or_create_user_creates_new(monkeypatch):
    service = _create_service()
    db = AsyncMock()

    panel_user = {"telegramId": 777, "username": "new_user"}
    new_user = object()

    create_user_mock = AsyncMock(return_value=new_user)

    monkeypatch.setattr("app.services.remnawave_service.create_user_no_commit", create_user_mock)

    user, created = await service._get_or_create_bot_user_from_panel(db, panel_user)

    assert user is new_user
    assert created is True
    create_user_mock.assert_awaited_once_with(
        db=db,
        telegram_id=777,
        username="new_user",
        first_name="User 777",
        last_name=None,
        language="ru",
    )


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


def _panel_user_obj(telegram_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        uuid=f"uuid-{telegram_id}",
        short_uuid=f"short-{telegram_id}",
        username=f"user_{telegram_id}",
        status=SimpleNamespace(value="ACTIVE"),
        telegram_id=telegram_id,
        expire_at=datetime(2030, 1, 1),
        traffic_limit_bytes=0,
        used_traffic_bytes=0,
        lifetime_used_traffic_bytes=0,
        first_connected_at=None,
        hwid_device_limit=1,
        subscription_url="",
        happ_crypto_link="",
        active_internal_squads=[],
    )


@pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен")
async def test_full_sync_finds_bot_users_from_every_chunk(monkeypatch):
    from app.database.models import Base, User

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()

    telegram_ids = [101, 102, 103, 104, 105]
    db.add_all(User(telegram_id=tg, referral_code=f"ref{tg}") for tg in telegram_ids)
    await db.commit()

    # Пачка меньше числа пользователей: синк должен пройти по всем пачкам.
    monkeypatch.setattr("app.services.remnawave_service._SYNC_USERS_CHUNK_SIZE", 2)

    class FakeApi:
        async def get_all_users(self, start, size, enrich_happ_links):
            users = [_panel_user_obj(tg) for tg in telegram_ids][start:start + size]
            return {"users": users, "total": len(telegram_ids)}

    @asynccontextmanager
    async def fake_api_client():
        yield FakeApi()

    service = _create_service()
    service.get_api_client = fake_api_client
    create_mock = AsyncMock(side_effect=AssertionError("пользователь бота не найден"))
    monkeypatch.setattr(service, "_get_or_create_bot_user_from_panel", create_mock)

    stats = await service.sync_users_from_panel(db, "new_only")

    create_mock.assert_not_awaited()
    assert stats["created"] == 0
    assert stats["errors"] == 0
    await db.close()
    await engine.dispose()
