from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.database.crud import share_token
from app.handlers import start


async def test_trial_install_prompt_is_shown_after_trial_activation(monkeypatch) -> None:
    user = SimpleNamespace(telegram_id=42, language="ru", subscription=None)
    events = []

    async def activate_trial(*args, **kwargs) -> bool:
        user.subscription = SimpleNamespace(id=7, actual_status="trial")
        events.append("activated")
        return True

    async def show_prompt(*args, **kwargs) -> None:
        assert user.subscription.actual_status == "trial"
        events.append("prompt_shown")

    monkeypatch.setattr(start, "activate_trial_for_user", activate_trial)
    monkeypatch.setattr(start, "_show_trial_install_prompt", show_prompt)

    result = await start._auto_activate_trial_and_show_install_prompt(
        AsyncMock(),
        AsyncMock(),
        user,
        AsyncMock(),
        language="ru",
    )

    assert result is True
    assert events == ["activated", "prompt_shown"]


async def test_trial_install_prompt_uses_share_page_and_main_menu(monkeypatch) -> None:
    message = AsyncMock()
    user = SimpleNamespace(
        telegram_id=42,
        language="ru",
        subscription=SimpleNamespace(id=7, actual_status="trial"),
    )
    token = SimpleNamespace(token="share-token")

    monkeypatch.setattr(
        type(start.settings),
        "get_share_site_base_url",
        lambda self: "https://vpn.example",
    )
    monkeypatch.setattr(share_token, "get_or_create_share_token", AsyncMock(return_value=token))

    await start._show_trial_install_prompt(
        AsyncMock(),
        user,
        message,
        is_callback=False,
        language="ru",
    )

    text, = message.answer.await_args.args
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert text == (
        "Привет, тебе доступна бесплатная подписка на 3 дня! "
        "Установи приложение и пользуйся - займет меньше минуты."
    )
    assert keyboard.inline_keyboard[0][0].url == "https://vpn.example/s/share-token"
    assert keyboard.inline_keyboard[1][0].callback_data == "main_menu"


async def test_trial_install_prompt_requires_active_subscription(monkeypatch) -> None:
    message = AsyncMock()
    user = SimpleNamespace(
        telegram_id=42,
        language="en",
        subscription=SimpleNamespace(id=7, actual_status="disabled"),
    )
    create_token = AsyncMock()

    monkeypatch.setattr(share_token, "get_or_create_share_token", create_token)

    await start._show_trial_install_prompt(
        AsyncMock(),
        user,
        message,
        is_callback=False,
        language="en",
    )

    create_token.assert_not_awaited()
    message.answer.assert_not_awaited()


async def test_trial_install_prompt_omits_install_button_without_share_site(monkeypatch) -> None:
    message = AsyncMock()
    user = SimpleNamespace(
        telegram_id=42,
        language="en",
        subscription=SimpleNamespace(id=7, actual_status="trial"),
    )
    monkeypatch.setattr(
        type(start.settings),
        "get_share_site_base_url",
        lambda self: None,
    )

    await start._show_trial_install_prompt(
        AsyncMock(),
        user,
        message,
        is_callback=False,
        language="en",
    )

    text, = message.answer.await_args.args
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert text == (
        "Hi, you have a free 3-day subscription! Install the app and start using it "
        "- it takes less than a minute."
    )
    assert len(keyboard.inline_keyboard) == 1
    assert keyboard.inline_keyboard[0][0].callback_data == "main_menu"
