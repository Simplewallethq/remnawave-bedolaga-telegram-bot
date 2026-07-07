"""Магазин Наград — обмен бонусных лучей ☀️ на призы.

Экраны на rich-разметке Bot API 10.1 (см. app/utils/rich_message.py):
каталог, карточка приза (покупка/нехватка), успех, «Мои заявки» с отменой.
Плюс админ-кнопки Выполнено/Отклонить на уведомлении о новой заявке.
"""

import html
import logging

from aiogram import Dispatcher, F, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ray_prize_claim import get_claim_by_id, get_user_claims
from app.database.crud.user import get_user_by_id
from app.database.models import RayPrizeClaimStatus, User
from app.localization.texts import get_texts
from app.services.rays_shop_service import (
    PRIZE_KIND_SUBSCRIPTION,
    RAY_PRIZES,
    cancel_prize_claim,
    complete_prize_claim,
    create_physical_prize_claim,
    get_prize_by_number,
    redeem_subscription_prize,
)
from app.utils.rich_message import RichScreen, show_rich_screen
from app.utils.timezone import format_local_datetime

logger = logging.getLogger(__name__)

_CLAIM_STATUS_VIEW = {
    RayPrizeClaimStatus.PENDING.value: ("🟡", "RAYS_CLAIM_STATUS_PENDING", "В обработке"),
    RayPrizeClaimStatus.COMPLETED.value: ("🟢", "RAYS_CLAIM_STATUS_COMPLETED", "Выполнено"),
    RayPrizeClaimStatus.CANCELLED.value: ("⚪", "RAYS_CLAIM_STATUS_CANCELLED", "Отменено"),
}


def _btn(text: str, callback_data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text=text, callback_data=callback_data)


def _rays_tier_lines(texts) -> list[str]:
    """Строки «срок — N ☀️» из настроек тиров начисления."""
    labels = {
        90: texts.t("RAYS_TIER_LABEL_3M", "3 мес"),
        180: texts.t("RAYS_TIER_LABEL_6M", "6 мес"),
        360: texts.t("RAYS_TIER_LABEL_1Y", "год"),
        720: texts.t("RAYS_TIER_LABEL_2Y", "2 года"),
    }
    tiers = sorted([
        (settings.RAYS_TIER_1_MIN_DAYS, settings.RAYS_TIER_1_AMOUNT),
        (settings.RAYS_TIER_2_MIN_DAYS, settings.RAYS_TIER_2_AMOUNT),
        (settings.RAYS_TIER_3_MIN_DAYS, settings.RAYS_TIER_3_AMOUNT),
    ])
    lines = []
    for min_days, amount in tiers:
        label = labels.get(min_days, f"{min_days} дн.")
        lines.append(f"{label} — {amount} ☀️")
    return lines


def _shop_screen_base(texts) -> RichScreen:
    """Общее начало экранов магазина: баннер-мозаика призов (если настроен URL)."""
    return RichScreen().banner(settings.RAYS_SHOP_BANNER_URL)


def _prize_line(texts, prize) -> str:
    return texts.t("RAYS_PRIZE_LINE", "Приз: {title} — {cost} ☀️").format(
        title=prize.title, cost=prize.cost,
    )


