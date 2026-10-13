"""Отдельные аккаунты нового пользователя по ботам и его имя в панели."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.branding import brand_scope  # noqa: E402
from app.config import settings  # noqa: E402
from app.database.crud.user import (  # noqa: E402
    create_user,
    get_user_by_telegram_id,
    get_users_by_telegram_id,
)
from app.database.models import Base, User  # noqa: E402
from app.utils import bot_registry  # noqa: E402
from app.utils.panel_identity import (  # noqa: E402
    find_panel_user,
    panel_description_for_user,
    resolve_panel_username,
)

PRIMARY_ID = 1000
LETO_COPY_ID = 1001
COPYCAT_A = 2001
COPYCAT_B = 2002
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
        LETO_COPY_ID,
        Path("bot_images/doctor.png"),
        brand_config={"token": "t1", "copycat": False},
    )
    for bot_id, name in ((COPYCAT_A, "Shuka"), (COPYCAT_B, "Shield")):
        bot_registry.register_bot(
            bot_id,
            Path("bot_images/shuka.jpg"),
            brand_config={"token": f"t{bot_id}", "copycat": True, "name": name},
        )
    yield
    bot_registry.clear()


async def _make_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


def test_account_scope_for_bot(bots):
    assert bot_registry.account_scope_for_bot(None) == PRIMARY_ID
    assert bot_registry.account_scope_for_bot(PRIMARY_ID) == PRIMARY_ID
    assert bot_registry.account_scope_for_bot(LETO_COPY_ID) == PRIMARY_ID
    assert bot_registry.account_scope_for_bot(COPYCAT_A) == COPYCAT_A


@pytest.mark.anyio
@needs_sqlite
async def test_new_user_gets_account_per_bot_scope(bots) -> None:
    db = await _make_session()

    main = await create_user(db, telegram_id=TG_ID, bot_id=PRIMARY_ID)
    in_a = await create_user(db, telegram_id=TG_ID, bot_id=COPYCAT_A)
    in_b = await create_user(db, telegram_id=TG_ID, bot_id=COPYCAT_B)

    assert {main.account_scope_bot_id, in_a.account_scope_bot_id, in_b.account_scope_bot_id} == {
        PRIMARY_ID,
        COPYCAT_A,
        COPYCAT_B,
    }
    assert len({main.id, in_a.id, in_b.id}) == 3

    assert (await get_user_by_telegram_id(db, TG_ID, bot_id=COPYCAT_A)).id == in_a.id
    assert (await get_user_by_telegram_id(db, TG_ID, bot_id=COPYCAT_B)).id == in_b.id
    # Копия Leto делит аккаунт с основным ботом.
    assert (await get_user_by_telegram_id(db, TG_ID, bot_id=LETO_COPY_ID)).id == main.id
    # Бот берётся из контекста апдейта, вне апдейта — основной.
    with brand_scope(COPYCAT_B):
        assert (await get_user_by_telegram_id(db, TG_ID)).id == in_b.id
    assert (await get_user_by_telegram_id(db, TG_ID)).id == main.id

    assert [u.id for u in await get_users_by_telegram_id(db, TG_ID)] == [main.id, in_a.id, in_b.id]
    await db.close()


@pytest.mark.anyio
@needs_sqlite
async def test_second_account_in_same_scope_is_rejected(bots) -> None:
    db = await _make_session()
    await create_user(db, telegram_id=TG_ID, bot_id=COPYCAT_A)

    with pytest.raises(IntegrityError):
        await create_user(db, telegram_id=TG_ID, bot_id=COPYCAT_A)
    await db.close()


@pytest.mark.anyio
@needs_sqlite
async def test_legacy_shared_account_wins_in_every_bot(bots) -> None:
    db = await _make_session()
    legacy = User(telegram_id=TG_ID, bot_id=COPYCAT_A, referral_code="reflegacy")
    db.add(legacy)
    await db.commit()

    for bot_id in (PRIMARY_ID, LETO_COPY_ID, COPYCAT_A, COPYCAT_B):
        found = await get_user_by_telegram_id(db, TG_ID, bot_id=bot_id)
        assert found is not None and found.id == legacy.id
    await db.close()


class _FakePanel:
    def __init__(self, users):
        self.users = {u.username: u for u in users}
        self.by_tg_calls = 0

    async def get_user_by_username(self, username):
        return self.users.get(username)

    async def get_user_by_uuid(self, uuid):
        return next((u for u in self.users.values() if u.uuid == uuid), None)

    async def get_user_by_telegram_id(self, telegram_id):
        self.by_tg_calls += 1
        return [u for u in self.users.values() if u.telegram_id == telegram_id]


def _panel_user(username, uuid):
    return SimpleNamespace(username=username, uuid=uuid, telegram_id=TG_ID)


def _account(**kwargs):
    defaults = dict(
        telegram_id=TG_ID,
        full_name="Ivan",
        username="ivan",
        remnawave_uuid=None,
        bot_id=COPYCAT_A,
        account_scope_bot_id=COPYCAT_A,
        id=1,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.anyio
async def test_panel_username_gets_iterator_on_duplicate(monkeypatch) -> None:
    monkeypatch.setattr(settings, "REMNAWAVE_USER_USERNAME_TEMPLATE", "user_{telegram_id}", raising=False)
    account = _account()

    assert await resolve_panel_username(_FakePanel([]), account) == f"user_{TG_ID}"

    taken = _FakePanel([_panel_user(f"user_{TG_ID}", "u1"), _panel_user(f"user_{TG_ID}_2", "u2")])
    assert await resolve_panel_username(taken, account) == f"user_{TG_ID}_3"

    # Своё имя — не дубль.
    own = _account(remnawave_uuid="u1")
    assert await resolve_panel_username(taken, own) == f"user_{TG_ID}"

    # Старый общий аккаунт — без итератора, как раньше.
    legacy = _account(account_scope_bot_id=None)
    assert await resolve_panel_username(taken, legacy) == f"user_{TG_ID}"


@pytest.mark.anyio
async def test_scoped_account_never_takes_other_panel_user() -> None:
    panel = _FakePanel([_panel_user(f"user_{TG_ID}", "u1")])

    assert await find_panel_user(panel, _account(), f"user_{TG_ID}") is None
    assert panel.by_tg_calls == 0
    assert (await find_panel_user(panel, _account(remnawave_uuid="u1"), "x")).uuid == "u1"

    legacy = _account(account_scope_bot_id=None)
    assert (await find_panel_user(panel, legacy, f"user_{TG_ID}")).uuid == "u1"


def test_panel_description_signs_bot_id(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "REMNAWAVE_USER_DESCRIPTION_TEMPLATE", "Bot user: {full_name} {username}", raising=False
    )
    assert panel_description_for_user(_account()) == f"Bot user: Ivan @ivan | bot {COPYCAT_A}"
    assert panel_description_for_user(_account(account_scope_bot_id=None)) == "Bot user: Ivan @ivan"
