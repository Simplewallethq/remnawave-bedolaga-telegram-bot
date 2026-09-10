"""Окно выборки привязок 1Payment: общее — в днях, для суточных подписок — в часах."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from typing import Any, Dict, List

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.crud.onepayment import list_onepayment_bindings_due  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Result:
    def scalars(self) -> "_Result":
        return self

    def all(self) -> List[Any]:
        return []


class _Db:
    def __init__(self) -> None:
        self.statements: List[str] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
        return _Result()


@pytest.mark.anyio
async def test_without_paid_trial_window_uses_single_end_date_bound() -> None:
    db = _Db()
    await list_onepayment_bindings_due(db, before=datetime(2030, 1, 2))
    sql = db.statements[0]
    assert "is_paid_trial" not in sql
    assert "subscriptions.end_date <= '2030-01-02 00:00:00'" in sql


@pytest.mark.anyio
async def test_paid_trial_window_is_applied_only_to_paid_trial_subscriptions() -> None:
    db = _Db()
    await list_onepayment_bindings_due(
        db, before=datetime(2030, 1, 2), paid_trial_before=datetime(2030, 1, 1, 3)
    )
    sql = db.statements[0]
    assert "subscriptions.is_paid_trial = false AND subscriptions.end_date <= '2030-01-02 00:00:00'" in sql
    assert "subscriptions.is_paid_trial = true AND subscriptions.end_date <= '2030-01-01 03:00:00'" in sql
    assert " OR " in sql
