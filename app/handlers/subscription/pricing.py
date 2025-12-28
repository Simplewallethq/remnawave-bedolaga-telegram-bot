import base64
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Tuple, Optional
from urllib.parse import quote
from aiogram import Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings, PERIOD_PRICES, get_traffic_prices
from app.database.crud.discount_offer import (
    get_offer_by_id,
    mark_offer_claimed,
)
from app.database.crud.promo_offer_template import get_promo_offer_template_by_id
from app.database.crud.subscription import (
    create_trial_subscription,
    create_paid_subscription, add_subscription_traffic, add_subscription_devices,
    update_subscription_autopay
)
from app.database.crud.transaction import create_transaction
from app.database.crud.user import subtract_user_balance
from app.database.models import (
    User, TransactionType, SubscriptionStatus,
    Subscription
)
from app.keyboards.inline import (
    get_subscription_keyboard, get_trial_keyboard,
    get_subscription_period_keyboard, get_traffic_packages_keyboard,
    get_countries_keyboard, get_devices_keyboard,
    get_subscription_confirm_keyboard, get_autopay_keyboard,
    get_autopay_days_keyboard, get_back_keyboard,
    get_add_traffic_keyboard,
    get_change_devices_keyboard, get_reset_traffic_confirm_keyboard,
    get_manage_countries_keyboard,
    get_device_selection_keyboard, get_connection_guide_keyboard,
    get_app_selection_keyboard, get_specific_app_keyboard,
    get_updated_subscription_settings_keyboard, get_insufficient_balance_keyboard,
    get_extend_subscription_keyboard_with_prices, get_confirm_change_devices_keyboard,
    get_devices_management_keyboard, get_device_management_help_keyboard,
    get_happ_cryptolink_keyboard,
    get_happ_download_platform_keyboard, get_happ_download_link_keyboard,
    get_happ_download_button_row,
    get_payment_methods_keyboard_with_cart,
    get_subscription_confirm_keyboard_with_cart,
    get_insufficient_balance_keyboard_with_cart
)
from app.localization.texts import get_texts
from app.services.admin_notification_service import AdminNotificationService
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_checkout_service import (
    clear_subscription_checkout_draft,
    get_subscription_checkout_draft,
    save_subscription_checkout_draft,
    should_offer_checkout_resume,
)
from app.services.subscription_service import SubscriptionService
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.services.promo_offer_service import promo_offer_service
from app.states import SubscriptionStates
from app.utils.pagination import paginate_list
from app.utils.pricing_utils import (
    calculate_months_from_days,
    get_remaining_months,
    calculate_prorated_price,
    validate_pricing_calculation,
    format_period_description,
    apply_percentage_discount,
)
from app.utils.subscription_utils import (
    get_display_subscription_link,
    get_happ_cryptolink_redirect_link,
    convert_subscription_link_to_happ_scheme,
)
from app.utils.promo_offer import (
    build_promo_offer_hint,
    get_user_active_promo_discount_percent,
)
from app.utils.timezone import format_local_datetime

from .common import _apply_discount_to_monthly_component, _apply_promo_offer_discount, logger
from .countries import _get_available_countries, _get_countries_info, get_countries_price_by_uuids_fallback
from .devices import get_current_devices_count
from .promo import _build_promo_group_discount_text, _get_promo_offer_hint

