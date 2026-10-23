"""Бот-конструктор: главный экран и мастер создания (токен → поддержка → обложка → запуск)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.constructor import screens, service
from app.constructor.handlers import render
from app.constructor.ui import Screen, new_session
from app.database.models import CopycatBot

from .conftest import TOKEN, needs_sqlite, png_bytes

pytestmark = [pytest.mark.anyio, needs_sqlite]

APP_URL = "https://cats.example/"


class FakeUI:
    def __init__(self):
        self.screens: list[Screen] = []

    async def show(self, uid, session, screen, *, fresh=False):
        self.screens.append(screen)

    @property
    def last(self) -> Screen:
        return self.screens[-1]


def _buttons(screen: Screen) -> list:
    return [button for row in screen.markup().inline_keyboard for button in row]


@pytest.fixture
def ctx(db, telegram, monkeypatch):
    monkeypatch.setattr(settings, "CONSTRUCTOR_WEBAPP_URL", APP_URL, raising=False)
    monkeypatch.setattr(settings, "CONSTRUCTOR_EXAMPLE_BOT_URL", "https://t.me/dostupoffbot", raising=False)

    async def download(file, destination):
        destination.write(png_bytes())

    bot = SimpleNamespace(download=AsyncMock(side_effect=download))
    tg = service.TgUser(id=1, username="owner_name")
    return screens.Ctx(bot=bot, ui=FakeUI(), db=db, tg=tg, session=new_session())


def _message(text=None, photo=None):
    return SimpleNamespace(text=text, caption=None, photo=photo, document=None)


async def _route(ctx, route, **kwargs):
    await render(ctx.ui, ctx, route, **kwargs)
    return ctx.session["route"]


async def test_newcomer_main_screen_offers_app_and_chat_creation(ctx):
    await _route(ctx, "main")
    buttons = _buttons(ctx.ui.last)
    assert buttons[0].web_app.url == APP_URL  # первым — приложение
    assert any(b.callback_data == "a:new" for b in buttons)
    assert any(b.url == "https://t.me/dostupoffbot" for b in buttons)


async def test_without_app_url_chat_creation_is_the_main_action(ctx, monkeypatch):
    monkeypatch.setattr(settings, "CONSTRUCTOR_WEBAPP_URL", "", raising=False)
    await _route(ctx, "main")
    buttons = _buttons(ctx.ui.last)
    assert not any(b.web_app for b in buttons)
    assert buttons[0].callback_data == "a:new" and buttons[0].style == "success"


async def test_wizard_creates_bot(ctx, telegram, db):
    telegram(TOKEN, 777000, "My_Vpn_Bot", "My VPN")
    assert await screens.get_action("new")(ctx, "") == "wiz:tok"
    assert await _route(ctx, "wiz:tok") == "wiz:tok"

    # шаги дальше нельзя открыть раньше времени
    assert await _route(ctx, "wiz:img") == "wiz:tok"

    with pytest.raises(screens.InputError):
        await screens.token_input(ctx, _message("какой-то текст"))
    route = await screens.token_input(ctx, _message(f"Use this token:\n{TOKEN}"))
    assert route == "wiz:sup" and ctx.draft["username"] == "My_Vpn_Bot"

    await _route(ctx, "wiz:sup")
    assert any(b.callback_data == "a:supme" for b in _buttons(ctx.ui.last))  # «Мой аккаунт @owner_name»
    with pytest.raises(screens.InputError):
        await screens.support_input(ctx, _message("ab"))
    assert await screens.support_input(ctx, _message("@help_desk")) == "wiz:img"

    await _route(ctx, "wiz:img")
    assert ctx.ui.last.cover == f"file:{service.DEFAULT_COVER}"

    route = await screens.get_action("launch")(ctx, "")
    row = (await db.execute(select(CopycatBot))).scalar_one()
    assert route == f"done:{row.id}" and ctx.session["draft"] is None
    assert (row.username, row.support, row.logo) == ("My_Vpn_Bot", "@help_desk", service.DEFAULT_COVER)

    await _route(ctx, route)
    buttons = _buttons(ctx.ui.last)
    assert buttons[0].url == "https://t.me/My_Vpn_Bot"
    assert buttons[1].web_app.url == f"{APP_URL}?bot={row.id}"  # управление — в приложении

    # вернувшийся владелец видит сводку и снова кнопку приложения
    await _route(ctx, "main")
    assert "С возвращением" in ctx.ui.last.body
    assert _buttons(ctx.ui.last)[0].web_app.url == APP_URL


async def test_picture_launches_bot(ctx, telegram, db):
    telegram(TOKEN, 777000, "My_Vpn_Bot")
    ctx.session["draft"] = {}
    await screens.token_input(ctx, _message(TOKEN))
    await screens.support_input(ctx, _message("help_desk"))

    with pytest.raises(screens.InputError):
        await screens.image_input(ctx, _message("не картинка"))
    route = await screens.image_input(ctx, _message(photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")]))
    row = (await db.execute(select(CopycatBot))).scalar_one()
    assert route == f"done:{row.id}"
    assert row.logo.startswith(settings.OWNER_BOT_IMAGES_DIR) and row.logo.endswith(".jpg")
    assert ctx.bot.download.await_args.args[0].file_id == "big"


async def test_foreign_webhook_asks_for_confirmation(ctx, telegram):
    telegram(TOKEN, 777000, "My_Vpn_Bot", webhook="https://other.service/hook")
    ctx.session["draft"] = {}
    assert await screens.token_input(ctx, _message(TOKEN)) == "wiz:tok"
    await _route(ctx, "wiz:tok")
    assert "другому сервису" in ctx.ui.last.body
    assert await screens.get_action("tokok")(ctx, "") == "wiz:sup"


async def test_own_disabled_bot_comes_back_right_after_token(ctx, telegram, db):
    from app.services import owner_platform_service as platform

    telegram(TOKEN, 777000, "My_Vpn_Bot")
    user = await service.ensure_owner_account(db, ctx.tg)
    row, _ = await service.launch_bot(db, user, await service.check_token(db, user.id, TOKEN), support="help_desk", logo=None)
    await platform.disable_bot(db, row)

    ctx.session["draft"] = {}
    assert await screens.token_input(ctx, _message(TOKEN)) == f"done:{row.id}"
    await db.refresh(row)
    assert row.enabled
    await _route(ctx, f"done:{row.id}")
    assert "снова подключён" in ctx.ui.last.body


async def test_taken_bot_is_reported_on_launch(ctx, telegram, db):
    """Пока шёл мастер, бота подключил кто-то другой — мастер начинается заново с ошибкой."""
    telegram(TOKEN, 777000, "My_Vpn_Bot")
    ctx.session["draft"] = {}
    await screens.token_input(ctx, _message(TOKEN))
    await screens.support_input(ctx, _message("help_desk"))

    other = await service.ensure_owner_account(db, service.TgUser(id=2, username="other"))
    await service.launch_bot(db, other, await service.check_token(db, other.id, TOKEN), support="x_support", logo=None)

    with pytest.raises(screens.Redirect) as redirect:
        await screens.get_action("launch")(ctx, "")
    assert redirect.value.route == "wiz:tok" and "уже подключён" in redirect.value.error


async def test_ui_keeps_one_message_and_falls_back_to_plain_emoji(tmp_path):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.methods import EditMessageText

    from app.constructor.ui import UI, Btn, Sessions

    bot = SimpleNamespace(
        send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        edit_message_text=AsyncMock(),
        delete_message=AsyncMock(),
    )
    ui = UI(bot, Sessions(MemoryStorage(), bot_id=555))
    session = await ui.sessions.load(1)
    screen = Screen('<h2><tg-emoji emoji-id="1">🛡</tg-emoji> VPN</h2>', [[Btn("Создать", "a:new", icon="robot")]])

    await ui.show(1, session, screen)
    rich = bot.send_rich_message.await_args.kwargs["rich_message"]
    assert rich.html.startswith('<img src="tg://photo?id=cover"/>') and session["msg_id"] == 10

    # следующий экран — правка того же сообщения
    await ui.show(1, session, screen)
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 10
    bot.send_rich_message.assert_awaited_once()

    # У владельца бота нет Premium: Telegram отклоняет премиум-эмодзи в любом запросе —
    # экран показывается обычными символами в том же сообщении.
    def reject_premium(result):
        async def call(**kwargs):
            if "tg-emoji" in kwargs["rich_message"].html:
                raise TelegramBadRequest(method=EditMessageText(text="x"), message="Bad Request: DOCUMENT_INVALID")
            return result
        return call

    bot.edit_message_text.side_effect = reject_premium(SimpleNamespace(message_id=10))
    bot.send_rich_message.side_effect = reject_premium(SimpleNamespace(message_id=11))
    await ui.show(1, session, screen)
    plain = bot.edit_message_text.await_args.kwargs
    assert "tg-emoji" not in plain["rich_message"].html and plain["message_id"] == 10
    assert plain["reply_markup"].inline_keyboard[0][0].icon_custom_emoji_id is None
    assert session["msg_id"] == 10

    # /start: новое сообщение внизу, старое удаляется
    bot.send_rich_message.side_effect = None
    bot.send_rich_message.return_value = SimpleNamespace(message_id=11)
    await ui.show(1, session, screen, fresh=True)
    assert session["msg_id"] == 11
    bot.delete_message.assert_awaited_with(chat_id=1, message_id=10)
