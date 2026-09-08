"""HTTP-клиент 1Payment (СБП): форма оплаты, GATE, рекуррент по токену, статус.

Документация: https://docs.1payment.com/pages/sbp/recurring

Подпись запроса: md5(<method> + "k=v&k=v" (ключи A→Z, без sign, значения как
переданы) + API_KEY). Подпись колбека — то же, но без префикса метода.
Все значения приводятся к строкам ДО подписи, и ровно эти строки уходят в
тело запроса (form-urlencoded), чтобы подпись всегда совпадала с телом.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Mapping, Optional

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)

PAYMENT_TYPE_SBP = "sbp"

STATUS_INIT = 1
STATUS_PENDING = 2
STATUS_SUCCESS = 3
STATUS_FAILURE = 4
STATUS_REFUND = 5
STATUS_REFUND_PENDING = 9

STATUS_LABELS = {
    STATUS_INIT: "INIT",
    STATUS_PENDING: "PENDING",
    STATUS_SUCCESS: "SUCCESS",
    STATUS_FAILURE: "FAILURE",
    STATUS_REFUND: "REFUND",
    STATUS_REFUND_PENDING: "REFUND_PENDING",
}

# Транзакции ещё нет в 1Payment: init_form только выдаёт ссылку на форму, а сама
# транзакция появляется, когда плательщик её открыл. Для неоплаченного счёта это
# штатное состояние, а не сбой.
ERROR_TRANSACTION_NOT_FOUND = 10

API_ERROR_DESCRIPTIONS = {
    1: "общая ошибка",
    2: "неверная подпись sign",
    3: "метод недоступен",
    4: "партнёр не найден",
    5: "проект не найден",
    6: "способ приёма не подключён к проекту",
    8: "user_data пустой или не уникален",
    10: "транзакция не найдена",
    11: "оплата с данного shop_url недоступна",
}


class OnePaymentAPIError(RuntimeError):
    """Ошибка API 1Payment (HTTP >= 400 или невалидный ответ)."""

    def __init__(self, message: str, *, error_code: Optional[int] = None, http_status: Optional[int] = None):
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status


def stringify_value(value: Any) -> Optional[str]:
    """Значение параметра в том виде, в котором оно участвует в подписи и запросе.

    bool → "true"/"false" (в JSON-колбеке они могут прийти как литералы),
    None → пропускается, остальное — str().
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def canonical_query(params: Mapping[str, Any]) -> str:
    """'k1=v1&k2=v2' по ключам A→Z, без sign и пустых (None) значений."""
    parts = []
    for key in sorted(params.keys()):
        if key == "sign":
            continue
        value = stringify_value(params[key])
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return "&".join(parts)


def build_sign(method: str, params: Mapping[str, Any], api_key: str) -> str:
    base = f"{method}{canonical_query(params)}{api_key}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def build_callback_sign(params: Mapping[str, Any], api_key: str) -> str:
    base = f"{canonical_query(params)}{api_key}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def format_amount(amount_kopeks: int) -> str:
    """Сумма в рублях с двумя знаками: 12345 → '123.45'."""
    kopeks = int(amount_kopeks)
    return f"{kopeks // 100}.{kopeks % 100:02d}"


