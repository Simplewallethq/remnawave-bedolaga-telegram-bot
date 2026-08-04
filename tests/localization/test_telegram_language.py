import pytest

from app.localization.language import INTERFACE_LANGUAGES, resolve_telegram_language
from app.keyboards.inline import get_language_selection_keyboard


@pytest.mark.parametrize(
    ("telegram_language", "expected"),
    [
        ("ru", "ru"),
        ("ru-RU", "ru"),
        ("ru_RU", "ru"),
        ("en", "en"),
        ("uk", "en"),
        ("zh-Hans", "en"),
        (None, "en"),
        ("", "en"),
    ],
)
def test_resolve_telegram_language_uses_russian_or_english_fallback(
    telegram_language: str | None,
    expected: str,
) -> None:
    assert resolve_telegram_language(telegram_language) == expected


def test_interface_language_selector_is_limited_to_russian_and_english() -> None:
    assert INTERFACE_LANGUAGES == ("ru", "en")
    callbacks = [
        button.callback_data
        for row in get_language_selection_keyboard().inline_keyboard
        for button in row
    ]
    assert callbacks == ["language_select:ru", "language_select:en"]
