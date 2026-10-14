"""Клавиатуры платформы владельцев витрин (бот OWNER_PLATFORM_BOT_ID)."""

from __future__ import annotations

from typing import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.localization.texts import get_texts


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(row) for row in rows])


def platform_choice_keyboard(language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_CHOICE_CREATE_BUTTON", "🤖 Создать своего VPN бота"), "wl_choice_create")],
        [_btn(texts.t("OWNER_CHOICE_VPN_BUTTON", "🛡 Попробовать тестовый VPN"), "wl_choice_vpn")],
    )


def owner_menu_keyboard(language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_SETTINGS_BUTTON", "⚙️ Настройки"), "wl_settings")],
        [_btn(texts.t("OWNER_EARNINGS_BUTTON", "💰 Заработок"), "wl_earn")],
        [_btn(texts.t("OWNER_TRY_VPN_BUTTON", "🛡 Попробовать тестовый VPN"), "wl_try_vpn")],
    )


def back_keyboard(language: str, data: str, text_key: str = "OWNER_BACK_BUTTON", default: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    return _kb([_btn(get_texts(language).t(text_key, default), data)])


def cancel_keyboard(language: str, data: str = "wl_home") -> InlineKeyboardMarkup:
    return back_keyboard(language, data, "OWNER_CANCEL_BUTTON", "❌ Отмена")


def bot_ready_keyboard(language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_SETTINGS_BUTTON", "⚙️ Настройки"), "wl_settings")],
        [_btn(texts.t("OWNER_HOME_BUTTON", "🏠 На главную"), "wl_menu")],
    )


def settings_keyboard(language: str, bots: Iterable[tuple[int, str]]) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows = [[_btn(f"@{username}", f"wl_bot:{row_id}")] for row_id, username in bots]
    rows.append([_btn(texts.t("OWNER_NEW_BOT_BUTTON", "➕ Создать нового бота"), "wl_new")])
    rows.append([_btn(texts.t("OWNER_HOME_BUTTON", "🏠 На главную"), "wl_menu")])
    return _kb(*rows)


def bot_keyboard(language: str, row_id: int) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_BOT_SUPPORT_BUTTON", "🆘 Саппорт"), f"wl_support:{row_id}")],
        [_btn(texts.t("OWNER_BOT_PRICES_BUTTON", "💳 Цены"), f"wl_prices:{row_id}")],
        [_btn(texts.t("OWNER_BOT_PICTURE_BUTTON", "🖼 Картинка"), f"wl_pic:{row_id}")],
        [_btn(texts.t("OWNER_BOT_DELETE_BUTTON", "🗑 Удалить бота"), f"wl_del:{row_id}")],
        [_btn(texts.t("OWNER_BACK_BUTTON", "⬅️ Назад"), "wl_menu")],
    )


def support_keyboard(language: str, row_id: int) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_SUPPORT_CHANGE_BUTTON", "✏️ Сменить саппорт"), f"wl_support_edit:{row_id}")],
        [_btn(texts.t("OWNER_BACK_BUTTON", "⬅️ Назад"), f"wl_bot:{row_id}")],
    )


def prices_keyboard(language: str, row_id: int) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_PRICES_CHANGE_BUTTON", "✏️ Изменить цены"), f"wl_prices_edit:{row_id}")],
        [_btn(texts.t("OWNER_BACK_BUTTON", "⬅️ Назад"), f"wl_bot:{row_id}")],
    )


def picture_keyboard(language: str, row_id: int) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_PICTURE_CHANGE_BUTTON", "🖼 Сменить картинку"), f"wl_pic_edit:{row_id}")],
        [_btn(texts.t("OWNER_BACK_BUTTON", "⬅️ Назад"), f"wl_bot:{row_id}")],
    )


def delete_keyboard(language: str, row_id: int, username: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_DELETE_BUTTON", "🗑 Удалить @{bot}").format(bot=username), f"wl_del_ask:{row_id}")],
        [_btn(texts.t("OWNER_BACK_BUTTON", "⬅️ Назад"), f"wl_bot:{row_id}")],
    )


def delete_confirm_keyboard(language: str, row_id: int) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_DELETE_CONFIRM_BUTTON", "✅ Да, удалить"), f"wl_del_yes:{row_id}")],
        [_btn(texts.t("OWNER_BACK_BUTTON", "⬅️ Назад"), f"wl_bot:{row_id}")],
    )


def earnings_keyboard(language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_PAYOUT_BUTTON", "💸 Вывести средства"), "wl_payout")],
        [_btn(texts.t("OWNER_WALLET_BUTTON", "👛 Кошелек для вывода"), "wl_wallet")],
        [_btn(texts.t("OWNER_BACK_BUTTON", "⬅️ Назад"), "wl_menu")],
    )


def payout_confirm_keyboard(language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return _kb(
        [_btn(texts.t("OWNER_PAYOUT_CONFIRM_BUTTON", "✅ Подтверждаю"), "wl_payout_confirm")],
        [_btn(texts.t("OWNER_CANCEL_BUTTON", "❌ Отмена"), "wl_earn")],
    )


def wallet_saved_keyboard(language: str) -> InlineKeyboardMarkup:
    return back_keyboard(language, "wl_payout", "OWNER_PAYOUT_BUTTON", "💸 Вывести средства")


def home_keyboard(language: str) -> InlineKeyboardMarkup:
    return back_keyboard(language, "wl_menu", "OWNER_HOME_BUTTON", "🏠 На главную")
