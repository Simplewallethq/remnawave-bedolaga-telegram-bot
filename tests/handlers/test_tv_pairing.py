from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.external import tv_pairing
from app.handlers import start


class FakeState:
    """Минимальный FSMContext: хранит данные и отдаёт их как aiogram."""

    def __init__(self, data=None):
        self._data = dict(data or {})

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)
        return dict(self._data)


def _callback(data):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


def _user(language="ru", user_id=555):
    return SimpleNamespace(id=user_id, language=language, telegram_id=999)


@pytest.fixture
def authorize(monkeypatch):
    """Подменяет вызов бэкенда и запоминает аргументы."""
    mock = AsyncMock(return_value=tv_pairing.STATUS_COMPLETED)
    monkeypatch.setattr(tv_pairing, "authorize_tv", mock)
    return mock


async def test_confirm_sends_code_and_reports_success(authorize):
    state = FakeState({"tv_pairing_code": "502476"})
    callback = _callback("tv_pair_confirm")

    await start.process_tv_pairing_decision(callback, state, db_user=_user())

    authorize.assert_awaited_once_with("502476", 555)
    text = callback.message.edit_text.await_args.args[0]
    assert "подключён" in text.lower()


async def test_confirm_clears_code_so_a_double_tap_cannot_replay(authorize):
    state = FakeState({"tv_pairing_code": "502476"})

    await start.process_tv_pairing_decision(_callback("tv_pair_confirm"), state, db_user=_user())
    assert (await state.get_data())["tv_pairing_code"] is None

    # Второе нажатие не должно уйти на бэкенд: сессия уже потрачена.
    await start.process_tv_pairing_decision(_callback("tv_pair_confirm"), state, db_user=_user())
    assert authorize.await_count == 1


async def test_cancel_never_calls_the_backend(authorize):
    state = FakeState({"tv_pairing_code": "502476"})
    callback = _callback("tv_pair_cancel")

    await start.process_tv_pairing_decision(callback, state, db_user=_user())

    authorize.assert_not_awaited()
    assert "отменено" in callback.message.edit_text.await_args.args[0].lower()


@pytest.mark.parametrize(
    "status",
    [
        tv_pairing.STATUS_DEVICE_LIMIT,
        tv_pairing.STATUS_NO_SUBSCRIPTION,
        tv_pairing.STATUS_ERROR,
    ],
)
async def test_retryable_outcomes_keep_the_code_and_offer_the_button(authorize, status):
    """Зритель освобождает слот или покупает подписку и жмёт ту же кнопку —
    возвращаться к телевизору за новым кодом не нужно."""
    authorize.return_value = status
    state = FakeState({"tv_pairing_code": "502476"})
    callback = _callback("tv_pair_confirm")

    await start.process_tv_pairing_decision(callback, state, db_user=_user())

    assert (await state.get_data())["tv_pairing_code"] == "502476"
    keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "tv_pair_confirm"


@pytest.mark.parametrize(
    "status",
    [
        tv_pairing.STATUS_PAIRING_EXPIRED,
        tv_pairing.STATUS_PAIRING_USED,
        tv_pairing.STATUS_PAIRING_NOT_FOUND,
    ],
)
async def test_dead_session_outcomes_drop_the_code_and_offer_no_retry(authorize, status):
    authorize.return_value = status
    state = FakeState({"tv_pairing_code": "502476"})
    callback = _callback("tv_pair_confirm")

    await start.process_tv_pairing_decision(callback, state, db_user=_user())

    assert (await state.get_data())["tv_pairing_code"] is None
    assert callback.message.edit_text.await_args.kwargs["reply_markup"] is None


async def test_missing_code_does_not_call_the_backend(authorize):
    callback = _callback("tv_pair_confirm")

    await start.process_tv_pairing_decision(callback, FakeState(), db_user=_user())

    authorize.assert_not_awaited()


async def test_english_user_gets_english_copy(authorize):
    state = FakeState({"tv_pairing_code": "502476"})
    callback = _callback("tv_pair_confirm")

    await start.process_tv_pairing_decision(callback, state, db_user=_user(language="en"))

    assert "TV connected" in callback.message.edit_text.await_args.args[0]


def test_is_configured_requires_a_url(monkeypatch):
    monkeypatch.setattr(tv_pairing.settings, "APP_TV_PAIRING_WEBHOOK_URL", "")
    assert tv_pairing.is_configured() is False

    monkeypatch.setattr(
        tv_pairing.settings, "APP_TV_PAIRING_WEBHOOK_URL", "https://example.com/hook"
    )
    monkeypatch.setattr(tv_pairing.settings, "APP_PUSH_API_KEY", "k")
    assert tv_pairing.is_configured() is True
