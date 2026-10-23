"""Конструктор: поиск токена, аккаунт владельца, проверка и запуск бота."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.constructor import service
from app.database.models import BotOwner, CopycatBot, User
from app.services import owner_platform_service as platform

from .conftest import OTHER_TOKEN, PLATFORM_BOT, TOKEN, needs_sqlite, png_bytes

pytestmark = [pytest.mark.anyio]

OWNER = service.TgUser(id=1, username="owner_name", first_name="Owner")


def test_find_token_in_botfather_reply():
    reply = f"Done! Congratulations on your new bot.\nUse this token to access the HTTP API:\n{TOKEN}\nKeep it secure"
    assert service.find_token(reply) == TOKEN
    assert service.find_token(f"  {TOKEN}  ") == TOKEN
    assert service.find_token("12345:short") is None
    assert service.find_token(None) is None


def test_save_picture_bytes_reencodes_to_jpeg(telegram):
    path = Path(platform.save_picture_bytes(png_bytes(), 777000))
    assert path.suffix == ".jpg" and path.parent == Path(settings.OWNER_BOT_IMAGES_DIR)
    with Image.open(path) as image:
        assert image.format == "JPEG" and image.mode == "RGB" and image.size == (64, 32)
    with pytest.raises(ValueError):
        platform.save_picture_bytes(b"not an image", 777000)
    with pytest.raises(ValueError):  # Telegram не примет полоску 1×100
        platform.save_picture_bytes(png_bytes((100, 1), "RGB"), 777000)


@needs_sqlite
async def test_owner_account_lives_in_platform_scope(db, telegram):
    assert await service.find_owner_account(db, OWNER.id) is None
    user = await service.ensure_owner_account(db, OWNER)
    assert user.bot_id == PLATFORM_BOT and user.username == "owner_name"
    assert (await service.ensure_owner_account(db, OWNER)).id == user.id
    assert (await service.find_owner_account(db, OWNER.id)).id == user.id


@needs_sqlite
async def test_blacklisted_user_gets_no_account(db, telegram, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(service.blacklist_service, "is_user_blacklisted", AsyncMock(return_value=(True, "spam")))
    with pytest.raises(service.ConnectError) as error:
        await service.ensure_owner_account(db, OWNER)
    assert error.value.code == "blocked"


@needs_sqlite
async def test_check_token(db, telegram):
    telegram(TOKEN, 777000, "My_Vpn_Bot", "My VPN")
    info = await service.check_token(db, None, TOKEN)
    assert (info.bot_id, info.username, info.title, info.restore_row_id, info.foreign_webhook) == (
        777000, "My_Vpn_Bot", "My VPN", None, False
    )

    with pytest.raises(service.ConnectError) as error:
        await service.check_token(db, None, OTHER_TOKEN)
    assert error.value.code == "invalid"

    telegram(OTHER_TOKEN, PLATFORM_BOT, "dostupoffbot")
    with pytest.raises(service.ConnectError) as error:
        await service.check_token(db, None, OTHER_TOKEN)
    assert error.value.code == "reserved"


@needs_sqlite
async def test_foreign_webhook_is_reported_but_our_own_is_not(db, telegram):
    telegram(TOKEN, 777000, "My_Vpn_Bot", webhook="https://other.service/hook")
    assert (await service.check_token(db, None, TOKEN)).foreign_webhook

    from app.services.copycat_service import mirror_webhook_url

    telegram(TOKEN, 777000, "My_Vpn_Bot", webhook=mirror_webhook_url(777000))
    assert not (await service.check_token(db, None, TOKEN)).foreign_webhook


@needs_sqlite
async def test_launch_new_bot_with_default_cover(db, telegram):
    telegram(TOKEN, 777000, "My_Vpn_Bot", "My VPN")
    user = await service.ensure_owner_account(db, OWNER)
    info = await service.check_token(db, user.id, TOKEN)

    row, restored = await service.launch_bot(db, user, info, support="@help_desk", logo=None)
    assert not restored
    assert (row.telegram_bot_id, row.owner_user_id, row.support, row.logo) == (
        777000, user.id, "@help_desk", service.DEFAULT_COVER
    )
    assert row.enabled and row.is_copycat and row.title == "My VPN"
    assert (await db.execute(select(BotOwner).where(BotOwner.user_id == user.id))).scalar_one().mode == "owner"
    platform.refresh_runtime.assert_awaited()

    # Подключённого бота второй раз не подключить — ни этому владельцу, ни другому.
    with pytest.raises(service.ConnectError) as error:
        await service.check_token(db, user.id, TOKEN)
    assert error.value.code == "active"


@needs_sqlite
async def test_disabled_bot_restores_for_owner_and_is_foreign_for_others(db, telegram):
    telegram(TOKEN, 777000, "My_Vpn_Bot")
    user = await service.ensure_owner_account(db, OWNER)
    row, _ = await service.launch_bot(db, user, await service.check_token(db, user.id, TOKEN), support="help_desk", logo="x.jpg")
    await platform.disable_bot(db, row)

    stranger = User(telegram_id=2, referral_code="stranger")
    db.add(stranger)
    await db.commit()
    with pytest.raises(service.ConnectError) as error:
        await service.check_token(db, stranger.id, TOKEN)
    assert error.value.code == "foreign"

    info = await service.check_token(db, user.id, TOKEN)
    assert info.restore_row_id == row.id
    restored_row, restored = await service.launch_bot(db, user, info, support=None, logo=None)
    assert restored and restored_row.id == row.id and restored_row.enabled
    assert (restored_row.support, restored_row.logo) == ("@help_desk", "x.jpg")  # настройки сохранились
    assert len((await db.execute(select(CopycatBot))).scalars().all()) == 1


@needs_sqlite
async def test_bot_limit(db, telegram, monkeypatch):
    monkeypatch.setattr(settings, "OWNER_MAX_BOTS", 1, raising=False)
    telegram(TOKEN, 777000, "My_Vpn_Bot")
    telegram(OTHER_TOKEN, 888000, "Second_Bot")
    user = await service.ensure_owner_account(db, OWNER)
    await service.launch_bot(db, user, await service.check_token(db, user.id, TOKEN), support="help_desk", logo=None)

    with pytest.raises(service.ConnectError) as error:
        await service.launch_bot(db, user, await service.check_token(db, user.id, OTHER_TOKEN), support="help_desk", logo=None)
    assert error.value.code == "limit" and "1 ботов" in error.value.message
