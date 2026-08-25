"""Premium custom emoji support for the primary Leto bot."""

from __future__ import annotations

from aiogram import Bot
from aiogram.client.default import Default
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.utils.premium_text import (
    RESTRICTED_EMOJI_SET_NAME,
    apply_premium_text_emojis,
    build_text_emoji_map,
    compile_text_emoji_pattern,
)


# Public Telegram custom emoji identifiers selected by the bot owner.
CUSTOM_EMOJI = {
    "accept": "6023940002008799618",
    "decline": "6021852682262682598",
    "language_ru": "6021623107670776267",
    "activate": "6021868492037298942",
    "trial": "6023826881160157558",
    "back": "5805509901048356965",
    "android": "5379619462213292306",
    "apple": "5364230401118192204",
    "windows": "5368718083596764561",
    "share_access": "6019076101869934284",
    "home": "6023896773162967617",
    "download_leto": "6021856393114426113",
    "key_happ": "6019290828759898301",
    "download_incy": "5371054275222850286",
    "key_incy": "6019076101869934284",
    "connect": "6023820193896077912",
    "manual_connect": "6021452855167164269",
    "balance": "6030462253445160459",
    "autopay": "5807492110059838726",
    "renew": "6030561664758191905",
    "change_tariff": "5807880911974308745",
    "autopay_connect": "5807642902066634351",
    "payment_universal": "6033067048030968741",
    "payment_wata": "6030410254276106984",
    "reset_device": "6026045953323046442",
    "bind_device": "6019245310696495518",
    "reset_all_devices": "6021770695631969012",
    "promo": "6021617983774791539",
    "language": "6019205852831947754",
    "info": "6021620268697393273",
    "privacy": "6019328362479097179",
    "rules": "6021435576513730578",
    "copy_link": "6021445064096486884",
    "rewards_shop": "6030845476197111640",
    "profile": "6021659919835469581",
    "claims": "6021525053567409034",
    "shop_activate": "6023761060786346622",
    "shop_order": "6021738534916854774",
    "invite": "6021678620123077295",
    "support": "6023911174188308145",
    "subscription": "6021582331251268218",
}


