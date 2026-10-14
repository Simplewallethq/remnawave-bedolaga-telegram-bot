"""Платформа владельцев: выбор режима и подключение своего бота (шаги 1–4)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from aiogram import types
from aiogram.fsm.context import FSMContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.copycat import get_copycat_bot_by_token
from app.database.crud.user import create_user, get_user_by_telegram_id, resolve_referrer_by_link_payload
from app.database.models import User, UserStatus
from app.keyboards.owner import back_keyboard, bot_ready_keyboard, cancel_keyboard
from app.services import owner_platform_service as platform
from app.services.blacklist_service import blacklist_service
from app.states import OwnerStates

from .common import language_of, show_home, t

logger = logging.getLogger(__name__)

_IMAGE_MIME_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}


# ---------------------------------------------------------------- режимы


async def _ensure_platform_user(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession) -> Optional[User]:
    """Аккаунт нового пользователя, выбравшего «создать бота»: без триала и онбординга."""
    from_user = callback.from_user
    user = await get_user_by_telegram_id(db, from_user.id)
    if user is not None and user.status != UserStatus.DELETED.value:
        return user

    is_blacklisted, reason = await blacklist_service.is_user_blacklisted(from_user.id, from_user.username)
    if is_blacklisted:
        await callback.message.answer(f"🚫 Регистрация невозможна\n\nПричина: {reason}")
        await state.clear()
        return None

    data = await state.get_data() or {}
    language = data.get("language") or "ru"
    if user is not None:
        user.status = UserStatus.ACTIVE.value
        user.username = from_user.username
        user.first_name = from_user.first_name
        user.last_name = from_user.last_name
        user.last_activity = datetime.utcnow()
        await db.commit()
        return user

    referrer_id = data.get("referrer_id")
    if not referrer_id and data.get("referral_code"):
        referrer = await resolve_referrer_by_link_payload(db, data["referral_code"])
        referrer_id = referrer.id if referrer else None
    return await create_user(
        db,
        telegram_id=from_user.id,
        username=from_user.username,
        first_name=from_user.first_name,
        last_name=from_user.last_name,
        language=language,
        language_code=from_user.language_code,
        referred_by_id=referrer_id,
        bot_id=callback.bot.id,
    )


async def choose_create(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: Optional[User] = None):
    user = db_user or await _ensure_platform_user(callback, state, db)
    if user is None:
        await callback.answer()
        return
    await state.clear()
    await platform.set_mode(db, user.id, platform.MODE_OWNER)
    await callback.answer()
    await start_creation(callback, state, db, user)


async def choose_vpn(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: Optional[User] = None):
    if db_user is None:
        # Новый пользователь: обычная регистрация с онбордингом и триалом.
        from app.handlers.start import _continue_registration_after_language

        await callback.answer()
        await _continue_registration_after_language(message=None, callback=callback, state=state, db=db)
        return
    await try_vpn(callback, state, db, db_user)


async def try_vpn(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    """Режим VPN: не проходил онбординг — проходит (триал), иначе — меню VPN."""
    await state.clear()
    await platform.set_mode(db, db_user.id, platform.MODE_VPN)
    await callback.answer()
    await db.refresh(db_user, ["subscription"])
    if db_user.subscription is None and not db_user.has_had_paid_subscription:
        from app.handlers.start import _activate_trial_or_deny_for_device

        activated = await _activate_trial_or_deny_for_device(
            callback.bot, db, db_user, None, callback, is_callback=True, language=language_of(db_user)
        )
        if activated:
            return
    from app.handlers.menu import show_main_menu

    await show_main_menu(callback, db_user, db, skip_callback_answer=True)


async def entry_from_vpn(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    """«Создать VPN бота» в меню VPN: есть боты — меню владельца, нет — шаг 1."""
    await state.clear()
    await platform.set_mode(db, db_user.id, platform.MODE_OWNER)
    await callback.answer()
    if await platform.list_owner_bots(db, db_user.id, enabled_only=True):
        await show_home(callback, db, db_user)
    else:
        await start_creation(callback, state, db, db_user)


async def go_home(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    await state.clear()
    await callback.answer()
    await show_home(callback, db, db_user)


# ---------------------------------------------------------------- шаг 1


async def _prompt_username(target, language: str) -> None:
    text = t(
        language,
        "OWNER_STEP1_TEXT",
        "Создай бота в @BotFather (команда /newbot) и пришли сюда его юзернейм, например @myvpn_bot. "
        "Либо укажи юзернейм уже готового бота.",
    )
    keyboard = back_keyboard(language, "wl_home")
    if isinstance(target, types.CallbackQuery):
        from .common import answer_screen

        await answer_screen(target, text, keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


async def start_creation(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    active = await platform.list_owner_bots(db, db_user.id, enabled_only=True)
    if len(active) >= int(settings.OWNER_MAX_BOTS):
        await callback.message.answer(
            t(language, "OWNER_MAX_BOTS_REACHED", "У тебя уже {count} ботов — это максимум.", count=len(active)),
            reply_markup=back_keyboard(language, "wl_home"),
        )
        return
    await state.set_state(OwnerStates.waiting_for_bot_username)
    await _prompt_username(callback, language)


async def new_bot(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    await callback.answer()
    await start_creation(callback, state, db, db_user)


async def handle_bot_username(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    username = platform.normalize_bot_username(message.text or "")
    if username is None:
        await message.answer(
            t(
                language,
                "OWNER_BOT_USERNAME_INVALID",
                "Юзернейм бота должен состоять из латиницы, цифр и _ (5–32 символа) и заканчиваться на bot, "
                "например myvpn_bot.",
            ),
            reply_markup=back_keyboard(language, "wl_home"),
        )
        return
    if username.lower() == (settings.BOT_USERNAME or "").lstrip("@").lower():
        await _reply_already_connected(message, language)
        return

    occupancy = await platform.occupancy_by_username(db, username, db_user.id)
    if occupancy.status == "active":
        await _reply_already_connected(message, language)
        return
    if occupancy.status == "foreign":
        await _reply_foreign(message, language)
        return

    await state.update_data(wl_username=username)
    await state.set_state(OwnerStates.waiting_for_bot_token)
    await _prompt_token(message, language)


async def _reply_already_connected(message: types.Message, language: str) -> None:
    await message.answer(
        t(language, "OWNER_BOT_ALREADY_CONNECTED", "Этот бот уже подключен к сервису, выбери другого бота."),
        reply_markup=back_keyboard(language, "wl_step1"),
    )


async def _reply_foreign(message: types.Message, language: str) -> None:
    await message.answer(
        t(language, "OWNER_BOT_FOREIGN", "Этот бот уже подключен к сервису другим владельцем."),
        reply_markup=back_keyboard(language, "wl_step1"),
    )


async def back_to_step1(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    await callback.answer()
    await state.set_state(OwnerStates.waiting_for_bot_username)
    await _prompt_username(callback, language_of(db_user))


# ---------------------------------------------------------------- шаг 2


async def _prompt_token(target, language: str) -> None:
    text = t(
        language,
        "OWNER_STEP2_TEXT",
        "Теперь дай нам API токен для управления этим ботом (его можно получить в @BotFather).",
    )
    keyboard = cancel_keyboard(language)
    if isinstance(target, types.CallbackQuery):
        from .common import answer_screen

        await answer_screen(target, text, keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


async def back_to_step2(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    await callback.answer()
    if not (await state.get_data()).get("wl_username"):
        await back_to_step1(callback, state, db, db_user)
        return
    await state.set_state(OwnerStates.waiting_for_bot_token)
    await _prompt_token(callback, language_of(db_user))


async def handle_bot_token(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    token = (message.text or "").strip()
    data = await state.get_data()
    username = data.get("wl_username")
    if not username:
        await state.set_state(OwnerStates.waiting_for_bot_username)
        await _prompt_username(message, language)
        return

    # Токен — секрет: убираем его из переписки.
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - нет прав удалить — не страшно
        pass

    me = await platform.fetch_bot_identity(token)
    if me is None:
        await message.answer(
            t(language, "OWNER_TOKEN_INVALID", "Токен не подходит. Скопируй его из @BotFather целиком."),
            reply_markup=back_keyboard(language, "wl_step2"),
        )
        return
    if (me.username or "").lower() != username.lower():
        await message.answer(
            t(
                language,
                "OWNER_TOKEN_OTHER_BOT",
                "Этот токен от @{other_bot}, а не от @{bot}. Пришли токен нужного бота.",
                other_bot=me.username,
                bot=username,
            ),
            reply_markup=back_keyboard(language, "wl_step2"),
        )
        return
    if platform.is_reserved_bot_id(me.id) or await get_copycat_bot_by_token(db, token):
        await _reply_already_connected(message, language)
        return

    occupancy = await platform.occupancy_by_bot_id(db, me.id, db_user.id)
    if occupancy.status == "active":
        await _reply_already_connected(message, language)
        return
    if occupancy.status == "foreign":
        await _reply_foreign(message, language)
        return
    if occupancy.status == "restore":
        await platform.restore_bot(db, occupancy.row, token=token, username=me.username, title=me.full_name)
        await state.clear()
        await platform.refresh_runtime()
        await message.answer(
            t(
                language,
                "OWNER_BOT_RESTORED",
                "Твой бот @{bot} снова подключен. Подписчики и настройки сохранились.",
                bot=me.username,
            ),
            reply_markup=bot_ready_keyboard(language),
        )
        return

    await state.update_data(wl_token=token, wl_bot_id=me.id, wl_username=me.username, wl_title=me.full_name)
    await state.set_state(OwnerStates.waiting_for_bot_picture)
    await _prompt_picture(message, language)


# ---------------------------------------------------------------- шаг 3


async def _prompt_picture(target, language: str) -> None:
    text = t(
        language,
        "OWNER_STEP3_TEXT",
        "Закачай картинку для твоего будущего бота (она показывается внутри бота над пользовательским меню).",
    )
    keyboard = cancel_keyboard(language)
    if isinstance(target, types.CallbackQuery):
        from .common import answer_screen

        await answer_screen(target, text, keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


async def back_to_step3(callback: types.CallbackQuery, state: FSMContext, db: AsyncSession, db_user: User):
    await callback.answer()
    if not (await state.get_data()).get("wl_token"):
        await back_to_step1(callback, state, db, db_user)
        return
    await state.set_state(OwnerStates.waiting_for_bot_picture)
    await _prompt_picture(callback, language_of(db_user))


def picture_file(message: types.Message) -> Optional[tuple[str, str]]:
    """(file_id, расширение) присланной картинки jpg/png, иначе None."""
    if message.photo:
        return message.photo[-1].file_id, ".jpg"
    document = message.document
    if document is not None and (document.mime_type or "").lower() in _IMAGE_MIME_TYPES:
        return document.file_id, _IMAGE_MIME_TYPES[document.mime_type.lower()]
    return None


async def reply_not_a_picture(message: types.Message, language: str, back: str) -> None:
    await message.answer(
        t(language, "OWNER_PICTURE_INVALID", "Необходимо прислать картинку в формате jpg, png."),
        reply_markup=back_keyboard(language, back),
    )


async def handle_bot_picture(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    picture = picture_file(message)
    if picture is None:
        await reply_not_a_picture(message, language, "wl_step3")
        return
    data = await state.get_data()
    if not data.get("wl_token"):
        await state.set_state(OwnerStates.waiting_for_bot_username)
        await _prompt_username(message, language)
        return

    file_id, suffix = picture
    try:
        logo = await platform.save_picture(message.bot, file_id, int(data["wl_bot_id"]), suffix=suffix)
    except Exception as error:  # noqa: BLE001
        logger.error("Не удалось сохранить картинку бота владельца: %s", error)
        await reply_not_a_picture(message, language, "wl_step3")
        return
    await state.update_data(wl_logo=logo)

    owner_username = platform.normalize_support_username(message.from_user.username or "")
    if owner_username is None:
        await state.set_state(OwnerStates.waiting_for_support_username)
        await message.answer(
            t(language, "OWNER_STEP4_TEXT", "Введи юзернейм для саппорта."),
            reply_markup=cancel_keyboard(language),
        )
        return
    await _finish_creation(message, state, db, db_user, support=owner_username, own_username=True)


# ---------------------------------------------------------------- шаг 4


async def handle_support_username(message: types.Message, state: FSMContext, db: AsyncSession, db_user: User):
    language = language_of(db_user)
    support = platform.normalize_support_username(message.text or "")
    if support is None:
        await message.answer(
            t(language, "OWNER_SUPPORT_INVALID", "Юзернейм может содержать латиницу, цифры и _ (5–32 символа)."),
            reply_markup=cancel_keyboard(language),
        )
        return
    await _finish_creation(message, state, db, db_user, support=support, own_username=False)


async def _finish_creation(
    message: types.Message,
    state: FSMContext,
    db: AsyncSession,
    db_user: User,
    *,
    support: str,
    own_username: bool,
) -> None:
    language = language_of(db_user)
    data = await state.get_data()
    active = await platform.list_owner_bots(db, db_user.id, enabled_only=True)
    if len(active) >= int(settings.OWNER_MAX_BOTS):
        await state.clear()
        await message.answer(
            t(language, "OWNER_MAX_BOTS_REACHED", "У тебя уже {count} ботов — это максимум.", count=len(active)),
            reply_markup=back_keyboard(language, "wl_home"),
        )
        return
    try:
        await platform.connect_bot(
            db,
            owner_user_id=db_user.id,
            token=data["wl_token"],
            bot_id=int(data["wl_bot_id"]),
            username=data["wl_username"],
            title=data.get("wl_title") or data["wl_username"],
            logo=data.get("wl_logo"),
            support=support,
        )
    except IntegrityError:
        # Пока владелец грузил картинку, этого бота успели подключить.
        await db.rollback()
        await state.clear()
        await _reply_already_connected(message, language)
        return
    await state.clear()
    await platform.refresh_runtime()

    if own_username:
        text = t(
            language,
            "OWNER_BOT_READY",
            "Твой бот @{bot} готов. В качестве адреса поддержки указан твой юзернейм (@{support}) "
            "и выбраны стандартные цены. Поменять ты можешь в разделе настройки.",
            bot=data["wl_username"],
            support=support,
        )
    else:
        text = t(
            language,
            "OWNER_BOT_READY_CUSTOM_SUPPORT",
            "Твой бот @{bot} готов. Выбраны стандартные цены. Поменять можешь в разделе настройки.",
            bot=data["wl_username"],
        )
    await message.answer(text, reply_markup=bot_ready_keyboard(language))
