"""Mixin for integrating WATA payment links into the payment service."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from importlib import import_module
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.subscription_auto_purchase_service import (
    auto_purchase_saved_cart_after_topup,
)
from app.services.tariff_partial_payment_service import (
    SNAPSHOT_METADATA_KEY,
    build_invoice_checkout_snapshot,
    extract_checkout_snapshot,
)
from app.services.wata_service import WataAPIError, WataService
from app.utils.success_notifications import format_topup_success_message
from app.utils.user_utils import format_referrer_info

logger = logging.getLogger(__name__)


def _extract_transaction_id(payment: Any, remote_link: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Try to find the remote WATA transaction identifier from stored payloads."""

    def _from_mapping(mapping: Any) -> Optional[str]:
        if isinstance(mapping, str):
            try:
                import json

                mapping = json.loads(mapping)
            except Exception:  # pragma: no cover - defensive parsing
                return None
        if not isinstance(mapping, dict):
            return None
        for key in ("id", "transaction_id", "transactionId"):
            value = mapping.get(key)
            if not value:
                continue
            value_str = str(value)
            if "-" in value_str:
                return value_str
        return None

    candidate = None

    if hasattr(payment, "callback_payload"):
        candidate = _from_mapping(getattr(payment, "callback_payload"))
        if candidate:
            return candidate

    metadata = getattr(payment, "metadata_json", None)
    if isinstance(metadata, dict):
        if "transaction" in metadata:
            candidate = _from_mapping(metadata.get("transaction"))
            if candidate:
                return candidate
        candidate = _from_mapping(metadata)
        if candidate:
            return candidate

    candidate = _from_mapping(remote_link)
    if candidate:
        return candidate

    return None


