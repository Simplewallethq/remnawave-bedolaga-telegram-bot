from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services import referral_service  # noqa: E402


def _message_text(send_call):
    return send_call.args[1]


def _referral_button(send_call):
    markup = send_call.kwargs["reply_markup"]
    return markup.inline_keyboard[0][0]


async def test_commission_accrues_on_first_topup(monkeypatch):
    user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name="Test User",
        referred_by_id=2,
        has_made_first_topup=False,
        referral_total_topup_kopeks=0,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name="Referrer",
        referral_commission_percent=25,
        qualified_referrals_count=0,
    )

    db = SimpleNamespace(
        commit=AsyncMock(),
        execute=AsyncMock(),
    )

    get_user_mock = AsyncMock(side_effect=[user, referrer])
    monkeypatch.setattr(referral_service, "get_user_by_id", get_user_mock)
    add_user_balance_mock = AsyncMock()
    monkeypatch.setattr(referral_service, "add_user_balance", add_user_balance_mock)
    create_referral_earning_mock = AsyncMock()
    monkeypatch.setattr(referral_service, "create_referral_earning", create_referral_earning_mock)
    monkeypatch.setattr("app.services.cabinet_notification_service.notify", AsyncMock())

    result = await referral_service.process_referral_topup(db, user.id, 15000)

    assert result is True
    assert user.has_made_first_topup is True
    assert user.referral_total_topup_kopeks == 15000
    assert referrer.qualified_referrals_count == 0

    db.commit.assert_awaited_once()

    add_user_balance_mock.assert_awaited_once()
    add_call = add_user_balance_mock.await_args
    assert add_call.args[1] is referrer
    assert add_call.args[2] == 3750
    assert add_call.args[3] == "Комиссия 25% с пополнения Test User"
    assert add_call.kwargs.get("bot") is None

    create_referral_earning_mock.assert_awaited_once()
    earning_call = create_referral_earning_mock.await_args
    assert earning_call.kwargs["amount_kopeks"] == 3750
    assert earning_call.kwargs["reason"] == "referral_commission"
    db.execute.assert_not_awaited()


async def test_registration_sends_updated_referral_notifications(monkeypatch):
    new_user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name="Иван <VPN>",
        referred_by_id=2,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name="Анна & Co",
        referral_commission_percent=30,
    )
    db = SimpleNamespace()
    bot = SimpleNamespace(send_message=AsyncMock())

    monkeypatch.setattr(
        referral_service,
        "get_user_by_id",
        AsyncMock(side_effect=[new_user, referrer]),
    )
    monkeypatch.setattr(
        "app.services.referral_contest_service.referral_contest_service.on_referral_registration",
        AsyncMock(),
    )
    monkeypatch.setattr("app.services.cabinet_notification_service.notify", AsyncMock())

    result = await referral_service.process_referral_registration(
        db,
        new_user.id,
        referrer.id,
        bot,
    )

    assert result is True
    assert bot.send_message.await_count == 2

    invited_call, inviter_call = bot.send_message.await_args_list
    assert invited_call.args[0] == new_user.telegram_id
    assert _message_text(invited_call) == (
        "🎉 <b>С прибытием!</b>\n"
        "Ты пришёл по приглашению Анна &amp; Co. "
        "Осваивайся — 3 дня VPN уже твои."
    )
    assert invited_call.kwargs["parse_mode"] == "HTML"
    assert invited_call.kwargs["reply_markup"] is None

    assert inviter_call.args[0] == referrer.telegram_id
    assert _message_text(inviter_call) == (
        "👥 <b>+1 в команду</b>\n"
        "Иван &lt;VPN&gt; пришёл по твоей ссылке — теперь тебе капает "
        "30% с каждого его платежа. Деньги можно вывести "
        "на карту (от 3000₽) или потратить на подписку.\n"
        "Позови ещё — ссылка та же."
    )
    button = _referral_button(inviter_call)
    assert button.text == "🔗 Моя ссылка"
    assert button.callback_data == "referral"


async def test_topup_sends_updated_commission_notification(monkeypatch):
    user = SimpleNamespace(
        id=1,
        telegram_id=101,
        full_name="Пётр <Friend>",
        referred_by_id=2,
        has_made_first_topup=False,
        referral_total_topup_kopeks=0,
    )
    referrer = SimpleNamespace(
        id=2,
        telegram_id=202,
        full_name="Referrer",
        referral_commission_percent=20,
        qualified_referrals_count=0,
    )
    db = SimpleNamespace(commit=AsyncMock())
    bot = SimpleNamespace(send_message=AsyncMock())

    monkeypatch.setattr(
        referral_service,
        "get_user_by_id",
        AsyncMock(side_effect=[user, referrer]),
    )
    monkeypatch.setattr(referral_service, "add_user_balance", AsyncMock())
    monkeypatch.setattr(referral_service, "create_referral_earning", AsyncMock())
    monkeypatch.setattr("app.services.cabinet_notification_service.notify", AsyncMock())

    result = await referral_service.process_referral_topup(db, user.id, 10000, bot)

    assert result is True
    bot.send_message.assert_awaited_once()
    send_call = bot.send_message.await_args
    assert send_call.args[0] == referrer.telegram_id
    assert _message_text(send_call) == (
        "💰 <b>+20 ₽</b>\n"
        "Пётр &lt;Friend&gt; оплатил — твои 20% уже на балансе. "
        "Накопишь 3000₽ — выведешь на карту.\n\n"
        "Зови ещё друзей и зарабатывай больше."
    )
    assert send_call.kwargs["parse_mode"] == "HTML"
    button = _referral_button(send_call)
    assert button.text == "🔗 Моя ссылка"
    assert button.callback_data == "referral"
