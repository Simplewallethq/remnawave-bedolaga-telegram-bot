"""Support finds a customer's account in any bot (copycats keep their own accounts)."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.database.crud.user import create_user  # noqa: E402
from app.database.models import Base  # noqa: E402
from app.utils import bot_registry  # noqa: E402
from app.webapi.routes.support_context import find_support_user  # noqa: E402

PRIMARY_ID = 1000
COPYCAT_A = 2001
TG_ID = 777


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


needs_sqlite = pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def bots(monkeypatch):
    monkeypatch.setattr(settings, "BOT_TOKEN", f"{PRIMARY_ID}:token", raising=False)
    bot_registry.clear()
    bot_registry.register_bot(
        COPYCAT_A,
        Path("bot_images/shuka.jpg"),
        brand_config={"token": "t2001", "copycat": True, "name": "Shuka"},
    )
    yield
    bot_registry.clear()


async def _make_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


@pytest.mark.anyio
@needs_sqlite
async def test_copycat_only_customer_is_found(bots) -> None:
    db = await _make_session()
    in_a = await create_user(db, telegram_id=TG_ID, bot_id=COPYCAT_A)

    # The bot's own lookup (main scope) misses this account; support must not.
    found, accounts = await find_support_user(db, TG_ID)
    assert found is not None and found.id == in_a.id
    assert accounts == 1
    await db.close()


@pytest.mark.anyio
@needs_sqlite
async def test_preference_picks_the_account(bots) -> None:
    db = await _make_session()
    main = await create_user(db, telegram_id=TG_ID, bot_id=PRIMARY_ID)
    in_a = await create_user(db, telegram_id=TG_ID, bot_id=COPYCAT_A)

    assert (await find_support_user(db, TG_ID, "main"))[0].id == main.id
    found, accounts = await find_support_user(db, TG_ID, "copy")
    assert found.id == in_a.id and accounts == 2
    await db.close()


@pytest.mark.anyio
@needs_sqlite
async def test_unknown_customer(bots) -> None:
    db = await _make_session()
    assert await find_support_user(db, TG_ID) == (None, 0)
    await db.close()
