"""Маппинг легаси-подписки на тарифный план при массовом переводе.

См. migrate_legacy_subscriptions_to_plans() в app/database/universal_migration.py.
"""

import pytest

from app.config import settings
from app.database.universal_migration import _pick_plan_for_legacy_subscription


SOLO = {"id": 1, "code": "solo", "device_limit": 1, "traffic_limit_gb": 0}
PLUS = {"id": 2, "code": "plus", "device_limit": 3, "traffic_limit_gb": 0}
PRO = {"id": 3, "code": "pro", "device_limit": 10, "traffic_limit_gb": 0}
PLANS = [SOLO, PLUS, PRO]


@pytest.mark.parametrize(
    "legacy_devices, expected_plan_id, expected_devices",
    [
        (None, SOLO["id"], 1),
        (0, SOLO["id"], 1),
        (1, SOLO["id"], 1),
        (2, PLUS["id"], 3),
        (3, PLUS["id"], 3),
        (4, PRO["id"], 10),
        (9, PRO["id"], 10),
        (10, PRO["id"], 10),
        # Легаси разрешал до MAX_DEVICES_LIMIT=20 устройств — выше планового лимита
        # берём топовый тариф, но сохраняем исходное число устройств.
        (11, PRO["id"], 11),
        (15, PRO["id"], 15),
        (20, PRO["id"], 20),
    ],
)
def test_plan_choice_never_lowers_device_limit(
    legacy_devices, expected_plan_id, expected_devices
):
    plan_id, device_limit, _ = _pick_plan_for_legacy_subscription(
        legacy_devices, 0, PLANS
    )
    assert plan_id == expected_plan_id
    assert device_limit == expected_devices
    assert device_limit >= (legacy_devices or 1)


@pytest.mark.parametrize("legacy_traffic_gb", [None, 0, 50, 100, 1000])
def test_unlimited_plan_always_grants_unlimited_traffic(legacy_traffic_gb):
    _, _, traffic_gb = _pick_plan_for_legacy_subscription(1, legacy_traffic_gb, PLANS)
    assert traffic_gb == 0


@pytest.mark.parametrize(
    "legacy_traffic_gb, expected",
    [
        (0, 0),      # безлимит у подписки не понижаем до планового лимита
        (10, 30),    # плановый лимит больше — апгрейд
        (30, 30),
        (100, 100),  # у подписки больше — оставляем как есть
    ],
)
def test_finite_plan_quota_is_never_a_downgrade(legacy_traffic_gb, expected):
    """Solo/Plus/Pro безлимитны, но каталог редактируем — проверяем и конечную квоту."""
    limited_plan = {"id": 9, "code": "app", "device_limit": 1, "traffic_limit_gb": 30}
    _, _, traffic_gb = _pick_plan_for_legacy_subscription(
        1, legacy_traffic_gb, [limited_plan]
    )
    assert traffic_gb == expected


def test_migration_is_disabled_by_default():
    """Чужие инсталляции не должны мигрировать подписки без явного включения."""
    assert settings.get_legacy_tariff_migration_mode() in {"off", "dry", "apply"}
    assert settings.__class__.model_fields["LEGACY_TARIFF_MIGRATION_MODE"].default == "off"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", []),
        ("123", [123]),
        ("123, 456", [123, 456]),
        ("123;456", [123, 456]),
        ("123, oops, 456", [123, 456]),
    ],
)
def test_pilot_telegram_ids_parsing(monkeypatch, raw, expected):
    monkeypatch.setattr(settings, "LEGACY_TARIFF_MIGRATION_TELEGRAM_IDS", raw, raising=False)
    assert settings.get_legacy_tariff_migration_telegram_ids() == expected