async def show_rays_shop(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Каталог Магазина Наград."""
    if not settings.is_rays_shop_enabled():
        texts = get_texts(db_user.language)
        await callback.answer(
            texts.t("RAYS_SHOP_DISABLED", "Магазин Наград временно недоступен"),
            show_alert=True,
        )
        return

    texts = get_texts(db_user.language)
    balance = db_user.rays_balance or 0

    screen = _shop_screen_base(texts)
    screen.details(
        texts.t("RAYS_SHOP_WHAT_SUMMARY", "Что такое лучи? ▾"),
        [
            texts.t("RAYS_SHOP_WHAT_INTRO", "Начисляем лучи за друзей с длинной подпиской:"),
            *_rays_tier_lines(texts),
            texts.t("RAYS_SHOP_WHAT_SUBS", "Подписка на Leto VPN в призах — выгоднее цены в рублях"),
            texts.t("RAYS_SHOP_WHAT_TECH", "Технику получаешь по заявке, поддержка уточнит доставку"),
        ],
    )
    screen.heading(
        texts.t("RAYS_SHOP_BALANCE_HEADING", "Баланс лучей: {balance} ☀️").format(balance=balance)
    )
    for prize in RAY_PRIZES:
        screen.paragraph(
            texts.t("RAYS_SHOP_PRIZE_ROW", "Приз №{number} · {title} — {cost} ☀️").format(
                number=prize.number, title=prize.title, cost=prize.cost,
            )
        )
    screen.divider()
    screen.paragraph(
        texts.t("RAYS_SHOP_FOOTER", "Чтобы забрать приз, нажмите соответствующую кнопку ниже 👇")
    )

    prize_btn_tpl = texts.t("RAYS_SHOP_PRIZE_BUTTON", "Приз №{number}")
    rows = []
    for i in range(0, len(RAY_PRIZES), 2):
        rows.append([
            _btn(prize_btn_tpl.format(number=p.number), f"rays_prize_{p.number}")
            for p in RAY_PRIZES[i:i + 2]
        ])
    rows.append([
        _btn(texts.t("RAYS_MY_CLAIMS_BUTTON", "📦 Мои заявки"), "rays_claims"),
        _btn(texts.BACK, "referral"),
    ])

    await show_rich_screen(callback, screen, types.InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


async def show_rays_prize(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Карточка приза: подтверждение покупки или экран нехватки лучей."""
    texts = get_texts(db_user.language)
    prize = get_prize_by_number(int(callback.data.rsplit("_", 1)[-1]))
    if prize is None or not settings.is_rays_shop_enabled():
        await callback.answer(
            texts.t("RAYS_PRIZE_UNAVAILABLE", "Приз временно недоступен"), show_alert=True,
        )
        return

    await _show_prize_card(callback, db_user, prize, texts)


async def _show_prize_card(callback: types.CallbackQuery, db_user: User, prize, texts):
    balance = db_user.rays_balance or 0
    screen = _shop_screen_base(texts)

    if balance < prize.cost:
        screen.heading(texts.t("RAYS_NOT_ENOUGH_HEADING", "Пока не хватает лучей ☀️"))
        screen.paragraph(_prize_line(texts, prize))
        screen.paragraph(
            texts.t(
                "RAYS_NOT_ENOUGH_BALANCE",
                "Ваш баланс: {balance} ☀️. <b>Не хватает: {missing} ☀️.</b>",
            ).format(balance=balance, missing=prize.cost - balance)
        )
        screen.paragraph(
            texts.t(
                "RAYS_NOT_ENOUGH_CTA",
                "Приглашайте друзей с подпиской от 3 месяцев — и лучи накопятся 🙂",
            )
        )
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [_btn(texts.t("RAYS_INVITE_FRIENDS_BUTTON", "🤝 Пригласить друзей"), "referral")],
            [_btn(texts.BACK, "rays_shop")],
        ])
        await show_rich_screen(callback, screen, keyboard)
        await callback.answer()
        return

    remainder = balance - prize.cost
    balance_line = texts.t(
        "RAYS_PRIZE_BALANCE_LINE",
        "Ваш баланс: {balance} ☀️. <b>Остаток после покупки: {remainder} ☀️.</b>",
    ).format(balance=balance, remainder=remainder)

    if prize.kind == PRIZE_KIND_SUBSCRIPTION:
        screen.heading(
            texts.t("RAYS_PRIZE_SUB_HEADING", "Получить {plan} на {period}").format(
                plan=prize.plan_title, period=prize.period_label,
            )
        )
        screen.paragraph(_prize_line(texts, prize))
        screen.paragraph(balance_line)
        screen.paragraph(
            texts.t("RAYS_PRIZE_SUB_NOTE", "⚡ Подписка активируется сразу после подтверждения.")
        )
        confirm_text = texts.t("RAYS_PRIZE_SUB_CONFIRM_BUTTON", "⚡ Активировать подписку")
    else:
        screen.heading(
            texts.t("RAYS_PRIZE_TECH_HEADING", "Заказать {title}").format(title=prize.title)
        )
        screen.paragraph(_prize_line(texts, prize))
        screen.paragraph(balance_line)
        screen.paragraph(
            texts.t(
                "RAYS_PRIZE_TECH_NOTE",
                "📦 Лучи спишутся сейчас. Поддержка свяжется, чтобы уточнить доставку. "
                "Статус — в «Мои заявки».",
            )
        )
        confirm_text = texts.t("RAYS_PRIZE_TECH_CONFIRM_BUTTON", "📦 Оформить заявку")

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [_btn(confirm_text, f"rays_prize_confirm_{prize.number}")],
        [_btn(texts.BACK, "rays_shop")],
    ])
    await show_rich_screen(callback, screen, keyboard)
    await callback.answer()


