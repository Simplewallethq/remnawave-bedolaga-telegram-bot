"""Подключение бота владельцем: шаги юзернейм → токен → картинка → готово."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import Base, CopycatBot, User
from app.handlers.owner_platform import create
from app.services import owner_platform_service as platform
from app.states import OwnerStates

TOKEN = "777000:AAH" + "x" * 32


def _aiosqlite_available() -> bool:
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not _aiosqlite_available(), reason="настоящий aiosqlite недоступен"),
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _message(text=None, *, photo=None, username="owner_name"):
    return SimpleNamespace(
        text=text,
        photo=photo,
        document=None,
        bot=SimpleNamespace(id=1),
        from_user=SimpleNamespace(id=1, username=username),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )


def _answered(message) -> str:
    return message.answer.await_args.args[0]


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(settings, "BOT_USERNAME", "letovpnbot", raising=False)
    monkeypatch.setattr(settings, "OWNER_MAX_BOTS", 10, raising=False)
    identities = {}

    async def fake_identity(token):
        return identities.get(token)

    monkeypatch.setattr(platform, "fetch_bot_identity", fake_identity)
    monkeypatch.setattr(platform, "refresh_runtime", AsyncMock())
    monkeypatch.setattr(platform, "save_picture", AsyncMock(return_value="data/bot_logos/777000_1.jpg"))
    monkeypatch.setattr(platform, "is_reserved_bot_id", lambda bot_id: False)
    return identities


async def _owner(db) -> User:
    user = User(telegram_id=1, referral_code="owner", username="owner_name")
    db.add(user)
    await db.commit()
    return user


async def test_full_creation(world) -> None:
    db = await _session()
    owner = await _owner(db)
    state = _state()
    world[TOKEN] = SimpleNamespace(id=777000, username="My_Vpn_Bot", full_name="My VPN")

    await state.set_state(OwnerStates.waiting_for_bot_username)
    await create.handle_bot_username(_message("@my_vpn_bot"), state, db, owner)
    assert await state.get_state() == OwnerStates.waiting_for_bot_token.state

    token_message = _message(TOKEN)
    await create.handle_bot_token(token_message, state, db, owner)
    token_message.delete.assert_awaited()  # токен убран из переписки
    assert await state.get_state() == OwnerStates.waiting_for_bot_picture.state

    not_picture = _message("картинка")
    await create.handle_bot_picture(not_picture, state, db, owner)
    assert "jpg, png" in _answered(not_picture)

    done = _message(photo=[SimpleNamespace(file_id="photo-1")])
    await create.handle_bot_picture(done, state, db, owner)
    assert "@My_Vpn_Bot готов" in _answered(done) and "@owner_name" in _answered(done)
    assert await state.get_state() is None
    platform.refresh_runtime.assert_awaited()

    row = (await db.execute(select(CopycatBot))).scalar_one()
    assert (row.telegram_bot_id, row.owner_user_id, row.support, row.logo) == (
        777000, owner.id, "@owner_name", "data/bot_logos/777000_1.jpg"
    )
    assert row.enabled and row.is_copycat and row.title == "My VPN"
    await db.close()


async def test_no_username_asks_for_support(world) -> None:
    db = await _session()
    owner = await _owner(db)
    state = _state()
    world[TOKEN] = SimpleNamespace(id=777000, username="my_vpn_bot", full_name="My VPN")
    await state.set_state(OwnerStates.waiting_for_bot_username)
    await create.handle_bot_username(_message("my_vpn_bot"), state, db, owner)
    await create.handle_bot_token(_message(TOKEN), state, db, owner)

    await create.handle_bot_picture(_message(photo=[SimpleNamespace(file_id="p")], username=None), state, db, owner)
    assert await state.get_state() == OwnerStates.waiting_for_support_username.state

    bad = _message("кто-то")
    await create.handle_support_username(bad, state, db, owner)
    assert "5–32" in _answered(bad)

    done = _message("@help_desk")
    await create.handle_support_username(done, state, db, owner)
    assert "Выбраны стандартные цены" in _answered(done)
    assert (await db.execute(select(CopycatBot.support))).scalar_one() == "@help_desk"
    await db.close()


async def test_token_errors(world) -> None:
    db = await _session()
    owner = await _owner(db)
    state = _state()
    await state.set_state(OwnerStates.waiting_for_bot_username)

    bad_name = _message("my_vpn")
    await create.handle_bot_username(bad_name, state, db, owner)
    assert "заканчиваться на bot" in _answered(bad_name)

    await create.handle_bot_username(_message("my_vpn_bot"), state, db, owner)
    dead = _message(TOKEN)
    await create.handle_bot_token(dead, state, db, owner)
    assert "Токен не подходит" in _answered(dead)

    world[TOKEN] = SimpleNamespace(id=1, username="other_bot", full_name="Other")
    wrong = _message(TOKEN)
    await create.handle_bot_token(wrong, state, db, owner)
    assert "от @other_bot, а не от @my_vpn_bot" in _answered(wrong)
    assert await state.get_state() == OwnerStates.waiting_for_bot_token.state
    await db.close()


async def test_connected_bots(world) -> None:
    db = await _session()
    owner = await _owner(db)
    stranger = User(telegram_id=2, referral_code="stranger")
    db.add(stranger)
    await db.flush()
    db.add_all(
        [
            CopycatBot(token="live", telegram_bot_id=10, username="live_bot", enabled=True, owner_user_id=stranger.id),
            CopycatBot(token="theirs", telegram_bot_id=11, username="theirs_bot", enabled=False),
            CopycatBot(
                token="old", telegram_bot_id=12, username="mine_bot", enabled=False,
                owner_user_id=owner.id, support="@kept",
            ),
        ]
    )
    await db.commit()
    state = _state()
    await state.set_state(OwnerStates.waiting_for_bot_username)

    live = _message("@live_bot")
    await create.handle_bot_username(live, state, db, owner)
    assert "уже подключен к сервису, выбери другого" in _answered(live)

    theirs = _message("theirs_bot")
    await create.handle_bot_username(theirs, state, db, owner)
    assert "другим владельцем" in _answered(theirs)

    main_bot = _message("@letovpnbot")
    await create.handle_bot_username(main_bot, state, db, owner)
    assert "уже подключен" in _answered(main_bot)

    # Свой выключенный — восстанавливается после токена, без картинки.
    await create.handle_bot_username(_message("mine_bot"), state, db, owner)
    world[TOKEN] = SimpleNamespace(id=12, username="mine_bot", full_name="Mine")
    restored = _message(TOKEN)
    await create.handle_bot_token(restored, state, db, owner)
    assert "снова подключен" in _answered(restored)
    row = (await db.execute(select(CopycatBot).where(CopycatBot.telegram_bot_id == 12))).scalar_one()
    assert row.enabled and row.token == TOKEN and row.support == "@kept"
    assert await state.get_state() is None
    await db.close()


async def test_price_editing(world, monkeypatch) -> None:
    from app.handlers.owner_platform import bot_settings
    from app.services import price_scheme_service as schemes

    schemes.set_snapshot_for_tests([], base={"solo": {30: 32000}, "plus": {30: 49000}, "pro": {30: 69000}})
    save = AsyncMock()
    monkeypatch.setattr(platform, "save_owner_prices", save)
    db = await _session()
    owner = await _owner(db)
    row = CopycatBot(token="t", telegram_bot_id=20, username="price_bot", enabled=True, owner_user_id=owner.id)
    db.add(row)
    await db.commit()
    state = _state()
    await state.set_state(OwnerStates.waiting_for_solo_price)
    await state.set_data({"wl_row": row.id})

    try:
        low = _message("100")
        await bot_settings.handle_price(low, state, db, owner)
        assert "Минимальная цена на Solo 320₽" in _answered(low)

        not_int = _message("3.5")
        await bot_settings.handle_price(not_int, state, db, owner)
        assert "целое число" in _answered(not_int)

        await bot_settings.handle_price(_message("400"), state, db, owner)
        assert await state.get_state() == OwnerStates.waiting_for_plus_price.state

        step = _message("439")  # 400 × 1.1 = 440
        await bot_settings.handle_price(step, state, db, owner)
        assert "минимум 440₽" in _answered(step)
        floor = _message("440")  # пол Plus — 490
        await bot_settings.handle_price(floor, state, db, owner)
        assert "Минимальная цена на Plus — 490₽" in _answered(floor)

        await bot_settings.handle_price(_message("500"), state, db, owner)
        pro_step = _message("599")  # 500 × 1.2 = 600
        await bot_settings.handle_price(pro_step, state, db, owner)
        assert "минимум 600₽" in _answered(pro_step)
        pro_floor = _message("650")  # пол Pro — 690
        await bot_settings.handle_price(pro_floor, state, db, owner)
        assert "Минимальная цена на Pro — 690₽" in _answered(pro_floor)
        save.assert_not_awaited()

        await bot_settings.handle_price(_message("690"), state, db, owner)
        save.assert_awaited_once()
        assert save.await_args.args[2] == {"solo": 400, "plus": 500, "pro": 690}
        assert await state.get_state() is None
    finally:
        schemes.set_snapshot_for_tests([])
        await db.close()


def test_vpn_menu_has_create_bot_button(monkeypatch) -> None:
    from app.branding import brand_scope
    from app.keyboards.inline import get_new_main_menu_keyboard

    monkeypatch.setattr(settings, "OWNER_PLATFORM_BOT_ID", 4242, raising=False)

    def callbacks() -> list[str]:
        keyboard = get_new_main_menu_keyboard(balance_rub=0)
        return [button.callback_data for row in keyboard.inline_keyboard for button in row]

    with brand_scope(4242):
        assert callbacks()[0] == "wl_entry"
    with brand_scope(4243):
        assert "wl_entry" not in callbacks()


def _callback(data: str):
    from unittest.mock import MagicMock

    from aiogram import types

    callback = MagicMock(spec=types.CallbackQuery)
    callback.data = data
    callback.message = SimpleNamespace(
        edit_text=AsyncMock(), answer=AsyncMock(), answer_photo=AsyncMock(), delete=AsyncMock()
    )
    callback.bot = SimpleNamespace(id=1)
    callback.from_user = SimpleNamespace(id=1, username="owner_name")
    callback.answer = AsyncMock()
    return callback


def _screen(callback) -> str:
    edit = callback.message.edit_text
    return (edit.await_args or callback.message.answer.await_args).args[0]


async def test_owner_screens(world, monkeypatch) -> None:
    from decimal import Decimal

    from app.handlers.owner_platform import bot_settings, common, earnings
    from app.services import price_scheme_service as schemes

    schemes.set_snapshot_for_tests([], base={"solo": {30: 32000}, "plus": {30: 49000}, "pro": {30: 69000}})
    monkeypatch.setattr(settings, "OWNER_TERMS_URL", "https://example.com/terms", raising=False)
    monkeypatch.setattr(settings, "OWNER_PRIVACY_URL", "https://example.com/privacy", raising=False)
    monkeypatch.setattr(platform, "bot_is_working", AsyncMock(return_value=False))
    monkeypatch.setattr(
        platform, "get_payout_rate",
        AsyncMock(return_value=platform.PayoutRate(cbr_rate=Decimal("100"), rate=Decimal("107"))),
    )
    db = await _session()
    owner = await _owner(db)
    row = CopycatBot(token="t", telegram_bot_id=30, username="shown_bot", enabled=True, owner_user_id=owner.id,
                     support="@helper")
    db.add(row)
    await db.commit()
    state = _state()
    try:
        # Без ботов — экран выбора, с ботами — меню владельца.
        empty = User(telegram_id=5, referral_code="empty")
        db.add(empty)
        await db.commit()
        choice = _callback("wl_home")
        await common.show_home(choice, db, empty)
        assert "Создай свой VPN бот" in _screen(choice)

        menu = _callback("wl_menu")
        await common.show_home(menu, db, owner)
        text = _screen(menu)
        assert "Активных ботов: <b>1</b>" in text and "https://example.com/terms" in text

        bot_screen = _callback(f"wl_bot:{row.id}")
        await bot_settings.show_bot(bot_screen, state, db, owner)
        assert "@shown_bot" in _screen(bot_screen) and "🔴 не работает" in _screen(bot_screen)
        assert "Удали бота и подключи заново" in _screen(bot_screen)

        support = _callback(f"wl_support:{row.id}")
        await bot_settings.show_support(support, state, db, owner)
        assert "@helper" in _screen(support)

        prices = _callback(f"wl_prices:{row.id}")
        await bot_settings.show_prices(prices, state, db, owner)
        assert "<b>Solo – 320₽ в мес</b>" in _screen(prices) and "10 устройств" in _screen(prices)

        # Чужой бот не открывается.
        stranger_bot = _callback("wl_bot:999")
        await bot_settings.show_bot(stranger_bot, state, db, owner)
        assert stranger_bot.answer.await_args.kwargs.get("show_alert")

        earn = _callback("wl_earn")
        await earnings.show_earnings(earn, state, db, owner)
        assert "Оборот всего: <b>0₽</b>" in _screen(earn) and "(60% от оборота)" in _screen(earn)

        # Вывод без кошелька ведёт к вводу кошелька.
        payout = _callback("wl_payout")
        await earnings.start_payout(payout, state, db, owner)
        assert "TRON TRC-20" in _screen(payout)
        assert await state.get_state() == OwnerStates.waiting_for_wallet.state

        bad_wallet = _message("TXYZ")
        await earnings.handle_wallet(bad_wallet, state, db, owner)
        assert "Это не адрес TRON" in _answered(bad_wallet)
        good_wallet = _message("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")
        await earnings.handle_wallet(good_wallet, state, db, owner)
        assert "TR7N…Lj6t" in _answered(good_wallet)

        payout = _callback("wl_payout")
        await earnings.start_payout(payout, state, db, owner)
        assert "Доступно к выводу: <b>0.00 USDT</b>" in _screen(payout)
        too_much = _message("10")
        await earnings.handle_payout_amount(too_much, state, db, owner)
        assert "больше доступной" in _answered(too_much)

        delete = _callback(f"wl_del_yes:{row.id}")
        monkeypatch.setattr(platform, "refresh_runtime", AsyncMock())
        await bot_settings.do_delete(delete, state, db, owner)
        assert not row.enabled
        assert "Создай свой VPN бот" in _screen(delete)  # ботов не осталось — снова выбор
    finally:
        schemes.set_snapshot_for_tests([])
        await db.close()


async def test_reconnect_with_the_same_token(world) -> None:
    db = await _session()
    owner = await _owner(db)
    db.add(CopycatBot(token=TOKEN, telegram_bot_id=777000, username="same_bot", enabled=False,
                      owner_user_id=owner.id, support="@kept"))
    await db.commit()
    world[TOKEN] = SimpleNamespace(id=777000, username="same_bot", full_name="Same")
    state = _state()
    await state.set_state(OwnerStates.waiting_for_bot_username)
    await create.handle_bot_username(_message("same_bot"), state, db, owner)

    restored = _message(TOKEN)
    await create.handle_bot_token(restored, state, db, owner)
    assert "снова подключен" in _answered(restored)
    row = (await db.execute(select(CopycatBot))).scalar_one()
    assert row.enabled and row.support == "@kept"
    await db.close()


async def test_start_shows_choice_until_a_mode_is_picked(monkeypatch) -> None:
    from app.handlers.owner_platform import common

    platform_bot = 8722
    monkeypatch.setattr(settings, "OWNER_PLATFORM_BOT_ID", platform_bot, raising=False)
    db = await _session()
    try:
        # Общий аккаунт из основного бота, в платформе режим не выбирал.
        legacy = User(telegram_id=7, referral_code="legacy", bot_id=None)
        # Зарегистрирован в платформе через «Попробовать VPN» до запоминания режима.
        local = User(telegram_id=8, referral_code="local", bot_id=platform_bot)
        db.add_all([legacy, local])
        await db.commit()

        start = _message("/start")
        assert await common.show_owner_home_on_start(start, _state(), db, legacy)
        assert "Создай свой VPN бот" in _answered(start)
        assert not await common.show_owner_home_on_start(_message("/start"), _state(), db, local)

        # Выбрал VPN — дальше обычное меню.
        await platform.set_mode(db, legacy.id, platform.MODE_VPN)
        assert not await common.show_owner_home_on_start(_message("/start"), _state(), db, legacy)

        # Выбрал «создать бота», ботов нет — снова выбор.
        await platform.set_mode(db, legacy.id, platform.MODE_OWNER)
        again = _message("/start")
        assert await common.show_owner_home_on_start(again, _state(), db, legacy)
        assert "Создай свой VPN бот" in _answered(again)
    finally:
        await db.close()
