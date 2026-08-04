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
    get_raw_subscription_link,
    convert_subscription_link_to_happ_scheme,
    is_ios_device_type,
)
from app.utils.promo_offer import (
    build_promo_offer_hint,
    get_user_active_promo_discount_percent,
)

from .common import _get_addon_discount_percent_for_user, _get_period_hint_from_subscription, format_additional_section, get_apps_for_device, get_device_name, get_step_description, logger
from .countries import _get_available_countries

async def get_current_devices_detailed(db_user: User) -> dict:
    try:
        if not db_user.remnawave_uuid:
            return {"count": 0, "devices": []}

        from app.services.remnawave_service import RemnaWaveService
        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                devices_info = response['response']
                total_devices = devices_info.get('total', 0)
                devices_list = devices_info.get('devices', [])

                return {
                    "count": total_devices,
                    "devices": devices_list[:5]
                }
            else:
                return {"count": 0, "devices": []}

    except Exception as e:
        logger.error(f"Ошибка получения детальной информации об устройствах: {e}")
        return {"count": 0, "devices": []}

async def get_servers_display_names(squad_uuids: List[str]) -> str:
    if not squad_uuids:
        return "Нет серверов"

    try:
        from app.database.database import AsyncSessionLocal
        from app.database.crud.server_squad import get_server_squad_by_uuid

        server_names = []

        async with AsyncSessionLocal() as db:
            for uuid in squad_uuids:
                server = await get_server_squad_by_uuid(db, uuid)
                if server:
                    server_names.append(server.display_name)
                    logger.debug(f"Найден сервер в БД: {uuid} -> {server.display_name}")
                else:
                    logger.warning(f"Сервер с UUID {uuid} не найден в БД")

        if not server_names:
            countries = await _get_available_countries()
            for uuid in squad_uuids:
                for country in countries:
                    if country['uuid'] == uuid:
                        server_names.append(country['name'])
                        logger.debug(f"Найден сервер в кэше: {uuid} -> {country['name']}")
                        break

        if not server_names:
            if len(squad_uuids) == 1:
                return "🎯 Тестовый сервер"
            return f"{len(squad_uuids)} стран"

        if len(server_names) > 6:
            displayed = ", ".join(server_names[:6])
            remaining = len(server_names) - 6
            return f"{displayed} и ещё {remaining}"
        else:
            return ", ".join(server_names)

    except Exception as e:
        logger.error(f"Ошибка получения названий серверов: {e}")
        if len(squad_uuids) == 1:
            return "🎯 Тестовый сервер"
        return f"{len(squad_uuids)} стран"

async def get_current_devices_count(db_user: User) -> str:
    try:
        if not db_user.remnawave_uuid:
            return "—"

        from app.services.remnawave_service import RemnaWaveService
        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                total_devices = response['response'].get('total', 0)
                return str(total_devices)
            else:
                return "—"

    except Exception as e:
        logger.error(f"Ошибка получения количества устройств: {e}")
        return "—"

async def handle_change_devices(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not settings.is_devices_selection_enabled():
        await callback.answer(
            texts.t("DEVICES_SELECTION_DISABLED", "⚠️ Изменение количества устройств недоступно"),
            show_alert=True,
        )
        return

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t("PAID_FEATURE_ONLY", "⚠️ Эта функция доступна только для платных подписок"),
            show_alert=True,
        )
        return

    current_devices = subscription.device_limit

    period_hint_days = _get_period_hint_from_subscription(subscription)
    devices_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        "devices",
        period_hint_days,
    )

    prompt_text = texts.t(
        "CHANGE_DEVICES_PROMPT",
        (
            "📱 <b>Изменение количества устройств</b>\n\n"
            "Текущий лимит: {current_devices} устройств\n"
            "Выберите новое количество устройств:\n\n"
            "💡 <b>Важно:</b>\n"
            "• При увеличении - доплата пропорционально оставшемуся времени\n"
            "• При уменьшении - возврат средств не производится"
        ),
    ).format(current_devices=current_devices)

    await callback.message.edit_text(
        prompt_text,
        reply_markup=get_change_devices_keyboard(
            current_devices,
            db_user.language,
            subscription.end_date,
            devices_discount_percent,
        ),
        parse_mode="HTML"
    )

    await callback.answer()

