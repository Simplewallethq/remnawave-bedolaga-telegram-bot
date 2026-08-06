from __future__ import annotations

from urllib.parse import urlsplit

# Сегмент пути, после которого панель remnawave ставит short uuid подписки:
# https://letovpn.com/sub/-BgpfZQ062Td9Fpk
_SUB_PATH_MARKER = "sub"


def extract_subscription_code(raw: str) -> tuple[str, bool]:
    """Приводит введённую строку к коду подписки (short uuid).

    Возвращает (код, была_ли_ссылка). Пользователь может вставить как сам код,
    так и полную ссылку на подписку — с завершающим слешем, ?query, #fragment
    или суффиксом клиента (/sub/<code>/happ, /sub/<code>/json).

    Никогда не бросает исключений: на мусоре возвращает ("", True) для ссылки
    без пригодного сегмента или (строку, False) для не-ссылки.
    """
    value = (raw or "").strip()
    if not value:
        return "", False

    if "://" not in value and "/" not in value:
        return value, False

    # urlsplit сам отрезает ?query и #fragment; для строки без схемы
    # ('letovpn.com/sub/XXXX') всё уезжает в path — это нас устраивает,
    # потому что нужен только последний осмысленный сегмент.
    segments = [segment for segment in urlsplit(value).path.split("/") if segment]
    if not segments:
        return "", True

    if _SUB_PATH_MARKER in segments:
        index = segments.index(_SUB_PATH_MARKER) + 1
        if index < len(segments):
            return segments[index], True
        return "", True

    return segments[-1], True
