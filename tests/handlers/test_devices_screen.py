from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.subscription import devices
from app.keyboards import inline
from app.services import remnawave_service


def _callback():
    return SimpleNamespace(answer=AsyncMock())


async def test_devices_list_uses_devices_image(monkeypatch):
    callback = _callback()
    user = SimpleNamespace(language="ru")
    render = AsyncMock()
    monkeypatch.setattr(devices, "edit_or_answer_photo", render)

    await devices.show_devices_page(
        callback,
        user,
        [{"platform": "Android", "deviceModel": "Pixel"}],
    )

    assert "Подключенные устройства" in render.await_args.args[1]
    assert render.await_args.kwargs["photo_path"] == "images/devices.jpg"


class _ApiContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def _make_request(self, _method, _path):
        return {"response": {"total": 0, "devices": []}}


async def test_empty_devices_screen_uses_devices_image(monkeypatch):
    callback = _callback()
    user = SimpleNamespace(
        language="ru",
        subscription=SimpleNamespace(is_trial=False),
        remnawave_uuid="user-uuid",
    )
    render = AsyncMock()
    monkeypatch.setattr(devices, "edit_or_answer_photo", render)
    monkeypatch.setattr(
        remnawave_service,
        "RemnaWaveService",
        lambda: SimpleNamespace(get_api_client=lambda: _ApiContext()),
    )

    await devices.handle_device_management(callback, user, AsyncMock())

    assert "нет подключенных устройств" in render.await_args.args[1]
    assert render.await_args.kwargs["photo_path"] == "images/devices.jpg"
    assert render.await_args.args[2].inline_keyboard[0][0].callback_data == "subscription"


def test_devices_management_back_returns_to_subscription_management():
    pagination = SimpleNamespace(
        total_pages=1,
        has_prev=False,
        has_next=False,
        page=1,
    )
    keyboard = inline.get_devices_management_keyboard([], pagination, "ru")

    assert keyboard.inline_keyboard[-1][0].callback_data == "subscription"
