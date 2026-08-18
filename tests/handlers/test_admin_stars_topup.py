from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.handlers.admin import stars_topup
from app.keyboards.admin import get_admin_root_keyboard


def test_admin_root_contains_stars_topup_button():
    keyboard = get_admin_root_keyboard("ru")

    assert ("⭐ Пополнить Stars", "admin_stars_topup") in [
        (button.text, button.callback_data)
        for row in keyboard.inline_keyboard
        for button in row
    ]


def test_admin_stars_payload_round_trip(monkeypatch):
    monkeypatch.setattr(stars_topup.secrets, "token_hex", lambda _size: "a" * 16)

    payload = stars_topup.build_admin_stars_payload(
        bot_id=100,
        admin_id=200,
        stars_amount=500,
    )

    assert stars_topup.parse_admin_stars_payload(payload) == (
        stars_topup.AdminStarsPayload(
            bot_id=100,
            admin_id=200,
            stars_amount=500,
            nonce="a" * 16,
        )
    )


def test_star_balance_format_preserves_integer_zeroes_and_nanostars():
    assert stars_topup._format_star_amount(
        SimpleNamespace(amount=750, nanostar_amount=0)
    ) == "750"
    assert stars_topup._format_star_amount(
        SimpleNamespace(amount=750, nanostar_amount=100_000_000)
    ) == "750.1"


async def test_invoice_is_bound_and_protected_from_forwarding(monkeypatch):
    monkeypatch.setattr(stars_topup.secrets, "token_hex", lambda _size: "a" * 16)
    message = SimpleNamespace(
        bot=SimpleNamespace(id=100),
        answer_invoice=AsyncMock(),
    )

    await stars_topup._send_topup_invoice(message, admin_id=200, amount=500)

    kwargs = message.answer_invoice.await_args.kwargs
    parsed = stars_topup.parse_admin_stars_payload(kwargs["payload"])
    assert parsed.bot_id == 100
    assert parsed.admin_id == 200
    assert parsed.stars_amount == 500
    assert kwargs["currency"] == "XTR"
    assert len(kwargs["prices"]) == 1
    assert kwargs["prices"][0].amount == 500
    assert kwargs["protect_content"] is True
    assert kwargs["start_parameter"].startswith("admin_stars_")


async def test_pre_checkout_accepts_only_bound_admin(monkeypatch):
    monkeypatch.setattr(
        type(stars_topup.settings),
        "is_admin",
        lambda _settings, user_id: user_id == 200,
    )
    payload = "admin_stars_topup:100:200:500:" + ("a" * 16)
    query = SimpleNamespace(
        invoice_payload=payload,
        from_user=SimpleNamespace(id=200),
        bot=SimpleNamespace(id=100),
        currency="XTR",
        total_amount=500,
        answer=AsyncMock(),
    )

    await stars_topup.handle_admin_stars_pre_checkout(query)

    query.answer.assert_awaited_once_with(ok=True)


async def test_pre_checkout_rejects_forwarded_invoice(monkeypatch):
    monkeypatch.setattr(
        type(stars_topup.settings),
        "is_admin",
        lambda _settings, _user_id: True,
    )
    payload = "admin_stars_topup:100:200:500:" + ("a" * 16)
    query = SimpleNamespace(
        invoice_payload=payload,
        from_user=SimpleNamespace(id=201),
        bot=SimpleNamespace(id=100),
        currency="XTR",
        total_amount=500,
        answer=AsyncMock(),
    )

    await stars_topup.handle_admin_stars_pre_checkout(query)

    assert query.answer.await_args.kwargs["ok"] is False


async def test_successful_admin_topup_is_recorded_without_user_balance_credit(monkeypatch):
    payload = "admin_stars_topup:100:200:500:" + ("a" * 16)
    payment = SimpleNamespace(
        invoice_payload=payload,
        currency="XTR",
        total_amount=500,
        telegram_payment_charge_id="charge-1",
        provider_payment_charge_id="",
    )
    bot = SimpleNamespace(
        id=100,
        get_my_star_balance=AsyncMock(
            return_value=SimpleNamespace(amount=750, nanostar_amount=0)
        ),
    )
    message = SimpleNamespace(
        successful_payment=payment,
        from_user=SimpleNamespace(id=200),
        bot=bot,
        answer=AsyncMock(),
    )
    record = SimpleNamespace(telegram_payment_charge_id="charge-1")
    record_topup = AsyncMock(return_value=(record, True))
    monkeypatch.setattr(stars_topup, "_record_successful_topup", record_topup)
    monkeypatch.setattr(
        type(stars_topup.settings),
        "is_admin",
        lambda _settings, user_id: user_id == 200,
    )

    await stars_topup.handle_admin_stars_successful_payment(
        message,
        AsyncMock(),
        db_user=SimpleNamespace(id=7),
    )

    record_topup.assert_awaited_once()
    assert record_topup.await_args.kwargs["stars_amount"] == 500
    assert record_topup.await_args.kwargs["admin_user_id"] == 7
    assert "500 ⭐" in message.answer.await_args.args[0]
    assert "750 ⭐" in message.answer.await_args.args[0]