# Exact labels emitted by the current production keyboards. The replacement
# text intentionally omits the old Unicode icon to prevent a duplicated icon.
_EXACT_RULES: dict[str, tuple[str, str]] = {
    "✅ Принимаю правила": ("accept", "Принимаю правила"),
    "❌ Не принимаю": ("decline", "Не принимаю"),
    "✅ Принять": ("accept", "Принять"),
    "❌ Отклонить": ("decline", "Отклонить"),
    "✅ 🇷🇺 Русский": ("language_ru", "Русский"),
    "✨ Активировать": ("activate", "Активировать"),
    "🎁 Активировать": ("activate", "Активировать"),
    "🎁 3 дня бесплатно": ("trial", "3 дня бесплатно"),
    "⬅️ Назад": ("back", "Назад"),
    "⬅️Назад": ("back", "Назад"),
    "◀️ Назад": ("back", "Назад"),
    "🤖 Android": ("android", "Android"),
    "🍎 iPhone/MacOS": ("apple", "iPhone/MacOS"),
    "🍎 iOS": ("apple", "iOS"),
    "💻 Windows": ("windows", "Windows"),
    "🔗 Переслать друзьям": ("share_access", "Переслать друзьям"),
    "🔗 Поделиться доступом": ("share_access", "Поделиться доступом"),
    "🏠 Основное меню": ("home", "Основное меню"),
    "🏠 Главное меню": ("home", "Главное меню"),
    "🏠 В главное меню": ("home", "В главное меню"),
    "🏠 На главную": ("home", "На главную"),
    "☀️ Скачать Leto VPN": ("download_leto", "Скачать Leto VPN"),
    "☀️ Скачать Leto App": ("download_leto", "Скачать Leto App"),
    "➡️ Ключ в Happ": ("key_incy", "Ключ в Happ"),
    "🛠 Передать ключ в Happ": ("key_incy", "Передать ключ в Happ"),
    "🍏 Скачать Incy": ("download_incy", "Скачать Incy"),
    "🍏 Скачать Incy (RU App Store)": ("download_incy", "Скачать Incy (RU App Store)"),
    "➡️ Ключ в Incy": ("key_incy", "Ключ в Incy"),
    "🛠 Передать ключ в Incy": ("key_incy", "Передать ключ в Incy"),
    "🍎 Скачать Happ": ("download_incy", "Скачать Happ"),
    "🍎 Скачать Happ (Int. App Store)": ("download_incy", "Скачать Happ (Int. App Store)"),
    "💻 Скачать Happ": ("windows", "Скачать Happ"),
    "🤖 Скачать Happ": ("android", "Скачать Happ"),
    "🚀 Подключиться": ("connect", "Подключиться"),
    "🔗 Подключиться": ("connect", "Подключиться"),
    "✅ Я подключился": ("activate", "Я подключился"),
    "🔗 Ручное подключение": ("manual_connect", "Ручное подключение"),
    "💬 Поддержка": ("support", "Поддержка"),
    "🔄 Автоплатеж": ("autopay", "Автоплатеж"),
    "🔄 Автоплатёж": ("autopay", "Автоплатёж"),
    "➕ Продлить": ("renew", "Продлить"),
    "➕ Продлить подписку": ("renew", "Продлить подписку"),
    "⏰ Продлить подписку": ("renew", "Продлить подписку"),
    "💳 Пополнить баланс": ("balance", "Пополнить баланс"),
    "📱 Моя подписка": ("subscription", "Моя подписка"),
    "🔀 Сменить тариф": ("change_tariff", "Сменить тариф"),
    "➕ Подключить автоплатеж": ("autopay_connect", "Подключить автоплатеж"),
    "➕ Подключить автоплатёж": ("autopay_connect", "Подключить автоплатёж"),
    "💳 Карта / СБП / Крипто": ("payment_universal", "Карта / СБП / Крипто"),
    "💳 Карта (WATA)": ("payment_wata", "Карта (WATA)"),
    "💳 Банковская карта (WATA)": ("payment_wata", "Банковская карта (WATA)"),
    "💳 WATA": ("payment_wata", "WATA"),
    "📲 Привязать устройство": ("bind_device", "Привязать устройство"),
    "🔄 Сбросить все устройства": ("reset_all_devices", "Сбросить все устройства"),
    "🔄 Сбросить устройства": ("reset_all_devices", "Сбросить устройства"),
    "✅ Да, сбросить": ("accept", "Да, сбросить"),
    "✅ Да, сбросить все устройства": ("accept", "Да, сбросить все устройства"),
    "❌ Нет": ("decline", "Нет"),
    "🎫 Ввести промокод": ("promo", "Ввести промокод"),
    "🌐 Язык/Language": ("language", "Язык/Language"),
    "ℹ️ Инфо": ("info", "Инфо"),
    "🛡️ Политика конф.": ("privacy", "Политика конф."),
    "🛡️ Политика конфиденциальности": ("privacy", "Политика конфиденциальности"),
    "📋 Правила сервиса": ("rules", "Правила сервиса"),
    "📤 Поделиться ссылкой": ("manual_connect", "Поделиться ссылкой"),
    "📋 Скопировать ссылку": ("copy_link", "Скопировать ссылку"),
    "🎁 Магазин наград": ("rewards_shop", "Магазин наград"),
    "👤 В Профиль": ("profile", "В Профиль"),
    "📦 Мои заявки": ("claims", "Мои заявки"),
    "⚡ Активировать подписку": ("shop_activate", "Активировать подписку"),
    "✅ Активировать подписку": ("shop_activate", "Активировать подписку"),
    "✅ Оформить заявку": ("shop_order", "Оформить заявку"),
    "👥 Пригласить друзей": ("invite", "Пригласить друзей"),
    "🎁 В магазин": ("rewards_shop", "В магазин"),
    "💬 Связаться с поддержкой": ("support", "Связаться с поддержкой"),
}


def _rule_for_button(button: InlineKeyboardButton) -> tuple[str, str] | None:
    exact = _EXACT_RULES.get(button.text)
    if exact is not None:
        return exact

    text = button.text
    callback = button.callback_data or ""

    if text.startswith("💰 Баланс:"):
        return "balance", text.removeprefix("💰 ")
    if text.startswith("💰 Оплатить с баланса"):
        return "balance", text.removeprefix("💰 ")
    if callback.startswith("reset_device_") and text.startswith("🔄 "):
        return "reset_device", text.removeprefix("🔄 ")
    if text.startswith("✅ Да, сбросить"):
        return "accept", text.removeprefix("✅ ")

    return None