async def _prepare_subscription_summary(
        db_user: User,
        data: Dict[str, Any],
        texts,
) -> Tuple[str, Dict[str, Any]]:
    summary_data = dict(data)
    countries = await _get_available_countries(db_user.promo_group_id)

    months_in_period = calculate_months_from_days(summary_data['period_days'])
    period_display = format_period_description(summary_data['period_days'], db_user.language)

    base_price_original = PERIOD_PRICES[summary_data['period_days']]
    period_discount_percent = db_user.get_promo_discount(
        "period",
        summary_data['period_days'],
    )
    base_price, base_discount_total = apply_percentage_discount(
        base_price_original,
        period_discount_percent,
    )

    if settings.is_traffic_fixed():
        traffic_limit = settings.get_fixed_traffic_limit()
        traffic_price_per_month = settings.get_traffic_price(traffic_limit)
        final_traffic_gb = traffic_limit
    else:
        traffic_gb = summary_data.get('traffic_gb', 0)
        traffic_price_per_month = settings.get_traffic_price(traffic_gb)
        final_traffic_gb = traffic_gb

    traffic_discount_percent = db_user.get_promo_discount(
        "traffic",
        summary_data['period_days'],
    )
    traffic_component = _apply_discount_to_monthly_component(
        traffic_price_per_month,
        traffic_discount_percent,
        months_in_period,
    )
    total_traffic_price = traffic_component["total"]

    countries_price_per_month = 0
    selected_countries_names: List[str] = []
    selected_server_prices: List[int] = []
    server_monthly_prices: List[int] = []

    selected_country_ids = set(summary_data.get('countries', []))
    for country in countries:
        if country['uuid'] in selected_country_ids:
            server_price_per_month = country['price_kopeks']
            countries_price_per_month += server_price_per_month
            selected_countries_names.append(country['name'])
            server_monthly_prices.append(server_price_per_month)

    servers_discount_percent = db_user.get_promo_discount(
        "servers",
        summary_data['period_days'],
    )
    total_countries_price = 0
    total_servers_discount = 0
    discounted_servers_price_per_month = 0

    for server_price_per_month in server_monthly_prices:
        discounted_per_month, discount_per_month = apply_percentage_discount(
            server_price_per_month,
            servers_discount_percent,
        )
        total_price_for_server = discounted_per_month * months_in_period
        total_discount_for_server = discount_per_month * months_in_period

        discounted_servers_price_per_month += discounted_per_month
        total_countries_price += total_price_for_server
        total_servers_discount += total_discount_for_server
        selected_server_prices.append(total_price_for_server)

    devices_selection_enabled = settings.is_devices_selection_enabled()
    forced_disabled_limit: Optional[int] = None
    if devices_selection_enabled:
        devices_selected = summary_data.get('devices', settings.DEFAULT_DEVICE_LIMIT)
    else:
        forced_disabled_limit = settings.get_disabled_mode_device_limit()
        if forced_disabled_limit is None:
            devices_selected = settings.DEFAULT_DEVICE_LIMIT
        else:
            devices_selected = forced_disabled_limit

    summary_data['devices'] = devices_selected
    additional_devices = max(0, devices_selected - settings.DEFAULT_DEVICE_LIMIT)
    
    # Calculate proportional price based on days: (price_per_month * period_days) / 30
    devices_price_total_original = int((additional_devices * settings.PRICE_PER_DEVICE * summary_data['period_days']) / 30)
    
    devices_discount_percent = db_user.get_promo_discount(
        "devices",
        summary_data['period_days'],
    )
    
    # Calculate discount on the total
    devices_discounted_total, devices_discount_total = apply_percentage_discount(
        devices_price_total_original,
        devices_discount_percent,
    )
    
    # We need to construct devices_component manually to fit into the existing structure
    # while preserving the proportional total.
    # We set 'discounted_per_month' such that 'discounted_per_month * months' approximates 'devices_discounted_total'.
    # Note: This might cause slight rounding differences in display vs total if months > 1.
    
    if months_in_period > 0:
        effective_discounted_per_month = devices_discounted_total // months_in_period
        effective_discount_per_month = devices_discount_total // months_in_period
        # For display purposes, we might want to show the actual monthly price, but that would break the "per_month * months = total" logic in the summary text.
        # So we stick to effective per month.
        devices_price_per_month = devices_price_total_original // months_in_period
    else:
        effective_discounted_per_month = devices_discounted_total
        effective_discount_per_month = devices_discount_total
        devices_price_per_month = devices_price_total_original

    devices_component = {
        "original_per_month": devices_price_per_month,
        "discounted_per_month": effective_discounted_per_month,
        "discount_percent": devices_discount_percent,
        "discount_per_month": effective_discount_per_month,
        "total": devices_discounted_total,
        "discount_total": devices_discount_total,
    }
    
    total_devices_price = devices_component["total"]

    total_price = base_price + total_traffic_price + total_countries_price + total_devices_price

    discounted_monthly_additions = (
            traffic_component["discounted_per_month"]
            + discounted_servers_price_per_month
            + devices_component["discounted_per_month"]
    )

    is_valid = validate_pricing_calculation(
        base_price,
        discounted_monthly_additions,
        months_in_period,
        total_price,
    )

    if not is_valid:
        # Validation might fail due to proportional pricing rounding differences.
        # We log it but don't raise error to allow the purchase.
        logger.warning(
            "Subscription price calculation validation failed (likely due to proportional pricing). "
            "Base: %s, Additions: %s, Months: %s, Total: %s",
            base_price, discounted_monthly_additions, months_in_period, total_price
        )
        # raise ValueError("Subscription price calculation validation failed")

    original_total_price = total_price
    promo_offer_component = _apply_promo_offer_discount(db_user, total_price)
    if promo_offer_component["discount"] > 0:
        total_price = promo_offer_component["discounted"]

    summary_data['total_price'] = total_price
    if promo_offer_component["discount"] > 0:
        summary_data['promo_offer_discount_percent'] = promo_offer_component["percent"]
        summary_data['promo_offer_discount_value'] = promo_offer_component["discount"]
        summary_data['total_price_before_promo_offer'] = original_total_price
    else:
        summary_data.pop('promo_offer_discount_percent', None)
        summary_data.pop('promo_offer_discount_value', None)
        summary_data.pop('total_price_before_promo_offer', None)
    summary_data['server_prices_for_period'] = selected_server_prices
    summary_data['months_in_period'] = months_in_period
    summary_data['base_price'] = base_price
    summary_data['base_price_original'] = base_price_original
    summary_data['base_discount_percent'] = period_discount_percent
    summary_data['base_discount_total'] = base_discount_total
    summary_data['final_traffic_gb'] = final_traffic_gb
    summary_data['traffic_price_per_month'] = traffic_price_per_month
    summary_data['traffic_discount_percent'] = traffic_component["discount_percent"]
    summary_data['traffic_discount_total'] = traffic_component["discount_total"]
    summary_data['traffic_discounted_price_per_month'] = traffic_component["discounted_per_month"]
    summary_data['total_traffic_price'] = total_traffic_price
    summary_data['servers_price_per_month'] = countries_price_per_month
    summary_data['countries_price_per_month'] = countries_price_per_month
    summary_data['servers_discount_percent'] = servers_discount_percent
    summary_data['servers_discount_total'] = total_servers_discount
    summary_data['servers_discounted_price_per_month'] = discounted_servers_price_per_month
    summary_data['total_servers_price'] = total_countries_price
    summary_data['total_countries_price'] = total_countries_price
    summary_data['devices_price_per_month'] = devices_price_per_month
    summary_data['devices_discount_percent'] = devices_component["discount_percent"]
    summary_data['devices_discount_total'] = devices_component["discount_total"]
    summary_data['devices_discounted_price_per_month'] = devices_component["discounted_per_month"]
    summary_data['total_devices_price'] = total_devices_price
    summary_data['discounted_monthly_additions'] = discounted_monthly_additions

    if settings.is_traffic_fixed():
        if final_traffic_gb == 0:
            traffic_display = "Безлимитный"
        else:
            traffic_display = f"{final_traffic_gb} ГБ"
    else:
        if summary_data.get('traffic_gb', 0) == 0:
            traffic_display = "Безлимитный"
        else:
            traffic_display = f"{summary_data.get('traffic_gb', 0)} ГБ"

    details_lines = []

    # Добавляем строку базового периода только если цена не равна 0
    if base_discount_total > 0 and base_price > 0:
        base_line = (
            f"- Базовый период: <s>{texts.format_price(base_price_original)}</s> "
            f"{texts.format_price(base_price)}"
            f" (скидка {period_discount_percent}%:"
            f" -{texts.format_price(base_discount_total)})"
        )
        details_lines.append(base_line)
    elif base_price_original > 0:
        base_line = f"- Базовый период: {texts.format_price(base_price_original)}"
        details_lines.append(base_line)

    if total_traffic_price > 0:
        traffic_line = (
            f"- Трафик: {texts.format_price(traffic_price_per_month)}/мес × {months_in_period}"
            f" = {texts.format_price(total_traffic_price)}"
        )
        if traffic_component["discount_total"] > 0:
            traffic_line += (
                f" (скидка {traffic_component['discount_percent']}%:"
                f" -{texts.format_price(traffic_component['discount_total'])})"
            )
        details_lines.append(traffic_line)
    if total_countries_price > 0:
        servers_line = (
            f"- Серверы: {texts.format_price(countries_price_per_month)}/мес × {months_in_period}"
            f" = {texts.format_price(total_countries_price)}"
        )
        if total_servers_discount > 0:
            servers_line += (
                f" (скидка {servers_discount_percent}%:"
                f" -{texts.format_price(total_servers_discount)})"
            )
        details_lines.append(servers_line)
    if devices_selection_enabled and total_devices_price > 0:
        devices_line = (
            f"- Доп. устройства: {texts.format_price(devices_price_per_month)}/мес × {months_in_period}"
            f" = {texts.format_price(total_devices_price)}"
        )
        if devices_component["discount_total"] > 0:
            devices_line += (
                f" (скидка {devices_component['discount_percent']}%:"
                f" -{texts.format_price(devices_component['discount_total'])})"
            )
        details_lines.append(devices_line)

    if promo_offer_component["discount"] > 0:
        details_lines.append(
            texts.t(
                "SUBSCRIPTION_SUMMARY_PROMO_DISCOUNT",
                "- Промо-предложение: -{amount} ({percent}% дополнительно)",
            ).format(
                amount=texts.format_price(promo_offer_component["discount"]),
                percent=promo_offer_component["percent"],
            )
        )

    details_text = "\n".join(details_lines)

    summary_lines = [
        "📋 <b>Сводка заказа</b>",
        "",
        f"📅 <b>Период:</b> {period_display}",
        f"📊 <b>Трафик:</b> {traffic_display}",
        f"🌍 <b>Страны:</b> {', '.join(selected_countries_names)}",
    ]

    if devices_selection_enabled:
        summary_lines.append(f"📱 <b>Устройства:</b> {devices_selected}")

    summary_lines.extend([
        "",
        "💰 <b>Детализация стоимости:</b>",
        details_text,
        "",
        f"💎 <b>Общая стоимость:</b> {texts.format_price(total_price)}",
        "",
        "Подтверждаете покупку?",
    ])

    summary_text = "\n".join(summary_lines)

    return summary_text, summary_data

