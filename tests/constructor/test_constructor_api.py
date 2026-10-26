"""API мини-аппа конструктора: авторизация, создание бота, настройки, кошелёк, вывод."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.constructor import api, service
from app.database.database import get_db
from app.database.models import Transaction, TransactionType, User
from app.services import owner_platform_service as platform
from app.services import price_scheme_service as schemes

from .conftest import OTHER_TOKEN, TOKEN, needs_sqlite, png_bytes

pytestmark = [pytest.mark.anyio, needs_sqlite]

OWNER = service.TgUser(id=1, username="owner_name", first_name="Owner")
WALLET = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@pytest.fixture
def client(db, telegram):
    app = FastAPI()
    app.include_router(api.router, prefix="/constructor/api")

    async def test_db():
        yield db

    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[api.tg_user] = lambda: OWNER
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test/constructor/api")


@pytest.fixture
def prices(monkeypatch, db):
    schemes.set_snapshot_for_tests([], base={"solo": {30: 15000}, "plus": {30: 25000}, "pro": {30: 50000}})

    async def reload_from_test_db():
        await schemes.load_schemes(db)

    monkeypatch.setattr(schemes, "reload_schemes", reload_from_test_db)
    monkeypatch.setattr(settings, "OWNER_PRICE_MIN_SOLO_RUB", 120, raising=False)
    monkeypatch.setattr(settings, "OWNER_PRICE_PERIOD_MULTIPLIERS", "90:2,360:5,720:7", raising=False)
    yield
    from app.utils import bot_registry

    bot_registry.clear()
    schemes.set_snapshot_for_tests([])


async def _create(client, telegram, **form):
    telegram(TOKEN, 777000, "My_Vpn_Bot", "My VPN")
    data = {"token": f"Use this token:\n{TOKEN}", "support": "@help_desk", **form}
    return await client.post("/bots", data=data)


def _signed_init_data(token: str, user: dict, auth_date: int | None = None) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "user": json.dumps(user)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def test_init_data_signature(db, telegram):
    app = FastAPI()
    app.include_router(api.router, prefix="/api")

    async def test_db():
        yield db

    app.dependency_overrides[get_db] = test_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api") as raw:
        assert (await raw.get("/state")).status_code == 401
        good = _signed_init_data(settings.CONSTRUCTOR_BOT_TOKEN, {"id": 42, "username": "someone"})
        response = await raw.get("/state", headers={"Authorization": f"tma {good}"})
        assert response.status_code == 200 and response.json()["user"]["id"] == 42
        # подписано чужим токеном (например, основного бота) — не пускаем
        forged = _signed_init_data("111:other-token", {"id": 42})
        assert (await raw.get("/state", headers={"Authorization": f"tma {forged}"})).status_code == 401


async def test_state_of_newcomer_does_not_create_account(client, db):
    async with client:
        state = (await client.get("/state")).json()
    assert state["bots"] == [] and state["available"] == 0 and state["wallet"] is None
    assert state["revshare"] == settings.OWNER_DEFAULT_REVSHARE_PERCENT
    assert await service.find_owner_account(db, OWNER.id) is None


async def test_token_check_and_create(client, telegram, prices):
    telegram(TOKEN, 777000, "My_Vpn_Bot", "My VPN")
    async with client:
        bad = await client.post("/token", json={"text": "привет"})
        assert bad.status_code == 400 and bad.json()["detail"]["field"] == "token"
        ok = (await client.post("/token", json={"text": TOKEN})).json()
        assert ok == {"username": "My_Vpn_Bot", "title": "My VPN", "restore": False, "foreign_webhook": False}

        no_support = await client.post("/bots", data={"token": TOKEN, "support": "x"})
        assert no_support.status_code == 400 and no_support.json()["detail"]["field"] == "support"

        response = await client.post("/bots", data={"token": TOKEN, "support": "@help_desk"},
                                     files={"image": ("cover.png", png_bytes(), "image/png")})
        assert response.status_code == 200, response.text
        body = response.json()
        bot = body["bot"]
        assert not body["restored"]
        assert (bot["username"], bot["support"], bot["custom_cover"], bot["link"]) == (
            "My_Vpn_Bot", "help_desk", True, "https://t.me/My_Vpn_Bot"
        )
        assert bot["prices"]["monthly"] == {"solo": 150, "plus": 250, "pro": 500}
        assert bot["stats"] == {"users_total": 0, "users_yesterday": 0, "turnover": 0}

        state = (await client.get("/state")).json()
        assert [b["username"] for b in state["bots"]] == ["My_Vpn_Bot"]

        again = await client.post("/bots", data={"token": TOKEN, "support": "@help_desk"})
        assert again.status_code == 400 and again.json()["detail"]["code"] == "active"


async def test_foreign_webhook_needs_confirmation(client, telegram, prices):
    telegram(TOKEN, 777000, "My_Vpn_Bot", webhook="https://other.service/hook")
    async with client:
        first = await client.post("/bots", data={"token": TOKEN, "support": "help_desk"})
        assert first.status_code == 409 and first.json()["detail"]["code"] == "webhook"
        forced = await client.post("/bots", data={"token": TOKEN, "support": "help_desk", "force": "true"})
        assert forced.status_code == 200


async def test_bot_settings(client, telegram, prices):
    async with client:
        bot = (await _create(client, telegram)).json()["bot"]
        path = f"/bots/{bot['id']}"

        assert (await client.patch(path, json={"support": "кириллица"})).status_code == 400
        assert (await client.patch(path, json={"support": "t.me/new_support"})).json()["bot"]["support"] == "new_support"

        # Plus должен быть на 10% дороже Solo
        low = await client.patch(path, json={"prices": {"solo": 200, "plus": 210, "pro": 400}})
        assert low.status_code == 400
        assert low.json()["detail"]["field"] == "prices.plus" and "220" in low.json()["detail"]["error"]

        # ниже минимальных цен для покупателей (цены основного бота) — нельзя
        floor = (await client.post("/prices/preview", json={"prices": {"solo": 200, "plus": 220, "pro": 300}})).json()
        assert set(floor["errors"]) == {"plus", "pro"} and "250" in floor["errors"]["plus"]

        preview = (await client.post("/prices/preview", json={"prices": {"solo": 200, "plus": 250, "pro": 500}})).json()
        assert preview["errors"] == {}
        assert preview["periods"]["solo"] == {"30": 200, "90": 400, "360": 1000, "720": 1400}

        saved = await client.patch(path, json={"prices": {"solo": 200, "plus": 250, "pro": 500}})
        assert saved.status_code == 200, saved.text
        assert saved.json()["bot"]["prices"]["monthly"] == {"solo": 200, "plus": 250, "pro": 500}

        cover = await client.post(f"{path}/cover", files={"image": ("c.png", png_bytes(), "image/png")})
        assert cover.json()["bot"]["custom_cover"]
        assert (await client.get(f"{path}/cover")).status_code == 200
        reset = await client.delete(f"{path}/cover")
        assert not reset.json()["bot"]["custom_cover"]

        assert (await client.delete(path)).json() == {"deleted": bot["id"]}
        assert (await client.get(path)).status_code == 404
        assert (await client.get("/state")).json()["bots"] == []


async def test_other_owner_cannot_touch_bot(client, telegram, prices, db):
    async with client:
        bot = (await _create(client, telegram)).json()["bot"]
        client._transport.app.dependency_overrides[api.tg_user] = lambda: service.TgUser(id=2, username="stranger")
        assert (await client.get(f"/bots/{bot['id']}")).status_code == 404
        assert (await client.patch(f"/bots/{bot['id']}", json={"support": "hijack_me"})).status_code == 404
        assert (await client.delete(f"/bots/{bot['id']}")).status_code == 404


async def test_wallet_and_payout(client, telegram, prices, db, monkeypatch):
    monkeypatch.setattr(settings, "OWNER_DEFAULT_REVSHARE_PERCENT", 60, raising=False)
    monkeypatch.setattr(settings, "OWNER_PAYOUT_HOLD_DAYS", 0, raising=False)
    monkeypatch.setattr(settings, "OWNER_PAYOUT_FEE_USDT", 3.0, raising=False)
    monkeypatch.setattr(
        platform, "get_payout_rate",
        AsyncMock(return_value=platform.PayoutRate(cbr_rate=Decimal("100"), rate=Decimal("100"))),
    )
    notify = AsyncMock()
    monkeypatch.setattr(platform, "notify_admins_new_payout", notify)

    async with client:
        await _create(client, telegram)
        # клиент бота пополнил на 1000 ₽ → владельцу 600 ₽ = 6 USDT
        buyer = User(telegram_id=10, referral_code="buyer", bot_id=777000)
        db.add(buyer)
        await db.flush()
        db.add(Transaction(user_id=buyer.id, type=TransactionType.DEPOSIT.value, amount_kopeks=100000,
                           payment_method="platega", is_completed=True))
        await db.commit()

        no_wallet = await client.post("/payouts", json={"amount": "5"})
        assert no_wallet.status_code == 400 and no_wallet.json()["detail"]["field"] == "wallet"

        assert (await client.put("/wallet", json={"address": "T123"})).status_code == 400
        assert (await client.put("/wallet", json={"address": WALLET})).json() == {"wallet": WALLET}

        earnings = (await client.get("/earnings")).json()
        assert (earnings["available"], earnings["available_usdt"], earnings["wallet"]) == (600, "6.00", WALLET)

        assert (await client.post("/payouts", json={"amount": "7"})).status_code == 400  # больше доступного
        assert (await client.post("/payouts", json={"amount": "3"})).status_code == 400  # не больше комиссии
        done = await client.post("/payouts", json={"amount": "5.50"})
        assert done.status_code == 200, done.text
        payout = done.json()["payout"]
        assert (payout["amount_usdt"], payout["send_usdt"], payout["status"]) == ("5.50", "2.50", "pending")
        assert done.json()["earnings"]["available"] == 50
        notify.assert_awaited_once()


async def test_old_init_data_from_telegram_desktop_is_accepted(db, telegram, monkeypatch):
    """Telegram Desktop кэширует initData (tdesktop#28303): подпись недельной
    давности должна проходить, просроченная по настройке — нет."""
    app = FastAPI()
    app.include_router(api.router, prefix="/api")

    async def test_db():
        yield db

    app.dependency_overrides[get_db] = test_db
    week_old = _signed_init_data(settings.CONSTRUCTOR_BOT_TOKEN, {"id": 42}, auth_date=int(time.time()) - 7 * 86400)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api") as raw:
        assert (await raw.get("/state", headers={"Authorization": f"tma {week_old}"})).status_code == 200
        monkeypatch.setattr(settings, "CONSTRUCTOR_INIT_DATA_MAX_AGE_SECONDS", 86400, raising=False)
        assert (await raw.get("/state", headers={"Authorization": f"tma {week_old}"})).status_code == 401


async def test_status_reports_starting_until_runtime_brings_bot_up(client, telegram, prices, monkeypatch):
    from app.services.copycat_service import copycat_runtime

    working = AsyncMock(return_value=True)
    monkeypatch.setattr(platform, "bot_is_working", working)
    monkeypatch.setattr(type(copycat_runtime), "can_change_live_set", property(lambda self: True))
    async with client:
        bot = (await _create(client, telegram)).json()["bot"]
        status = (await client.get(f"/bots/{bot['id']}/status")).json()
        assert status == {"working": False, "starting": True}
        working.assert_not_awaited()  # пока бот не поднят, в Telegram не ходим

        monkeypatch.setitem(copycat_runtime._live, bot["id"], object())
        assert (await client.get(f"/bots/{bot['id']}/status")).json() == {"working": True, "starting": False}
