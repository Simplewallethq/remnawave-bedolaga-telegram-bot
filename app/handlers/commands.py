from aiogram import Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.handlers import menu, subscription
from app.utils.fsm import clear_state_preserving_pending_start_payload


class _CommandScreenCallback:
    """Provides the callback interface required by existing screen handlers."""

    def __init__(self, command_message: types.Message, screen_message: types.Message):
        self.message = screen_message
        self.from_user = command_message.from_user
        self.bot = command_message.bot

    async def answer(self, text: str | None = None, **_: object) -> None:
        # Callback handlers acknowledge successful button presses with no text.
        # Commands have no callback to acknowledge, but alert text still needs a UI.
        if text:
            await self.message.edit_text(text)


async def _create_screen_callback(message: types.Message) -> _CommandScreenCallback:
    screen_message = await message.answer("Loading...")
    return _CommandScreenCallback(message, screen_message)


async def command_connect(
    message: types.Message,
    state: FSMContext,
    db: AsyncSession,
) -> None:
    await clear_state_preserving_pending_start_payload(state)
    await menu.handle_howto(await _create_screen_callback(message), state, db)


async def command_subscription(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
) -> None:
    await clear_state_preserving_pending_start_payload(state)
    await subscription.purchase.handle_subscription_menu(
        await _create_screen_callback(message), db_user, db
    )


async def command_referrals(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
) -> None:
    await clear_state_preserving_pending_start_payload(state)
    await menu.handle_referral(await _create_screen_callback(message), db_user, db)


async def command_profile(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
) -> None:
    await clear_state_preserving_pending_start_payload(state)
    await menu.handle_profile(await _create_screen_callback(message), db_user, db)


async def command_support(
    message: types.Message,
    state: FSMContext,
    db_user: User,
    db: AsyncSession,
) -> None:
    await clear_state_preserving_pending_start_payload(state)
    await menu.handle_support(await _create_screen_callback(message), db_user, db)


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(command_connect, Command("connect"))
    dp.message.register(command_subscription, Command("subscription"))
    dp.message.register(command_referrals, Command("referrals"))
    dp.message.register(command_profile, Command("profile"))
    dp.message.register(command_support, Command("support"))
