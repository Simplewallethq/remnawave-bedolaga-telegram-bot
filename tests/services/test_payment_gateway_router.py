"""Тесты взвешенной маршрутизации счетов между универсальными шлюзами."""

from __future__ import annotations

import random
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.services.payment_gateway_router import (  # noqa: E402
    GATEWAY_PLATEGA,
    GATEWAY_WATA,
    GATEWAY_YOOKASSA,
    SOURCE_BALANCE,
    PaymentGatewayRouter,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def router() -> PaymentGatewayRouter:
    return PaymentGatewayRouter()


@pytest.fixture(autouse=True)
def routed_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Все три шлюза настроены, веса равные, лимиты выровнены."""
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_ENABLED", True, raising=False)
    monkeypatch.setattr(
        settings,
        "PAYMENT_ROUTER_SURFACES",
        "balance_topup,subscription_cart,tariff_partial,simple_pay,cabinet,miniapp",
        raising=False,
    )
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_FALLBACK_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_LOG_ENABLED", False, raising=False)

    monkeypatch.setattr(settings, "PLATEGA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_UNIVERSAL_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MERCHANT_ID", "m", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_SECRET", "s", raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "PLATEGA_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)

    monkeypatch.setattr(settings, "WATA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "WATA_ACCESS_TOKEN", "t", raising=False)
    monkeypatch.setattr(settings, "WATA_TERMINAL_PUBLIC_ID", "p", raising=False)
    monkeypatch.setattr(settings, "WATA_MIN_AMOUNT_KOPEKS", 10_000, raising=False)
    monkeypatch.setattr(settings, "WATA_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)

    monkeypatch.setattr(settings, "YOOKASSA_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SHOP_ID", "shop", raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SECRET_KEY", "key", raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", False, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_DEFAULT_RECEIPT_EMAIL", None, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_MIN_AMOUNT_KOPEKS", 5_000, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_MAX_AMOUNT_KOPEKS", 5_000_000, raising=False)


class DummyUser:
    def __init__(self, user_id: int = 7) -> None:
        self.id = user_id
        self.language = "ru"
        self.email = None


class DummySession:
    async def commit(self) -> None:  # pragma: no cover - логики нет
        return None

    async def refresh(self, *_: Any) -> None:  # pragma: no cover
        return None


class StubPaymentService:
    """Возвращает ответы в РЕАЛЬНОЙ форме каждого провайдера."""

    def __init__(self, results: Optional[Dict[str, Any]] = None) -> None:
        self.results = results or {}
        self.calls: List[str] = []

    def _result(self, gateway: str, default: Dict[str, Any]) -> Any:
        self.calls.append(gateway)
        if gateway in self.results:
            value = self.results[gateway]
            if isinstance(value, Exception):
                raise value
            return value
        return default

    async def create_platega_universal_payment(self, db, **kwargs: Any) -> Any:
        return self._result(
            GATEWAY_PLATEGA,
            {
                "local_payment_id": 11,
                "transaction_id": "tx-platega",
                "redirect_url": "https://platega.example/pay",
                "status": "PENDING",
            },
        )

    async def create_wata_payment(self, db, **kwargs: Any) -> Any:
        return self._result(
            GATEWAY_WATA,
            {
                "local_payment_id": 22,
                "payment_link_id": "link-wata",
                "payment_url": "https://wata.example/pay",
                "status": "Opened",
            },
        )

    async def create_yookassa_payment(self, **kwargs: Any) -> Any:
        return self._result(
            GATEWAY_YOOKASSA,
            {
                "local_payment_id": 33,
                "yookassa_payment_id": "yk-1",
                "confirmation_url": "https://yookassa.example/pay",
                "status": "pending",
            },
        )


# --------------------------------------------------------------- пригодность


def test_all_gateways_eligible_for_valid_amount(router: PaymentGatewayRouter) -> None:
    assert sorted(router.eligible_gateways(50_000)) == sorted(
        [GATEWAY_PLATEGA, GATEWAY_WATA, GATEWAY_YOOKASSA]
    )


def test_amount_below_minimum_excludes_gateways(
    router: PaymentGatewayRouter,
) -> None:
    # 60 ₽ проходит только по минимуму YooKassa (50 ₽), Platega и WATA требуют 100 ₽.
    assert router.eligible_gateways(6_000) == [GATEWAY_YOOKASSA]


def test_amount_above_maximum_excludes_gateway(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_MAX_AMOUNT_KOPEKS", 1_000_000, raising=False)
    eligible = router.eligible_gateways(1_500_000)
    assert GATEWAY_YOOKASSA not in eligible
    assert sorted(eligible) == sorted([GATEWAY_PLATEGA, GATEWAY_WATA])


def test_bypass_minimum_skips_min_but_not_max(
    router: PaymentGatewayRouter,
) -> None:
    assert sorted(router.eligible_gateways(1_000, bypass_minimum=True)) == sorted(
        [GATEWAY_PLATEGA, GATEWAY_WATA, GATEWAY_YOOKASSA]
    )
    assert router.eligible_gateways(9_000_000, bypass_minimum=True) == []


def test_zero_weight_disables_gateway(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 0, raising=False)
    assert GATEWAY_WATA not in router.eligible_gateways(50_000)


def test_yookassa_excluded_when_receipt_required_without_email(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Иначе YooKassa вернула бы ошибку КАЖДОМУ пользователю."""
    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", True, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_DEFAULT_RECEIPT_EMAIL", "", raising=False)
    assert GATEWAY_YOOKASSA not in router.eligible_gateways(50_000)


