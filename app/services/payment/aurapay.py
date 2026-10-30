"""Mixin интеграции AuraPay в PaymentService.

Шлюз живёт только в роутере платежей: глобальный вес 0, выпадает у витрин с
собственными весами (`scripts.copycats weights BOT aurapay=1`). Поэтому всё,
что уходит пользователю (удаление счёта, сообщение об успехе), идёт через бот,
выставивший счёт (`payment_router.bot_id`), — пользователь основного бота мог
оплатить в витрине, и ответ должен прийти туда же.

Касса — основная или своя у витрины (`scripts.copycats set BOT aurapay_shop=mig`).
Её имя пишется в `metadata_json.aurapay_shop` счёта: проверка статуса и сверка
вебхука идут в ту же кассу, даже если витрину потом перевели на другую.

Защита от повторного зачёта — атомарный CAS: AuraPay ретраит вебхук до 5 раз,
параллельно могут прийти «Проверить оплату» и автосверка.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from importlib import import_module
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.branding.context import brand_scope
from app.config import settings
from app.database.models import AurapayPayment, PaymentMethod, TransactionType
from app.services.aurapay_service import (
    AurapayAPIError,
    aurapay_service_for_shop,
    normalize_shop,
)
from app.services.payment.common import _record_router_payment
from app.services.subscription_auto_purchase_service import (
    auto_purchase_saved_cart_after_topup,
)
from app.services.tariff_partial_payment_service import (
    SNAPSHOT_METADATA_KEY,
    build_invoice_checkout_snapshot,
    extract_checkout_snapshot,
)
from app.utils.bot_registry import bot_for_user, get_aurapay_shop, get_bot_instance
from app.utils.success_notifications import format_topup_success_message
from app.utils.user_utils import format_referrer_info

logger = logging.getLogger(__name__)

GATEWAY = "aurapay"
SHOP_METADATA_KEY = "aurapay_shop"


def parse_amount_to_kopeks(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(round(float(str(raw).replace(",", ".")) * 100))
    except (TypeError, ValueError):
        return None


def payment_shop(payment: Any) -> Optional[str]:
    """Касса, в которой выставлен счёт; None — основная."""
    return normalize_shop((getattr(payment, "metadata_json", None) or {}).get(SHOP_METADATA_KEY))


class AurapayPaymentMixin:
    """Создание счетов AuraPay, вебхук, проверка статуса и зачисление."""

    def _aurapay_service_for(self, shop: Optional[str]) -> Any:
        """Клиент кассы; None — AuraPay выключена или касса не описана в .env."""
        service = getattr(self, "aurapay_service", None)
        if service is None or shop is None:
            return service
        return aurapay_service_for_shop(shop)

    def _aurapay_payment_bot(self, payment: Any, user: Any) -> tuple:
        """(бот, bot_id бренда) для сообщений по счёту: бот, где выставлен счёт,
        иначе бот регистрации пользователя, иначе основной."""
        base = getattr(self, "bot", None)
        if base is None:
            return None, None
        router_meta = (getattr(payment, "metadata_json", None) or {}).get("payment_router") or {}
        bot_id = router_meta.get("bot_id")
        invoice_bot = get_bot_instance(bot_id)
        if invoice_bot is not None:
            return invoice_bot, bot_id
        return bot_for_user(user, base), getattr(user, "bot_id", None)

    async def create_aurapay_payment(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        description: str,
        *,
        language: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        router_meta = (metadata or {}).get("payment_router") or {}
        shop = normalize_shop(get_aurapay_shop(router_meta.get("bot_id")))
        service = self._aurapay_service_for(shop)
        if not service:
            # Своя касса витрины без ключей — не в основную: деньги ушли бы чужому.
            logger.error("AuraPay касса %s не настроена", shop or "основная")
            return None

        if amount_kopeks < settings.AURAPAY_MIN_AMOUNT_KOPEKS:
            logger.warning(
                "Сумма AuraPay меньше минимальной: %s < %s",
                amount_kopeks,
                settings.AURAPAY_MIN_AMOUNT_KOPEKS,
            )
            return None
        if amount_kopeks > settings.AURAPAY_MAX_AMOUNT_KOPEKS:
            logger.warning(
                "Сумма AuraPay больше максимальной: %s > %s",
                amount_kopeks,
                settings.AURAPAY_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module("app.services.payment_service")

        order_id = f"aurapay_{user_id}_{uuid.uuid4().hex[:12]}"
        lifetime = int(settings.AURAPAY_INVOICE_LIFETIME_MINUTES or 60)

        try:
            response = await service.create_invoice(
                amount_kopeks=amount_kopeks,
                order_id=order_id,
                comment=description,
                callback_url=settings.get_aurapay_webhook_url(),
                lifetime_minutes=lifetime,
            )
        except AurapayAPIError as error:
            logger.error("Ошибка создания AuraPay инвойса: %s", error)
            return None
        except Exception as error:  # pragma: no cover - safety net
            logger.exception("Непредвиденная ошибка при создании AuraPay инвойса: %s", error)
            return None

        invoice_id = response.get("id")
        payment_url = (response.get("payment_data") or {}).get("url")
        if not invoice_id or not payment_url:
            logger.error("AuraPay не вернула id или ссылку оплаты: %s", response)
            return None

        # Время в ответе AuraPay без зоны — считаем срок сами от UTC.
        expires_at = datetime.utcnow() + timedelta(minutes=lifetime)

        payment_metadata: Dict[str, Any] = {
            "response": response,
            "language": language or settings.DEFAULT_LANGUAGE,
            **(metadata or {}),
        }
        if shop:
            payment_metadata[SHOP_METADATA_KEY] = shop
        snapshot = await build_invoice_checkout_snapshot(user_id, amount_kopeks)
        if snapshot:
            payment_metadata[SNAPSHOT_METADATA_KEY] = snapshot

        local_payment = await payment_module.create_aurapay_payment(
            db,
            user_id=user_id,
            order_id=order_id,
            invoice_id=str(invoice_id),
            amount_kopeks=amount_kopeks,
            description=description,
            status=str(response.get("status") or AurapayPayment.STATUS_PENDING).upper(),
            url=payment_url,
            metadata=payment_metadata,
            expires_at=expires_at,
        )

        return {
            "local_payment_id": local_payment.id,
            "invoice_id": str(invoice_id),
            "payment_url": payment_url,
            "order_id": order_id,
            "status": local_payment.status,
            "expires_at": expires_at,
        }

    # ------------------------------------------------------------------ вебхук

    async def process_aurapay_webhook(
        self,
        db: AsyncSession,
        payload: Dict[str, Any],
    ) -> bool:
        """Подпись уже проверена роутом. False — платёж не наш / не найден."""
        payment_module = import_module("app.services.payment_service")

        if not isinstance(payload, dict):
            logger.error("AuraPay webhook не словарь: %s", payload)
            return False

        order_id = str(payload.get("order_id") or "").strip() or None
        invoice_id = str(payload.get("id") or "").strip() or None
        status = str(payload.get("status") or "").strip().upper()

        payment = None
        if order_id:
            payment = await payment_module.get_aurapay_payment_by_order_id(db, order_id)
        if not payment and invoice_id:
            payment = await payment_module.get_aurapay_payment_by_invoice_id(db, invoice_id)
        if not payment:
            logger.error(
                "AuraPay платеж не найден (order_id=%s, id=%s)", order_id, invoice_id
            )
            return False

        shop_id = str(payload.get("shop_id") or "").strip()
        shop_service = self._aurapay_service_for(payment_shop(payment))
        expected_shop_id = getattr(shop_service, "shop_id", None) or settings.AURAPAY_SHOP_ID
        if shop_id and expected_shop_id and shop_id != expected_shop_id:
            logger.error(
                "AuraPay webhook чужой кассы %s для платежа %s", shop_id, payment.order_id
            )
            return False

        return await self._apply_aurapay_status(db, payment, status, payload)

    async def _apply_aurapay_status(
        self,
        db: AsyncSession,
        payment: AurapayPayment,
        status: str,
        payload: Dict[str, Any],
    ) -> bool:
        payment_module = import_module("app.services.payment_service")

        if status == AurapayPayment.STATUS_PAID:
            if payment.is_paid:
                logger.info("AuraPay платеж %s уже оплачен", payment.order_id)
                return True

            paid_amount = parse_amount_to_kopeks(payload.get("amount"))
            if paid_amount is not None and paid_amount < payment.amount_kopeks:
                logger.error(
                    "AuraPay: оплачено %s коп. меньше суммы счёта %s (%s), не зачисляем",
                    paid_amount,
                    payment.order_id,
                    payment.amount_kopeks,
                )
                await payment_module.update_aurapay_payment(
                    db, payment, callback_payload=payload
                )
                return True

            await self._finalize_aurapay_payment(db, payment, payload)
            return True

        if status and not payment.is_paid and status != payment.status:
            await payment_module.update_aurapay_payment(
                db, payment, status=status, callback_payload=payload
            )
        return True

    # ------------------------------------------------------- статус (кнопка)

    async def get_aurapay_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> Optional[Dict[str, Any]]:
        payment_module = import_module("app.services.payment_service")

        payment = await payment_module.get_aurapay_payment_by_id(db, local_payment_id)
        if not payment:
            return None

        remote: Optional[Dict[str, Any]] = None
        service = self._aurapay_service_for(payment_shop(payment))
        if service and not payment.is_paid:
            try:
                remote = await service.get_invoice_status(
                    invoice_id=payment.invoice_id, order_id=payment.order_id
                )
            except AurapayAPIError as error:
                logger.error("Ошибка статуса AuraPay %s: %s", payment.order_id, error)
            except Exception as error:  # pragma: no cover - safety net
                logger.exception("Непредвиденная ошибка статуса AuraPay: %s", error)

        if remote:
            remote_status = str(remote.get("status") or "").upper()
            if remote_status:
                await self._apply_aurapay_status(db, payment, remote_status, remote)
                payment = (
                    await payment_module.get_aurapay_payment_by_id(db, local_payment_id)
                    or payment
                )

        return {
            "payment": payment,
            "status": payment.status,
            "is_paid": payment.is_paid,
            "remote": remote,
        }

    # -------------------------------------------------------------- зачисление

    async def _finalize_aurapay_payment(
        self,
        db: AsyncSession,
        payment: AurapayPayment,
        payload: Dict[str, Any],
    ) -> AurapayPayment:
        payment_module = import_module("app.services.payment_service")

        won = await payment_module.mark_aurapay_payment_paid_cas(
            db, payment.id, paid_at=datetime.utcnow()
        )
        if not won:
            logger.info("AuraPay платеж %s уже финализируется", payment.order_id)
            return payment

        payment = await payment_module.get_aurapay_payment_by_id(db, payment.id) or payment

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error("Пользователь %s не найден при обработке AuraPay", payment.user_id)
            return payment

        user_bot, brand_bot_id = self._aurapay_payment_bot(payment, user)

        existing_metadata = dict(getattr(payment, "metadata_json", {}) or {})
        invoice_message = existing_metadata.get("invoice_message") or {}
        if user_bot and invoice_message:
            chat_id = invoice_message.get("chat_id")
            message_id = invoice_message.get("message_id")
            if chat_id and message_id:
                try:
                    await user_bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - depends on rights
                    logger.warning("Не удалось удалить счёт AuraPay %s: %s", message_id, delete_error)
                else:
                    existing_metadata.pop("invoice_message", None)
        existing_metadata["callback"] = payload

        await payment_module.update_aurapay_payment(
            db,
            payment,
            callback_payload=payload,
            metadata=existing_metadata,
        )

        if payment.transaction_id:
            logger.info(
                "AuraPay платеж %s уже привязан к транзакции %s",
                payment.order_id,
                payment.transaction_id,
            )
            return payment

        credit_amount_kopeks = int(payment.amount_kopeks)
        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=credit_amount_kopeks,
            description=f"Пополнение через {settings.get_aurapay_display_name()} ({payment.order_id})",
            payment_method=PaymentMethod.AURAPAY,
            external_id=payment.invoice_id or payment.order_id,
            is_completed=True,
        )
        await payment_module.link_aurapay_payment_to_transaction(db, payment, transaction.id)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += credit_amount_kopeks
        user.updated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(user)

        await _record_router_payment(
            db,
            gateway=GATEWAY,
            payment=payment,
            transaction=transaction,
            amount_kopeks=credit_amount_kopeks,
        )

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db, user.id, credit_amount_kopeks, getattr(self, "bot", None)
            )
        except Exception as error:
            logger.error("Ошибка обработки реферального пополнения AuraPay: %s", error)

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
                    topup_status="🆕 Первое пополнение" if was_first_topup else "🔄 Пополнение",
                    referrer_info=format_referrer_info(user),
                    subscription=getattr(user, "subscription", None),
                    promo_group=user.get_primary_promo_group(),
                    db=db,
                )
            except Exception as error:
                logger.error("Ошибка отправки админ уведомления AuraPay: %s", error)

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
                        bot=user_bot,
                        checkout_snapshot=checkout_snapshot,
                    )
                except Exception as auto_error:
                    logger.error(
                        "Ошибка автоматической покупки подписки для пользователя %s: %s",
                        user.id,
                        auto_error,
                        exc_info=True,
                    )
        except Exception as error:
            logger.debug("Не удалось проверить корзину после AuraPay: %s", error)

        if not auto_purchase_success:
            await self._notify_aurapay_topup(user, credit_amount_kopeks, user_bot, brand_bot_id)

        return payment

    async def _notify_aurapay_topup(
        self, user: Any, amount_kopeks: int, bot: Any, brand_bot_id: Optional[int]
    ) -> None:
        if not bot or not getattr(user, "telegram_id", None):
            return
        with brand_scope(brand_bot_id):
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await bot.send_message(
                    user.telegram_id,
                    format_topup_success_message(settings.format_price(amount_kopeks)),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error("Ошибка отправки уведомления пользователю AuraPay: %s", error)
