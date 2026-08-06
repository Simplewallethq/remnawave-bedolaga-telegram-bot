"""Нормализация кода/ссылки подписки для входа в приложение и кабинет."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("BOT_TOKEN", "test-token")

from app.utils.subscription_code import extract_subscription_code  # noqa: E402

CODE = "-BgpfZQ062Td9Fpk"


@pytest.mark.parametrize(
    "raw",
    [
        f"https://letovpn.com/sub/{CODE}",
        f"https://letovpn.com/sub/{CODE}/",
        f"  https://letovpn.com/sub/{CODE}  ",
        f"https://letovpn.com/sub/{CODE}?utm=tg",
        f"https://letovpn.com/sub/{CODE}#fragment",
        f"https://letovpn.com/sub/{CODE}/happ",
        f"https://letovpn.com/sub/{CODE}/json",
        f"http://letovpn.com/sub/{CODE}",
        f"letovpn.com/sub/{CODE}",
    ],
)
def test_link_normalizes_to_code(raw: str) -> None:
    """Пользователь копирует ссылку как угодно — из кабинета, из клиента,
    с хвостом от шаринга. Из всех форм должен получиться один и тот же код."""
    assert extract_subscription_code(raw) == (CODE, True)


def test_plain_code_is_not_a_link() -> None:
    """Голый код должен пройти дальше по цепочке (код привязки/share-код),
    поэтому он не помечается как ссылка."""
    assert extract_subscription_code(CODE) == (CODE, False)
    assert extract_subscription_code(f"  {CODE} ") == (CODE, False)


def test_link_without_sub_segment_falls_back_to_last_segment() -> None:
    """Если домен раздаёт подписку по другому пути — берём последний сегмент,
    а не отказываем пользователю."""
    assert extract_subscription_code(f"https://letovpn.com/{CODE}") == (CODE, True)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ("", False)),
        ("   ", ("", False)),
        (None, ("", False)),
        ("https://letovpn.com/sub/", ("", True)),
        ("https://letovpn.com/", ("", True)),
        ("///", ("", True)),
    ],
)
def test_garbage_returns_empty_code(raw, expected) -> None:
    """Мусор не должен ронять хендлер — только пустой код, на который
    вызывающий ответит 404."""
    assert extract_subscription_code(raw) == expected
