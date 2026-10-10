"""Копикеты из БД: бренд бот → группа, импорт yaml, живой реестр, веса роутера."""

from __future__ import annotations

import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.branding.context import brand_scope  # noqa: E402
from app.config import settings  # noqa: E402
from app.database.crud.copycat import list_copycat_bots  # noqa: E402
from app.database.models import Base, CopycatBot, CopycatGroup  # noqa: E402
from app.services import copycat_service  # noqa: E402
from app.services.copycat_service import (  # noqa: E402
    CopycatRuntime,
    brand_config_for,
    effective_gateway_weights,
    effective_owner_share_percent,
    import_legacy_mirrors,
)
from app.services.payment_gateway_router import PaymentGatewayRouter  # noqa: E402
from app.utils import bot_registry  # noqa: E402


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


needs_sqlite = pytest.mark.skipif(
    not _aiosqlite_available(), reason="настоящий aiosqlite недоступен"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_registry():
    bot_registry.clear()
    yield
    bot_registry.clear()


@pytest.fixture
async def session_maker(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import app.database.database as database

    monkeypatch.setattr(database, "AsyncSessionLocal", maker)
    yield maker
    await engine.dispose()


# ------------------------------------------------------------ бренд и веса


def test_bot_field_beats_group_and_group_fills_the_gaps():
    group = CopycatGroup(name="g", support="@group_support", terms_url="https://t/group")
    row = CopycatBot(token="1:A", is_copycat=True, support="@own", title="Dr. Guard | Быстро ⚡")

    config = brand_config_for(row, group)

    assert config["support"] == "@own"
    assert config["terms_url"] == "https://t/group"
    assert config["name"] == "Dr. Guard"
    assert config["copycat"] is True


def test_profile_from_db_record_falls_back_to_copycat_settings():
    row = CopycatBot(token="1:A", is_copycat=True, title="Shuka VPN")
    bot_registry.register_bot(555, Path("x.jpg"), brand_config=brand_config_for(row, None))

    profile = bot_registry.get_brand_for_bot(555)

    assert profile.is_copycat
    assert profile.name == "Shuka VPN"
    assert profile.support == settings.COPYCAT_SUPPORT_USERNAME


def test_weights_and_share_come_from_bot_then_group():
    group = CopycatGroup(name="risky", payment_gateway_weights={"wata": 1}, owner_share_percent=40)
    plain = CopycatBot(token="1:A")
    own = CopycatBot(token="2:B", payment_gateway_weights={"platega": 5, "bogus": 3}, owner_share_percent=70)

    assert effective_gateway_weights(plain, group) == ({"wata": 1}, "group:risky")
    assert effective_gateway_weights(own, group) == ({"platega": 5}, "bot")
    assert effective_gateway_weights(plain, None) is None
    assert effective_owner_share_percent(plain, group) == 40
    assert effective_owner_share_percent(own, group) == 70


# ------------------------------------------------------------ роутер


@pytest.fixture
def two_gateways(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_ONEPAYMENT", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_FALLBACK_ENABLED", True, raising=False)
    monkeypatch.setattr(type(settings), "is_platega_universal_enabled", lambda self: True)
    monkeypatch.setattr(type(settings), "is_wata_enabled", lambda self: True)
    monkeypatch.setattr(settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 100, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 10_000_000, raising=False)
    monkeypatch.setattr(settings, "WATA_MIN_AMOUNT_KOPEKS", 100, raising=False)
    monkeypatch.setattr(settings, "WATA_MAX_AMOUNT_KOPEKS", 10_000_000, raising=False)


def test_router_uses_weights_of_the_answering_bot(two_gateways):
    router = PaymentGatewayRouter()
    bot_registry.set_payment_weights(777, {"wata": 1}, "group:risky")

    assert router.enabled_gateways() == ["platega", "wata"]
    with brand_scope(777):
        assert router.enabled_gateways() == ["wata"]
        assert router.pick_order(50_000, rng=random.Random(1)) == ["wata"]
    # Вне апдейта — бот, где пользователь зарегистрирован.
    assert router.routing_bot_id(SimpleNamespace(bot_id=777)) == 777
    assert router.enabled_gateways(bot_id=777) == ["wata"]


@pytest.mark.anyio
async def test_invoice_records_bot_and_weights_source(two_gateways, monkeypatch):
    router = PaymentGatewayRouter()
    bot_registry.set_payment_weights(777, {"platega": 1}, "bot")
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_LOG_ENABLED", False, raising=False)
    seen = {}

    async def fake_platega(payment_service, db, user, amount, description, language, metadata):
        seen.update(metadata["payment_router"])
        return {"redirect_url": "https://pay", "local_payment_id": 1, "transaction_id": "t"}

    monkeypatch.setattr(router, "_create_platega", fake_platega)
    user = SimpleNamespace(id=1, bot_id=777, language="ru")

    routed = await router.create_invoice(
        None, payment_service=None, user=user, amount_kopeks=50_000, source="balance_topup",
        description="d",
    )

    assert routed.gateway == "platega"
    assert seen["bot_id"] == 777
    assert seen["weights_source"] == "bot"


# ------------------------------------------------------------ импорт yaml


@needs_sqlite
@pytest.mark.anyio
async def test_legacy_import_is_idempotent_and_groups_copycats(session_maker):
    items = [
        {"token": "1:A", "logo": "a.jpg", "image_url": "https://img/a", "support": "@a"},
        {"token": "2:B", "logo": "b.jpg", "copycat": False},
        {"token": "1:A"},
        {"logo": "no-token.jpg"},
    ]
    async with session_maker() as db:
        result = await import_legacy_mirrors(db, items)
        await db.commit()
        again = await import_legacy_mirrors(db, items)
        await db.commit()
        rows = await list_copycat_bots(db)

    assert (result.added, result.skipped) == (2, 2)
    assert again.added == 0
    copycat, mirror = rows
    assert copycat.is_copycat and copycat.group.name == "default"
    assert copycat.logo == "a.jpg" and copycat.image_url == "https://img/a" and copycat.support == "@a"
    assert not mirror.is_copycat and mirror.group is None


# ------------------------------------------------------------ живой реестр


class FakeBot:
    instances: dict[str, "FakeBot"] = {}

    def __init__(self, token, default=None):
        self.token = token
        self.id = int(token.split(":", 1)[0])
        self.webhooks: list[str] = []
        self.closed = False
        self.session = SimpleNamespace(close=self._close)
        FakeBot.instances[token] = self

    async def _close(self):
        self.closed = True

    async def get_me(self):
        if self.token.endswith(":DEAD"):
            raise RuntimeError("Unauthorized")
        return SimpleNamespace(id=self.id, username=f"bot{self.id}", full_name=f"Bot {self.id} | Быстрый VPN")

    async def set_webhook(self, url, **kwargs):
        self.webhooks.append(url)

    async def delete_webhook(self, **kwargs):
        self.webhooks.append("deleted")


class FakeHub:
    def __init__(self):
        self.bots: dict[int, object] = {}

    async def add(self, bot):
        self.bots[bot.id] = bot

    async def remove(self, bot_id):
        self.bots.pop(bot_id, None)


@needs_sqlite
@pytest.mark.anyio
async def test_runtime_follows_the_table_without_restart(session_maker, monkeypatch):
    monkeypatch.setattr(copycat_service, "Bot", FakeBot)
    monkeypatch.setattr(settings, "WEBHOOK_URL", "https://hook.example", raising=False)
    monkeypatch.setattr(type(settings), "get_legacy_mirror_items", lambda self: [])
    async with session_maker() as db:
        group = CopycatGroup(name="risky", payment_gateway_weights={"wata": 1})
        db.add(group)
        await db.flush()
        db.add(CopycatBot(token="101:A", group_id=group.id))
        db.add(CopycatBot(token="102:DEAD"))
        await db.commit()

    runtime = CopycatRuntime()
    live = await runtime.start(None, primary_bot_id=None)
    assert [bot.id for bot in live] == [101]
    assert bot_registry.get_brand_for_bot(101).name == "Bot 101"
    assert bot_registry.get_payment_weights(101) == ({"wata": 1}, "group:risky")

    hub = FakeHub()
    await runtime.attach_webhooks(hub, ["message"])
    assert FakeBot.instances["101:A"].webhooks[-1].endswith("/mirror/101")
    assert set(hub.bots) == {101}

    # Новая витрина и правка группы — без рестарта.
    async with session_maker() as db:
        db.add(CopycatBot(token="103:C", name="Своё имя"))
        rows = await list_copycat_bots(db)
        rows[0].group.payment_gateway_weights = None
        await db.commit()
    await runtime.sync()
    assert set(hub.bots) == {101, 103}
    assert bot_registry.get_brand_for_bot(103).name == "Своё имя"
    assert bot_registry.get_payment_weights(101) is None
    async with session_maker() as db:
        stored = {row.token: row for row in await list_copycat_bots(db)}
    assert stored["103:C"].telegram_bot_id == 103
    assert stored["103:C"].username == "bot103"

    # Выключенная гасится, но её аудитория остаётся витриной.
    async with session_maker() as db:
        row = next(r for r in await list_copycat_bots(db) if r.token == "101:A")
        row.enabled = False
        await db.commit()
    await runtime.sync()
    assert set(hub.bots) == {103}
    assert FakeBot.instances["101:A"].webhooks[-1] == "deleted"
    assert FakeBot.instances["101:A"].closed
    assert bot_registry.get_bot_instance(101) is None
    assert bot_registry.is_copycat_bot(101)

    # Удалённая из таблицы забывается целиком.
    async with session_maker() as db:
        row = next(r for r in await list_copycat_bots(db) if r.token == "101:A")
        await db.delete(row)
        await db.commit()
    await runtime.sync()
    assert not bot_registry.is_copycat_bot(101)
    assert 101 not in bot_registry.copycat_bot_ids()


@needs_sqlite
@pytest.mark.anyio
async def test_polling_runtime_does_not_change_the_live_set(session_maker, monkeypatch):
    monkeypatch.setattr(copycat_service, "Bot", FakeBot)
    monkeypatch.setattr(type(settings), "get_legacy_mirror_items", lambda self: [])
    runtime = CopycatRuntime()
    await runtime.start(None, primary_bot_id=None)

    async with session_maker() as db:
        db.add(CopycatBot(token="201:A"))
        await db.commit()
    await runtime.sync()

    assert runtime.live_bots() == []