class WataPaymentMixin:
    """Encapsulates creation and status handling for WATA payment links."""

    async def create_wata_payment(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        description: str,
        *,
        language: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not getattr(self, "wata_service", None):
            logger.error("WATA service is not initialised")
            return None

        if amount_kopeks < settings.WATA_MIN_AMOUNT_KOPEKS:
            logger.warning(
                "Сумма WATA меньше минимальной: %s < %s",
                amount_kopeks,
                settings.WATA_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.WATA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                "Сумма WATA больше максимальной: %s > %s",
                amount_kopeks,
                settings.WATA_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module("app.services.payment_service")

        order_id = f"wata_{user_id}_{uuid.uuid4().hex[:12]}"

        try:
            response = await self.wata_service.create_payment_link(  # type: ignore[union-attr]
                amount_kopeks=amount_kopeks,
                currency="RUB",
                description=description,
                order_id=order_id,
            )
        except WataAPIError as error:
            logger.error("Ошибка создания WATA платежа: %s", error)
            return None
        except Exception as error:  # pragma: no cover - safety net
            logger.exception("Непредвиденная ошибка при создании WATA платежа: %s", error)
            return None

        payment_link_id = response.get("id")
        payment_url = response.get("url") or response.get("paymentUrl")
        status = response.get("status") or "Opened"
        terminal_public_id = response.get("terminalPublicId")
        success_url = response.get("successRedirectUrl")
        fail_url = response.get("failRedirectUrl")

        if not payment_link_id:
            logger.error("WATA API не вернула идентификатор платежной ссылки: %s", response)
            return None

        expiration_raw = response.get("expirationDateTime")
        expires_at = WataService._parse_datetime(expiration_raw)

        metadata = {
            "response": response,
            "language": language or settings.DEFAULT_LANGUAGE,
        }
        snapshot = await build_invoice_checkout_snapshot(user_id, amount_kopeks)
        if snapshot:
            metadata[SNAPSHOT_METADATA_KEY] = snapshot

        local_payment = await payment_module.create_wata_payment(
            db=db,
            user_id=user_id,
            payment_link_id=payment_link_id,
            amount_kopeks=amount_kopeks,
            currency="RUB",
            description=description,
            status=status,
            type_=response.get("type"),
            url=payment_url,
            order_id=order_id,
            metadata=metadata,
            expires_at=expires_at,
            terminal_public_id=terminal_public_id,
            success_redirect_url=success_url,
            fail_redirect_url=fail_url,
        )

        logger.info(
            "Создан WATA платеж %s на %s₽ для пользователя %s",
            payment_link_id,
            amount_kopeks / 100,
            user_id,
        )

        return {
            "local_payment_id": local_payment.id,
            "payment_link_id": payment_link_id,
            "payment_url": payment_url,
            "status": status,
            "order_id": order_id,
        }

    async def process_wata_webhook(
        self,
        db: AsyncSession,
        payload: Dict[str, Any],
    ) -> bool:
        """Handles asynchronous webhook notifications from WATA."""

        payment_module = import_module("app.services.payment_service")

        if not isinstance(payload, dict):
            logger.error("WATA webhook payload не является словарём: %s", payload)
            return False

        order_id_raw = payload.get("orderId")
        payment_link_raw = payload.get("paymentLinkId") or payload.get("id")
        transaction_status_raw = payload.get("transactionStatus")

        order_id = str(order_id_raw) if order_id_raw else None
        payment_link_id = str(payment_link_raw) if payment_link_raw else None
        transaction_status = (transaction_status_raw or "").strip()

        if not order_id and not payment_link_id:
            logger.error(
                "WATA webhook без orderId и paymentLinkId: %s",
                payload,
            )
            return False

        if not transaction_status:
            logger.error("WATA webhook без статуса транзакции: %s", payload)
            return False

        payment = None
        if order_id:
            payment = await payment_module.get_wata_payment_by_order_id(db, order_id)
        if not payment and payment_link_id:
            payment = await payment_module.get_wata_payment_by_link_id(db, payment_link_id)

        if not payment:
            logger.error(
                "WATA платеж не найден (order_id=%s, payment_link_id=%s)",
                order_id,
                payment_link_id,
            )
            return False

        status_lower = transaction_status.lower()
        metadata = dict(getattr(payment, "metadata_json", {}) or {})
        metadata["last_webhook"] = payload
        terminal_public_id = (
            payload.get("terminalPublicId")
            or payload.get("terminal_public_id")
            or payload.get("terminalPublicID")
        )

        update_kwargs: Dict[str, Any] = {
            "metadata": metadata,
            "callback_payload": payload,
            "terminal_public_id": terminal_public_id,
        }

        if transaction_status:
            update_kwargs["status"] = transaction_status
            update_kwargs["last_status"] = transaction_status

        if status_lower != "paid" and not payment.is_paid:
            update_kwargs["is_paid"] = False

        payment = await payment_module.update_wata_payment_status(
            db,
            payment=payment,
            **update_kwargs,
        )

        if status_lower == "paid":
            if payment.is_paid:
                logger.info(
                    "WATA платеж %s уже помечен как оплачен",
                    payment.payment_link_id,
                )
                return True

            await self._finalize_wata_payment(db, payment, payload)
            return True

        if status_lower == "declined":
            logger.info(
                "WATA платеж %s отклонён",
                payment.payment_link_id,
            )

        return True

    async def get_wata_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> Optional[Dict[str, Any]]:
        payment_module = import_module("app.services.payment_service")

        payment = await payment_module.get_wata_payment_by_id(db, local_payment_id)
        if not payment:
            return None

        remote_link: Optional[Dict[str, Any]] = None
        transaction_payload: Optional[Dict[str, Any]] = None
        transaction_id: Optional[str] = None

        if getattr(self, "wata_service", None) and payment.payment_link_id:
            try:
                remote_link = await self.wata_service.get_payment_link(payment.payment_link_id)  # type: ignore[union-attr]
            except WataAPIError as error:
                logger.error("Ошибка получения WATA ссылки %s: %s", payment.payment_link_id, error)
            except Exception as error:  # pragma: no cover - safety net
                logger.exception("Непредвиденная ошибка при запросе WATA ссылки: %s", error)

        if remote_link:
            remote_status = remote_link.get("status") or payment.status
            if remote_status != payment.status:
                existing_metadata = dict(getattr(payment, "metadata_json", {}) or {})
                existing_metadata["link"] = remote_link
                await payment_module.update_wata_payment_status(
                    db,
                    payment=payment,
                    status=remote_status,
                    last_status=remote_status,
                    url=remote_link.get("url") or remote_link.get("paymentUrl"),
                    metadata=existing_metadata,
                    terminal_public_id=remote_link.get("terminalPublicId"),
                )
                payment = await payment_module.get_wata_payment_by_id(db, local_payment_id)

            remote_status_normalized = (remote_status or "").lower()
            if remote_status_normalized in {"closed", "paid"} and not payment.is_paid:
                transaction_id = _extract_transaction_id(payment, remote_link)
                if transaction_id:
                    try:
                        transaction_payload = await self.wata_service.get_transaction(  # type: ignore[union-attr]
                            transaction_id
                        )
                    except WataAPIError as error:
                        logger.error(
                            "Ошибка получения WATA транзакции %s: %s",
                            transaction_id,
                            error,
                        )
                    except Exception as error:  # pragma: no cover - safety net
                        logger.exception(
                            "Непредвиденная ошибка при запросе WATA транзакции %s: %s",
                            transaction_id,
                            error,
                        )
                if not transaction_payload:
                    try:
                        tx_response = await self.wata_service.search_transactions(  # type: ignore[union-attr]
                            order_id=payment.order_id,
                            payment_link_id=payment.payment_link_id,
                            status="Paid",
                            limit=5,
                        )
                        items = tx_response.get("items") or []
                        for item in items:
                            if (item or {}).get("status") == "Paid":
                                transaction_payload = item
                                break
                    except WataAPIError as error:
                        logger.error(
                            "Ошибка поиска WATA транзакций для %s: %s",
                            payment.payment_link_id,
                            error,
                        )
                    except Exception as error:  # pragma: no cover - safety net
                        logger.exception("Непредвиденная ошибка при поиске WATA транзакции: %s", error)

        if (
            not transaction_payload
            and not payment.is_paid
            and getattr(self, "wata_service", None)
        ):
            fallback_transaction_id = transaction_id or _extract_transaction_id(payment)
            if fallback_transaction_id:
                try:
                    transaction_payload = await self.wata_service.get_transaction(  # type: ignore[union-attr]
                        fallback_transaction_id
                    )
                except WataAPIError as error:
                    logger.error(
                        "Ошибка повторного запроса WATA транзакции %s: %s",
                        fallback_transaction_id,
                        error,
                    )
                except Exception as error:  # pragma: no cover - safety net
                    logger.exception(
                        "Непредвиденная ошибка при повторном запросе WATA транзакции %s: %s",
                        fallback_transaction_id,
                        error,
                    )

        if transaction_payload and not payment.is_paid:
            normalized_status = None
            if isinstance(transaction_payload, dict):
                raw_status = transaction_payload.get("status") or transaction_payload.get("statusName")
                if raw_status:
                    normalized_status = str(raw_status).lower()
            if normalized_status == "paid":
                payment = await self._finalize_wata_payment(db, payment, transaction_payload)
            else:
                logger.debug(
                    "WATA транзакция %s в статусе %s, повторная обработка не требуется",
                    transaction_id or getattr(payment, "payment_link_id", ""),
                    normalized_status or "unknown",
                )

        return {
            "payment": payment,
            "status": payment.status,
            "is_paid": payment.is_paid,
            "remote_link": remote_link,
            "transaction": transaction_payload,
        }

    async def _finalize_wata_payment(
        self,
        db: AsyncSession,
        payment: Any,
        transaction_payload: Dict[str, Any],
    ) -> Any:
        payment_module = import_module("app.services.payment_service")

        if isinstance(transaction_payload, dict):
            paid_status = transaction_payload.get("status") or transaction_payload.get("statusName")
        else:
            paid_status = None
        if paid_status and str(paid_status).lower() not in {"paid", "declined", "pending"}:
            logger.debug(
                "Неизвестный статус WATA транзакции %s: %s",
                getattr(payment, "payment_link_id", ""),
                paid_status,
            )

        paid_at = None
        if isinstance(transaction_payload, dict):
            paid_at = WataService._parse_datetime(transaction_payload.get("paymentTime"))
        if not paid_at and getattr(payment, "paid_at", None):
            paid_at = payment.paid_at
        existing_metadata = dict(getattr(payment, "metadata_json", {}) or {})

        invoice_message = existing_metadata.get("invoice_message") or {}
        invoice_message_removed = False
        if getattr(self, "bot", None) and invoice_message:
            chat_id = invoice_message.get("chat_id")
            message_id = invoice_message.get("message_id")
            if chat_id and message_id:
                try:
                    await self.bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - depends on rights
                    logger.warning(
                        "Не удалось удалить счёт WATA %s: %s",
                        message_id,
                        delete_error,
                    )
                else:
                    invoice_message_removed = True
                    existing_metadata.pop("invoice_message", None)

        existing_metadata["transaction"] = transaction_payload

        await payment_module.update_wata_payment_status(
            db,
            payment=payment,
            status="Paid",
            is_paid=True,
            paid_at=paid_at,
            callback_payload=transaction_payload,
            metadata=existing_metadata,
        )

        if payment.transaction_id:
            logger.info(
                "WATA платеж %s уже привязан к транзакции %s",
                payment.payment_link_id,
                payment.transaction_id,
            )
            return payment

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error("Пользователь %s не найден при обработке WATA", payment.user_id)
            return payment

        transaction_external_id = str(transaction_payload.get("id") or transaction_payload.get("transactionId") or "")
        description = f"Пополнение через WATA ({payment.payment_link_id})"

        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=payment.amount_kopeks,
            description=description,
            payment_method=PaymentMethod.WATA,
            external_id=transaction_external_id or payment.payment_link_id,
            is_completed=True,
        )

        await payment_module.link_wata_payment_to_transaction(db, payment, transaction.id)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(user)

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, "subscription", None)
        referrer_info = format_referrer_info(user)
        topup_status = "🆕 Первое пополнение" if was_first_topup else "🔄 Пополнение"

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, "bot", None),
            )
        except Exception as error:
            logger.error("Ошибка обработки реферального пополнения WATA: %s", error)

        if was_first_topup and not user.has_made_first_topup:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        if getattr(self, "bot", None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:
                logger.error("Ошибка отправки админ уведомления WATA: %s", error)

        # Сначала пробуем автопокупку
        auto_purchase_success = False
        try:
            from app.services.user_cart_service import user_cart_service

            checkout_snapshot = extract_checkout_snapshot(payment)
            has_saved_cart = await user_cart_service.has_user_cart(user.id)
            if has_saved_cart or checkout_snapshot:
                try:
                    auto_purchase_success = await auto_purchase_saved_cart_after_topup(
                        db,
                        user,
                        bot=getattr(self, "bot", None),
                        checkout_snapshot=checkout_snapshot,
                    )
                except Exception as auto_error:
                    logger.error(
                        "Ошибка автоматической покупки подписки для пользователя %s: %s",
                        user.id,
                        auto_error,
                        exc_info=True,
                    )

                if auto_purchase_success:
                    has_saved_cart = False
        except Exception as error:
            logger.debug("Не удалось отправить напоминание о корзине после WATA: %s", error)

        # Уведомление пользователю только если автопокупка не сработала
        if not auto_purchase_success and getattr(self, "bot", None):
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    format_topup_success_message(settings.format_price(payment.amount_kopeks)),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error("Ошибка отправки уведомления пользователю WATA: %s", error)

        return payment
