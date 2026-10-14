"""Платформа для владельцев витрин: в боте OWNER_PLATFORM_BOT_ID любой может
подключить своего VPN-бота и вести его (см. app/services/owner_platform_service.py)."""

from aiogram import Dispatcher, F

from app.states import OwnerStates

from . import bot_settings, create, earnings
from .common import OwnerPlatformBot, show_owner_home_on_start, show_platform_choice

__all__ = ["register_handlers", "show_owner_home_on_start", "show_platform_choice"]


def register_handlers(dp: Dispatcher) -> None:
    on_platform = OwnerPlatformBot()
    callback = dp.callback_query.register
    message = dp.message.register

    # Выбор режима и переходы.
    callback(create.choose_create, on_platform, F.data == "wl_choice_create")
    callback(create.choose_vpn, on_platform, F.data == "wl_choice_vpn")
    callback(create.entry_from_vpn, on_platform, F.data == "wl_entry")
    callback(create.try_vpn, on_platform, F.data == "wl_try_vpn")
    callback(create.go_home, on_platform, F.data.in_({"wl_home", "wl_menu"}))

    # Подключение бота.
    callback(create.new_bot, on_platform, F.data == "wl_new")
    callback(create.back_to_step1, on_platform, F.data == "wl_step1")
    callback(create.back_to_step2, on_platform, F.data == "wl_step2")
    callback(create.back_to_step3, on_platform, F.data == "wl_step3")
    message(create.handle_bot_username, on_platform, OwnerStates.waiting_for_bot_username)
    message(create.handle_bot_token, on_platform, OwnerStates.waiting_for_bot_token)
    message(create.handle_bot_picture, on_platform, OwnerStates.waiting_for_bot_picture)
    message(create.handle_support_username, on_platform, OwnerStates.waiting_for_support_username)

    # Настройки ботов.
    callback(bot_settings.show_settings, on_platform, F.data == "wl_settings")
    callback(bot_settings.show_bot, on_platform, F.data.startswith("wl_bot:"))
    callback(bot_settings.show_support, on_platform, F.data.startswith("wl_support:"))
    callback(bot_settings.edit_support, on_platform, F.data.startswith("wl_support_edit:"))
    callback(bot_settings.show_prices, on_platform, F.data.startswith("wl_prices:"))
    callback(bot_settings.edit_prices, on_platform, F.data.startswith("wl_prices_edit:"))
    callback(bot_settings.show_picture, on_platform, F.data.startswith("wl_pic:"))
    callback(bot_settings.edit_picture, on_platform, F.data.startswith("wl_pic_edit:"))
    callback(bot_settings.ask_delete, on_platform, F.data.startswith("wl_del:"))
    callback(bot_settings.confirm_delete, on_platform, F.data.startswith("wl_del_ask:"))
    callback(bot_settings.do_delete, on_platform, F.data.startswith("wl_del_yes:"))
    message(bot_settings.handle_new_support, on_platform, OwnerStates.waiting_for_new_support)
    message(bot_settings.handle_price, on_platform, OwnerStates.waiting_for_solo_price)
    message(bot_settings.handle_price, on_platform, OwnerStates.waiting_for_plus_price)
    message(bot_settings.handle_price, on_platform, OwnerStates.waiting_for_pro_price)
    message(bot_settings.handle_new_picture, on_platform, OwnerStates.waiting_for_new_picture)

    # Заработок и вывод.
    callback(earnings.show_earnings, on_platform, F.data == "wl_earn")
    callback(earnings.show_wallet, on_platform, F.data == "wl_wallet")
    callback(earnings.start_payout, on_platform, F.data == "wl_payout")
    callback(earnings.confirm_payout, on_platform, F.data == "wl_payout_confirm")
    message(earnings.handle_wallet, on_platform, OwnerStates.waiting_for_wallet)
    message(earnings.handle_payout_amount, on_platform, OwnerStates.waiting_for_payout_amount)

    # Кнопки заявок на вывод в админ-чате — их присылает основной бот.
    callback(earnings.admin_process_payout, F.data.startswith("admin_owner_payout_"))
