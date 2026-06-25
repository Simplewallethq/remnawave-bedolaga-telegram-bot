import logging

from aiogram import Dispatcher, F, types
from aiogram.filters import BaseFilter, StateFilter
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.feedback import (
    get_feedback_by_id,
    get_waiting_feedback_for_user,
    set_feedback_answer,
    set_feedback_selected_option,
)
from app.database.models import Feedback, User
from app.services.expired_subscription_feedback_service import (
    EXPIRED_SUBSCRIPTION_FEEDBACK_CALLBACK_PREFIX,
    EXPIRED_SUBSCRIPTION_FEEDBACK_OPTIONS,
    EXPIRED_SUBSCRIPTION_FEEDBACK_TYPE,
    EXPIRED_SUBSCRIPTION_FOLLOWUP_QUESTIONS,
    FEEDBACK_STATUS_COMPLETED,
    FEEDBACK_STATUS_WAITING_FOR_ANSWER,
    OPTION_NOT_NEEDED,
)
from app.states import ExpiredSubscriptionFeedbackStates


logger = logging.getLogger(__name__)


THANK_YOU_TEXT = "Спасибо 🙏"
BUSY_STATE_TEXT = "Сначала завершите или отмените текущее действие, затем ответьте на опрос."


class WaitingExpiredSubscriptionFeedbackFilter(BaseFilter):
    async def __call__(
        self,
        message: types.Message,
        db_user: User | None = None,
        db: AsyncSession | None = None,
    ):
        if not db_user or not db or not message.text or message.text.startswith("/"):
            return False

        feedback = await get_waiting_feedback_for_user(
            db,
            user_id=db_user.id,
            feedback_type=EXPIRED_SUBSCRIPTION_FEEDBACK_TYPE,
        )
        if not feedback:
            return False

        return {"waiting_feedback": feedback}


async def _remove_feedback_keyboard(callback: types.CallbackQuery) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception as error:
        logger.debug("Не удалось убрать клавиатуру feedback-сообщения: %s", error)


async def _send_plain_callback_message(callback: types.CallbackQuery, text: str) -> None:
    if callback.message:
        await callback.bot.send_message(
            chat_id=callback.message.chat.id,
            text=text,
        )


def _feedback_context(feedback: Feedback) -> dict:
    if isinstance(feedback.context, dict):
        return dict(feedback.context)
    return {}


async def handle_expired_subscription_feedback_callback(
    callback: types.CallbackQuery,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
):
    try:
        _, feedback_id_raw, option = callback.data.split(":", 2)
        feedback_id = int(feedback_id_raw)
    except (AttributeError, ValueError):
        await callback.answer("Некорректные данные", show_alert=True)
        return

    if option not in EXPIRED_SUBSCRIPTION_FEEDBACK_OPTIONS:
        await callback.answer("Некорректный вариант", show_alert=True)
        return

    feedback = await get_feedback_by_id(db, feedback_id)
    if not feedback or feedback.user_id != db_user.id:
        await callback.answer("Опрос не найден", show_alert=True)
        return

    if feedback.type != EXPIRED_SUBSCRIPTION_FEEDBACK_TYPE:
        await callback.answer("Опрос не найден", show_alert=True)
        return

    if feedback.status == FEEDBACK_STATUS_COMPLETED:
        await callback.answer(THANK_YOU_TEXT)
        return

    context = _feedback_context(feedback)

    if feedback.status == FEEDBACK_STATUS_WAITING_FOR_ANSWER:
        followup_question = context.get("followup_question")
        if not followup_question and feedback.selected_option:
            followup_question = EXPIRED_SUBSCRIPTION_FOLLOWUP_QUESTIONS.get(
                feedback.selected_option,
            )
        await state.update_data(expired_subscription_feedback_id=feedback.id)
        await state.set_state(ExpiredSubscriptionFeedbackStates.waiting_for_answer)
        if followup_question:
            await _send_plain_callback_message(callback, followup_question)
        await callback.answer()
        return

    if feedback.selected_option:
        await callback.answer(THANK_YOU_TEXT)
        return

    if option == OPTION_NOT_NEEDED:
        await set_feedback_selected_option(
            db,
            feedback,
            selected_option=option,
            status=FEEDBACK_STATUS_COMPLETED,
            context=context,
        )
        await db.commit()
        await _remove_feedback_keyboard(callback)
        await _send_plain_callback_message(callback, THANK_YOU_TEXT)
        await callback.answer()
        return

    followup_question = EXPIRED_SUBSCRIPTION_FOLLOWUP_QUESTIONS[option]
    context["followup_question"] = followup_question
    await set_feedback_selected_option(
        db,
        feedback,
        selected_option=option,
        status=FEEDBACK_STATUS_WAITING_FOR_ANSWER,
        context=context,
    )
    await db.commit()
    await state.update_data(expired_subscription_feedback_id=feedback.id)
    await state.set_state(ExpiredSubscriptionFeedbackStates.waiting_for_answer)

    await _remove_feedback_keyboard(callback)
    await _send_plain_callback_message(callback, followup_question)
    await callback.answer()


async def handle_expired_subscription_feedback_busy_callback(
    callback: types.CallbackQuery,
):
    await callback.answer(BUSY_STATE_TEXT, show_alert=True)


async def handle_expired_subscription_feedback_answer(
    message: types.Message,
    state: FSMContext,
    db: AsyncSession,
    db_user: User,
):
    answer = (message.text or "").strip()
    if not answer:
        return

    state_data = await state.get_data()
    feedback_id = state_data.get("expired_subscription_feedback_id")
    try:
        feedback_id = int(feedback_id)
    except (TypeError, ValueError):
        feedback_id = None

    feedback = await get_feedback_by_id(db, feedback_id) if feedback_id else None
    if (
        not feedback
        or feedback.user_id != db_user.id
        or feedback.type != EXPIRED_SUBSCRIPTION_FEEDBACK_TYPE
        or feedback.status != FEEDBACK_STATUS_WAITING_FOR_ANSWER
    ):
        await state.clear()
        await message.bot.send_message(
            chat_id=message.chat.id,
            text="Опрос уже завершён или не найден.",
        )
        return

    await set_feedback_answer(
        db,
        feedback,
        answer=answer,
    )
    await db.commit()
    await state.clear()
    await message.bot.send_message(
        chat_id=message.chat.id,
        text=THANK_YOU_TEXT,
    )


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(
        handle_expired_subscription_feedback_callback,
        StateFilter(None),
        F.data.startswith(f"{EXPIRED_SUBSCRIPTION_FEEDBACK_CALLBACK_PREFIX}:"),
    )
    dp.callback_query.register(
        handle_expired_subscription_feedback_busy_callback,
        F.data.startswith(f"{EXPIRED_SUBSCRIPTION_FEEDBACK_CALLBACK_PREFIX}:"),
    )
    dp.message.register(
        handle_expired_subscription_feedback_answer,
        StateFilter(ExpiredSubscriptionFeedbackStates.waiting_for_answer),
        F.text.is_not(None),
        ~F.text.startswith("/"),
    )
