"""Сквады бота (платный и основного бренда) добавляются при отправке и вырезаются при импорте."""

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.utils import bot_registry
from app.utils.panel_squads import build_panel_squads, strip_bot_managed_squads

PAID = "7c8060f5-cacc-47ae-8aa4-484c48171764"
DEFAULT = "53b1b439-0396-49bc-ba5d-cab4340e6247"
BRAND = "c2e3b15d-65a5-480b-9fd1-a163e9d335a1"
COPYCAT_BOT_ID = 7000000001

LETO_USER = SimpleNamespace(bot_id=None)
COPYCAT_USER = SimpleNamespace(bot_id=COPYCAT_BOT_ID)


@pytest.fixture(autouse=True)
def no_brand_squad(monkeypatch):
    monkeypatch.setattr(settings, "PRIMARY_BRAND_INTERNAL_SQUAD_UUID", None)


@pytest.fixture
def paid_squad(monkeypatch):
    monkeypatch.setattr(settings, "PAID_INTERNAL_SQUAD_UUID", PAID)
    return PAID


@pytest.fixture
def brand_squad(monkeypatch, paid_squad):
    monkeypatch.setattr(settings, "PRIMARY_BRAND_INTERNAL_SQUAD_UUID", BRAND)
    bot_registry.register_bot(
        COPYCAT_BOT_ID, Path("logo.png"), brand_config={"copycat": True, "name": "Copycat VPN"}
    )
    yield BRAND
    bot_registry.forget_bot(COPYCAT_BOT_ID)


_UNSET = object()


def _subscription(is_trial: bool, squads=_UNSET):
    return SimpleNamespace(is_trial=is_trial, connected_squads=[DEFAULT] if squads is _UNSET else squads)


def test_without_setting_list_is_untouched(monkeypatch):
    monkeypatch.setattr(settings, "PAID_INTERNAL_SQUAD_UUID", None)
    assert build_panel_squads(_subscription(False), user=LETO_USER) == [DEFAULT]
    assert strip_bot_managed_squads([DEFAULT, PAID]) == [DEFAULT, PAID]


def test_blank_setting_means_disabled(monkeypatch):
    monkeypatch.setattr(settings, "PAID_INTERNAL_SQUAD_UUID", "   ")
    assert build_panel_squads(_subscription(False), user=LETO_USER) == [DEFAULT]


def test_paid_subscription_gets_squad(paid_squad):
    assert build_panel_squads(_subscription(False), user=LETO_USER) == [DEFAULT, PAID]


def test_trial_subscription_does_not(paid_squad):
    assert build_panel_squads(_subscription(True), user=LETO_USER) == [DEFAULT]


def test_trial_loses_squad_left_over_in_panel(paid_squad):
    # Базу передали из панели, где сквад уже был (например, после смены типа подписки).
    assert build_panel_squads(_subscription(True), [DEFAULT, PAID], user=LETO_USER) == [DEFAULT]


def test_no_duplicates_when_already_present(paid_squad):
    assert build_panel_squads(_subscription(False), [PAID, DEFAULT], user=LETO_USER) == [DEFAULT, PAID]


def test_explicit_squads_override_connected(paid_squad):
    subscription = _subscription(False, squads=["ignored"])
    assert build_panel_squads(subscription, ["explicit"], user=LETO_USER) == ["explicit", PAID]


def test_empty_connected_squads_still_gets_paid(paid_squad):
    assert build_panel_squads(_subscription(False, squads=[]), user=LETO_USER) == [PAID]
    assert build_panel_squads(_subscription(False, squads=None), user=LETO_USER) == [PAID]


def test_strip_removes_only_paid(paid_squad):
    assert strip_bot_managed_squads([DEFAULT, PAID, "other"]) == [DEFAULT, "other"]
    assert strip_bot_managed_squads([]) == []


def test_leto_trial_and_paid_get_brand_squad(brand_squad):
    assert build_panel_squads(_subscription(True), user=LETO_USER) == [DEFAULT, BRAND]
    assert build_panel_squads(_subscription(False), user=LETO_USER) == [DEFAULT, PAID, BRAND]


def test_copycat_does_not_get_brand_squad(brand_squad):
    assert build_panel_squads(_subscription(True), user=COPYCAT_USER) == [DEFAULT]
    assert build_panel_squads(_subscription(False), user=COPYCAT_USER) == [DEFAULT, PAID]


def test_copycat_loses_brand_squad_left_over_in_panel(brand_squad):
    assert build_panel_squads(_subscription(True), [BRAND, DEFAULT], user=COPYCAT_USER) == [DEFAULT]


def test_brand_squad_not_duplicated(brand_squad):
    assert build_panel_squads(_subscription(True), [BRAND, DEFAULT, BRAND], user=LETO_USER) == [DEFAULT, BRAND]


def test_mirror_user_gets_brand_squad(brand_squad):
    mirror_id = 7000000002
    bot_registry.register_bot(mirror_id, Path("logo.png"), brand_config={"copycat": False})
    try:
        user = SimpleNamespace(bot_id=mirror_id)
        assert build_panel_squads(_subscription(True), user=user) == [DEFAULT, BRAND]
    finally:
        bot_registry.forget_bot(mirror_id)


def test_strip_removes_both_bot_squads(brand_squad):
    assert strip_bot_managed_squads([DEFAULT, BRAND, PAID, "other"]) == [DEFAULT, "other"]
