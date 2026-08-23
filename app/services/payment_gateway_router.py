"""Взвешенная маршрутизация счетов между универсальными шлюзами.

Пользователь видит одну кнопку «Оплатить N ₽»; конкретный шлюз (Platega /
WATA / YooKassa) выбирается здесь в момент формирования счёта по настраиваемым
весам. Все три шлюза универсальны — карты и СБП показываются на их собственной
странице, поэтому подмена шлюза для пользователя прозрачна.

Модуль намеренно не является миксином PaymentService: это кросс-провайдерная
политика, её вызывают и хендлеры, и FastAPI-роуты, и админка, а тесты должны
уметь подсунуть детерминированный RNG.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User

logger = logging.getLogger(__name__)

GATEWAY_PLATEGA = "platega"
GATEWAY_WATA = "wata"
GATEWAY_YOOKASSA = "yookassa"

ROUTED_GATEWAYS: tuple = (GATEWAY_PLATEGA, GATEWAY_WATA, GATEWAY_YOOKASSA)

# Поверхности — используются и для поэтапного выката, и для сегментации статистики.
SOURCE_BALANCE = "balance_topup"
SOURCE_CART = "subscription_cart"
SOURCE_PARTIAL = "tariff_partial"
SOURCE_SIMPLE = "simple_pay"
SOURCE_CABINET = "cabinet"
SOURCE_MINIAPP = "miniapp"
SOURCE_ADMIN = "admin_test"

# Метод в callback-данных: topup_amount|auto|{kopeks}
AUTO_METHOD = "auto"


@dataclass(frozen=True)
class GatewayLimits:
    gateway: str
    min_kopeks: int
    max_kopeks: int


@dataclass
class RoutedInvoice:
    gateway: str
    requested_gateway: str
    payment_url: str
    local_payment_id: Optional[int]
    external_id: Optional[str]
    check_callback: str
    amount_kopeks: int
    fallback_used: bool = False
    routing_log_id: Optional[int] = None
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


class PaymentGatewayRouter:
    """Единственное место, где принимается решение о шлюзе."""

    # gateway -> (ключ URL в ответе, ключ внешнего id, префикс callback проверки)
    _RESULT_SHAPE = {
        GATEWAY_PLATEGA: ("redirect_url", "transaction_id", "check_platega_"),
        GATEWAY_WATA: ("payment_url", "payment_link_id", "check_wata_"),
        GATEWAY_YOOKASSA: (
            "confirmation_url",
            "yookassa_payment_id",
            "check_yookassa_",
        ),
    }

    # ------------------------------------------------------------------ состояние

    def is_enabled(self, source: str) -> bool:
        return settings.is_payment_router_surface_enabled(source)

    def current_weights(self) -> Dict[str, int]:
        return settings.get_payment_router_weights()

    def gateway_limits(self, gateway: str) -> GatewayLimits:
        if gateway == GATEWAY_PLATEGA:
            return GatewayLimits(
                gateway,
                int(settings.PLATEGA_MIN_AMOUNT_KOPEKS),
                int(settings.PLATEGA_MAX_AMOUNT_KOPEKS),
            )
        if gateway == GATEWAY_WATA:
            return GatewayLimits(
                gateway,
                int(settings.WATA_MIN_AMOUNT_KOPEKS),
                int(settings.WATA_MAX_AMOUNT_KOPEKS),
            )
        if gateway == GATEWAY_YOOKASSA:
            return GatewayLimits(
                gateway,
                int(settings.YOOKASSA_MIN_AMOUNT_KOPEKS),
                int(settings.YOOKASSA_MAX_AMOUNT_KOPEKS),
            )
        raise ValueError(f"Неизвестный шлюз: {gateway}")

    def _gateway_usable(self, gateway: str) -> bool:
        """Шлюз в принципе может выставить счёт (без учёта суммы)."""

        if self.current_weights().get(gateway, 0) <= 0:
            return False

        if gateway == GATEWAY_PLATEGA:
            return settings.is_platega_universal_enabled()
        if gateway == GATEWAY_WATA:
            return settings.is_wata_enabled()
        if gateway == GATEWAY_YOOKASSA:
            # Без разрешимого чека YooKassa вернёт ошибку КАЖДОМУ пользователю,
            # поэтому исключаем шлюз на входе: тихий 100% отказ превращается
            # в честные 0% доли.
            return (
                settings.is_yookassa_enabled()
                and settings.is_yookassa_receipt_satisfiable()
            )
        return False

    def enabled_gateways(self) -> List[str]:
        return [g for g in ROUTED_GATEWAYS if self._gateway_usable(g)]

    def eligible_gateways(
        self,
        amount_kopeks: int,
        *,
        bypass_minimum: bool = False,
    ) -> List[str]:
        amount = int(amount_kopeks)
        result = []
        for gateway in self.enabled_gateways():
            limits = self.gateway_limits(gateway)
            if not bypass_minimum and amount < limits.min_kopeks:
                continue
            if limits.max_kopeks and amount > limits.max_kopeks:
                continue
            result.append(gateway)
        return result

    def combined_min_kopeks(self) -> int:
        """Минимум для метода `auto`.

        Именно МАКСИМУМ из минимумов: счёт должен быть оплатим любым шлюзом,
        который может выпасть. Если взять минимум, пользователь, которому
        выпала WATA, получит отказ.
        """
        gateways = self.enabled_gateways()
        if not gateways:
            return 0
        return max(self.gateway_limits(g).min_kopeks for g in gateways)

    def combined_max_kopeks(self) -> int:
        """Максимум для `auto` — симметрично, минимум из максимумов."""
        gateways = self.enabled_gateways()
        if not gateways:
            return 0
        return min(self.gateway_limits(g).max_kopeks for g in gateways)

    # ------------------------------------------------------------------- выбор

    def pick_order(
        self,
        amount_kopeks: int,
        *,
        bypass_minimum: bool = False,
        rng: Optional[random.Random] = None,
    ) -> List[str]:
        """Взвешенная выборка БЕЗ возвращения.

        Возвращает [выпавший, фолбэк1, фолбэк2]: первый элемент — назначение
        для аналитики, остальные — цепочка фолбэка.
        """
        candidates = self.eligible_gateways(
            amount_kopeks, bypass_minimum=bypass_minimum
        )
        if not candidates:
            return []

        weights = self.current_weights()
        chooser = rng or random
        remaining = list(candidates)
        order: List[str] = []

        while remaining:
            current_weights = [max(0, weights.get(g, 0)) for g in remaining]
            if sum(current_weights) <= 0:
                order.extend(remaining)
                break
            picked = chooser.choices(remaining, weights=current_weights, k=1)[0]
            order.append(picked)
            remaining.remove(picked)

        if not settings.PAYMENT_ROUTER_FALLBACK_ENABLED:
            return order[:1]
        return order

    # ------------------------------------------------------------- нормализация

    def _normalize(self, gateway: str, raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        if raw.get("error"):
            return None

        url_key, external_key, check_prefix = self._RESULT_SHAPE[gateway]
        payment_url = raw.get(url_key) or raw.get("payment_url")
        local_payment_id = raw.get("local_payment_id")

        if not payment_url or not local_payment_id:
            return None

        return {
            "payment_url": payment_url,
            "local_payment_id": local_payment_id,
            "external_id": raw.get(external_key),
            "check_callback": f"{check_prefix}{local_payment_id}",
            "expires_at": raw.get("expires_at"),
        }

    # ---------------------------------------------------------------- адаптеры

    async def _create_platega(
        self, payment_service, db, user, amount_kopeks, description, language, metadata
    ):
        return await payment_service.create_platega_universal_payment(
            db,
            user_id=user.id,
            amount_kopeks=amount_kopeks,
            description=description,
            language=language,
            metadata=metadata,
        )

    async def _create_wata(
        self, payment_service, db, user, amount_kopeks, description, language, metadata
    ):
        return await payment_service.create_wata_payment(
            db,
            user_id=user.id,
            amount_kopeks=amount_kopeks,
            description=description,
            language=language,
            metadata=metadata,
        )

    @staticmethod
    def _flatten_router_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Схлопывает служебный блок роутера в одну строку.

        ЮKassa — единственный из трёх, кто пересылает metadata на свою сторону,
        и она принимает только плоские строковые значения (вложенный объект даёт
        invalid_request по параметру metadata.payment_router). Ограничения:
        не более 16 ключей и 512 символов на значение.
        """
        result = dict(metadata or {})
        router_meta = result.get("payment_router")
        if isinstance(router_meta, dict):
            result["payment_router"] = ";".join(
                f"{key}={value}" for key, value in router_meta.items()
            )[:512]
        return result

    async def _create_yookassa(
        self, payment_service, db, user, amount_kopeks, description, language, metadata
    ):
        metadata = self._flatten_router_metadata(metadata)
        receipt_email = None
        if settings.is_yookassa_receipt_required():
            # user.email почти всегда пуст (заполняется только через кабинет),
            # поэтому основной источник — адрес магазина по умолчанию.
            receipt_email = (getattr(user, "email", None) or "").strip() or None

        return await payment_service.create_yookassa_payment(
            db=db,
            user_id=user.id,
            amount_kopeks=amount_kopeks,
            description=description,
            receipt_email=receipt_email,
            metadata=metadata,
        )

    def _adapter(self, gateway: str):
        return {
            GATEWAY_PLATEGA: self._create_platega,
            GATEWAY_WATA: self._create_wata,
            GATEWAY_YOOKASSA: self._create_yookassa,
        }[gateway]

    # ------------------------------------------------------------ создание счёта

    async def create_invoice(
        self,
        db: AsyncSession,
        *,
        payment_service,
        user: User,
        amount_kopeks: int,
        source: str,
        description: Optional[str] = None,
        language: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        bypass_minimum: bool = False,
        rng: Optional[random.Random] = None,
    ) -> Optional[RoutedInvoice]:
        amount_kopeks = int(amount_kopeks)
        description = description or settings.get_balance_payment_description(
            amount_kopeks
        )
        language = language or getattr(user, "language", None) or settings.DEFAULT_LANGUAGE

        order = self.pick_order(
            amount_kopeks, bypass_minimum=bypass_minimum, rng=rng
        )
        if not order:
            logger.warning(
                "payment_router: нет доступных шлюзов (сумма=%s, source=%s, user=%s)",
                amount_kopeks,
                source,
                user.id,
            )
            return None

        weights = self.current_weights()
        log_id = await self._open_log(
            db,
            user_id=user.id,
            source=source,
            amount_kopeks=amount_kopeks,
            requested_gateway=order[0],
            weights=weights,
        )

        attempts: List[Dict[str, Any]] = []

        for index, gateway in enumerate(order):
            call_metadata = dict(metadata or {})
            call_metadata["payment_router"] = {
                "v": 1,
                "routing_log_id": log_id,
                "source": source,
                "requested_gateway": order[0],
                "gateway": gateway,
                "attempt": index,
            }

            started = time.monotonic()
            error: Optional[str] = None
            raw: Any = None
            try:
                raw = await self._adapter(gateway)(
                    payment_service,
                    db,
                    user,
                    amount_kopeks,
                    description,
                    language,
                    call_metadata,
                )
            except Exception as exc:  # pragma: no cover - сетевые ошибки
                logger.exception(
                    "payment_router: шлюз %s упал с исключением: %s", gateway, exc
                )
                error = repr(exc)

            normalized = self._normalize(gateway, raw)
            attempts.append(
                {
                    "gateway": gateway,
                    "ok": bool(normalized),
                    "error": error,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            )

            if not normalized:
                continue

            fallback_used = index > 0
            if fallback_used:
                logger.warning(
                    "payment_router fallback: %s -> %s (user=%s, amount=%s, source=%s)",
                    order[0],
                    gateway,
                    user.id,
                    amount_kopeks,
                    source,
                )

            await self._close_log_issued(
                db,
                log_id,
                gateway=gateway,
                normalized=normalized,
                fallback_used=fallback_used,
                attempts=attempts,
            )

            logger.info(
                "payment_router issued: gateway=%s requested=%s fallback=%s "
                "user=%s amount=%s source=%s log_id=%s",
                gateway,
                order[0],
                fallback_used,
                user.id,
                amount_kopeks,
                source,
                log_id,
            )

            return RoutedInvoice(
                gateway=gateway,
                requested_gateway=order[0],
                payment_url=normalized["payment_url"],
                local_payment_id=normalized["local_payment_id"],
                external_id=normalized["external_id"],
                check_callback=normalized["check_callback"],
                amount_kopeks=amount_kopeks,
                fallback_used=fallback_used,
                routing_log_id=log_id,
                attempts=attempts,
                raw=raw if isinstance(raw, dict) else {},
            )

        await self._close_log_failed(db, log_id, attempts=attempts)
        logger.error(
            "payment_router: все шлюзы отказали (user=%s, amount=%s, source=%s, "
            "attempts=%s)",
            user.id,
            amount_kopeks,
            source,
            attempts,
        )
        return None

    # ------------------------------------------------------------------- журнал

    async def _open_log(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        source: str,
        amount_kopeks: int,
        requested_gateway: str,
        weights: Dict[str, int],
    ) -> Optional[int]:
        if not settings.PAYMENT_ROUTER_LOG_ENABLED:
            return None
        try:
            from app.database.crud.payment_routing import create_routing_log

            entry = await create_routing_log(
                db,
                user_id=user_id,
                source=source,
                amount_kopeks=amount_kopeks,
                requested_gateway=requested_gateway,
                weights=weights,
            )
            return entry.id
        except Exception as error:  # pragma: no cover - журнал не блокирует оплату
            logger.warning("Не удалось открыть запись журнала роутинга: %s", error)
            return None

    async def _close_log_issued(
        self,
        db: AsyncSession,
        log_id: Optional[int],
        *,
        gateway: str,
        normalized: Dict[str, Any],
        fallback_used: bool,
        attempts: List[Dict[str, Any]],
    ) -> None:
        if log_id is None:
            return
        try:
            from app.database.crud.payment_routing import mark_routing_issued

            await mark_routing_issued(
                db,
                log_id,
                gateway=gateway,
                local_payment_id=normalized["local_payment_id"],
                external_id=normalized["external_id"],
                payment_url=normalized["payment_url"],
                fallback_used=fallback_used,
                attempts=attempts,
                expires_at=normalized.get("expires_at"),
            )
        except Exception as error:  # pragma: no cover
            logger.warning("Не удалось обновить журнал роутинга: %s", error)

    async def _close_log_failed(
        self,
        db: AsyncSession,
        log_id: Optional[int],
        *,
        attempts: List[Dict[str, Any]],
    ) -> None:
        if log_id is None:
            return
        try:
            from app.database.crud.payment_routing import mark_routing_failed

            await mark_routing_failed(db, log_id, attempts=attempts)
        except Exception as error:  # pragma: no cover
            logger.warning("Не удалось отметить провал в журнале роутинга: %s", error)

    async def record_payment(
        self,
        db: AsyncSession,
        *,
        gateway: str,
        local_payment_id: Optional[int],
        transaction_id: Optional[int] = None,
        amount_kopeks: Optional[int] = None,
        paid_at: Optional[datetime] = None,
    ) -> None:
        """Отмечает факт оплаты. Вызывается из finalize каждого шлюза.

        Никогда не бросает исключений: баг в журнале не должен блокировать
        зачисление денег.
        """
        if local_payment_id is None or not settings.PAYMENT_ROUTER_LOG_ENABLED:
            return
        try:
            from app.database.crud.payment_routing import mark_routing_paid

            await mark_routing_paid(
                db,
                gateway=gateway,
                local_payment_id=local_payment_id,
                transaction_id=transaction_id,
                amount_kopeks=amount_kopeks,
                paid_at=paid_at,
            )
        except Exception as error:  # pragma: no cover
            logger.warning(
                "Не удалось отметить оплату в журнале роутинга (%s #%s): %s",
                gateway,
                local_payment_id,
                error,
            )

    # ------------------------------------------------- сообщение со счётом

    async def attach_invoice_message(
        self,
        db: AsyncSession,
        routed: RoutedInvoice,
        *,
        chat_id: int,
        message_id: int,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Записывает координаты сообщения со счётом в metadata провайдера.

        Нужно, чтобы finalize смог удалить сообщение после оплаты. Все три
        модели имеют metadata_json и updated_at, поэтому запись единообразна.
        """
        from sqlalchemy import update as sa_update

        from app.database.models import (
            PlategaPayment,
            WataPayment,
            YooKassaPayment,
        )

        model = {
            GATEWAY_PLATEGA: PlategaPayment,
            GATEWAY_WATA: WataPayment,
            GATEWAY_YOOKASSA: YooKassaPayment,
        }.get(routed.gateway)

        if model is None or routed.local_payment_id is None:
            return

        try:
            from sqlalchemy import select as sa_select

            result = await db.execute(
                sa_select(model.metadata_json).where(
                    model.id == routed.local_payment_id
                )
            )
            payment_metadata = dict(result.scalar_one_or_none() or {})
            payment_metadata["invoice_message"] = {
                "chat_id": chat_id,
                "message_id": message_id,
            }
            if extra_metadata:
                payment_metadata.update(extra_metadata)

            await db.execute(
                sa_update(model)
                .where(model.id == routed.local_payment_id)
                .values(
                    metadata_json=payment_metadata,
                    updated_at=datetime.utcnow(),
                )
            )
            await db.commit()
        except Exception as error:  # pragma: no cover - диагностический лог
            logger.warning(
                "Не удалось сохранить сообщение счёта (%s #%s): %s",
                routed.gateway,
                routed.local_payment_id,
                error,
            )


payment_gateway_router = PaymentGatewayRouter()