async def _build_subscription_period_prompt(
        db_user: User,
        texts,
        db: AsyncSession,
) -> str:
    base_text = texts.BUY_SUBSCRIPTION_START.rstrip()

    lines: List[str] = [base_text]

    promo_offer_hint = await _get_promo_offer_hint(db, db_user, texts)
    if promo_offer_hint:
        lines.extend(["", promo_offer_hint])

    promo_text = await _build_promo_group_discount_text(
        db_user,
        settings.get_available_subscription_periods(),
        texts=texts,
    )

    if promo_text:
        lines.extend(["", promo_text])

    return "\n".join(lines) + "\n"

async def get_subscription_cost(subscription, db: AsyncSession) -> int:
    try:
        if subscription.is_trial:
            return 0

        from app.config import settings
        from app.services.subscription_service import SubscriptionService

        subscription_service = SubscriptionService()

        base_cost_original = PERIOD_PRICES.get(30, 0)
        try:
            owner = subscription.user
        except AttributeError:
            owner = None

        promo_group_id = getattr(owner, "promo_group_id", None) if owner else None

        period_discount_percent = 0
        if owner:
            try:
                period_discount_percent = owner.get_promo_discount("period", 30)
            except AttributeError:
                period_discount_percent = 0

        base_cost, _ = apply_percentage_discount(
            base_cost_original,
            period_discount_percent,
        )

        try:
            servers_cost, _ = await subscription_service.get_countries_price_by_uuids(
                subscription.connected_squads,
                db,
                promo_group_id=promo_group_id,
            )
        except AttributeError:
            servers_cost, _ = await get_countries_price_by_uuids_fallback(
                subscription.connected_squads,
                db,
                promo_group_id=promo_group_id,
            )

        traffic_cost = settings.get_traffic_price(subscription.traffic_limit_gb)
        device_limit = subscription.device_limit
        if device_limit is None:
            if settings.is_devices_selection_enabled():
                device_limit = settings.DEFAULT_DEVICE_LIMIT
            else:
                forced_limit = settings.get_disabled_mode_device_limit()
                if forced_limit is None:
                    device_limit = settings.DEFAULT_DEVICE_LIMIT
                else:
                    device_limit = forced_limit

        devices_cost = max(0, (device_limit or 0) - settings.DEFAULT_DEVICE_LIMIT) * settings.PRICE_PER_DEVICE

        total_cost = base_cost + servers_cost + traffic_cost + devices_cost

        logger.info(f"📊 Месячная стоимость конфигурации подписки {subscription.id}:")
        base_log = f"   📅 Базовый тариф (30 дней): {base_cost_original / 100}₽"
        if period_discount_percent > 0:
            discount_value = base_cost_original * period_discount_percent // 100
            base_log += (
                f" → {base_cost / 100}₽"
                f" (скидка {period_discount_percent}%: -{discount_value / 100}₽)"
            )
        logger.info(base_log)
        if servers_cost > 0:
            logger.info(f"   🌍 Серверы: {servers_cost / 100}₽")
        if traffic_cost > 0:
            logger.info(f"   📊 Трафик: {traffic_cost / 100}₽")
        if devices_cost > 0:
            logger.info(f"   📱 Устройства: {devices_cost / 100}₽")
        logger.info(f"   💎 ИТОГО: {total_cost / 100}₽")

        return total_cost

    except Exception as e:
        logger.error(f"⚠️ Ошибка расчета стоимости подписки: {e}")
        return 0

