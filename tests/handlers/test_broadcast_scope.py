"""Область ботов для админских рассылок: leto | all | copycat:<bot_id>."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.handlers.admin import messages as admin_messages
from app.keyboards import admin as admin_keyboards
from app.utils import bot_registry
from app.webapi.schemas.broadcasts import BroadcastCreateRequest


PRIMARY_BOT_ID = 123456
MIRROR_BOT_ID = 2001
COPYCAT_BOT_ID = 3001
OTHER_COPYCAT_BOT_ID = 3002


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(bot_registry, "_primary_bot_id", lambda: PRIMARY_BOT_ID)
    bot_registry.clear()
    bot_registry.register_bot(MIRROR_BOT_ID, Path("mirror.jpg"), brand_config={"token": "m"})
    bot_registry.register_bot(
        COPYCAT_BOT_ID,
        Path("shuka.jpg"),
        brand_config={"token": "s", "copycat": True, "name": "Shuka"},
    )
    bot_registry.register_bot(
        OTHER_COPYCAT_BOT_ID,
        Path("other.jpg"),
        brand_config={"token": "o", "copycat": True, "name": "Other"},
    )
    try:
        yield bot_registry
    finally:
        bot_registry.clear()


def _user(bot_id):
    return SimpleNamespace(telegram_id=1, bot_id=bot_id)


LEGACY = _user(None)
PRIMARY = _user(PRIMARY_BOT_ID)
MIRROR = _user(MIRROR_BOT_ID)
COPYCAT = _user(COPYCAT_BOT_ID)
OTHER_COPYCAT = _user(OTHER_COPYCAT_BOT_ID)


def test_leto_scope_keeps_primary_and_plain_mirrors_only(registry):
    in_scope = admin_messages._user_in_bot_scope
    assert in_scope(LEGACY, "leto")
    assert in_scope(PRIMARY, "leto")
    assert in_scope(MIRROR, "leto")
    assert not in_scope(COPYCAT, "leto")
    assert not in_scope(OTHER_COPYCAT, "leto")


def test_default_scope_is_leto(registry):
    assert admin_messages.DEFAULT_BOT_SCOPE == "leto"
    assert admin_messages._user_in_bot_scope(MIRROR, None)
    assert not admin_messages._user_in_bot_scope(COPYCAT, None)


def test_all_scope_accepts_everyone(registry):
    for user in (LEGACY, PRIMARY, MIRROR, COPYCAT, OTHER_COPYCAT):
        assert admin_messages._user_in_bot_scope(user, "all")


def test_copycat_scope_matches_only_that_bot(registry):
    scope = f"copycat:{COPYCAT_BOT_ID}"
    assert admin_messages._user_in_bot_scope(COPYCAT, scope)
    assert not admin_messages._user_in_bot_scope(OTHER_COPYCAT, scope)
    assert not admin_messages._user_in_bot_scope(MIRROR, scope)
    assert not admin_messages._user_in_bot_scope(LEGACY, scope)
    assert not admin_messages._user_in_bot_scope(PRIMARY, scope)


def test_unknown_scope_matches_nobody(registry):
    assert not admin_messages._user_in_bot_scope(PRIMARY, "copycat:abc")
    assert not admin_messages._user_in_bot_scope(PRIMARY, "weird")


def test_filter_by_bot_scope_keeps_order(registry):
    users = [COPYCAT, PRIMARY, MIRROR, OTHER_COPYCAT, LEGACY]
    assert admin_messages._filter_by_bot_scope(users, "leto") == [PRIMARY, MIRROR, LEGACY]
    assert admin_messages._filter_by_bot_scope(users, "all") == users
    assert admin_messages._filter_by_bot_scope(users, f"copycat:{OTHER_COPYCAT_BOT_ID}") == [OTHER_COPYCAT]


def test_leto_scope_without_copycats_keeps_everyone(monkeypatch):
    monkeypatch.setattr(bot_registry, "_primary_bot_id", lambda: PRIMARY_BOT_ID)
    bot_registry.clear()
    try:
        assert admin_messages._user_in_bot_scope(_user(999), "leto")
        assert admin_messages._user_in_bot_scope(LEGACY, "leto")
    finally:
        bot_registry.clear()


@pytest.mark.parametrize(
    "scope, valid",
    [
        ("leto", True),
        ("all", True),
        ("copycat:3001", True),
        ("copycat:", False),
        ("copycat:x", False),
        ("mirror", False),
        (None, False),
    ],
)
def test_is_valid_bot_scope(scope, valid):
    assert admin_messages.is_valid_bot_scope(scope) is valid


def test_scope_display_name_uses_brand_name(registry):
    assert admin_messages.get_bot_scope_display_name("leto") == "Leto (основной + зеркала)"
    assert admin_messages.get_bot_scope_display_name(None) == "Leto (основной + зеркала)"
    assert admin_messages.get_bot_scope_display_name("all") == "Все боты"
    assert admin_messages.get_bot_scope_display_name(f"copycat:{COPYCAT_BOT_ID}") == "Shuka (копикет)"


def test_scope_keyboard_lists_leto_every_copycat_and_all(registry):
    keyboard = admin_keyboards.get_broadcast_bot_scope_keyboard("ru", back_callback="admin_msg_custom")
    rows = keyboard.inline_keyboard
    callbacks = [button.callback_data for row in rows for button in row]
    assert callbacks == [
        "broadcast_scope:leto",
        f"broadcast_scope:copycat:{COPYCAT_BOT_ID}",
        f"broadcast_scope:copycat:{OTHER_COPYCAT_BOT_ID}",
        "broadcast_scope:all",
        "admin_msg_custom",
    ]
    texts = [button.text for row in rows for button in row]
    assert "🎭 Shuka" in texts
    assert "🎭 Other" in texts


def test_schema_bot_scope_defaults_to_leto():
    request = BroadcastCreateRequest(target="all", message_text="hi")
    assert request.bot_scope == "leto"
    assert BroadcastCreateRequest(target="all", message_text="hi", bot_scope=None).bot_scope == "leto"


@pytest.mark.parametrize("scope", ["leto", "all", "copycat:3001", " ALL "])
def test_schema_bot_scope_accepts_valid_values(scope):
    request = BroadcastCreateRequest(target="all", message_text="hi", bot_scope=scope)
    assert request.bot_scope == scope.strip().lower()


@pytest.mark.parametrize("scope", ["mirror", "copycat:", "copycat:abc", "leto,all"])
def test_schema_bot_scope_rejects_garbage(scope):
    with pytest.raises(ValueError):
        BroadcastCreateRequest(target="all", message_text="hi", bot_scope=scope)


def test_broadcast_config_has_leto_scope_by_default():
    from app.services.broadcast_service import BroadcastConfig

    config = BroadcastConfig(target="all", message_text="hi", selected_buttons=[])
    assert config.bot_scope == "leto"
