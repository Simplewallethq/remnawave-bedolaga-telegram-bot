import logging
import os
from pathlib import Path

import qrcode
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.inline import get_referral_keyboard
from app.localization.texts import get_texts
from app.utils.photo_message import edit_or_answer_photo
from app.utils.user_utils import (
    get_detailed_referral_list,
    get_referral_analytics,
    get_user_referral_summary,
)

logger = logging.getLogger(__name__)


async def show_referral_info(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    texts = get_texts(db_user.language)

    summary = await get_user_referral_summary(db, db_user.id)

    bot_username = (await callback.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={db_user.referral_code}"

    earned_rub = int(summary['total_earned_kopeks'] / 100)

    referral_text = (
        texts.t("REFERRAL_TITLE", "Реферальная программа\n\n")
        + texts.t("REFERRAL_INVITED", "Приглашено друзей: {count}").format(count=summary['invited_count'])
        + "\n"
        + texts.t("REFERRAL_EARNED", "Заработано: {amount}₽").format(amount=earned_rub)
        + "\n"
        + texts.t("REFERRAL_YOUR_LINK", "\nВаша реферальная ссылка:")
        + f"\n<code>{referral_link}</code>\n"
        + texts.t("REFERRAL_REWARD", "\nПолучайте 50% со всех платежей приглашённых друзей!")
    )

    await edit_or_answer_photo(
        callback,
        referral_text,
        get_referral_keyboard(db_user.language),
        photo_path=(
            os.path.join("images", "ref.webp")
            if os.path.exists(os.path.join("images", "ref.webp"))
            else None
        ),
    )
    await callback.answer()


async def show_referral_qr(
    callback: types.CallbackQuery,
    db_user: User,
):
    await callback.answer()

    texts = get_texts(db_user.language)

    bot_username = (await callback.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={db_user.referral_code}"

    qr_dir = Path("data") / "referral_qr"
    qr_dir.mkdir(parents=True, exist_ok=True)

    file_path = qr_dir / f"{db_user.id}.png"
    if not file_path.exists():
        img = qrcode.make(referral_link)
        img.save(file_path)

    photo = FSInputFile(file_path)
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data="menu_referrals")]]
    )

    try:
        await callback.message.edit_media(
            types.InputMediaPhoto(
                media=photo,
                caption=texts.t(
                    "REFERRAL_LINK_CAPTION",
                    "🔗 Ваша реферальная ссылка:\n{link}",
                ).format(link=referral_link),
            ),
            reply_markup=keyboard,
        )
    except TelegramBadRequest:
        await callback.message.delete()
        await callback.message.answer_photo(
            photo,
            caption=texts.t(
                "REFERRAL_LINK_CAPTION",
                "🔗 Ваша реферальная ссылка:\n{link}",
            ).format(link=referral_link),
            reply_markup=keyboard,
        )


