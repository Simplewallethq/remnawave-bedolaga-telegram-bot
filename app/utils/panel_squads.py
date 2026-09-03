"""Сборка списка внутренних сквадов панели для пользователя.

Панель RemnaWave показывает хост пользователю, только если инбаунд хоста есть хотя бы
в одном из его сквадов. Чтобы часть хостов видели только платные, у каждой не-триальной
подписки в панели должен быть дополнительный сквад (``PAID_INTERNAL_SQUAD_UUID``).

Сквад намеренно **не хранится** в ``Subscription.connected_squads``: тот список — это
выбранные пользователем серверы (каталог, цены, миграции). Платный сквад добавляется
здесь, в момент отправки в панель, и вырезается при импорте из панели.
"""

from typing import Iterable, List, Optional

from app.config import settings


def get_paid_squad_uuid() -> Optional[str]:
    return settings.get_paid_internal_squad_uuid()


def is_paid_squad_member(subscription) -> bool:
    """Платный сквад получает любая не-триальная подписка: партнёрская, истёкшая — тоже.

    У истёкшей в панели статус EXPIRED, доступ закрыт независимо от сквадов, зато
    после продления любой путь кода отправит её в панель уже со сквадом.
    """
    return not bool(getattr(subscription, "is_trial", False))


def build_panel_squads(subscription, squads: Optional[Iterable[str]] = None) -> List[str]:
    """Список сквадов для ``activeInternalSquads``.

    База — ``squads`` (если передан) или ``subscription.connected_squads``. Платный сквад
    убирается из базы и добавляется в конец, если подписка не триальная — так он не
    дублируется, а у триала не остаётся от прошлых правок в панели.
    """
    base = list(squads if squads is not None else (getattr(subscription, "connected_squads", None) or []))
    paid_uuid = get_paid_squad_uuid()
    if not paid_uuid:
        return base

    result = [uuid for uuid in base if uuid != paid_uuid]
    if is_paid_squad_member(subscription):
        result.append(paid_uuid)
    return result


def strip_paid_squad(squads: Iterable[str]) -> List[str]:
    """Убирает платный сквад из списка, пришедшего из панели, перед записью в ``connected_squads``."""
    paid_uuid = get_paid_squad_uuid()
    if not paid_uuid:
        return list(squads)
    return [uuid for uuid in squads if uuid != paid_uuid]
