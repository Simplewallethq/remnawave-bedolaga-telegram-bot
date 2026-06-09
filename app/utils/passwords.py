"""Хеширование и проверка паролей пользователей личного кабинета (bcrypt)."""

import bcrypt


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Пароль не может быть пустым")
    # bcrypt ограничен 72 байтами — обрезаем во избежание ошибки
    password_bytes = password.encode("utf-8")[:72]
    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password or not password_hash:
        return False
    try:
        password_bytes = password.encode("utf-8")[:72]
        return bcrypt.checkpw(password_bytes, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False
