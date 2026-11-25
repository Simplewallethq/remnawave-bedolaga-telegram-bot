"""Заметка копикета в scripts.copycats: comment, disable с причиной, set comment=."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.models import Base, CopycatBot  # noqa: E402
from scripts import copycats  # noqa: E402


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


pytestmark = pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(copycats, "engine", engine)
    monkeypatch.setattr(copycats, "AsyncSessionLocal", session_maker)
    async with session_maker() as db:
        db.add(CopycatBot(id=7, token="111:aaa", username="shopbot", enabled=True, is_copycat=True))
        await db.commit()
    yield session_maker
    await engine.dispose()


async def _cli(*argv: str) -> None:
    await copycats.run(copycats.build_parser().parse_args(list(argv)))


async def _bot(maker) -> CopycatBot:
    async with maker() as db:
        return await db.get(CopycatBot, 7)


@pytest.mark.anyio
async def test_disable_with_reason_keeps_it_as_comment(maker):
    await _cli("disable", "@shopbot", "жалоба", "на", "бренд")

    row = await _bot(maker)
    assert row.enabled is False
    assert row.comment == "жалоба на бренд"


@pytest.mark.anyio
async def test_disable_and_enable_without_reason_keep_comment(maker):
    await _cli("comment", "7", "неприбылен")
    await _cli("disable", "7")
    await _cli("enable", "7")

    row = await _bot(maker)
    assert row.enabled is True
    assert row.comment == "неприбылен"


@pytest.mark.anyio
async def test_comment_without_text_clears_it(maker):
    await _cli("set", "7", "comment=временно")
    assert (await _bot(maker)).comment == "временно"

    await _cli("comment", "7")
    assert (await _bot(maker)).comment is None
