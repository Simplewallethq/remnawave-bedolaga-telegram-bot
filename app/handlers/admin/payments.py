from __future__ import annotations

import html
import math
from typing import Optional

from aiogram import Dispatcher, F, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, User
from app.localization.texts import get_texts
from app.services.payment_service import PaymentService
from app.services.payment_verification_service import (
    PendingPayment,
    SUPPORTED_MANUAL_CHECK_METHODS,
    get_payment_record,
    list_recent_pending_payments,
    run_manual_check,
)
from app.utils.decorators import admin_required, error_handler
from app.utils.formatters import format_datetime, format_time_ago, format_username


PAGE_SIZE = 6


def _method_display(method: PaymentMethod) -> str:
    if method == PaymentMethod.MULENPAY:
        return settings.get_mulenpay_display_name()
    if method == PaymentMethod.PAL24:
        return "PayPalych"
    if method == PaymentMethod.WATA:
        return "WATA"
    if method == PaymentMethod.HELEKET:
        return "Heleket"
    if method == PaymentMethod.YOOKASSA:
        return "YooKassa"
    if method == PaymentMethod.PLATEGA:
        return settings.get_platega_display_name()
    if method == PaymentMethod.CRYPTOBOT:
        return "CryptoBot"
    if method == PaymentMethod.TELEGRAM_STARS:
        return "Telegram Stars"
    return method.value


