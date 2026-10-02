"""Экран розыгрыша: условия, билеты и кнопка «Проверить» под каждым условием.

Экран — rich-сообщение (Bot API 10.1): баннер, заголовок, чек-лист условий,
раскрывающиеся правила. Если Telegram его не принял — тот же экран фото с
подписью. Любое нажатие пересчитывает все условия (см. giveaway_service),
перерисовывает экран и показывает алерт: что выполнено или чего не хватает.
"""

import html
import logging
import os
from datetime import datetime
from typing import Optional

from aiogram import Dispatcher, F, types
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.keyboards.inline import MAIN_MENU_CUSTOM_EMOJI_IDS
from app.utils.bot_registry import is_primary_bot
from app.utils.premium_text import apply_premium_text_emojis
from app.services.giveaway_service import (
    CHANNEL_TICKETS,
    FRIEND_PAID_TICKETS,
    INVITE_TIERS,
    PLAN_TICKETS,
    PLAN_TITLES,
    GiveawayProgress,
    giveaway_service,
)

logger = logging.getLogger(__name__)

GIVEAWAY_IMAGE = os.path.join("images", "giveaway.webp")
MENU_CALLBACK = "giveaway_menu"
CHECK_PREFIX = "giveaway_check:"
CHECK_KEYS = frozenset(
    ["channel", "friends_paid"]
    + [f"invite{threshold}" for threshold, _ in INVITE_TIERS]
    + [f"plan_{code}" for code in PLAN_TICKETS]
)

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _tickets_word(n: int) -> str:
    return _plural(n, "билет", "билета", "билетов")


def _friends_word(n: int) -> str:
    return _plural(n, "друга", "друзей", "друзей")


def _end_date_label() -> str:
    # Дата в часовом поясе, в котором она задана в конфиге (МСК).
    end = datetime.fromisoformat(settings.GIVEAWAY_END_AT)
    return f"{end.day} {_MONTHS[end.month - 1]} {end.year}"


def _channel_name() -> str:
    return settings.GIVEAWAY_CHANNEL


def _mark(done: bool) -> str:
    return "✅" if done else "❌"


def _referral_link(bot_username: Optional[str], user: User) -> Optional[str]:
    if not bot_username or not user.referral_code:
        return None
    return f"https://t.me/{bot_username}?start={user.referral_code}"


RULES = (
    "Подписка засчитывается на момент проверки: её можно оформить или продлить "
    "до конца розыгрыша. Пробный период не считается.",
    "Друзья — только новые, пришедшие по вашей ссылке с начала розыгрыша. "
    "Их оплаты тоже учитываются с начала розыгрыша.",
    "Нажмите «Проверить» под условием, чтобы обновить результат.",
)
TROPHY = "🏆"


def _conditions(progress: GiveawayProgress) -> list[tuple[bool, str]]:
    """(выполнено, подпись в HTML) — общий список для rich-экрана и фото-фолбэка."""
    channel_url = html.escape(settings.GIVEAWAY_CHANNEL_URL, quote=True)
    rows = [(
        bool(progress.channel_subscribed),
        f'Подписка на канал <a href="{channel_url}">{html.escape(_channel_name())}</a>'
        f" — <b>+{CHANNEL_TICKETS}</b>",
    )]
    for threshold, tickets in INVITE_TIERS:
        rows.append((
            progress.invite_tier_reached(threshold),
            f"Пригласить {threshold} {_friends_word(threshold)} — <b>+{tickets}</b>",
        ))
    for code, tickets in PLAN_TICKETS.items():
        rows.append((
            progress.plan_code == code,
            f"Активная подписка {PLAN_TITLES[code]} — <b>+{tickets}</b>",
        ))
    rows.append((
        progress.invited_paid_count > 0,
        f"Друг оплатил любую подписку — <b>+{FRIEND_PAID_TICKETS}</b> за каждого",
    ))
    return rows


