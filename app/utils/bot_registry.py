from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from app.branding.profile import BrandProfile

PAGE_IMAGES_DIR = Path("images")

_registry: dict[int, Path] = {}
# Живые экземпляры aiogram.Bot по id — чтобы фоновые сервисы могли писать
# пользователю из того бота, в котором он зарегистрирован (user.bot_id).
_instances: dict[int, object] = {}
# Запись витрины из БД (copycat_bots + группа). Профиль бренда строится из неё при
# каждом обращении: фолбэки берутся из настроек основного бота, а те
# правятся админкой на лету.
_brand_configs: dict[int, Mapping[str, Any]] = {}
# Веса платёжного роутера витрины (своё или от группы) и откуда они взяты:
# "bot" | "group:<name>". Нет записи — глобальные веса.
_payment_weights: dict[int, tuple[dict[str, int], str]] = {}
# Ценовая схема витрины (своя или от группы). Нет записи — глобальные цены.
_price_schemes: dict[int, int] = {}
# Своя касса AuraPay витрины (имя из AURAPAY_EXTRA_SHOPS). Нет записи — основная.
_aurapay_shops: dict[int, str] = {}
# Боты владельцев: старые общие аккаунты сюда не пускаются (см. telegram_account_clause).
_isolated_accounts: set[int] = set()


def register_bot(
    bot_id: int,
    logo_path: Path,
    bot: object | None = None,
    *,
    brand_config: Mapping[str, Any] | None = None,
) -> None:
    _registry[bot_id] = logo_path
    if bot is not None:
        _instances[bot_id] = bot
    if brand_config is not None:
        _brand_configs[bot_id] = dict(brand_config)
    else:
        _brand_configs.pop(bot_id, None)


def unregister_instance(bot_id: int) -> object | None:
    """Убирает живой экземпляр бота; бренд остаётся.

    Выключенная витрина больше не отвечает, но её пользователи по-прежнему
    считаются её аудиторией: рассылки основного бренда их не должны задеть.
    """
    return _instances.pop(bot_id, None)


def forget_bot(bot_id: int) -> None:
    """Витрина удалена из БД: бот забывается целиком."""
    _registry.pop(bot_id, None)
    _instances.pop(bot_id, None)
    _brand_configs.pop(bot_id, None)
    _payment_weights.pop(bot_id, None)
    _price_schemes.pop(bot_id, None)
    _aurapay_shops.pop(bot_id, None)
    _isolated_accounts.discard(bot_id)


def set_payment_weights(
    bot_id: int, weights: Mapping[str, int] | None, source: str = "bot"
) -> None:
    if weights is None:
        _payment_weights.pop(bot_id, None)
    else:
        _payment_weights[bot_id] = (dict(weights), source)


def get_payment_weights(bot_id: int | None) -> tuple[dict[str, int], str] | None:
    """Свои веса роутера для бота или None — тогда глобальные."""
    if bot_id is None:
        return None
    entry = _payment_weights.get(bot_id)
    if entry is None:
        return None
    return dict(entry[0]), entry[1]


def payments_disabled(bot_id: int | None) -> bool:
    """Все свои веса бота нулевые — в этом боте оплата не принимается вовсе.

    Без этого нулевые веса лишь убирают кнопку «Оплатить», и бот откатывается
    на прямые кнопки шлюзов.
    """
    entry = get_payment_weights(bot_id)
    return entry is not None and all(weight <= 0 for weight in entry[0].values())


def set_price_scheme(bot_id: int, scheme_id: int | None) -> None:
    if scheme_id is None:
        _price_schemes.pop(bot_id, None)
    else:
        _price_schemes[bot_id] = int(scheme_id)


def set_aurapay_shop(bot_id: int, shop: str | None) -> None:
    if shop:
        _aurapay_shops[bot_id] = shop
    else:
        _aurapay_shops.pop(bot_id, None)


def get_aurapay_shop(bot_id: int | None) -> str | None:
    """Касса AuraPay, в которую бот выставляет счета; None — основная."""
    if bot_id is None:
        return None
    return _aurapay_shops.get(bot_id)


def set_isolated_accounts(bot_id: int, isolated: bool) -> None:
    if isolated:
        _isolated_accounts.add(bot_id)
    else:
        _isolated_accounts.discard(bot_id)


def has_isolated_accounts(bot_id: int | None) -> bool:
    """Бот видит только свои аккаунты: общий аккаунт основного бота в нём не находится."""
    return bot_id is not None and bot_id in _isolated_accounts


def get_price_scheme_id(bot_id: int | None) -> int | None:
    if bot_id is None:
        return None
    return _price_schemes.get(bot_id)


