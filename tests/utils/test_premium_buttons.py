from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.utils.premium_buttons import CUSTOM_EMOJI, apply_premium_button_icons


def _markup(*buttons: InlineKeyboardButton) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button] for button in buttons])


def test_applies_icons_and_removes_old_unicode_prefixes():
    markup = _markup(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="back"),
        InlineKeyboardButton(text="💰 Баланс: 123 ₽  →", callback_data="balance_topup"),
        InlineKeyboardButton(text="💳 Карта / СБП / Крипто", callback_data="pay:auto:30"),
    )

    result = apply_premium_button_icons(markup)
    buttons = [row[0] for row in result.inline_keyboard]

    assert [(button.text, button.icon_custom_emoji_id) for button in buttons] == [
        ("Назад", CUSTOM_EMOJI["back"]),
        ("Баланс: 123 ₽  →", CUSTOM_EMOJI["balance"]),
        ("Карта / СБП / Крипто", CUSTOM_EMOJI["payment_universal"]),
    ]


def test_dynamic_device_button_uses_reset_icon():
    markup = _markup(
        InlineKeyboardButton(
            text="🔄 iPhone · 22 августа",
            callback_data="reset_device_1_1",
        )
    )

    button = apply_premium_button_icons(markup).inline_keyboard[0][0]

    assert button.text == "iPhone · 22 августа"
    assert button.icon_custom_emoji_id == CUSTOM_EMOJI["reset_device"]


def test_current_wata_and_trial_variants_are_supported():
    markup = _markup(
        InlineKeyboardButton(text="💳 Банковская карта (WATA)", callback_data="pay:wata:30"),
        InlineKeyboardButton(text="🎁 Активировать", callback_data="trial_activate"),
        InlineKeyboardButton(text="🔄 Сбросить устройства", callback_data="sub_reset_devices"),
    )

    result = apply_premium_button_icons(markup)
    buttons = [row[0] for row in result.inline_keyboard]

    assert [(button.text, button.icon_custom_emoji_id) for button in buttons] == [
        ("Банковская карта (WATA)", CUSTOM_EMOJI["payment_wata"]),
        ("Активировать", CUSTOM_EMOJI["activate"]),
        ("Сбросить устройства", CUSTOM_EMOJI["reset_all_devices"]),
    ]


def test_ios_happ_download_uses_same_icon_as_incy_download():
    markup = _markup(
        InlineKeyboardButton(text="🍏 Скачать Incy", url="https://example.com/incy"),
        InlineKeyboardButton(text="🍎 Скачать Happ", url="https://example.com/happ"),
    )

    result = apply_premium_button_icons(markup)
    icons = [row[0].icon_custom_emoji_id for row in result.inline_keyboard]

    assert icons == [CUSTOM_EMOJI["download_incy"], CUSTOM_EMOJI["download_incy"]]


def test_leaves_unmapped_and_already_decorated_buttons_unchanged():
    existing_id = "6030597532030081221"
    markup = _markup(
        InlineKeyboardButton(text="⭐️ Telegram Stars", callback_data="pay:stars:30"),
        InlineKeyboardButton(
            text="Подключить устройство",
            callback_data="howto",
            icon_custom_emoji_id=existing_id,
        ),
    )

    result = apply_premium_button_icons(markup)

    assert result is markup
    assert result.inline_keyboard[0][0].icon_custom_emoji_id is None
    assert result.inline_keyboard[1][0].icon_custom_emoji_id == existing_id
