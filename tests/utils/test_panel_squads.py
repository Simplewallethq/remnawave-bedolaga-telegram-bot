"""Платный сквад панели добавляется при отправке и вырезается при импорте."""

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.utils.panel_squads import build_panel_squads, strip_paid_squad

PAID = "7c8060f5-cacc-47ae-8aa4-484c48171764"
DEFAULT = "53b1b439-0396-49bc-ba5d-cab4340e6247"


@pytest.fixture
def paid_squad(monkeypatch):
    monkeypatch.setattr(settings, "PAID_INTERNAL_SQUAD_UUID", PAID)
    return PAID


_UNSET = object()


def _subscription(is_trial: bool, squads=_UNSET):
    return SimpleNamespace(is_trial=is_trial, connected_squads=[DEFAULT] if squads is _UNSET else squads)


def test_without_setting_list_is_untouched(monkeypatch):
    monkeypatch.setattr(settings, "PAID_INTERNAL_SQUAD_UUID", None)
    assert build_panel_squads(_subscription(False)) == [DEFAULT]
    assert strip_paid_squad([DEFAULT, PAID]) == [DEFAULT, PAID]


def test_blank_setting_means_disabled(monkeypatch):
    monkeypatch.setattr(settings, "PAID_INTERNAL_SQUAD_UUID", "   ")
    assert build_panel_squads(_subscription(False)) == [DEFAULT]


def test_paid_subscription_gets_squad(paid_squad):
    assert build_panel_squads(_subscription(False)) == [DEFAULT, PAID]


def test_trial_subscription_does_not(paid_squad):
    assert build_panel_squads(_subscription(True)) == [DEFAULT]


def test_trial_loses_squad_left_over_in_panel(paid_squad):
    # Базу передали из панели, где сквад уже был (например, после смены типа подписки).
    assert build_panel_squads(_subscription(True), [DEFAULT, PAID]) == [DEFAULT]


def test_no_duplicates_when_already_present(paid_squad):
    assert build_panel_squads(_subscription(False), [PAID, DEFAULT]) == [DEFAULT, PAID]


def test_explicit_squads_override_connected(paid_squad):
    subscription = _subscription(False, squads=["ignored"])
    assert build_panel_squads(subscription, ["explicit"]) == ["explicit", PAID]


def test_empty_connected_squads_still_gets_paid(paid_squad):
    assert build_panel_squads(_subscription(False, squads=[])) == [PAID]
    assert build_panel_squads(_subscription(False, squads=None)) == [PAID]


def test_strip_removes_only_paid(paid_squad):
    assert strip_paid_squad([DEFAULT, PAID, "other"]) == [DEFAULT, "other"]
    assert strip_paid_squad([]) == []
