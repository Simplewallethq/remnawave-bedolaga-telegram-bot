import uuid
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any, List

from yookassa import Configuration, Payment as YooKassaPayment
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
from yookassa.domain.common.confirmation_type import ConfirmationType

from app.config import settings

logger = logging.getLogger(__name__)


# SDK ЮKassa синхронный и по умолчанию БЕЗ таймаута сокета. На дефолтном
# executor'е зависший эндпоинт выел бы общий пул потоков и подвесил бота
# целиком, поэтому: (1) навязываем таймаут запросам SDK, (2) уводим вызовы
# в отдельный ограниченный пул, (3) сверху страхуемся asyncio-таймаутом.

_YOOKASSA_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(getattr(settings, "YOOKASSA_MAX_CONCURRENT_REQUESTS", 4))),
    thread_name_prefix="yookassa-sdk",
)

_YOOKASSA_CALL_TIMEOUT = 30


def _patch_yookassa_timeout() -> None:
    """Навязывает таймаут HTTP-запросам SDK.

    Патчим get_session, а не execute: execute не принимает timeout, а таймаут
    нужен именно на уровне requests.Session — иначе зависший эндпоинт держит
    поток бесконечно.
    """
    try:
        from yookassa.client import ApiClient
    except Exception as error:  # pragma: no cover - зависит от версии SDK
        logger.warning("Не удалось пропатчить таймаут YooKassa SDK: %s", error)
        return

    if getattr(ApiClient, "_bedolaga_timeout_patched", False):
        return

    if not hasattr(ApiClient, "get_session"):  # pragma: no cover
        logger.warning("YooKassa SDK: get_session отсутствует, таймаут не навязан")
        return

    connect_timeout = float(getattr(settings, "YOOKASSA_HTTP_CONNECT_TIMEOUT", 5))
    read_timeout = float(getattr(settings, "YOOKASSA_HTTP_READ_TIMEOUT", 20))
    original_get_session = ApiClient.get_session

    def get_session_with_timeout(self, *args, **kwargs):
        session = original_get_session(self, *args, **kwargs)
        if session is not None and not getattr(session, "_bedolaga_timeout", False):
            original_request = session.request

            def request_with_timeout(method, url, **request_kwargs):
                request_kwargs.setdefault("timeout", (connect_timeout, read_timeout))
                return original_request(method, url, **request_kwargs)

            session.request = request_with_timeout
            session._bedolaga_timeout = True
        return session

    ApiClient.get_session = get_session_with_timeout
    ApiClient._bedolaga_timeout_patched = True
    logger.info(
        "YooKassa SDK: таймауты запросов %s/%s сек", connect_timeout, read_timeout
    )


_patch_yookassa_timeout()


