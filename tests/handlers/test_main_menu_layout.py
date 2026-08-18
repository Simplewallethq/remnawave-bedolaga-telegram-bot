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


def test_tariff_cards_describe_access_to_all_services_without_bypass_wording():
    expected_wording = {
        "ru": "полный VPN, доступ ко всем сервисам",
        "en": "full VPN, access to all services",
        "ua": "повний VPN, доступ до всіх сервісів",
        "zh": "完整 VPN，可访问所有服务",
    }

    for language, wording in expected_wording.items():
        texts = get_texts(language)
        for tariff_key in ("TARIFF_CARD_SOLO", "TARIFF_CARD_PLUS", "TARIFF_CARD_PRO"):
            card = texts.t(tariff_key)
            assert wording in card
            assert "обход" not in card.lower()
            assert "bypass" not in card.lower()


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
