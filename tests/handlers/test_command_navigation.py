from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.handlers import commands, start
from app.utils.fsm import clear_state_preserving_pending_start_payload


def _message():
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=42)
    message.bot = AsyncMock()
    message.answer = AsyncMock(return_value=AsyncMock())
    return message


async def test_connect_command_reuses_howto_handler(monkeypatch):
    message = _message()
    state = AsyncMock()
    state.get_data.return_value = {}
    db = AsyncMock()
    handler = AsyncMock()
    monkeypatch.setattr(commands.menu, "handle_howto", handler)

    await commands.command_connect(message, state, db)

    state.clear.assert_awaited_once()
    message.answer.assert_awaited_once_with("Loading...")
    callback, received_state, received_db = handler.await_args.args
    assert callback.message is message.answer.return_value
    assert callback.from_user is message.from_user
    assert callback.bot is message.bot
    assert received_state is state
    assert received_db is db


async def test_subscription_command_reuses_subscription_menu_handler(monkeypatch):
    message = _message()
    state = AsyncMock()
    state.get_data.return_value = {}
    db_user = SimpleNamespace()
    db = AsyncMock()
    handler = AsyncMock()
    monkeypatch.setattr(commands.subscription.purchase, "handle_subscription_menu", handler)

    await commands.command_subscription(message, state, db_user, db)

    state.clear.assert_awaited_once()
    callback, received_user, received_db = handler.await_args.args
    assert callback.message is message.answer.return_value
    assert received_user is db_user
    assert received_db is db


async def test_referrals_profile_and_support_commands_reuse_menu_handlers(monkeypatch):
    message = _message()
    state = AsyncMock()
    state.get_data.return_value = {}
    db_user = SimpleNamespace()
    db = AsyncMock()
    referrals_handler = AsyncMock()
    profile_handler = AsyncMock()
    support_handler = AsyncMock()
    monkeypatch.setattr(commands.menu, "handle_referral", referrals_handler)
    monkeypatch.setattr(commands.menu, "handle_profile", profile_handler)
    monkeypatch.setattr(commands.menu, "handle_support", support_handler)

    await commands.command_referrals(message, state, db_user, db)
    await commands.command_profile(message, state, db_user, db)
    await commands.command_support(message, state, db_user, db)

    assert state.clear.await_count == 3
    for handler in (referrals_handler, profile_handler, support_handler):
        callback, received_user, received_db = handler.await_args.args
        assert callback.message is message.answer.return_value
        assert received_user is db_user
        assert received_db is db


async def test_command_callback_renders_callback_alert_text():
    screen_message = AsyncMock()
    callback = commands._CommandScreenCallback(_message(), screen_message)

    await callback.answer("Subscription is unavailable", show_alert=True)
    await callback.answer()

    screen_message.edit_text.assert_awaited_once_with("Subscription is unavailable")


async def test_navigation_state_reset_preserves_pending_start_payload():
    state = AsyncMock()
    state.get_data.return_value = {
        "pending_start_payload": "referral-code",
        "selected_period": 30,
    }

    await clear_state_preserving_pending_start_payload(state)

    state.clear.assert_awaited_once()
    state.update_data.assert_awaited_once_with(
        pending_start_payload="referral-code"
    )


async def test_connect_command_preserves_pending_start_payload(monkeypatch):
    message = _message()
    state = AsyncMock()
    state.get_data.return_value = {"pending_start_payload": "partner-link"}
    db = AsyncMock()
    handler = AsyncMock()
    monkeypatch.setattr(commands.menu, "handle_howto", handler)

    await commands.command_connect(message, state, db)

    state.clear.assert_awaited_once()
    state.update_data.assert_awaited_once_with(
        pending_start_payload="partner-link"
    )


def test_register_handlers_registers_all_botfather_commands():
    dispatcher = MagicMock()

    commands.register_handlers(dispatcher)

    assert [call.args[0] for call in dispatcher.message.register.call_args_list] == [
        commands.command_connect,
        commands.command_subscription,
        commands.command_referrals,
        commands.command_profile,
        commands.command_support,
    ]


async def test_plain_start_clears_state_before_showing_main_menu(monkeypatch):
    events = []
    message = _message()
    message.text = "/start"
    message.from_user = SimpleNamespace(
        id=42,
        username="user",
        first_name="First",
        last_name="Last",
    )

    async def clear_state():
        events.append("state_cleared")

    async def send_menu(*args, **kwargs):
        events.append("menu_sent")
        return AsyncMock()

    state = AsyncMock()
    state.clear.side_effect = clear_state
    state.get_data.return_value = {}
    message.answer.side_effect = send_menu
    db = AsyncMock()
    user = SimpleNamespace(
        status="active",
        telegram_id=42,
        username="user",
        first_name="First",
        last_name="Last",
        last_activity=None,
        balance_kopeks=0,
        subscription=None,
        language="ru",
    )

    monkeypatch.setattr(start, "get_active_pinned_message", AsyncMock(return_value=None))
    monkeypatch.setattr(start, "get_main_menu_text", AsyncMock(return_value="Main menu"))
    monkeypatch.setattr(start, "get_new_main_menu_keyboard", MagicMock())
    monkeypatch.setattr(
        type(start.settings),
        "is_text_main_menu_mode",
        lambda self: True,
    )

    await start.cmd_start(message, state, db, user)

    assert events[0] == "state_cleared"
    assert "menu_sent" in events
