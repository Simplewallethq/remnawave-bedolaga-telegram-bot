from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

import app.services.legacy_review_bonus_service as review_module
from app.services.interactive_notification_service import interactive_notification_service
from app.services.legacy_review_bonus_service import LegacyReviewBonusService


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


def test_slot_is_registered_for_2100_moscow_until_august_30() -> None:
    slots = {slot.key: slot for slot in interactive_notification_service.SLOTS}
    slot = slots[LegacyReviewBonusService.SLOT_KEY]

    assert slot.time.hour == 21
    assert slot.time.minute == 0


def test_message_and_google_play_button_match_campaign() -> None:
    service = LegacyReviewBonusService()
    button = service._keyboard().inline_keyboard[0][0]

    assert service.TEXT.startswith("<b>💡 +30 дней VPN — просто за отзыв!</b>\n\n")
    assert "теплых слов\n\n2. Отправь" in service.TEXT
    assert "@letosupportbot\n\n3. Получи" in service.TEXT
    assert "@letosupportbot" in service.TEXT
    assert button.text == "Жми и забирай подарок 👇"
    assert button.url == "https://play.google.com/store/apps/details?id=com.leto.split"


async def test_disabled_campaign_does_not_claim_recipients(monkeypatch) -> None:
    service = LegacyReviewBonusService()
    monkeypatch.setattr(service, "IS_ENABLED", False)
    claim_recipients = AsyncMock()
    monkeypatch.setattr(service, "_claim_recipients", claim_recipients)

    await service.run(SimpleNamespace(send_message=AsyncMock()))

    claim_recipients.assert_not_awaited()


async def test_recipient_query_uses_legacy_inactive_filters_and_excludes_attempts(monkeypatch) -> None:
    service = LegacyReviewBonusService()
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(all=lambda: [])
    monkeypatch.setattr(
        review_module.LegacyNotifyOnceService,
        "_legacy_cohort_condition",
        staticmethod(lambda: review_module.User.id > 0),
    )
    monkeypatch.setattr(
        review_module.LegacyNotifyOnceService,
        "_inactive_subscription_condition",
        staticmethod(lambda _now: review_module.User.id > 0),
    )

    await service._list_recipients(db)

    statement = db.execute.await_args.args[0]
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "LEFT OUTER JOIN subscriptions" in sql
    assert "NOT (EXISTS" in sql
    assert LegacyReviewBonusService.SLOT_KEY in sql
    assert statement._limit_clause.value == 10_000


async def test_claim_reserves_each_recipient_before_sending(monkeypatch) -> None:
    service = LegacyReviewBonusService()
    db = AsyncMock()
    db.begin = MagicMock(return_value=_TransactionContext())
    db.add = MagicMock()
    db.add_all = MagicMock()
    db.execute.side_effect = [
        SimpleNamespace(),
        SimpleNamespace(scalar_one_or_none=lambda: None),
        SimpleNamespace(scalar_one_or_none=lambda: None),
    ]
    db.flush = AsyncMock(side_effect=lambda: setattr(campaign_log, "id", 55))
    recipients = [
        SimpleNamespace(id=10, telegram_id=1000),
        SimpleNamespace(id=20, telegram_id=2000),
    ]
    campaign_log = None

    def add(log):
        nonlocal campaign_log
        if log.user_id is None:
            campaign_log = log

    db.add.side_effect = add
    monkeypatch.setattr(
        review_module,
        "AsyncSessionLocal",
        lambda: _AsyncSessionContext(db),
    )
    monkeypatch.setattr(service, "_list_recipients", AsyncMock(return_value=recipients))

    claim = await service._claim_recipients(date(2026, 8, 13))

    assert claim.log_id == 55
    assert claim.recipients == recipients
    reserved = db.add_all.call_args.args[0]
    assert [log.user_id for log in reserved] == [10, 20]
    assert all(log.status == "queued" for log in reserved)


async def test_failed_delivery_is_recorded_without_retry(monkeypatch) -> None:
    service = LegacyReviewBonusService()
    monkeypatch.setattr(service, "IS_ENABLED", True)
    recipient = SimpleNamespace(id=10, telegram_id=1000)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("blocked")))
    record_delivery = AsyncMock()
    finish_campaign = AsyncMock()

    monkeypatch.setattr(
        service,
        "_claim_recipients",
        AsyncMock(return_value=review_module._CampaignClaim(55, [recipient])),
    )
    monkeypatch.setattr(service, "_record_delivery", record_delivery)
    monkeypatch.setattr(service, "_finish_campaign", finish_campaign)
    monkeypatch.setattr(review_module.asyncio, "sleep", AsyncMock())

    await service.run(bot)

    record_delivery.assert_awaited_once_with(10, status="failed", error="blocked")
    finish_campaign.assert_awaited_once_with(
        55,
        status="processed",
        counters={"selected": 1, "sent": 0, "failed": 1},
    )