def test_yookassa_included_when_receipt_disabled(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_SEND_RECEIPT", False, raising=False)
    monkeypatch.setattr(settings, "YOOKASSA_DEFAULT_RECEIPT_EMAIL", None, raising=False)
    assert GATEWAY_YOOKASSA in router.eligible_gateways(50_000)


# ------------------------------------------------------------- границы `auto`


def test_combined_min_is_max_of_minimums(router: PaymentGatewayRouter) -> None:
    """Счёт должен быть оплатим ЛЮБЫМ шлюзом, который может выпасть."""
    assert router.combined_min_kopeks() == 10_000


def test_combined_max_is_min_of_maximums(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "YOOKASSA_MAX_AMOUNT_KOPEKS", 1_000_000, raising=False)
    assert router.combined_max_kopeks() == 1_000_000


# ------------------------------------------------------------------ выбор


def test_equal_weights_distribute_evenly(router: PaymentGatewayRouter) -> None:
    rng = random.Random(1234)
    counts: Dict[str, int] = {}
    draws = 3000
    for _ in range(draws):
        picked = router.pick_order(50_000, rng=rng)[0]
        counts[picked] = counts.get(picked, 0) + 1

    assert set(counts) == {GATEWAY_PLATEGA, GATEWAY_WATA, GATEWAY_YOOKASSA}
    expected = draws / 3
    for gateway, count in counts.items():
        assert abs(count - expected) < expected * 0.15, (gateway, counts)


def test_weights_are_respected(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 3, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 0, raising=False)

    rng = random.Random(99)
    counts: Dict[str, int] = {}
    draws = 2000
    for _ in range(draws):
        picked = router.pick_order(50_000, rng=rng)[0]
        counts[picked] = counts.get(picked, 0) + 1

    assert GATEWAY_YOOKASSA not in counts
    assert counts[GATEWAY_PLATEGA] > counts[GATEWAY_WATA] * 2


def test_pick_order_is_a_permutation_without_repeats(
    router: PaymentGatewayRouter,
) -> None:
    order = router.pick_order(50_000, rng=random.Random(7))
    assert sorted(order) == sorted([GATEWAY_PLATEGA, GATEWAY_WATA, GATEWAY_YOOKASSA])


def test_fallback_disabled_returns_single_gateway(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_FALLBACK_ENABLED", False, raising=False)
    assert len(router.pick_order(50_000, rng=random.Random(3))) == 1


def test_no_eligible_gateways_returns_empty_order(
    router: PaymentGatewayRouter,
) -> None:
    assert router.pick_order(1) == []


# ------------------------------------------------------------- поверхности


def test_surface_gate(router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "PAYMENT_ROUTER_SURFACES", "balance_topup", raising=False
    )
    assert router.is_enabled(SOURCE_BALANCE) is True
    assert router.is_enabled("miniapp") is False


def test_router_disabled_disables_all_surfaces(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_ENABLED", False, raising=False)
    assert router.is_enabled(SOURCE_BALANCE) is False


# ---------------------------------------------------------- создание счёта


@pytest.mark.anyio
async def test_create_invoice_normalizes_platega(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 0, raising=False)

    routed = await router.create_invoice(
        DummySession(),
        payment_service=StubPaymentService(),
        user=DummyUser(),
        amount_kopeks=50_000,
        source=SOURCE_BALANCE,
        description="test",
    )

    assert routed is not None
    assert routed.gateway == GATEWAY_PLATEGA
    assert routed.payment_url == "https://platega.example/pay"
    assert routed.external_id == "tx-platega"
    assert routed.check_callback == "check_platega_11"
    assert routed.fallback_used is False


@pytest.mark.anyio
async def test_create_invoice_normalizes_wata(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 0, raising=False)

    routed = await router.create_invoice(
        DummySession(),
        payment_service=StubPaymentService(),
        user=DummyUser(),
        amount_kopeks=50_000,
        source=SOURCE_BALANCE,
    )

    assert routed is not None
    assert routed.gateway == GATEWAY_WATA
    assert routed.payment_url == "https://wata.example/pay"
    assert routed.external_id == "link-wata"
    assert routed.check_callback == "check_wata_22"


@pytest.mark.anyio
async def test_create_invoice_normalizes_yookassa(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 0, raising=False)

    routed = await router.create_invoice(
        DummySession(),
        payment_service=StubPaymentService(),
        user=DummyUser(),
        amount_kopeks=50_000,
        source=SOURCE_BALANCE,
    )

    assert routed is not None
    assert routed.gateway == GATEWAY_YOOKASSA
    assert routed.payment_url == "https://yookassa.example/pay"
    assert routed.external_id == "yk-1"
    assert routed.check_callback == "check_yookassa_33"


@pytest.mark.anyio
async def test_fallback_used_when_first_gateway_returns_none(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 1, raising=False)

    service = StubPaymentService({GATEWAY_PLATEGA: None})
    routed = await router.create_invoice(
        DummySession(),
        payment_service=service,
        user=DummyUser(),
        amount_kopeks=50_000,
        source=SOURCE_BALANCE,
        rng=random.Random(0),
    )

    assert routed is not None
    if routed.requested_gateway == GATEWAY_PLATEGA:
        assert routed.gateway == GATEWAY_YOOKASSA
        assert routed.fallback_used is True
        assert len(routed.attempts) == 2
        assert routed.attempts[0]["ok"] is False


@pytest.mark.anyio
async def test_fallback_used_when_first_gateway_raises(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 1, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 0, raising=False)

    service = StubPaymentService({GATEWAY_PLATEGA: RuntimeError("boom")})
    routed = await router.create_invoice(
        DummySession(),
        payment_service=service,
        user=DummyUser(),
        amount_kopeks=50_000,
        source=SOURCE_BALANCE,
    )

    assert routed is None  # других шлюзов с ненулевым весом нет


@pytest.mark.anyio
async def test_all_gateways_failing_returns_none(
    router: PaymentGatewayRouter,
) -> None:
    service = StubPaymentService(
        {GATEWAY_PLATEGA: None, GATEWAY_WATA: None, GATEWAY_YOOKASSA: None}
    )
    routed = await router.create_invoice(
        DummySession(),
        payment_service=service,
        user=DummyUser(),
        amount_kopeks=50_000,
        source=SOURCE_BALANCE,
    )
    assert routed is None
    assert len(service.calls) == 3


@pytest.mark.anyio
async def test_error_dict_is_treated_as_failure(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YooKassa сигнализирует об ошибке словарём {'error': True}, а не None."""
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_PLATEGA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 0, raising=False)

    service = StubPaymentService({GATEWAY_YOOKASSA: {"error": True}})
    routed = await router.create_invoice(
        DummySession(),
        payment_service=service,
        user=DummyUser(),
        amount_kopeks=50_000,
        source=SOURCE_BALANCE,
    )
    assert routed is None


@pytest.mark.anyio
async def test_router_metadata_is_attached(
    router: PaymentGatewayRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_WATA", 0, raising=False)
    monkeypatch.setattr(settings, "PAYMENT_ROUTER_WEIGHT_YOOKASSA", 0, raising=False)

    captured: Dict[str, Any] = {}

    class CapturingService(StubPaymentService):
        async def create_platega_universal_payment(self, db, **kwargs: Any) -> Any:
            captured.update(kwargs.get("metadata") or {})
            return await super().create_platega_universal_payment(db, **kwargs)

    await router.create_invoice(
        DummySession(),
        payment_service=CapturingService(),
        user=DummyUser(),
        amount_kopeks=50_000,
        source=SOURCE_BALANCE,
    )

    assert captured["payment_router"]["gateway"] == GATEWAY_PLATEGA
    assert captured["payment_router"]["source"] == SOURCE_BALANCE
    assert captured["payment_router"]["attempt"] == 0
