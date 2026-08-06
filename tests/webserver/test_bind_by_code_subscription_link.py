"""Привязка устройства по коду подписки / ссылке remnawave.

DB не поднимаем: цепочка фолбэков в `bind_device_by_code` — это чистая
маршрутизация между тремя CRUD-лукапами, поэтому подменяем сами лукапы и
проверяем, какой путь отработал и что вернулось приложению.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("BOT_TOKEN", "test-token")

from app.database.crud.device_binding_code import (  # noqa: E402
    CONSUME_NOT_FOUND,
    CONSUME_OK,
)
from app.database.crud.share_token import ACTIVATE_NOT_FOUND, ACTIVATE_OK  # noqa: E402
from app.webapi.routes import devices as devices_route  # noqa: E402
from app.webapi.schemas.devices import BindByCodeRequest  # noqa: E402

CODE = "-BgpfZQ062Td9Fpk"
LINK = f"https://letovpn.com/sub/{CODE}"
DEVICE_ID = "test-device-1"


class FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, *_args, **_kwargs):  # pragma: no cover - не должен вызываться
        raise AssertionError("Прямых запросов в этом пути быть не должно")


def _make_user() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        last_app_name=None,
        acquisition_source=None,
        tg_user_id=None,
        has_used_mobile_app=False,
    )


def _make_subscription(*, device_limit: int = 3) -> SimpleNamespace:
    return SimpleNamespace(id=42, device_limit=device_limit, user=_make_user())


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch):
    """Подменяет все внешние вызовы роутера и пишет ход выполнения в calls."""
    calls: dict[str, list] = {
        "consume": [],
        "share": [],
        "short_uuid": [],
        "created": [],
        "rebound": [],
    }
    state = {
        "subscription": _make_subscription(),
        "existing_link": None,
        "link_count": 0,
        "consume": (None, CONSUME_NOT_FOUND),
        "share": (None, ACTIVATE_NOT_FOUND, {}),
        "short_uuid_hit": True,
    }

    async def fake_consume(_db, code, device_id):
        calls["consume"].append((code, device_id))
        return state["consume"]

    async def fake_activate(_db, code, device_id):
        calls["share"].append((code, device_id))
        return state["share"]

    async def fake_by_short_uuid(_db, short_uuid):
        calls["short_uuid"].append(short_uuid)
        return state["subscription"] if state["short_uuid_hit"] else None

    async def fake_get_device_link(_db, _device_id):
        return state["existing_link"]

    async def fake_count(_db, _subscription_id):
        return state["link_count"]

    async def fake_create(_db, subscription_id, device_id):
        calls["created"].append((subscription_id, device_id))
        state["link_count"] += 1

    async def fake_rebind(_db, link, subscription_id):
        calls["rebound"].append((link.subscription_id, subscription_id))
        state["link_count"] += 1

    monkeypatch.setattr(devices_route, "consume_binding_code", fake_consume)
    monkeypatch.setattr(devices_route, "activate_share_code", fake_activate)
    monkeypatch.setattr(devices_route, "get_subscription_by_short_uuid", fake_by_short_uuid)
    monkeypatch.setattr(devices_route, "get_device_link", fake_get_device_link)
    monkeypatch.setattr(devices_route, "count_device_links", fake_count)
    monkeypatch.setattr(devices_route, "create_device_link", fake_create)
    monkeypatch.setattr(devices_route, "rebind_device_link", fake_rebind)
    monkeypatch.setattr(
        devices_route,
        "_serialize_subscription",
        lambda sub: SimpleNamespace(subscription_id=sub.id, connected_devices=0),
    )
    return SimpleNamespace(calls=calls, state=state, db=FakeDB())


async def _bind(db, code: str, *, app_name: str | None = None):
    payload = BindByCodeRequest(code=code, device_id=DEVICE_ID, app_name=app_name)
    return await devices_route.bind_device_by_code(payload, None, db)


@pytest.mark.asyncio
async def test_link_binds_device_and_skips_code_lookups(wiring) -> None:
    """Ссылка не может быть кодом привязки, поэтому идём сразу к подписке —
    и не тратим with_for_update на заведомо чужой формат."""
    response = await _bind(wiring.db, LINK)

    assert response.subscription_id == 42
    assert response.connected_devices == 1
    assert wiring.calls["created"] == [(42, DEVICE_ID)]
    assert wiring.calls["short_uuid"] == [CODE]
    assert wiring.calls["consume"] == []
    assert wiring.calls["share"] == []


@pytest.mark.asyncio
async def test_link_with_trailing_path_and_query(wiring) -> None:
    """Ссылка из клиента приходит с хвостом — код всё равно должен найтись."""
    await _bind(wiring.db, f"{LINK}/happ?utm=tg")
    assert wiring.calls["short_uuid"] == [CODE]


@pytest.mark.asyncio
async def test_unknown_link_returns_404(wiring) -> None:
    """Приложение показывает «неверный код» — teleVpn мапит 404 в invalid_code."""
    wiring.state["short_uuid_hit"] = False

    with pytest.raises(HTTPException) as exc:
        await _bind(wiring.db, LINK)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Code not found"


@pytest.mark.asyncio
async def test_link_respects_device_limit(wiring) -> None:
    """Вход по ссылке — полноценная привязка, поэтому лимит устройств
    действует так же, как для кода привязки."""
    wiring.state["subscription"] = _make_subscription(device_limit=2)
    wiring.state["link_count"] = 2

    with pytest.raises(HTTPException) as exc:
        await _bind(wiring.db, LINK)

    assert exc.value.status_code == 422
    assert "Device limit exceeded (2/2)" in exc.value.detail
    assert wiring.calls["created"] == []


@pytest.mark.asyncio
async def test_link_rebind_is_idempotent_for_same_subscription(wiring) -> None:
    """Повторный вход с того же устройства не должен занимать второй слот."""
    wiring.state["existing_link"] = SimpleNamespace(subscription_id=42)
    wiring.state["link_count"] = 1

    response = await _bind(wiring.db, LINK)

    assert response.connected_devices == 1
    assert wiring.calls["created"] == []
    assert wiring.calls["rebound"] == []


@pytest.mark.asyncio
async def test_link_moves_device_from_another_subscription(wiring) -> None:
    wiring.state["existing_link"] = SimpleNamespace(subscription_id=7)

    await _bind(wiring.db, LINK)

    assert wiring.calls["rebound"] == [(7, 42)]


@pytest.mark.asyncio
async def test_link_stores_app_name_but_not_referrer(wiring) -> None:
    """app_name описывает клиент устройства и обновляется, а install referrer
    по ссылке не применяем — ссылку мог переслать не владелец подписки."""
    subscription = wiring.state["subscription"]

    await _bind(wiring.db, LINK, app_name="letoAndroid/1.2.1")

    assert subscription.user.last_app_name == "letoAndroid/1.2.1"
    assert subscription.user.acquisition_source is None
    assert wiring.db.commits == 1


@pytest.mark.asyncio
async def test_bare_code_falls_through_to_subscription_lookup(wiring) -> None:
    """Голый short uuid — тот же «код подписки», что и в кабинете: пробуем его
    последним, после кода привязки и share-кода."""
    response = await _bind(wiring.db, CODE)

    assert response.subscription_id == 42
    assert wiring.calls["consume"] == [(CODE, DEVICE_ID)]
    assert wiring.calls["share"] == [(CODE, DEVICE_ID)]
    assert wiring.calls["short_uuid"] == [CODE]


@pytest.mark.asyncio
async def test_binding_code_path_unchanged(wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    """Регресс: личный код привязки по-прежнему обслуживается первым лукапом."""
    subscription = wiring.state["subscription"]
    wiring.state["consume"] = (subscription, CONSUME_OK)
    monkeypatch.setattr(
        devices_route,
        "_refetch_subscription_with_user",
        lambda _db, sub_id: _async_value(subscription),
    )

    response = await _bind(wiring.db, "ABCDEFGH12345678")

    assert response.subscription_id == 42
    assert wiring.calls["share"] == []
    assert wiring.calls["short_uuid"] == []
    assert wiring.calls["created"] == [(42, DEVICE_ID)]


@pytest.mark.asyncio
async def test_share_code_path_unchanged(wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    """Регресс: share-код обслуживается вторым лукапом и не доходит до подписки."""
    subscription = wiring.state["subscription"]
    wiring.state["share"] = (subscription, ACTIVATE_OK, {})
    monkeypatch.setattr(
        devices_route,
        "_refetch_subscription_with_user",
        lambda _db, sub_id: _async_value(subscription),
    )

    response = await _bind(wiring.db, "ABCDEFGH12345678")

    assert response.subscription_id == 42
    assert wiring.calls["short_uuid"] == []


@pytest.mark.asyncio
async def test_blank_code_is_rejected(wiring) -> None:
    with pytest.raises(HTTPException) as exc:
        await _bind(wiring.db, "https://letovpn.com/sub/")

    assert exc.value.status_code == 404


async def _async_value(value):
    return value
