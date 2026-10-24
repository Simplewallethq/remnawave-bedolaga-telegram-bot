"""Сборка списка внутренних сквадов панели для пользователя.

Панель RemnaWave показывает хост пользователю, только если инбаунд хоста есть хотя бы
в одном из его сквадов. Поверх выбранных серверов бот добавляет два сквада:

* ``PAID_INTERNAL_SQUAD_UUID`` — каждой не-триальной подписке, чтобы часть хостов
  видели только платные;
* ``PRIMARY_BRAND_INTERNAL_SQUAD_UUID`` — всем пользователям основного бренда (Leto и
  его копии), но не копикетам.

Эти сквады намеренно **не хранятся** в ``Subscription.connected_squads``: тот список —
это выбранные пользователем серверы (каталог, цены, миграции). Они добавляются здесь,
в момент отправки в панель, и вырезаются при импорте из панели.
"""

from typing import Iterable, List, Optional

from app.config import settings
from app.utils.bot_registry import is_copycat_user


def get_paid_squad_uuid() -> Optional[str]:
    return settings.get_paid_internal_squad_uuid()


def get_primary_brand_squad_uuid() -> Optional[str]:
    return settings.get_primary_brand_internal_squad_uuid()


def is_paid_squad_member(subscription) -> bool:
    """Платный сквад получает любая не-триальная подписка: партнёрская, истёкшая — тоже.

    У истёкшей в панели статус EXPIRED, доступ закрыт независимо от сквадов, зато
    после продления любой путь кода отправит её в панель уже со сквадом.
    """
    return not bool(getattr(subscription, "is_trial", False))


def is_primary_brand_squad_member(user) -> bool:
    """Сквад основного бренда — всем, кроме пользователей копикетов (по ``users.bot_id``)."""
    return not is_copycat_user(user)


def build_panel_squads(subscription, squads: Optional[Iterable[str]] = None, *, user) -> List[str]:
    """Список сквадов для ``activeInternalSquads``.

    База — ``squads`` (если передан) или ``subscription.connected_squads``. Сквады бота
    убираются из базы и добавляются в конец тем, кому положены — так они не
    дублируются, а у триала или копикета не остаются от прошлых правок в панели.
    """
    base = list(squads if squads is not None else (getattr(subscription, "connected_squads", None) or []))
    paid_uuid = get_paid_squad_uuid()
    brand_uuid = get_primary_brand_squad_uuid()

    managed = {uuid for uuid in (paid_uuid, brand_uuid) if uuid}
    result = [uuid for uuid in base if uuid not in managed]
    if paid_uuid and is_paid_squad_member(subscription):
        result.append(paid_uuid)
    if brand_uuid and brand_uuid != paid_uuid and is_primary_brand_squad_member(user):
        result.append(brand_uuid)
    return result


def strip_bot_managed_squads(squads: Iterable[str]) -> List[str]:
    """Убирает сквады бота из списка, пришедшего из панели, перед записью в ``connected_squads``."""
    managed = {uuid for uuid in (get_paid_squad_uuid(), get_primary_brand_squad_uuid()) if uuid}
    if not managed:
        return list(squads)
    return [uuid for uuid in squads if uuid not in managed]
