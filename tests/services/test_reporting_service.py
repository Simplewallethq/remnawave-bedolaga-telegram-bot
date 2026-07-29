from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services import reporting_service as reporting_module
from app.services.reporting_service import ReportingService
from app.services.vpn_deposit_bonus_service import vpn_deposit_bonus_service


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _RowsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _ScalarsResult:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return self._values


class _ExecuteResult:
    def __init__(self, *, rows: list[Any] | None = None, scalars: list[Any] | None = None) -> None:
        self._rows = rows or []
        self._scalars = scalars or []

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _ScalarsResult:
        return _ScalarsResult(self._scalars)


class _FakeSession:
    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.commits = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.results.pop(0)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.anyio("asyncio")
async def test_registration_report_queues_vpn_bonus_for_newly_connected_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_row = SimpleNamespace(
        id=42,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        auth_source="telegram",
        has_connected_to_vpn=False,
        remnawave_uuid="uuid-42",
        has_had_paid_subscription=False,
    )
    user = SimpleNamespace(id=42, telegram_id=4200)
    session = _FakeSession(
        [
            _RowsResult([report_row]),
            _ExecuteResult(),
            _ExecuteResult(scalars=[user]),
        ]
    )
    service = ReportingService()
    queue_mock = AsyncMock()

    monkeypatch.setattr(reporting_module, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(
        service,
        "_check_vpn_connections",
        AsyncMock(return_value=(True, 1, 0, {42})),
    )
    monkeypatch.setattr(
        vpn_deposit_bonus_service,
        "on_first_vpn_connection_detected",
        queue_mock,
    )

    report = await service.build_registration_cohort_report(days=1)

    assert report.api_checked == 1
    assert session.commits == 1
    queue_mock.assert_awaited_once_with(
        session,
        user,
        source="reporting_vpn_connection_check",
    )
