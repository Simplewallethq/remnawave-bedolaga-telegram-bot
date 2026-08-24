from app.keyboards import inline
from app.localization.texts import get_texts


def test_active_subscriber_main_menu_places_devices_between_connect_and_subscription():
    keyboard = inline.get_new_main_menu_keyboard(
        balance_rub=0,
        has_active_subscription=True,
        language="ru",
    )

    assert [
        (row[0].text, row[0].callback_data)
        for row in keyboard.inline_keyboard[:4]
    ] == [
        ("🚀 Подключить устройство", "howto"),
        ("📱 Мои устройства", "subscription_manage_devices"),
        ("⚙️ Управление Подпиской", "subscription"),
        ("💲 Приглашай и зарабатывай", "referral"),
    ]


def test_primary_bot_main_menu_uses_custom_emoji_without_duplicate_unicode_icons():
    keyboard = inline.get_new_main_menu_keyboard(
        balance_rub=0,
        has_active_subscription=True,
        is_admin=True,
        language="ru",
        use_premium_emoji=True,
    )

    buttons = {
        button.callback_data: button
        for row in keyboard.inline_keyboard
        for button in row
    }

    expected = {
        "howto": ("Подключить устройство", "connect"),
        "subscription_manage_devices": ("Мои устройства", "devices"),
        "subscription": ("Управление Подпиской", "subscription"),
        "referral": ("Приглашай и зарабатывай", "referral"),
        "support": ("Поддержка", "support"),
        "profile": ("Профиль", "profile"),
        "admin_panel": ("Админ-панель", "admin"),
    }

    for callback_data, (text, emoji_key) in expected.items():
        button = buttons[callback_data]
        assert button.text == text
        assert button.icon_custom_emoji_id == inline.MAIN_MENU_CUSTOM_EMOJI_IDS[emoji_key]


def test_inactive_subscriber_main_menu_starts_the_extend_flow():
    keyboard = inline.get_new_main_menu_keyboard(
        balance_rub=0,
        has_active_subscription=False,
        language="ru",
    )

    assert ("✅ Активировать подписку", "sub_add_days") in [
        (button.text, button.callback_data)
        for row in keyboard.inline_keyboard
        for button in row
    ]


def test_non_paid_users_do_not_see_device_management():
    for kwargs in ({}, {"trial_active": True}):
        keyboard = inline.get_new_main_menu_keyboard(
            balance_rub=0,
            language="ru",
            **kwargs,
        )

        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        assert "subscription_manage_devices" not in callbacks


def test_tariff_cards_describe_all_bypasses():
    expected_wording = {
        "ru": "полный VPN, все обходы",
        "en": "full VPN, all bypasses",
        "ua": "повний VPN, усі обходи",
        "zh": "完整 VPN，解锁所有限制",
    }

    for language, wording in expected_wording.items():
        texts = get_texts(language)
        for tariff_key in ("TARIFF_CARD_SOLO", "TARIFF_CARD_PLUS", "TARIFF_CARD_PRO"):
            card = texts.t(tariff_key)
            assert wording in card
            assert "доступ ко всем сервисам" not in card
            assert "access to all services" not in card


def test_youtube_perk_sits_above_traffic_on_plus_and_pro_only():
    expected_line = {
        "ru": "Youtube без рекламы",
        "en": "YouTube without ads",
        "ua": "Youtube без реклами",
        "zh": "YouTube 无广告",
    }

    for language, line in expected_line.items():
        texts = get_texts(language)

        for tariff_key in ("TARIFF_CARD_PLUS", "TARIFF_CARD_PRO"):
            card = texts.t(tariff_key)
            assert line in card
            assert card.index(line) < card.index(chr(9854))

        for tariff_key in ("TARIFF_CARD_SOLO", "TARIFF_CARD_APP"):
            assert line not in texts.t(tariff_key)


def test_main_menu_labels_are_localized_for_every_supported_language():
    expected_labels = {
        "ru": ("🚀 Подключить устройство", "💲 Приглашай и зарабатывай"),
        "en": ("🚀 Connect device", "💲 Invite and earn"),
        "ua": ("🚀 Підключити пристрій", "💲 Запрошуй і заробляй"),
        "zh": ("🚀 连接设备", "💲邀请好友赚奖励"),
    }

    for language, (connect_label, referral_label) in expected_labels.items():
        texts = get_texts(language)

        assert texts.t("MENU_CONNECT_BUTTON") == connect_label
        assert texts.t("MENU_REFERRAL_BUTTON") == referral_label
        assert texts.t("MENU_REFERRALS") == referral_label
