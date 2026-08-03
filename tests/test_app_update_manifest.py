"""Тесты манифеста автообновления приложения и расчёта версий."""

import pytest

from app.config import settings
from app.webapi.routes.app_updates import _is_older, _parse_version


@pytest.fixture
def windows_channel(monkeypatch):
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_ENABLED", True)
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_VERSION", "1.4.2")
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_URL", "https://cdn.example.com/setup-1.4.2.exe")
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_MIRRORS", None)
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_SHA256", None)
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_MIN_VERSION", None)
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_NOTES", None)


def test_manifest_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_ENABLED", False)
    assert settings.get_app_update_manifest("windows") is None


def test_manifest_requires_version_and_url(monkeypatch, windows_channel):
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_URL", "   ")
    assert settings.get_app_update_manifest("windows") is None


def test_manifest_normalizes_mirrors_and_hash(monkeypatch, windows_channel):
    monkeypatch.setattr(
        settings,
        "APP_UPDATE_WINDOWS_MIRRORS",
        " https://github.com/org/app/releases/download/v1.4.2/setup.exe , ,https://mirror.example.com/setup.exe ",
    )
    monkeypatch.setattr(settings, "APP_UPDATE_WINDOWS_SHA256", "  ABCDEF  ")

    manifest = settings.get_app_update_manifest("windows")

    assert manifest["version"] == "1.4.2"
    assert manifest["mirrors"] == [
        "https://github.com/org/app/releases/download/v1.4.2/setup.exe",
        "https://mirror.example.com/setup.exe",
    ]
    assert manifest["sha256"] == "abcdef"
    assert manifest["min_supported_version"] is None


def test_manifest_platform_aliases_and_unknown(windows_channel):
    assert settings.get_app_update_manifest("PC")["platform"] == "windows"
    assert settings.get_app_update_manifest(" Windows ")["platform"] == "windows"
    assert settings.get_app_update_manifest("macos") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.4.2", (1, 4, 2)),
        (" 2.0 ", (2, 0)),
        ("1.4.2-beta.1", (1, 4, 2)),
        ("v1.4.2", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_version(raw, expected):
    assert _parse_version(raw) == expected


def test_is_older_handles_different_lengths():
    assert _is_older((1, 4), (1, 4, 1))
    assert not _is_older((1, 4, 0), (1, 4))
    assert not _is_older((1, 5), (1, 4, 9))
