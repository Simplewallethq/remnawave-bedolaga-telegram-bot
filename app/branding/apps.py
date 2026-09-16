"""Клиентские приложения по бренду: витрина без своего приложения ведёт в Happ/Incy."""

from __future__ import annotations

from app.branding.context import current_brand

HAPP_ANDROID_PLAY_URL = "https://play.google.com/store/apps/details?id=com.happproxy&hl=ru"
HAPP_WINDOWS_DOWNLOAD_URL = (
    "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe"
)


def brand_has_own_app() -> bool:
    return current_brand().has_own_app


def access_key_label(texts) -> str:
    if brand_has_own_app():
        return texts.t(
            "CONNECT_ACCESS_KEY_LABEL",
            "<b>Твой ключ доступа</b> (для приложений {project_name}, Happ, Incy)",
        )
    return texts.t(
        "CONNECT_ACCESS_KEY_LABEL_NO_APP",
        "<b>Твой ключ доступа</b> (для приложений Happ, Incy)",
    )


def android_connect_text(texts) -> str:
    if brand_has_own_app():
        return texts.t(
            "CONNECT_ANDROID_TEXT",
            "Скачай {project_name} VPN по кнопке ниже и авторизуйся через Telegram "
            "или с помощью ключа доступа.",
        )
    return texts.t(
        "CONNECT_ANDROID_TEXT_HAPP",
        "Скачай Happ по кнопке ниже и вставь туда ключ доступа.",
    )


def windows_connect_text(texts) -> str:
    if brand_has_own_app():
        return texts.t(
            "CONNECT_WINDOWS_TEXT",
            "<b>☀️ {project_name} App</b>\n"
            "Скачай по ссылке ниже и установи. Затем войди через Telegram или "
            "вставь ключ доступа — ключ выше.\n\n"
            "<b>💻 Happ</b>\n"
            "Скачай по кнопке ниже и вставь в него ключ доступа — ключ выше.",
        )
    return texts.t(
        "CONNECT_WINDOWS_TEXT_HAPP",
        "<b>💻 Happ</b>\n"
        "Скачай по кнопке ниже и вставь в него ключ доступа — ключ выше.",
    )


def android_tv_happ_text(texts) -> str:
    """Витрина без своего TV-приложения: телевизор подключается через Happ."""
    return texts.t(
        "CONNECT_ANDROID_TV_TEXT_HAPP",
        "<b>📺 Как подключить Android TV</b>\n\n"
        "1. Открой <b>Google Play</b> на телевизоре и найди в поиске <b>Happ</b>. "
        "Установи приложение.\n"
        "2. Запусти Happ и добавь подписку — вставь ключ доступа.\n"
        "3. Готово: выбери страну и включай.",
    )


def onboarding_android_text(texts) -> str:
    if brand_has_own_app():
        return texts.t(
            "ONBOARDING_CONNECTION_TEXT_ANDROID",
            "Установи приложение {project_name} по кнопке ниже.\n\nПосле авторизуйся в приложении "
            "через Telegram → все настроится в один клик.",
        )
    return texts.t(
        "ONBOARDING_CONNECTION_TEXT",
        "Установи приложение Happ по кнопке ниже.\n\nПосле установки нажми кнопку "
        "\"Подключиться\"  ниже → все настроится автоматически.",
    )
