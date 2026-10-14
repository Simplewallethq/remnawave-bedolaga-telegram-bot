"""Адреса TRON (TRC-20): base58check с версией 0x41."""

from __future__ import annotations

import hashlib

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: index for index, char in enumerate(_ALPHABET)}
_TRON_VERSION = 0x41


def _b58decode(value: str) -> bytes | None:
    number = 0
    for char in value:
        digit = _INDEX.get(char)
        if digit is None:
            return None
        number = number * 58 + digit
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeros + body


def is_trc20_address(address: str | None) -> bool:
    """Адрес кошелька в сети TRON: T…, 34 символа, верная контрольная сумма."""
    address = (address or "").strip()
    if len(address) != 34 or not address.startswith("T"):
        return False
    raw = _b58decode(address)
    if raw is None or len(raw) != 25 or raw[0] != _TRON_VERSION:
        return False
    payload, checksum = raw[:21], raw[21:]
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == checksum


def mask_address(address: str) -> str:
    """XXXX…YYYY — для экранов подтверждения."""
    address = (address or "").strip()
    if len(address) <= 8:
        return address
    return f"{address[:4]}…{address[-4:]}"
