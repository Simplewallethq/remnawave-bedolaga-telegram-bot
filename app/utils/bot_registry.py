from pathlib import Path

PAGE_IMAGES_DIR = Path("images")

_registry: dict[int, Path] = {}


def register_bot(bot_id: int, logo_path: Path) -> None:
    _registry[bot_id] = logo_path


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


def is_primary_bot(bot_id: int | None) -> bool:
    """Return whether the bot ID belongs to the configured primary bot."""
    if bot_id is None:
        return False

    from app.config import settings

    try:
        primary_bot_id = int(settings.BOT_TOKEN.split(":", 1)[0])
    except (AttributeError, TypeError, ValueError):
        return False
    return bot_id == primary_bot_id


def clear() -> None:
    _registry.clear()


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
