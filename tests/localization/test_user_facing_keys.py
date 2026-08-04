import json
from pathlib import Path


LOCALES_DIR = Path("app/localization/locales")

USER_FACING_KEYS = {
    "AUTH_START_REQUIRED",
    "AUTH_ACCOUNT_BLOCKED",
    "THROTTLE_MESSAGE",
    "SUBSCRIPTION_REQUIRED",
    "PAYMENT_CREATE_ERROR",
    "PAYMENT_CHECK_STATUS",
    "PAYMENT_PAY_BY_CARD",
    "PAYMENT_PAY_CRYPTOBOT",
    "PAYMENT_STARS_PAY",
    "SIMPLE_SUBSCRIPTION_UNAVAILABLE",
    "NOTIFICATION_TRIAL_ENDING",
    "NOTIFICATION_COMPLETE_PAYMENT_BUTTON",
    "REFERRAL_HEADER",
    "REFERRAL_HOW_SHOP",
    "RAYS_SHOP_TITLE",
    "RAYS_CLAIM_COMPLETED_USER_NOTIFY",
    "FEEDBACK_COMPLETED_OR_NOT_FOUND",
}


def test_new_user_facing_keys_have_russian_and_english_translations() -> None:
    ru = json.loads((LOCALES_DIR / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8"))

    for key in USER_FACING_KEYS:
        assert ru.get(key), f"Missing Russian translation for {key}"
        assert en.get(key), f"Missing English translation for {key}"


def test_non_admin_russian_keys_have_english_translations() -> None:
    ru = json.loads((LOCALES_DIR / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8"))

    missing = sorted(key for key in ru if not key.startswith("ADMIN_") and key not in en)

    assert not missing, f"Missing English translations: {', '.join(missing)}"
