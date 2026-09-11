"""Карта вместо СБП на счёте 1Payment: общие ключи metadata.

1Payment умеет только СБП, поэтому на его счёте есть кнопка «Оплатить
картой»: по нажатию выставляется Platega-счёт с методом «банковская карта»
на ту же сумму. Platega-счёт создаётся лениво — только когда пользователь
действительно выбрал карту, иначе каждый СБП-счёт тянул бы за собой
PENDING-транзакцию Platega и ронял её конверсию.

Обе записи помечаются, чтобы аналитика могла отделить этот поток:

* Platega-платёж: ``metadata_json.purpose = CARD_ALT_PURPOSE`` и
  ``source_onepayment_payment_id`` — в панелях конверсии Platega такие
  счета исключаются (или считаются отдельной серией);
* счёт 1Payment: блок ``metadata_json.card_alt`` с id Platega-платежа, а после
  оплаты картой — плоский ``card_alt_paid_at``: счёт оплачен не по СБП, и
  в конверсии 1Payment он не участвует ни как успех, ни как отказ.

Модуль без тяжёлых импортов — его тянут и миксин 1Payment, и миксин Platega.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# purpose в metadata Platega-платежа, выставленного с СБП-счёта 1Payment
CARD_ALT_PURPOSE = "onepayment_card_alt"
# metadata Platega-платежа: id исходного счёта 1Payment
CARD_ALT_SOURCE_KEY = "source_onepayment_payment_id"
# metadata счёта 1Payment: блок с данными карточной альтернативы
CARD_ALT_METADATA_KEY = "card_alt"
# metadata счёта 1Payment: момент оплаты картой (плоский ключ для SQL-панелей)
CARD_ALT_PAID_AT_KEY = "card_alt_paid_at"

# Префикс callback-данных кнопки «Оплатить картой»: onepay_card_{onepayment_payment_id}
CARD_ALT_CALLBACK_PREFIX = "onepay_card_"


def is_card_alt_platega_metadata(metadata: Any) -> bool:
    return isinstance(metadata, dict) and metadata.get("purpose") == CARD_ALT_PURPOSE


def get_card_alt_block(metadata: Any) -> Optional[Dict[str, Any]]:
    """Блок card_alt из metadata счёта 1Payment, если карта уже запрашивалась."""
    if not isinstance(metadata, dict):
        return None
    block = metadata.get(CARD_ALT_METADATA_KEY)
    if not isinstance(block, dict) or not block.get("platega_payment_id"):
        return None
    return block


def parse_card_alt_callback(data: Optional[str]) -> Optional[int]:
    if not data or not data.startswith(CARD_ALT_CALLBACK_PREFIX):
        return None
    try:
        return int(data[len(CARD_ALT_CALLBACK_PREFIX) :])
    except ValueError:
        return None
