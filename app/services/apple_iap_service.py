"""Verification and activation of Apple In-App Purchase (StoreKit 2) transactions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import jwt
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.models.JWSTransactionDecodedPayload import (
    JWSTransactionDecodedPayload,
)
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import apple_iap as apple_iap_crud
from app.database.crud.subscription import create_paid_subscription
from app.database.crud.subscription_event import record_subscription_renewal_event
from app.database.crud.transaction import create_transaction
from app.database.models import (
    PaymentMethod,
    Subscription,
    SubscriptionStatus,
    TransactionType,
    User,
)
from app.services.plan_pricing_service import get_plan_by_code

logger = logging.getLogger(__name__)

ROOT_CERTS_DIR = Path(__file__).resolve().parent.parent / "data" / "apple_root_certs"


class AppleIapVerificationFailedError(Exception):
    """Signature/chain/bundle-id/environment verification failed (maps to 422)."""


class AppleIapConfigurationError(Exception):
    """Verifier could not be constructed: missing root certs or app_apple_id (maps to 500)."""


class AppleIapUnknownProductError(Exception):
    """productId is not mapped, or maps to an inactive/unknown plan (maps to 422)."""


class AppleIapExpiredTransactionError(Exception):
    """Verified expiresDate is already in the past (maps to 422)."""


class AppleIapDuplicateTransactionError(Exception):
    """Lost a concurrent race on transaction_id — caller should treat as idempotent 200."""


@dataclass(frozen=True)
class AppleIapVerifiedTransaction:
    transaction_id: str
    original_transaction_id: str
    product_id: str
    expires_date: datetime
    purchase_date: datetime
    environment: str


def _load_root_certificates() -> List[bytes]:
    if not ROOT_CERTS_DIR.is_dir():
        raise AppleIapConfigurationError(f"Root cert directory not found: {ROOT_CERTS_DIR}")
    certs = []
    for cert_path in sorted(ROOT_CERTS_DIR.glob("*.cer")):
        certs.append(cert_path.read_bytes())
    if not certs:
        raise AppleIapConfigurationError(f"No root certificates found in {ROOT_CERTS_DIR}")
    return certs


def _epoch_millis_to_datetime(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.utcfromtimestamp(value / 1000)


class AppleIapVerificationService:
    """Wraps Apple's SignedDataVerifier for Production and Sandbox environments."""

    def __init__(self) -> None:
        self._root_certificates: Optional[List[bytes]] = None
        self._production_verifier: Optional[SignedDataVerifier] = None
        self._sandbox_verifier: Optional[SignedDataVerifier] = None

    def _get_root_certificates(self) -> List[bytes]:
        if self._root_certificates is None:
            self._root_certificates = _load_root_certificates()
        return self._root_certificates

    def _get_production_verifier(self) -> SignedDataVerifier:
        if self._production_verifier is None:
            if not settings.APPLE_IAP_APP_APPLE_ID:
                raise AppleIapConfigurationError(
                    "APPLE_IAP_APP_APPLE_ID is required for the Production verifier"
                )
            self._production_verifier = SignedDataVerifier(
                root_certificates=self._get_root_certificates(),
                enable_online_checks=False,
                environment=Environment.PRODUCTION,
                bundle_id=settings.APPLE_IAP_BUNDLE_ID,
                app_apple_id=settings.APPLE_IAP_APP_APPLE_ID,
            )
        return self._production_verifier

    def _get_sandbox_verifier(self) -> SignedDataVerifier:
        if self._sandbox_verifier is None:
            self._sandbox_verifier = SignedDataVerifier(
                root_certificates=self._get_root_certificates(),
                enable_online_checks=False,
                environment=Environment.SANDBOX,
                bundle_id=settings.APPLE_IAP_BUNDLE_ID,
            )
        return self._sandbox_verifier

    def verify_transaction(self, transaction_jws: str) -> AppleIapVerifiedTransaction:
        try:
            unverified = jwt.decode(
                transaction_jws, options={"verify_signature": False}
            )
        except Exception as exc:
            raise AppleIapVerificationFailedError("malformed JWS") from exc

        raw_environment = unverified.get("environment")
        if raw_environment == Environment.PRODUCTION.value:
            verifier = self._get_production_verifier()
        elif raw_environment == Environment.SANDBOX.value:
            verifier = self._get_sandbox_verifier()
        else:
            raise AppleIapVerificationFailedError(f"unsupported environment: {raw_environment}")

        try:
            decoded: JWSTransactionDecodedPayload = verifier.verify_and_decode_signed_transaction(
                transaction_jws
            )
        except VerificationException as exc:
            raise AppleIapVerificationFailedError(str(exc)) from exc
        except Exception as exc:
            raise AppleIapVerificationFailedError("verification failed") from exc

        if not decoded.transactionId or not decoded.originalTransactionId or not decoded.productId:
            raise AppleIapVerificationFailedError("incomplete transaction payload")

        expires_date = _epoch_millis_to_datetime(decoded.expiresDate)
        purchase_date = _epoch_millis_to_datetime(decoded.purchaseDate)
        if expires_date is None or purchase_date is None:
            raise AppleIapVerificationFailedError("missing expiresDate/purchaseDate")

        return AppleIapVerifiedTransaction(
            transaction_id=decoded.transactionId,
            original_transaction_id=decoded.originalTransactionId,
            product_id=decoded.productId,
            expires_date=expires_date,
            purchase_date=purchase_date,
            environment=decoded.environment.value,
        )


