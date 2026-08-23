from app.handlers.menu import (
    MAIN_MENU_TEXT_CUSTOM_EMOJI_IDS,
    _decorate_main_menu_text,
)


def test_active_subscription_label_and_emojis_are_decorated():
    source = (
        "Подписка: 🟢Активна\nдо 23.09.2026 (30 дн.)\n\n"
        '<a href="https://t.me/vpnleto">➡️</a> '
        '<a href="https://t.me/vpnleto">Подпишись на наш канал</a>'
    )

    result = _decorate_main_menu_text(source, "active", use_premium_emoji=True)

    assert "<b>Подписка:</b>" in result
    assert f'emoji-id="{MAIN_MENU_TEXT_CUSTOM_EMOJI_IDS["active"]}"' in result
    assert f'emoji-id="{MAIN_MENU_TEXT_CUSTOM_EMOJI_IDS["channel"]}"' in result
    assert '<a href="https://t.me/vpnleto">➡️</a>' not in result


def test_inactive_subscription_uses_red_custom_emoji():
    result = _decorate_main_menu_text(
        "Подписка: 🔴Истекла",
        "inactive",
        use_premium_emoji=True,
    )

    assert result.startswith("<b>Подписка:</b>")
    assert f'emoji-id="{MAIN_MENU_TEXT_CUSTOM_EMOJI_IDS["inactive"]}"' in result


def test_trial_available_text_does_not_gain_subscription_label():
    result = _decorate_main_menu_text(
        "Вам доступно 3 дня бесплатно 🎁",
        "available",
        use_premium_emoji=True,
    )

    assert "<b>Подписка:</b>" not in result


def test_mirror_bot_keeps_standard_emoji_but_bolds_subscription_label():
    result = _decorate_main_menu_text(
        "Подписка: 🟢Активна",
        "active",
        use_premium_emoji=False,
    )

    assert result == "<b>Подписка:</b> 🟢Активна"
