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
# Запись зеркала из mirror_bots.yaml. Профиль бренда строится из неё при
# каждом обращении: фолбэки берутся из настроек основного бота, а те
# правятся админкой на лету.
_brand_configs: dict[int, Mapping[str, Any]] = {}


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


def clear() -> None:
    _registry.clear()
    _instances.clear()
    _brand_configs.clear()


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