def build_giveaway_rich_html(
    progress: GiveawayProgress,
    referral_link: Optional[str],
    *,
    banner_url: Optional[str],
    trophy_emoji_id: Optional[str] = None,
) -> str:
    """Экран розыгрыша в rich HTML (sendRichMessage, Bot API 10.1)."""
    trophy = (
        f'<tg-emoji emoji-id="{trophy_emoji_id}">{TROPHY}</tg-emoji>'
        if trophy_emoji_id
        else TROPHY
    )
    tickets = progress.total_tickets
    parts = []
    if banner_url:
        parts.append(f'<img src="{html.escape(banner_url, quote=True)}"/>')
    parts += [
        f"<h2>{trophy} Розыгрыш 2 × GTA VI</h2>",
        "<p>Разыгрываем <b>2 лицензии GTA VI</b> среди пользователей Leto VPN. "
        f"Итоги — <b>{_end_date_label()}</b>.</p>",
        f"<aside>🎫 Ваши билеты: <b>{tickets}</b>"
        f"<cite>каждый билет — ещё один шанс на победу</cite></aside>",
        "<hr/>",
        "<h3>🎟 Условия</h3>",
        "<ul>"
        + "".join(
            f'<li><input type="checkbox"{" checked" if done else ""}>{label}</li>'
            for done, label in _conditions(progress)
        )
        + "</ul>",
        "<p>Билеты за все выполненные условия складываются.</p>",
        "<h3>👥 Ваши друзья</h3>",
        f"<p>Новых друзей: <b>{progress.invited_count}</b> · "
        f"оплатили подписку: <b>{progress.invited_paid_count}</b></p>",
    ]
    if referral_link:
        parts.append(f"<p>Ваша ссылка: <code>{html.escape(referral_link)}</code></p>")
    parts += [
        "<details><summary>ℹ️ Правила</summary><ul>"
        + "".join(f"<li>{rule}</li>" for rule in RULES)
        + "</ul></details>",
        "<footer>Нажмите «Проверить» под условием, чтобы обновить результат.</footer>",
    ]
    return "".join(parts)


def build_giveaway_text(progress: GiveawayProgress, referral_link: Optional[str]) -> str:
    """Тот же экран подписью к фото — фолбэк, если rich-сообщение не прошло."""
    lines = [
        "🔴 <b>РОЗЫГРЫШ 2 × GTA VI</b>",
        "",
        "🎮 Разыгрываем <b>2 лицензии GTA VI</b> среди пользователей Leto VPN.",
        f"⏳ Итоги — <b>{_end_date_label()}</b>",
        "",
        "<blockquote>Выполняйте условия и копите билеты: каждый билет — ещё один "
        "шанс на победу. Билеты за все выполненные условия складываются.</blockquote>",
        "",
        "<b>🎟 Условия</b>",
        "",
    ]
    lines += [f"{_mark(done)} {label}" for done, label in _conditions(progress)]

    lines += [
        "",
        f"👥 Новых друзей: <b>{progress.invited_count}</b> · "
        f"оплатили: <b>{progress.invited_paid_count}</b>",
    ]
    if referral_link:
        lines.append(f"🔗 Ваша ссылка: <code>{html.escape(referral_link)}</code>")

    lines += [
        "",
        "<blockquote expandable>ℹ️ <b>Правила</b>\n"
        + "\n".join(f"• {rule}" for rule in RULES)
        + "</blockquote>",
        "",
        f"🎫 <b>Ваши билеты: {progress.total_tickets}</b>",
    ]
    return "\n".join(lines)


def _check_button(done: bool, done_text: str, todo_text: str, key: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=done_text if done else todo_text,
        callback_data=f"{CHECK_PREFIX}{key}",
        style=ButtonStyle.SUCCESS if done else ButtonStyle.DANGER,
    )