def _status_info(
    record: PendingPayment,
    *,
    texts,
) -> tuple[str, str]:
    status = (record.status or "").lower()

    if record.is_paid:
        return "✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")

    if record.method == PaymentMethod.PAL24:
        mapping = {
            "new": ("⏳", texts.t("ADMIN_PAYMENT_STATUS_PENDING", "⏳ Pending")),
            "process": ("⌛", texts.t("ADMIN_PAYMENT_STATUS_PROCESSING", "⌛ Processing")),
            "success": ("✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")),
            "fail": ("❌", texts.t("ADMIN_PAYMENT_STATUS_FAILED", "❌ Failed")),
            "canceled": ("❌", texts.t("ADMIN_PAYMENT_STATUS_CANCELED", "❌ Cancelled")),
            "cancel": ("❌", texts.t("ADMIN_PAYMENT_STATUS_CANCELED", "❌ Cancelled")),
        }
        return mapping.get(status, ("❓", texts.t("ADMIN_PAYMENT_STATUS_UNKNOWN", "❓ Unknown")))

    if record.method == PaymentMethod.MULENPAY:
        mapping = {
            "created": ("⏳", texts.t("ADMIN_PAYMENT_STATUS_PENDING", "⏳ Pending")),
            "processing": ("⌛", texts.t("ADMIN_PAYMENT_STATUS_PROCESSING", "⌛ Processing")),
            "hold": ("🔒", texts.t("ADMIN_PAYMENT_STATUS_ON_HOLD", "🔒 Hold")),
            "success": ("✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")),
            "canceled": ("❌", texts.t("ADMIN_PAYMENT_STATUS_CANCELED", "❌ Cancelled")),
            "cancel": ("❌", texts.t("ADMIN_PAYMENT_STATUS_CANCELED", "❌ Cancelled")),
            "error": ("⚠️", texts.t("ADMIN_PAYMENT_STATUS_FAILED", "❌ Failed")),
        }
        return mapping.get(status, ("❓", texts.t("ADMIN_PAYMENT_STATUS_UNKNOWN", "❓ Unknown")))

    if record.method == PaymentMethod.WATA:
        mapping = {
            "opened": ("⏳", texts.t("ADMIN_PAYMENT_STATUS_PENDING", "⏳ Pending")),
            "pending": ("⏳", texts.t("ADMIN_PAYMENT_STATUS_PENDING", "⏳ Pending")),
            "processing": ("⌛", texts.t("ADMIN_PAYMENT_STATUS_PROCESSING", "⌛ Processing")),
            "paid": ("✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")),
            "closed": ("✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")),
            "declined": ("❌", texts.t("ADMIN_PAYMENT_STATUS_FAILED", "❌ Failed")),
            "canceled": ("❌", texts.t("ADMIN_PAYMENT_STATUS_CANCELED", "❌ Cancelled")),
            "expired": ("⌛", texts.t("ADMIN_PAYMENT_STATUS_EXPIRED", "⌛ Expired")),
        }
        return mapping.get(status, ("❓", texts.t("ADMIN_PAYMENT_STATUS_UNKNOWN", "❓ Unknown")))

    if record.method == PaymentMethod.PLATEGA:
        mapping = {
            "pending": ("⏳", texts.t("ADMIN_PAYMENT_STATUS_PENDING", "⏳ Pending")),
            "inprogress": ("⌛", texts.t("ADMIN_PAYMENT_STATUS_PROCESSING", "⌛ Processing")),
            "confirmed": ("✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")),
            "failed": ("❌", texts.t("ADMIN_PAYMENT_STATUS_FAILED", "❌ Failed")),
            "canceled": ("❌", texts.t("ADMIN_PAYMENT_STATUS_CANCELED", "❌ Cancelled")),
            "cancelled": ("❌", texts.t("ADMIN_PAYMENT_STATUS_CANCELED", "❌ Cancelled")),
            "expired": ("⌛", texts.t("ADMIN_PAYMENT_STATUS_EXPIRED", "⌛ Expired")),
        }
        return mapping.get(status, ("❓", texts.t("ADMIN_PAYMENT_STATUS_UNKNOWN", "❓ Unknown")))

    if record.method == PaymentMethod.HELEKET:
        if status in {"pending", "created", "waiting", "check", "processing"}:
            return "⏳", texts.t("ADMIN_PAYMENT_STATUS_PENDING", "⏳ Pending")
        if status in {"paid", "paid_over"}:
            return "✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")
        if status in {"cancel", "canceled", "fail", "failed", "expired"}:
            return "❌", texts.t("ADMIN_PAYMENT_STATUS_CANCELED", "❌ Cancelled")
        return "❓", texts.t("ADMIN_PAYMENT_STATUS_UNKNOWN", "❓ Unknown")

    if record.method == PaymentMethod.YOOKASSA:
        mapping = {
            "pending": ("⏳", texts.t("ADMIN_PAYMENT_STATUS_PENDING", "⏳ Pending")),
            "waiting_for_capture": ("⌛", texts.t("ADMIN_PAYMENT_STATUS_PROCESSING", "⌛ Processing")),
            "succeeded": ("✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")),
            "canceled": ("❌", texts.t("ADMIN_PAYMENT_STATUS_CANCELED", "❌ Cancelled")),
        }
        return mapping.get(status, ("❓", texts.t("ADMIN_PAYMENT_STATUS_UNKNOWN", "❓ Unknown")))

    if record.method == PaymentMethod.CRYPTOBOT:
        mapping = {
            "active": ("⏳", texts.t("ADMIN_PAYMENT_STATUS_PENDING", "⏳ Pending")),
            "paid": ("✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")),
            "expired": ("⌛", texts.t("ADMIN_PAYMENT_STATUS_EXPIRED", "⌛ Expired")),
        }
        return mapping.get(status, ("❓", texts.t("ADMIN_PAYMENT_STATUS_UNKNOWN", "❓ Unknown")))

    if record.method == PaymentMethod.TELEGRAM_STARS:
        if record.is_paid:
            return "✅", texts.t("ADMIN_PAYMENT_STATUS_PAID", "✅ Paid")
        return "⏳", texts.t("ADMIN_PAYMENT_STATUS_PENDING", "⏳ Pending")

    return "❓", texts.t("ADMIN_PAYMENT_STATUS_UNKNOWN", "❓ Unknown")