async def show_detailed_referral_list(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    page: int = 1
):
    texts = get_texts(db_user.language)

    referrals_data = await get_detailed_referral_list(db, db_user.id, limit=10, offset=(page - 1) * 10)

    if not referrals_data['referrals']:
        await edit_or_answer_photo(
            callback,
            texts.t(
                "REFERRAL_LIST_EMPTY",
                "📋 У вас пока нет рефералов.\n\nПоделитесь своей реферальной ссылкой, чтобы начать зарабатывать!",
            ),
            types.InlineKeyboardMarkup(
                inline_keyboard=[[types.InlineKeyboardButton(text=texts.BACK, callback_data="menu_referrals")]]
            ),
            parse_mode=None,
        )
        await callback.answer()
        return

    text = texts.t(
        "REFERRAL_LIST_HEADER",
        "👥 <b>Ваши рефералы</b> (стр. {current}/{total})",
    ).format(
        current=referrals_data['current_page'],
        total=referrals_data['total_pages'],
    ) + "\n\n"
    
    for i, referral in enumerate(referrals_data['referrals'], 1):
        status_emoji = "🟢" if referral['status'] == 'active' else "🔴"
        
        topup_emoji = "💰" if referral['has_made_first_topup'] else "⏳"
        
        text += texts.t(
            "REFERRAL_LIST_ITEM_HEADER",
            "{index}. {status} <b>{name}</b>",
        ).format(index=i, status=status_emoji, name=referral['full_name']) + "\n"
        text += texts.t(
            "REFERRAL_LIST_ITEM_TOPUPS",
            "   {emoji} Пополнений: {count}",
        ).format(emoji=topup_emoji, count=referral['topups_count']) + "\n"
        text += texts.t(
            "REFERRAL_LIST_ITEM_EARNED",
            "   💎 Заработано с него: {amount}",
        ).format(amount=texts.format_price(referral['total_earned_kopeks'])) + "\n"
        text += texts.t(
            "REFERRAL_LIST_ITEM_REGISTERED",
            "   📅 Регистрация: {days} дн. назад",
        ).format(days=referral['days_since_registration']) + "\n"

        if referral['days_since_activity'] is not None:
            text += texts.t(
                "REFERRAL_LIST_ITEM_ACTIVITY",
                "   🕐 Активность: {days} дн. назад",
            ).format(days=referral['days_since_activity']) + "\n"
        else:
            text += texts.t(
                "REFERRAL_LIST_ITEM_ACTIVITY_LONG_AGO",
                "   🕐 Активность: давно",
            ) + "\n"
        
        text += "\n"
    
    keyboard = []
    nav_buttons = []
    
    if referrals_data['has_prev']:
        nav_buttons.append(types.InlineKeyboardButton(
            text=texts.t("REFERRAL_LIST_PREV_PAGE", "⬅️ Назад"),
            callback_data=f"referral_list_page_{page - 1}"
        ))

    if referrals_data['has_next']:
        nav_buttons.append(types.InlineKeyboardButton(
            text=texts.t("REFERRAL_LIST_NEXT_PAGE", "Вперед ➡️"),
            callback_data=f"referral_list_page_{page + 1}"
        ))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([types.InlineKeyboardButton(
        text=texts.BACK,
        callback_data="menu_referrals"
    )])

    await edit_or_answer_photo(
        callback,
        text,
        types.InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    await callback.answer()


async def show_referral_analytics(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession
):
    texts = get_texts(db_user.language)

    analytics = await get_referral_analytics(db, db_user.id)

    text = texts.t("REFERRAL_ANALYTICS_TITLE", "📊 <b>Аналитика рефералов</b>") + "\n\n"

    text += texts.t(
        "REFERRAL_ANALYTICS_EARNINGS_HEADER",
        "💰 <b>Доходы по периодам:</b>",
    ) + "\n"
    text += texts.t(
        "REFERRAL_ANALYTICS_EARNINGS_TODAY",
        "• Сегодня: {amount}",
    ).format(amount=texts.format_price(analytics['earnings_by_period']['today'])) + "\n"
    text += texts.t(
        "REFERRAL_ANALYTICS_EARNINGS_WEEK",
        "• За неделю: {amount}",
    ).format(amount=texts.format_price(analytics['earnings_by_period']['week'])) + "\n"
    text += texts.t(
        "REFERRAL_ANALYTICS_EARNINGS_MONTH",
        "• За месяц: {amount}",
    ).format(amount=texts.format_price(analytics['earnings_by_period']['month'])) + "\n"
    text += texts.t(
        "REFERRAL_ANALYTICS_EARNINGS_QUARTER",
        "• За квартал: {amount}",
    ).format(amount=texts.format_price(analytics['earnings_by_period']['quarter'])) + "\n\n"

    if analytics['top_referrals']:
        text += texts.t(
            "REFERRAL_ANALYTICS_TOP_TITLE",
            "🏆 <b>Топ-{count} рефералов:</b>",
        ).format(count=len(analytics['top_referrals'])) + "\n"
        for i, ref in enumerate(analytics['top_referrals'], 1):
            text += texts.t(
                "REFERRAL_ANALYTICS_TOP_ITEM",
                "{index}. {name}: {amount} ({count} начислений)",
            ).format(
                index=i,
                name=ref['referral_name'],
                amount=texts.format_price(ref['total_earned_kopeks']),
                count=ref['earnings_count'],
            ) + "\n"
        text += "\n"

    text += texts.t(
        "REFERRAL_ANALYTICS_FOOTER",
        "📈 Продолжайте развивать свою реферальную сеть!",
    )

    await edit_or_answer_photo(
        callback,
        text,
        types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text=texts.BACK, callback_data="menu_referrals")]
        ]),
    )
    await callback.answer()


async def create_invite_message(
    callback: types.CallbackQuery,
    db_user: User
):
    texts = get_texts(db_user.language)

    bot_username = (await callback.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start={db_user.referral_code}"

    invite_text = (
        texts.t("REFERRAL_INVITE_TITLE", "🎉 Присоединяйся к VPN сервису!")
        + "\n\n"
        + texts.t("REFERRAL_INVITE_FEATURE_FAST", "🚀 Быстрое подключение")
        + "\n"
        + texts.t("REFERRAL_INVITE_FEATURE_SERVERS", "🌍 Серверы по всему миру")
        + "\n"
        + texts.t("REFERRAL_INVITE_FEATURE_SECURE", "🔒 Надежная защита")
        + "\n\n"
        + texts.t("REFERRAL_INVITE_LINK_PROMPT", "👇 Переходи по ссылке:")
        + f"\n{referral_link}"
    )

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(
            text=texts.t("REFERRAL_SHARE_BUTTON", "📤 Поделиться"),
            switch_inline_query=invite_text
        )],
        [types.InlineKeyboardButton(
            text=texts.BACK,
            callback_data="menu_referrals"
        )]
    ])

    await edit_or_answer_photo(
        callback,
        (
            texts.t("REFERRAL_INVITE_CREATED_TITLE", "📝 <b>Приглашение создано!</b>")
            + "\n\n"
            + texts.t(
                "REFERRAL_INVITE_CREATED_INSTRUCTION",
                "Нажмите кнопку «📤 Поделиться» чтобы отправить приглашение в любой чат, или скопируйте текст ниже:",
            )
            + "\n\n"
            f"<code>{invite_text}</code>"
        ),
        keyboard,
    )
    await callback.answer()


def register_handlers(dp: Dispatcher):
    
    dp.callback_query.register(
        show_referral_info,
        F.data == "menu_referrals"
    )
    
    dp.callback_query.register(
        create_invite_message,
        F.data == "referral_create_invite"
    )

    dp.callback_query.register(
        show_referral_qr,
        F.data == "referral_show_qr"
    )
    
    dp.callback_query.register(
        show_detailed_referral_list,
        F.data == "referral_list"
    )
    
    dp.callback_query.register(
        show_referral_analytics,
        F.data == "referral_analytics"
    )
    
    dp.callback_query.register(
        lambda callback, db_user, db: show_detailed_referral_list(
            callback, db_user, db, int(callback.data.split('_')[-1])
        ),
        F.data.startswith("referral_list_page_")
    )
