from types import SimpleNamespace
from pathlib import Path
import sys

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.services.system_settings_service import bot_configuration_service


async def test_env_override_prevents_set_value(monkeypatch):
    bot_configuration_service.initialize_definitions()

    env_value = "env_support"
    monkeypatch.setattr(settings, "SUPPORT_USERNAME", env_value)
    original_values = dict(bot_configuration_service._original_values)
    original_values["SUPPORT_USERNAME"] = env_value
    monkeypatch.setattr(bot_configuration_service, "_original_values", original_values)

    env_keys = set(bot_configuration_service._env_override_keys)
    env_keys.add("SUPPORT_USERNAME")
    monkeypatch.setattr(bot_configuration_service, "_env_override_keys", env_keys)
    monkeypatch.setattr(bot_configuration_service, "_overrides_raw", {})

    async def fake_upsert(db, key, value, description=None):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        "app.services.system_settings_service.upsert_system_setting",
        fake_upsert,
    )

    await bot_configuration_service.set_value(
        object(),
        "SUPPORT_USERNAME",
        "db_support",
    )

    assert settings.SUPPORT_USERNAME == env_value
    assert not bot_configuration_service.has_override("SUPPORT_USERNAME")


async def test_env_override_prevents_reset_value(monkeypatch):
    bot_configuration_service.initialize_definitions()

    env_value = "env_support"
    monkeypatch.setattr(settings, "SUPPORT_USERNAME", env_value)
    original_values = dict(bot_configuration_service._original_values)
    original_values["SUPPORT_USERNAME"] = env_value
    monkeypatch.setattr(bot_configuration_service, "_original_values", original_values)

    env_keys = set(bot_configuration_service._env_override_keys)
    env_keys.add("SUPPORT_USERNAME")
    monkeypatch.setattr(bot_configuration_service, "_env_override_keys", env_keys)
    monkeypatch.setattr(bot_configuration_service, "_overrides_raw", {"SUPPORT_USERNAME": "db"})

    async def fake_delete(db, key):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        "app.services.system_settings_service.delete_system_setting",
        fake_delete,
    )

    await bot_configuration_service.reset_value(
        object(),
        "SUPPORT_USERNAME",
    )

    assert settings.SUPPORT_USERNAME == env_value
    assert not bot_configuration_service.has_override("SUPPORT_USERNAME")


async def test_initialize_skips_db_value_for_env_override(monkeypatch):
    bot_configuration_service.initialize_definitions()

    env_value = "env_support"
    monkeypatch.setattr(settings, "SUPPORT_USERNAME", env_value)
    original_values = dict(bot_configuration_service._original_values)
    original_values["SUPPORT_USERNAME"] = env_value
    monkeypatch.setattr(bot_configuration_service, "_original_values", original_values)

    env_keys = set(bot_configuration_service._env_override_keys)
    env_keys.add("SUPPORT_USERNAME")
    monkeypatch.setattr(bot_configuration_service, "_env_override_keys", env_keys)
    monkeypatch.setattr(bot_configuration_service, "_overrides_raw", {})

    class DummyResult:
        def scalars(self):
            return self

        def all(self):
            return [SimpleNamespace(key="SUPPORT_USERNAME", value="db_support")]

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        async def execute(self, query):  # noqa: ANN001
            return DummyResult()

    monkeypatch.setattr(
        "app.services.system_settings_service.AsyncSessionLocal",
        lambda: DummySession(),
    )

    async def fake_sync():
        return True

    monkeypatch.setattr(
        "app.services.system_settings_service.ensure_default_web_api_token",
        fake_sync,
        raising=False,
    )

    await bot_configuration_service.initialize()

    assert settings.SUPPORT_USERNAME == env_value
    assert "SUPPORT_USERNAME" not in bot_configuration_service._overrides_raw
    assert not bot_configuration_service.has_override("SUPPORT_USERNAME")


async def test_set_value_applies_without_env_override(monkeypatch):
    bot_configuration_service.initialize_definitions()

    monkeypatch.setattr(bot_configuration_service, "_env_override_keys", set())
    monkeypatch.setattr(bot_configuration_service, "_overrides_raw", {})

    initial_value = True
    target_value = False

    monkeypatch.setattr(settings, "SUPPORT_MENU_ENABLED", initial_value)
    original_values = dict(bot_configuration_service._original_values)
    original_values["SUPPORT_MENU_ENABLED"] = initial_value
    monkeypatch.setattr(bot_configuration_service, "_original_values", original_values)

    async def fake_upsert(db, key, value, description=None):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        "app.services.system_settings_service.upsert_system_setting",
        fake_upsert,
    )

    await bot_configuration_service.set_value(
        object(),
        "SUPPORT_MENU_ENABLED",
        target_value,
    )

    assert settings.SUPPORT_MENU_ENABLED is target_value
    assert bot_configuration_service.has_override("SUPPORT_MENU_ENABLED")


def test_payment_router_keys_are_runtime_editable() -> None:
    """Ключи роутера не должны попадать в .env.

    ENV_OVERRIDE_KEYS замораживает любой ключ, присутствующий в окружении:
    BotConfigurationService сохранит значение в БД, но НЕ применит его. Для
    рубильника и весов это означало бы невозможность отката без деплоя.
    """
    from app.config import ENV_OVERRIDE_KEYS

    router_keys = {
        "PAYMENT_ROUTER_ENABLED",
        "PAYMENT_ROUTER_SURFACES",
        "PAYMENT_ROUTER_WEIGHT_PLATEGA",
        "PAYMENT_ROUTER_WEIGHT_WATA",
        "PAYMENT_ROUTER_WEIGHT_YOOKASSA",
        "PAYMENT_ROUTER_FALLBACK_ENABLED",
        "PAYMENT_ROUTER_LOG_ENABLED",
    }
    frozen = router_keys & ENV_OVERRIDE_KEYS
    assert not frozen, (
        f"Ключи роутера заданы в окружении и не будут применяться из БД: {frozen}"
    )


def test_payment_router_settings_land_in_own_category() -> None:
    """PAYMENT_ROUTER_ не должен схлопнуться в общую категорию PAYMENT."""
    from app.services.system_settings_service import BotConfigurationService

    BotConfigurationService.initialize_definitions()
    definition = BotConfigurationService.get_definition("PAYMENT_ROUTER_ENABLED")
    assert definition.category_key == "PAYMENT_ROUTER"