def _is_checkable(record: PendingPayment) -> bool:
    if record.method not in SUPPORTED_MANUAL_CHECK_METHODS:
        return False
    if not record.is_recent():
        return False
    status = (record.status or "").lower()
    if record.method == PaymentMethod.PAL24:
        return status in {"new", "process"}
    if record.method == PaymentMethod.MULENPAY:
        return status in {"created", "processing", "hold"}
    if record.method == PaymentMethod.WATA:
        return status in {"opened", "pending", "processing", "inprogress", "in_progress"}
    if record.method == PaymentMethod.PLATEGA:
        return status in {"pending", "inprogress", "in_progress"}
    if record.method == PaymentMethod.HELEKET:
        return status not in {"paid", "paid_over", "cancel", "canceled", "fail", "failed", "expired"}
    if record.method == PaymentMethod.YOOKASSA:
        return status in {"pending", "waiting_for_capture"}
    if record.method == PaymentMethod.CRYPTOBOT:
        return status in {"active"}
    return False


def _record_display_number(record: PendingPayment) -> str:
    if record.identifier:
        return str(record.identifier)
    return str(record.local_id)


def _build_list_keyboard(
    records: list[PendingPayment],
    *,
    page: int,
    total_pages: int,
    language: str,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    texts = get_texts(language)

    for record in records:
        number = _record_display_number(record)
        details_template = texts.t("ADMIN_PAYMENTS_ITEM_DETAILS", "📄 #{number}")
        try:
            button_text = details_template.format(number=number)
        except Exception:  # pragma: no cover - fallback for broken localization
            button_text = f"📄 {number}"
        buttons.append(
            [
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"admin_payment_{record.method.value}_{record.local_id}",
                )
            ]
        )

    if total_pages > 1:
        navigation_row: list[InlineKeyboardButton] = []
        if page > 1:
            navigation_row.append(
                InlineKeyboardButton(
                    text="⬅️",
                    callback_data=f"admin_payments_page_{page - 1}",
                )
            )

        navigation_row.append(
            InlineKeyboardButton(
                text=f"{page}/{total_pages}",
                callback_data="admin_payments_page_current",
            )
        )

        if page < total_pages:
            navigation_row.append(
                InlineKeyboardButton(
                    text="➡️",
                    callback_data=f"admin_payments_page_{page + 1}",
                )
            )

        buttons.append(navigation_row)

    if settings.PAYMENT_ROUTER_LOG_ENABLED:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🎲 Статистика роутинга",
                    callback_data="admin_payments_routing:24h",
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text=texts.BACK, callback_data="admin_panel")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_detail_keyboard(
    record: PendingPayment,
    *,
    language: str,
) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    rows: list[list[InlineKeyboardButton]] = []

    payment = record.payment
    payment_url = getattr(payment, "payment_url", None)
    if record.method == PaymentMethod.PAL24:
        payment_url = payment.link_url or payment.link_page_url or payment_url
    elif record.method == PaymentMethod.WATA:
        payment_url = payment.url or payment_url
    elif record.method == PaymentMethod.YOOKASSA:
        payment_url = getattr(payment, "confirmation_url", None) or payment_url
    elif record.method == PaymentMethod.CRYPTOBOT:
        payment_url = (
            payment.bot_invoice_url
            or payment.mini_app_invoice_url
            or payment.web_app_invoice_url
            or payment_url
        )

    if payment_url:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t("ADMIN_PAYMENT_OPEN_LINK", "🔗 Open link"),
                    url=payment_url,
                )
            ]
        )

    if _is_checkable(record):
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t("ADMIN_PAYMENT_CHECK_BUTTON", "🔁 Check status"),
                    callback_data=f"admin_payment_check_{record.method.value}_{record.local_id}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data="admin_payments")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_user_line(user: User) -> str:
    username = format_username(user.username, user.telegram_id, user.full_name)
    return f"👤 {html.escape(username)} (<code>{user.telegram_id}</code>)"