def parse_status(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_amount_to_kopeks(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        text = str(value).strip().replace(",", ".")
        if not text:
            return None
        rubles = float(text)
        return int(round(rubles * 100))
    except (TypeError, ValueError):
        return None


class OnePaymentService:
    """Тонкая обёртка над REST API 1Payment."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        partner_id: Optional[str] = None,
        project_id: Optional[str] = None,
        api_key: Optional[str] = None,
        shop_url: Optional[str] = None,
        request_timeout: Optional[int] = None,
    ) -> None:
        self.base_url = (base_url or settings.ONEPAYMENT_BASE_URL or "https://api.1payment.com").rstrip("/")
        self.partner_id = partner_id or settings.ONEPAYMENT_PARTNER_ID
        self.project_id = project_id or settings.ONEPAYMENT_PROJECT_ID
        self.api_key = api_key or settings.ONEPAYMENT_API_KEY
        self.shop_url = shop_url or settings.get_onepayment_shop_url()
        self.request_timeout = int(request_timeout or settings.ONEPAYMENT_REQUEST_TIMEOUT or 30)

    @property
    def is_configured(self) -> bool:
        return bool(self.partner_id and self.project_id and self.api_key)

    # ------------------------------------------------------------------ подпись

    def sign(self, method: str, params: Mapping[str, Any]) -> str:
        if not self.api_key:
            raise OnePaymentAPIError("1Payment API key is not configured")
        return build_sign(method, params, self.api_key)

    def verify_callback_sign(self, payload: Mapping[str, Any]) -> bool:
        """Подпись колбека: md5(параметры A→Z без sign + API_KEY), без префикса."""
        if not self.api_key:
            logger.error("1Payment: API key не настроен, подпись колбека не проверена")
            return False
        provided = str(payload.get("sign") or "").strip().lower()
        if not provided:
            return False
        expected = build_callback_sign(payload, self.api_key)
        if hmac.compare_digest(expected, provided):
            return True
        logger.warning(
            "1Payment: подпись колбека не совпала (canonical=%s)",
            canonical_query(payload),
        )
        return False

    # ------------------------------------------------------------------ запросы

    def _base_params(self) -> Dict[str, str]:
        if not self.is_configured:
            raise OnePaymentAPIError("1Payment service is not configured")
        return {
            "partner_id": str(self.partner_id),
            "project_id": str(self.project_id),
            "payment_type": PAYMENT_TYPE_SBP,
        }

    def _prepare(self, method: str, params: Dict[str, Any]) -> Dict[str, str]:
        prepared: Dict[str, str] = {}
        for key, value in params.items():
            text = stringify_value(value)
            if text is None:
                continue
            prepared[key] = text
        prepared["sign"] = self.sign(method, prepared)
        return prepared

    async def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        prepared = self._prepare(method, params)
        url = f"{self.base_url}/{method}"
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=prepared) as response:
                    raw_text = await response.text()
                    data: Any = None
                    if raw_text:
                        try:
                            data = json.loads(raw_text)
                        except json.JSONDecodeError:
                            data = None

                    if response.status >= 400:
                        error_code = None
                        if isinstance(data, dict):
                            error_code = parse_status(data.get("error_code"))
                        description = API_ERROR_DESCRIPTIONS.get(error_code or -1, "")
                        logger.error(
                            "1Payment API error %s %s: error_code=%s %s body=%s",
                            response.status,
                            method,
                            error_code,
                            description,
                            raw_text[:500],
                        )
                        raise OnePaymentAPIError(
                            f"1Payment {method} returned {response.status}: error_code={error_code} {description}".strip(),
                            error_code=error_code,
                            http_status=response.status,
                        )

                    if not isinstance(data, dict):
                        logger.error("1Payment API %s вернул не-JSON ответ: %s", method, raw_text[:500])
                        raise OnePaymentAPIError(
                            f"1Payment {method} returned invalid JSON",
                            http_status=response.status,
                        )

                    # 1Payment отдаёт отказы с HTTP 200 и полем error_code, поэтому
                    # ориентироваться на код ответа нельзя: без этой проверки
                    # отклонённое рекуррентное списание записалось бы как PENDING
                    # и заблокировало повтор на сутки.
                    if "error_code" in data:
                        error_code = parse_status(data.get("error_code"))
                        description = API_ERROR_DESCRIPTIONS.get(error_code or -1, "")
                        logger.error(
                            "1Payment API %s отказал: error_code=%s %s",
                            method,
                            error_code,
                            description,
                        )
                        raise OnePaymentAPIError(
                            f"1Payment {method} error_code={error_code} {description}".strip(),
                            error_code=error_code,
                            http_status=response.status,
                        )

                    return data
        except aiohttp.ClientError as error:
            logger.error("Ошибка соединения с 1Payment (%s): %s", method, error)
            raise OnePaymentAPIError(f"Failed to communicate with 1Payment: {error}") from error

    # ------------------------------------------------------------------- методы

    async def create_payment(
        self,
        *,
        amount_kopeks: int,
        user_data: str,
        description: Optional[str] = None,
        language: Optional[str] = None,
        user_id: Optional[int] = None,
        subscribe: bool = True,
        init_method: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Первичный платёж. Возвращает нормализованный ответ:

        {"url": ссылка на оплату, "order_id": id в 1Payment (может быть None у формы),
         "status": int|None, "method": "init_form"|"init_payment", "raw": ответ}
        """
        method_mode = (init_method or settings.get_onepayment_init_method()).lower()
        api_method = "init_payment" if method_mode == "gate" else "init_form"

        params: Dict[str, Any] = self._base_params()
        params.update(
            {
                "amount": format_amount(amount_kopeks),
                "user_data": user_data,
                "shop_url": self.shop_url or "",
            }
        )
        if subscribe:
            params["subscribe"] = 1
        if description:
            params["description"] = description[:255]
        if user_id is not None:
            params["user_id"] = str(user_id)

        if api_method == "init_form":
            success_url = settings.get_onepayment_success_url()
            failure_url = settings.get_onepayment_failure_url()
            if success_url:
                params["success_url"] = success_url
            if failure_url:
                params["failure_url"] = failure_url
            if language:
                params["lang"] = language[:2].lower()
        else:
            return_url = settings.get_onepayment_success_url()
            if return_url:
                params["return_url"] = return_url

        logger.info(
            "1Payment %s: user_data=%s amount=%s subscribe=%s",
            api_method,
            user_data,
            params["amount"],
            int(subscribe),
        )
        raw = await self._request(api_method, params)

        url = raw.get("url") or raw.get("redirect_url")
        return {
            "url": url,
            "order_id": raw.get("order_id"),
            "status": parse_status(raw.get("status")),
            "method": api_method,
            "raw": raw,
        }

    async def charge_token(
        self,
        *,
        token: str,
        amount_kopeks: int,
        user_data: str,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Рекуррентное списание по сохранённому токену (init_payment + token)."""
        params: Dict[str, Any] = self._base_params()
        params.update(
            {
                "amount": format_amount(amount_kopeks),
                "token": token,
                "user_data": user_data,
                "shop_url": self.shop_url or "",
            }
        )
        if description:
            params["description"] = description[:255]

        logger.info("1Payment recurring charge: user_data=%s amount=%s", user_data, params["amount"])
        raw = await self._request("init_payment", params)
        return {
            "order_id": raw.get("order_id"),
            "status": parse_status(raw.get("status")),
            "status_description": raw.get("status_description"),
            "raw": raw,
        }

    async def get_payment_status(
        self,
        *,
        user_data: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not user_data and not order_id:
            raise OnePaymentAPIError("user_data or order_id is required for status_payment")
        params: Dict[str, Any] = {
            "partner_id": str(self.partner_id),
            "project_id": str(self.project_id),
        }
        if not self.is_configured:
            raise OnePaymentAPIError("1Payment service is not configured")
        if order_id:
            params["order_id"] = order_id
        else:
            params["user_data"] = user_data
        return await self._request("status_payment", params)
