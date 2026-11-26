"""Рассылка «Чёрная пятница»: сегменты, форматирование, медиа в чужих ботах."""

import asyncio
import io
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, MessageEntity, PhotoSize

from app.config import settings
from app.handlers.admin import messages as admin_messages
from app.utils import bot_registry
from app.utils.broadcast_delivery import BroadcastSender, message_html, strip_custom_emoji

PRIMARY_BOT_ID = 123456
COPYCAT_BOT_ID = 3001
NOW = datetime(2026, 11, 26, 12, 0)
CUTOFF = datetime(2026, 6, 9, 12, 0)


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(bot_registry, "_primary_bot_id", lambda: PRIMARY_BOT_ID)
    monkeypatch.setattr(settings, "TARIFFS_NEW_PRICING_CUTOFF", CUTOFF.isoformat())
    bot_registry.clear()
    bot_registry.register_bot(
        COPYCAT_BOT_ID,
        Path("shuka.jpg"),
        brand_config={"token": "s", "copycat": True, "name": "Shuka"},
    )
    try:
        yield bot_registry
    finally:
        bot_registry.clear()


def _sub(*, active=True, trial=False, paid_trial=False, period=30, partner=False, status="active"):
    return SimpleNamespace(
        status=status,
        end_date=NOW + timedelta(days=5) if active else NOW - timedelta(days=5),
        is_trial=trial,
        is_paid_trial=paid_trial,
        is_partner=partner,
        plan_period_days=None if trial else period,
    )


def _user(subscription=None, *, bot_id=PRIMARY_BOT_ID, created=NOW - timedelta(days=30), override=None):
    return SimpleNamespace(
        telegram_id=1,
        bot_id=bot_id,
        created_at=created,
        tariff_pricing_cohort_override=override,
        subscription=subscription,
    )


@pytest.mark.parametrize(
    "subscription, expected",
    [
        (None, "lapsed"),
        (_sub(trial=True), "active"),
        (_sub(paid_trial=True), "active"),
        (_sub(trial=True, active=False), "lapsed"),
        (_sub(period=30), "active"),
        (_sub(period=90), "active"),
        (_sub(period=180), None),
        (_sub(period=360), None),
        (_sub(period=720), None),
        (_sub(period=360, active=False), "lapsed"),
        (_sub(period=30, status="expired"), "lapsed"),
        (_sub(period=30, partner=True), None),
    ],
)
def test_segment_by_subscription(registry, subscription, expected):
    assert admin_messages.black_friday_segment(_user(subscription), NOW) == expected


def test_legacy_leto_users_are_left_out(registry):
    old = _user(_sub(period=30), created=CUTOFF - timedelta(days=1))
    assert admin_messages.black_friday_segment(old, NOW) is None
    forced_new = _user(_sub(period=30), created=CUTOFF - timedelta(days=1), override="new")
    assert admin_messages.black_friday_segment(forced_new, NOW) == "active"


def test_old_copycat_users_stay_in(registry):
    old_copycat = _user(_sub(period=90), bot_id=COPYCAT_BOT_ID, created=CUTOFF - timedelta(days=100))
    assert admin_messages.black_friday_segment(old_copycat, NOW) == "active"


def test_segment_targets_have_names():
    assert "Чёрная пятница" in admin_messages.get_target_name("bf_active")
    assert "Чёрная пятница" in admin_messages.get_target_name("bf_lapsed")


def test_tariffs_button_opens_extension(registry):
    keyboard = admin_messages.create_broadcast_keyboard(["tariffs"], "ru")
    button = keyboard.inline_keyboard[0][0]
    assert button.callback_data == "subscription_extend"


def _u16(text):
    return len(text.encode("utf-16-le")) // 2


def test_message_html_keeps_formatting_and_premium_emoji():
    text = "🎁 Скидки\n\nПереходи"
    entities = [
        MessageEntity(type="custom_emoji", offset=0, length=_u16("🎁"), custom_emoji_id="531"),
        MessageEntity(type="bold", offset=_u16("🎁 "), length=_u16("Скидки")),
        MessageEntity(type="blockquote", offset=_u16("🎁 Скидки\n\n"), length=_u16("Переходи")),
    ]
    message = Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=1, type="private"),
        caption=text,
        caption_entities=entities,
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)],
    )
    assert message_html(message) == (
        '<tg-emoji emoji-id="531">🎁</tg-emoji> <b>Скидки</b>\n\n<blockquote>Переходи</blockquote>'
    )


def test_message_html_without_entities_keeps_typed_tags():
    message = Message(message_id=1, date=NOW, chat=Chat(id=1, type="private"), text="<b>жирно</b>")
    assert message_html(message) == "<b>жирно</b>"


def test_strip_custom_emoji():
    assert strip_custom_emoji('<tg-emoji emoji-id="1">🎁</tg-emoji> <b>x</b>') == "🎁 <b>x</b>"


class FakeBot:
    def __init__(self, bot_id, *, reject_custom_emoji=False, source=b"img"):
        self.id = bot_id
        self.reject_custom_emoji = reject_custom_emoji
        self.source = source
        self.calls = []
        self.downloads = 0

    def _check(self, text):
        if self.reject_custom_emoji and "<tg-emoji" in (text or ""):
            raise TelegramBadRequest(SendMessage(chat_id=1, text="x"), "custom emoji not allowed")

    async def send_message(self, chat_id, text, **kwargs):
        self._check(text)
        self.calls.append(("text", chat_id, text))
        return SimpleNamespace()

    async def send_photo(self, chat_id, photo, caption, **kwargs):
        await asyncio.sleep(0)
        self._check(caption)
        self.calls.append(("photo", chat_id, photo, caption))
        file_id = photo if isinstance(photo, str) else f"{self.id}-uploaded"
        return SimpleNamespace(photo=[SimpleNamespace(file_id=file_id)])

    async def download(self, file_id):
        self.downloads += 1
        if self.source is None:
            raise RuntimeError("no file")
        return io.BytesIO(self.source)


