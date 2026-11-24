"""Сборка списка внутренних сквадов панели для пользователя.

Панель RemnaWave показывает хост пользователю, только если инбаунд хоста есть хотя бы
в одном из его сквадов. Поверх выбранных серверов бот добавляет два сквада:

* ``PAID_INTERNAL_SQUAD_UUID`` — каждой не-триальной подписке, чтобы часть хостов
  видели только платные;
* ``PRIMARY_BRAND_INTERNAL_SQUAD_UUID`` — всем пользователям основного бренда (Leto и
  его копии), но не копикетам.

Третий управляемый сквад — сквад оплаченного слота «выделенный IP»
(``Subscription.dedicated_ip_squad_uuid``, см. ``app/services/dedicated_ip_service.py``).
Сквады всего пула IP вырезаются из любой базы: чужой слот не должен попасть юзеру
из старых данных панели.

Эти сквады намеренно **не хранятся** в ``Subscription.connected_squads``: тот список —
это выбранные пользователем серверы (каталог, цены, миграции). Они добавляются здесь,
в момент отправки в панель, и вырезаются при импорте из панели.
"""

from typing import Iterable, List, Optional, Set

from app.config import settings
from app.utils.bot_registry import is_copycat_user


# Сквады всех слотов пула «выделенный IP». Заполняет dedicated_ip_service.refresh_pool_cache()
# на старте и в цикле мониторинга.
_dedicated_ip_pool_squads: Set[str] = set()


def set_dedicated_ip_pool_squads(squads: Iterable[str]) -> None:
    global _dedicated_ip_pool_squads
    _dedicated_ip_pool_squads = {uuid for uuid in squads if uuid}


def get_dedicated_ip_pool_squads() -> Set[str]:
    return set(_dedicated_ip_pool_squads)


def get_dedicated_ip_squad_uuid(subscription) -> Optional[str]:
    value = getattr(subscription, "dedicated_ip_squad_uuid", None)
    return value or None


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

    dedicated_uuid = get_dedicated_ip_squad_uuid(subscription)

    managed = {uuid for uuid in (paid_uuid, brand_uuid, dedicated_uuid) if uuid}
    managed |= _dedicated_ip_pool_squads
    result = [uuid for uuid in base if uuid not in managed]
    if paid_uuid and is_paid_squad_member(subscription):
        result.append(paid_uuid)
    if brand_uuid and brand_uuid != paid_uuid and is_primary_brand_squad_member(user):
        result.append(brand_uuid)
    if dedicated_uuid and dedicated_uuid not in result:
        result.append(dedicated_uuid)
    return result


def strip_bot_managed_squads(squads: Iterable[str]) -> List[str]:
    """Убирает сквады бота из списка, пришедшего из панели, перед записью в ``connected_squads``."""
    managed = {uuid for uuid in (get_paid_squad_uuid(), get_primary_brand_squad_uuid()) if uuid}
    managed |= _dedicated_ip_pool_squads
    if not managed:
        return list(squads)
    return [uuid for uuid in squads if uuid not in managed]