def get_brand_for_bot(bot_id: int | None) -> "BrandProfile":
    """Бренд бота: основной — из настроек, зеркало/копикет — из его записи."""
    from app.branding.profile import mirror_profile_from_config, primary_profile

    if bot_id is None or is_primary_bot(bot_id):
        return primary_profile()
    config = _brand_configs.get(bot_id)
    if config is None:
        if bot_id in _registry:
            # Зеркало без записи бренда (тесты, legacy): Leto со своей картинкой.
            return mirror_profile_from_config(bot_id, {"logo": str(_registry[bot_id])})
        return primary_profile()
    return mirror_profile_from_config(bot_id, config)


def brand_for_user(user: object) -> "BrandProfile":
    return get_brand_for_bot(getattr(user, "bot_id", None))


def copycat_bot_ids() -> frozenset[int]:
    """Боты-копикеты. Основной бот попадает сюда, если весь процесс переименован (v1 через .env)."""
    from app.branding.profile import is_copycat_config, primary_profile

    ids = {bot_id for bot_id, config in _brand_configs.items() if is_copycat_config(config)}
    if primary_profile().is_copycat:
        primary_id = _primary_bot_id()
        if primary_id is not None:
            ids.add(primary_id)
    return frozenset(ids)


def is_copycat_bot(bot_id: int | None) -> bool:
    if bot_id is None:
        return False
    return get_brand_for_bot(bot_id).is_copycat


def is_copycat_user(user: object) -> bool:
    return is_copycat_bot(getattr(user, "bot_id", None))


def get_bot_instance(bot_id: int | None) -> object | None:
    if bot_id is None:
        return None
    return _instances.get(bot_id)


def bot_for_user(user: object, default: object) -> object:
    """Бот, в котором пользователь зарегистрирован, иначе `default`.

    Основной бот не может писать тому, кто стартовал только зеркало:
    Telegram отвечает «chat not found».
    """
    return get_bot_instance(getattr(user, "bot_id", None)) or default


def get_logo_for_bot(bot_id: int | None) -> Path:
    from app.config import settings
    default = Path(settings.LOGO_FILE)
    if bot_id is None:
        return default
    return _registry.get(bot_id, default)


def get_primary_logo() -> Path:
    from app.config import settings
    if not _registry:
        return Path(settings.LOGO_FILE)
    return next(iter(_registry.values()))


def _primary_bot_id() -> int | None:
    from app.config import settings

    try:
        return int(settings.BOT_TOKEN.split(":", 1)[0])
    except (AttributeError, TypeError, ValueError):
        return None


def is_primary_bot(bot_id: int | None) -> bool:
    """Return whether the bot ID belongs to the configured primary bot."""
    if bot_id is None:
        return False
    primary_bot_id = _primary_bot_id()
    return primary_bot_id is not None and bot_id == primary_bot_id


def account_scope_for_bot(bot_id: int | None) -> int | None:
    """Скоуп аккаунта, который заводит новый пользователь в этом боте.

    Копикет — свой аккаунт на каждого бота. Основной бот и копии Leto делят
    один аккаунт: скоуп — id основного бота. Вне апдейта (кабинет, web API)
    бота нет, и это тоже скоуп основного.
    """
    if bot_id is not None and is_copycat_bot(bot_id):
        return bot_id
    return _primary_bot_id()


def clear() -> None:
    _registry.clear()
    _instances.clear()
    _brand_configs.clear()
    _payment_weights.clear()
    _price_schemes.clear()
    _aurapay_shops.clear()
    _isolated_accounts.clear()


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.resolve() == second.resolve()
    except OSError:
        return str(first) == str(second)


def get_mirror_logo(bot_id: int | None) -> Path | None:
    """Return the mirror bot's own artwork, or None when it has none."""
    if bot_id is None or is_primary_bot(bot_id):
        return None

    from app.config import settings

    logo = _registry.get(bot_id)
    if logo is None:
        return None
    if _same_file(logo, Path(settings.LOGO_FILE)):
        return None
    if not logo.exists():
        return None
    return logo


def _is_shared_artwork(path: Path) -> bool:
    """Shared artwork is the primary logo or a page image from images/."""
    from app.config import settings

    if _same_file(path, Path(settings.LOGO_FILE)):
        return True
    try:
        path.resolve().relative_to(PAGE_IMAGES_DIR.resolve())
    except (ValueError, OSError):
        return False
    return True


def resolve_photo_for_bot(bot_id: int | None, photo_path: "str | Path") -> Path:
    """Swap shared page artwork for the mirror bot's own picture.

    Mirrors without their own picture keep the shared one.
    """
    path = Path(photo_path)
    mirror_logo = get_mirror_logo(bot_id)
    if mirror_logo is None or not _is_shared_artwork(path):
        return path
    return mirror_logo
