import logging

from aiogram import Dispatcher, F, types
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AndroidRateRequestClick, User
from app.services.android_rate_request_service import (
    ANDROID_RATE_REQUEST_CLICK_CALLBACK_PREFIX,
    build_android_rate_request_review_url,
)


logger = logging.getLogger(__name__)


async def _redirect_to_google_play(callback: types.CallbackQuery, redirect_url: str) -> None:
    try:
        await callback.answer(url=redirect_url)
        return
    except Exception as error:
        logger.error("Failed to redirect Android rate request click: %s", error)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="Открыть Google Play", url=redirect_url)]
        ]
    )
    text = "Нажмите кнопку ниже, чтобы открыть Google Play."

    try:
        if callback.message:
            await callback.message.answer(text, reply_markup=keyboard)
        elif callback.from_user:
            await callback.bot.send_message(
                chat_id=callback.from_user.id,
                text=text,
                reply_markup=keyboard,
            )
    except Exception as error:
        logger.error(
            "Failed to send Android rate request fallback redirect button: %s",
            error,
        )


def _parse_sent_notification_id(callback_data: str | None) -> int | None:
    if not callback_data:
        return None

    prefix = f"{ANDROID_RATE_REQUEST_CLICK_CALLBACK_PREFIX}:"
    if not callback_data.startswith(prefix):
        return None

    try:
        value = int(callback_data[len(prefix):])
    except ValueError:
        return None

    return value if value > 0 else None


async def handle_android_rate_request_click(
    callback: types.CallbackQuery,
    db: AsyncSession,
    db_user: User | None = None,
) -> None:
    sent_notification_id = _parse_sent_notification_id(callback.data)
    if sent_notification_id is None:
        await callback.answer("Некорректная ссылка", show_alert=True)
        return

    redirect_url = build_android_rate_request_review_url(sent_notification_id)

    try:
        existing_click = await db.execute(
            select(AndroidRateRequestClick.id).where(
                AndroidRateRequestClick.sent_notification_id == sent_notification_id
            )
        )
        if existing_click.scalar_one_or_none() is not None:
            await _redirect_to_google_play(callback, redirect_url)
            return

        db.add(
            AndroidRateRequestClick(
                sent_notification_id=sent_notification_id,
                user_id=db_user.id if db_user else None,
                telegram_id=callback.from_user.id if callback.from_user else None,
                message_id=getattr(callback.message, "message_id", None),
                callback_query_id=callback.id,
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
            "Failed to record Android rate request click for notification %s: %s",
            sent_notification_id,
            error,
        )

    await _redirect_to_google_play(callback, redirect_url)


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(
        handle_android_rate_request_click,
        F.data.startswith(f"{ANDROID_RATE_REQUEST_CLICK_CALLBACK_PREFIX}:"),
    )