async def confirm_rays_prize(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Подтверждение: списывает лучи, активирует подписку или создаёт заявку."""
    texts = get_texts(db_user.language)
    prize = get_prize_by_number(int(callback.data.rsplit("_", 1)[-1]))
    if prize is None or not settings.is_rays_shop_enabled():
        await callback.answer(
            texts.t("RAYS_PRIZE_UNAVAILABLE", "Приз временно недоступен"), show_alert=True,
        )
        return

    if (db_user.rays_balance or 0) < prize.cost:
        # Баланс изменился между экранами — показываем экран нехватки.
        await _show_prize_card(callback, db_user, prize, texts)
        return

    if prize.kind == PRIZE_KIND_SUBSCRIPTION:
        subscription = await redeem_subscription_prize(db, db_user, prize)
        if subscription is None:
            await callback.answer(
                texts.t("RAYS_PRIZE_UNAVAILABLE", "Приз временно недоступен"), show_alert=True,
            )
            return

        screen = _shop_screen_base(texts)
        screen.heading(texts.t("RAYS_SUB_SUCCESS_HEADING", "Готово! ⚡"))
        screen.paragraph(
            texts.t("RAYS_SUB_SUCCESS_TITLE", "<b>{plan} на {period} активирован.</b>").format(
                plan=prize.plan_title, period=prize.period_label,
            )
        )
        screen.paragraph(
            texts.t("RAYS_SUB_SUCCESS_NOTE", "Подписка уже работает на ваших устройствах.")
        )
        screen.paragraph(
            texts.t(
                "RAYS_SUB_SUCCESS_BALANCE",
                "Списано: {cost} ☀️. <b>Остаток: {remainder} ☀️.</b>",
            ).format(cost=prize.cost, remainder=db_user.rays_balance or 0)
        )
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [_btn(texts.t("RAYS_TO_SHOP_BUTTON", "🎁 В магазин"), "rays_shop")],
            [_btn(texts.t("RAYS_TO_MAIN_MENU_BUTTON", "🏠 В главное меню"), "main_menu")],
        ])
        await show_rich_screen(callback, screen, keyboard)
        await callback.answer()
        return

    claim = await create_physical_prize_claim(db, db_user, prize, bot=callback.bot)
    if claim is None:
        await callback.answer(
            texts.t("RAYS_CLAIM_FAILED", "Не удалось оформить заявку, попробуйте позже"),
            show_alert=True,
        )
        return

    screen = _shop_screen_base(texts)
    screen.heading(texts.t("RAYS_CLAIM_SUCCESS_HEADING", "Заявка создана ✅"))
    screen.paragraph(
        texts.t("RAYS_CLAIM_SUCCESS_TITLE", "<b>{title} — заявка №{claim_id}.</b>").format(
            title=prize.title, claim_id=claim.id,
        )
    )
    screen.paragraph(
        texts.t(
            "RAYS_CLAIM_SUCCESS_NOTE",
            "Поддержка свяжется в течение 1–2 дней, чтобы уточнить доставку.",
        )
    )
    screen.paragraph(
        texts.t(
            "RAYS_CLAIM_SUCCESS_BALANCE",
            "Зарезервировано: {cost} ☀️. <b>Остаток: {remainder} ☀️.</b>",
        ).format(cost=prize.cost, remainder=db_user.rays_balance or 0)
    )
    screen.paragraph(
        texts.t(
            "RAYS_CLAIM_SUCCESS_REFUND_NOTE",
            "Если не получится оформить — лучи вернём. Статус — в «Мои заявки».",
        )
    )
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [_btn(texts.t("RAYS_MY_CLAIMS_BUTTON", "📦 Мои заявки"), "rays_claims")],
        [_btn(texts.t("RAYS_TO_SHOP_BUTTON", "🎁 В магазин"), "rays_shop")],
    ])
    await show_rich_screen(callback, screen, keyboard)
    await callback.answer()


async def show_rays_claims(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Экран «Мои заявки»."""
    texts = get_texts(db_user.language)
    claims = await get_user_claims(db, db_user.id)

    screen = _shop_screen_base(texts)
    screen.heading(texts.t("RAYS_CLAIMS_HEADING", "Мои заявки"))

    rows: list[list[types.InlineKeyboardButton]] = []

    if not claims:
        screen.paragraph(
            texts.t(
                "RAYS_CLAIMS_EMPTY",
                "У вас пока нет заявок. Накопите лучи и закажите приз в магазине 🎁",
            )
        )
    else:
        for index, claim in enumerate(claims):
            emoji, status_key, status_default = _CLAIM_STATUS_VIEW.get(
                claim.status, ("⚪", "RAYS_CLAIM_STATUS_CANCELLED", "Отменено"),
            )
            if index:
                screen.divider()
            screen.paragraph(
                f"<b>{emoji} №{claim.id} · {html.escape(claim.prize_title)} — {claim.cost_rays} ☀️</b>"
            )
            date_str = format_local_datetime(claim.created_at, "%d.%m")
            screen.paragraph(f"{texts.t(status_key, status_default)} · {date_str}")

            if claim.is_pending:
                rows.append([
                    _btn(
                        texts.t("RAYS_CLAIM_CANCEL_BUTTON", "✖️ Отменить №{claim_id}").format(
                            claim_id=claim.id,
                        ),
                        f"rays_claim_cancel_{claim.id}",
                    )
                ])

    rows.append([
        _btn(texts.t("RAYS_TO_SHOP_BUTTON", "🎁 В магазин"), "rays_shop"),
        _btn(texts.BACK, "referral"),
    ])
    await show_rich_screen(callback, screen, types.InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


async def ask_cancel_rays_claim(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Подтверждение отмены заявки."""
    texts = get_texts(db_user.language)
    claim = await get_claim_by_id(db, int(callback.data.rsplit("_", 1)[-1]))
    if claim is None or claim.user_id != db_user.id or not claim.is_pending:
        await callback.answer(
            texts.t("RAYS_CLAIM_NOT_CANCELLABLE", "Эту заявку уже нельзя отменить"),
            show_alert=True,
        )
        return

    screen = _shop_screen_base(texts)
    screen.heading(
        texts.t("RAYS_CLAIM_CANCEL_HEADING", "Отменить заявку на {title}?").format(
            title=html.escape(claim.prize_title),
        )
    )
    screen.paragraph(
        texts.t(
            "RAYS_CLAIM_CANCEL_NOTE", "Лучи ({cost} ☀️) вернутся на баланс.",
        ).format(cost=claim.cost_rays)
    )
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[
        _btn(texts.t("YES_BUTTON", "✅ Да"), f"rays_claim_cancel_yes_{claim.id}"),
        _btn(texts.t("NO_BUTTON", "✖️ Нет"), "rays_claims"),
    ]])
    await show_rich_screen(callback, screen, keyboard)
    await callback.answer()


async def cancel_rays_claim(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    """Отмена заявки пользователем: возврат лучей и возврат к списку заявок."""
    texts = get_texts(db_user.language)
    claim = await get_claim_by_id(db, int(callback.data.rsplit("_", 1)[-1]))
    if claim is None or claim.user_id != db_user.id:
        await callback.answer(
            texts.t("RAYS_CLAIM_NOT_CANCELLABLE", "Эту заявку уже нельзя отменить"),
            show_alert=True,
        )
        return

    cancelled = await cancel_prize_claim(db, claim, db_user)
    if not cancelled:
        await callback.answer(
            texts.t("RAYS_CLAIM_NOT_CANCELLABLE", "Эту заявку уже нельзя отменить"),
            show_alert=True,
        )
        return

    await callback.answer(
        texts.t("RAYS_CLAIM_CANCELLED_TOAST", "Заявка отменена, лучи возвращены ☀️")
    )
    await show_rays_claims(callback, db_user, db)


# --- Админ-кнопки на уведомлении о заявке ---

async def admin_complete_rays_claim(callback: types.CallbackQuery):
    if not settings.is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет прав", show_alert=True)
        return

    from app.database.database import AsyncSessionLocal

    claim_id = int(callback.data.rsplit("_", 1)[-1])
    async with AsyncSessionLocal() as db:
        claim = await get_claim_by_id(db, claim_id)
        if claim is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        if not await complete_prize_claim(db, claim):
            await callback.answer("Заявка уже обработана", show_alert=True)
            return
        claim_user = await get_user_by_id(db, claim.user_id)

    try:
        await callback.message.edit_text(
            callback.message.html_text + f"\n\n✅ <b>Выполнено</b> (админ @{callback.from_user.username or callback.from_user.id})",
            parse_mode="HTML",
        )
    except Exception:
        pass

    if claim_user and claim_user.telegram_id:
        try:
            user_texts = get_texts(claim_user.language)
            await callback.bot.send_message(
                claim_user.telegram_id,
                user_texts.t(
                    "RAYS_CLAIM_COMPLETED_USER_NOTIFY",
                    "🟢 Заявка №{claim_id} ({title}) выполнена. Спасибо, что копите лучи! ☀️",
                ).format(claim_id=claim.id, title=claim.prize_title),
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя о выполнении заявки №{claim.id}: {e}")

    await callback.answer("Заявка отмечена выполненной")


async def admin_reject_rays_claim(callback: types.CallbackQuery):
    if not settings.is_admin(callback.from_user.id):
        await callback.answer("⛔ Нет прав", show_alert=True)
        return

    from app.database.database import AsyncSessionLocal

    claim_id = int(callback.data.rsplit("_", 1)[-1])
    async with AsyncSessionLocal() as db:
        claim = await get_claim_by_id(db, claim_id)
        if claim is None:
            await callback.answer("Заявка не найдена", show_alert=True)
            return
        claim_user = await get_user_by_id(db, claim.user_id)
        if claim_user is None:
            await callback.answer("Пользователь заявки не найден", show_alert=True)
            return
        if not await cancel_prize_claim(db, claim, claim_user, cancelled_by_admin=True):
            await callback.answer("Заявка уже обработана", show_alert=True)
            return

    try:
        await callback.message.edit_text(
            callback.message.html_text + f"\n\n❌ <b>Отклонено, лучи возвращены</b> (админ @{callback.from_user.username or callback.from_user.id})",
            parse_mode="HTML",
        )
    except Exception:
        pass

    if claim_user.telegram_id:
        try:
            user_texts = get_texts(claim_user.language)
            await callback.bot.send_message(
                claim_user.telegram_id,
                user_texts.t(
                    "RAYS_CLAIM_REJECTED_USER_NOTIFY",
                    "⚪ Заявка №{claim_id} ({title}) отменена. Лучи ({cost} ☀️) вернулись на баланс.",
                ).format(claim_id=claim.id, title=claim.prize_title, cost=claim.cost_rays),
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя об отмене заявки №{claim.id}: {e}")

    await callback.answer("Заявка отклонена, лучи возвращены")


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_rays_shop, F.data == "rays_shop")
    dp.callback_query.register(confirm_rays_prize, F.data.startswith("rays_prize_confirm_"))
    dp.callback_query.register(show_rays_prize, F.data.startswith("rays_prize_"))
    dp.callback_query.register(show_rays_claims, F.data == "rays_claims")
    dp.callback_query.register(cancel_rays_claim, F.data.startswith("rays_claim_cancel_yes_"))
    dp.callback_query.register(ask_cancel_rays_claim, F.data.startswith("rays_claim_cancel_"))
    dp.callback_query.register(admin_complete_rays_claim, F.data.startswith("admin_rays_claim_done_"))
    dp.callback_query.register(admin_reject_rays_claim, F.data.startswith("admin_rays_claim_reject_"))
