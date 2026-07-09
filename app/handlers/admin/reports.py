import logging
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.admin import (
    get_admin_report_result_keyboard,
    get_admin_reports_keyboard,
)
from app.localization.texts import get_texts
from app.services.reporting_service import (
    ReportPeriod,
    ReportingServiceError,
    reporting_service,
)
from app.utils.decorators import admin_required, error_handler


logger = logging.getLogger(__name__)


@admin_required
@error_handler
async def show_reports_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    await callback.message.edit_text(
        "📊 <b>Отчеты</b>\n\n"
        "Выберите период, чтобы отправить отчет в админский топик.",
        reply_markup=get_admin_reports_keyboard(db_user.language),
        parse_mode="HTML",
    )
    await callback.answer()


@admin_required
@error_handler
async def send_daily_report(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    await _send_report(callback, ReportPeriod.DAILY, db_user.language)


@admin_required
@error_handler
async def send_weekly_report(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    await _send_report(callback, ReportPeriod.WEEKLY, db_user.language)


@admin_required
@error_handler
async def send_monthly_report(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    await _send_report(callback, ReportPeriod.MONTHLY, db_user.language)


async def _send_report(
    callback: types.CallbackQuery,
    period: ReportPeriod,
    language: str,
) -> None:
    try:
        report_text = await reporting_service.send_report(period, send_to_topic=True)
    except ReportingServiceError as exc:
        logger.warning("Не удалось отправить отчет: %s", exc)
        await callback.answer(str(exc), show_alert=True)
        return
    except Exception as exc:  # noqa: BLE001
        logger.error("Непредвиденная ошибка при отправке отчета: %s", exc)
        await callback.answer("Не удалось отправить отчет. Попробуйте позже.", show_alert=True)
        return

    await callback.message.answer(
        report_text,
        reply_markup=get_admin_report_result_keyboard(language),
    )
    await callback.answer("Отчет отправлен в топик")


@admin_required
@error_handler
async def send_registration_report(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    await callback.answer("🔄 Генерация отчета запущена...")

    progress_msg = await callback.message.edit_text(
        "⏳ <b>Готовим отчет по регистрациям за 30 дней...</b>\n\n"
        "Проверяем подключения в RemnaWave — это может занять до минуты.",
        parse_mode="HTML",
    )

    try:
        report = await reporting_service.build_registration_cohort_report(days=30)
    except Exception as exc:  # noqa: BLE001
        logger.error("Ошибка генерации отчета по регистрациям: %s", exc)
        await progress_msg.edit_text(
            "❌ Не удалось сформировать отчет. Попробуйте позже.",
            reply_markup=get_admin_reports_keyboard(db_user.language),
        )
        return

    csv_text = reporting_service.render_registration_report_csv(report)
    filename = f"registrations-{report.generated_at_msk.strftime('%Y%m%d-%H%M%S')}.csv"
    document = types.BufferedInputFile(
        csv_text.encode("utf-8-sig"),
        filename=filename,
    )

    await callback.message.answer_document(
        document=document,
        caption=reporting_service.render_registration_report_summary(report),
        parse_mode="HTML",
    )

    try:
        await progress_msg.edit_text(
            "📊 <b>Отчеты</b>\n\n"
            "Выберите период, чтобы отправить отчет в админский топик.",
            reply_markup=get_admin_reports_keyboard(db_user.language),
            parse_mode="HTML",
        )
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("Не удалось восстановить меню отчетов: %s", exc)


@admin_required
@error_handler
async def close_report_message(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
) -> None:
    texts = get_texts(db_user.language)

    try:
        await callback.message.delete()
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.warning("Не удалось закрыть сообщение отчета: %s", exc)
        await callback.answer(texts.t("REPORT_CLOSE_ERROR", "Не удалось закрыть отчет."), show_alert=True)
        return

    await callback.answer(texts.t("REPORT_CLOSED", "Отчет закрыт."))


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_reports_menu, F.data == "admin_reports")
    dp.callback_query.register(send_daily_report, F.data == "admin_reports_daily")
    dp.callback_query.register(send_weekly_report, F.data == "admin_reports_weekly")
    dp.callback_query.register(send_monthly_report, F.data == "admin_reports_monthly")
    dp.callback_query.register(send_registration_report, F.data == "admin_reports_registrations")
    dp.callback_query.register(close_report_message, F.data == "admin_close_report")