def build_giveaway_keyboard(progress: GiveawayProgress) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    channel_done = bool(progress.channel_subscribed)
    channel_row = [
        _check_button(
            channel_done,
            f"✅ Канал · +{CHANNEL_TICKETS}",
            "🔍 Проверить подписку на канал",
            "channel",
        )
    ]
    if not channel_done:
        channel_row.insert(
            0,
            InlineKeyboardButton(text="📢 Подписаться", url=settings.GIVEAWAY_CHANNEL_URL),
        )
    rows.append(channel_row)

    for threshold, tickets in INVITE_TIERS:
        rows.append([
            _check_button(
                progress.invite_tier_reached(threshold),
                f"✅ {threshold} {_friends_word(threshold)} · +{tickets}",
                f"🔍 Проверить: {threshold} {_friends_word(threshold)}",
                f"invite{threshold}",
            )
        ])

    for code, tickets in PLAN_TICKETS.items():
        rows.append([
            _check_button(
                progress.plan_code == code,
                f"✅ Подписка {PLAN_TITLES[code]} · +{tickets}",
                f"🔍 Проверить: подписка {PLAN_TITLES[code]}",
                f"plan_{code}",
            )
        ])

    rows.append([
        _check_button(
            progress.invited_paid_count > 0,
            f"✅ Оплатили друзья: {progress.invited_paid_count} · "
            f"+{progress.friend_paid_tickets}",
            "🔍 Проверить: оплата друзей",
            "friends_paid",
        )
    ])

    rows.append([InlineKeyboardButton(text="👥 Пригласить друзей", callback_data="menu_referrals")])
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def check_result_text(key: str, progress: GiveawayProgress) -> str:
    """Текст алерта по нажатому условию (до 200 символов — лимит Telegram)."""
    if key == "channel":
        if progress.channel_subscribed:
            return f"✅ Подписка на канал подтверждена: +{CHANNEL_TICKETS} билет."
        if progress.channel_subscribed is None:
            return "⚠️ Не удалось проверить подписку на канал. Попробуйте чуть позже."
        return (
            f"❌ Вы не подписаны на {_channel_name()}. "
            "Подпишитесь и нажмите «Проверить» ещё раз."
        )

    if key.startswith("invite"):
        threshold = int(key.removeprefix("invite"))
        tickets = dict(INVITE_TIERS)[threshold]
        count = progress.invited_count
        if count >= threshold:
            return (
                f"✅ Условие выполнено: новых друзей — {count}. "
                f"+{tickets} {_tickets_word(tickets)}."
            )
        missing = threshold - count
        return (
            f"❌ Новых друзей: {count} из {threshold}. Пригласите ещё "
            f"{missing} {_friends_word(missing)} по своей ссылке."
        )

    if key.startswith("plan_"):
        code = key.removeprefix("plan_")
        title = PLAN_TITLES[code]
        if progress.plan_code == code:
            tickets = PLAN_TICKETS[code]
            return f"✅ Подписка {title} активна: +{tickets} {_tickets_word(tickets)}."
        if progress.plan_code:
            current = PLAN_TITLES[progress.plan_code]
            tickets = progress.plan_tickets
            return (
                f"У вас активна подписка {current} — за неё уже начислено "
                f"+{tickets} {_tickets_word(tickets)}. Билеты за {title} даются "
                f"только при этом тарифе."
            )
        return (
            f"❌ Нет активной платной подписки {title}. Пробный период не считается — "
            f"оформите подписку до {_end_date_label()}."
        )

    if key == "friends_paid":
        paid = progress.invited_paid_count
        if paid:
            tickets = progress.friend_paid_tickets
            return (
                f"✅ Оплатили подписку друзей: {paid}. "
                f"+{tickets} {_tickets_word(tickets)}."
            )
        if progress.invited_count:
            return (
                "❌ Пока никто из новых друзей не оплатил подписку. "
                f"Новых друзей: {progress.invited_count}."
            )
        return (
            "❌ С начала розыгрыша у вас нет новых друзей. "
            "Пригласите друзей по своей ссылке."
        )

    return "Условие не найдено."


def _finished_text(tickets: Optional[int]) -> str:
    lines = [
        "🏁 <b>Розыгрыш 2 × GTA VI завершён</b>",
        "",
        "Условия больше не проверяются — скоро подведём итоги и объявим "
        "победителей в канале.",
    ]
    if tickets is not None:
        lines += ["", f"🎫 <b>Ваши билеты: {tickets}</b>"]
    return "\n".join(lines)


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="back_to_menu")]
    ])


def _is_not_modified(error: TelegramBadRequest) -> bool:
    return "message is not modified" in str(error)


def _with_premium_emoji(bot, rich_html: str) -> str:
    """Анимированные эмодзи основного бота.

    PremiumEmojiBot подменяет эмодзи только в text/caption, rich_message он не
    видит — применяем ту же замену к rich HTML сами.
    """
    emoji_map = getattr(bot, "_text_emoji_map", None)
    if not emoji_map:
        return rich_html
    return apply_premium_text_emojis(
        rich_html, emoji_map, getattr(bot, "_text_emoji_pattern", None)
    )


def _banner_url() -> Optional[str]:
    if settings.GIVEAWAY_BANNER_URL:
        return settings.GIVEAWAY_BANNER_URL
    if settings.WEBHOOK_URL:
        return f"{settings.WEBHOOK_URL.rstrip('/')}/miniapp/static/giveaway.jpg"
    return None


