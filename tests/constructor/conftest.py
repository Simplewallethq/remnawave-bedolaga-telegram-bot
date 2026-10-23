"""Общее для тестов конструктора: база в памяти, поддельный Telegram для токенов."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.constructor import service
from app.database.models import Base
from app.services import owner_platform_service as platform

PLATFORM_BOT = 9000
TOKEN = "777000:AAH" + "x" * 32
OTHER_TOKEN = "888000:BBH" + "y" * 32


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


needs_sqlite = pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    yield session
    await session.close()
    await engine.dispose()


@pytest.fixture
def telegram(monkeypatch, tmp_path):
    """Боты, которые «знает» Telegram: токен → (getMe, url вебхука)."""
    monkeypatch.setattr(settings, "OWNER_PLATFORM_BOT_ID", PLATFORM_BOT, raising=False)
    monkeypatch.setattr(settings, "CONSTRUCTOR_BOT_TOKEN", "555000:CCH" + "z" * 32, raising=False)
    monkeypatch.setattr(settings, "OWNER_MAX_BOTS", 10, raising=False)
    monkeypatch.setattr(settings, "OWNER_BOTS_GROUP", "owners", raising=False)
    monkeypatch.setattr(settings, "OWNER_BOT_IMAGES_DIR", str(tmp_path / "logos"), raising=False)
    monkeypatch.setattr(settings, "WEBHOOK_URL", "https://hooks.example", raising=False)
    monkeypatch.setattr(platform, "refresh_runtime", AsyncMock())
    monkeypatch.setattr(platform, "is_reserved_bot_id", lambda bot_id: bot_id == PLATFORM_BOT)
    monkeypatch.setattr(service.blacklist_service, "is_user_blacklisted", AsyncMock(return_value=(False, None)))
    bots: dict[str, tuple] = {}

    async def fake_probe(token):
        return bots.get(token, (None, None))

    monkeypatch.setattr(service, "_probe", fake_probe)

    def add(token: str, bot_id: int, username: str, title: str = "My VPN", webhook: str | None = None):
        bots[token] = (SimpleNamespace(id=bot_id, username=username, full_name=title), webhook)

    return add


def png_bytes(size=(64, 32), mode="RGBA") -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new(mode, size, (10, 20, 30, 128) if mode == "RGBA" else (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()
