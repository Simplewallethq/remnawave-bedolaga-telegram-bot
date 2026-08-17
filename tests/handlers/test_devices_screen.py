from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.subscription import devices
from app.keyboards import inline


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
        devices,
        "RemnaWaveService",
        lambda: SimpleNamespace(get_api_client=lambda: _ApiContext()),
    )

    await devices.handle_device_management(callback, user, AsyncMock())

    assert "нет подключенных устройств" in render.await_args.args[1]
    assert render.await_args.kwargs["photo_path"] == "images/devices.jpg"
    keyboard = render.await_args.args[2]
    assert keyboard.inline_keyboard[0][0].text == "📲 Привязать устройство"
    assert keyboard.inline_keyboard[0][0].callback_data == "howto"
    assert keyboard.inline_keyboard[-1][0].callback_data == "main_menu"


def test_devices_management_has_bind_action_and_returns_to_main_menu():
    pagination = SimpleNamespace(
        total_pages=1,
        has_prev=False,
        has_next=False,
        page=1,
    )
    keyboard = inline.get_devices_management_keyboard([], pagination, "ru")

    assert keyboard.inline_keyboard[0][0].text == "📲 Привязать устройство"
    assert keyboard.inline_keyboard[0][0].callback_data == "howto"
    assert keyboard.inline_keyboard[-1][0].callback_data == "main_menu"


def test_device_unlink_confirmation_keyboard_has_yes_and_no_actions():
    keyboard = inline.get_device_unlink_confirm_keyboard(2, 3, "ru")

    assert [
        (row[0].text, row[0].callback_data)
        for row in keyboard.inline_keyboard
    ] == [
        ("✅ Да", "confirm_reset_device_2_3"),
        ("❌ Нет", "devices_page_3"),
    ]


def test_all_devices_reset_confirmation_keyboard_has_yes_and_no_actions():
    keyboard = inline.get_all_devices_reset_confirm_keyboard("ru")

    assert [
        (row[0].text, row[0].callback_data)
        for row in keyboard.inline_keyboard
    ] == [
        ("✅ Да", "confirm_reset_all_devices"),
        ("❌ Нет", "subscription_manage_devices"),
    ]


class _DeviceApiContext:
    def __init__(self, responses):
        self.requests = []
        self._responses = iter(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def _make_request(self, method, path, data=None):
        self.requests.append((method, path, data))
        return next(self._responses)


async def test_selecting_device_shows_confirmation_without_unlinking(monkeypatch):
    callback = SimpleNamespace(data="reset_device_0_1", answer=AsyncMock())
    user = SimpleNamespace(language="ru", remnawave_uuid="user-uuid")
    api = _DeviceApiContext([
        {
            "response": {
                "devices": [{"hwid": "device-1", "platform": "Android", "deviceModel": "Pixel"}]
            }
        }
    ])
    render = AsyncMock()
    monkeypatch.setattr(devices, "edit_or_answer_photo", render)
    monkeypatch.setattr(
        devices,
        "RemnaWaveService",
        lambda: SimpleNamespace(get_api_client=lambda: api),
    )

    await devices.handle_single_device_reset(callback, user, AsyncMock())

    assert api.requests == [("GET", "/api/hwid/devices/user-uuid", None)]
    assert render.await_args.args[1] == "Вы точно хотите отвязать Android - Pixel?"


async def test_confirming_device_unlink_shows_success_and_returns_to_devices(monkeypatch):
    callback = SimpleNamespace(data="confirm_reset_device_0_1", answer=AsyncMock())
    user = SimpleNamespace(language="ru", remnawave_uuid="user-uuid", telegram_id=42)
    api = _DeviceApiContext([
        {
            "response": {
                "devices": [{"hwid": "device-1", "platform": "Android", "deviceModel": "Pixel"}]
            }
        },
        {"response": {}},
    ])
    render = AsyncMock()
    monkeypatch.setattr(devices, "edit_or_answer_photo", render)
    monkeypatch.setattr(
        devices,
        "RemnaWaveService",
        lambda: SimpleNamespace(get_api_client=lambda: api),
    )

    await devices.confirm_single_device_reset(callback, user, AsyncMock())

    assert api.requests == [
        ("GET", "/api/hwid/devices/user-uuid", None),
        (
            "POST",
            "/api/hwid/devices/delete",
            {"userUuid": "user-uuid", "hwid": "device-1"},
        ),
    ]
    assert render.await_args.args[1] == "✅ Устройство Android - Pixel успешно отвязано!"
    assert render.await_args.args[2].inline_keyboard[0][0].callback_data == "subscription_manage_devices"


async def test_reset_all_devices_shows_confirmation_without_deleting(monkeypatch):
    callback = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(language="ru")
    render = AsyncMock()
    monkeypatch.setattr(devices, "edit_or_answer_photo", render)

    await devices.show_all_devices_reset_confirmation(callback, user, AsyncMock())

    assert render.await_args.args[1] == "Вы точно хотите сбросить все устройства?"
    keyboard = render.await_args.args[2]
    assert keyboard.inline_keyboard[0][0].callback_data == "confirm_reset_all_devices"
    callback.answer.assert_awaited_once()


async def test_confirming_reset_all_devices_deletes_every_hwid(monkeypatch):
    callback = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(language="ru", remnawave_uuid="user-uuid", telegram_id=42)
    api = _DeviceApiContext([
        {
            "response": {
                "devices": [{"hwid": "device-1"}, {"hwid": "device-2"}]
            }
        },
        {"response": {}},
        {"response": {}},
    ])
    render = AsyncMock()
    monkeypatch.setattr(devices, "edit_or_answer_photo", render)
    monkeypatch.setattr(
        devices,
        "RemnaWaveService",
        lambda: SimpleNamespace(get_api_client=lambda: api),
    )

    await devices.handle_all_devices_reset_from_management(callback, user, AsyncMock())

    assert api.requests == [
        ("GET", "/api/hwid/devices/user-uuid", None),
        (
            "POST",
            "/api/hwid/devices/delete",
            {"userUuid": "user-uuid", "hwid": "device-1"},
        ),
        (
            "POST",
            "/api/hwid/devices/delete",
            {"userUuid": "user-uuid", "hwid": "device-2"},
        ),
    ]
    assert "Все устройства успешно сброшены" in render.await_args.args[1]
    assert render.await_args.args[2].inline_keyboard[0][0].callback_data == "subscription_manage_devices"
