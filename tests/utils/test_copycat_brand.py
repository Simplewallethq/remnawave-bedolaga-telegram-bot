"""Бренд по боту: профиль копикета, фолбэки, контекст, плейсхолдеры, домен ключа."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.branding import brand_scope, current_brand, mirror_profile_from_config, primary_profile
from app.branding.filters import not_copycat_user_clause
from app.config import settings
from app.localization.texts import apply_brand_placeholders, get_texts
from app.utils import bot_registry
from app.utils.subscription_utils import (
    get_display_subscription_link,
    get_raw_subscription_link,
    rewrite_subscription_host,
)

PRIMARY_ID = int(settings.BOT_TOKEN.split(":", 1)[0]) if settings.BOT_TOKEN.split(":", 1)[0].isdigit() else None
MIRROR_ID = 4001
COPYCAT_ID = 4002

COPYCAT_CFG = {
    "token": "x",
    "logo": "bot_images/shuka.jpg",
    "copycat": True,
    "name": "Shuka",
    "support": "@shuka_support",
    "privacy_url": "https://example.com/privacy",
    "terms_url": "https://example.com/terms",
    "channel_link": "https://t.me/shuka_channel",
    "subscription_domain": "https://sub.neutral.com/",
}


@pytest.fixture
def registry():
    bot_registry.clear()
    bot_registry.register_bot(
        MIRROR_ID,
        Path("bot_images/doctor.png"),
        brand_config={
            "token": "t",
            "logo": "bot_images/doctor.png",
            "image_url": "https://x",
            "name": "Doctor",
            "copycat": False,
        },
    )
    bot_registry.register_bot(COPYCAT_ID, Path("bot_images/shuka.jpg"), brand_config=COPYCAT_CFG)
    yield bot_registry
    bot_registry.clear()


def test_mirror_opted_out_keeps_primary_brand_with_own_logo(registry):
    profile = registry.get_brand_for_bot(MIRROR_ID)
    base = primary_profile()
    assert profile.is_copycat is False
    assert profile.name == base.name == "Leto"
    assert profile.privacy_url == base.privacy_url
    assert profile.support == base.support
    assert profile.logo == "bot_images/doctor.png"
    assert registry.copycat_bot_ids() == frozenset({COPYCAT_ID})


def test_copycat_profile_and_fallbacks(registry):
    profile = registry.get_brand_for_bot(COPYCAT_ID)
    assert profile.is_copycat and profile.name == "Shuka"
    assert profile.support_url == "https://t.me/shuka_support"
    assert profile.subscription_domain == "sub.neutral.com"
    assert profile.has_own_app is False and profile.rays_enabled is False

    sparse = mirror_profile_from_config(5, {"name": "Mini"})
    base = primary_profile()
    assert sparse.is_copycat is True
    assert sparse.privacy_url == base.privacy_url
    assert sparse.terms_url == base.terms_url
    # Саппорт и домен ключа общие для всех витрин, а не от основного бота.
    assert sparse.support == settings.COPYCAT_SUPPORT_USERNAME
    assert sparse.subscription_domain == settings.COPYCAT_SUBSCRIPTION_DOMAIN
    assert sparse.logo == base.logo
    # Канал основного бренда не наследуется.
    assert sparse.channel_link is None and sparse.channel_enabled is False


def test_mirror_without_a_name_stays_on_the_primary_brand(registry):
    profile = mirror_profile_from_config(6, {"logo": "x.png"})
    assert profile.is_copycat is False and profile.name == "Leto"


def test_mirror_is_a_copycat_unless_it_opts_out(registry):
    # Боты из mirror_bots.yaml — витрины: отдельного флага для этого не нужно.
    assert mirror_profile_from_config(8, {"name": "Shield"}).is_copycat is True
    assert mirror_profile_from_config(9, {"name": "Shield", "copycat": False}).is_copycat is False


def test_brand_name_does_not_double_the_vpn_word(registry):
    texts = get_texts("ru")
    for name, expected in (("Adrenalin VPN", "Adrenalin VPN"), ("Doctor", "Doctor VPN")):
        profile = mirror_profile_from_config(10, {"name": name})
        rendered = apply_brand_placeholders("Скачай {project_name} VPN сейчас", profile)
        assert rendered == f"Скачай {expected} сейчас"
    assert texts is not None


def test_unknown_bot_and_no_scope_resolve_to_primary(registry):
    assert registry.get_brand_for_bot(999999).is_copycat is False
    assert current_brand().name == "Leto"


def test_brand_scope_switches_texts_and_settings_wrappers(registry):
    texts = get_texts("ru")
    with brand_scope(COPYCAT_ID):
        assert current_brand().name == "Shuka"
        assert settings.get_support_contact_url() == "https://t.me/shuka_support"
        assert settings.is_rebranded() is True
        assert settings.brand_has_own_app() is False
        assert settings.is_brand_channel_enabled() is True
        rendered = texts.t("X_TEST", "Скачай Leto VPN — {project_name} — {support_contact} {days}")
        assert rendered == "Скачай Shuka VPN — Shuka — @shuka_support {days}"
        legal = texts.MAIN_MENU_LEGAL_LINKS
        assert "https://example.com/privacy" in legal and "https://example.com/terms" in legal
        assert "telegra.ph" not in legal
    # После выхода из скоупа — основной бренд.
    assert settings.is_rebranded() is False
    assert "telegra.ph" in texts.MAIN_MENU_LEGAL_LINKS


def test_placeholder_line_is_dropped_when_empty(registry):
    empty = mirror_profile_from_config(7, {"copycat": True, "name": "Mini"})
    text = "Привет\nКанал: {channel_link}\nПока"
    assert apply_brand_placeholders(text, empty) == "Привет\nПока"
    assert apply_brand_placeholders("Без плейсхолдеров {days}", empty) == "Без плейсхолдеров {days}"


def test_rewrite_subscription_host_keeps_path_and_query():
    assert (
        rewrite_subscription_host("http://sub.letovpn.com/sub/abc?x=1", "sub.neutral.com")
        == "https://sub.neutral.com/sub/abc?x=1"
    )
    assert rewrite_subscription_host("https://a/b", None) == "https://a/b"
    assert rewrite_subscription_host(None, "x") is None


def test_copycat_key_link_uses_neutral_domain_and_never_crypto(registry, monkeypatch):
    sub = SimpleNamespace(
        subscription_url="https://sub.letovpn.com/sub/token",
        subscription_crypto_link="happ://crypt/xyz",
    )
    monkeypatch.setattr(settings, "CONNECT_BUTTON_MODE", "happ_cryptolink")
    with brand_scope(COPYCAT_ID):
        assert get_raw_subscription_link(sub) == "https://sub.neutral.com/sub/token"
        assert get_display_subscription_link(sub) == "https://sub.neutral.com/sub/token"
    with brand_scope(MIRROR_ID):
        assert get_raw_subscription_link(sub) == "https://sub.letovpn.com/sub/token"
        assert get_display_subscription_link(sub) == "happ://crypt/xyz"


def test_not_copycat_clause(registry):
    clause = str(not_copycat_user_clause().compile(compile_kwargs={"literal_binds": True}))
    assert "users.bot_id IS NULL" in clause and str(COPYCAT_ID) in clause
    registry.clear()
    assert str(not_copycat_user_clause()) == "true"


def test_rays_gates_follow_user_bot(registry, monkeypatch):
    from app.utils.user_utils import is_rays_program_available_for, is_rays_shop_available_for

    monkeypatch.setattr(settings, "RAYS_PROGRAM_ENABLED", True)
    monkeypatch.setattr(settings, "RAYS_SHOP_ENABLED", True)
    monkeypatch.setattr(settings, "BRAND_RAYS_ENABLED", True)
    leto_user = SimpleNamespace(bot_id=MIRROR_ID, is_partner=False)
    copycat_user = SimpleNamespace(bot_id=COPYCAT_ID, is_partner=False)
    assert is_rays_program_available_for(leto_user) is True
    assert is_rays_shop_available_for(leto_user) is True
    assert is_rays_program_available_for(copycat_user) is False
    assert is_rays_shop_available_for(copycat_user) is False


def test_mirror_bots_config_passes_copycat_fields(tmp_path, monkeypatch):
    yaml_path = tmp_path / "mirror_bots.yaml"
    yaml_path.write_text(
        "bots:\n"
        "  - token: '1:a'\n    logo: doctor.png\n    image_url: https://x\n"
        "  - token: '2:b'\n    copycat: true\n    name: Shuka\n    support: '@s'\n"
        "  - token: '3:c'\n    copycat: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "MIRROR_BOTS_FILE", str(yaml_path))
    bots = settings.get_mirror_bots()
    assert [b["token"] for b in bots] == ["1:a", "2:b", "3:c"]
    assert "image_url" not in bots[0] and "copycat" not in bots[0]
    assert bots[1]["copycat"] is True and bots[1]["name"] == "Shuka" and bots[1]["support"] == "@s"
    # Первая запись — прод-формат без имени: имя подставляет app/bot.py из Telegram.
    assert mirror_profile_from_config(1, bots[0]).is_copycat is False
    assert mirror_profile_from_config(1, {**bots[0], "name": "Doctor"}).is_copycat is True
    assert mirror_profile_from_config(2, bots[1]).is_copycat is True
    assert mirror_profile_from_config(3, bots[2]).is_copycat is False


@pytest.mark.anyio
async def test_brand_context_middleware_sets_and_resets_scope(registry):
    from app.middlewares.brand_context import BrandContextMiddleware

    seen = {}

    async def handler(event, data):
        seen["name"] = current_brand().name
        return "ok"

    middleware = BrandContextMiddleware()
    result = await middleware(handler, object(), {"bot": SimpleNamespace(id=COPYCAT_ID)})
    assert result == "ok" and seen["name"] == "Shuka"
    assert current_brand().name == "Leto"


def _button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def test_android_keyboard_offers_happ_instead_of_leto_app(registry, monkeypatch):
    from app.keyboards.inline import get_connect_android_keyboard, get_onboarding_connection_keyboard

    monkeypatch.setattr(settings, "LETO_APP_DOWNLOAD_LINK_ANDROID", "https://play.google.com/store/apps/details?id=com.leto.split")
    with brand_scope(MIRROR_ID):
        texts = _button_texts(get_connect_android_keyboard("ru", 1))
        assert any("Leto" in t for t in texts)
        assert not any("Happ" in t and "Скачать" in t for t in texts)
    with brand_scope(COPYCAT_ID):
        texts = _button_texts(get_connect_android_keyboard("ru", 1))
        assert "🤖 Скачать Happ" in texts
        assert not any("Leto" in t or "Shuka" in t for t in texts)
        onboarding = _button_texts(
            get_onboarding_connection_keyboard("android", "ru", "https://sub.neutral.com/sub/x", telegram_id=1)
        )
        assert "🤖 Скачать Happ" in onboarding
        assert not any("Shuka" in t for t in onboarding)


def test_share_links_and_key_label_for_copycat(registry, monkeypatch):
    from app.branding.apps import HAPP_ANDROID_PLAY_URL, access_key_label
    from app.handlers.subscription.purchase import _get_share_app_links

    monkeypatch.setattr(settings, "LETO_APP_DOWNLOAD_LINK_ANDROID", "https://play.google.com/store/apps/details?id=com.leto.split")
    texts = get_texts("ru")
    with brand_scope(MIRROR_ID):
        assert "com.leto.split" in _get_share_app_links()["android"]
        assert "Leto, Happ, Incy" in access_key_label(texts)
    with brand_scope(COPYCAT_ID):
        assert _get_share_app_links()["android"] == HAPP_ANDROID_PLAY_URL
        assert access_key_label(texts) == "<b>Твой ключ доступа</b> (для приложений Happ, Incy)"


@pytest.mark.anyio
async def test_copycat_rules_text_links_to_its_documents(registry):
    from app.localization.texts import get_rules

    with brand_scope(COPYCAT_ID):
        rules = await get_rules("ru")
        assert "https://example.com/terms" in rules and "https://example.com/privacy" in rules
        assert "Shuka" in rules
        assert get_texts("ru").RULES_TEXT == rules