def _build_record_lines(
    record: PendingPayment,
    *,
    index: int,
    texts,
    language: str,
) -> list[str]:
    amount = settings.format_price(record.amount_kopeks)
    if record.method == PaymentMethod.CRYPTOBOT:
        crypto_amount = getattr(record.payment, "amount", None)
        crypto_asset = getattr(record.payment, "asset", None)
        if crypto_amount and crypto_asset:
            amount = f"{crypto_amount} {crypto_asset}"
    method_name = _method_display(record.method)
    emoji, status_text = _status_info(record, texts=texts)
    created = format_datetime(record.created_at)
    age = format_time_ago(record.created_at, language)
    identifier = (
        html.escape(str(record.identifier)) if record.identifier else ""
    )
    display_number = html.escape(_record_display_number(record))

    lines = [
        f"{index}. <b>{html.escape(method_name)}</b> — {amount}",
        f"   {emoji} {status_text}",
        f"   🕒 {created} ({age})",
        _format_user_line(record.user),
    ]

    if identifier:
        lines.append(f"   🆔 ID: <code>{identifier}</code>")
    else:
        lines.append(f"   🆔 ID: <code>{display_number}</code>")

    return lines


def _build_payment_details_text(record: PendingPayment, *, texts, language: str) -> str:
    method_name = _method_display(record.method)
    emoji, status_text = _status_info(record, texts=texts)
    amount = settings.format_price(record.amount_kopeks)
    if record.method == PaymentMethod.CRYPTOBOT:
        crypto_amount = getattr(record.payment, "amount", None)
        crypto_asset = getattr(record.payment, "asset", None)
        if crypto_amount and crypto_asset:
            amount = f"{crypto_amount} {crypto_asset}"
    created = format_datetime(record.created_at)
    age = format_time_ago(record.created_at, language)
    raw_identifier = record.identifier if record.identifier else record.local_id
    identifier = html.escape(str(raw_identifier)) if raw_identifier is not None else "—"
    lines = [
        texts.t("ADMIN_PAYMENT_DETAILS_TITLE", "💳 <b>Payment details</b>"),
        "",
        f"<b>{html.escape(method_name)}</b>",
        f"{emoji} {status_text}",
        "",
        f"💰 {texts.t('ADMIN_PAYMENT_AMOUNT', 'Amount')}: {amount}",
        f"🕒 {texts.t('ADMIN_PAYMENT_CREATED', 'Created')}: {created} ({age})",
        f"🆔 ID: <code>{identifier}</code>",
        _format_user_line(record.user),
    ]

    if record.expires_at:
        expires_at = format_datetime(record.expires_at)
        lines.append(f"⏳ {texts.t('ADMIN_PAYMENT_EXPIRES', 'Expires')}: {expires_at}")

    payment = record.payment

    if record.method == PaymentMethod.PAL24:
        if getattr(payment, "payment_status", None):
            lines.append(
                f"💳 {texts.t('ADMIN_PAYMENT_GATEWAY_STATUS', 'Gateway status')}: "
                f"{html.escape(str(payment.payment_status))}"
            )
        if getattr(payment, "payment_method", None):
            lines.append(
                f"🏦 {texts.t('ADMIN_PAYMENT_GATEWAY_METHOD', 'Method')}: "
                f"{html.escape(str(payment.payment_method))}"
            )
        if getattr(payment, "balance_amount", None):
            lines.append(
                f"💱 {texts.t('ADMIN_PAYMENT_GATEWAY_AMOUNT', 'Gateway amount')}: "
                f"{html.escape(str(payment.balance_amount))}"
            )
        if getattr(payment, "payer_account", None):
            lines.append(
                f"👛 {texts.t('ADMIN_PAYMENT_GATEWAY_ACCOUNT', 'Payer account')}: "
                f"{html.escape(str(payment.payer_account))}"
            )

    if record.method == PaymentMethod.MULENPAY:
        if getattr(payment, "mulen_payment_id", None):
            lines.append(
                f"🧾 {texts.t('ADMIN_PAYMENT_GATEWAY_ID', 'Gateway ID')}: "
                f"{html.escape(str(payment.mulen_payment_id))}"
            )

    if record.method == PaymentMethod.WATA:
        if getattr(payment, "order_id", None):
            lines.append(
                f"🧾 {texts.t('ADMIN_PAYMENT_GATEWAY_ID', 'Gateway ID')}: "
                f"{html.escape(str(payment.order_id))}"
            )
        if getattr(payment, "terminal_public_id", None):
            lines.append(
                f"🏦 Terminal: {html.escape(str(payment.terminal_public_id))}"
            )

    if record.method == PaymentMethod.HELEKET:
        if getattr(payment, "order_id", None):
            lines.append(
                f"🧾 {texts.t('ADMIN_PAYMENT_GATEWAY_ID', 'Gateway ID')}: "
                f"{html.escape(str(payment.order_id))}"
            )
        if getattr(payment, "payer_amount", None) and getattr(payment, "payer_currency", None):
            lines.append(
                f"🪙 {texts.t('ADMIN_PAYMENT_PAYER_AMOUNT', 'Paid amount')}: "
                f"{html.escape(str(payment.payer_amount))} {html.escape(str(payment.payer_currency))}"
            )

    if record.method == PaymentMethod.YOOKASSA:
        if getattr(payment, "payment_method_type", None):
            lines.append(
                f"💳 {texts.t('ADMIN_PAYMENT_GATEWAY_METHOD', 'Method')}: "
                f"{html.escape(str(payment.payment_method_type))}"
            )
        if getattr(payment, "confirmation_url", None):
            lines.append(texts.t("ADMIN_PAYMENT_HAS_LINK", "🔗 Payment link is available above."))

    if record.method == PaymentMethod.CRYPTOBOT:
        if getattr(payment, "amount", None) and getattr(payment, "asset", None):
            lines.append(
                f"🪙 {texts.t('ADMIN_PAYMENT_CRYPTO_AMOUNT', 'Crypto amount')}: "
                f"{html.escape(str(payment.amount))} {html.escape(str(payment.asset))}"
            )
        if getattr(payment, "bot_invoice_url", None) or getattr(payment, "mini_app_invoice_url", None):
            lines.append(
                texts.t("ADMIN_PAYMENT_HAS_LINK", "🔗 Payment link is available above.")
            )
        if getattr(payment, "status", None):
            lines.append(
                f"📊 {texts.t('ADMIN_PAYMENT_GATEWAY_STATUS', 'Gateway status')}: "
                f"{html.escape(str(payment.status))}"
            )

    if record.method == PaymentMethod.TELEGRAM_STARS:
        description = getattr(payment, "description", "") or ""
        if description:
            lines.append(f"📝 {html.escape(description)}")
        if getattr(payment, "external_id", None):
            lines.append(
                f"🧾 {texts.t('ADMIN_PAYMENT_GATEWAY_ID', 'Gateway ID')}: "
                f"{html.escape(str(payment.external_id))}"
            )

    if _is_checkable(record):
        lines.append("")
        lines.append(texts.t("ADMIN_PAYMENT_CHECK_HINT", "ℹ️ You can trigger a manual status check."))

    return "\n".join(lines)


