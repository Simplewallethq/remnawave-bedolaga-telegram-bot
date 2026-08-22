"""Жизненный цикл журнала маршрутизации на настоящей SQLite-сессии."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database.crud.payment_routing import (  # noqa: E402
    STATUS_FAILED,
    STATUS_ISSUED,
    STATUS_PAID,
    backfill_routing_paid_flags,
    create_routing_log,
    mark_routing_failed,
    mark_routing_issued,
    mark_routing_paid,
    routing_stats,
)
from app.database.models import Base, PaymentRoutingLog, WataPayment  # noqa: E402


def _aiosqlite_available() -> bool:
    """conftest подставляет заглушку, только если настоящего драйвера нет."""
    module = sys.modules.get("aiosqlite")
    return module is not None and hasattr(module, "DatabaseError")


pytestmark = pytest.mark.skipif(
    not _aiosqlite_available(), reason="настоящий aiosqlite недоступен"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"



async def _make_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return maker()


@pytest.mark.anyio
async def test_lifecycle_pending_issued_paid() -> None:
    db = await _make_session()
    try:
        entry = await create_routing_log(
            db,
            user_id=None,
            source="balance_topup",
            amount_kopeks=50_000,
            requested_gateway="platega",
            weights={"platega": 1, "wata": 1, "yookassa": 1},
        )
        assert entry.status == "pending"

        await mark_routing_issued(
            db,
            entry.id,
            gateway="wata",
            local_payment_id=77,
            external_id="link-1",
            payment_url="https://wata.example/pay",
            fallback_used=True,
            attempts=[{"gateway": "platega", "ok": False}],
        )
        await db.refresh(entry)
        assert entry.status == STATUS_ISSUED
        assert entry.gateway == "wata"
        # Назначение сохраняется отдельно — иначе конверсию не посчитать.
        assert entry.requested_gateway == "platega"
        assert entry.fallback_used is True

        assert await mark_routing_paid(
            db, gateway="wata", local_payment_id=77, amount_kopeks=50_000
        )
        await db.refresh(entry)
        assert entry.status == STATUS_PAID
        assert entry.paid_at is not None
        assert entry.paid_amount_kopeks == 50_000
    finally:
        await db.close()


@pytest.mark.anyio
async def test_mark_paid_is_idempotent_and_tolerates_unknown() -> None:
    db = await _make_session()
    try:
        entry = await create_routing_log(
            db,
            user_id=None,
            source="balance_topup",
            amount_kopeks=10_000,
            requested_gateway="wata",
            weights={},
        )
        await mark_routing_issued(
            db,
            entry.id,
            gateway="wata",
            local_payment_id=5,
            external_id=None,
            payment_url="u",
            fallback_used=False,
            attempts=[],
        )
        assert await mark_routing_paid(db, gateway="wata", local_payment_id=5)
        first = (await db.get(PaymentRoutingLog, entry.id)).paid_at
        assert await mark_routing_paid(db, gateway="wata", local_payment_id=5)
        assert (await db.get(PaymentRoutingLog, entry.id)).paid_at == first

        # Неизвестный счёт не должен ронять денежный путь.
        assert not await mark_routing_paid(db, gateway="wata", local_payment_id=999)
    finally:
        await db.close()


@pytest.mark.anyio
async def test_failed_invoices_are_recorded() -> None:
    """Провалы обязаны попадать в знаменатель, иначе шлюз выглядит идеальным."""
    db = await _make_session()
    try:
        entry = await create_routing_log(
            db,
            user_id=None,
            source="balance_topup",
            amount_kopeks=20_000,
            requested_gateway="yookassa",
            weights={},
        )
        await mark_routing_failed(
            db, entry.id, attempts=[{"gateway": "yookassa", "ok": False}]
        )
        await db.refresh(entry)
        assert entry.status == STATUS_FAILED

        stats = await routing_stats(db, since=datetime.utcnow() - timedelta(hours=1))
        row = next(r for r in stats if r["gateway"] == "yookassa")
        assert row["requested"] == 1
        assert row["issued"] == 0
        assert row["paid"] == 0
    finally:
        await db.close()


@pytest.mark.anyio
async def test_backfill_recovers_missed_payment_hook() -> None:
    db = await _make_session()
    try:
        payment = WataPayment(
            user_id=1,
            payment_link_id="link-9",
            amount_kopeks=30_000,
            currency="RUB",
            status="Paid",
            is_paid=True,
            paid_at=datetime.utcnow(),
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)

        entry = await create_routing_log(
            db,
            user_id=None,
            source="balance_topup",
            amount_kopeks=30_000,
            requested_gateway="wata",
            weights={},
        )
        await mark_routing_issued(
            db,
            entry.id,
            gateway="wata",
            local_payment_id=payment.id,
            external_id="link-9",
            payment_url="u",
            fallback_used=False,
            attempts=[],
        )

        updated = await backfill_routing_paid_flags(
            db, since=datetime.utcnow() - timedelta(hours=1)
        )
        assert updated == 1
        await db.refresh(entry)
        assert entry.status == STATUS_PAID
    finally:
        await db.close()


@pytest.mark.anyio
async def test_routing_stats_group_by_requested_gateway() -> None:
    db = await _make_session()
    try:
        for gateway, paid in (("platega", True), ("platega", False), ("wata", True)):
            entry = await create_routing_log(
                db,
                user_id=None,
                source="balance_topup",
                amount_kopeks=10_000,
                requested_gateway=gateway,
                weights={},
            )
            await mark_routing_issued(
                db,
                entry.id,
                gateway=gateway,
                local_payment_id=entry.id,
                external_id=None,
                payment_url="u",
                fallback_used=False,
                attempts=[],
            )
            if paid:
                await mark_routing_paid(
                    db,
                    gateway=gateway,
                    local_payment_id=entry.id,
                    amount_kopeks=10_000,
                )

        stats = {
            row["gateway"]: row
            for row in await routing_stats(
                db, since=datetime.utcnow() - timedelta(hours=1)
            )
        }
        assert stats["platega"]["requested"] == 2
        assert stats["platega"]["paid"] == 1
        assert stats["wata"]["paid"] == 1
        assert stats["wata"]["paid_kopeks"] == 10_000
    finally:
        await db.close()
