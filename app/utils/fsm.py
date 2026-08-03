from aiogram.fsm.context import FSMContext


async def clear_state_preserving_pending_start_payload(state: FSMContext) -> None:
    data = await state.get_data() or {}
    pending_start_payload = data.get("pending_start_payload")

    await state.clear()

    if pending_start_payload is not None:
        await state.update_data(pending_start_payload=pending_start_payload)