def _parse_method_and_id(payload: str, *, prefix: str) -> Optional[tuple[PaymentMethod, int]]:
    suffix = payload[len(prefix) :]
    try:
        method_str, identifier = suffix.rsplit("_", 1)
        method = PaymentMethod(method_str)
        payment_id = int(identifier)
        return method, payment_id
    except (ValueError, KeyError):
        return None


@admin_required
@error_handler
async def show_payments_overview(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)

    page = 1
    if callback.data.startswith("admin_payments_page_"):
        try:
            page = int(callback.data.split("_")[-1])
        except ValueError:
            page = 1

    records = await list_recent_pending_payments(db)
    total = len(records)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages

    start_index = (page - 1) * PAGE_SIZE
    page_records = records[start_index : start_index + PAGE_SIZE]

    header = texts.t("ADMIN_PAYMENTS_TITLE", "💳 <b>Top-up verification</b>")
    description = texts.t(
        "ADMIN_PAYMENTS_DESCRIPTION",
        "Pending invoices created during the last 24 hours.",
    )
    notice = texts.t(
        "ADMIN_PAYMENTS_NOTICE",
        "Only invoices younger than 24 hours and waiting for payment can be checked.",
    )

    lines = [header, "", description]

    if page_records:
        for idx, record in enumerate(page_records, start=start_index + 1):
            lines.extend(_build_record_lines(record, index=idx, texts=texts, language=db_user.language))
            lines.append("")
        lines.append(notice)
    else:
        empty_text = texts.t("ADMIN_PAYMENTS_EMPTY", "No pending top-ups in the last 24 hours.")
        lines.append("")
        lines.append(empty_text)

    keyboard = _build_list_keyboard(
        page_records,
        page=page,
        total_pages=total_pages,
        language=db_user.language,
    )

    await callback.message.edit_text(
        "\n".join(line for line in lines if line is not None),
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await callback.answer()


async def _render_payment_details(
    callback: types.CallbackQuery,
    db_user: User,
    record: PendingPayment,
) -> None:
    texts = get_texts(db_user.language)
    text = _build_payment_details_text(record, texts=texts, language=db_user.language)
    keyboard = _build_detail_keyboard(record, language=db_user.language)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@admin_required
@error_handler
async def show_payment_details(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    parsed = _parse_method_and_id(callback.data, prefix="admin_payment_")
    if not parsed:
        await callback.answer("❌ Invalid payment reference", show_alert=True)
        return

    method, payment_id = parsed
    record = await get_payment_record(db, method, payment_id)
    if not record:
        await callback.answer("❌ Платеж не найден", show_alert=True)
        return

    await _render_payment_details(callback, db_user, record)
    await callback.answer()


@admin_required
@error_handler
async def manual_check_payment(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    parsed = _parse_method_and_id(callback.data, prefix="admin_payment_check_")
    if not parsed:
        await callback.answer("❌ Invalid payment reference", show_alert=True)
        return

    method, payment_id = parsed
    record = await get_payment_record(db, method, payment_id)
    texts = get_texts(db_user.language)

    if not record:
        await callback.answer(texts.t("ADMIN_PAYMENT_NOT_FOUND", "Payment not found."), show_alert=True)
        return

    if not _is_checkable(record):
        await callback.answer(
            texts.t("ADMIN_PAYMENT_CHECK_NOT_AVAILABLE", "Manual check is not available for this invoice."),
            show_alert=True,
        )
        return

    payment_service = PaymentService(callback.bot)
    updated = await run_manual_check(db, method, payment_id, payment_service)

    if not updated:
        await callback.answer(
            texts.t("ADMIN_PAYMENT_CHECK_FAILED", "Failed to refresh the payment status."),
            show_alert=True,
        )
        return

    await _render_payment_details(callback, db_user, updated)

    if updated.status != record.status or updated.is_paid != record.is_paid:
        emoji, status_text = _status_info(updated, texts=texts)
        message = texts.t(
            "ADMIN_PAYMENT_CHECK_SUCCESS",
            "Status updated: {status}",
        ).format(status=f"{emoji} {status_text}")
    else:
        message = texts.t(
            "ADMIN_PAYMENT_CHECK_NO_CHANGES",
            "Status is unchanged after the check.",
        )

    await callback.answer(message, show_alert=True)


_ROUTING_WINDOWS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}
_ROUTING_WINDOW_TITLES = {"24h": "24 часа", "7d": "7 дней", "30d": "30 дней"}


def _percent(part: int, whole: int) -> str:
    if not whole:
        return "—"
    return f"{part / whole * 100:.1f}%"


@admin_required
@error_handler
async def show_routing_stats(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    """Конверсия по шлюзам из журнала маршрутизации.

    Знаменатель — назначенный шлюз (requested), а не фактический: при фолбэке
    счёт выставит другой шлюз, и по факту конверсия назначения исказилась бы.
    """
    from datetime import datetime, timedelta

    from app.database.crud.payment_routing import (
        backfill_routing_paid_flags,
        routing_stats,
    )

    _, _, window = callback.data.partition(":")
    window = window or "24h"
    hours = _ROUTING_WINDOWS.get(window, 24)
    since = datetime.utcnow() - timedelta(hours=hours)

    # Догоняем пропущенные отметки оплаты перед показом — расхождения
    # самозалечиваются, отдельный фоновый цикл не нужен.
    try:
        await backfill_routing_paid_flags(db, since=since)
    except Exception:  # pragma: no cover - статистика не критична
        pass

    rows = await routing_stats(db, since=since)

    lines = [
        f"🎲 <b>Роутинг платежей — {_ROUTING_WINDOW_TITLES.get(window, window)}</b>",
        "",
    ]

    if settings.is_payment_router_enabled():
        weights = settings.get_payment_router_weights()
        lines.append(
            "Веса: " + ", ".join(f"{g}={w}" for g, w in weights.items())
        )
    else:
        lines.append("⚠️ Роутер выключен")
    lines.append("")

    total_requested = sum(row["requested"] for row in rows)
    total_paid = sum(row["paid"] for row in rows)

    if not rows:
        lines.append("Данных за период нет.")
    else:
        for row in rows:
            lines.append(f"<b>{html.escape(row['gateway'])}</b>")
            lines.append(
                f"  назначено {row['requested']} · выставлено {row['issued']} · "
                f"оплачено {row['paid']}"
            )
            lines.append(
                f"  конверсия {_percent(row['paid'], row['requested'])} · "
                f"уник. юзеров {row['users']}"
            )
            if row["fallbacks"]:
                lines.append(f"  ⚠️ фолбэков: {row['fallbacks']}")
            lines.append(f"  сумма оплат: {settings.format_price(row['paid_kopeks'])}")
            lines.append("")

        lines.append(
            f"<b>Итого конверсия по счетам: {_percent(total_paid, total_requested)}</b> "
            f"({total_paid} из {total_requested})"
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=("· 24ч ·" if window == "24h" else "24ч"),
                    callback_data="admin_payments_routing:24h",
                ),
                InlineKeyboardButton(
                    text=("· 7д ·" if window == "7d" else "7д"),
                    callback_data="admin_payments_routing:7d",
                ),
                InlineKeyboardButton(
                    text=("· 30д ·" if window == "30d" else "30д"),
                    callback_data="admin_payments_routing:30d",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=get_texts(db_user.language).BACK,
                    callback_data="admin_payments",
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(
            "\n".join(lines), reply_markup=keyboard, parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            "\n".join(lines), reply_markup=keyboard, parse_mode="HTML"
        )
    await callback.answer()


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(manual_check_payment, F.data.startswith("admin_payment_check_"))
    dp.callback_query.register(
        show_payment_details,
        F.data.startswith("admin_payment_") & ~F.data.startswith("admin_payment_check_"),
    )
    dp.callback_query.register(
        show_routing_stats, F.data.startswith("admin_payments_routing")
    )
    dp.callback_query.register(show_payments_overview, F.data.startswith("admin_payments_page_"))
    dp.callback_query.register(show_payments_overview, F.data == "admin_payments")