def test_main_bot_sends_media_by_file_id():
    main = FakeBot(1)
    sender = BroadcastSender(main, text="hi", media_type="photo", media_file_id="main-file")
    asyncio.run(sender.send(main, 10))
    assert main.calls == [("photo", 10, "main-file", "hi")]
    assert main.downloads == 0


def test_other_bot_uploads_once_then_reuses_file_id():
    main, copycat = FakeBot(1), FakeBot(2)
    sender = BroadcastSender(main, text="hi", media_type="photo", media_file_id="main-file")

    async def run():
        await asyncio.gather(*(sender.send(copycat, chat_id) for chat_id in range(5)))

    asyncio.run(run())
    uploads = [call for call in copycat.calls if not isinstance(call[2], str)]
    reused = [call for call in copycat.calls if call[2] == "2-uploaded"]
    assert len(uploads) == 1
    assert len(reused) == 4
    assert main.downloads == 1


def test_media_falls_back_to_text_when_source_cannot_be_downloaded():
    main, copycat = FakeBot(1, source=None), FakeBot(2)
    sender = BroadcastSender(main, text="hi", media_type="photo", media_file_id="main-file")
    asyncio.run(sender.send(copycat, 7))
    assert copycat.calls == [("text", 7, "hi")]


def test_bot_without_premium_owner_gets_plain_emoji():
    main, copycat = FakeBot(1), FakeBot(2, reject_custom_emoji=True)
    text = '<tg-emoji emoji-id="1">🎁</tg-emoji> Скидки'
    sender = BroadcastSender(main, text=text)
    asyncio.run(sender.send(copycat, 1))
    asyncio.run(sender.send(copycat, 2))
    asyncio.run(sender.send(main, 3))
    assert copycat.calls == [("text", 1, "🎁 Скидки"), ("text", 2, "🎁 Скидки")]
    assert main.calls == [("text", 3, text)]


def test_other_bad_requests_are_not_swallowed():
    class BrokenBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise TelegramBadRequest(SendMessage(chat_id=1, text="x"), "chat not found")

    bot = BrokenBot(2)
    sender = BroadcastSender(FakeBot(1), text="<tg-emoji emoji-id=\"1\">🎁</tg-emoji>")
    with pytest.raises(TelegramBadRequest):
        asyncio.run(sender.send(bot, 1))
    assert bot.id not in sender._plain_emoji_bots


def test_broadcast_recipients_skip_users_without_telegram(monkeypatch):
    with_tg = SimpleNamespace(telegram_id=5)
    cabinet_only = SimpleNamespace(telegram_id=None)

    async def fake_target_users(db, target, *, bot_scope):
        return [with_tg, cabinet_only]

    monkeypatch.setattr(admin_messages, "get_target_users", fake_target_users)
    recipients = asyncio.run(admin_messages.get_broadcast_recipients(None, "bf_lapsed", bot_scope="leto"))
    assert recipients == [with_tg]


def test_sale_discounts_expire_and_parse(monkeypatch):
    monkeypatch.setattr(settings, "TARIFF_SALE_PERIODS", "360:40,720:50")
    monkeypatch.setattr(settings, "TARIFF_SALE_UNTIL", "2026-11-28T21:00:00")
    assert settings.get_tariff_sale_discounts(datetime(2026, 11, 28, 20, 59)) == {360: 40, 720: 50}
    assert settings.get_tariff_sale_discounts(datetime(2026, 11, 28, 21, 0)) == {}
    monkeypatch.setattr(settings, "TARIFF_SALE_PERIODS", "")
    assert settings.get_tariff_sale_discounts(datetime(2026, 11, 27)) == {}


def test_sale_periods_get_fire_on_buttons():
    from app.keyboards.inline import get_renew_periods_keyboard, get_tariff_periods_keyboard

    prices = {30: 29000, 90: 57000, 360: 86000, 720: 96000}
    for keyboard in (
        get_tariff_periods_keyboard("solo", prices, sale_discounts={360: 40, 720: 50}),
        get_renew_periods_keyboard(1, prices, sale_discounts={360: 40, 720: 50}),
    ):
        labels = {b.callback_data.rsplit(":", 1)[1]: b.text for row in keyboard.inline_keyboard[:-1] for b in row}
        assert labels["360"].startswith("🔥 ")
        assert labels["720"].startswith("🔥 ")
        assert not labels["30"].startswith("🔥")
        assert not labels["90"].startswith("🔥")
    plain = get_tariff_periods_keyboard("solo", prices)
    assert not any(b.text.startswith("🔥") for row in plain.inline_keyboard for b in row)


def test_legacy_users_see_no_sale(registry, monkeypatch):
    from app.handlers.subscription import tariffs

    monkeypatch.setattr(settings, "TARIFF_SALE_UNTIL", "2099-01-01T00:00:00")
    new_user = _user(None)
    legacy_user = _user(None, created=CUTOFF - timedelta(days=1))
    assert tariffs._tariff_sale_discounts(new_user) == {360: 40, 720: 50}
    assert tariffs._tariff_sale_discounts(legacy_user) == {}