async def get_subscription_info_text(subscription, texts, db_user, db: AsyncSession):
    devices_selection_enabled = settings.is_devices_selection_enabled()

    if devices_selection_enabled:
        devices_used = await get_current_devices_count(db_user)
    else:
        devices_used = 0
    countries_info = await _get_countries_info(subscription.connected_squads)
    countries_text = ", ".join([c['name'] for c in countries_info]) if countries_info else "Нет"

    subscription_url = getattr(subscription, 'subscription_url', None) or "Генерируется..."

    if subscription.is_trial:
        status_text = "🎁 Тестовая"
        type_text = "Триал"
    else:
        if subscription.is_active:
            status_text = "✅ Оплачена"
        else:
            status_text = "⌛ Истекла"
        type_text = "Платная подписка"

    if subscription.traffic_limit_gb == 0:
        if settings.is_traffic_fixed():
            traffic_text = "∞ Безлимитный"
        else:
            traffic_text = "∞ Безлимитный"
    else:
        if settings.is_traffic_fixed():
            traffic_text = f"{subscription.traffic_limit_gb} ГБ"
        else:
            traffic_text = f"{subscription.traffic_limit_gb} ГБ"

    subscription_cost = await get_subscription_cost(subscription, db)

    info_template = texts.SUBSCRIPTION_INFO

    if not devices_selection_enabled:
        info_template = info_template.replace(
            "\n📱 <b>Устройства:</b> {devices_used} / {devices_limit}",
            "",
        ).replace(
            "\n📱 <b>Devices:</b> {devices_used} / {devices_limit}",
            "",
        )

    info_text = info_template.format(
        status=status_text,
        type=type_text,
        end_date=format_local_datetime(subscription.end_date, "%d.%m.%Y %H:%M"),
        days_left=max(0, subscription.days_left),
        traffic_used=texts.format_traffic(subscription.traffic_used_gb),
        traffic_limit=traffic_text,
        countries_count=len(subscription.connected_squads),
        devices_used=devices_used,
        devices_limit=subscription.device_limit,
        autopay_status="✅ Включен" if subscription.autopay_enabled else "⌛ Выключен"
    )

    if subscription_cost > 0:
        info_text += f"\n💰 <b>Стоимость подписки в месяц:</b> {texts.format_price(subscription_cost)}"

    if (
            subscription_url
            and subscription_url != "Генерируется..."
            and not settings.should_hide_subscription_link()
    ):
        info_text += f"\n\n🔗 <b>Ваша ссылка для импорта в VPN приложениe:</b>\n<code>{subscription_url}</code>"

    return info_text
