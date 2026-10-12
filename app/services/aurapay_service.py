"""HTTP-клиент AuraPay (https://docs.aurapay.tech): инвойсы и подпись вебхуков."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Iterable, Optional

import aiohttp

from app.config import settings

logger = logging.getLogger(__name__)


class AurapayAPIError(RuntimeError):
    """AuraPay вернула ошибку или недоступна."""


def _php_scalar(value: Any) -> str:
    """Значение так, как его склеивает PHP implode у AuraPay."""
    if value is None:
        return ""
    if value is True:
        return "1"
    if value is False:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _signature_strings(raw_body: bytes) -> Iterable[str]:
    """Строки-кандидаты для подписи.

    AuraPay подписывает значения «в том виде, в котором переданы в JSON».
    Обычно суммы приходят строками ("1250.00"), но если число придёт числом,
    его текст в теле ("1250.00") и PHP-представление (1250) расходятся —
    поэтому проверяем оба варианта.
    """
    literal = json.loads(raw_body, parse_float=lambda text: text, parse_int=lambda text: text)
    parsed = json.loads(raw_body)
    if not isinstance(literal, dict) or not isinstance(parsed, dict):
        return []

    def _php_number(value: Any) -> Any:
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    keys = sorted(literal.keys())
    candidates = [
        "".join(_php_scalar(literal[key]) for key in keys),
        "".join(_php_scalar(_php_number(parsed[key])) for key in keys),
    ]
    return list(dict.fromkeys(candidates))


class AurapayService:
    """Тонкая обёртка над REST API AuraPay."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        shop_id: Optional[str] = None,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        request_timeout: Optional[int] = None,
    ) -> None:
        self.base_url = (base_url or settings.AURAPAY_BASE_URL or "").rstrip("/")
        self.shop_id = shop_id or settings.AURAPAY_SHOP_ID
        self.api_key = api_key or settings.AURAPAY_API_KEY
        self.secret_key = secret_key or settings.AURAPAY_SECRET_KEY
        self.request_timeout = request_timeout or int(settings.AURAPAY_REQUEST_TIMEOUT)

    # ----------------------------------------------------------------- подпись

    @staticmethod
    def build_signature(payload: Dict[str, Any], secret_key: str) -> str:
        concatenated = "".join(_php_scalar(payload[key]) for key in sorted(payload))
        return hmac.new(
            secret_key.encode("utf-8"), concatenated.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def verify_signature(self, raw_body: bytes, signature: Optional[str]) -> bool:
        if not signature or not self.secret_key:
            return False
        try:
            candidates = _signature_strings(raw_body)
        except (ValueError, TypeError):
            return False
        received = signature.strip().lower()
        for concatenated in candidates:
            expected = hmac.new(
                self.secret_key.encode("utf-8"),
                concatenated.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(expected, received):
                return True
        return False

    # ----------------------------------------------------------------- запросы

    async def _request(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not (self.base_url and self.shop_id and self.api_key):
            raise AurapayAPIError("AuraPay не настроена")

        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {
            "X-ApiKey": self.api_key,
            "X-ShopId": self.shop_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    text = await response.text()
                    if response.status >= 400:
                        logger.error("AuraPay %s -> %s: %s", path, response.status, text)
                        raise AurapayAPIError(
                            f"AuraPay {path} вернула {response.status}: {text[:300]}"
                        )
                    try:
                        data = json.loads(text) if text else {}
                    except ValueError as error:
                        raise AurapayAPIError(f"AuraPay {path}: не JSON: {text[:300]}") from error
        except aiohttp.ClientError as error:
            logger.error("AuraPay %s недоступна: %s", path, error)
            raise AurapayAPIError(f"AuraPay {path} недоступна: {error}") from error

        if not isinstance(data, dict):
            raise AurapayAPIError(f"AuraPay {path}: неожиданный ответ {data!r}")
        if data.get("error"):
            raise AurapayAPIError(f"AuraPay {path}: {data.get('error')}")
        return data

    async def create_invoice(
        self,
        *,
        amount_kopeks: int,
        order_id: str,
        comment: Optional[str] = None,
        callback_url: Optional[str] = None,
        lifetime_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "amount": round(int(amount_kopeks) / 100, 2),
            "order_id": order_id,
        }
        if comment:
            payload["comment"] = comment
        if callback_url:
            payload["callback_url"] = callback_url
        if lifetime_minutes:
            payload["lifetime"] = max(1, min(int(lifetime_minutes), 43200))

        logger.info("AuraPay: создаём инвойс order_id=%s amount=%s", order_id, payload["amount"])
        return await self._request("/invoice/create", payload)

    async def get_invoice_status(
        self, *, invoice_id: Optional[str] = None, order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        if invoice_id:
            payload = {"id": invoice_id}
        elif order_id:
            payload = {"order_id": order_id}
        else:
            raise AurapayAPIError("нужен id или order_id инвойса")
        return await self._request("/invoice/status", payload)
