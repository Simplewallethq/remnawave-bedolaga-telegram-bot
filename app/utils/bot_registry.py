from pathlib import Path

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
