"""Экраны «🔒 Выделенный IP» (Trello #141). Логика — app/services/dedicated_ip_service.py."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import DedicatedIpSlot, User
from app.localization.texts import get_texts
from app.services import dedicated_ip_service as dip
from app.utils.photo_message import edit_or_answer_photo

logger = logging.getLogger(__name__)

MENU_CALLBACK = "dedicated_ip"
SUBSCRIPTION_IMAGE = os.path.join("images", "subscription.webp")


def _image() -> Optional[str]:
    return SUBSCRIPTION_IMAGE if os.path.exists(SUBSCRIPTION_IMAGE) else None


def _btn(text: str, callback_data: str) -> types.InlineKeyboardButton:
    return types.InlineKeyboardButton(text=text, callback_data=callback_data)


def _date(value: Optional[datetime]) -> str:
    return dip._fmt_date(value)


# ---------------------------------------------------------------- для меню подписки

async def subscription_menu_line(db: AsyncSession, user: User) -> Optional[str]:
    """Строка «Выделенный IP: …» в «Управление подпиской» (None — фича не для юзера)."""
    if not dip.is_feature_enabled_for(user):
        return None
    texts = get_texts(user.language)
    slot = await dip.get_user_slot(db, user.id)
    subscription = getattr(user, "subscription", None)
    country = dip.country_label()
    if slot is not None and slot.status == DedicatedIpSlot.STATUS_ACTIVE:
        if slot.autorenew:
            return texts.t("DEDICATED_IP_MENU_LINE_ACTIVE", "Выделенный IP: {country} до {date}").format(
                country=country, date=_date(slot.paid_until)
            )
        return texts.t(
            "DEDICATED_IP_MENU_LINE_NO_RENEW", "Выделенный IP: {country} до {date}, не продлевается"
        ).format(country=country, date=_date(slot.paid_until))
    if slot is not None and slot.status == DedicatedIpSlot.STATUS_HOLD:
        return texts.t("DEDICATED_IP_MENU_LINE_HOLD", "Выделенный IP: не продлён, держим до {date}").format(
            date=_date(slot.hold_until)
        )
    if not await dip.is_on_sale(db, user):
        return None
    if not dip.subscription_is_eligible(subscription):
        return texts.t("DEDICATED_IP_MENU_LINE_TEASER", "Выделенный IP: доступен с платной подпиской")
    return texts.t("DEDICATED_IP_MENU_LINE_NONE", "Выделенный IP: не подключён")


async def subscription_menu_button(db: AsyncSession, user: User) -> Optional[types.InlineKeyboardButton]:
    """Кнопка между «Продлить подписку» и «Баланс» (None — не показывать)."""
    if not dip.is_feature_enabled_for(user):
        return None
    texts = get_texts(user.language)
    slot = await dip.get_user_slot(db, user.id)
    if slot is not None:
        return _btn(texts.t("DEDICATED_IP_MY_BUTTON", "🔒 Мой IP"), MENU_CALLBACK)
    if not await dip.is_on_sale(db, user):
        return None
    return _btn(texts.t("DEDICATED_IP_BUTTON", "🔒 Выделенный IP"), MENU_CALLBACK)


# ---------------------------------------------------------------- экраны

async def _render(target, text: str, rows: list, *, is_message: bool = False) -> None:
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=rows)
    if is_message:
        image = _image()
        if image:
            await target.answer_photo(types.FSInputFile(image), caption=text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await target.answer(text, reply_markup=keyboard, parse_mode="HTML")
        return
    await edit_or_answer_photo(target, text, keyboard, photo_path=_image())


async def show_dedicated_ip(
    target,
    db_user: User,
    db: AsyncSession,
    *,
    is_message: bool = False,
) -> None:
    """Экран по состоянию: «Мой IP», оффер, тизер (триал/нет подписки) или «нет свободных»."""
    texts = get_texts(db_user.language)
    country = dip.country_label()
    back_rows = [[_btn(texts.t("BACK_BUTTON", "⬅️ Назад"), "subscription")]]

    if not dip.is_feature_enabled_for(db_user):
        await _render(target, texts.t("DEDICATED_IP_UNAVAILABLE", "🔒 Выделенный IP сейчас недоступен."), back_rows, is_message=is_message)
        return

    slot = await dip.get_user_slot(db, db_user.id)
    subscription = getattr(db_user, "subscription", None)

    if slot is not None:
        await _show_my_ip(target, db_user, db, slot, is_message=is_message)
        return

    if await dip.count_free_slots(db) <= 0:
        await _render(
            target,
            texts.t(
                "DEDICATED_IP_SOLD_OUT",
                "🔒 <b>Выделенный IP</b>\n\n"
                "Свободные адреса временно закончились — мы уже добавляем новые. Загляни позже.",
            ),
            back_rows,
            is_message=is_message,
        )
        return

    if not dip.subscription_is_eligible(subscription):
        text = texts.t(
            "DEDICATED_IP_TEASER",
            "🔒 <b>Выделенный IP</b>\n\n"
            "Личный IP-адрес ({country}) — только для тебя.\n"
            "Подключается к платной подписке: выбери тариф,\n"
            "и IP можно будет добавить прямо при оплате.",
        ).format(country=country)
        rows = [
            [_btn(texts.t("DEDICATED_IP_CHOOSE_TARIFF_BUTTON", "Выбрать тариф"), "subscription_tariffs")],
            [_btn(texts.t("DEDICATED_IP_INFO_BUTTON", "Как работает и для чего нужен"), "dip_info")],
            *back_rows,
        ]
        await _render(target, text, rows, is_message=is_message)
        return

    price = dip.price_for_remaining_kopeks(subscription.end_date)
    text = texts.t(
        "DEDICATED_IP_OFFER",
        "🔒 <b>Выделенный IP</b>\n\n"
        "Личный IP-адрес ({country}) — только для тебя.\n"
        "Меньше капч и блокировок из-за чужих нарушений.\n\n"
        "До конца подписки (до {date}): {price}\n"
        "Дальше: +{monthly}/мес к подписке, отключить можно в любой момент.",
    ).format(
        country=country,
        date=_date(subscription.end_date),
        price=dip.format_rub(price),
        monthly=dip.format_rub(dip.price_per_month_kopeks()),
    )
    rows = [
        [_btn(texts.t("DEDICATED_IP_CONNECT_BUTTON", "Подключить выделенный IP"), "dip_buy")],
        [_btn(texts.t("DEDICATED_IP_INFO_BUTTON", "Как работает и для чего нужен"), "dip_info")],
        *back_rows,
    ]
    await _render(target, text, rows, is_message=is_message)


async def _show_my_ip(target, db_user: User, db: AsyncSession, slot: DedicatedIpSlot, *, is_message: bool = False) -> None:
    texts = get_texts(db_user.language)
    country = dip.country_label()
    subscription = getattr(db_user, "subscription", None)
    back = [_btn(texts.t("BACK_BUTTON", "⬅️ Назад"), "subscription")]

    if slot.status == DedicatedIpSlot.STATUS_HOLD:
        text = texts.t(
            "DEDICATED_IP_HOLD_SCREEN",
            "🔒 <b>Мой выделенный IP</b>\n\n"
            "Адрес: <code>{ip}</code> ({country})\n"
            "IP не продлён и отключён. Держим адрес за тобой до {date}.",
        ).format(ip=slot.ip, country=country, date=_date(slot.hold_until))
        rows = []
        if dip.subscription_is_eligible(subscription):
            price = dip.price_for_remaining_kopeks(subscription.end_date)
            rows.append([_btn(
                texts.t("DEDICATED_IP_RETURN_BUTTON", "Вернуть IP до {date} — {price}").format(
                    date=_date(subscription.end_date), price=dip.format_rub(price)
                ),
                "dip_buy",
            )])
        else:
            rows.append([_btn(texts.t("SUB_ADD_DAYS_SHORT_BUTTON", "➕ Продлить подписку"), "sub_add_days")])
        rows.append(back)
        await _render(target, text, rows, is_message=is_message)
        return

    if slot.autorenew:
        renew_line = texts.t(
            "DEDICATED_IP_RENEW_ON", "Продление: вместе с подпиской, +{monthly}/мес"
        ).format(monthly=dip.format_rub(dip.price_per_month_kopeks()))
        toggle = _btn(texts.t("DEDICATED_IP_DISABLE_BUTTON", "Отключить выделенный IP"), "dip_off")
    else:
        renew_line = texts.t(
            "DEDICATED_IP_RENEW_OFF", "Продление: отключено, IP работает до {date}"
        ).format(date=_date(slot.paid_until))
        toggle = _btn(texts.t("DEDICATED_IP_ENABLE_BUTTON", "Включить продление"), "dip_on")

    text = texts.t(
        "DEDICATED_IP_MY_SCREEN",
        "🔒 <b>Мой выделенный IP</b>\n\n"
        "Адрес: <code>{ip}</code> ({country})\n"
        "Оплачен до: {date}\n"
        "{renew_line}\n\n"
        "В приложении выбери сервер «🔒 Мой IP».",
    ).format(ip=slot.ip, country=country, date=_date(slot.paid_until), renew_line=renew_line)
    rows = [
        [_btn(texts.t("DEDICATED_IP_HOWTO_BUTTON", "Как подключиться"), "howto")],
        [toggle],
        back,
    ]
    await _render(target, text, rows, is_message=is_message)


async def handle_dedicated_ip_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await show_dedicated_ip(callback, db_user, db)
    await callback.answer()


async def handle_dedicated_ip_info(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    text = texts.t(
        "DEDICATED_IP_INFO",
        "Обычно один IP-адрес VPN делят сотни людей. Если кто-то\n"
        "из них нарушает правила, сервисы начинают показывать капчи\n"
        "или блокируют адрес целиком — и тебя заодно.\n\n"
        "Выделенный IP — адрес только для тебя. Пригодится, если:\n"
        "• Google, ChatGPT, Instagram постоянно просят капчу\n"
        "• сервисы блокируют вход «с подозрительного адреса»\n"
        "• нужен постоянный IP для работы или белых списков\n\n"
        "После оплаты в приложении появится сервер «🔒 Мой IP».",
    )
    subscription = getattr(db_user, "subscription", None)
    rows = []
    if dip.is_feature_enabled_for(db_user) and await dip.count_free_slots(db) > 0:
        if dip.subscription_is_eligible(subscription):
            rows.append([_btn(texts.t("DEDICATED_IP_CONNECT_BUTTON", "Подключить выделенный IP"), "dip_buy")])
        else:
            rows.append([_btn(texts.t("DEDICATED_IP_CHOOSE_TARIFF_BUTTON", "Выбрать тариф"), "subscription_tariffs")])
    rows.append([_btn(texts.t("BACK_BUTTON", "⬅️ Назад"), MENU_CALLBACK)])
    await _render(callback, text, rows)
    await callback.answer()


# ---------------------------------------------------------------- покупка к действующей подписке

async def _resolve_purchase(db: AsyncSession, db_user: User):
    """(subscription, price) для покупки к действующей подписке или (None, текст ошибки)."""
    texts = get_texts(db_user.language)
    if not dip.is_feature_enabled_for(db_user):
        return None, texts.t("DEDICATED_IP_UNAVAILABLE", "🔒 Выделенный IP сейчас недоступен.")
    subscription = getattr(db_user, "subscription", None)
    if not dip.subscription_is_eligible(subscription):
        return None, texts.t("DEDICATED_IP_NEEDS_SUBSCRIPTION", "Выделенный IP подключается к платной подписке.")
    slot = await dip.get_user_slot(db, db_user.id)
    if slot is not None and slot.status == DedicatedIpSlot.STATUS_ACTIVE:
        return None, texts.t("DEDICATED_IP_ALREADY", "Выделенный IP уже подключён.")
    if slot is None and await dip.count_free_slots(db) <= 0:
        return None, texts.t("DEDICATED_IP_SOLD_OUT_SHORT", "Свободные IP временно закончились.")
    price = dip.price_for_remaining_kopeks(subscription.end_date)
    if price <= 0:
        return None, texts.t("DEDICATED_IP_NEEDS_SUBSCRIPTION", "Выделенный IP подключается к платной подписке.")
    return (subscription, price), None


async def handle_dedicated_ip_buy(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """«Подключить» → обычный экран оплаты: стоимость, баланс, не хватает, Оплатить."""
    from app.handlers.subscription.tariffs import _start_tariff_platega_checkout
    from app.services.user_cart_service import user_cart_service

    resolved, error = await _resolve_purchase(db, db_user)
    if error:
        await callback.answer(error, show_alert=True)
        return
    subscription, price = resolved

    cart = {
        "cart_mode": "dedicated_ip",
        "intent": True,
        "total_price": price,
        "subscription_end": subscription.end_date.isoformat(),
    }
    await user_cart_service.save_user_cart(db_user.id, cart, ttl=3600)

    texts = get_texts(db_user.language)
    await _start_tariff_platega_checkout(
        callback,
        db_user,
        state,
        total_kopeks=price,
        back_callback=MENU_CALLBACK,
        balance_callback="dip_pay_balance",
        note=texts.t(
            "DEDICATED_IP_INVOICE_NOTE", "🔒 Выделенный IP до {date}"
        ).format(date=_date(subscription.end_date)),
    )
    logger.info("🔒 Счёт на выделенный IP для %s: %s коп. до %s", db_user.telegram_id, price, subscription.end_date)


async def handle_dedicated_ip_pay_balance(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    from app.services.user_cart_service import user_cart_service

    texts = get_texts(db_user.language)
    resolved, error = await _resolve_purchase(db, db_user)
    if error:
        await callback.answer(error, show_alert=True)
        return
    subscription, price = resolved
    if db_user.balance_kopeks < price:
        await callback.answer(texts.t("INSUFFICIENT_BALANCE", "Недостаточно средств на балансе."), show_alert=True)
        return
    try:
        slot = await dip.charge_and_apply(db, db_user, subscription, price_kopeks=price, bot=callback.bot)
    except dip.NoFreeSlotError:
        await callback.answer(texts.t("DEDICATED_IP_SOLD_OUT_SHORT", "Свободные IP временно закончились."), show_alert=True)
        return
    except dip.InsufficientBalanceError:
        await callback.answer(texts.t("INSUFFICIENT_BALANCE", "Недостаточно средств на балансе."), show_alert=True)
        return
    await user_cart_service.delete_user_cart(db_user.id)
    await callback.answer()
    await _render(callback, _ready_text(db_user, slot), _ready_rows(db_user))


def _ready_text(user: User, slot: DedicatedIpSlot) -> str:
    texts = get_texts(user.language)
    return texts.t(
        "DEDICATED_IP_READY",
        "✅ Готово! Твой выделенный IP: <code>{ip}</code> ({country})\n\n"
        "В приложении появился сервер «🔒 Мой IP» — выбери его.\n"
        "Не видишь? Обнови подписку в приложении.",
    ).format(ip=slot.ip, country=dip.country_label())


def _ready_rows(user: User) -> list:
    texts = get_texts(user.language)
    return [
        [_btn(texts.t("DEDICATED_IP_HOWTO_BUTTON", "Как подключиться"), "howto")],
        [_btn(texts.t("DEDICATED_IP_TO_MENU_BUTTON", "В меню"), "back_to_menu")],
    ]


async def send_dedicated_ip_ready(bot, user: User, slot: DedicatedIpSlot) -> None:
    """Отдельное сообщение после оплаты: если не найдёт сервер — решит, что заплатил зря."""
    if bot is None or slot is None or not getattr(user, "telegram_id", None):
        return
    try:
        from app.branding.context import use_brand_for_user
        from app.utils.bot_registry import bot_for_user

        target_bot = bot_for_user(user, bot)
        with use_brand_for_user(user):
            await target_bot.send_message(
                user.telegram_id,
                _ready_text(user, slot),
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=_ready_rows(user)),
                parse_mode="HTML",
            )
    except Exception as error:
        logger.warning("🔒 Не отправлено сообщение о готовом IP юзеру %s: %s", user.telegram_id, error)


# ---------------------------------------------------------------- продление

async def handle_dedicated_ip_off(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    texts = get_texts(db_user.language)
    slot = await dip.get_user_slot(db, db_user.id)
    if slot is None or slot.status != DedicatedIpSlot.STATUS_ACTIVE:
        await show_dedicated_ip(callback, db_user, db)
        await callback.answer()
        return
    text = texts.t(
        "DEDICATED_IP_OFF_CONFIRM",
        "Отключить выделенный IP?\n\n"
        "IP будет работать до {date} и после этого не продлится.\n"
        "Подписка на VPN останется как есть. Деньги за оставшиеся дни не возвращаются.",
    ).format(date=_date(slot.paid_until))
    rows = [
        [_btn(texts.t("DEDICATED_IP_OFF_YES", "Да, отключить"), "dip_off_yes")],
        [_btn(texts.t("DEDICATED_IP_OFF_NO", "Оставить"), MENU_CALLBACK)],
    ]
    await _render(callback, text, rows)
    await callback.answer()


async def _set_autorenew(callback: types.CallbackQuery, db_user: User, db: AsyncSession, value: bool):
    slot = await dip.get_user_slot(db, db_user.id)
    if slot is not None and slot.status == DedicatedIpSlot.STATUS_ACTIVE:
        await dip.set_autorenew(db, slot, value)
        logger.info("🔒 Юзер %s %s продление IP %s", db_user.telegram_id, "включил" if value else "выключил", slot.ip)
    await show_dedicated_ip(callback, db_user, db)
    await callback.answer()


async def handle_dedicated_ip_off_yes(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await _set_autorenew(callback, db_user, db, False)


async def handle_dedicated_ip_on(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await _set_autorenew(callback, db_user, db, True)


# ---------------------------------------------------------------- админ

async def admin_dedicated_ips(message: types.Message, db_user: User, db: AsyncSession):
    """/dip — сводка пула; /dip release <слот> — освободить слот вручную."""
    if not settings.is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) >= 3 and parts[1] == "release":
        try:
            slot_no = int(parts[2])
        except ValueError:
            await message.answer("Формат: /dip release <номер слота>")
            return
        result = await db.execute(select(DedicatedIpSlot).where(DedicatedIpSlot.slot_no == slot_no))
        slot = result.scalar_one_or_none()
        if slot is None:
            await message.answer(f"Слот {slot_no} не найден")
            return
        await dip.release_slot(db, slot, reason=f"вручную админом {message.from_user.id}")
        await message.answer(f"Слот {slot_no} ({slot.ip}) освобождён")
        return

    stats = await dip.pool_stats(db)
    result = await db.execute(
        select(DedicatedIpSlot)
        .where(DedicatedIpSlot.status.in_([DedicatedIpSlot.STATUS_ACTIVE, DedicatedIpSlot.STATUS_HOLD]))
        .order_by(DedicatedIpSlot.slot_no)
    )
    lines = [
        "🔒 <b>Выделенные IP</b>",
        f"Всего {stats.get('total', 0)}: свободно {stats.get('free', 0)}, занято {stats.get('active', 0)}, "
        f"в удержании {stats.get('hold', 0)}, выведено {stats.get('disabled', 0)}",
        f"Продажа: {'включена' if settings.DEDICATED_IP_ENABLED else 'выключена'}"
        + (" (только админам)" if settings.DEDICATED_IP_ADMINS_ONLY else ""),
        "",
    ]
    for slot in result.scalars().all():
        until = _date(slot.paid_until if slot.status == DedicatedIpSlot.STATUS_ACTIVE else slot.hold_until)
        flag = "" if slot.autorenew else ", без продления"
        lines.append(f"#{slot.slot_no} {slot.ip} — {slot.status} до {until}, user_id {slot.user_id}{flag}, {slot.source or '—'}")
    await message.answer("\n".join(lines), parse_mode="HTML")


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_dedicated_ip_menu, F.data == MENU_CALLBACK)
    dp.callback_query.register(handle_dedicated_ip_info, F.data == "dip_info")
    dp.callback_query.register(handle_dedicated_ip_buy, F.data == "dip_buy")
    dp.callback_query.register(handle_dedicated_ip_pay_balance, F.data == "dip_pay_balance")
    dp.callback_query.register(handle_dedicated_ip_off, F.data == "dip_off")
    dp.callback_query.register(handle_dedicated_ip_off_yes, F.data == "dip_off_yes")
    dp.callback_query.register(handle_dedicated_ip_on, F.data == "dip_on")
    dp.message.register(admin_dedicated_ips, Command("dip"))
