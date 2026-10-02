"""Админка розыгрыша: сводка, топ и CSV со всеми участниками."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.handlers.admin.giveaway import build_admin_giveaway_text, build_participants_csv
from app.services.giveaway_service import GiveawaySummary


def _row(tickets, username=None, first_name="Имя", telegram_id=1, plan=None):
    entry = SimpleNamespace(
        tickets=tickets, channel_subscribed=True, invited_count=3, invited_paid_count=1,
        plan_code=plan, checked_at=datetime(2026, 10, 5, 9, 30),
    )
    user = SimpleNamespace(
        id=telegram_id, telegram_id=telegram_id, username=username,
        first_name=first_name, last_name=None,
    )
    return entry, user


SUMMARY = GiveawaySummary(
    participants=2, tickets=30, channel_subscribed=2, invited=6, invited_paid=2,
    plans={"solo": 0, "plus": 1, "pro": 1},
)


def test_admin_text_shows_totals_and_top() -> None:
    text = build_admin_giveaway_text(
        SUMMARY, [_row(20, username="ivan"), _row(10, first_name="<Без ника>", telegram_id=5)]
    )

    assert "Участников: <b>2</b>" in text
    assert "Билетов всего: <b>30</b>" in text
    assert "Plus 1 · Pro 1" in text
    assert "1. @ivan — <b>20</b>" in text
    assert '<a href="tg://user?id=5">&lt;Без ника&gt;</a>' in text


def test_admin_text_when_nobody_joined() -> None:
    empty = GiveawaySummary(0, 0, 0, 0, 0, {"solo": 0, "plus": 0, "pro": 0})

    assert "Пока никто не участвует" in build_admin_giveaway_text(empty, [])


def test_csv_lists_everyone_in_ticket_order() -> None:
    data = build_participants_csv([_row(20, username="ivan", plan="pro"), _row(10)])
    text = data.decode("utf-8")

    assert text.startswith("\ufeff")
    lines = text.lstrip("\ufeff").splitlines()
    assert lines[0].startswith("место;user_id;telegram_id")
    assert lines[1].split(";")[:6] == ["1", "1", "1", "@ivan", "Имя", "20"]
    assert lines[1].endswith("Pro;05.10.2026 12:30")
    assert len(lines) == 3