async def _render_rich(
    callback: types.CallbackQuery, rich_html: str, keyboard: InlineKeyboardMarkup
) -> bool:
    """Показывает rich-экран; False — Telegram его не принял, нужен фолбэк."""
    message = callback.message
    rich = InputRichMessage(html=_with_premium_emoji(callback.bot, rich_html))

    if message is not None and not message.photo:
        try:
            await callback.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                rich_message=rich,
                reply_markup=keyboard,
            )
            return True
        except TelegramBadRequest as error:
            if _is_not_modified(error):
                return True
            logger.debug("Розыгрыш: rich-редактирование не удалось: %s", error)

    try:
        await callback.bot.send_rich_message(
            chat_id=callback.from_user.id, rich_message=rich, reply_markup=keyboard
        )
    except Exception as error:  # noqa: BLE001 — любой отказ rich = показываем фото
        logger.warning("Розыгрыш: rich-сообщение не отправлено, показываем фото: %s", error)
        return False

    if message is not None:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
    return True


async def _render(
    callback: types.CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup,
    *,
    on_screen: bool,
) -> None:
    """Фото с подписью — финальный экран и фолбэк, если rich не прошёл.

    Свой рендер вместо edit_or_answer_photo: тот меряет лимит подписи вместе с
    HTML-тегами и при длинной разметке уходит в текст без картинки. on_screen —
    нажатие пришло с самого экрана розыгрыша: картинка уже стоит, меняем подпись.
    """
    message = callback.message
    if message is not None and message.photo:
        try:
            if on_screen:
                await message.edit_caption(
                    caption=text, reply_markup=keyboard, parse_mode="HTML"
                )
            else:
                await message.edit_media(
                    InputMediaPhoto(
                        media=FSInputFile(GIVEAWAY_IMAGE), caption=text, parse_mode="HTML"
                    ),
                    reply_markup=keyboard,
                )
            return
        except TelegramBadRequest as error:
            # Повторная проверка без изменений — экран уже актуален.
            if _is_not_modified(error):
                return
            logger.debug("Розыгрыш: не удалось отредактировать, переотправляем: %s", error)

    if message is not None:
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
    await callback.bot.send_photo(
        chat_id=callback.from_user.id,
        photo=FSInputFile(GIVEAWAY_IMAGE),
        caption=text,
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def _show(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    check_key: Optional[str],
) -> None:
    on_screen = check_key is not None
    if not giveaway_service.is_running():
        entry = await giveaway_service.get_entry(db, db_user.id)
        await _render(
            callback,
            _finished_text(entry.tickets if entry else None),
            _back_keyboard(),
            on_screen=on_screen,
        )
        await callback.answer()
        return

    progress = await giveaway_service.collect(db, callback.bot, db_user)
    if progress.channel_subscribed is None:
        # Telegram не ответил — не отнимаем уже подтверждённую подписку на канал.
        entry = await giveaway_service.get_entry(db, db_user.id)
        if entry is not None and entry.channel_subscribed:
            progress = GiveawayProgress(
                channel_subscribed=True,
                invited_count=progress.invited_count,
                invited_paid_count=progress.invited_paid_count,
                plan_code=progress.plan_code,
            )
    await giveaway_service.save_entry(db, db_user.id, progress)

    bot_username = (await callback.bot.get_me()).username
    referral_link = _referral_link(bot_username, db_user)
    keyboard = build_giveaway_keyboard(progress)
    is_primary = is_primary_bot(callback.bot.id if callback.bot else None)
    rich_html = build_giveaway_rich_html(
        progress,
        referral_link,
        banner_url=_banner_url(),
        trophy_emoji_id=MAIN_MENU_CUSTOM_EMOJI_IDS["giveaway"] if is_primary else None,
    )
    if not await _render_rich(callback, rich_html, keyboard):
        await _render(
            callback,
            build_giveaway_text(progress, referral_link),
            keyboard,
            on_screen=on_screen,
        )

    if check_key:
        await callback.answer(check_result_text(check_key, progress), show_alert=True)
    else:
        await callback.answer()


async def show_giveaway(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    await _show(callback, db_user, db, check_key=None)


async def check_giveaway_condition(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession
):
    key = callback.data.removeprefix(CHECK_PREFIX)
    await _show(callback, db_user, db, check_key=key if key in CHECK_KEYS else None)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(show_giveaway, F.data == MENU_CALLBACK)
    dp.callback_query.register(
        check_giveaway_condition, F.data.startswith(CHECK_PREFIX)
    )