async def _run_yookassa_call(func):
    """Выполняет синхронный вызов SDK в отдельном пуле под общим таймаутом."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_YOOKASSA_EXECUTOR, func),
        timeout=_YOOKASSA_CALL_TIMEOUT,
    )



class YooKassaService:

    def __init__(self,
                 shop_id: Optional[str] = None,
                 secret_key: Optional[str] = None,
                 configured_return_url: Optional[str] = None,
                 bot_username_for_default_return: Optional[str] = None):

        shop_id = shop_id or getattr(settings, 'YOOKASSA_SHOP_ID', None)
        secret_key = secret_key or getattr(settings, 'YOOKASSA_SECRET_KEY', None)
        configured_return_url = configured_return_url or getattr(settings, 'YOOKASSA_RETURN_URL', None)

        self.configured = False

        if not shop_id or not secret_key:
            logger.warning(
                "YooKassa SHOP_ID или SECRET_KEY не настроены в settings. "
                "Функционал платежей будет ОТКЛЮЧЕН.")
        else:
            try:
                Configuration.configure(shop_id, secret_key)
                self.configured = True
                logger.info(
                    f"YooKassa SDK сконфигурирован для shop_id: {shop_id[:5]}...")
            except Exception as error:
                logger.error(
                    "Ошибка конфигурации YooKassa SDK: %s",
                    error,
                    exc_info=True,
                )
                self.configured = False

        if not self.configured:
            self.return_url = "https://t.me/"
            logger.warning(
                "YooKassa не активна, используем заглушку return_url: %s",
                self.return_url,
            )
        elif configured_return_url:
            self.return_url = configured_return_url
        elif bot_username_for_default_return:
            self.return_url = f"https://t.me/{bot_username_for_default_return}"
            logger.info(
                f"YOOKASSA_RETURN_URL не установлен, используем бота: {self.return_url}")
        else:
            self.return_url = "https://t.me/"
            logger.warning(
                f"КРИТИЧНО: YOOKASSA_RETURN_URL не установлен И username бота не предоставлен. "
                f"Используем заглушку: {self.return_url}. Платежи могут работать некорректно.")

        logger.info(f"YooKassa Service return_url: {self.return_url}")

    async def create_payment(
            self,
            amount: float,
            currency: str,
            description: str,
            metadata: Dict[str, Any],
            receipt_email: Optional[str] = None,
            receipt_phone: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Создает платеж в YooKassa"""

        if not self.configured:
            logger.error("YooKassa не сконфигурирован. Невозможно создать платеж.")
            return None

        # Чек 54-ФЗ обязателен, только если фискализация магазина идёт через
        # ЮKassa. Если нет — receipt не отправляем вовсе, и контакт не нужен.
        # ЮKassa сама email у покупателя не спрашивает и доставляет чек только
        # на почту, поэтому «пусть спросит на своей странице» не работает.
        send_receipt = settings.is_yookassa_receipt_required()

        customer_contact_for_receipt = {}
        if send_receipt:
            if receipt_email:
                customer_contact_for_receipt["email"] = receipt_email
            elif receipt_phone:
                customer_contact_for_receipt["phone"] = receipt_phone
            elif getattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', None):
                customer_contact_for_receipt["email"] = settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL
            else:
                logger.error(
                    "КРИТИЧНО: Не предоставлен email/телефон для чека YooKassa и YOOKASSA_DEFAULT_RECEIPT_EMAIL не установлен.")
                return {
                    "error": True,
                    "internal_message": "Отсутствуют контактные данные для чека YooKassa и не настроен email по умолчанию."
                }

        try:
            builder = PaymentRequestBuilder()
            builder.set_amount({
                "value": str(round(amount, 2)),
                "currency": currency.upper()
            })
            builder.set_capture(True)
            builder.set_confirmation({
                "type": ConfirmationType.REDIRECT,
                "return_url": self.return_url
            })
            builder.set_description(description)
            builder.set_metadata(metadata)

            receipt_items_list: List[Dict[str, Any]] = [{
                "description": description[:128],
                "quantity": "1.00",
                "amount": {
                    "value": str(round(amount, 2)),
                    "currency": currency.upper()
                },
                "vat_code": int(getattr(settings, 'YOOKASSA_VAT_CODE', 1)),
                "payment_mode": getattr(settings, 'YOOKASSA_PAYMENT_MODE', 'full_payment'),
                "payment_subject": getattr(settings, 'YOOKASSA_PAYMENT_SUBJECT', 'service')
            }]

            receipt_data_dict: Dict[str, Any] = {
                "customer": customer_contact_for_receipt,
                "items": receipt_items_list
            }

            if send_receipt:
                builder.set_receipt(receipt_data_dict)

            idempotence_key = str(uuid.uuid4())
            payment_request = builder.build()

            logger.info(
                f"Создание платежа YooKassa (Idempotence-Key: {idempotence_key}). "
                f"Сумма: {amount} {currency}. Метаданные: {metadata}. Чек: {receipt_data_dict}")

            response = await _run_yookassa_call(lambda: YooKassaPayment.create(payment_request, idempotence_key))

            logger.info(
                f"Ответ YooKassa Payment.create: ID={response.id}, Status={response.status}, Paid={response.paid}")

            return {
                "id": response.id,
                "confirmation_url": response.confirmation.confirmation_url if response.confirmation else None,
                "status": response.status,
                "metadata": response.metadata,
                "amount_value": float(response.amount.value),
                "amount_currency": response.amount.currency,
                "idempotence_key_used": idempotence_key,
                "paid": response.paid,
                "refundable": response.refundable,
                "created_at": response.created_at.isoformat() if hasattr(
                    response.created_at, 'isoformat') else str(response.created_at),
                "description_from_yk": response.description,
                "test_mode": response.test if hasattr(response, 'test') else None
            }
        except Exception as e:
            logger.error(f"Ошибка создания платежа YooKassa: {e}", exc_info=True)
            return None

    async def create_sbp_payment(
            self,
            amount: float,
            currency: str,
            description: str,
            metadata: Dict[str, Any],
            receipt_email: Optional[str] = None,
            receipt_phone: Optional[str] = None) -> Optional[Dict[str, Any]]:

        if not self.configured:
            logger.error("YooKassa не сконфигурирован. Невозможно создать платеж через СБП.")
            return None

        # Чек 54-ФЗ обязателен, только если фискализация магазина идёт через
        # ЮKassa. Если нет — receipt не отправляем вовсе, и контакт не нужен.
        # ЮKassa сама email у покупателя не спрашивает и доставляет чек только
        # на почту, поэтому «пусть спросит на своей странице» не работает.
        send_receipt = settings.is_yookassa_receipt_required()

        customer_contact_for_receipt = {}
        if send_receipt:
            if receipt_email:
                customer_contact_for_receipt["email"] = receipt_email
            elif receipt_phone:
                customer_contact_for_receipt["phone"] = receipt_phone
            elif getattr(settings, 'YOOKASSA_DEFAULT_RECEIPT_EMAIL', None):
                customer_contact_for_receipt["email"] = settings.YOOKASSA_DEFAULT_RECEIPT_EMAIL
            else:
                logger.error(
                    "КРИТИЧНО: Не предоставлен email/телефон для чека YooKassa и YOOKASSA_DEFAULT_RECEIPT_EMAIL не установлен.")
                return {
                    "error": True,
                    "internal_message": "Отсутствуют контактные данные для чека YooKassa и не настроен email по умолчанию."
                }

        try:
            # Создаем один платеж с подтверждением через QR
            # Это позволит получить QR-код для пользователя
            builder = PaymentRequestBuilder()

            builder.set_amount({
                "value": str(round(amount, 2)),
                "currency": currency.upper()
            })

            builder.set_capture(True)

            # Устанавливаем подтверждение через redirect для получения вебхуков
            builder.set_confirmation({
                "type": "redirect",
                "return_url": self.return_url
            })

            builder.set_description(description)

            builder.set_metadata(metadata)

            builder.set_payment_method_data({
                "type": "sbp"
            })

            receipt_items_list: List[Dict[str, Any]] = [{
                "description": description[:128],
                "quantity": "1.00",
                "amount": {
                    "value": str(round(amount, 2)),
                    "currency": currency.upper()
                },
                "vat_code": int(getattr(settings, 'YOOKASSA_VAT_CODE', 1)),
                "payment_mode": getattr(settings, 'YOOKASSA_PAYMENT_MODE', 'full_payment'),
                "payment_subject": getattr(settings, 'YOOKASSA_PAYMENT_SUBJECT', 'service')
            }]

            receipt_data_dict: Dict[str, Any] = {
                "customer": customer_contact_for_receipt,
                "items": receipt_items_list
            }

            if send_receipt:
                builder.set_receipt(receipt_data_dict)

            idempotence_key = str(uuid.uuid4())

            payment_request = builder.build()

            logger.info(
                f"Создание платежа YooKassa СБП с подтверждением 'qr' (Idempotence-Key: {idempotence_key}). "
                f"Сумма: {amount} {currency}. Метаданные: {metadata}. Чек: {receipt_data_dict}")

            response = await _run_yookassa_call(lambda: YooKassaPayment.create(payment_request, idempotence_key))

            logger.info(
                f"Ответ YooKassa Payment.create (СБП, qr): ID={response.id}, Status={response.status}, Paid={response.paid}")

            # Возвращаем данные платежа с QR-подтверждением
            # Пользователь может использовать QR-код или оплатить через приложение банка по ID платежа
            return {
                "id": response.id,
                "qr_confirmation_data": response.confirmation.confirmation_data if response.confirmation and hasattr(response.confirmation, 'confirmation_data') else None,
                "confirmation_url": response.confirmation.confirmation_url if response.confirmation and hasattr(response.confirmation, 'confirmation_url') else None,
                "status": response.status,
                "metadata": response.metadata,
                "amount_value": float(response.amount.value),
                "amount_currency": response.amount.currency,
                "idempotence_key_used": idempotence_key,
                "paid": response.paid,
                "refundable": response.refundable,
                "created_at": response.created_at.isoformat() if hasattr(
                    response.created_at, 'isoformat') else str(response.created_at),
                "description_from_yk": response.description,
                "test_mode": response.test if hasattr(response, 'test') else None
            }
        except Exception as e:
            logger.error(f"Ошибка создания платежа YooKassa СБП: {e}", exc_info=True)
            return None

    async def _create_sbp_payment_with_confirmation_type(
            self,
            amount: float,
            currency: str,
            description: str,
            metadata: Dict[str, Any],
            customer_contact_for_receipt: Dict[str, str],
            confirmation_type: str) -> Optional[Dict[str, Any]]:
        """Создает SBP платеж с указанным типом подтверждения"""
        try:
            builder = PaymentRequestBuilder()

            builder.set_amount({
                "value": str(round(amount, 2)),
                "currency": currency.upper()
            })

            builder.set_capture(True)

            if confirmation_type == "qr":
                builder.set_confirmation({
                    "type": "qr"
                })
            else:  # redirect
                builder.set_confirmation({
                    "type": "redirect",
                    "return_url": self.return_url
                })

            builder.set_description(description)

            builder.set_metadata(metadata)

            builder.set_payment_method_data({
                "type": "sbp"
            })

            receipt_items_list: List[Dict[str, Any]] = [{
                "description": description[:128],
                "quantity": "1.00",
                "amount": {
                    "value": str(round(amount, 2)),
                    "currency": currency.upper()
                },
                "vat_code": int(getattr(settings, 'YOOKASSA_VAT_CODE', 1)),
                "payment_mode": getattr(settings, 'YOOKASSA_PAYMENT_MODE', 'full_payment'),
                "payment_subject": getattr(settings, 'YOOKASSA_PAYMENT_SUBJECT', 'service')
            }]

            receipt_data_dict: Dict[str, Any] = {
                "customer": customer_contact_for_receipt,
                "items": receipt_items_list
            }

            if settings.is_yookassa_receipt_required():
                builder.set_receipt(receipt_data_dict)

            idempotence_key = str(uuid.uuid4())

            payment_request = builder.build()

            logger.info(
                f"Создание платежа YooKassa СБП с подтверждением '{confirmation_type}' (Idempotence-Key: {idempotence_key}). "
                f"Сумма: {amount} {currency}. Метаданные: {metadata}. Чек: {receipt_data_dict}")

            response = await _run_yookassa_call(lambda: YooKassaPayment.create(payment_request, idempotence_key))

            logger.info(
                f"Ответ YooKassa Payment.create (СБП, {confirmation_type}): ID={response.id}, Status={response.status}, Paid={response.paid}")

            result = {
                "id": response.id,
                "status": response.status,
                "metadata": response.metadata,
                "amount_value": float(response.amount.value),
                "amount_currency": response.amount.currency,
                "idempotence_key_used": idempotence_key,
                "paid": response.paid,
                "refundable": response.refundable,
                "created_at": response.created_at.isoformat() if hasattr(
                    response.created_at, 'isoformat') else str(response.created_at),
                "description_from_yk": response.description,
                "test_mode": response.test if hasattr(response, 'test') else None
            }

            # Добавляем данные подтверждения в зависимости от типа
            if confirmation_type == "qr":
                if response.confirmation and hasattr(response.confirmation, 'confirmation_data'):
                    result["confirmation_data"] = response.confirmation.confirmation_data
            else:  # redirect
                if response.confirmation and hasattr(response.confirmation, 'confirmation_url'):
                    result["confirmation_url"] = response.confirmation.confirmation_url

            return result
        except Exception as e:
            logger.error(f"Ошибка создания платежа YooKassa СБП с подтверждением '{confirmation_type}': {e}", exc_info=True)
            return None

    async def get_payment_info(
            self, payment_id_in_yookassa: str) -> Optional[Dict[str, Any]]:

        if not self.configured:
            logger.error("YooKassa не сконфигурирован. Невозможно получить информацию о платеже.")
            return None

        try:
            logger.info(f"Получение информации о платеже YooKassa ID: {payment_id_in_yookassa}")

            payment_info_yk = await _run_yookassa_call(lambda: YooKassaPayment.find_one(payment_id_in_yookassa))

            if payment_info_yk:
                logger.info(
                    f"Информация о платеже YooKassa {payment_id_in_yookassa}: "
                    f"Status={payment_info_yk.status}, Paid={payment_info_yk.paid}")
                return {
                    "id": payment_info_yk.id,
                    "status": payment_info_yk.status,
                    "paid": payment_info_yk.paid,
                    "amount_value": float(payment_info_yk.amount.value),
                    "amount_currency": payment_info_yk.amount.currency,
                    "metadata": payment_info_yk.metadata,
                    "description": payment_info_yk.description,
                    "refundable": payment_info_yk.refundable,
                    "created_at": payment_info_yk.created_at.isoformat() if hasattr(
                        payment_info_yk.created_at, 'isoformat') else str(payment_info_yk.created_at),
                    "captured_at": payment_info_yk.captured_at.isoformat()
                    if payment_info_yk.captured_at and hasattr(
                        payment_info_yk.captured_at, 'isoformat') else None,
                    "payment_method_type": payment_info_yk.payment_method.type
                    if payment_info_yk.payment_method else None,
                    "test_mode": payment_info_yk.test if hasattr(payment_info_yk, 'test') else None
                }
            else:
                logger.warning(f"Платеж не найден в YooKassa ID: {payment_id_in_yookassa}")
                return None
        except Exception as e:
            logger.error(f"Ошибка получения информации о платеже YooKassa {payment_id_in_yookassa}: {e}",
                         exc_info=True)
            return None