def apply_premium_button_icons(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Return a copy of an inline keyboard with configured premium icons."""
    changed = False
    rows: list[list[InlineKeyboardButton]] = []

    for row in markup.inline_keyboard:
        new_row: list[InlineKeyboardButton] = []
        for button in row:
            if button.icon_custom_emoji_id:
                new_row.append(button)
                continue

            rule = _rule_for_button(button)
            if rule is None:
                new_row.append(button)
                continue

            emoji_key, text = rule
            new_row.append(
                button.model_copy(
                    update={
                        "text": text,
                        "icon_custom_emoji_id": CUSTOM_EMOJI[emoji_key],
                    }
                )
            )
            changed = True
        rows.append(new_row)

    if not changed:
        return markup
    return markup.model_copy(update={"inline_keyboard": rows})


class PremiumEmojiBot(Bot):
    """Primary bot client that decorates outgoing keyboards and message text."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._text_emoji_map: dict[str, str] = {}
        self._text_emoji_pattern = None

    async def load_text_emoji_set(
        self,
        name: str = RESTRICTED_EMOJI_SET_NAME,
    ) -> tuple[int, int]:
        sticker_set = await self.get_sticker_set(name=name)
        self._text_emoji_map = build_text_emoji_map(sticker_set.stickers)
        self._text_emoji_pattern = compile_text_emoji_pattern(self._text_emoji_map)
        return len(sticker_set.stickers), len(self._text_emoji_map)

    def _is_html_parse_mode(self, parse_mode) -> bool:
        if isinstance(parse_mode, Default):
            parse_mode = self.default.parse_mode
        return parse_mode in {ParseMode.HTML, ParseMode.HTML.value}

    def _enhance_media_caption(self, media):
        caption = getattr(media, "caption", None)
        entities = getattr(media, "caption_entities", None)
        parse_mode = getattr(media, "parse_mode", None)

        if (
            not isinstance(caption, str)
            or entities
            or not self._is_html_parse_mode(parse_mode)
        ):
            return media

        enhanced_caption = apply_premium_text_emojis(
            caption,
            self._text_emoji_map,
            self._text_emoji_pattern,
        )
        if enhanced_caption == caption:
            return media
        return media.model_copy(update={"caption": enhanced_caption})

    def _prepare_method(self, method):
        updates = {}

        markup = getattr(method, "reply_markup", None)
        if isinstance(markup, InlineKeyboardMarkup):
            enhanced = apply_premium_button_icons(markup)
            if enhanced is not markup:
                updates["reply_markup"] = enhanced

        if self._text_emoji_map:
            if hasattr(method, "parse_mode") and self._is_html_parse_mode(
                method.parse_mode
            ):
                for field_name, entities_name in (
                    ("text", "entities"),
                    ("caption", "caption_entities"),
                ):
                    value = getattr(method, field_name, None)
                    entities = getattr(method, entities_name, None)
                    if not isinstance(value, str) or entities:
                        continue

                    enhanced_text = apply_premium_text_emojis(
                        value,
                        self._text_emoji_map,
                        self._text_emoji_pattern,
                    )
                    if enhanced_text != value:
                        updates[field_name] = enhanced_text

            # EditMessageMedia and SendMediaGroup keep captions inside one or
            # more InputMedia objects rather than on the Telegram method.
            media = getattr(method, "media", None)
            if isinstance(media, (list, tuple)):
                enhanced_media = [self._enhance_media_caption(item) for item in media]
                if any(new is not old for new, old in zip(enhanced_media, media)):
                    updates["media"] = enhanced_media
            elif media is not None:
                enhanced_media = self._enhance_media_caption(media)
                if enhanced_media is not media:
                    updates["media"] = enhanced_media

        if updates:
            return method.model_copy(update=updates)
        return method

    async def __call__(self, method, request_timeout=None):
        method = self._prepare_method(method)
        return await super().__call__(method, request_timeout=request_timeout)
