from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.crud import server_squad
from app.handlers.subscription import purchase


pytestmark = pytest.mark.asyncio


async def test_trial_offer_uses_trial_image(monkeypatch):
    callback = SimpleNamespace(
        message=SimpleNamespace(),
        answer=AsyncMock(),
    )
    user = SimpleNamespace(
        language="ru",
        subscription=None,
        has_had_paid_subscription=False,
    )
    render = AsyncMock()

    monkeypatch.setattr(
        server_squad,
        "get_trial_eligible_server_squads",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(purchase, "edit_or_answer_photo", render)
    monkeypatch.setattr(purchase.os.path, "exists", lambda _path: True)

    await purchase.show_trial_offer(callback, user, AsyncMock())

    assert render.await_args.kwargs["photo_path"] == purchase.os.path.join(
        "images",
        "trial.webp",
    )
    assert render.await_args.kwargs["caption"]
    callback.answer.assert_awaited_once()
