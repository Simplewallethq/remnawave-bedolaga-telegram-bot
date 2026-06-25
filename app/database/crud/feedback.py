from typing import Any, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Feedback


async def create_feedback(
    db: AsyncSession,
    *,
    feedback_type: str,
    event_key: str,
    user_id: Optional[int] = None,
    subscription_id: Optional[int] = None,
    status: str = "sent",
    context: Optional[dict[str, Any]] = None,
) -> Feedback:
    feedback = Feedback(
        type=feedback_type,
        user_id=user_id,
        subscription_id=subscription_id,
        event_key=event_key,
        status=status,
        context=context,
    )
    db.add(feedback)
    await db.flush()
    await db.refresh(feedback)
    return feedback


async def get_feedback_by_id(db: AsyncSession, feedback_id: int) -> Optional[Feedback]:
    return await db.get(Feedback, feedback_id)


async def get_feedback_by_event_key(
    db: AsyncSession,
    event_key: str,
) -> Optional[Feedback]:
    result = await db.execute(
        select(Feedback).where(Feedback.event_key == event_key)
    )
    return result.scalar_one_or_none()


async def get_waiting_feedback_for_user(
    db: AsyncSession,
    *,
    user_id: int,
    feedback_type: str,
) -> Optional[Feedback]:
    result = await db.execute(
        select(Feedback)
        .where(
            and_(
                Feedback.user_id == user_id,
                Feedback.type == feedback_type,
                Feedback.status == "waiting_for_answer",
            )
        )
        .order_by(Feedback.updated_at.desc(), Feedback.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def set_feedback_message_id(
    db: AsyncSession,
    feedback: Feedback,
    message_id: int,
) -> Feedback:
    feedback.message_id = message_id
    await db.flush()
    return feedback


async def set_feedback_selected_option(
    db: AsyncSession,
    feedback: Feedback,
    *,
    selected_option: str,
    status: str,
    context: Optional[dict[str, Any]] = None,
) -> Feedback:
    feedback.selected_option = selected_option
    feedback.status = status
    if context is not None:
        feedback.context = context
    await db.flush()
    return feedback


async def set_feedback_answer(
    db: AsyncSession,
    feedback: Feedback,
    *,
    answer: str,
) -> Feedback:
    feedback.answer = answer
    feedback.status = "completed"
    await db.flush()
    return feedback


async def set_feedback_status(
    db: AsyncSession,
    feedback: Feedback,
    status: str,
) -> Feedback:
    feedback.status = status
    await db.flush()
    return feedback
