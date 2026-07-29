import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union, get_args, get_origin

from app.database.universal_migration import ensure_default_web_api_token

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    Settings,
    settings,
    refresh_period_prices,
    refresh_traffic_prices,
    ENV_OVERRIDE_KEYS,
)
from app.database.crud.system_setting import (
    delete_system_setting,
    upsert_system_setting,
)
from app.database.database import AsyncSessionLocal
from app.database.models import SystemSetting


logger = logging.getLogger(__name__)


def _title_from_key(key: str) -> str:
    parts = key.split("_")
    if not parts:
        return key
    return " ".join(part.capitalize() for part in parts)


def _truncate(value: str, max_len: int = 60) -> str:
    value = value.strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


@dataclass(slots=True)
class SettingDefinition:
    key: str
    category_key: str
    category_label: str
    python_type: Type[Any]
    type_label: str
    is_optional: bool

    @property
    def display_name(self) -> str:
        return _title_from_key(self.key)


@dataclass(slots=True)
class ChoiceOption:
    value: Any
    label: str
    description: Optional[str] = None


class ReadOnlySettingError(RuntimeError):
    """Исключение, выбрасываемое при попытке изменить настройку только для чтения."""


class BotConfigurationService:
    EXCLUDED_KEYS: set[str] = {"BOT_TOKEN", "ADMIN_IDS"}

    READ_ONLY_KEYS: set[str] = {"EXTERNAL_ADMIN_TOKEN", "EXTERNAL_ADMIN_TOKEN_BOT_ID"}
    PLAIN_TEXT_KEYS: set[str] = {"EXTERNAL_ADMIN_TOKEN", "EXTERNAL_ADMIN_TOKEN_BOT_ID"}

    CATEGORY_TITLES: Dict[str, str] = {
        "CORE": "🤖 Основные настройки",
        "SUPPORT": "💬 Поддержка и тикеты",
        "LOCALIZATION": "🌍 Языки интерфейса",
        "CHANNEL": "📣 Обязательная подписка",
        "TIMEZONE": "🗂 Timezone",
        "PAYMENT": "💳 Общие платежные настройки",
        "PAYMENT_VERIFICATION": "🕵️ Проверка платежей",
        "TELEGRAM": "⭐ Telegram Stars",
        "CRYPTOBOT": "🪙 CryptoBot",
        "HELEKET": "🪙 Heleket",
        "CLOUDPAYMENTS": "💳 CloudPayments",
        "YOOKASSA": "🟣 YooKassa",
        "PLATEGA": "💳 {platega_name}",
        "TRIBUTE": "🎁 Tribute",
        "MULENPAY": "💰 {mulenpay_name}",
        "PAL24": "🏦 PAL24 / PayPalych",
        "WATA": "💠 Wata",
        "EXTERNAL_ADMIN": "🛡️ Внешняя админка",
        "SUBSCRIPTIONS_CORE": "📅 Подписки и лимиты",
        "SIMPLE_SUBSCRIPTION": "⚡ Простая покупка",
        "PERIODS": "📆 Периоды подписок",
        "SUBSCRIPTION_PRICES": "💵 Стоимость тарифов",
        "TRAFFIC": "📊 Трафик",
        "TRAFFIC_PACKAGES": "📦 Пакеты трафика",
        "TRIAL": "🎁 Пробный период",
        "REFERRAL": "👥 Реферальная программа",
        "RAYS": "✨ Лучи (реферальные баллы)",
        "AUTOPAY": "🔄 Автопродление",
        "NOTIFICATIONS": "🔔 Уведомления пользователям",
        "ADMIN_NOTIFICATIONS": "📣 Оповещения администраторам",
        "ADMIN_REPORTS": "🗂 Автоматические отчеты",
        "INTERFACE": "🎨 Интерфейс и брендинг",
        "INTERFACE_BRANDING": "🖼️ Брендинг",
        "INTERFACE_SUBSCRIPTION": "🔗 Ссылка на подписку",
        "CONNECT_BUTTON": "🚀 Кнопка подключения",
        "MINIAPP": "📱 Mini App",
        "HAPP": "🅷 Happ",
        "SKIP": "⚡ Быстрый старт",
        "ADDITIONAL": "📱 Дополнительные приложения",
        "DATABASE": "💾 База данных",
        "POSTGRES": "🐘 PostgreSQL",
        "SQLITE": "🧱 SQLite",
        "REDIS": "🧠 Redis",
        "REMNAWAVE": "🌐 RemnaWave API",
        "SERVER_STATUS": "📊 Статус серверов",
        "MONITORING": "📈 Мониторинг",
        "MAINTENANCE": "🔧 Обслуживание",
        "BACKUP": "💾 Резервные копии",
        "VERSION": "🔄 Проверка версий",
        "WEB_API": "⚡ Web API",
        "WEBHOOK": "🌐 Webhook",
        "LOG": "📝 Логирование",
        "DEBUG": "🧪 Режим разработки",
        "MODERATION": "🛡️ Модерация и фильтры",
    }

    CATEGORY_DESCRIPTIONS: Dict[str, str] = {
        "CORE": "Базовые параметры работы бота и обязательные ссылки.",
        "SUPPORT": "Контакты поддержки, SLA и режимы обработки обращений.",
        "LOCALIZATION": "Доступные языки, локализация интерфейса и выбор языка.",
        "CHANNEL": "Настройки обязательной подписки на канал или группу.",
        "TIMEZONE": "Часовой пояс панели и отображение времени.",
        "PAYMENT": "Общие тексты платежей, описания чеков и шаблоны.",
        "PAYMENT_VERIFICATION": "Автоматическая проверка пополнений и интервал выполнения.",
        "YOOKASSA": "Интеграция с YooKassa: идентификаторы магазина и вебхуки.",
        "CRYPTOBOT": "CryptoBot и криптоплатежи через Telegram.",
        "HELEKET": "Heleket: криптоплатежи, ключи мерчанта и вебхуки.",
        "CLOUDPAYMENTS": "CloudPayments: оплата банковскими картами, Public ID, API Secret и вебхуки.",
        "PLATEGA": "{platega_name}: merchant ID, секрет, ссылки возврата и методы оплаты.",
        "MULENPAY": "Платежи {mulenpay_name} и параметры магазина.",
        "PAL24": "PAL24 / PayPalych подключения и лимиты.",
        "TRIBUTE": "Tribute и донат-сервисы.",
        "TELEGRAM": "Telegram Stars и их стоимость.",
        "WATA": "Wata: токен доступа, тип платежа и пределы сумм.",
        "EXTERNAL_ADMIN": "Токен внешней админки для проверки запросов.",
        "SUBSCRIPTIONS_CORE": "Лимиты устройств, трафика и базовые цены подписок.",
        "SIMPLE_SUBSCRIPTION": "Параметры упрощённой покупки: период, трафик, устройства и сквады.",
        "PERIODS": "Доступные периоды подписок и продлений.",
        "SUBSCRIPTION_PRICES": "Стоимость подписок по периодам в копейках.",
        "TRAFFIC": "Лимиты трафика и стратегии сброса.",
        "TRAFFIC_PACKAGES": "Цены пакетов трафика и конфигурация предложений.",
        "TRIAL": "Длительность и ограничения пробного периода.",
        "REFERRAL": "Бонусы и пороги реферальной программы.",
        "RAYS": "Начисление лучей за длительные покупки рефералов (тиры по периоду).",
        "AUTOPAY": "Настройки автопродления и минимальный баланс.",
        "NOTIFICATIONS": "Пользовательские уведомления и кэширование сообщений.",
        "ADMIN_NOTIFICATIONS": "Оповещения админам о событиях и тикетах.",
        "ADMIN_REPORTS": "Автоматические отчеты для команды.",
        "INTERFACE": "Глобальные параметры интерфейса и брендирования.",
        "INTERFACE_BRANDING": "Логотип и фирменный стиль.",
        "INTERFACE_SUBSCRIPTION": "Отображение ссылок и кнопок подписок.",
        "CONNECT_BUTTON": "Поведение кнопки «Подключиться» и miniapp.",
        "MINIAPP": "Mini App и кастомные ссылки.",
        "HAPP": "Интеграция Happ и связанные ссылки.",
        "SKIP": "Настройки быстрого старта и гайд по подключению.",
        "ADDITIONAL": "Конфигурация app-config.json, deep links и кеша.",
        "DATABASE": "Режим работы базы данных и пути до файлов.",
        "POSTGRES": "Параметры подключения к PostgreSQL.",
        "SQLITE": "Файл SQLite и резервные параметры.",
        "REDIS": "Подключение к Redis для кэша.",
        "REMNAWAVE": "Параметры авторизации и интеграция с RemnaWave API.",
        "SERVER_STATUS": "Отображение статуса серверов и external URL.",
        "MONITORING": "Интервалы мониторинга и хранение логов.",
        "MAINTENANCE": "Режим обслуживания, сообщения и интервалы.",
        "BACKUP": "Резервное копирование и расписание.",
        "VERSION": "Отслеживание обновлений репозитория.",
        "WEB_API": "Web API, токены и права доступа.",
        "WEBHOOK": "Пути и секреты вебхуков.",
        "LOG": "Уровни логирования и ротация.",
        "DEBUG": "Отладочные функции и безопасный режим.",
        "MODERATION": "Настройки фильтров отображаемых имен и защиты от фишинга.",
    }

    @staticmethod
    def _format_dynamic_copy(category_key: Optional[str], value: str) -> str:
        if not value:
            return value
        if category_key == "MULENPAY":
            return value.format(mulenpay_name=settings.get_mulenpay_display_name())
        if category_key == "PLATEGA":
            return value.format(platega_name=settings.get_platega_display_name())
        return value

    CATEGORY_KEY_OVERRIDES: Dict[str, str] = {
        "DATABASE_URL": "DATABASE",
        "DATABASE_MODE": "DATABASE",
        "LOCALES_PATH": "LOCALIZATION",
        "CHANNEL_SUB_ID": "CHANNEL",
        "CHANNEL_LINK": "CHANNEL",
        "CHANNEL_IS_REQUIRED_SUB": "CHANNEL",
        "BOT_USERNAME": "CORE",
        "DEFAULT_LANGUAGE": "LOCALIZATION",
        "AVAILABLE_LANGUAGES": "LOCALIZATION",
        "LANGUAGE_SELECTION_ENABLED": "LOCALIZATION",
        "DEFAULT_DEVICE_LIMIT": "SUBSCRIPTIONS_CORE",
        "DEFAULT_TRAFFIC_LIMIT_GB": "SUBSCRIPTIONS_CORE",
        "MAX_DEVICES_LIMIT": "SUBSCRIPTIONS_CORE",
        "PRICE_PER_DEVICE": "SUBSCRIPTIONS_CORE",
        "DEVICES_SELECTION_ENABLED": "SUBSCRIPTIONS_CORE",
        "DEVICES_SELECTION_DISABLED_AMOUNT": "SUBSCRIPTIONS_CORE",
        "BASE_SUBSCRIPTION_PRICE": "SUBSCRIPTIONS_CORE",
        "DEFAULT_TRAFFIC_RESET_STRATEGY": "TRAFFIC",
        "RESET_TRAFFIC_ON_PAYMENT": "TRAFFIC",
        "TRAFFIC_SELECTION_MODE": "TRAFFIC",
        "FIXED_TRAFFIC_LIMIT_GB": "TRAFFIC",
        "AVAILABLE_SUBSCRIPTION_PERIODS": "PERIODS",
        "AVAILABLE_RENEWAL_PERIODS": "PERIODS",
        "PRICE_1_DAYS": "SUBSCRIPTION_PRICES",
        "PRICE_5_DAYS": "SUBSCRIPTION_PRICES",
        "PRICE_14_DAYS": "SUBSCRIPTION_PRICES",
        "PRICE_15_DAYS": "SUBSCRIPTION_PRICES",
        "PRICE_30_DAYS": "SUBSCRIPTION_PRICES",
        "PRICE_60_DAYS": "SUBSCRIPTION_PRICES",
        "PRICE_90_DAYS": "SUBSCRIPTION_PRICES",
        "PRICE_180_DAYS": "SUBSCRIPTION_PRICES",
        "PRICE_360_DAYS": "SUBSCRIPTION_PRICES",
        "PAID_SUBSCRIPTION_USER_TAG": "SUBSCRIPTION_PRICES",
        "TRAFFIC_PACKAGES_CONFIG": "TRAFFIC_PACKAGES",
        "BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED": "SUBSCRIPTIONS_CORE",
        "BASE_PROMO_GROUP_PERIOD_DISCOUNTS": "SUBSCRIPTIONS_CORE",
        "DEFAULT_AUTOPAY_ENABLED": "AUTOPAY",
        "DEFAULT_AUTOPAY_DAYS_BEFORE": "AUTOPAY",
        "MIN_BALANCE_FOR_AUTOPAY_KOPEKS": "AUTOPAY",
        "TRIAL_WARNING_HOURS": "TRIAL",
        "TRIAL_USER_TAG": "TRIAL",
        "SUPPORT_USERNAME": "SUPPORT",
        "SUPPORT_EMAIL": "SUPPORT",
        "SUPPORT_MENU_ENABLED": "SUPPORT",
        "SUPPORT_SYSTEM_MODE": "SUPPORT",
        "SUPPORT_TICKET_SLA_ENABLED": "SUPPORT",
        "SUPPORT_TICKET_SLA_MINUTES": "SUPPORT",
        "SUPPORT_TICKET_SLA_CHECK_INTERVAL_SECONDS": "SUPPORT",
        "SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES": "SUPPORT",
        "ADMIN_NOTIFICATIONS_ENABLED": "ADMIN_NOTIFICATIONS",
        "ADMIN_NOTIFICATIONS_CHAT_ID": "ADMIN_NOTIFICATIONS",
        "ADMIN_NOTIFICATIONS_TOPIC_ID": "ADMIN_NOTIFICATIONS",
        "ADMIN_NOTIFICATIONS_TICKET_TOPIC_ID": "ADMIN_NOTIFICATIONS",
        "ADMIN_REPORTS_ENABLED": "ADMIN_REPORTS",
        "ADMIN_REPORTS_CHAT_ID": "ADMIN_REPORTS",
        "ADMIN_REPORTS_TOPIC_ID": "ADMIN_REPORTS",
        "ADMIN_REPORTS_SEND_TIME": "ADMIN_REPORTS",
        "PAYMENT_SERVICE_NAME": "PAYMENT",
        "PAYMENT_BALANCE_DESCRIPTION": "PAYMENT",
        "PAYMENT_SUBSCRIPTION_DESCRIPTION": "PAYMENT",
        "PAYMENT_BALANCE_TEMPLATE": "PAYMENT",
        "PAYMENT_SUBSCRIPTION_TEMPLATE": "PAYMENT",
        "AUTO_PURCHASE_AFTER_TOPUP_ENABLED": "PAYMENT",
        "SIMPLE_SUBSCRIPTION_ENABLED": "SIMPLE_SUBSCRIPTION",
        "SIMPLE_SUBSCRIPTION_PERIOD_DAYS": "SIMPLE_SUBSCRIPTION",
        "SIMPLE_SUBSCRIPTION_DEVICE_LIMIT": "SIMPLE_SUBSCRIPTION",
        "SIMPLE_SUBSCRIPTION_TRAFFIC_GB": "SIMPLE_SUBSCRIPTION",
        "SIMPLE_SUBSCRIPTION_SQUAD_UUID": "SIMPLE_SUBSCRIPTION",
        "DISABLE_TOPUP_BUTTONS": "PAYMENT",
        "SUPPORT_TOPUP_ENABLED": "PAYMENT",
        "ENABLE_NOTIFICATIONS": "NOTIFICATIONS",
        "NOTIFICATION_RETRY_ATTEMPTS": "NOTIFICATIONS",
        "NOTIFICATION_CACHE_HOURS": "NOTIFICATIONS",
        "MONITORING_LOGS_RETENTION_DAYS": "MONITORING",
        "MONITORING_INTERVAL": "MONITORING",
        "ENABLE_LOGO_MODE": "INTERFACE_BRANDING",
        "LOGO_FILE": "INTERFACE_BRANDING",
        "HIDE_SUBSCRIPTION_LINK": "INTERFACE_SUBSCRIPTION",
        "MAIN_MENU_MODE": "INTERFACE",
        "CONNECT_BUTTON_MODE": "CONNECT_BUTTON",
        "MINIAPP_CUSTOM_URL": "CONNECT_BUTTON",
        "APP_CONFIG_PATH": "ADDITIONAL",
        "ENABLE_DEEP_LINKS": "ADDITIONAL",
        "APP_CONFIG_CACHE_TTL": "ADDITIONAL",
        "INACTIVE_USER_DELETE_MONTHS": "MAINTENANCE",
        "MAINTENANCE_MESSAGE": "MAINTENANCE",
        "MAINTENANCE_CHECK_INTERVAL": "MAINTENANCE",
        "MAINTENANCE_AUTO_ENABLE": "MAINTENANCE",
        "MAINTENANCE_RETRY_ATTEMPTS": "MAINTENANCE",
        "WEBHOOK_URL": "WEBHOOK",
        "WEBHOOK_SECRET": "WEBHOOK",
        "VERSION_CHECK_ENABLED": "VERSION",
        "VERSION_CHECK_REPO": "VERSION",
        "VERSION_CHECK_INTERVAL_HOURS": "VERSION",
        "TELEGRAM_STARS_RATE_RUB": "TELEGRAM",
        "REMNAWAVE_USER_DESCRIPTION_TEMPLATE": "REMNAWAVE",
        "REMNAWAVE_USER_USERNAME_TEMPLATE": "REMNAWAVE",
        "REMNAWAVE_AUTO_SYNC_ENABLED": "REMNAWAVE",
        "REMNAWAVE_AUTO_SYNC_TIMES": "REMNAWAVE",
    }

    CATEGORY_PREFIX_OVERRIDES: Dict[str, str] = {
        "SUPPORT_": "SUPPORT",
        "ADMIN_NOTIFICATIONS": "ADMIN_NOTIFICATIONS",
        "ADMIN_REPORTS": "ADMIN_REPORTS",
        "CHANNEL_": "CHANNEL",
        "POSTGRES_": "POSTGRES",
        "SQLITE_": "SQLITE",
        "REDIS_": "REDIS",
        "REMNAWAVE": "REMNAWAVE",
        "TRIAL_": "TRIAL",
        "TRAFFIC_PACKAGES": "TRAFFIC_PACKAGES",
        "PRICE_TRAFFIC": "TRAFFIC_PACKAGES",
        "TRAFFIC_": "TRAFFIC",
        "REFERRAL_": "REFERRAL",
        "RAYS_": "RAYS",
        "AUTOPAY_": "AUTOPAY",
        "TELEGRAM_STARS": "TELEGRAM",
        "TRIBUTE_": "TRIBUTE",
        "YOOKASSA_": "YOOKASSA",
        "CRYPTOBOT_": "CRYPTOBOT",
        "HELEKET_": "HELEKET",
        "CLOUDPAYMENTS_": "CLOUDPAYMENTS",
        "PLATEGA_": "PLATEGA",
        "MULENPAY_": "MULENPAY",
        "PAL24_": "PAL24",
        "PAYMENT_": "PAYMENT",
        "PAYMENT_VERIFICATION_": "PAYMENT_VERIFICATION",
        "WATA_": "WATA",
        "EXTERNAL_ADMIN_": "EXTERNAL_ADMIN",
        "SIMPLE_SUBSCRIPTION_": "SIMPLE_SUBSCRIPTION",
        "CONNECT_BUTTON_HAPP": "HAPP",
        "HAPP_": "HAPP",
        "SKIP_": "SKIP",
        "MINIAPP_": "MINIAPP",
        "MONITORING_": "MONITORING",
        "NOTIFICATION_": "NOTIFICATIONS",
        "SERVER_STATUS": "SERVER_STATUS",
        "MAINTENANCE_": "MAINTENANCE",
        "VERSION_CHECK": "VERSION",
        "BACKUP_": "BACKUP",
        "WEBHOOK_": "WEBHOOK",
        "LOG_": "LOG",
        "WEB_API_": "WEB_API",
        "DEBUG": "DEBUG",
        "DISPLAY_NAME_": "MODERATION",
    }

    CHOICES: Dict[str, List[ChoiceOption]] = {
        "DATABASE_MODE": [
            ChoiceOption("auto", "🤖 Авто"),
            ChoiceOption("postgresql", "🐘 PostgreSQL"),
            ChoiceOption("sqlite", "💾 SQLite"),
        ],
        "REMNAWAVE_AUTH_TYPE": [
            ChoiceOption("api_key", "🔑 API Key"),
            ChoiceOption("basic_auth", "🧾 Basic Auth"),
        ],
        "REMNAWAVE_USER_DELETE_MODE": [
            ChoiceOption("delete", "🗑 Удалять"),
            ChoiceOption("disable", "🚫 Деактивировать"),
        ],
        "TRAFFIC_SELECTION_MODE": [
            ChoiceOption("selectable", "📦 Выбор пакетов"),
            ChoiceOption("fixed", "📏 Фиксированный лимит"),
        ],
        "DEFAULT_TRAFFIC_RESET_STRATEGY": [
            ChoiceOption("NO_RESET", "♾️ Без сброса"),
            ChoiceOption("DAY", "📅 Ежедневно"),
            ChoiceOption("WEEK", "🗓 Еженедельно"),
            ChoiceOption("MONTH", "📆 Ежемесячно"),
        ],
        "SUPPORT_SYSTEM_MODE": [
            ChoiceOption("tickets", "🎫 Только тикеты"),
            ChoiceOption("contact", "💬 Только контакт"),
            ChoiceOption("both", "🔁 Оба варианта"),
        ],
        "CONNECT_BUTTON_MODE": [
            ChoiceOption("guide", "📘 Гайд"),
            ChoiceOption("miniapp_subscription", "🧾 Mini App подписка"),
            ChoiceOption("miniapp_custom", "🧩 Mini App (ссылка)"),
            ChoiceOption("link", "🔗 Прямая ссылка"),
            ChoiceOption("happ_cryptolink", "🪙 Happ CryptoLink"),
        ],
        "MAIN_MENU_MODE": [
            ChoiceOption("default", "📋 Полное меню"),
            ChoiceOption("text", "📝 Текстовое меню"),
        ],
        "SERVER_STATUS_MODE": [
            ChoiceOption("disabled", "🚫 Отключено"),
            ChoiceOption("external_link", "🌐 Внешняя ссылка"),
            ChoiceOption("external_link_miniapp", "🧭 Mini App ссылка"),
            ChoiceOption("xray", "📊 XRay Checker"),
        ],
        "YOOKASSA_PAYMENT_MODE": [
            ChoiceOption("full_payment", "💳 Полная оплата"),
            ChoiceOption("partial_payment", "🪙 Частичная оплата"),
            ChoiceOption("advance", "💼 Аванс"),
            ChoiceOption("full_prepayment", "📦 Полная предоплата"),
            ChoiceOption("partial_prepayment", "📦 Частичная предоплата"),
            ChoiceOption("credit", "💰 Кредит"),
            ChoiceOption("credit_payment", "💸 Погашение кредита"),
        ],
        "YOOKASSA_PAYMENT_SUBJECT": [
            ChoiceOption("commodity", "📦 Товар"),
            ChoiceOption("excise", "🥃 Подакцизный товар"),
            ChoiceOption("job", "🛠 Работа"),
            ChoiceOption("service", "🧾 Услуга"),
            ChoiceOption("gambling_bet", "🎲 Ставка"),
            ChoiceOption("gambling_prize", "🏆 Выигрыш"),
            ChoiceOption("lottery", "🎫 Лотерея"),
            ChoiceOption("lottery_prize", "🎁 Приз лотереи"),
            ChoiceOption("intellectual_activity", "🧠 Интеллектуальная деятельность"),
            ChoiceOption("payment", "💱 Платеж"),
            ChoiceOption("agent_commission", "🤝 Комиссия агента"),
            ChoiceOption("composite", "🧩 Композитный"),
            ChoiceOption("another", "📄 Другое"),
        ],
        "YOOKASSA_VAT_CODE": [
            ChoiceOption(1, "1 — НДС не облагается"),
            ChoiceOption(2, "2 — НДС 0%"),
            ChoiceOption(3, "3 — НДС 10%"),
            ChoiceOption(4, "4 — НДС 20%"),
            ChoiceOption(5, "5 — НДС 10/110"),
            ChoiceOption(6, "6 — НДС 20/120"),
        ],
        "MULENPAY_LANGUAGE": [
            ChoiceOption("ru", "🇷🇺 Русский"),
            ChoiceOption("en", "🇬🇧 Английский"),
        ],
        "LOG_LEVEL": [
            ChoiceOption("DEBUG", "🐞 Debug"),
            ChoiceOption("INFO", "ℹ️ Info"),
            ChoiceOption("WARNING", "⚠️ Warning"),
            ChoiceOption("ERROR", "❌ Error"),
            ChoiceOption("CRITICAL", "🔥 Critical"),
        ],
    }

    SETTING_HINTS: Dict[str, Dict[str, str]] = {
        "YOOKASSA_ENABLED": {
            "description": (
                "Включает оплату через YooKassa. "
                "Требует корректных идентификаторов магазина и секретного ключа."
            ),
            "format": "Булево значение: выберите \"Включить\" или \"Выключить\".",
            "example": "Включено при полностью настроенной интеграции.",
            "warning": "При включении без Shop ID и Secret Key пользователи увидят ошибки при оплате.",
            "dependencies": "YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, YOOKASSA_RETURN_URL",
        },
        "SIMPLE_SUBSCRIPTION_ENABLED": {
            "description": "Показывает в меню пункт с быстрой покупкой подписки.",
            "format": "Булево значение.",
            "example": "true",
            "warning": "Если остались не настроенные параметры, предложение может вести себя некорректно.",
        },
        "SIMPLE_SUBSCRIPTION_PERIOD_DAYS": {
            "description": "Период подписки, который предлагается при быстрой покупке.",
            "format": "Выберите один из доступных периодов.",
            "example": "30 дн. — 990 ₽",
            "warning": "Не забудьте настроить цену периода в блоке «Стоимость тарифов».",
        },
        "SIMPLE_SUBSCRIPTION_DEVICE_LIMIT": {
            "description": "Сколько устройств получит пользователь вместе с подпиской по быстрой покупке.",
            "format": "Выберите число устройств.",
            "example": "2 устройства",
            "warning": "Значение не должно превышать допустимый лимит в настройках подписок.",
        },
        "SIMPLE_SUBSCRIPTION_TRAFFIC_GB": {
            "description": "Объём трафика, включённый в простую подписку (0 = безлимит).",
            "format": "Выберите пакет трафика.",
            "example": "Безлимит",
        },
        "SIMPLE_SUBSCRIPTION_SQUAD_UUID": {
            "description": (
                "Привязка быстрой подписки к конкретному скваду. "
                "Оставьте пустым для любого доступного сервера."
            ),
            "format": "Выберите сквад из списка или очистите значение.",
            "example": "d4aa2b8c-9a36-4f31-93a2-6f07dad05fba",
            "warning": "Убедитесь, что выбранный сквад активен и доступен для подписки.",
        },
        "DEVICES_SELECTION_ENABLED": {
            "description": "Разрешает пользователям выбирать количество устройств при покупке и продлении подписки.",
            "format": "Булево значение.",
            "example": "false",
            "warning": "При отключении пользователи не смогут докупать устройства из интерфейса бота.",
        },
        "DEVICES_SELECTION_DISABLED_AMOUNT": {
            "description": (
                "Лимит устройств, который автоматически назначается, когда выбор количества устройств выключен. "
                "Значение 0 отключает назначение устройств."
            ),
            "format": "Целое число от 0 и выше.",
            "example": "3",
            "warning": "При 0 RemnaWave не получит лимит устройств, пользователям не показываются цифры в интерфейсе.",
        },
        "CRYPTOBOT_ENABLED": {
            "description": "Разрешает принимать криптоплатежи через CryptoBot.",
            "format": "Булево значение.",
            "example": "Включите после указания токена API и секрета вебхука.",
            "warning": "Пустой токен или неверный вебхук приведут к отказам платежей.",
            "dependencies": "CRYPTOBOT_API_TOKEN, CRYPTOBOT_WEBHOOK_SECRET",
        },
        "CRYPTOBOT_MIN_AMOUNT": {
            "description": "Минимальная сумма пополнения через CryptoBot в рублях.",
            "format": "Целое число.",
            "example": "100",
        },
        "CRYPTOBOT_MIN_AMOUNT_USD": {
            "description": "Минимальная сумма пополнения через CryptoBot в USD.",
            "format": "Дробное число (через точку).",
            "example": "1.0",
        },
        "PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED": {
            "description": (
                "Запускает фоновую проверку ожидающих пополнений и повторно обращается "
                "к платёжным провайдерам без участия администратора."
            ),
            "format": "Булево значение.",
            "example": "Включено, чтобы автоматически перепроверять зависшие платежи.",
            "warning": "Требует активных интеграций YooKassa, {mulenpay_name}, PayPalych, WATA или CryptoBot.",
        },
        "PAYMENT_VERIFICATION_AUTO_CHECK_INTERVAL_MINUTES": {
            "description": (
                "Интервал между автоматическими проверками ожидающих пополнений в минутах."
            ),
            "format": "Целое число не меньше 1.",
            "example": "10",
            "warning": "Слишком малый интервал может привести к частым обращениям к платёжным API.",
            "dependencies": "PAYMENT_VERIFICATION_AUTO_CHECK_ENABLED",
        },
        "BASE_PROMO_GROUP_PERIOD_DISCOUNTS_ENABLED": {
            "description": (
                "Включает применение базовых скидок на периоды подписок в групповых промо."
            ),
            "format": "Булево значение.",
            "example": "true",
            "warning": "Скидки применяются только если указаны корректные пары периодов и процентов.",
        },
        "BASE_PROMO_GROUP_PERIOD_DISCOUNTS": {
            "description": (
                "Список скидок для групп: каждая пара задаёт дни периода и процент скидки."
            ),
            "format": "Через запятую пары вида &lt;дней&gt;:&lt;скидка&gt;.",
            "example": "30:10,60:20,90:30,180:50,360:65",
            "warning": "Некорректные записи будут проигнорированы. Процент ограничен 0-100.",
        },
        "AUTO_PURCHASE_AFTER_TOPUP_ENABLED": {
            "description": (
                "При достаточном балансе автоматически оформляет сохранённую подписку сразу после пополнения."
            ),
            "format": "Булево значение.",
            "example": "true",
            "warning": (
                "Используйте с осторожностью: средства будут списаны мгновенно, если корзина найдена."
            ),
        },
        "SUPPORT_TICKET_SLA_MINUTES": {
            "description": "Лимит времени для ответа модераторов на тикет в минутах.",
            "format": "Целое число от 1 до 1440.",
            "example": "5",
            "warning": "Слишком низкое значение может вызвать частые напоминания, слишком высокое — ухудшить SLA.",
            "dependencies": "SUPPORT_TICKET_SLA_ENABLED, SUPPORT_TICKET_SLA_REMINDER_COOLDOWN_MINUTES",
        },
        "MAINTENANCE_MODE": {
            "description": "Переводит бота в режим технического обслуживания и скрывает действия для пользователей.",
            "format": "Булево значение.",
            "example": "Включено на время плановых работ.",
            "warning": "Не забудьте отключить после завершения работ, иначе бот останется недоступен.",
            "dependencies": "MAINTENANCE_MESSAGE, MAINTENANCE_CHECK_INTERVAL",
        },
        "MAINTENANCE_MONITORING_ENABLED": {
            "description": (
                "Управляет автоматическим запуском мониторинга панели Remnawave при старте бота."
            ),
            "format": "Булево значение.",
            "example": "false",
            "warning": (
                "При отключении мониторинг можно запустить вручную из панели администратора."
            ),
            "dependencies": "MAINTENANCE_CHECK_INTERVAL",
        },
        "MAINTENANCE_RETRY_ATTEMPTS": {
            "description": (
                "Сколько раз повторять проверку панели Remnawave перед фиксацией недоступности."
            ),
            "format": "Целое число не меньше 1.",
            "example": "3",
            "warning": (
                "Большие значения увеличивают время реакции на реальные сбои, но помогают избежать ложных срабатываний."
            ),
            "dependencies": "MAINTENANCE_CHECK_INTERVAL",
        },
        "DISPLAY_NAME_BANNED_KEYWORDS": {
            "description": (
                "Список слов и фрагментов, при наличии которых в отображаемом имени "
                "пользователь будет заблокирован."
            ),
            "format": "Перечислите ключевые слова через запятую или с новой строки.",
            "example": "support, security, служебн",
            "warning": "Слишком агрессивные фильтры могут блокировать добросовестных пользователей.",
            "dependencies": "Фильтр отображаемых имен",
        },
        "REMNAWAVE_API_URL": {
            "description": "Базовый адрес панели RemnaWave, с которой синхронизируется бот.",
            "format": "Полный URL вида https://panel.example.com.",
            "example": "https://panel.remnawave.net",
            "warning": "Недоступный адрес приведет к ошибкам при управлении VPN-учетками.",
            "dependencies": "REMNAWAVE_API_KEY или REMNAWAVE_USERNAME/REMNAWAVE_PASSWORD",
        },
        "REMNAWAVE_AUTO_SYNC_ENABLED": {
            "description": "Автоматически запускает синхронизацию пользователей и серверов с панелью RemnaWave.",
            "format": "Булево значение.",
            "example": "Включено при корректно настроенных API-ключах.",
            "warning": "При включении без расписания синхронизация не будет выполнена.",
            "dependencies": "REMNAWAVE_AUTO_SYNC_TIMES",
        },
        "REMNAWAVE_AUTO_SYNC_TIMES": {
            "description": (
                "Список времени в формате HH:MM, когда запускается автосинхронизация "
                "в течение суток."
            ),
            "format": "Перечислите время через запятую или с новой строки (например, 03:00, 15:00).",
            "example": "03:00, 15:00",
            "warning": (
                "Минимальный интервал между запусками не ограничен, но слишком частые "
                "синхронизации нагружают панель."
            ),
            "dependencies": "REMNAWAVE_AUTO_SYNC_ENABLED",
        },
        "REMNAWAVE_USER_DESCRIPTION_TEMPLATE": {
            "description": (
                "Шаблон текста, который бот передает в поле Description при создании "
                "или обновлении пользователя в панели RemnaWave."
            ),
            "format": (
                "Доступные плейсхолдеры: {full_name}, {username}, {username_clean}, {telegram_id}."
            ),
            "example": "Bot user: {full_name} {username}",
            "warning": "Плейсхолдер {username} автоматически очищается, если у пользователя нет @username.",
        },
        "REMNAWAVE_USER_USERNAME_TEMPLATE": {
            "description": (
                "Шаблон имени пользователя, которое создаётся в панели RemnaWave для "
                "телеграм-пользователя."
            ),
            "format": (
                "Доступные плейсхолдеры: {full_name}, {username}, {username_clean}, {telegram_id}."
            ),
            "example": "vpn_{username_clean}_{telegram_id}",
            "warning": (
                "Недопустимые символы автоматически заменяются на подчёркивания. "
                "Если результат пустой, используется user_{telegram_id}."
            ),
        },
        "EXTERNAL_ADMIN_TOKEN": {
            "description": "Приватный токен, который использует внешняя админка для проверки запросов.",
            "format": "Значение генерируется автоматически из username бота и его токена и доступно только для чтения.",
            "example": "Генерируется автоматически",
            "warning": "Токен обновится при смене username или токена бота.",
            "dependencies": "Username телеграм-бота, токен бота",
        },
        "EXTERNAL_ADMIN_TOKEN_BOT_ID": {
            "description": "Идентификатор телеграм-бота, с которым связан токен внешней админки.",
            "format": "Проставляется автоматически после первого запуска и не редактируется вручную.",
            "example": "123456789",
            "warning": "Несовпадение ID блокирует обновление токена, предотвращая его подмену на другом боте.",
            "dependencies": "Результат вызова getMe() в Telegram Bot API",
        },
        "TRIAL_USER_TAG": {
            "description": (
                "Тег, который бот передаст пользователю при активации триальной подписки в панели RemnaWave."
            ),
            "format": "До 16 символов: заглавные A-Z, цифры и подчёркивание.",
            "example": "TRIAL_USER",
            "warning": "Неверный формат будет проигнорирован при создании пользователя.",
            "dependencies": "Активация триала и включенная интеграция с RemnaWave",
        },
        "PAID_SUBSCRIPTION_USER_TAG": {
            "description": (
                "Тег, который бот ставит пользователю при покупке платной подписки в панели RemnaWave."
            ),
            "format": "До 16 символов: заглавные A-Z, цифры и подчёркивание.",
            "example": "PAID_USER",
            "warning": "Если тег не задан или невалиден, существующий тег не будет изменён.",
            "dependencies": "Оплата подписки и интеграция с RemnaWave",
        },
    }

    @classmethod
    def get_category_description(cls, category_key: str) -> str:
        description = cls.CATEGORY_DESCRIPTIONS.get(category_key, "")
        return cls._format_dynamic_copy(category_key, description)

    @classmethod
    def is_toggle(cls, key: str) -> bool:
        definition = cls.get_definition(key)
        return definition.python_type is bool

    @classmethod
    def is_read_only(cls, key: str) -> bool:
        return key in cls.READ_ONLY_KEYS

    @classmethod
    def _is_env_override(cls, key: str) -> bool:
        return key in cls._env_override_keys

    @classmethod
    def _format_numeric_with_unit(cls, key: str, value: Union[int, float]) -> Optional[str]:
        if isinstance(value, bool):
            return None
        upper_key = key.upper()
        if any(suffix in upper_key for suffix in ("PRICE", "_KOPEKS", "AMOUNT")):
            try:
                return settings.format_price(int(value))
            except Exception:
                return f"{value}"
        if upper_key.endswith("_PERCENT") or "PERCENT" in upper_key:
            return f"{value}%"
        if upper_key.endswith("_HOURS"):
            return f"{value} ч"
        if upper_key.endswith("_MINUTES"):
            return f"{value} мин"
        if upper_key.endswith("_SECONDS"):
            return f"{value} сек"
        if upper_key.endswith("_DAYS"):
            return f"{value} дн"
        if upper_key.endswith("_GB"):
            return f"{value} ГБ"
        if upper_key.endswith("_MB"):
            return f"{value} МБ"
        return None

    @classmethod
    def _split_comma_values(cls, text: str) -> Optional[List[str]]:
        raw = (text or "").strip()
        if not raw or "," not in raw:
            return None
        parts = [segment.strip() for segment in raw.split(",") if segment.strip()]
        return parts or None

    @classmethod
    def format_value_human(cls, key: str, value: Any) -> str:
        if key == "SIMPLE_SUBSCRIPTION_SQUAD_UUID":
            if value is None:
                return "Любой доступный"
            if isinstance(value, str):
                cleaned_value = value.strip()
                if not cleaned_value:
                    return "Любой доступный"

        if value is None:
            return "—"

        if isinstance(value, bool):
            return "✅ ВКЛЮЧЕНО" if value else "❌ ВЫКЛЮЧЕНО"

        if isinstance(value, (int, float)):
            formatted = cls._format_numeric_with_unit(key, value)
            return formatted or str(value)

        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return "—"
            if key in cls.PLAIN_TEXT_KEYS:
                return cleaned
            if any(keyword in key.upper() for keyword in ("TOKEN", "SECRET", "PASSWORD", "KEY")):
                return "••••••••"
            items = cls._split_comma_values(cleaned)
            if items:
                return ", ".join(items)
            return cleaned

        if isinstance(value, (list, tuple, set)):
            return ", ".join(str(item) for item in value)

        if isinstance(value, dict):
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)

        return str(value)

    @classmethod
    def get_setting_guidance(cls, key: str) -> Dict[str, str]:
        definition = cls.get_definition(key)
        original = cls.get_original_value(key)
        type_label = definition.type_label
        hints = dict(cls.SETTING_HINTS.get(key, {}))

        base_description = (
            hints.get("description")
            or f"Параметр <b>{definition.display_name}</b> управляет категорией «{definition.category_label}»."
        )
        base_format = hints.get("format") or (
            "Булево значение (да/нет)." if definition.python_type is bool
            else "Введите значение соответствующего типа (число или строку)."
        )
        example = hints.get("example") or (
            cls.format_value_human(key, original) if original is not None else "—"
        )
        warning = hints.get("warning") or (
            "Неверные значения могут привести к некорректной работе бота."
        )
        dependencies = hints.get("dependencies") or definition.category_label

        return {
            "description": base_description,
            "format": base_format,
            "example": example,
            "warning": warning,
            "dependencies": dependencies,
            "type": type_label,
        }

    _definitions: Dict[str, SettingDefinition] = {}
    _original_values: Dict[str, Any] = settings.model_dump()
    _overrides_raw: Dict[str, Optional[str]] = {}
    _env_override_keys: set[str] = set(ENV_OVERRIDE_KEYS)
    _callback_tokens: Dict[str, str] = {}
    _token_to_key: Dict[str, str] = {}
    _choice_tokens: Dict[str, Dict[Any, str]] = {}
    _choice_token_lookup: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def initialize_definitions(cls) -> None:
        if cls._definitions:
            return

        for key, field in Settings.model_fields.items():
            if key in cls.EXCLUDED_KEYS:
                continue

            annotation = field.annotation
            python_type, is_optional = cls._normalize_type(annotation)
            type_label = cls._type_to_label(python_type, is_optional)

            category_key = cls._resolve_category_key(key)
            category_label = cls.CATEGORY_TITLES.get(
                category_key,
                category_key.capitalize() if category_key else "Прочее",
            )
            category_label = cls._format_dynamic_copy(category_key, category_label)

            cls._definitions[key] = SettingDefinition(
                key=key,
                category_key=category_key or "other",
                category_label=category_label,
                python_type=python_type,
                type_label=type_label,
                is_optional=is_optional,
            )

            cls._register_callback_token(key)
            if key in cls.CHOICES:
                cls._ensure_choice_tokens(key)


    @classmethod
    def _resolve_category_key(cls, key: str) -> str:
        override = cls.CATEGORY_KEY_OVERRIDES.get(key)
        if override:
            return override

        for prefix, category in sorted(
            cls.CATEGORY_PREFIX_OVERRIDES.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if key.startswith(prefix):
                return category

        if "_" not in key:
            return key.upper()
        prefix = key.split("_", 1)[0]
        return prefix.upper()

    @classmethod
    def _normalize_type(cls, annotation: Any) -> Tuple[Type[Any], bool]:
        if annotation is None:
            return str, True

        origin = get_origin(annotation)
        if origin is Union:
            args = [arg for arg in get_args(annotation) if arg is not type(None)]
            if len(args) == 1:
                nested_type, nested_optional = cls._normalize_type(args[0])
                return nested_type, True
            return str, True

        if annotation in {int, float, bool, str}:
            return annotation, False

        if annotation in {Optional[int], Optional[float], Optional[bool], Optional[str]}:
            nested = get_args(annotation)[0]
            return nested, True

        # Paths, lists, dicts и прочее будем хранить как строки
        return str, False

    @classmethod
    def _type_to_label(cls, python_type: Type[Any], is_optional: bool) -> str:
        base = {
            bool: "bool",
            int: "int",
            float: "float",
            str: "str",
        }.get(python_type, "str")
        return f"optional[{base}]" if is_optional else base

    @classmethod
    def get_categories(cls) -> List[Tuple[str, str, int]]:
        cls.initialize_definitions()
        categories: Dict[str, List[SettingDefinition]] = {}

        for definition in cls._definitions.values():
            categories.setdefault(definition.category_key, []).append(definition)

        result: List[Tuple[str, str, int]] = []
        for category_key, items in categories.items():
            label = items[0].category_label
            result.append((category_key, label, len(items)))

        result.sort(key=lambda item: item[1])
        return result

    @classmethod
    def get_settings_for_category(cls, category_key: str) -> List[SettingDefinition]:
        cls.initialize_definitions()
        filtered = [
            definition
            for definition in cls._definitions.values()
            if definition.category_key == category_key
        ]
        filtered.sort(key=lambda definition: definition.key)
        return filtered

    @classmethod
    def get_definition(cls, key: str) -> SettingDefinition:
        cls.initialize_definitions()
        return cls._definitions[key]

    @classmethod
    def has_override(cls, key: str) -> bool:
        if cls._is_env_override(key):
            return False
        return key in cls._overrides_raw

    @classmethod
    def get_current_value(cls, key: str) -> Any:
        return getattr(settings, key)

    @classmethod
    def get_original_value(cls, key: str) -> Any:
        return cls._original_values.get(key)

    @classmethod
    def format_value(cls, value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "✅ Да" if value else "❌ Нет"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, (list, dict, tuple, set)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)
        return str(value)

    @classmethod
    def format_value_for_list(cls, key: str) -> str:
        value = cls.get_current_value(key)
        formatted = cls.format_value_human(key, value)
        if formatted == "—":
            return formatted
        return _truncate(formatted)

    @classmethod
    def get_choice_options(cls, key: str) -> List[ChoiceOption]:
        cls.initialize_definitions()
        dynamic = cls._get_dynamic_choice_options(key)
        if dynamic is not None:
            cls.CHOICES[key] = dynamic
            cls._invalidate_choice_cache(key)
            return dynamic
        return cls.CHOICES.get(key, [])

    @classmethod
    def _invalidate_choice_cache(cls, key: str) -> None:
        cls._choice_tokens.pop(key, None)
        cls._choice_token_lookup.pop(key, None)

    @classmethod
    def _get_dynamic_choice_options(cls, key: str) -> Optional[List[ChoiceOption]]:
        if key == "SIMPLE_SUBSCRIPTION_PERIOD_DAYS":
            return cls._build_simple_subscription_period_choices()
        if key == "SIMPLE_SUBSCRIPTION_DEVICE_LIMIT":
            return cls._build_simple_subscription_device_choices()
        if key == "SIMPLE_SUBSCRIPTION_TRAFFIC_GB":
            return cls._build_simple_subscription_traffic_choices()
        return None

    @staticmethod
    def _build_simple_subscription_period_choices() -> List[ChoiceOption]:
        raw_periods = str(getattr(settings, "AVAILABLE_SUBSCRIPTION_PERIODS", "") or "")
        period_values: set[int] = set()

        for segment in raw_periods.split(","):
            segment = segment.strip()
            if not segment:
                continue
            try:
                period = int(segment)
            except ValueError:
                continue
            if period > 0:
                period_values.add(period)

        fallback_period = getattr(settings, "SIMPLE_SUBSCRIPTION_PERIOD_DAYS", 30) or 30
        try:
            fallback_period = int(fallback_period)
        except (TypeError, ValueError):
            fallback_period = 30
        period_values.add(max(1, fallback_period))

        options: List[ChoiceOption] = []
        for days in sorted(period_values):
            price_attr = f"PRICE_{days}_DAYS"
            price_value = getattr(settings, price_attr, None)
            if not isinstance(price_value, int):
                price_value = settings.BASE_SUBSCRIPTION_PRICE

            label = f"{days} дн."
            try:
                if isinstance(price_value, int):
                    label = f"{label} — {settings.format_price(price_value)}"
            except Exception:
                logger.debug("Не удалось форматировать цену для периода %s", days, exc_info=True)

            options.append(ChoiceOption(days, label))

        return options

    @classmethod
    def _build_simple_subscription_device_choices(cls) -> List[ChoiceOption]:
        default_limit = getattr(settings, "DEFAULT_DEVICE_LIMIT", 1) or 1
        try:
            default_limit = int(default_limit)
        except (TypeError, ValueError):
            default_limit = 1

        max_limit = getattr(settings, "MAX_DEVICES_LIMIT", default_limit) or default_limit
        try:
            max_limit = int(max_limit)
        except (TypeError, ValueError):
            max_limit = default_limit

        current_limit = getattr(settings, "SIMPLE_SUBSCRIPTION_DEVICE_LIMIT", default_limit) or default_limit
        try:
            current_limit = int(current_limit)
        except (TypeError, ValueError):
            current_limit = default_limit

        upper_bound = max(default_limit, max_limit, current_limit, 1)
        upper_bound = min(max(upper_bound, 1), 50)

        options: List[ChoiceOption] = []
        for count in range(1, upper_bound + 1):
            label = f"{count} {cls._pluralize_devices(count)}"
            if count == default_limit:
                label = f"{label} (по умолчанию)"
            options.append(ChoiceOption(count, label))

        return options

    @staticmethod
    def _build_simple_subscription_traffic_choices() -> List[ChoiceOption]:
        try:
            packages = settings.get_traffic_packages()
        except Exception as error:
            logger.warning("Не удалось получить пакеты трафика: %s", error, exc_info=True)
            packages = []

        traffic_values: set[int] = {0}
        for package in packages:
            gb_value = package.get("gb")
            try:
                gb = int(gb_value)
            except (TypeError, ValueError):
                continue
            if gb >= 0:
                traffic_values.add(gb)

        default_limit = getattr(settings, "DEFAULT_TRAFFIC_LIMIT_GB", 0) or 0
        try:
            default_limit = int(default_limit)
        except (TypeError, ValueError):
            default_limit = 0
        if default_limit >= 0:
            traffic_values.add(default_limit)

        current_limit = getattr(settings, "SIMPLE_SUBSCRIPTION_TRAFFIC_GB", default_limit)
        try:
            current_limit = int(current_limit)
        except (TypeError, ValueError):
            current_limit = default_limit
        if current_limit >= 0:
            traffic_values.add(current_limit)

        options: List[ChoiceOption] = []
        for gb in sorted(traffic_values):
            if gb <= 0:
                label = "Безлимит"
            else:
                label = f"{gb} ГБ"

            price_label = None
            for package in packages:
                try:
                    package_gb = int(package.get("gb"))
                except (TypeError, ValueError):
                    continue
                if package_gb != gb:
                    continue
                price_raw = package.get("price")
                try:
                    price_value = int(price_raw)
                    if price_value >= 0:
                        price_label = settings.format_price(price_value)
                except (TypeError, ValueError):
                    continue
                break

            if price_label:
                label = f"{label} — {price_label}"

            options.append(ChoiceOption(gb, label))

        return options

    @staticmethod
    def _pluralize_devices(count: int) -> str:
        count = abs(int(count))
        last_two = count % 100
        last_one = count % 10
        if 11 <= last_two <= 14:
            return "устройств"
        if last_one == 1:
            return "устройство"
        if 2 <= last_one <= 4:
            return "устройства"
        return "устройств"

    @classmethod
    def has_choices(cls, key: str) -> bool:
        return bool(cls.get_choice_options(key))

    @classmethod
    def get_callback_token(cls, key: str) -> str:
        cls.initialize_definitions()
        return cls._callback_tokens[key]

    @classmethod
    def resolve_callback_token(cls, token: str) -> str:
        cls.initialize_definitions()
        return cls._token_to_key[token]

    @classmethod
    def get_choice_token(cls, key: str, value: Any) -> Optional[str]:
        cls.initialize_definitions()
        cls._ensure_choice_tokens(key)
        return cls._choice_tokens.get(key, {}).get(value)

    @classmethod
    def resolve_choice_token(cls, key: str, token: str) -> Any:
        cls.initialize_definitions()
        cls._ensure_choice_tokens(key)
        return cls._choice_token_lookup.get(key, {})[token]

    @classmethod
    def _register_callback_token(cls, key: str) -> None:
        if key in cls._callback_tokens:
            return

        base = hashlib.blake2s(key.encode("utf-8"), digest_size=6).hexdigest()
        candidate = base
        counter = 1
        while candidate in cls._token_to_key and cls._token_to_key[candidate] != key:
            suffix = cls._encode_base36(counter)
            candidate = f"{base}{suffix}"[:16]
            counter += 1

        cls._callback_tokens[key] = candidate
        cls._token_to_key[candidate] = key

    @classmethod
    def _ensure_choice_tokens(cls, key: str) -> None:
        if key in cls._choice_tokens:
            return

        options = cls.CHOICES.get(key, [])
        value_to_token: Dict[Any, str] = {}
        token_to_value: Dict[str, Any] = {}

        for index, option in enumerate(options):
            token = cls._encode_base36(index)
            value_to_token[option.value] = token
            token_to_value[token] = option.value

        cls._choice_tokens[key] = value_to_token
        cls._choice_token_lookup[key] = token_to_value

    @staticmethod
    def _encode_base36(number: int) -> str:
        if number < 0:
            raise ValueError("number must be non-negative")
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
        if number == 0:
            return "0"
        result = []
        while number:
            number, rem = divmod(number, 36)
            result.append(alphabet[rem])
        return "".join(reversed(result))

    @classmethod
    async def initialize(cls) -> None:
        cls.initialize_definitions()

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(SystemSetting))
            rows = result.scalars().all()

        overrides: Dict[str, Optional[str]] = {}
        for row in rows:
            if row.key in cls._definitions:
                overrides[row.key] = row.value

        for key, raw_value in overrides.items():
            if cls._is_env_override(key):
                logger.debug(
                    "Пропускаем настройку %s из БД: используется значение из окружения",
                    key,
                )
                continue
            try:
                parsed_value = cls.deserialize_value(key, raw_value)
            except Exception as error:
                logger.error("Не удалось применить настройку %s: %s", key, error)
                continue

            cls._overrides_raw[key] = raw_value
            cls._apply_to_settings(key, parsed_value)

        await cls._sync_default_web_api_token()

    @classmethod
    async def reload(cls) -> None:
        cls._overrides_raw.clear()
        await cls.initialize()

    @classmethod
    def deserialize_value(cls, key: str, raw_value: Optional[str]) -> Any:
        if raw_value is None:
            return None

        definition = cls.get_definition(key)
        python_type = definition.python_type

        if python_type is bool:
            value_lower = raw_value.strip().lower()
            if value_lower in {"1", "true", "on", "yes", "да"}:
                return True
            if value_lower in {"0", "false", "off", "no", "нет"}:
                return False
            raise ValueError(f"Неверное булево значение: {raw_value}")

        if python_type is int:
            return int(raw_value)

        if python_type is float:
            return float(raw_value)

        return raw_value

    @classmethod
    def serialize_value(cls, key: str, value: Any) -> Optional[str]:
        if value is None:
            return None

        definition = cls.get_definition(key)
        python_type = definition.python_type

        if python_type is bool:
            return "true" if value else "false"
        if python_type in {int, float}:
            return str(value)
        return str(value)

    @classmethod
    def parse_user_value(cls, key: str, user_input: str) -> Any:
        definition = cls.get_definition(key)
        text = (user_input or "").strip()

        if text.lower() in {"отмена", "cancel"}:
            raise ValueError("Ввод отменен пользователем")

        if definition.is_optional and text.lower() in {"none", "null", "пусто", ""}:
            return None

        python_type = definition.python_type

        if python_type is bool:
            lowered = text.lower()
            if lowered in {"1", "true", "on", "yes", "да", "вкл", "enable", "enabled"}:
                return True
            if lowered in {"0", "false", "off", "no", "нет", "выкл", "disable", "disabled"}:
                return False
            raise ValueError("Введите 'true' или 'false' (или 'да'/'нет')")

        if python_type is int:
            parsed_value: Any = int(text)
        elif python_type is float:
            parsed_value = float(text.replace(",", "."))
        else:
            parsed_value = text

        choices = cls.get_choice_options(key)
        if choices:
            allowed_values = {option.value for option in choices}
            if python_type is str:
                lowered_map = {
                    str(option.value).lower(): option.value for option in choices
                }
                normalized = lowered_map.get(str(parsed_value).lower())
                if normalized is not None:
                    parsed_value = normalized
                elif parsed_value not in allowed_values:
                    readable = ", ".join(
                        f"{option.label} ({cls.format_value(option.value)})" for option in choices
                    )
                    raise ValueError(f"Доступные значения: {readable}")
            elif parsed_value not in allowed_values:
                readable = ", ".join(
                    f"{option.label} ({cls.format_value(option.value)})" for option in choices
                )
                raise ValueError(f"Доступные значения: {readable}")

        return parsed_value

    @classmethod
    async def set_value(
        cls,
        db: AsyncSession,
        key: str,
        value: Any,
        *,
        force: bool = False,
    ) -> None:
        if cls.is_read_only(key) and not force:
            raise ReadOnlySettingError(f"Setting {key} is read-only")

        raw_value = cls.serialize_value(key, value)
        await upsert_system_setting(db, key, raw_value)
        if cls._is_env_override(key):
            logger.info(
                "Настройка %s сохранена в БД, но не применена: значение задаётся через окружение",
                key,
            )
            cls._overrides_raw.pop(key, None)
        else:
            cls._overrides_raw[key] = raw_value
            cls._apply_to_settings(key, value)

        if key in {"WEB_API_DEFAULT_TOKEN", "WEB_API_DEFAULT_TOKEN_NAME"}:
            await cls._sync_default_web_api_token()

    @classmethod
    async def reset_value(
        cls,
        db: AsyncSession,
        key: str,
        *,
        force: bool = False,
    ) -> None:
        if cls.is_read_only(key) and not force:
            raise ReadOnlySettingError(f"Setting {key} is read-only")

        await delete_system_setting(db, key)
        cls._overrides_raw.pop(key, None)
        if cls._is_env_override(key):
            logger.info(
                "Настройка %s сброшена в БД, используется значение из окружения",
                key,
            )
        else:
            original = cls.get_original_value(key)
            cls._apply_to_settings(key, original)

        if key in {"WEB_API_DEFAULT_TOKEN", "WEB_API_DEFAULT_TOKEN_NAME"}:
            await cls._sync_default_web_api_token()

    @classmethod
    def _apply_to_settings(cls, key: str, value: Any) -> None:
        if cls._is_env_override(key):
            logger.debug(
                "Пропуск применения настройки %s: значение задано через окружение",
                key,
            )
            return
        try:
            setattr(settings, key, value)
            if key in {
                "PRICE_1_DAYS",
                "PRICE_5_DAYS",
                "PRICE_14_DAYS",
                "PRICE_15_DAYS",
                "PRICE_30_DAYS",
                "PRICE_60_DAYS",
                "PRICE_90_DAYS",
                "PRICE_180_DAYS",
                "PRICE_360_DAYS",
            }:
                refresh_period_prices()
            elif key.startswith("PRICE_TRAFFIC_") or key == "TRAFFIC_PACKAGES_CONFIG":
                refresh_traffic_prices()
            elif key in {"REMNAWAVE_AUTO_SYNC_ENABLED", "REMNAWAVE_AUTO_SYNC_TIMES"}:
                try:
                    from app.services.remnawave_sync_service import remnawave_sync_service

                    remnawave_sync_service.schedule_refresh(
                        run_immediately=(key == "REMNAWAVE_AUTO_SYNC_ENABLED" and bool(value))
                    )
                except Exception as error:
                    logger.error(
                        "Не удалось обновить сервис автосинхронизации RemnaWave: %s",
                        error,
                    )
            elif key in {
                "REMNAWAVE_API_URL",
                "REMNAWAVE_API_KEY",
                "REMNAWAVE_SECRET_KEY",
                "REMNAWAVE_USERNAME",
                "REMNAWAVE_PASSWORD",
                "REMNAWAVE_AUTH_TYPE",
            }:
                try:
                    from app.services.remnawave_sync_service import remnawave_sync_service

                    remnawave_sync_service.refresh_configuration()
                except Exception as error:
                    logger.error(
                        "Не удалось обновить конфигурацию сервиса автосинхронизации RemnaWave: %s",
                        error,
                    )
        except Exception as error:
            logger.error("Не удалось применить значение %s=%s: %s", key, value, error)

    @staticmethod
    async def _sync_default_web_api_token() -> None:
        default_token = (settings.WEB_API_DEFAULT_TOKEN or "").strip()
        if not default_token:
            return

        success = await ensure_default_web_api_token()
        if not success:
            logger.warning(
                "Не удалось синхронизировать бутстрап токен веб-API после обновления настроек",
            )

    @classmethod
    def get_setting_summary(cls, key: str) -> Dict[str, Any]:
        definition = cls.get_definition(key)
        current = cls.get_current_value(key)
        original = cls.get_original_value(key)
        has_override = cls.has_override(key)

        return {
            "key": key,
            "name": definition.display_name,
            "current": cls.format_value_human(key, current),
            "original": cls.format_value_human(key, original),
            "type": definition.type_label,
            "category_key": definition.category_key,
            "category_label": definition.category_label,
            "has_override": has_override,
            "is_read_only": cls.is_read_only(key),
        }


bot_configuration_service = BotConfigurationService