apple_iap_verification_service = AppleIapVerificationService()


async def activate_apple_iap_transaction(
    db: AsyncSession,
    *,
    user: User,
    verified: AppleIapVerifiedTransaction,
) -> Subscription:
    mapping = settings.get_apple_iap_products().get(verified.product_id)
    if mapping is None:
        raise AppleIapUnknownProductError(f"unknown product_id: {verified.product_id}")

    plan = await get_plan_by_code(db, mapping["plan_code"])
    if not plan or not plan.is_active:
        raise AppleIapUnknownProductError(f"plan not active: {mapping['plan_code']}")

    if verified.expires_date <= datetime.utcnow():
        raise AppleIapExpiredTransactionError("transaction already expired")

    period_days = mapping["period_days"]

    subscription = user.subscription
    old_end_date: Optional[datetime] = None
    if subscription is None:
        subscription = await create_paid_subscription(
            db,
            user_id=user.id,
            duration_days=period_days,
            traffic_limit_gb=plan.traffic_limit_gb,
            device_limit=plan.device_limit,
        )
    else:
        old_end_date = subscription.end_date

    subscription.plan_id = plan.id
    subscription.plan_period_days = period_days
    subscription.device_limit = plan.device_limit
    subscription.traffic_limit_gb = plan.traffic_limit_gb
    subscription.is_trial = False
    subscription.status = SubscriptionStatus.ACTIVE.value
    subscription.end_date = verified.expires_date

    try:
        apple_row = await apple_iap_crud.create_record(
            db,
            user_id=user.id,
            transaction_id=verified.transaction_id,
            original_transaction_id=verified.original_transaction_id,
            product_id=verified.product_id,
            environment=verified.environment,
            expires_date=verified.expires_date,
            purchase_date=verified.purchase_date,
        )
    except IntegrityError:
        await db.rollback()
        raise AppleIapDuplicateTransactionError(
            f"transaction_id already processed: {verified.transaction_id}"
        )

    transaction = await create_transaction(
        db=db,
        user_id=user.id,
        type=TransactionType.SUBSCRIPTION_PAYMENT,
        amount_kopeks=0,
        description=f"Apple IAP {verified.product_id}",
        payment_method=PaymentMethod.APPLE_IAP,
        external_id=verified.transaction_id,
    )

    apple_row.transaction_record_id = transaction.id
    await db.commit()

    await record_subscription_renewal_event(
        db,
        user_id=user.id,
        subscription_id=subscription.id,
        transaction_id=transaction.id,
        amount_kopeks=0,
        period_days=period_days,
        previous_end_date=old_end_date,
        new_end_date=verified.expires_date,
        payment_method=PaymentMethod.APPLE_IAP.value,
        source="apple_iap",
    )

    return subscription
