"""Internal Apple In-App Purchase verification endpoint, called only by teleVpn.

Verifies a StoreKit 2 signed transaction (JWS) and activates/extends the caller's
subscription. Bot is the source of truth for subscriptions — see
openspec/changes/add-apple-iap-verify/design.md for the full contract.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Security, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud import apple_iap as apple_iap_crud
from app.database.crud.user import get_user_by_id
from app.services.apple_iap_service import (
    AppleIapConfigurationError,
    AppleIapDuplicateTransactionError,
    AppleIapExpiredTransactionError,
    AppleIapUnknownProductError,
    AppleIapVerificationFailedError,
    activate_apple_iap_transaction,
    apple_iap_verification_service,
)
from app.services.subscription_service import SubscriptionService

from ..dependencies import get_db_session, require_api_token
from ..schemas.iap import AppleIapVerifyRequest, AppleIapVerifyResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/verify",
    response_model=AppleIapVerifyResponse,
    summary="Verify a StoreKit 2 signed transaction and activate the subscription",
    responses={
        404: {"description": "Unknown user"},
        409: {"description": "Transaction bound to another account"},
        422: {"description": "Invalid or expired transaction"},
        500: {"description": "Verification unavailable"},
    },
)
async def verify_apple_iap(
    payload: AppleIapVerifyRequest,
    _=Security(require_api_token),
    db: AsyncSession = Depends(get_db_session),
) -> AppleIapVerifyResponse:
    user = await get_user_by_id(db, payload.user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown user")

    try:
        verified = apple_iap_verification_service.verify_transaction(payload.transaction_jws)
    except AppleIapVerificationFailedError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid transaction")
    except AppleIapConfigurationError:
        logger.exception("Apple IAP verifier unavailable")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="verification unavailable")

    existing = await apple_iap_crud.get_by_transaction_id(db, verified.transaction_id)
    if existing:
        return AppleIapVerifyResponse(status="ok", subscription_end_date=existing.expires_date)

    binding = await apple_iap_crud.get_binding_by_original_transaction_id(
        db, verified.original_transaction_id
    )
    if binding and binding.user_id != user.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="transaction bound to another account"
        )

    try:
        subscription = await activate_apple_iap_transaction(db, user=user, verified=verified)
    except AppleIapUnknownProductError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unknown product")
    except AppleIapExpiredTransactionError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="transaction expired")
    except AppleIapDuplicateTransactionError:
        winner = await apple_iap_crud.get_by_transaction_id(db, verified.transaction_id)
        if winner is None:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, detail="verification unavailable"
            )
        return AppleIapVerifyResponse(status="ok", subscription_end_date=winner.expires_date)

    try:
        service = SubscriptionService()
        if service.is_configured:
            await service.update_remnawave_user(db, subscription)
    except Exception:
        logger.exception("Ошибка синхронизации подписки %s после Apple IAP", subscription.id)

    return AppleIapVerifyResponse(status="ok", subscription_end_date=subscription.end_date)
