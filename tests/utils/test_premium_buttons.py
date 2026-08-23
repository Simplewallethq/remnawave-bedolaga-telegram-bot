from aiogram.methods import EditMessageMedia
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from app.utils.premium_buttons import (
    CUSTOM_EMOJI,
    PremiumEmojiBot,
    apply_premium_button_icons,
)
from app.utils.premium_text import compile_text_emoji_pattern


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


def test_happ_key_uses_same_icon_as_incy_key_on_every_platform():
    markup = _markup(
        InlineKeyboardButton(text="➡️ Ключ в Incy", url="https://example.com/incy"),
        InlineKeyboardButton(text="➡️ Ключ в Happ", url="https://example.com/happ"),
        InlineKeyboardButton(text="🛠 Передать ключ в Happ", url="https://example.com/happ-transfer"),
    )

    result = apply_premium_button_icons(markup)
    icons = [row[0].icon_custom_emoji_id for row in result.inline_keyboard]

    assert icons == [CUSTOM_EMOJI["key_incy"]] * 3


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


def test_transforms_caption_nested_inside_edit_message_media():
    bot = PremiumEmojiBot(token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    bot._text_emoji_map = {"☀️": "123456789"}
    bot._text_emoji_pattern = compile_text_emoji_pattern(bot._text_emoji_map)
    method = EditMessageMedia(
        chat_id=1,
        message_id=2,
        media=InputMediaPhoto(
            media="existing-photo-file-id",
            caption="☀️ <b>Подписка:</b>",
            parse_mode="HTML",
        ),
    )

    result = bot._prepare_method(method)

    assert result.media.caption == (
        '<tg-emoji emoji-id="123456789">☀️</tg-emoji> <b>Подписка:</b>'
    )
    assert method.media.caption == "☀️ <b>Подписка:</b>"
