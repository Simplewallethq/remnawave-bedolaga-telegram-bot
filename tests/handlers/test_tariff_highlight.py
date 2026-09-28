from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.enums import ButtonStyle

from app.branding import brand_scope
from app.config import settings
from app.handlers.menu import get_main_menu_text
from app.keyboards import inline
from app.localization.texts import get_texts
from app.utils import bot_registry

COPYCAT_ID = 4102


@pytest.fixture
def copycat():
    bot_registry.clear()
    bot_registry.register_bot(
        COPYCAT_ID,
        Path("bot_images/shuka.jpg"),
        brand_config={
            "token": "x",
            "copycat": True,
            "name": "Shuka",
            "channel_link": "https://t.me/shuka_channel",
        },
    )
    yield COPYCAT_ID
    bot_registry.clear()


def _plan(plan_id, code, name):
    return SimpleNamespace(id=plan_id, code=code, display_name=name)


def _plans_from_db_order():
    # Как на проде: sort_order Solo < Plus < Pro.
    return [
        (_plan(2, "solo", "Solo"), 19000),
        (_plan(3, "plus", "Plus"), 29000),
        (_plan(4, "pro", "Pro"), 39000),
    ]


def test_tariff_buttons_put_highlighted_plus_first():
    keyboard = inline.get_tariffs_keyboard(_plans_from_db_order(), language="ru")
    rows = keyboard.inline_keyboard[:-1]

    assert [(row[0].text, row[0].callback_data) for row in rows] == [
        ("Выбрать Plus · ⭐️ хит", "tariff_select:plus"),
        ("Выбрать Pro", "tariff_select:pro"),
        ("Выбрать Solo", "tariff_select:solo"),
    ]
    assert [row[0].style for row in rows] == [ButtonStyle.SUCCESS, None, None]


def test_current_plan_keeps_its_label_but_stays_in_place():
    keyboard = inline.get_tariffs_keyboard(
        _plans_from_db_order(),
        language="ru",
        current_plan_id=4,
        current_plan_label="✅ Текущий: Pro",
    )

    assert [row[0].text for row in keyboard.inline_keyboard[:-1]] == [
        "Выбрать Plus · ⭐️ хит",
        "✅ Текущий: Pro",
        "Выбрать Solo",
    ]


def test_period_buttons_put_highlighted_three_months_first():
    prices = {30: 29000, 90: 69000, 360: 199000, 720: 299000}

    for keyboard in (
        inline.get_tariff_periods_keyboard("plus", prices, language="ru"),
        inline.get_renew_periods_keyboard(3, prices, language="ru"),
    ):
        rows = keyboard.inline_keyboard[:-1]
        periods = [int(row[0].callback_data.rsplit(":", 1)[1]) for row in rows]

        assert periods == [90, 30, 360, 720]
        assert rows[0][0].text.startswith("3 мес — 690₽")
        assert rows[0][0].text.endswith(" · ⭐️ хит")
        assert [row[0].style for row in rows] == [ButtonStyle.SUCCESS, None, None, None]
        assert "хит" not in "".join(row[0].text for row in rows[1:])


def test_period_title_nudges_towards_longer_periods():
    title = get_texts("ru").t("TARIFF_PERIODS_TITLE_PROMO").format(name="Plus")

    assert title.endswith("Выберите период. Чем дольше — тем выгоднее 👇")


def test_main_menu_ends_with_full_width_web_cabinet_button(monkeypatch):
    monkeypatch.setattr(settings, "CABINET_BASE_URL", "https://lk.letovpn.com")

    keyboard = inline.get_new_main_menu_keyboard(
        balance_rub=0,
        has_active_subscription=True,
        language="ru",
    )
    last_row = keyboard.inline_keyboard[-1]

    assert len(last_row) == 1
    assert last_row[0].text == "Личный Кабинет (WEB)"
    assert last_row[0].url == "https://lk.letovpn.com"


def test_web_cabinet_button_stays_above_admin_panel(monkeypatch):
    monkeypatch.setattr(settings, "CABINET_BASE_URL", "https://lk.letovpn.com")

    keyboard = inline.get_new_main_menu_keyboard(balance_rub=0, is_admin=True, language="ru")

    assert keyboard.inline_keyboard[-2][0].url == "https://lk.letovpn.com"
    assert keyboard.inline_keyboard[-1][0].callback_data == "admin_panel"


def test_web_cabinet_button_hidden_without_cabinet_url(monkeypatch):
    monkeypatch.setattr(settings, "CABINET_BASE_URL", "")

    keyboard = inline.get_new_main_menu_keyboard(balance_rub=0, language="ru")

    assert all(button.url is None for row in keyboard.inline_keyboard for button in row)


async def test_news_channel_link_sits_next_to_legal_links():
    text = await get_main_menu_text(
        SimpleNamespace(subscription=None),
        get_texts("ru"),
        None,
    )
    legal_line = text.strip().splitlines()[-1]

    assert "Политика конфиденциальности</a> | " in legal_line
    assert legal_line.endswith(
        ' | <a href="https://t.me/vpnleto">Новостной канал</a>'
    )


def test_copycat_tariffs_and_periods_stay_as_before(copycat):
    prices = {30: 29000, 90: 69000, 360: 199000, 720: 299000}
    with brand_scope(copycat):
        tariffs = inline.get_tariffs_keyboard(_plans_from_db_order(), language="ru")
        periods = inline.get_tariff_periods_keyboard("plus", prices, language="ru")

    assert [row[0].text for row in tariffs.inline_keyboard[:-1]] == [
        "Выбрать Solo",
        "Выбрать Plus",
        "Выбрать Pro",
    ]
    period_rows = periods.inline_keyboard[:-1]
    assert [int(row[0].callback_data.rsplit(":", 1)[1]) for row in period_rows] == [30, 90, 360, 720]
    buttons = [row[0] for row in tariffs.inline_keyboard + period_rows]
    assert all(button.style is None for button in buttons)
    assert all("хит" not in button.text for button in buttons)


async def test_copycat_menu_keeps_channel_hint_and_has_no_cabinet(copycat, monkeypatch):
    monkeypatch.setattr(settings, "CABINET_BASE_URL", "https://lk.letovpn.com")
    with brand_scope(copycat):
        text = await get_main_menu_text(SimpleNamespace(subscription=None), get_texts("ru"), None)
        keyboard = inline.get_new_main_menu_keyboard(balance_rub=0, language="ru")

    assert "Подпишись на наш канал" in text
    assert "shuka_channel" in text
    assert "Новостной канал" not in text
    assert all(button.url is None for row in keyboard.inline_keyboard for button in row)
