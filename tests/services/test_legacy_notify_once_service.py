from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

import app.services.legacy_notify_once_service as notify_module
from app.services.interactive_notification_service import interactive_notification_service
from app.services.legacy_notify_once_service import LegacyNotifyOnceService


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _TransactionContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return None


def test_slot_is_registered_as_one_off_campaign() -> None:
    slots = {slot.key: slot for slot in interactive_notification_service.SLOTS}
    slot = slots[LegacyNotifyOnceService.SLOT_KEY]

    assert slot.is_one_off
    assert slot.run_at == LegacyNotifyOnceService.RUN_AT


def test_message_has_only_the_first_line_bold_and_extend_button() -> None:
    service = LegacyNotifyOnceService()
    keyboard = service._keyboard()

    assert service.TEXT.startswith("<b>👍 Хорошие новости о ценах</b>\n\n")
    assert service.TEXT.count("<b>") == 1
    assert keyboard.inline_keyboard[0][0].text == "Продлить за 220₽"
    assert keyboard.inline_keyboard[0][0].callback_data == "subscription_extend"


async def test_recipient_query_uses_sql_cohort_and_inactive_subscription_conditions(monkeypatch) -> None:
    service = LegacyNotifyOnceService()
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(all=lambda: [])
    monkeypatch.setattr(
        notify_module,
        "AsyncSessionLocal",
        lambda: _AsyncSessionContext(db),
    )
    monkeypatch.setattr(
        type(notify_module.settings),
        "get_tariffs_new_pricing_cutoff",
        lambda _self: datetime(2026, 6, 9, 12, 0),
    )

    await service._list_recipients(123)

    statement = db.execute.await_args.args[0]
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "LEFT OUTER JOIN subscriptions" in sql
    assert "users.id > 123" in sql
    assert "lower(users.tariff_pricing_cohort_override) = 'legacy'" in sql
    assert "users.created_at < '2026-06-09 12:00:00'" in sql
    assert "subscriptions.status != 'active'" in sql
    assert statement._limit_clause.value == 500


async def test_any_campaign_log_is_terminal(monkeypatch) -> None:
    service = LegacyNotifyOnceService()
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: 1)
    monkeypatch.setattr(
        notify_module,
        "AsyncSessionLocal",
        lambda: _AsyncSessionContext(db),
    )

    assert await service.is_terminal()
    statement = db.execute.await_args.args[0]
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert LegacyNotifyOnceService.SLOT_KEY in sql
    assert "status" not in sql


async def test_campaign_claim_locks_table_before_creating_log(monkeypatch) -> None:
    service = LegacyNotifyOnceService()
    db = AsyncMock()
    db.begin = MagicMock(return_value=_TransactionContext())
    db.add = MagicMock()
    db.execute.side_effect = [
        SimpleNamespace(),
        SimpleNamespace(scalar_one_or_none=lambda: None),
    ]

    async def refresh(log):
        log.id = 55

    db.refresh.side_effect = refresh
    monkeypatch.setattr(
        notify_module,
        "AsyncSessionLocal",
        lambda: _AsyncSessionContext(db),
    )

    assert await service._start_campaign() == 55

    lock_statement = db.execute.await_args_list[0].args[0]
    assert str(lock_statement) == (
        "LOCK TABLE interactive_notification_logs IN SHARE ROW EXCLUSIVE MODE"
    )
    db.add.assert_called_once()


async def test_run_sends_keyset_batches_and_writes_only_final_counts(monkeypatch) -> None:
    service = LegacyNotifyOnceService()
    first_batch = [
        SimpleNamespace(id=10, telegram_id=1000),
        SimpleNamespace(id=20, telegram_id=2000),
    ]
    second_batch = [SimpleNamespace(id=30, telegram_id=3000)]
    list_recipients = AsyncMock(side_effect=[first_batch, second_batch, []])
    finish_campaign = AsyncMock()
    bot = SimpleNamespace(send_message=AsyncMock())

    monkeypatch.setattr(service, "_start_campaign", AsyncMock(return_value=55))
    monkeypatch.setattr(service, "_list_recipients", list_recipients)
    monkeypatch.setattr(service, "_finish_campaign", finish_campaign)
    monkeypatch.setattr(notify_module.asyncio, "sleep", AsyncMock())

    await service.run(bot)

    assert [call.args[0] for call in list_recipients.await_args_list] == [0, 20, 30]
    assert bot.send_message.await_count == 3
    finish_campaign.assert_awaited_once_with(
        55,
        status="processed",
        counters={"selected": 3, "sent": 3, "failed": 0, "skipped": 0, "batches": 2},
    )
