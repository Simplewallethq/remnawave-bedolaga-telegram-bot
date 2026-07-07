import logging

from fastapi import APIRouter, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database.database import AsyncSessionLocal
from app.database.models import AndroidRateRequestClick, SentNotification, User
from app.services.android_rate_request_service import (
    ANDROID_RATE_REQUEST_CLICK_PATH,
    ANDROID_RATE_REQUEST_NOTIFICATION_TYPE,
    build_android_rate_request_review_url,
)


logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(ANDROID_RATE_REQUEST_CLICK_PATH, include_in_schema=False)
async def handle_android_rate_request_click_redirect(
    sent_notification_id: int = Query(..., gt=0),
) -> RedirectResponse:
    redirect_url = build_android_rate_request_review_url(sent_notification_id)

    async with AsyncSessionLocal() as db:
        try:
            notification_result = await db.execute(
                select(SentNotification).where(
                    SentNotification.id == sent_notification_id,
                    SentNotification.notification_type
                    == ANDROID_RATE_REQUEST_NOTIFICATION_TYPE,
                )
            )
            notification = notification_result.scalar_one_or_none()
            if notification is None:
                logger.warning(
                    "Android rate request click references missing notification %s",
                    sent_notification_id,
                )
                return RedirectResponse(
                    url=redirect_url,
                    status_code=status.HTTP_302_FOUND,
                )

            existing_result = await db.execute(
                select(AndroidRateRequestClick.id).where(
                    AndroidRateRequestClick.sent_notification_id == sent_notification_id
                )
            )
            if existing_result.scalar_one_or_none() is None:
                user_result = await db.execute(
                    select(User.telegram_id).where(User.id == notification.user_id)
                )
                telegram_id = user_result.scalar_one_or_none()
                db.add(
                    AndroidRateRequestClick(
                        sent_notification_id=sent_notification_id,
                        user_id=notification.user_id,
                        telegram_id=telegram_id,
                        review_url=redirect_url,
                    )
                )
                await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.info(
                "Android rate request click already recorded for notification %s",
                sent_notification_id,
            )
        except Exception as error:
            await db.rollback()
            logger.error(
                "Failed to record Android rate request redirect click for notification %s: %s",
                sent_notification_id,
                error,
            )

    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)