async def confirm_change_devices(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    new_devices_count = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not settings.is_devices_selection_enabled():
        await callback.answer(
            texts.t("DEVICES_SELECTION_DISABLED", "⚠️ Изменение количества устройств недоступно"),
            show_alert=True,
        )
        return

    current_devices = subscription.device_limit

    if new_devices_count == current_devices:
        await callback.answer(
            texts.t("DEVICES_NO_CHANGE", "ℹ️ Количество устройств не изменилось"),
            show_alert=True,
        )
        return

    if settings.MAX_DEVICES_LIMIT > 0 and new_devices_count > settings.MAX_DEVICES_LIMIT:
        await callback.answer(
            texts.t(
                "DEVICES_LIMIT_EXCEEDED",
                "⚠️ Превышен максимальный лимит устройств ({limit})",
            ).format(limit=settings.MAX_DEVICES_LIMIT),
            show_alert=True
        )
        return

    devices_difference = new_devices_count - current_devices

    if devices_difference > 0:
        additional_devices = devices_difference

        if current_devices < settings.DEFAULT_DEVICE_LIMIT:
            free_devices = settings.DEFAULT_DEVICE_LIMIT - current_devices
            chargeable_devices = max(0, additional_devices - free_devices)
        else:
            chargeable_devices = additional_devices

        devices_price_per_month = chargeable_devices * settings.PRICE_PER_DEVICE
        months_hint = get_remaining_months(subscription.end_date)
        period_hint_days = months_hint * 30 if months_hint > 0 else None
        devices_discount_percent = _get_addon_discount_percent_for_user(
            db_user,
            "devices",
            period_hint_days,
        )
        discounted_per_month, discount_per_month = apply_percentage_discount(
            devices_price_per_month,
            devices_discount_percent,
        )
        price, charged_months = calculate_prorated_price(
            discounted_per_month,
            subscription.end_date,
        )
        total_discount = discount_per_month * charged_months

        if price > 0 and db_user.balance_kopeks < price:
            missing_kopeks = price - db_user.balance_kopeks
            required_text = f"{texts.format_price(price)} (за {charged_months} мес)"
            message_text = texts.t(
                "ADDON_INSUFFICIENT_FUNDS_MESSAGE",
                (
                    "⚠️ <b>Недостаточно средств</b>\n\n"
                    "Стоимость услуги: {required}\n"
                    "На балансе: {balance}\n"
                    "Не хватает: {missing}\n\n"
                    "Выберите способ пополнения. Сумма подставится автоматически."
                ),
            ).format(
                required=required_text,
                balance=texts.format_price(db_user.balance_kopeks),
                missing=texts.format_price(missing_kopeks),
            )

            await callback.message.answer(
                message_text,
                reply_markup=get_insufficient_balance_keyboard(
                    db_user.language,
                    amount_kopeks=missing_kopeks,
                ),
                parse_mode="HTML",
            )
            await callback.answer()
            return

        action_text = texts.t(
            "DEVICE_CHANGE_ACTION_INCREASE",
            "увеличить до {count}",
        ).format(count=new_devices_count)
        if price > 0:
            cost_text = texts.t(
                "DEVICE_CHANGE_EXTRA_COST",
                "Доплата: {amount} (за {months} мес)",
            ).format(
                amount=texts.format_price(price),
                months=charged_months,
            )
            if total_discount > 0:
                cost_text += texts.t(
                    "DEVICE_CHANGE_DISCOUNT_INFO",
                    " (скидка {percent}%: -{amount})",
                ).format(
                    percent=devices_discount_percent,
                    amount=texts.format_price(total_discount),
                )
        else:
            cost_text = texts.t("DEVICE_CHANGE_FREE", "Бесплатно")

    else:
        price = 0
        action_text = texts.t(
            "DEVICE_CHANGE_ACTION_DECREASE",
            "уменьшить до {count}",
        ).format(count=new_devices_count)
        cost_text = texts.t("DEVICE_CHANGE_NO_REFUND", "Возврат средств не производится")

    confirm_text = texts.t(
        "DEVICE_CHANGE_CONFIRMATION",
        (
            "📱 <b>Подтверждение изменения</b>\n\n"
            "Текущее количество: {current} устройств\n"
            "Новое количество: {new} устройств\n\n"
            "Действие: {action}\n"
            "💰 {cost}\n\n"
            "Подтвердить изменение?"
        ),
    ).format(
        current=current_devices,
        new=new_devices_count,
        action=action_text,
        cost=cost_text,
    )

    await callback.message.edit_text(
        confirm_text,
        reply_markup=get_confirm_change_devices_keyboard(new_devices_count, price, db_user.language),
        parse_mode="HTML"
    )

    await callback.answer()

async def execute_change_devices(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    callback_parts = callback.data.split('_')
    new_devices_count = int(callback_parts[3])
    price = int(callback_parts[4])

    texts = get_texts(db_user.language)
    subscription = db_user.subscription
    current_devices = subscription.device_limit

    if not settings.is_devices_selection_enabled():
        await callback.answer(
            texts.t("DEVICES_SELECTION_DISABLED", "⚠️ Изменение количества устройств недоступно"),
            show_alert=True,
        )
        return

    try:
        if price > 0:
            success = await subtract_user_balance(
                db, db_user, price,
                f"Изменение количества устройств с {current_devices} до {new_devices_count}"
            )

            if not success:
                await callback.answer(
                    texts.t("PAYMENT_CHARGE_ERROR", "⚠️ Ошибка списания средств"),
                    show_alert=True,
                )
                return

            charged_months = get_remaining_months(subscription.end_date)
            await create_transaction(
                db=db,
                user_id=db_user.id,
                type=TransactionType.SUBSCRIPTION_PAYMENT,
                amount_kopeks=price,
                description=f"Изменение устройств с {current_devices} до {new_devices_count} на {charged_months} мес"
            )

        subscription.device_limit = new_devices_count
        subscription.updated_at = datetime.utcnow()

        await db.commit()

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        await db.refresh(db_user)
        await db.refresh(subscription)

        try:
            from app.services.admin_notification_service import AdminNotificationService
            notification_service = AdminNotificationService(callback.bot)
            await notification_service.send_subscription_update_notification(
                db, db_user, subscription, "devices", current_devices, new_devices_count, price
            )
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления об изменении устройств: {e}")

        if new_devices_count > current_devices:
            success_text = texts.t(
                "DEVICE_CHANGE_INCREASE_SUCCESS",
                "✅ Количество устройств увеличено!\n\n",
            )
            success_text += texts.t(
                "DEVICE_CHANGE_RESULT_LINE",
                "📱 Было: {old} → Стало: {new}\n",
            ).format(old=current_devices, new=new_devices_count)
            if price > 0:
                success_text += texts.t(
                    "DEVICE_CHANGE_CHARGED",
                    "💰 Списано: {amount}",
                ).format(amount=texts.format_price(price))
        else:
            success_text = texts.t(
                "DEVICE_CHANGE_DECREASE_SUCCESS",
                "✅ Количество устройств уменьшено!\n\n",
            )
            success_text += texts.t(
                "DEVICE_CHANGE_RESULT_LINE",
                "📱 Было: {old} → Стало: {new}\n",
            ).format(old=current_devices, new=new_devices_count)
            success_text += texts.t(
                "DEVICE_CHANGE_NO_REFUND_INFO",
                "ℹ️ Возврат средств не производится",
            )

        await callback.message.edit_text(
            success_text,
            reply_markup=get_back_keyboard(db_user.language)
        )

        logger.info(
            f"✅ Пользователь {db_user.telegram_id} изменил количество устройств с {current_devices} на {new_devices_count}, доплата: {price / 100}₽")

    except Exception as e:
        logger.error(f"Ошибка изменения количества устройств: {e}")
        await callback.message.edit_text(
            texts.ERROR,
            reply_markup=get_back_keyboard(db_user.language)
        )

    await callback.answer()

async def handle_device_management(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not subscription or subscription.is_trial:
        await callback.answer(
            texts.t("PAID_FEATURE_ONLY", "⚠️ Эта функция доступна только для платных подписок"),
            show_alert=True,
        )
        return

    if not db_user.remnawave_uuid:
        await callback.answer(
            texts.t("DEVICE_UUID_NOT_FOUND", "❌ UUID пользователя не найден"),
            show_alert=True,
        )
        return

    try:
        from app.services.remnawave_service import RemnaWaveService
        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                devices_info = response['response']
                total_devices = devices_info.get('total', 0)
                devices_list = devices_info.get('devices', [])

                if total_devices == 0:
                    await callback.message.edit_text(
                        texts.t("DEVICE_NONE_CONNECTED", "ℹ️ У вас нет подключенных устройств"),
                        reply_markup=get_back_keyboard(db_user.language)
                    )
                    await callback.answer()
                    return

                await show_devices_page(callback, db_user, devices_list, page=1)
            else:
                await callback.answer(
                    texts.t(
                        "DEVICE_FETCH_INFO_ERROR",
                        "❌ Ошибка получения информации об устройствах",
                    ),
                    show_alert=True,
                )

    except Exception as e:
        logger.error(f"Ошибка получения списка устройств: {e}")
        await callback.answer(
            texts.t(
                "DEVICE_FETCH_INFO_ERROR",
                "❌ Ошибка получения информации об устройствах",
            ),
            show_alert=True,
        )

    await callback.answer()

async def show_devices_page(
        callback: types.CallbackQuery,
        db_user: User,
        devices_list: List[dict],
        page: int = 1
):
    texts = get_texts(db_user.language)
    devices_per_page = 5

    pagination = paginate_list(devices_list, page=page, per_page=devices_per_page)

    devices_text = texts.t(
        "DEVICE_MANAGEMENT_OVERVIEW",
        (
            "🔄 <b>Управление устройствами</b>\n\n"
            "📊 Всего подключено: {total} устройств\n"
            "📄 Страница {page} из {pages}\n\n"
        ),
    ).format(total=len(devices_list), page=pagination.page, pages=pagination.total_pages)

    if pagination.items:
        devices_text += texts.t(
            "DEVICE_MANAGEMENT_CONNECTED_HEADER",
            "<b>Подключенные устройства:</b>\n",
        )
        for i, device in enumerate(pagination.items, 1):
            platform = device.get('platform', 'Unknown')
            device_model = device.get('deviceModel', 'Unknown')
            device_info = f"{platform} - {device_model}"

            if len(device_info) > 35:
                device_info = device_info[:32] + "..."

            devices_text += texts.t(
                "DEVICE_MANAGEMENT_LIST_ITEM",
                "• {device}\n",
            ).format(device=device_info)

    devices_text += texts.t(
        "DEVICE_MANAGEMENT_ACTIONS",
        (
            "\n💡 <b>Действия:</b>\n"
            "• Выберите устройство для сброса\n"
            "• Или сбросьте все устройства сразу"
        ),
    )

    await callback.message.edit_text(
        devices_text,
        reply_markup=get_devices_management_keyboard(
            pagination.items,
            pagination,
            db_user.language
        ),
        parse_mode="HTML"
    )

async def handle_devices_page(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    page = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)

    try:
        from app.services.remnawave_service import RemnaWaveService
        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                devices_list = response['response'].get('devices', [])
                await show_devices_page(callback, db_user, devices_list, page=page)
            else:
                await callback.answer(
                    texts.t("DEVICE_FETCH_ERROR", "❌ Ошибка получения устройств"),
                    show_alert=True,
                )

    except Exception as e:
        logger.error(f"Ошибка перехода на страницу устройств: {e}")
        await callback.answer(
            texts.t("DEVICE_PAGE_LOAD_ERROR", "❌ Ошибка загрузки страницы"),
            show_alert=True,
        )

async def handle_single_device_reset(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    try:
        callback_parts = callback.data.split('_')
        if len(callback_parts) < 4:
            logger.error(f"Некорректный формат callback_data: {callback.data}")
            await callback.answer(
                texts.t("DEVICE_RESET_INVALID_REQUEST", "❌ Ошибка: некорректный запрос"),
                show_alert=True,
            )
            return

        device_index = int(callback_parts[2])
        page = int(callback_parts[3])

        logger.info(f"🔧 Сброс устройства: index={device_index}, page={page}")

    except (ValueError, IndexError) as e:
        logger.error(f"❌ Ошибка парсинга callback_data {callback.data}: {e}")
        await callback.answer(
            texts.t("DEVICE_RESET_PARSE_ERROR", "❌ Ошибка обработки запроса"),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)

    try:
        from app.services.remnawave_service import RemnaWaveService
        service = RemnaWaveService()

        async with service.get_api_client() as api:
            response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if response and 'response' in response:
                devices_list = response['response'].get('devices', [])

                devices_per_page = 5
                pagination = paginate_list(devices_list, page=page, per_page=devices_per_page)

                if device_index < len(pagination.items):
                    device = pagination.items[device_index]
                    device_hwid = device.get('hwid')

                    if device_hwid:
                        delete_data = {
                            "userUuid": db_user.remnawave_uuid,
                            "hwid": device_hwid
                        }

                        await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)

                        platform = device.get('platform', 'Unknown')
                        device_model = device.get('deviceModel', 'Unknown')
                        device_info = f"{platform} - {device_model}"

                        await callback.answer(
                            texts.t(
                                "DEVICE_RESET_SUCCESS",
                                "✅ Устройство {device} успешно сброшено!",
                            ).format(device=device_info),
                            show_alert=True,
                        )

                        updated_response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')
                        if updated_response and 'response' in updated_response:
                            updated_devices = updated_response['response'].get('devices', [])

                            if updated_devices:
                                updated_pagination = paginate_list(updated_devices, page=page,
                                                                   per_page=devices_per_page)
                                if not updated_pagination.items and page > 1:
                                    page = page - 1

                                await show_devices_page(callback, db_user, updated_devices, page=page)
                            else:
                                await callback.message.edit_text(
                                    texts.t(
                                        "DEVICE_RESET_ALL_DONE",
                                        "ℹ️ Все устройства сброшены",
                                    ),
                                    reply_markup=get_back_keyboard(db_user.language)
                                )

                        logger.info(f"✅ Пользователь {db_user.telegram_id} сбросил устройство {device_info}")
                    else:
                        await callback.answer(
                            texts.t(
                                "DEVICE_RESET_ID_FAILED",
                                "❌ Не удалось получить ID устройства",
                            ),
                            show_alert=True,
                        )
                else:
                    await callback.answer(
                        texts.t("DEVICE_RESET_NOT_FOUND", "❌ Устройство не найдено"),
                        show_alert=True,
                    )
            else:
                await callback.answer(
                    texts.t("DEVICE_FETCH_ERROR", "❌ Ошибка получения устройств"),
                    show_alert=True,
                )

    except Exception as e:
        logger.error(f"Ошибка сброса устройства: {e}")
        await callback.answer(
            texts.t("DEVICE_RESET_ERROR", "❌ Ошибка сброса устройства"),
            show_alert=True,
        )

async def handle_all_devices_reset_from_management(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    texts = get_texts(db_user.language)

    if not db_user.remnawave_uuid:
        await callback.answer(
            texts.t("DEVICE_UUID_NOT_FOUND", "❌ UUID пользователя не найден"),
            show_alert=True,
        )
        return

    try:
        from app.services.remnawave_service import RemnaWaveService
        service = RemnaWaveService()

        async with service.get_api_client() as api:
            devices_response = await api._make_request('GET', f'/api/hwid/devices/{db_user.remnawave_uuid}')

            if not devices_response or 'response' not in devices_response:
                await callback.answer(
                    texts.t(
                        "DEVICE_LIST_FETCH_ERROR",
                        "❌ Ошибка получения списка устройств",
                    ),
                    show_alert=True,
                )
                return

            devices_list = devices_response['response'].get('devices', [])

            if not devices_list:
                await callback.answer(
                    texts.t("DEVICE_NONE_CONNECTED", "ℹ️ У вас нет подключенных устройств"),
                    show_alert=True,
                )
                return

            logger.info(f"🔧 Найдено {len(devices_list)} устройств для сброса")

            success_count = 0
            failed_count = 0

            for device in devices_list:
                device_hwid = device.get('hwid')
                if device_hwid:
                    try:
                        delete_data = {
                            "userUuid": db_user.remnawave_uuid,
                            "hwid": device_hwid
                        }

                        await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                        success_count += 1
                        logger.info(f"✅ Устройство {device_hwid} удалено")

                    except Exception as device_error:
                        failed_count += 1
                        logger.error(f"❌ Ошибка удаления устройства {device_hwid}: {device_error}")
                else:
                    failed_count += 1
                    logger.warning(f"⚠️ У устройства нет HWID: {device}")

            if success_count > 0:
                if failed_count == 0:
                    await callback.message.edit_text(
                        texts.t(
                            "DEVICE_RESET_ALL_SUCCESS_MESSAGE",
                            (
                                "✅ <b>Все устройства успешно сброшены!</b>\n\n"
                                "🔄 Сброшено: {count} устройств\n"
                                "📱 Теперь вы можете заново подключить свои устройства\n\n"
                                "💡 Используйте ссылку из раздела 'Моя подписка' для повторного подключения"
                            ),
                        ).format(count=success_count),
                        reply_markup=get_back_keyboard(db_user.language),
                        parse_mode="HTML"
                    )
                    logger.info(f"✅ Пользователь {db_user.telegram_id} успешно сбросил {success_count} устройств")
                else:
                    await callback.message.edit_text(
                        texts.t(
                            "DEVICE_RESET_PARTIAL_MESSAGE",
                            (
                                "⚠️ <b>Частичный сброс устройств</b>\n\n"
                                "✅ Удалено: {success} устройств\n"
                                "❌ Не удалось удалить: {failed} устройств\n\n"
                                "Попробуйте еще раз или обратитесь в поддержку."
                            ),
                        ).format(success=success_count, failed=failed_count),
                        reply_markup=get_back_keyboard(db_user.language),
                        parse_mode="HTML"
                    )
                    logger.warning(
                        f"⚠️ Частичный сброс у пользователя {db_user.telegram_id}: {success_count}/{len(devices_list)}")
            else:
                await callback.message.edit_text(
                    texts.t(
                        "DEVICE_RESET_ALL_FAILED_MESSAGE",
                        (
                            "❌ <b>Не удалось сбросить устройства</b>\n\n"
                            "Попробуйте еще раз позже или обратитесь в техподдержку.\n\n"
                            "Всего устройств: {total}"
                        ),
                    ).format(total=len(devices_list)),
                    reply_markup=get_back_keyboard(db_user.language),
                    parse_mode="HTML"
                )
                logger.error(f"❌ Не удалось сбросить ни одного устройства у пользователя {db_user.telegram_id}")

    except Exception as e:
        logger.error(f"Ошибка сброса всех устройств: {e}")
        await callback.message.edit_text(
            texts.ERROR,
            reply_markup=get_back_keyboard(db_user.language)
        )

    await callback.answer()

async def confirm_add_devices(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    devices_count = int(callback.data.split('_')[2])
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    if not settings.is_devices_selection_enabled():
        await callback.answer(
            texts.t("DEVICES_SELECTION_DISABLED", "⚠️ Изменение количества устройств недоступно"),
            show_alert=True,
        )
        return

    resume_callback = None

    new_total_devices = subscription.device_limit + devices_count

    if settings.MAX_DEVICES_LIMIT > 0 and new_total_devices > settings.MAX_DEVICES_LIMIT:
        await callback.answer(
            f"⚠️ Превышен максимальный лимит устройств ({settings.MAX_DEVICES_LIMIT}). "
            f"У вас: {subscription.device_limit}, добавляете: {devices_count}",
            show_alert=True
        )
        return

    devices_price_per_month = devices_count * settings.PRICE_PER_DEVICE
    months_hint = get_remaining_months(subscription.end_date)
    period_hint_days = months_hint * 30 if months_hint > 0 else None
    devices_discount_percent = _get_addon_discount_percent_for_user(
        db_user,
        "devices",
        period_hint_days,
    )
    discounted_per_month, discount_per_month = apply_percentage_discount(
        devices_price_per_month,
        devices_discount_percent,
    )
    price, charged_months = calculate_prorated_price(
        discounted_per_month,
        subscription.end_date,
    )
    total_discount = discount_per_month * charged_months

    logger.info(
        "Добавление %s устройств: %.2f₽/мес × %s мес = %.2f₽ (скидка %.2f₽)",
        devices_count,
        discounted_per_month / 100,
        charged_months,
        price / 100,
        total_discount / 100,
    )

    if db_user.balance_kopeks < price:
        missing_kopeks = price - db_user.balance_kopeks
        required_text = f"{texts.format_price(price)} (за {charged_months} мес)"
        message_text = texts.t(
            "ADDON_INSUFFICIENT_FUNDS_MESSAGE",
            (
                "⚠️ <b>Недостаточно средств</b>\n\n"
                "Стоимость услуги: {required}\n"
                "На балансе: {balance}\n"
                "Не хватает: {missing}\n\n"
                "Выберите способ пополнения. Сумма подставится автоматически."
            ),
        ).format(
            required=required_text,
            balance=texts.format_price(db_user.balance_kopeks),
            missing=texts.format_price(missing_kopeks),
        )

        await callback.message.edit_text(
            message_text,
            reply_markup=get_insufficient_balance_keyboard(
                db_user.language,
                resume_callback=resume_callback,
                amount_kopeks=missing_kopeks,
            ),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    try:
        success = await subtract_user_balance(
            db, db_user, price,
            f"Добавление {devices_count} устройств на {charged_months} мес"
        )

        if not success:
            await callback.answer(
                texts.t("BALANCE_DEBIT_ERROR", "⚠️ Ошибка списания средств"),
                show_alert=True,
            )
            return

        await add_subscription_devices(db, subscription, devices_count)

        subscription_service = SubscriptionService()
        await subscription_service.update_remnawave_user(db, subscription)

        await create_transaction(
            db=db,
            user_id=db_user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=price,
            description=f"Добавление {devices_count} устройств на {charged_months} мес"
        )

        await db.refresh(db_user)
        await db.refresh(subscription)

        success_text = (
            "✅ Устройства успешно добавлены!\n\n"
            f"📱 Добавлено: {devices_count} устройств\n"
            f"Новый лимит: {subscription.device_limit} устройств\n"
        )
        success_text += f"💰 Списано: {texts.format_price(price)} (за {charged_months} мес)"
        if total_discount > 0:
            success_text += (
                f" (скидка {devices_discount_percent}%:"
                f" -{texts.format_price(total_discount)})"
            )

        await callback.message.edit_text(
            success_text,
            reply_markup=get_back_keyboard(db_user.language)
        )

        logger.info(f"✅ Пользователь {db_user.telegram_id} добавил {devices_count} устройств за {price / 100}₽")

    except Exception as e:
        logger.error(f"Ошибка добавления устройств: {e}")
        await callback.message.edit_text(
            texts.ERROR,
            reply_markup=get_back_keyboard(db_user.language)
        )

    await callback.answer()

async def handle_reset_devices(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    await handle_device_management(callback, db_user, db)

async def confirm_reset_devices(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    await handle_device_management(callback, db_user, db)

async def handle_device_guide(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    device_type = callback.data.split('_')[2]
    texts = get_texts(db_user.language)
    subscription = db_user.subscription
    subscription_link = get_display_subscription_link(subscription)

    if is_ios_device_type(device_type):
        # На Apple-платформах подписка добавляется обычной ссылкой, а не криптоссылкой.
        subscription_link = get_raw_subscription_link(subscription) or subscription_link

    if not subscription_link:
        await callback.answer(
            texts.t("SUBSCRIPTION_LINK_UNAVAILABLE", "❌ Ссылка подписки недоступна"),
            show_alert=True,
        )
        return

    apps = get_apps_for_device(device_type, db_user.language)
    hide_subscription_link = settings.should_hide_subscription_link()

    if not apps:
        await callback.answer(
            texts.t("SUBSCRIPTION_DEVICE_APPS_NOT_FOUND", "❌ Приложения для этого устройства не найдены"),
            show_alert=True,
        )
        return

    featured_app = next((app for app in apps if app.get('isFeatured', False)), apps[0])
    featured_app_id = featured_app.get('id')
    other_apps = [
        app for app in apps
        if isinstance(app, dict) and app.get('id') and app.get('id') != featured_app_id
    ]

    other_app_names = ", ".join(
        str(app.get('name')).strip()
        for app in other_apps
        if isinstance(app.get('name'), str) and app.get('name').strip()
    )

    if hide_subscription_link:
        link_section = (
                texts.t("SUBSCRIPTION_DEVICE_LINK_TITLE", "🔗 <b>Ссылка подписки:</b>")
                + "\n"
                + texts.t(
            "SUBSCRIPTION_LINK_HIDDEN_NOTICE",
            "ℹ️ Ссылка подписки доступна по кнопкам ниже или в разделе \"Моя подписка\".",
        )
                + "\n\n"
        )
    else:
        link_section = (
                texts.t("SUBSCRIPTION_DEVICE_LINK_TITLE", "🔗 <b>Ссылка подписки:</b>")
                + f"\n<code>{subscription_link}</code>\n\n"
        )

    installation_description = get_step_description(featured_app, "installationStep", db_user.language)
    add_description = get_step_description(featured_app, "addSubscriptionStep", db_user.language)
    connect_description = get_step_description(featured_app, "connectAndUseStep", db_user.language)
    additional_before_text = format_additional_section(
        featured_app.get("additionalBeforeAddSubscriptionStep"),
        texts,
        db_user.language,
    )
    additional_after_text = format_additional_section(
        featured_app.get("additionalAfterAddSubscriptionStep"),
        texts,
        db_user.language,
    )

    guide_text = (
            texts.t(
                "SUBSCRIPTION_DEVICE_GUIDE_TITLE",
                "📱 <b>Настройка для {device_name}</b>",
            ).format(device_name=get_device_name(device_type, db_user.language))
            + "\n\n"
            + link_section
            + texts.t(
        "SUBSCRIPTION_DEVICE_FEATURED_APP",
        "📋 <b>Рекомендуемое приложение:</b> {app_name}",
    ).format(app_name=featured_app.get('name', ''))
    )

    if other_app_names:
        guide_text += "\n\n" + texts.t(
            "SUBSCRIPTION_DEVICE_OTHER_APPS",
            "📦 <b>Другие приложения:</b> {app_list}",
        ).format(app_list=other_app_names)
        guide_text += "\n" + texts.t(
            "SUBSCRIPTION_DEVICE_OTHER_APPS_HINT",
            "Нажмите кнопку \"Другие приложения\" ниже, чтобы выбрать приложение.",
        )

    guide_text += "\n\n" + texts.t("SUBSCRIPTION_DEVICE_STEP_INSTALL_TITLE", "<b>Шаг 1 - Установка:</b>")
    if installation_description:
        guide_text += f"\n{installation_description}"

    if additional_before_text:
        guide_text += f"\n\n{additional_before_text}"

    guide_text += "\n\n" + texts.t("SUBSCRIPTION_DEVICE_STEP_ADD_TITLE", "<b>Шаг 2 - Добавление подписки:</b>")
    if add_description:
        guide_text += f"\n{add_description}"

    guide_text += "\n\n" + texts.t("SUBSCRIPTION_DEVICE_STEP_CONNECT_TITLE", "<b>Шаг 3 - Подключение:</b>")
    if connect_description:
        guide_text += f"\n{connect_description}"

    guide_text += "\n\n" + texts.t("SUBSCRIPTION_DEVICE_HOW_TO_TITLE", "💡 <b>Как подключить:</b>")
    guide_text += "\n" + "\n".join(
        [
            texts.t(
                "SUBSCRIPTION_DEVICE_HOW_TO_STEP1",
                "1. Установите приложение по ссылке выше",
            ),
            texts.t(
                "SUBSCRIPTION_DEVICE_HOW_TO_STEP2",
                "2. Нажмите кнопку \"Подключиться\" ниже",
            ),
            texts.t(
                "SUBSCRIPTION_DEVICE_HOW_TO_STEP3",
                "3. Откройте приложение и вставьте ссылку",
            ),
            texts.t(
                "SUBSCRIPTION_DEVICE_HOW_TO_STEP4",
                "4. Подключитесь к серверу",
            ),
        ]
    )

    if additional_after_text:
        guide_text += f"\n\n{additional_after_text}"

    await callback.message.edit_text(
        guide_text,
        reply_markup=get_connection_guide_keyboard(
            subscription_link,
            featured_app,
            device_type,
            db_user.language,
            has_other_apps=bool(other_apps),
            raw_subscription_url=get_raw_subscription_link(subscription),
        ),
        parse_mode="HTML"
    )
    await callback.answer()

async def handle_app_selection(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    device_type = callback.data.split('_')[2]
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    apps = get_apps_for_device(device_type, db_user.language)

    if not apps:
        await callback.answer(
            texts.t("SUBSCRIPTION_DEVICE_APPS_NOT_FOUND", "❌ Приложения для этого устройства не найдены"),
            show_alert=True,
        )
        return

    app_text = (
            texts.t(
                "SUBSCRIPTION_APPS_TITLE",
                "📱 <b>Приложения для {device_name}</b>",
            ).format(device_name=get_device_name(device_type, db_user.language))
            + "\n\n"
            + texts.t("SUBSCRIPTION_APPS_PROMPT", "Выберите приложение для подключения:")
    )

    await callback.message.edit_text(
        app_text,
        reply_markup=get_app_selection_keyboard(device_type, apps, db_user.language),
        parse_mode="HTML"
    )
    await callback.answer()

async def handle_specific_app_guide(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    _, device_type, app_id = callback.data.split('_')
    texts = get_texts(db_user.language)
    subscription = db_user.subscription

    subscription_link = get_display_subscription_link(subscription)

    if is_ios_device_type(device_type):
        # На Apple-платформах подписка добавляется обычной ссылкой, а не криптоссылкой.
        subscription_link = get_raw_subscription_link(subscription) or subscription_link

    if not subscription_link:
        await callback.answer(
            texts.t("SUBSCRIPTION_LINK_UNAVAILABLE", "❌ Ссылка подписки недоступна"),
            show_alert=True,
        )
        return

    apps = get_apps_for_device(device_type, db_user.language)
    app = next((a for a in apps if a['id'] == app_id), None)

    if not app:
        await callback.answer(
            texts.t("SUBSCRIPTION_APP_NOT_FOUND", "❌ Приложение не найдено"),
            show_alert=True,
        )
        return

    hide_subscription_link = settings.should_hide_subscription_link()

    if hide_subscription_link:
        link_section = (
                texts.t("SUBSCRIPTION_DEVICE_LINK_TITLE", "🔗 <b>Ссылка подписки:</b>")
                + "\n"
                + texts.t(
            "SUBSCRIPTION_LINK_HIDDEN_NOTICE",
            "ℹ️ Ссылка подписки доступна по кнопкам ниже или в разделе \"Моя подписка\".",
        )
                + "\n\n"
        )
    else:
        link_section = (
                texts.t("SUBSCRIPTION_DEVICE_LINK_TITLE", "🔗 <b>Ссылка подписки:</b>")
                + f"\n<code>{subscription_link}</code>\n\n"
        )

    installation_description = get_step_description(app, "installationStep", db_user.language)
    add_description = get_step_description(app, "addSubscriptionStep", db_user.language)
    connect_description = get_step_description(app, "connectAndUseStep", db_user.language)
    additional_before_text = format_additional_section(
        app.get("additionalBeforeAddSubscriptionStep"),
        texts,
        db_user.language,
    )
    additional_after_text = format_additional_section(
        app.get("additionalAfterAddSubscriptionStep"),
        texts,
        db_user.language,
    )

    guide_text = (
            texts.t(
                "SUBSCRIPTION_SPECIFIC_APP_TITLE",
                "📱 <b>{app_name} - {device_name}</b>",
            ).format(app_name=app.get('name', ''), device_name=get_device_name(device_type, db_user.language))
            + "\n\n"
            + link_section
    )

    guide_text += texts.t("SUBSCRIPTION_DEVICE_STEP_INSTALL_TITLE", "<b>Шаг 1 - Установка:</b>")
    if installation_description:
        guide_text += f"\n{installation_description}"

    if additional_before_text:
        guide_text += f"\n\n{additional_before_text}"

    guide_text += "\n\n" + texts.t("SUBSCRIPTION_DEVICE_STEP_ADD_TITLE", "<b>Шаг 2 - Добавление подписки:</b>")
    if add_description:
        guide_text += f"\n{add_description}"

    guide_text += "\n\n" + texts.t("SUBSCRIPTION_DEVICE_STEP_CONNECT_TITLE", "<b>Шаг 3 - Подключение:</b>")
    if connect_description:
        guide_text += f"\n{connect_description}"

    if additional_after_text:
        guide_text += f"\n\n{additional_after_text}"

    await callback.message.edit_text(
        guide_text,
        reply_markup=get_specific_app_keyboard(
            subscription_link,
            app,
            device_type,
            db_user.language,
            raw_subscription_url=get_raw_subscription_link(subscription),
        ),
        parse_mode="HTML"
    )
    await callback.answer()

async def show_device_connection_help(
        callback: types.CallbackQuery,
        db_user: User,
        db: AsyncSession
):
    subscription = db_user.subscription
    subscription_link = get_display_subscription_link(subscription)

    if not subscription_link:
        await callback.answer("❌ Ссылка подписки недоступна", show_alert=True)
        return

    help_text = f"""
📱 <b>Как подключить устройство заново</b>

После сброса устройства вам нужно:

<b>1. Получить ссылку подписки:</b>
📋 Скопируйте ссылку ниже или найдите её в разделе "Моя подписка"

<b>2. Настроить VPN приложение:</b>
• Откройте ваше VPN приложение
• Найдите функцию "Добавить подписку" или "Import"
• Вставьте скопированную ссылку

<b>3. Подключиться:</b>
• Выберите сервер
• Нажмите "Подключить"

<b>🔗 Ваша ссылка подписки:</b>
<code>{subscription_link}</code>

💡 <b>Совет:</b> Сохраните эту ссылку - она понадобится для подключения новых устройств
"""

    await callback.message.edit_text(
        help_text,
        reply_markup=get_device_management_help_keyboard(db_user.language),
        parse_mode="HTML"
    )
    await callback.answer()
