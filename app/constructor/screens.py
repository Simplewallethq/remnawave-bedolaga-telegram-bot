"""Экраны бота-конструктора: быстрый старт и мастер создания бота.

Всё управление ботами (цены, статистика, кошелёк, вывод) — в мини-аппе;
бот ведёт туда кнопкой «Открыть приложение».

Маршрут (route) — строка экрана: "main", "how", "wiz:tok", "wiz:sup",
"wiz:img", "done:<id>". Текущий маршрут хранится в сессии, поэтому любой ввод
обрабатывает экран, который пользователь видит.

callback_data:  "g:<route>"            — перейти на экран
                "a:<action>[:<arg>]"   — действие (возвращает маршрут)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import escape
from io import BytesIO
from typing import Awaitable, Callable, Optional

from aiogram import Bot
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import owner_platform_service as platform

from . import service
from .emoji import e
from .ui import PRIMARY, SUCCESS, UI, Btn, Screen

logger = logging.getLogger(__name__)

STEPS = ("tok", "sup", "img")
TITLES = {"tok": "Создание бота", "sup": "Поддержка", "img": "Обложка бота"}
WEBHOOK_WARNING = "Этот бот уже подключён к другому сервису. Если продолжить, там он перестанет работать."
IMAGE_ERROR = "Отправьте картинку — фото или файлом (JPG, PNG)"
MAX_IMAGE = 20 * 1024 * 1024  # больше Bot API скачать не даст


class InputError(Exception):
    """Неверный ввод: экран перерисовывается с текстом ошибки."""


class Redirect(Exception):
    """Экран нельзя показать — показать другой (с ошибкой)."""

    def __init__(self, route: str, error: Optional[str] = None):
        super().__init__(route)
        self.route, self.error = route, error


@dataclass
class Ctx:
    bot: Bot
    ui: UI
    db: AsyncSession
    tg: service.TgUser
    session: dict
    match: Optional[re.Match] = None
    error: Optional[str] = None
    notice: Optional[str] = None
    toast: Optional[str] = None

    @property
    def uid(self) -> int:
        return self.tg.id

    @property
    def draft(self) -> Optional[dict]:
        return self.session.get("draft")

    def alert(self) -> str:
        if self.error:
            return f"<blockquote>{e('warn')} <b>{escape(self.error)}</b></blockquote>"
        if self.notice:
            return f"<blockquote>{e('check')} {self.notice}</blockquote>"
        return ""


Build = Callable[[Ctx], Awaitable[Screen]]
OnInput = Callable[[Ctx, Message], Awaitable[str]]
Action = Callable[[Ctx, str], Awaitable[str]]

_pages: dict[str, tuple[re.Pattern, Build]] = {}
_inputs: dict[str, OnInput] = {}
_actions: dict[str, Action] = {}


def page(pattern: str):
    def deco(fn: Build) -> Build:
        _pages[pattern] = (re.compile(pattern + r"\Z"), fn)
        return fn

    return deco


def on_input(pattern: str):
    """Обработчик ввода на экране с тем же шаблоном маршрута."""

    def deco(fn: OnInput) -> OnInput:
        _inputs[pattern] = fn
        return fn

    return deco


def action(name: str):
    def deco(fn: Action) -> Action:
        _actions[name] = fn
        return fn

    return deco


def resolve(route: str) -> Optional[tuple[Build, Optional[OnInput], re.Match]]:
    for pattern, (compiled, build) in _pages.items():
        if match := compiled.match(route):
            return build, _inputs.get(pattern), match
    return None


def get_action(name: str) -> Optional[Action]:
    return _actions.get(name)


# ---------------------------------------------------------------- общее


def app_url(row_id: Optional[int] = None) -> str:
    url = (settings.CONSTRUCTOR_WEBAPP_URL or "").strip()
    if url and row_id is not None:
        url += ("&" if "?" in url else "?") + f"bot={row_id}"
    return url


def app_btn(text: str = "Открыть приложение", row_id: Optional[int] = None, style: str = SUCCESS) -> Optional[Btn]:
    url = app_url(row_id)
    return Btn(text, web_app=url, style=style, icon="rocket") if url else None


def example_btn(text: str = "Пример рабочего VPN-сервиса") -> Optional[Btn]:
    url = (settings.CONSTRUCTOR_EXAMPLE_BOT_URL or "").strip()
    return Btn(text, url=url, style=PRIMARY, icon="eyes") if url else None


def money(kopeks: int) -> str:
    return f"{int(kopeks) // 100:,}".replace(",", " ") + " ₽"


def _progress(step: int) -> str:
    return "▰" * step + "▱" * (len(STEPS) - step)


def _head(step: str) -> str:
    n = STEPS.index(step) + 1
    return f"<h3>{TITLES[step]}</h3><p><code>{_progress(n)}</code>  Шаг {n} из {len(STEPS)}</p>"


def _text(message: Message) -> str:
    if not message.text:
        raise InputError("Отправьте ответ текстом")
    return message.text.strip()


def _require_draft(c: Ctx, step: str) -> dict:
    """Шаг мастера нельзя открыть, пока не пройдены предыдущие."""
    draft = c.draft
    if draft is None:
        raise Redirect("main")
    if step in ("sup", "img") and not draft.get("token"):
        raise Redirect("wiz:tok", "Сначала подключите бота токеном")
    if step == "img" and not draft.get("support"):
        raise Redirect("wiz:sup", "Укажите аккаунт поддержки")
    return draft


# ---------------------------------------------------------------- главная


@page("main")
async def main_screen(c: Ctx) -> Screen:
    user = await service.find_owner_account(c.db, c.uid)
    bots = await platform.list_owner_bots(c.db, user.id, enabled_only=True) if user else []
    owner = await platform.get_owner(c.db, user.id) if user else None
    share = platform.revshare_percent(owner)

    if not bots:
        return Screen(
            f"<h2>{e('shield')} VPN Constructor</h2>" + c.alert()
            + "<p>Запустите собственный VPN-сервис в Telegram за 5 минут — "
            "<b>бесплатно</b> и без программирования.</p>"
            "<ul>"
            "<li>Серверы, протоколы и ключи — на нас</li>"
            "<li>Приём оплат и выдача доступа — автоматически</li>"
            f"<li>{share}% с каждой оплаты — ваши</li>"
            "</ul>"
            + ("<footer>В приложении — создание, цены, статистика и вывод заработка</footer>" if app_url() else ""),
            [
                [app_btn()],
                [Btn("Создать бота в чате" if app_url() else "Создать свой VPN", "a:new",
                     style=None if app_url() else SUCCESS, icon="robot")],
                [Btn("Как это работает?", "g:how", icon="question")],
                [example_btn()],
            ],
        )

    stats = await platform.owner_stats(c.db, user.id)
    earnings = await platform.owner_earnings(c.db, owner) if owner else None
    available = earnings.available_kopeks if earnings else 0
    return Screen(
        f"<h2>{e('shield')} VPN Constructor</h2>" + c.alert()
        + "<p>С возвращением! Ваши VPN-сервисы работают.</p>"
        "<table bordered compact>"
        f"<tr><td>Ботов</td><td align=\"right\"><b>{len(bots)}</b></td></tr>"
        f"<tr><td>Пользователей</td><td align=\"right\"><b>{stats.users_total}</b></td></tr>"
        f"<tr><td>Доступно к выводу</td><td align=\"right\"><b>{money(available)}</b></td></tr>"
        "</table>"
        "<footer>Цены, статистика, кошелёк и вывод — в приложении</footer>",
        [
            [app_btn()],
            [Btn("Создать ещё бота", "a:new", icon="plus")],
            [Btn("Как это работает?", "g:how", icon="question")],
        ],
    )


@action("new")
async def new_bot(c: Ctx, _: str) -> str:
    user = await service.find_owner_account(c.db, c.uid)
    if user is not None:
        active = await platform.list_owner_bots(c.db, user.id, enabled_only=True)
        if len(active) >= int(settings.OWNER_MAX_BOTS):
            c.toast = service.ConnectError("limit").message
            return "main"
    c.session["draft"] = {}
    return "wiz:tok"


@page("how")
async def how_screen(c: Ctx) -> Screen:
    user = await service.find_owner_account(c.db, c.uid)
    owner = await platform.get_owner(c.db, user.id) if user else None
    share = platform.revshare_percent(owner)
    return Screen(
        f"<h2>{e('question')} Как это работает</h2>"
        "<ol>"
        "<li>Создаёте бота в @BotFather и присылаете нам токен</li>"
        "<li>Указываете поддержку и обложку — цены уже настроены</li>"
        "<li>Клиенты покупают VPN прямо в вашем боте</li>"
        "<li>Вы выводите доход в USDT (TRC-20)</li>"
        "</ol>"
        "<details><summary>Что если VPN заблокируют?</summary>"
        "<p>Серверы и протоколы обновляем мы — ваш бот и клиенты продолжают работать без ваших действий.</p>"
        "</details>"
        "<details><summary>Сколько я зарабатываю?</summary>"
        f"<p>Вы получаете <b>{share}%</b> с каждого пополнения ваших клиентов. "
        "Цены по умолчанию уже настроены, изменить их можно в приложении.</p></details>"
        "<details><summary>Как вывести деньги?</summary>"
        "<p>Укажите кошелёк USDT TRC-20 в приложении и создайте заявку. Выводятся продажи старше "
        f"{settings.OWNER_PAYOUT_HOLD_DAYS} дней, комиссия сети — {platform.payout_fee().normalize()} USDT.</p>"
        "</details>"
        "<details><summary>Это правда бесплатно?</summary>"
        "<p>Да. Создание и обслуживание бота — бесплатно, мы зарабатываем на доле с продаж.</p></details>",
        [
            [app_btn()],
            [Btn("Создать бота в чате", "a:new", icon="robot")],
            [example_btn("Пример"), Btn("Назад", "g:main")],
        ],
        cover="how",
    )


# ---------------------------------------------------------------- шаг 1: токен

_HOW_TO = (
    "<details><summary>Как это сделать</summary><ol>"
    "<li>Откройте @BotFather и отправьте <code>/newbot</code></li>"
    "<li>Придумайте название, например <i>Flash VPN</i></li>"
    "<li>Придумайте username, оканчивающийся на <code>bot</code></li>"
    "<li>Перешлите сюда ответ BotFather с токеном</li>"
    "</ol></details>"
)


def _mask_token(token: str) -> str:
    bot_id, _, secret = token.partition(":")
    return f"<code>{bot_id}:{secret[:4]}•••{secret[-3:]}</code>"


@page("wiz:tok")
async def token_screen(c: Ctx) -> Screen:
    draft = _require_draft(c, "tok")
    if draft.get("pending"):
        return Screen(
            _head("tok")
            + f"<blockquote>{e('warn')} <b>{WEBHOOK_WARNING}</b></blockquote>"
            f"<p>Бот <b>@{escape(draft['username'])}</b> · {_mask_token(draft['token'])}</p>",
            [[Btn("Подключить", "a:tokok", style=SUCCESS), Btn("Отмена", "a:tokno")]],
            cover="create",
        )
    current = (
        f"<blockquote>{e('check')} Подключён <b>@{escape(draft['username'])}</b>. "
        "Чтобы сменить бота — пришлите другой токен</blockquote>"
        if draft.get("token") else ""
    )
    rows = [[Btn("Открыть @BotFather", url="https://t.me/BotFather", style=SUCCESS, icon="robot")]]
    if draft.get("token"):
        rows.append([Btn(f"Продолжить с @{draft['username']}", "g:wiz:sup", style=PRIMARY)])
    rows.append([Btn("Отмена", "a:cancel")])
    return Screen(
        _head("tok") + c.alert()
        + "<p>Создайте бота в @BotFather командой <code>/newbot</code> и перешлите сюда его ответ с токеном.</p>"
        + _HOW_TO + current,
        rows,
        cover="create",
    )


@on_input("wiz:tok")
async def token_input(c: Ctx, message: Message) -> str:
    draft = _require_draft(c, "tok")
    token = service.find_token(message.text or message.caption)
    if not token:
        raise InputError(service.ERRORS["invalid"])
    user = await service.find_owner_account(c.db, c.uid)
    try:
        info = await service.check_token(c.db, user.id if user else None, token)
    except service.ConnectError as error:
        raise InputError(error.message) from None

    draft.clear()
    draft.update(token=info.token, bot_id=info.bot_id, username=info.username, title=info.title)
    if info.restore_row_id is not None:
        # Свой выключенный бот: возвращаем сразу, со всеми прежними настройками.
        return await _launch(c)
    if info.foreign_webhook:
        draft["pending"] = True
        return "wiz:tok"
    return "wiz:sup"


@action("tokok")
async def token_confirm(c: Ctx, _: str) -> str:
    if c.draft and c.draft.get("pending"):
        c.draft.pop("pending")
        return "wiz:sup"
    return "wiz:tok"


@action("tokno")
async def token_cancel(c: Ctx, _: str) -> str:
    if c.draft is not None:
        c.draft.clear()
    return "wiz:tok"


@action("cancel")
async def cancel(c: Ctx, _: str) -> str:
    c.session["draft"] = None
    return "main"


# ---------------------------------------------------------------- шаг 2: поддержка


@page("wiz:sup")
async def support_screen(c: Ctx) -> Screen:
    draft = _require_draft(c, "sup")
    own = c.tg.username
    rows = []
    if own and draft.get("support") != own:
        rows.append([Btn(f"Мой аккаунт @{own}", "a:supme", style=PRIMARY, icon="robot")])
    if draft.get("support"):
        rows.append([Btn(f"Далее · @{draft['support']}", "g:wiz:img", style=SUCCESS)])
    rows.append([Btn("Назад", "g:wiz:tok")])
    return Screen(
        _head("sup")
        + (c.alert() or f"<blockquote>{e('check')} Бот <b>@{escape(draft['username'])}</b> подключён</blockquote>")
        + "<p>Куда клиентам писать с вопросами? Пришлите username, например "
        "<code>@flashvpn_support</code> — на него будет вести кнопка «Поддержка».</p>",
        rows,
        cover="create",
    )


@on_input("wiz:sup")
async def support_input(c: Ctx, message: Message) -> str:
    draft = _require_draft(c, "sup")
    support = platform.normalize_support_username(_text(message))
    if support is None:
        raise InputError("Нужен username вида @support_name: латиница, цифры и _ (5–32 символа)")
    draft["support"] = support
    return "wiz:img"


@action("supme")
async def support_me(c: Ctx, _: str) -> str:
    draft = _require_draft(c, "sup")
    support = platform.normalize_support_username(c.tg.username or "")
    if support is None:
        c.toast = "У вашего аккаунта нет подходящего username — пришлите username поддержки"
        return "wiz:sup"
    draft["support"] = support
    return "wiz:img"


# ---------------------------------------------------------------- шаг 3: обложка и запуск


@page("wiz:img")
async def image_screen(c: Ctx) -> Screen:
    draft = _require_draft(c, "img")
    custom = bool(draft.get("logo"))
    rows = []
    if custom:
        rows.append([Btn("Вернуть стандартную", "a:img0")])
    rows.append([
        Btn("Назад", "g:wiz:sup"),
        Btn("Запустить с этой обложкой" if custom else "Оставить стандартную", "a:launch", style=SUCCESS, icon="rocket"),
    ])
    return Screen(
        _head("img") + c.alert()
        + "<p>Отправьте картинку — она будет в сообщениях вашего бота.</p>"
        f"<p>Выше — как она выглядит сейчас: <b>{'ваша обложка' if custom else 'стандартная'}</b>.</p>"
        "<blockquote>Лучше всего горизонтальная картинка 1280×640 с логотипом сервиса</blockquote>",
        rows,
        cover=f"file:{draft['logo']}" if custom else f"file:{service.DEFAULT_COVER}",
    )


async def _picture_bytes(c: Ctx, message: Message) -> bytes:
    if message.photo:
        file = message.photo[-1]
    elif message.document and (
        (message.document.mime_type or "").startswith("image/")
        or re.search(r"\.(jpe?g|png|webp)$", message.document.file_name or "", re.I)
    ):
        file = message.document
        if (file.file_size or 0) > MAX_IMAGE:
            raise InputError("Картинка больше 20 МБ — отправьте поменьше")
    else:
        raise InputError(IMAGE_ERROR)
    buffer = BytesIO()
    try:
        await c.bot.download(file, destination=buffer)
    except Exception as error:  # noqa: BLE001
        logger.warning("Конструктор: картинку не скачать: %s", error)
        raise InputError("Telegram не отдал картинку. Попробуйте ещё раз") from None
    return buffer.getvalue()


@on_input("wiz:img")
async def image_input(c: Ctx, message: Message) -> str:
    draft = _require_draft(c, "img")
    data = await _picture_bytes(c, message)
    try:
        draft["logo"] = platform.save_picture_bytes(data, int(draft["bot_id"]))
    except ValueError:
        raise InputError("Эту картинку Telegram не покажет — пришлите JPG или PNG обычных пропорций") from None
    return await _launch(c)  # картинка — последний шаг: сразу запуск


@action("img0")
async def image_reset(c: Ctx, _: str) -> str:
    draft = _require_draft(c, "img")
    draft.pop("logo", None)
    c.toast = "Стандартная обложка"
    return "wiz:img"


@action("launch")
async def launch(c: Ctx, _: str) -> str:
    _require_draft(c, "img")
    return await _launch(c)


async def _launch(c: Ctx) -> str:
    draft = c.draft or {}
    try:
        user = await service.ensure_owner_account(c.db, c.tg)
        user_id = user.id
        # Токен проверяем ещё раз: пока шёл мастер, бота могли подключить.
        info = await service.check_token(c.db, user_id, draft["token"])
        row, restored = await service.launch_bot(
            c.db, user, info, support=draft.get("support"), logo=draft.get("logo")
        )
    except service.ConnectError as error:
        c.session["draft"] = {}
        raise Redirect("wiz:tok", error.message) from None
    c.session["draft"] = None
    c.session["restored"] = restored
    return f"done:{row.id}"


# ---------------------------------------------------------------- готово


@page(r"done:(?P<id>\d+)")
async def done_screen(c: Ctx) -> Screen:
    user = await service.find_owner_account(c.db, c.uid)
    row = await platform.get_owner_bot(c.db, user.id, int(c.match["id"])) if user else None
    if row is None or not row.enabled:
        raise Redirect("main")
    link = service.bot_link(row)
    if c.session.get("restored"):
        text = (
            f"<p>Ваш бот <b>@{escape(row.username)}</b> снова подключён. "
            "Подписчики и настройки сохранились.</p>"
        )
    else:
        text = (
            f"<p><b>@{escape(row.username)}</b> уже принимает клиентов. "
            "Отправьте ссылку друзьям или разместите в своём канале.</p>"
            f"<p><code>{escape(link)}</code></p>"
        )
    return Screen(
        f"<h2>{e('party')} Бот запущен!</h2>" + text
        + "<footer>Цены стоят стандартные. Цены, статистика, кошелёк и вывод — в приложении.</footer>",
        [
            [Btn(f"Открыть @{row.username}", url=link, style=SUCCESS, icon="link")],
            [app_btn("Управлять в приложении", row.id, style=PRIMARY)],
            [Btn("Главное меню", "g:main")],
        ],
        cover="success",
    )
