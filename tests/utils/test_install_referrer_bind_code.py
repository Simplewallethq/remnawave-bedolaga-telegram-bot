from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

from app.handlers import menu
from app.utils.install_referrer import build_personal_play_link, parse_install_referrer

PLAY = "https://play.google.com/store/apps/details?id=com.leto.split"


def _referrer(url: str) -> dict:
    raw = parse_qs(urlsplit(url).query)["referrer"][0]
    return {key: values[0] for key, values in parse_qs(raw).items()}


def test_play_link_carries_bind_code_for_first_launch_sign_in():
    url = build_personal_play_link(PLAY, 42, "ABCDEFGHJKLMNPQR")

    assert _referrer(url) == {
        "utm_source": "telegram",
        "tg_user_id": "42",
        "bind_code": "ABCDEFGHJKLMNPQR",
    }


def test_play_link_without_code_stays_attribution_only():
    assert _referrer(build_personal_play_link(PLAY, 42)) == {
        "utm_source": "telegram",
        "tg_user_id": "42",
    }


def test_attribution_parser_ignores_the_bind_code():
    assert parse_install_referrer(
        "utm_source=telegram&tg_user_id=42&bind_code=ABCDEFGHJKLMNPQR"
    ) == ("telegram", 42)


async def test_bind_code_is_issued_for_the_users_subscription(monkeypatch):
    issue = AsyncMock(return_value=SimpleNamespace(code="ABCDEFGHJKLMNPQR"))
    monkeypatch.setattr(menu, "get_or_create_binding_code", issue)
    user = SimpleNamespace(id=1, subscription=SimpleNamespace(id=7))
    db = AsyncMock()

    assert await menu._play_link_bind_code(db, user) == "ABCDEFGHJKLMNPQR"
    issue.assert_awaited_once_with(db, 7)


async def test_no_subscription_means_no_code(monkeypatch):
    issue = AsyncMock()
    monkeypatch.setattr(menu, "get_or_create_binding_code", issue)

    assert await menu._play_link_bind_code(AsyncMock(), SimpleNamespace(id=1, subscription=None)) is None
    issue.assert_not_awaited()


async def test_code_failure_never_breaks_the_menu(monkeypatch):
    monkeypatch.setattr(menu, "get_or_create_binding_code", AsyncMock(side_effect=RuntimeError("db down")))
    db = AsyncMock()

    assert await menu._play_link_bind_code(db, SimpleNamespace(id=1, subscription=SimpleNamespace(id=7))) is None
    db.rollback.assert_awaited_once()
