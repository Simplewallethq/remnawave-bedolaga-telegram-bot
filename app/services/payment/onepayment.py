"""Mixin интеграции 1Payment (СБП) в PaymentService.

Отличия от WATA/Platega:
* каждый первичный счёт выставляется с subscribe=1 — успешная оплата приносит
  в колбеке token, который становится активной привязкой пользователя
  (OnePaymentBinding) и потом используется для автосписания за продление;
* защита от повторного зачёта — атомарный CAS (UPDATE ... WHERE is_paid=false):
  1Payment ретраит колбек раз в минуту 10 минут, а параллельно может прийти
  «Проверить оплату»;
* рекуррентный платёж несёт в metadata снимок `renewal`, по которому после
  зачисления баланса сразу проводится продление подписки.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from importlib import import_module
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    OnePaymentBinding,
    OnePaymentPayment,
    PaymentMethod,
    Subscription,
    SubscriptionStatus,
    TransactionType,
)
from app.localization.texts import get_texts
from app.services.onepayment_service import (
    ERROR_TRANSACTION_NOT_FOUND,
    STATUS_FAILURE,
    STATUS_INIT,
    STATUS_LABELS,
    STATUS_PENDING,
    STATUS_SUCCESS,
    OnePaymentAPIError,
    parse_amount_to_kopeks,
    parse_status,
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
from app.services.trial_paid_offer_service import trial_paid_offer_service
from app.utils.success_notifications import format_topup_success_message
from app.utils.user_utils import format_referrer_info

logger = logging.getLogger(__name__)

GATEWAY = "onepayment"
RENEWAL_METADATA_KEY = "renewal"
RENEWAL_SOURCE = "onepayment_recurring"
RECURRING_INVOICE_TTL = timedelta(hours=24)


def _is_truthy_flag(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _status_label(status: Optional[int]) -> str:
    if status is None:
        return OnePaymentPayment.STATUS_PENDING
    return STATUS_LABELS.get(status, str(status))


def _parse_period_end(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed.replace(microsecond=0)


class OnePaymentPaymentMixin:
    """Создание счетов, обработка колбеков и рекуррентные списания 1Payment."""

    # ------------------------------------------------------------- первичный счёт

    async def create_onepayment_payment(
        self,
        db: AsyncSession,
        user_id: int,
        amount_kopeks: int,
        description: str,
        *,
        language: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        allow_below_min: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Первичный счёт с subscribe=1.

        `allow_below_min` — пропустить проверку ONEPAYMENT_MIN_AMOUNT_KOPEKS
        (оффер «доступ за 1 ₽»); максимум проверяется всегда.
        """
        service = getattr(self, "onepayment_service", None)
        if not service or not service.is_configured:
            logger.error("1Payment сервис не инициализирован")
            return None

        if amount_kopeks <= 0:
            logger.warning("Сумма 1Payment должна быть положительной: %s", amount_kopeks)
            return None
        if not allow_below_min and amount_kopeks < settings.ONEPAYMENT_MIN_AMOUNT_KOPEKS:
            logger.warning(
                "Сумма 1Payment меньше минимальной: %s < %s",
                amount_kopeks,
                settings.ONEPAYMENT_MIN_AMOUNT_KOPEKS,
            )
            return None
        if amount_kopeks > settings.ONEPAYMENT_MAX_AMOUNT_KOPEKS:
            logger.warning(
                "Сумма 1Payment больше максимальной: %s > %s",
                amount_kopeks,
                settings.ONEPAYMENT_MAX_AMOUNT_KOPEKS,
            )
            return None

        payment_module = import_module("app.services.payment_service")
        user_data = f"1p_{user_id}_{uuid.uuid4().hex[:12]}"

        try:
            response = await service.create_payment(
                amount_kopeks=amount_kopeks,
                user_data=user_data,
                description=description,
                language=language or settings.DEFAULT_LANGUAGE,
                user_id=user_id,
                subscribe=True,
            )
        except OnePaymentAPIError as error:
            logger.error("Ошибка создания 1Payment платежа: %s", error)
            return None
        except Exception as error:  # pragma: no cover - safety net
            logger.exception("Непредвиденная ошибка при создании 1Payment платежа: %s", error)
            return None

        payment_url = response.get("url")
        if not payment_url:
            logger.error("1Payment не вернул ссылку на оплату: %s", response.get("raw"))
            return None

        status_value = response.get("status")
        status = _status_label(status_value) if status_value is not None else OnePaymentPayment.STATUS_INIT

        payment_metadata: Dict[str, Any] = {
            "response": response.get("raw"),
            "init_method": response.get("method"),
            "language": language or settings.DEFAULT_LANGUAGE,
            "subscribe": True,
            **(metadata or {}),
        }
        snapshot = await build_invoice_checkout_snapshot(user_id, amount_kopeks)
        if snapshot:
            payment_metadata[SNAPSHOT_METADATA_KEY] = snapshot

        local_payment = await payment_module.create_onepayment_payment(
            db,
            user_id=user_id,
            user_data=user_data,
            amount_kopeks=amount_kopeks,
            currency="RUB",
            description=description,
            status=status,
            url=payment_url,
            provider_order_id=response.get("order_id"),
            metadata=payment_metadata,
            expires_at=None,
        )

        logger.info(
            "Создан 1Payment платеж %s на %s₽ для пользователя %s",
            user_data,
            amount_kopeks / 100,
            user_id,
        )

        return {
            "local_payment_id": local_payment.id,
            "order_id": user_data,
            "provider_order_id": response.get("order_id"),
            "payment_url": payment_url,
            "status": status,
        }

    # ------------------------------------------------------------------- колбек

    async def process_onepayment_webhook(
        self,
        db: AsyncSession,
        payload: Dict[str, Any],
    ) -> bool:
        """Колбек 1Payment. Подпись уже проверена в HTTP-роуте.

        Возвращает True всегда, когда колбек понят, — включая «платёж не найден»
        и дубликаты: иначе 1Payment будет ретраить его 10 раз.
        """
        payment_module = import_module("app.services.payment_service")

        if not isinstance(payload, dict):
            logger.error("1Payment webhook payload не является словарём: %s", payload)
            return False

        user_data = str(payload.get("user_data") or "").strip()
        provider_order_id = str(payload.get("order_id") or "").strip() or None
        status = parse_status(payload.get("status"))

        if not user_data and not provider_order_id:
            logger.error("1Payment webhook без user_data и order_id: %s", payload)
            return False
        if status is None:
            logger.error("1Payment webhook без статуса: %s", payload)
            return False

        payment = None
        if user_data:
            payment = await payment_module.get_onepayment_payment_by_user_data(db, user_data)
        if not payment and provider_order_id:
            payment = await payment_module.get_onepayment_payment_by_provider_order_id(
                db, provider_order_id
            )
        if not payment:
            logger.warning(
                "1Payment webhook: платеж не найден (user_data=%s, order_id=%s)",
                user_data,
                provider_order_id,
            )
            return True

        if _is_truthy_flag(payload.get("test")) and not settings.ONEPAYMENT_ACCEPT_TEST_PAYMENTS:
            logger.warning(
                "1Payment: тестовый колбек для платежа %s проигнорирован (ONEPAYMENT_ACCEPT_TEST_PAYMENTS=False)",
                payment.user_data,
            )
            return True

        metadata = dict(getattr(payment, "metadata_json", {}) or {})
        metadata["last_webhook"] = payload
        update_kwargs: Dict[str, Any] = {
            "metadata": metadata,
            "callback_payload": payload,
        }
        if provider_order_id:
            update_kwargs["provider_order_id"] = provider_order_id
        status_code = payload.get("status_code")
        if status_code:
            update_kwargs["status_code"] = str(status_code)

        if status == STATUS_SUCCESS:
            if payment.is_paid:
                logger.info("1Payment платеж %s уже помечен как оплачен", payment.user_data)
                await payment_module.update_onepayment_payment(db, payment, **update_kwargs)
                return True
            payment = await payment_module.update_onepayment_payment(db, payment, **update_kwargs)
            await self._finalize_onepayment_payment(db, payment, payload)
            return True

        if status == STATUS_FAILURE:
            if payment.is_paid:
                logger.warning(
                    "1Payment: FAILURE для уже оплаченного платежа %s проигнорирован",
                    payment.user_data,
                )
                return True
            payment = await payment_module.update_onepayment_payment(
                db,
                payment,
                status=OnePaymentPayment.STATUS_FAILURE,
                is_paid=False,
                **update_kwargs,
            )
            logger.info(
                "1Payment платеж %s отклонён (status_code=%s)",
                payment.user_data,
                status_code,
            )
            if payment.is_recurring:
                await self._handle_onepayment_recurring_failure(db, payment, payload)
            return True

        if status in {STATUS_INIT, STATUS_PENDING}:
            if not payment.is_paid:
                update_kwargs["status"] = _status_label(status)
                redirect_url = payload.get("redirect_url")
                if redirect_url and not payment.url:
                    update_kwargs["url"] = str(redirect_url)
            await payment_module.update_onepayment_payment(db, payment, **update_kwargs)
            return True

        # REFUND / REFUND_PENDING и прочее — фиксируем, деньги не трогаем.
        update_kwargs["status"] = _status_label(status)
        await payment_module.update_onepayment_payment(db, payment, **update_kwargs)
        logger.info("1Payment платеж %s перешёл в статус %s", payment.user_data, _status_label(status))
        return True

    # --------------------------------------------------------------- финализация

    async def _finalize_onepayment_payment(
        self,
        db: AsyncSession,
        payment: OnePaymentPayment,
        payload: Dict[str, Any],
    ) -> OnePaymentPayment:
        payment_module = import_module("app.services.payment_service")

        won = await payment_module.mark_onepayment_payment_paid_cas(
            db, payment.id, paid_at=datetime.utcnow()
        )
        if not won:
            logger.info(
                "1Payment платеж %s уже финализируется другим обработчиком", payment.user_data
            )
            return payment

        payment = await payment_module.get_onepayment_payment_by_id(db, payment.id) or payment

        existing_metadata = dict(getattr(payment, "metadata_json", {}) or {})

        invoice_message = existing_metadata.get("invoice_message") or {}
        if getattr(self, "bot", None) and invoice_message:
            chat_id = invoice_message.get("chat_id")
            message_id = invoice_message.get("message_id")
            if chat_id and message_id:
                try:
                    await self.bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - depends on rights
                    logger.warning("Не удалось удалить счёт 1Payment %s: %s", message_id, delete_error)
                else:
                    existing_metadata.pop("invoice_message", None)

        callback_amount = parse_amount_to_kopeks(payload.get("merchant_price"))
        if callback_amount is not None and callback_amount != payment.amount_kopeks:
            logger.warning(
                "1Payment: сумма в колбеке (%s) не совпадает со счётом %s (%s), зачисляем сумму счёта",
                callback_amount,
                payment.user_data,
                payment.amount_kopeks,
            )
        existing_metadata["callback"] = payload

        await payment_module.update_onepayment_payment(
            db,
            payment,
            status=OnePaymentPayment.STATUS_SUCCESS,
            is_paid=True,
            callback_payload=payload,
            metadata=existing_metadata,
            provider_order_id=str(payload.get("order_id")) if payload.get("order_id") else None,
        )

        if payment.transaction_id:
            logger.info(
                "1Payment платеж %s уже привязан к транзакции %s",
                payment.user_data,
                payment.transaction_id,
            )
            return payment

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error("Пользователь %s не найден при обработке 1Payment", payment.user_id)
            return payment

        credit_amount_kopeks = int(payment.amount_kopeks)
        description = (
            f"Автосписание по СБП ({settings.get_onepayment_display_name()})"
            if payment.is_recurring
            else f"Пополнение через {settings.get_onepayment_display_name()} ({payment.user_data})"
        )

        transaction = await payment_module.create_transaction(
            db,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            amount_kopeks=credit_amount_kopeks,
            description=description,
            payment_method=PaymentMethod.ONEPAYMENT,
            external_id=payment.provider_order_id or payment.user_data,
            is_completed=True,
        )
        await payment_module.link_onepayment_payment_to_transaction(db, payment, transaction.id)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup
        # Оффер «за 1 ₽»: символическая оплата не считается первым пополнением.
        paid_trial_snapshot = trial_paid_offer_service.extract_snapshot(existing_metadata)

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

        # Токен рекуррентов: приходит при привязке, может обновиться при списании.
        binding: Optional[OnePaymentBinding] = None
        token = str(payload.get("token") or "").strip()
        if token:
            try:
                binding = await payment_module.upsert_active_onepayment_binding(
                    db,
                    user_id=user.id,
                    token=token,
                    source_payment_id=payment.id,
                )
                logger.info(
                    "1Payment: привязка #%s пользователя %s обновлена по платежу %s",
                    binding.id,
                    user.id,
                    payment.user_data,
                )
            except Exception as error:
                logger.error(
                    "1Payment: не удалось сохранить привязку токена для пользователя %s: %s",
                    user.id,
                    error,
                    exc_info=True,
                )
                binding = None
        elif payment.binding_id:
            binding = await payment_module.get_onepayment_binding_by_id(db, payment.binding_id)

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(db, user.id, credit_amount_kopeks, getattr(self, "bot", None))
        except Exception as error:
            logger.error("Ошибка обработки реферального пополнения 1Payment: %s", error)

        if was_first_topup and not user.has_made_first_topup and paid_trial_snapshot is None:
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
                    topup_status=(
                        "🔁 Автосписание СБП"
                        if payment.is_recurring
                        else ("🆕 Первое пополнение" if was_first_topup else "🔄 Пополнение")
                    ),
                    referrer_info=format_referrer_info(user),
                    subscription=getattr(user, "subscription", None),
                    promo_group=user.get_primary_promo_group(),
                    db=db,
                )
            except Exception as error:
                logger.error("Ошибка отправки админ уведомления 1Payment: %s", error)

        if payment.is_recurring:
            # Отмечаем удачу самого списания отдельно от продления: деньги банк
            # уже отдал, даже если продлевать нечего (подписку продлили вручную).
            if binding is not None:
                try:
                    binding = (
                        await payment_module.get_onepayment_binding_by_id_for_update(db, binding.id)
                        or binding
                    )
                    await payment_module.update_onepayment_binding(
                        db,
                        binding,
                        last_charge_status=OnePaymentPayment.STATUS_SUCCESS,
                        failed_attempts=0,
                    )
                except Exception as error:
                    logger.warning(
                        "1Payment: не удалось отметить удачное списание в привязке #%s: %s",
                        binding.id,
                        error,
                    )

            renewed = False
            try:
                renewed = await self._apply_onepayment_recurring_renewal(db, payment, user, binding)
            except Exception as error:
                # Деньги уже на балансе; страховка — ветка «баланс покрывает цену»
                # в следующем цикле мониторинга. Наверх не пробрасываем: колбек
                # должен получить 200.
                logger.error(
                    "1Payment: ошибка продления по рекурренту %s: %s",
                    payment.user_data,
                    error,
                    exc_info=True,
                )
            if not renewed:
                await self._notify_onepayment_topup(user, credit_amount_kopeks)
            return payment

        # Оффер «доступ за 1 ₽»: счёт самодостаточен — активируем по снимку.
        if paid_trial_snapshot is not None:
            activated = False
            try:
                activated = await trial_paid_offer_service.activate_from_payment(
                    db, user, paid_trial_snapshot, bot=getattr(self, "bot", None)
                )
            except Exception as error:
                logger.error(
                    "1Payment: ошибка активации платного триала по счёту %s: %s",
                    payment.user_data,
                    error,
                    exc_info=True,
                )
            if activated:
                return payment
            await self._notify_onepayment_topup(user, credit_amount_kopeks)
            return payment

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
        except Exception as error:
            logger.debug("Не удалось проверить корзину после 1Payment: %s", error)

        if not auto_purchase_success:
            await self._notify_onepayment_topup(user, credit_amount_kopeks)

        return payment

    async def _notify_onepayment_topup(self, user: Any, amount_kopeks: int) -> None:
        bot = getattr(self, "bot", None)
        if not bot or not getattr(user, "telegram_id", None):
            return
        try:
            keyboard = await self.build_topup_success_keyboard(user)
            await bot.send_message(
                user.telegram_id,
                format_topup_success_message(settings.format_price(amount_kopeks)),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception as error:
            logger.error("Ошибка отправки уведомления пользователю 1Payment: %s", error)

    # ------------------------------------------------------------ статус (кнопка)

    async def get_onepayment_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> Optional[Dict[str, Any]]:
        payment_module = import_module("app.services.payment_service")

        payment = await payment_module.get_onepayment_payment_by_id(db, local_payment_id)
        if not payment:
            return None

        remote: Optional[Dict[str, Any]] = None
        service = getattr(self, "onepayment_service", None)
        if service and not payment.is_paid:
            try:
                remote = await service.get_payment_status(user_data=payment.user_data)
            except OnePaymentAPIError as error:
                if error.error_code == ERROR_TRANSACTION_NOT_FOUND:
                    # Пользователь ещё не открыл форму оплаты — счёт просто ждёт.
                    logger.debug(
                        "1Payment: транзакция %s ещё не создана на стороне провайдера",
                        payment.user_data,
                    )
                else:
                    logger.error(
                        "Ошибка получения статуса 1Payment %s: %s", payment.user_data, error
                    )
            except Exception as error:  # pragma: no cover - safety net
                logger.exception("Непредвиденная ошибка статуса 1Payment: %s", error)

        if remote:
            remote_status = parse_status(remote.get("status"))
            if remote_status == STATUS_SUCCESS and not payment.is_paid:
                payload = {str(k): ("" if v is None else str(v)) for k, v in remote.items()}
                payment = await self._finalize_onepayment_payment(db, payment, payload)
                payment = await payment_module.get_onepayment_payment_by_id(db, payment.id) or payment
            elif remote_status is not None and not payment.is_paid:
                label = _status_label(remote_status)
                if label != payment.status:
                    payment = await payment_module.update_onepayment_payment(
                        db,
                        payment,
                        status=label,
                        provider_order_id=str(remote.get("order_id")) if remote.get("order_id") else None,
                    )
                if remote_status == STATUS_FAILURE and payment.is_recurring:
                    await self._handle_onepayment_recurring_failure(db, payment, remote)

        return {
            "payment": payment,
            "status": payment.status,
            "is_paid": payment.is_paid,
            "remote": remote,
        }

    # ------------------------------------------------------------- рекурренты

    async def charge_onepayment_binding(
        self,
        db: AsyncSession,
        binding: OnePaymentBinding,
        *,
        amount_kopeks: int,
        description: str,
        renewal_snapshot: Dict[str, Any],
        language: Optional[str] = None,
    ) -> Optional[OnePaymentPayment]:
        """Инициирует списание по токену. Итог приходит колбеком.

        None — запрос к 1Payment не прошёл (счётчик неудач увеличен).
        """
        service = getattr(self, "onepayment_service", None)
        if not service or not service.is_configured:
            logger.error("1Payment сервис не инициализирован для рекуррента")
            return None

        payment_module = import_module("app.services.payment_service")
        user_data = f"1p_{binding.user_id}_r{uuid.uuid4().hex[:12]}"
        now = datetime.utcnow()

        payment = await payment_module.create_onepayment_payment(
            db,
            user_id=binding.user_id,
            user_data=user_data,
            amount_kopeks=amount_kopeks,
            currency="RUB",
            description=description,
            status=OnePaymentPayment.STATUS_INIT,
            metadata={
                RENEWAL_METADATA_KEY: renewal_snapshot,
                "language": language or settings.DEFAULT_LANGUAGE,
                "binding_id": binding.id,
            },
            expires_at=now + RECURRING_INVOICE_TTL,
            is_recurring=True,
            binding_id=binding.id,
        )

        try:
            response = await service.charge_token(
                token=binding.token,
                amount_kopeks=amount_kopeks,
                user_data=user_data,
                description=description,
            )
        except OnePaymentAPIError as error:
            logger.error(
                "1Payment: рекуррентное списание по привязке #%s не принято: %s",
                binding.id,
                error,
            )
            await payment_module.update_onepayment_payment(
                db,
                payment,
                status=OnePaymentPayment.STATUS_FAILURE,
                status_code=f"api_error_{error.error_code}" if error.error_code is not None else "api_error",
                metadata={**(payment.metadata_json or {}), "api_error": str(error)},
            )
            await self._register_onepayment_charge_failure(db, binding, now)
            return None
        except Exception as error:  # pragma: no cover - safety net
            logger.exception("1Payment: непредвиденная ошибка рекуррента #%s: %s", binding.id, error)
            await payment_module.update_onepayment_payment(
                db,
                payment,
                status=OnePaymentPayment.STATUS_FAILURE,
                status_code="exception",
            )
            await self._register_onepayment_charge_failure(db, binding, now)
            return None

        remote_status = response.get("status")
        payment = await payment_module.update_onepayment_payment(
            db,
            payment,
            status=_status_label(remote_status) if remote_status is not None else OnePaymentPayment.STATUS_PENDING,
            provider_order_id=str(response.get("order_id")) if response.get("order_id") else None,
            metadata={**(payment.metadata_json or {}), "charge_response": response.get("raw")},
        )
        await payment_module.update_onepayment_binding(
            db,
            binding,
            last_charge_at=now,
            last_charge_status=OnePaymentPayment.STATUS_PENDING,
        )

        # Синхронный итог (редко, но допустим по доке).
        if remote_status == STATUS_SUCCESS:
            payload = {str(k): ("" if v is None else str(v)) for k, v in (response.get("raw") or {}).items()}
            payload.setdefault("user_data", user_data)
            await self._finalize_onepayment_payment(db, payment, payload)
        elif remote_status == STATUS_FAILURE:
            await self._handle_onepayment_recurring_failure(db, payment, response.get("raw") or {})

        return payment

    async def _register_onepayment_charge_failure(
        self,
        db: AsyncSession,
        binding: OnePaymentBinding,
        now: Optional[datetime] = None,
    ) -> OnePaymentBinding:
        payment_module = import_module("app.services.payment_service")
        attempts = int(binding.failed_attempts or 0) + 1
        binding = await payment_module.update_onepayment_binding(
            db,
            binding,
            last_charge_at=now or datetime.utcnow(),
            last_charge_status=OnePaymentPayment.STATUS_FAILURE,
            failed_attempts=attempts,
        )
        if attempts >= settings.get_onepayment_recurring_max_attempts():
            binding = await payment_module.deactivate_onepayment_binding(
                db, binding, status=OnePaymentBinding.STATUS_FAILED
            )
            logger.warning(
                "1Payment: привязка #%s переведена в FAILED после %s неудач",
                binding.id,
                attempts,
            )
        return binding

    async def _handle_onepayment_recurring_failure(
        self,
        db: AsyncSession,
        payment: OnePaymentPayment,
        payload: Dict[str, Any],
    ) -> None:
        payment_module = import_module("app.services.payment_service")
        binding = None
        if payment.binding_id:
            binding = await payment_module.get_onepayment_binding_by_id_for_update(db, payment.binding_id)
        if binding is not None and binding.status == OnePaymentBinding.STATUS_ACTIVE:
            binding = await self._register_onepayment_charge_failure(db, binding)

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            return
        await self._notify_onepayment_recurring_failed(
            user,
            payment.amount_kopeks,
            binding_failed=binding is not None and binding.status == OnePaymentBinding.STATUS_FAILED,
        )

    async def _apply_onepayment_recurring_renewal(
        self,
        db: AsyncSession,
        payment: OnePaymentPayment,
        user: Any,
        binding: Optional[OnePaymentBinding],
    ) -> bool:
        """Продлевает подписку по снимку `renewal` после зачисления денег.

        True — продление проведено (и пользователь уведомлён).
        False — продлевать нечего (подписка уже продлена/изменилась): деньги
        остаются на балансе, вызывающий шлёт обычное уведомление о пополнении.
        """
        payment_module = import_module("app.services.payment_service")
        metadata = dict(payment.metadata_json or {})
        snapshot = metadata.get(RENEWAL_METADATA_KEY)
        if not isinstance(snapshot, dict):
            return False
        if metadata.get("renewal_applied"):
            return True

        try:
            subscription_id = int(snapshot.get("subscription_id"))
            period_days = int(snapshot.get("period_days"))
            price_kopeks = int(snapshot.get("price_kopeks"))
        except (TypeError, ValueError):
            logger.warning("1Payment: некорректный снимок продления в платеже %s", payment.user_data)
            return False
        period_end = _parse_period_end(snapshot.get("period_end"))

        result = await db.execute(
            select(Subscription).where(Subscription.id == subscription_id).with_for_update()
        )
        subscription = result.scalar_one_or_none()
        if subscription is None or subscription.user_id != user.id:
            logger.warning("1Payment: подписка %s для продления не найдена", subscription_id)
            return False

        current_end = subscription.end_date.replace(microsecond=0) if subscription.end_date else None
        if period_end is not None and current_end != period_end:
            logger.info(
                "1Payment: подписка %s уже продлена вручную (end_date=%s, snapshot=%s), деньги остаются на балансе",
                subscription.id,
                current_end,
                period_end,
            )
            return False
        if subscription.status not in {SubscriptionStatus.ACTIVE.value, SubscriptionStatus.EXPIRED.value}:
            logger.info("1Payment: подписка %s в статусе %s, продление пропущено", subscription.id, subscription.status)
            return False
        if user.balance_kopeks < price_kopeks:
            logger.warning(
                "1Payment: после зачисления баланс %s < цены продления %s (подписка %s)",
                user.balance_kopeks,
                price_kopeks,
                subscription.id,
            )
            return False

        old_end_date = subscription.end_date
        transaction_id: Optional[int] = None

        if snapshot.get("is_legacy"):
            from app.database.crud.subscription import extend_subscription
            from app.database.crud.user import subtract_user_balance
            from app.services.subscription_service import SubscriptionService

            ok = await subtract_user_balance(
                db, user, price_kopeks, "Автопродление подписки (СБП 1Payment)"
            )
            if not ok:
                return False
            await extend_subscription(db, subscription, period_days)
            try:
                await SubscriptionService().update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=settings.RESET_TRAFFIC_ON_PAYMENT,
                    reset_reason="автопродление подписки (1Payment)",
                )
            except Exception as error:
                logger.warning("1Payment: не удалось пересинхронизировать подписку %s: %s", subscription.id, error)
        else:
            from app.handlers.subscription.tariffs import finalize_tariff_renewal
            from app.services.plan_pricing_service import get_plan_by_id

            plan_id = snapshot.get("plan_id") or subscription.plan_id
            plan = await get_plan_by_id(db, int(plan_id)) if plan_id else None
            if plan is None:
                logger.warning("1Payment: тариф %s подписки %s не найден", plan_id, subscription.id)
                return False
            renewal = await finalize_tariff_renewal(
                db, user, subscription, plan, period_days, price_kopeks
            )
            if renewal is None:
                return False
            subscription, transaction, old_end_date = renewal
            transaction_id = getattr(transaction, "id", None)

        await db.refresh(user)

        try:
            from app.database.crud.subscription_event import record_subscription_renewal_event

            await record_subscription_renewal_event(
                db,
                user_id=user.id,
                subscription_id=subscription.id,
                transaction_id=transaction_id,
                amount_kopeks=price_kopeks,
                period_days=period_days,
                previous_end_date=old_end_date,
                new_end_date=subscription.end_date,
                payment_method=PaymentMethod.ONEPAYMENT,
                balance_after=user.balance_kopeks,
                source=RENEWAL_SOURCE,
            )
        except Exception as error:
            logger.warning("1Payment: не удалось записать событие продления: %s", error)

        metadata["renewal_applied"] = True
        metadata["renewal_new_end_date"] = subscription.end_date.isoformat() if subscription.end_date else None
        await payment_module.update_onepayment_payment(db, payment, metadata=metadata)

        if binding is not None:
            try:
                binding = await payment_module.get_onepayment_binding_by_id_for_update(db, binding.id) or binding
                await payment_module.update_onepayment_binding(
                    db,
                    binding,
                    last_charge_status=OnePaymentPayment.STATUS_SUCCESS,
                    last_charged_period_end=old_end_date,
                    set_last_charged_period_end=True,
                    failed_attempts=0,
                )
            except Exception as error:
                logger.warning("1Payment: не удалось обновить привязку #%s: %s", binding.id, error)

        logger.info(
            "1Payment: подписка %s пользователя %s продлена на %s дн. по рекурренту (списано %s)",
            subscription.id,
            user.id,
            period_days,
            price_kopeks,
        )
        await self._notify_onepayment_recurring_success(user, payment.amount_kopeks, period_days, subscription)
        return True

    # ------------------------------------------------------------- отмена

    async def cancel_onepayment_binding(
        self,
        db: AsyncSession,
        *,
        binding_id: int,
        user_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Локальная отмена привязки: у 1Payment нет API отмены токена, мы просто
        перестаём им пользоваться."""
        payment_module = import_module("app.services.payment_service")
        binding = await payment_module.get_onepayment_binding_by_id_for_update(db, binding_id)
        if not binding or binding.user_id != user_id:
            return None
        if binding.status == OnePaymentBinding.STATUS_CANCELLED:
            return {"binding": binding, "already_cancelled": True}
        binding = await payment_module.deactivate_onepayment_binding(
            db, binding, status=OnePaymentBinding.STATUS_CANCELLED
        )
        logger.info("1Payment: привязка #%s пользователя %s отменена", binding.id, user_id)
        return {"binding": binding, "already_cancelled": False}

    # ---------------------------------------------------------- уведомления

    async def _notify_onepayment_recurring_success(
        self, user: Any, charged_kopeks: int, period_days: int, subscription: Subscription
    ) -> None:
        bot = getattr(self, "bot", None)
        if not bot or not getattr(user, "telegram_id", None):
            return
        try:
            from app.utils.timezone import format_local_datetime

            texts = get_texts(getattr(user, "language", None) or settings.DEFAULT_LANGUAGE)
            text = texts.t(
                "ONEPAYMENT_RECURRING_SUCCESS",
                "✅ <b>Подписка продлена автоматически</b>\n\n"
                "По СБП списано {amount}. Подписка продлена на {days} дн. — до {end_date}.\n\n"
                "Отключить автоплатёж можно в разделе «Управление подпиской → Автоплатеж».",
            ).format(
                amount=settings.format_price(charged_kopeks),
                days=period_days,
                end_date=format_local_datetime(subscription.end_date, "%d.%m.%Y %H:%M")
                if subscription.end_date
                else "—",
            )
            await bot.send_message(user.telegram_id, text, parse_mode="HTML")
        except Exception as error:
            logger.error("1Payment: не удалось уведомить об автопродлении пользователя %s: %s", user.id, error)

    async def _notify_onepayment_recurring_failed(
        self, user: Any, amount_kopeks: int, *, binding_failed: bool = False
    ) -> None:
        bot = getattr(self, "bot", None)
        if not bot or not getattr(user, "telegram_id", None):
            return
        try:
            from aiogram.types import InlineKeyboardMarkup

            from app.utils.miniapp_buttons import build_miniapp_or_callback_button

            texts = get_texts(getattr(user, "language", None) or settings.DEFAULT_LANGUAGE)
            if binding_failed:
                text = texts.t(
                    "ONEPAYMENT_RECURRING_FAILED_FINAL",
                    "❌ <b>Автоплатёж по СБП отключён</b>\n\n"
                    "Несколько попыток списать {amount} не удались. Продлите подписку вручную — "
                    "при оплате по СБП автоплатёж подключится заново.",
                ).format(amount=settings.format_price(amount_kopeks))
            else:
                text = texts.t(
                    "ONEPAYMENT_RECURRING_FAILED",
                    "❌ <b>Не удалось списать оплату по СБП</b>\n\n"
                    "Банк отклонил автосписание {amount} за продление подписки. "
                    "Мы повторим попытку позже, а пока подписку можно продлить вручную.",
                ).format(amount=settings.format_price(amount_kopeks))

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [build_miniapp_or_callback_button(text="⏰ Продлить подписку", callback_data="subscription")],
                    [build_miniapp_or_callback_button(text="📱 Моя подписка", callback_data="subscription")],
                ]
            )
            await bot.send_message(user.telegram_id, text, parse_mode="HTML", reply_markup=keyboard)
        except Exception as error:
            logger.error("1Payment: не удалось уведомить о неудачном списании пользователя %s: %s", user.id, error)
