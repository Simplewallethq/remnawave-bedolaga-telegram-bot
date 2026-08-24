import logging
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal, engine
from app.database.models import WebApiToken
from app.utils.security import hash_api_token

logger = logging.getLogger(__name__)

async def get_database_type():
    return engine.dialect.name


async def sync_postgres_sequences() -> bool:
    """Ensure PostgreSQL sequences match the current max values after restores."""

    db_type = await get_database_type()

    if db_type != "postgresql":
        logger.debug("Пропускаем синхронизацию последовательностей: тип БД %s", db_type)
        return True

    try:
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT
                        cols.table_schema,
                        cols.table_name,
                        cols.column_name,
                        pg_get_serial_sequence(
                            format('%I.%I', cols.table_schema, cols.table_name),
                            cols.column_name
                        ) AS sequence_path
                    FROM information_schema.columns AS cols
                    WHERE cols.column_default LIKE 'nextval(%'
                      AND cols.table_schema NOT IN ('pg_catalog', 'information_schema')
                    """
                )
            )

            sequences = result.fetchall()

            if not sequences:
                logger.info("ℹ️ Не найдено последовательностей PostgreSQL для синхронизации")
                return True

            for table_schema, table_name, column_name, sequence_path in sequences:
                if not sequence_path:
                    continue

                max_result = await conn.execute(
                    text(
                        f'SELECT COALESCE(MAX("{column_name}"), 0) '
                        f'FROM "{table_schema}"."{table_name}"'
                    )
                )
                max_value = max_result.scalar() or 0

                parts = sequence_path.split('.')
                if len(parts) == 2:
                    seq_schema, seq_name = parts
                else:
                    seq_schema, seq_name = 'public', parts[-1]

                seq_schema = seq_schema.strip('"')
                seq_name = seq_name.strip('"')
                current_result = await conn.execute(
                    text(
                        f'SELECT last_value, is_called FROM "{seq_schema}"."{seq_name}"'
                    )
                )
                current_row = current_result.fetchone()

                if current_row:
                    current_last, is_called = current_row
                    current_next = current_last + 1 if is_called else current_last
                    if current_next > max_value:
                        continue

                await conn.execute(
                    text(
                        """
                        SELECT setval(:sequence_name, :new_value, TRUE)
                        """
                    ),
                    {"sequence_name": sequence_path, "new_value": max_value},
                )
                logger.info(
                    "🔄 Последовательность %s синхронизирована: MAX=%s, следующий ID=%s",
                    sequence_path,
                    max_value,
                    max_value + 1,
                )

        return True

    except Exception as error:
        logger.error("❌ Ошибка синхронизации последовательностей PostgreSQL: %s", error)
        return False

async def check_table_exists(table_name: str) -> bool:
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            if db_type == 'sqlite':
                result = await conn.execute(text(f"""
                    SELECT name FROM sqlite_master 
                    WHERE type='table' AND name='{table_name}'
                """))
                return result.fetchone() is not None
                
            elif db_type == 'postgresql':
                result = await conn.execute(text("""
                    SELECT table_name FROM information_schema.tables 
                    WHERE table_schema = 'public' AND table_name = :table_name
                """), {"table_name": table_name})
                return result.fetchone() is not None
                
            elif db_type == 'mysql':
                result = await conn.execute(text("""
                    SELECT table_name FROM information_schema.tables 
                    WHERE table_schema = DATABASE() AND table_name = :table_name
                """), {"table_name": table_name})
                return result.fetchone() is not None
                
            return False
            
    except Exception as e:
        logger.error(f"Ошибка проверки существования таблицы {table_name}: {e}")
        return False

async def check_column_exists(table_name: str, column_name: str) -> bool:
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            if db_type == 'sqlite':
                result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
                columns = result.fetchall()
                return any(col[1] == column_name for col in columns)
                
            elif db_type == 'postgresql':
                result = await conn.execute(text("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = :table_name 
                    AND column_name = :column_name
                """), {"table_name": table_name, "column_name": column_name})
                return result.fetchone() is not None
                
            elif db_type == 'mysql':
                result = await conn.execute(text("""
                    SELECT COLUMN_NAME 
                    FROM information_schema.COLUMNS 
                    WHERE TABLE_NAME = :table_name 
                    AND COLUMN_NAME = :column_name
                """), {"table_name": table_name, "column_name": column_name})
                return result.fetchone() is not None
                
            return False
            
    except Exception as e:
        logger.error(f"Ошибка проверки существования колонки {column_name}: {e}")
        return False


async def check_constraint_exists(table_name: str, constraint_name: str) -> bool:
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "postgresql":
                result = await conn.execute(
                    text(
                        """
                    SELECT 1
                    FROM information_schema.table_constraints
                    WHERE table_schema = 'public'
                      AND table_name = :table_name
                      AND constraint_name = :constraint_name
                """
                    ),
                    {"table_name": table_name, "constraint_name": constraint_name},
                )
                return result.fetchone() is not None

            if db_type == "mysql":
                result = await conn.execute(
                    text(
                        """
                    SELECT 1
                    FROM information_schema.table_constraints
                    WHERE table_schema = DATABASE()
                      AND table_name = :table_name
                      AND constraint_name = :constraint_name
                """
                    ),
                    {"table_name": table_name, "constraint_name": constraint_name},
                )
                return result.fetchone() is not None

            if db_type == "sqlite":
                result = await conn.execute(text(f"PRAGMA foreign_key_list({table_name})"))
                rows = result.fetchall()
                return any(row[5] == constraint_name for row in rows)

            return False

    except Exception as e:
        logger.error(
            f"Ошибка проверки существования ограничения {constraint_name} для {table_name}: {e}"
        )
        return False


async def check_index_exists(table_name: str, index_name: str) -> bool:
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "postgresql":
                result = await conn.execute(
                    text(
                        """
                    SELECT 1
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND tablename = :table_name
                      AND indexname = :index_name
                """
                    ),
                    {"table_name": table_name, "index_name": index_name},
                )
                return result.fetchone() is not None

            if db_type == "mysql":
                result = await conn.execute(
                    text(
                        """
                    SELECT 1
                    FROM information_schema.statistics
                    WHERE table_schema = DATABASE()
                      AND table_name = :table_name
                      AND index_name = :index_name
                """
                    ),
                    {"table_name": table_name, "index_name": index_name},
                )
                return result.fetchone() is not None

            if db_type == "sqlite":
                result = await conn.execute(text(f"PRAGMA index_list({table_name})"))
                rows = result.fetchall()
                return any(row[1] == index_name for row in rows)

            return False

    except Exception as e:
        logger.error(
            f"Ошибка проверки существования индекса {index_name} для {table_name}: {e}"
        )
        return False


async def fetch_duplicate_payment_links(conn) -> List[Tuple[str, int]]:
    result = await conn.execute(
        text(
            "SELECT payment_link_id, COUNT(*) AS cnt "
            "FROM wata_payments "
            "WHERE payment_link_id IS NOT NULL AND payment_link_id <> '' "
            "GROUP BY payment_link_id "
            "HAVING COUNT(*) > 1"
        )
    )
    return [(row[0], row[1]) for row in result.fetchall()]


def _build_dedup_suffix(base_suffix: str, record_id: int, max_length: int = 64) -> Tuple[str, int]:
    suffix = f"{base_suffix}{record_id}"
    trimmed_length = max_length - len(suffix)
    if trimmed_length < 1:
        # Fallback: use the record id only to stay within the limit.
        suffix = f"dup-{record_id}"
        trimmed_length = max_length - len(suffix)
    return suffix, trimmed_length


async def resolve_duplicate_payment_links(conn, db_type: str) -> bool:
    duplicates = await fetch_duplicate_payment_links(conn)

    if not duplicates:
        return True

    logger.warning(
        "Найдены дубликаты payment_link_id в wata_payments: %s",
        ", ".join(f"{link}×{count}" for link, count in duplicates[:5]),
    )

    for payment_link_id, _ in duplicates:
        result = await conn.execute(
            text(
                "SELECT id, payment_link_id FROM wata_payments "
                "WHERE payment_link_id = :payment_link_id "
                "ORDER BY id"
            ),
            {"payment_link_id": payment_link_id},
        )

        rows = result.fetchall()

        if not rows:
            continue

        # Skip the first occurrence to preserve the original link value.
        for duplicate_row in rows[1:]:
            record_id = duplicate_row[0]
            original_link = duplicate_row[1] or ""
            suffix, trimmed_length = _build_dedup_suffix("-dup-", record_id)
            new_base = original_link[:trimmed_length] if trimmed_length > 0 else ""
            new_link = f"{new_base}{suffix}" if new_base else suffix

            await conn.execute(
                text(
                    "UPDATE wata_payments SET payment_link_id = :new_link "
                    "WHERE id = :record_id"
                ),
                {"new_link": new_link, "record_id": record_id},
            )

    remaining_duplicates = await fetch_duplicate_payment_links(conn)

    if remaining_duplicates:
        logger.error(
            "Не удалось устранить дубликаты payment_link_id: %s",
            ", ".join(f"{link}×{count}" for link, count in remaining_duplicates[:5]),
        )
        return False

    logger.info("✅ Дубликаты payment_link_id устранены")
    return True


async def enforce_wata_payment_link_constraints(
    conn,
    db_type: str,
    unique_index_exists: bool,
    legacy_index_exists: bool,
) -> Tuple[bool, bool]:
    try:
        if db_type == "sqlite":
            await conn.execute(
                text(
                    "UPDATE wata_payments "
                    "SET payment_link_id = 'legacy-' || id "
                    "WHERE payment_link_id IS NULL OR payment_link_id = ''"
                )
            )

            if not await resolve_duplicate_payment_links(conn, db_type):
                return unique_index_exists, legacy_index_exists

            if not unique_index_exists:
                await conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_wata_payment_link "
                        "ON wata_payments(payment_link_id)"
                    )
                )
                logger.info("✅ Создан уникальный индекс uq_wata_payment_link для payment_link_id")
                unique_index_exists = True
            else:
                logger.info("ℹ️ Уникальный индекс для payment_link_id уже существует")

            if legacy_index_exists and unique_index_exists:
                await conn.execute(text("DROP INDEX IF EXISTS idx_wata_link_id"))
                logger.info("ℹ️ Удалён устаревший индекс idx_wata_link_id")
                legacy_index_exists = False

            return unique_index_exists, legacy_index_exists

        if db_type == "postgresql":
            await conn.execute(
                text(
                    "UPDATE wata_payments "
                    "SET payment_link_id = 'legacy-' || id::text "
                    "WHERE payment_link_id IS NULL OR payment_link_id = ''"
                )
            )

            await conn.execute(
                text(
                    "ALTER TABLE wata_payments "
                    "ALTER COLUMN payment_link_id SET NOT NULL"
                )
            )
            logger.info("✅ Колонка payment_link_id теперь NOT NULL")

            if not await resolve_duplicate_payment_links(conn, db_type):
                return unique_index_exists, legacy_index_exists

            if not unique_index_exists:
                await conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_wata_payment_link "
                        "ON wata_payments(payment_link_id)"
                    )
                )
                logger.info("✅ Создан уникальный индекс uq_wata_payment_link для payment_link_id")
                unique_index_exists = True
            else:
                logger.info("ℹ️ Уникальный индекс для payment_link_id уже существует")

            if legacy_index_exists and unique_index_exists:
                await conn.execute(text("DROP INDEX IF EXISTS idx_wata_link_id"))
                logger.info("ℹ️ Удалён устаревший индекс idx_wata_link_id")
                legacy_index_exists = False

            return unique_index_exists, legacy_index_exists

        if db_type == "mysql":
            await conn.execute(
                text(
                    "UPDATE wata_payments "
                    "SET payment_link_id = CONCAT('legacy-', id) "
                    "WHERE payment_link_id IS NULL OR payment_link_id = ''"
                )
            )

            await conn.execute(
                text(
                    "ALTER TABLE wata_payments "
                    "MODIFY COLUMN payment_link_id VARCHAR(64) NOT NULL"
                )
            )
            logger.info("✅ Колонка payment_link_id теперь NOT NULL")

            if not await resolve_duplicate_payment_links(conn, db_type):
                return unique_index_exists, legacy_index_exists

            if not unique_index_exists:
                await conn.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_wata_payment_link "
                        "ON wata_payments(payment_link_id)"
                    )
                )
                logger.info("✅ Создан уникальный индекс uq_wata_payment_link для payment_link_id")
                unique_index_exists = True
            else:
                logger.info("ℹ️ Уникальный индекс для payment_link_id уже существует")

            if legacy_index_exists and unique_index_exists:
                await conn.execute(text("DROP INDEX idx_wata_link_id ON wata_payments"))
                logger.info("ℹ️ Удалён устаревший индекс idx_wata_link_id")
                legacy_index_exists = False

            return unique_index_exists, legacy_index_exists

        logger.warning(
            "⚠️ Неизвестный тип БД %s — не удалось усилить ограничения payment_link_id", db_type
        )
        return unique_index_exists, legacy_index_exists

    except Exception as e:
        logger.error(f"Ошибка настройки ограничений payment_link_id: {e}")
        return unique_index_exists, legacy_index_exists

async def create_cryptobot_payments_table():
    table_exists = await check_table_exists('cryptobot_payments')
    if table_exists:
        logger.info("Таблица cryptobot_payments уже существует")
        return True
    
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE cryptobot_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    invoice_id VARCHAR(255) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    asset VARCHAR(10) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    description TEXT NULL,
                    payload TEXT NULL,
                    bot_invoice_url TEXT NULL,
                    mini_app_invoice_url TEXT NULL,
                    web_app_invoice_url TEXT NULL,
                    paid_at DATETIME NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );
                
                CREATE INDEX idx_cryptobot_payments_user_id ON cryptobot_payments(user_id);
                CREATE INDEX idx_cryptobot_payments_invoice_id ON cryptobot_payments(invoice_id);
                CREATE INDEX idx_cryptobot_payments_status ON cryptobot_payments(status);
                """
                
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE cryptobot_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    invoice_id VARCHAR(255) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    asset VARCHAR(10) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    description TEXT NULL,
                    payload TEXT NULL,
                    bot_invoice_url TEXT NULL,
                    mini_app_invoice_url TEXT NULL,
                    web_app_invoice_url TEXT NULL,
                    paid_at TIMESTAMP NULL,
                    transaction_id INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );
                
                CREATE INDEX idx_cryptobot_payments_user_id ON cryptobot_payments(user_id);
                CREATE INDEX idx_cryptobot_payments_invoice_id ON cryptobot_payments(invoice_id);
                CREATE INDEX idx_cryptobot_payments_status ON cryptobot_payments(status);
                """
                
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE cryptobot_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    invoice_id VARCHAR(255) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    asset VARCHAR(10) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    description TEXT NULL,
                    payload TEXT NULL,
                    bot_invoice_url TEXT NULL,
                    mini_app_invoice_url TEXT NULL,
                    web_app_invoice_url TEXT NULL,
                    paid_at DATETIME NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );
                
                CREATE INDEX idx_cryptobot_payments_user_id ON cryptobot_payments(user_id);
                CREATE INDEX idx_cryptobot_payments_invoice_id ON cryptobot_payments(invoice_id);
                CREATE INDEX idx_cryptobot_payments_status ON cryptobot_payments(status);
                """
            else:
                logger.error(f"Неподдерживаемый тип БД для создания таблицы: {db_type}")
                return False
            
            await conn.execute(text(create_sql))
            logger.info("Таблица cryptobot_payments успешно создана")
            return True
            
    except Exception as e:
        logger.error(f"Ошибка создания таблицы cryptobot_payments: {e}")
        return False


async def create_heleket_payments_table():
    table_exists = await check_table_exists('heleket_payments')
    if table_exists:
        logger.info("Таблица heleket_payments уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE heleket_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    uuid VARCHAR(255) UNIQUE NOT NULL,
                    order_id VARCHAR(128) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    currency VARCHAR(10) NOT NULL,
                    payer_amount VARCHAR(50) NULL,
                    payer_currency VARCHAR(10) NULL,
                    exchange_rate DOUBLE PRECISION NULL,
                    discount_percent INTEGER NULL,
                    status VARCHAR(50) NOT NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    paid_at DATETIME NULL,
                    expires_at DATETIME NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_heleket_payments_user_id ON heleket_payments(user_id);
                CREATE INDEX idx_heleket_payments_uuid ON heleket_payments(uuid);
                CREATE INDEX idx_heleket_payments_order_id ON heleket_payments(order_id);
                CREATE INDEX idx_heleket_payments_status ON heleket_payments(status);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE heleket_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    uuid VARCHAR(255) UNIQUE NOT NULL,
                    order_id VARCHAR(128) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    currency VARCHAR(10) NOT NULL,
                    payer_amount VARCHAR(50) NULL,
                    payer_currency VARCHAR(10) NULL,
                    exchange_rate DOUBLE PRECISION NULL,
                    discount_percent INTEGER NULL,
                    status VARCHAR(50) NOT NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    paid_at TIMESTAMP NULL,
                    expires_at TIMESTAMP NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX idx_heleket_payments_user_id ON heleket_payments(user_id);
                CREATE INDEX idx_heleket_payments_uuid ON heleket_payments(uuid);
                CREATE INDEX idx_heleket_payments_order_id ON heleket_payments(order_id);
                CREATE INDEX idx_heleket_payments_status ON heleket_payments(status);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE heleket_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    uuid VARCHAR(255) UNIQUE NOT NULL,
                    order_id VARCHAR(128) UNIQUE NOT NULL,
                    amount VARCHAR(50) NOT NULL,
                    currency VARCHAR(10) NOT NULL,
                    payer_amount VARCHAR(50) NULL,
                    payer_currency VARCHAR(10) NULL,
                    exchange_rate DOUBLE NULL,
                    discount_percent INT NULL,
                    status VARCHAR(50) NOT NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    paid_at DATETIME NULL,
                    expires_at DATETIME NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_heleket_payments_user_id ON heleket_payments(user_id);
                CREATE INDEX idx_heleket_payments_uuid ON heleket_payments(uuid);
                CREATE INDEX idx_heleket_payments_order_id ON heleket_payments(order_id);
                CREATE INDEX idx_heleket_payments_status ON heleket_payments(status);
                """

            else:
                logger.error(f"Неподдерживаемый тип БД для таблицы heleket_payments: {db_type}")
                return False

            await conn.execute(text(create_sql))
            logger.info("Таблица heleket_payments успешно создана")
            return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы heleket_payments: {e}")
        return False


async def create_mulenpay_payments_table():
    table_exists = await check_table_exists('mulenpay_payments')
    if table_exists:
        logger.info("Таблица mulenpay_payments уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE mulenpay_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    mulen_payment_id INTEGER NULL,
                    uuid VARCHAR(255) NOT NULL UNIQUE,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'created',
                    is_paid BOOLEAN DEFAULT 0,
                    paid_at DATETIME NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_mulenpay_uuid ON mulenpay_payments(uuid);
                CREATE INDEX idx_mulenpay_payment_id ON mulenpay_payments(mulen_payment_id);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE mulenpay_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    mulen_payment_id INTEGER NULL,
                    uuid VARCHAR(255) NOT NULL UNIQUE,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'created',
                    is_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    paid_at TIMESTAMP NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX idx_mulenpay_uuid ON mulenpay_payments(uuid);
                CREATE INDEX idx_mulenpay_payment_id ON mulenpay_payments(mulen_payment_id);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE mulenpay_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    mulen_payment_id INT NULL,
                    uuid VARCHAR(255) NOT NULL UNIQUE,
                    amount_kopeks INT NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'created',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    payment_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_mulenpay_uuid ON mulenpay_payments(uuid);
                CREATE INDEX idx_mulenpay_payment_id ON mulenpay_payments(mulen_payment_id);
                """

            else:
                logger.error(f"Неподдерживаемый тип БД для таблицы mulenpay_payments: {db_type}")
                return False

            await conn.execute(text(create_sql))
            logger.info("Таблица mulenpay_payments успешно создана")
            return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы mulenpay_payments: {e}")
        return False


async def ensure_mulenpay_payment_schema() -> bool:
    logger.info("=== ОБНОВЛЕНИЕ СХЕМЫ MULEN PAY ===")

    table_exists = await check_table_exists("mulenpay_payments")
    if not table_exists:
        logger.warning("⚠️ Таблица mulenpay_payments отсутствует — создаём заново")
        return await create_mulenpay_payments_table()

    try:
        column_exists = await check_column_exists("mulenpay_payments", "mulen_payment_id")
        paid_at_column_exists = await check_column_exists("mulenpay_payments", "paid_at")
        index_exists = await check_index_exists("mulenpay_payments", "idx_mulenpay_payment_id")

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not column_exists:
                if db_type == "sqlite":
                    alter_sql = "ALTER TABLE mulenpay_payments ADD COLUMN mulen_payment_id INTEGER NULL"
                elif db_type == "postgresql":
                    alter_sql = "ALTER TABLE mulenpay_payments ADD COLUMN mulen_payment_id INTEGER NULL"
                elif db_type == "mysql":
                    alter_sql = "ALTER TABLE mulenpay_payments ADD COLUMN mulen_payment_id INT NULL"
                else:
                    logger.error(
                        "Неподдерживаемый тип БД для добавления mulen_payment_id в mulenpay_payments: %s",
                        db_type,
                    )
                    return False

                await conn.execute(text(alter_sql))
                logger.info("✅ Добавлена колонка mulenpay_payments.mulen_payment_id")
            else:
                logger.info("ℹ️ Колонка mulenpay_payments.mulen_payment_id уже существует")

            if not paid_at_column_exists:
                if db_type == "sqlite":
                    alter_paid_at_sql = "ALTER TABLE mulenpay_payments ADD COLUMN paid_at DATETIME NULL"
                elif db_type == "postgresql":
                    alter_paid_at_sql = "ALTER TABLE mulenpay_payments ADD COLUMN paid_at TIMESTAMP NULL"
                elif db_type == "mysql":
                    alter_paid_at_sql = "ALTER TABLE mulenpay_payments ADD COLUMN paid_at DATETIME NULL"
                else:
                    logger.error(
                        "Неподдерживаемый тип БД для добавления paid_at в mulenpay_payments: %s",
                        db_type,
                    )
                    return False

                await conn.execute(text(alter_paid_at_sql))
                logger.info("✅ Добавлена колонка mulenpay_payments.paid_at")
            else:
                logger.info("ℹ️ Колонка mulenpay_payments.paid_at уже существует")

            if not index_exists:
                if db_type == "sqlite":
                    create_index_sql = (
                        "CREATE INDEX IF NOT EXISTS idx_mulenpay_payment_id "
                        "ON mulenpay_payments(mulen_payment_id)"
                    )
                elif db_type == "postgresql":
                    create_index_sql = (
                        "CREATE INDEX IF NOT EXISTS idx_mulenpay_payment_id "
                        "ON mulenpay_payments(mulen_payment_id)"
                    )
                elif db_type == "mysql":
                    create_index_sql = (
                        "CREATE INDEX idx_mulenpay_payment_id "
                        "ON mulenpay_payments(mulen_payment_id)"
                    )
                else:
                    logger.error(
                        "Неподдерживаемый тип БД для создания индекса mulenpay_payment_id: %s",
                        db_type,
                    )
                    return False

                await conn.execute(text(create_index_sql))
                logger.info("✅ Создан индекс idx_mulenpay_payment_id")
            else:
                logger.info("ℹ️ Индекс idx_mulenpay_payment_id уже существует")

        return True

    except Exception as e:
        logger.error(f"Ошибка обновления схемы mulenpay_payments: {e}")
        return False


async def create_pal24_payments_table():
    table_exists = await check_table_exists('pal24_payments')
    if table_exists:
        logger.info("Таблица pal24_payments уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE pal24_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    bill_id VARCHAR(255) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(20) NOT NULL DEFAULT 'normal',
                    status VARCHAR(50) NOT NULL DEFAULT 'NEW',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    last_status VARCHAR(50) NULL,
                    last_status_checked_at DATETIME NULL,
                    link_url TEXT NULL,
                    link_page_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    payment_id VARCHAR(255) NULL,
                    payment_status VARCHAR(50) NULL,
                    payment_method VARCHAR(50) NULL,
                    balance_amount VARCHAR(50) NULL,
                    balance_currency VARCHAR(10) NULL,
                    payer_account VARCHAR(255) NULL,
                    ttl INTEGER NULL,
                    expires_at DATETIME NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_pal24_bill_id ON pal24_payments(bill_id);
                CREATE INDEX idx_pal24_order_id ON pal24_payments(order_id);
                CREATE INDEX idx_pal24_payment_id ON pal24_payments(payment_id);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE pal24_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    bill_id VARCHAR(255) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(20) NOT NULL DEFAULT 'normal',
                    status VARCHAR(50) NOT NULL DEFAULT 'NEW',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    is_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    paid_at TIMESTAMP NULL,
                    last_status VARCHAR(50) NULL,
                    last_status_checked_at TIMESTAMP NULL,
                    link_url TEXT NULL,
                    link_page_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    payment_id VARCHAR(255) NULL,
                    payment_status VARCHAR(50) NULL,
                    payment_method VARCHAR(50) NULL,
                    balance_amount VARCHAR(50) NULL,
                    balance_currency VARCHAR(10) NULL,
                    payer_account VARCHAR(255) NULL,
                    ttl INTEGER NULL,
                    expires_at TIMESTAMP NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX idx_pal24_bill_id ON pal24_payments(bill_id);
                CREATE INDEX idx_pal24_order_id ON pal24_payments(order_id);
                CREATE INDEX idx_pal24_payment_id ON pal24_payments(payment_id);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE pal24_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    bill_id VARCHAR(255) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INT NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(20) NOT NULL DEFAULT 'normal',
                    status VARCHAR(50) NOT NULL DEFAULT 'NEW',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    last_status VARCHAR(50) NULL,
                    last_status_checked_at DATETIME NULL,
                    link_url TEXT NULL,
                    link_page_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    payment_id VARCHAR(255) NULL,
                    payment_status VARCHAR(50) NULL,
                    payment_method VARCHAR(50) NULL,
                    balance_amount VARCHAR(50) NULL,
                    balance_currency VARCHAR(10) NULL,
                    payer_account VARCHAR(255) NULL,
                    ttl INT NULL,
                    expires_at DATETIME NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE INDEX idx_pal24_bill_id ON pal24_payments(bill_id);
                CREATE INDEX idx_pal24_order_id ON pal24_payments(order_id);
                CREATE INDEX idx_pal24_payment_id ON pal24_payments(payment_id);
                """

            else:
                logger.error(f"Неподдерживаемый тип БД для таблицы pal24_payments: {db_type}")
                return False

            await conn.execute(text(create_sql))
            logger.info("Таблица pal24_payments успешно создана")
            return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы pal24_payments: {e}")
        return False


async def create_wata_payments_table():
    table_exists = await check_table_exists('wata_payments')
    if table_exists:
        logger.info("Таблица wata_payments уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE wata_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    payment_link_id VARCHAR(64) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(50) NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'Opened',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    last_status VARCHAR(50) NULL,
                    terminal_public_id VARCHAR(64) NULL,
                    url TEXT NULL,
                    success_redirect_url TEXT NULL,
                    fail_redirect_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    expires_at DATETIME NULL,
                    transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE UNIQUE INDEX idx_wata_link_id ON wata_payments(payment_link_id);
                CREATE INDEX idx_wata_order_id ON wata_payments(order_id);
                """

            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE wata_payments (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    payment_link_id VARCHAR(64) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(50) NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'Opened',
                    is_paid BOOLEAN NOT NULL DEFAULT FALSE,
                    paid_at TIMESTAMP NULL,
                    last_status VARCHAR(50) NULL,
                    terminal_public_id VARCHAR(64) NULL,
                    url TEXT NULL,
                    success_redirect_url TEXT NULL,
                    fail_redirect_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    expires_at TIMESTAMP NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE UNIQUE INDEX idx_wata_link_id ON wata_payments(payment_link_id);
                CREATE INDEX idx_wata_order_id ON wata_payments(order_id);
                """

            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE wata_payments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    payment_link_id VARCHAR(64) NOT NULL UNIQUE,
                    order_id VARCHAR(255) NULL,
                    amount_kopeks INT NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    type VARCHAR(50) NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'Opened',
                    is_paid BOOLEAN NOT NULL DEFAULT 0,
                    paid_at DATETIME NULL,
                    last_status VARCHAR(50) NULL,
                    terminal_public_id VARCHAR(64) NULL,
                    url TEXT NULL,
                    success_redirect_url TEXT NULL,
                    fail_redirect_url TEXT NULL,
                    metadata_json JSON NULL,
                    callback_payload JSON NULL,
                    expires_at DATETIME NULL,
                    transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
                );

                CREATE UNIQUE INDEX idx_wata_link_id ON wata_payments(payment_link_id);
                CREATE INDEX idx_wata_order_id ON wata_payments(order_id);
                """

            else:
                logger.error(f"Неподдерживаемый тип БД для таблицы wata_payments: {db_type}")
                return False

            await conn.execute(text(create_sql))
            logger.info("Таблица wata_payments успешно создана")
            return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы wata_payments: {e}")
        return False


async def ensure_wata_payment_schema() -> bool:
    try:
        table_exists = await check_table_exists("wata_payments")
        if not table_exists:
            logger.warning("⚠️ Таблица wata_payments отсутствует — создаём заново")
            return await create_wata_payments_table()

        db_type = await get_database_type()

        legacy_link_index_exists = await check_index_exists(
            "wata_payments", "idx_wata_link_id"
        )
        unique_link_index_exists = await check_index_exists(
            "wata_payments", "uq_wata_payment_link"
        )
        builtin_unique_index_exists = await check_index_exists(
            "wata_payments", "wata_payments_payment_link_id_key"
        )
        sqlite_auto_unique_exists = (
            await check_index_exists("wata_payments", "sqlite_autoindex_wata_payments_1")
            if db_type == "sqlite"
            else False
        )
        order_index_exists = await check_index_exists("wata_payments", "idx_wata_order_id")

        payment_link_column_exists = await check_column_exists(
            "wata_payments", "payment_link_id"
        )
        order_id_column_exists = await check_column_exists("wata_payments", "order_id")

        unique_index_exists = (
            unique_link_index_exists
            or builtin_unique_index_exists
            or sqlite_auto_unique_exists
        )

        async with engine.begin() as conn:
            if not payment_link_column_exists:
                if db_type == "sqlite":
                    await conn.execute(
                        text(
                            "ALTER TABLE wata_payments "
                            "ADD COLUMN payment_link_id VARCHAR(64) NOT NULL DEFAULT ''"
                        )
                    )
                    payment_link_column_exists = True
                    unique_index_exists = False
                elif db_type == "postgresql":
                    await conn.execute(
                        text(
                            "ALTER TABLE wata_payments "
                            "ADD COLUMN IF NOT EXISTS payment_link_id VARCHAR(64)"
                        )
                    )
                    payment_link_column_exists = True
                elif db_type == "mysql":
                    await conn.execute(
                        text("ALTER TABLE wata_payments ADD COLUMN payment_link_id VARCHAR(64)")
                    )
                    payment_link_column_exists = True
                else:
                    logger.warning(
                        "⚠️ Неизвестный тип БД %s — пропущено добавление payment_link_id",
                        db_type,
                    )

                if payment_link_column_exists:
                    logger.info("✅ Добавлена колонка payment_link_id в wata_payments")

            if payment_link_column_exists:
                unique_index_exists, legacy_link_index_exists = (
                    await enforce_wata_payment_link_constraints(
                        conn,
                        db_type,
                        unique_index_exists,
                        legacy_link_index_exists,
                    )
                )

            if not order_id_column_exists:
                if db_type == "sqlite":
                    await conn.execute(
                        text("ALTER TABLE wata_payments ADD COLUMN order_id VARCHAR(255)")
                    )
                    order_id_column_exists = True
                elif db_type == "postgresql":
                    await conn.execute(
                        text(
                            "ALTER TABLE wata_payments "
                            "ADD COLUMN IF NOT EXISTS order_id VARCHAR(255)"
                        )
                    )
                    order_id_column_exists = True
                elif db_type == "mysql":
                    await conn.execute(
                        text("ALTER TABLE wata_payments ADD COLUMN order_id VARCHAR(255)")
                    )
                    order_id_column_exists = True
                else:
                    logger.warning(
                        "⚠️ Неизвестный тип БД %s — пропущено добавление order_id",
                        db_type,
                    )

                if order_id_column_exists:
                    logger.info("✅ Добавлена колонка order_id в wata_payments")

            if not order_index_exists:
                if not order_id_column_exists:
                    logger.warning(
                        "⚠️ Пропущено создание индекса idx_wata_order_id — колонка order_id отсутствует"
                    )
                else:
                    index_created = False
                    if db_type in {"sqlite", "postgresql"}:
                        await conn.execute(
                            text(
                                "CREATE INDEX IF NOT EXISTS idx_wata_order_id ON wata_payments(order_id)"
                            )
                        )
                        index_created = True
                    elif db_type == "mysql":
                        await conn.execute(
                            text("CREATE INDEX idx_wata_order_id ON wata_payments(order_id)")
                        )
                        index_created = True
                    else:
                        logger.warning(
                            "⚠️ Неизвестный тип БД %s — пропущено создание индекса idx_wata_order_id",
                            db_type,
                        )

                    if index_created:
                        logger.info("✅ Создан индекс idx_wata_order_id")
            else:
                logger.info("ℹ️ Индекс idx_wata_order_id уже существует")

        return True

    except Exception as e:
        logger.error(f"Ошибка обновления схемы wata_payments: {e}")
        return False


async def create_discount_offers_table():
    table_exists = await check_table_exists('discount_offers')
    if table_exists:
        logger.info("Таблица discount_offers уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(text("""
                    CREATE TABLE discount_offers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        subscription_id INTEGER NULL,
                        notification_type VARCHAR(50) NOT NULL,
                        discount_percent INTEGER NOT NULL DEFAULT 0,
                        bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        expires_at DATETIME NOT NULL,
                        claimed_at DATETIME NULL,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount',
                        extra_data TEXT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
                    )
                """))
                await conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_discount_offers_user_type
                    ON discount_offers (user_id, notification_type)
                """))

            elif db_type == 'postgresql':
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS discount_offers (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        subscription_id INTEGER NULL REFERENCES subscriptions(id) ON DELETE SET NULL,
                        notification_type VARCHAR(50) NOT NULL,
                        discount_percent INTEGER NOT NULL DEFAULT 0,
                        bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        expires_at TIMESTAMP NOT NULL,
                        claimed_at TIMESTAMP NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount',
                        extra_data JSON NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                await conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS ix_discount_offers_user_type
                    ON discount_offers (user_id, notification_type)
                """))

            elif db_type == 'mysql':
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS discount_offers (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        user_id INTEGER NOT NULL,
                        subscription_id INTEGER NULL,
                        notification_type VARCHAR(50) NOT NULL,
                        discount_percent INTEGER NOT NULL DEFAULT 0,
                        bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        expires_at DATETIME NOT NULL,
                        claimed_at DATETIME NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount',
                        extra_data JSON NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        CONSTRAINT fk_discount_offers_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        CONSTRAINT fk_discount_offers_subscription FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
                    )
                """))
                await conn.execute(text("""
                    CREATE INDEX ix_discount_offers_user_type
                    ON discount_offers (user_id, notification_type)
                """))

            else:
                raise ValueError(f"Unsupported database type: {db_type}")

        logger.info("✅ Таблица discount_offers успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы discount_offers: {e}")
        return False


async def create_referral_contests_table() -> bool:
    table_exists = await check_table_exists("referral_contests")
    if table_exists:
        logger.info("Таблица referral_contests уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                await conn.execute(text("""
                    CREATE TABLE referral_contests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title VARCHAR(255) NOT NULL,
                        description TEXT NULL,
                        prize_text TEXT NULL,
                        contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid',
                        start_at DATETIME NOT NULL,
                        end_at DATETIME NOT NULL,
                        daily_summary_time TIME NOT NULL DEFAULT '12:00:00',
                        daily_summary_times VARCHAR(255) NULL,
                        timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        last_daily_summary_date DATE NULL,
                        last_daily_summary_at DATETIME NULL,
                        final_summary_sent BOOLEAN NOT NULL DEFAULT 0,
                        created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            elif db_type == "postgresql":
                await conn.execute(text("""
                    CREATE TABLE referral_contests (
                        id SERIAL PRIMARY KEY,
                        title VARCHAR(255) NOT NULL,
                        description TEXT NULL,
                        prize_text TEXT NULL,
                        contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid',
                        start_at TIMESTAMP NOT NULL,
                        end_at TIMESTAMP NOT NULL,
                        daily_summary_time TIME NOT NULL DEFAULT '12:00:00',
                        daily_summary_times VARCHAR(255) NULL,
                        timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        last_daily_summary_date DATE NULL,
                        last_daily_summary_at TIMESTAMP NULL,
                        final_summary_sent BOOLEAN NOT NULL DEFAULT FALSE,
                        created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            elif db_type == "mysql":
                await conn.execute(text("""
                    CREATE TABLE referral_contests (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        title VARCHAR(255) NOT NULL,
                        description TEXT NULL,
                        prize_text TEXT NULL,
                        contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid',
                        start_at DATETIME NOT NULL,
                        end_at DATETIME NOT NULL,
                        daily_summary_time TIME NOT NULL DEFAULT '12:00:00',
                        daily_summary_times VARCHAR(255) NULL,
                        timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        last_daily_summary_date DATE NULL,
                        last_daily_summary_at DATETIME NULL,
                        final_summary_sent BOOLEAN NOT NULL DEFAULT FALSE,
                        created_by INTEGER NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        CONSTRAINT fk_referral_contest_creator FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                    )
                """))
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

        logger.info("✅ Таблица referral_contests создана")
        return True
    except Exception as error:
        logger.error(f"Ошибка создания таблицы referral_contests: {error}")
        return False


async def create_referral_contest_events_table() -> bool:
    table_exists = await check_table_exists("referral_contest_events")
    if table_exists:
        logger.info("Таблица referral_contest_events уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                await conn.execute(text("""
                    CREATE TABLE referral_contest_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        contest_id INTEGER NOT NULL,
                        referrer_id INTEGER NOT NULL,
                        referral_id INTEGER NOT NULL,
                        event_type VARCHAR(50) NOT NULL,
                        amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(contest_id) REFERENCES referral_contests(id) ON DELETE CASCADE,
                        FOREIGN KEY(referrer_id) REFERENCES users(id) ON DELETE CASCADE,
                        FOREIGN KEY(referral_id) REFERENCES users(id) ON DELETE CASCADE,
                        UNIQUE(contest_id, referral_id)
                    )
                """))
                await conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_referral_contest_referrer
                    ON referral_contest_events (contest_id, referrer_id)
                """))
            elif db_type == "postgresql":
                await conn.execute(text("""
                    CREATE TABLE referral_contest_events (
                        id SERIAL PRIMARY KEY,
                        contest_id INTEGER NOT NULL REFERENCES referral_contests(id) ON DELETE CASCADE,
                        referrer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        referral_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        event_type VARCHAR(50) NOT NULL,
                        amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_referral_contest_referral UNIQUE (contest_id, referral_id)
                    )
                """))
                await conn.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_referral_contest_referrer
                    ON referral_contest_events (contest_id, referrer_id)
                """))
            elif db_type == "mysql":
                await conn.execute(text("""
                    CREATE TABLE referral_contest_events (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        contest_id INTEGER NOT NULL,
                        referrer_id INTEGER NOT NULL,
                        referral_id INTEGER NOT NULL,
                        event_type VARCHAR(50) NOT NULL,
                        amount_kopeks INTEGER NOT NULL DEFAULT 0,
                        occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_referral_contest FOREIGN KEY(contest_id) REFERENCES referral_contests(id) ON DELETE CASCADE,
                        CONSTRAINT fk_referral_contest_referrer FOREIGN KEY(referrer_id) REFERENCES users(id) ON DELETE CASCADE,
                        CONSTRAINT fk_referral_contest_referral FOREIGN KEY(referral_id) REFERENCES users(id) ON DELETE CASCADE,
                        CONSTRAINT uq_referral_contest_referral UNIQUE (contest_id, referral_id)
                    )
                """))
                await conn.execute(text("""
                    CREATE INDEX idx_referral_contest_referrer
                    ON referral_contest_events (contest_id, referrer_id)
                """))
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

        logger.info("✅ Таблица referral_contest_events создана")
        return True
    except Exception as error:
        logger.error(f"Ошибка создания таблицы referral_contest_events: {error}")
    return False


async def ensure_referral_contest_summary_columns() -> bool:
    ok = True
    for column in ["daily_summary_times", "last_daily_summary_at"]:
        exists = await check_column_exists("referral_contests", column)
        if exists:
            logger.info("Колонка %s в referral_contests уже существует", column)
            continue
        try:
            async with engine.begin() as conn:
                db_type = await get_database_type()
                if db_type == "postgresql":
                    await conn.execute(
                        text(
                            f"ALTER TABLE referral_contests ADD COLUMN {column} "
                            + ("VARCHAR(255)" if column == "daily_summary_times" else "TIMESTAMP")
                        )
                    )
                else:
                    await conn.execute(
                        text(
                            f"ALTER TABLE referral_contests ADD COLUMN {column} "
                            + ("VARCHAR(255)" if column == "daily_summary_times" else "DATETIME")
                        )
                    )
            logger.info("✅ Колонка %s в referral_contests добавлена", column)
        except Exception as error:
            ok = False
            logger.error("Ошибка добавления %s в referral_contests: %s", column, error)
    return ok


async def create_contest_templates_table() -> bool:
    table_exists = await check_table_exists("contest_templates")
    if table_exists:
        logger.info("Таблица contest_templates уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                await conn.execute(text("""
                    CREATE TABLE contest_templates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(100) NOT NULL,
                        slug VARCHAR(50) NOT NULL UNIQUE,
                        description TEXT NULL,
                        prize_days INTEGER NOT NULL DEFAULT 1,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        times_per_day INTEGER NOT NULL DEFAULT 1,
                        schedule_times VARCHAR(255) NULL,
                        cooldown_hours INTEGER NOT NULL DEFAULT 24,
                        payload TEXT NULL,
                        is_enabled BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            elif db_type == "postgresql":
                await conn.execute(text("""
                    CREATE TABLE contest_templates (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(100) NOT NULL,
                        slug VARCHAR(50) NOT NULL UNIQUE,
                        description TEXT NULL,
                        prize_days INTEGER NOT NULL DEFAULT 1,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        times_per_day INTEGER NOT NULL DEFAULT 1,
                        schedule_times VARCHAR(255) NULL,
                        cooldown_hours INTEGER NOT NULL DEFAULT 24,
                        payload JSON NULL,
                        is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
            elif db_type == "mysql":
                await conn.execute(text("""
                    CREATE TABLE contest_templates (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        name VARCHAR(100) NOT NULL,
                        slug VARCHAR(50) NOT NULL UNIQUE,
                        description TEXT NULL,
                        prize_days INTEGER NOT NULL DEFAULT 1,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        times_per_day INTEGER NOT NULL DEFAULT 1,
                        schedule_times VARCHAR(255) NULL,
                        cooldown_hours INTEGER NOT NULL DEFAULT 24,
                        payload JSON NULL,
                        is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                """))
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

        logger.info("✅ Таблица contest_templates создана")
        return True
    except Exception as error:
        logger.error(f"Ошибка создания таблицы contest_templates: {error}")
        return False


async def create_contest_rounds_table() -> bool:
    table_exists = await check_table_exists("contest_rounds")
    if table_exists:
        logger.info("Таблица contest_rounds уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                await conn.execute(text("""
                    CREATE TABLE contest_rounds (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        template_id INTEGER NOT NULL,
                        starts_at DATETIME NOT NULL,
                        ends_at DATETIME NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'active',
                        payload TEXT NULL,
                        winners_count INTEGER NOT NULL DEFAULT 0,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        message_id BIGINT NULL,
                        chat_id BIGINT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(template_id) REFERENCES contest_templates(id) ON DELETE CASCADE
                    )
                """))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_contest_round_status ON contest_rounds(status)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_contest_round_template ON contest_rounds(template_id)"))
            elif db_type == "postgresql":
                await conn.execute(text("""
                    CREATE TABLE contest_rounds (
                        id SERIAL PRIMARY KEY,
                        template_id INTEGER NOT NULL REFERENCES contest_templates(id) ON DELETE CASCADE,
                        starts_at TIMESTAMP NOT NULL,
                        ends_at TIMESTAMP NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'active',
                        payload JSON NULL,
                        winners_count INTEGER NOT NULL DEFAULT 0,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        message_id BIGINT NULL,
                        chat_id BIGINT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_contest_round_status ON contest_rounds(status)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_contest_round_template ON contest_rounds(template_id)"))
            elif db_type == "mysql":
                await conn.execute(text("""
                    CREATE TABLE contest_rounds (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        template_id INTEGER NOT NULL,
                        starts_at DATETIME NOT NULL,
                        ends_at DATETIME NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'active',
                        payload JSON NULL,
                        winners_count INTEGER NOT NULL DEFAULT 0,
                        max_winners INTEGER NOT NULL DEFAULT 1,
                        attempts_per_user INTEGER NOT NULL DEFAULT 1,
                        message_id BIGINT NULL,
                        chat_id BIGINT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        CONSTRAINT fk_contest_round_template FOREIGN KEY(template_id) REFERENCES contest_templates(id) ON DELETE CASCADE
                    )
                """))
                await conn.execute(text("CREATE INDEX idx_contest_round_status ON contest_rounds(status)"))
                await conn.execute(text("CREATE INDEX idx_contest_round_template ON contest_rounds(template_id)"))
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

        logger.info("✅ Таблица contest_rounds создана")
        return True
    except Exception as error:
        logger.error(f"Ошибка создания таблицы contest_rounds: {error}")
        return False


async def create_contest_attempts_table() -> bool:
    table_exists = await check_table_exists("contest_attempts")
    if table_exists:
        logger.info("Таблица contest_attempts уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                await conn.execute(text("""
                    CREATE TABLE contest_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        round_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        answer TEXT NULL,
                        is_winner BOOLEAN NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(round_id) REFERENCES contest_rounds(id) ON DELETE CASCADE,
                        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        UNIQUE(round_id, user_id)
                    )
                """))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_contest_attempt_round ON contest_attempts(round_id)"))
            elif db_type == "postgresql":
                await conn.execute(text("""
                    CREATE TABLE contest_attempts (
                        id SERIAL PRIMARY KEY,
                        round_id INTEGER NOT NULL REFERENCES contest_rounds(id) ON DELETE CASCADE,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        answer TEXT NULL,
                        is_winner BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_round_user_attempt UNIQUE(round_id, user_id)
                    )
                """))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_contest_attempt_round ON contest_attempts(round_id)"))
            elif db_type == "mysql":
                await conn.execute(text("""
                    CREATE TABLE contest_attempts (
                        id INTEGER PRIMARY KEY AUTO_INCREMENT,
                        round_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        answer TEXT NULL,
                        is_winner BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_contest_attempt_round FOREIGN KEY(round_id) REFERENCES contest_rounds(id) ON DELETE CASCADE,
                        CONSTRAINT fk_contest_attempt_user FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                        CONSTRAINT uq_round_user_attempt UNIQUE(round_id, user_id)
                    )
                """))
                await conn.execute(text("CREATE INDEX idx_contest_attempt_round ON contest_attempts(round_id)"))
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

        logger.info("✅ Таблица contest_attempts создана")
        return True
    except Exception as error:
        logger.error(f"Ошибка создания таблицы contest_attempts: {error}")
        return False


async def ensure_referral_contest_type_column() -> bool:
    column_exists = await check_column_exists("referral_contests", "contest_type")
    if column_exists:
        logger.info("Колонка contest_type в referral_contests уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                await conn.execute(
                    text(
                        "ALTER TABLE referral_contests "
                        "ADD COLUMN contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid'"
                    )
                )
            elif db_type == "postgresql":
                await conn.execute(
                    text(
                        "ALTER TABLE referral_contests "
                        "ADD COLUMN contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid'"
                    )
                )
            elif db_type == "mysql":
                await conn.execute(
                    text(
                        "ALTER TABLE referral_contests "
                        "ADD COLUMN contest_type VARCHAR(50) NOT NULL DEFAULT 'referral_paid'"
                    )
                )
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

        logger.info("✅ Колонка contest_type в referral_contests добавлена")
        return True
    except Exception as error:
        logger.error(f"Ошибка добавления contest_type в referral_contests: {error}")
        return False


async def ensure_discount_offer_columns():
    try:
        effect_exists = await check_column_exists('discount_offers', 'effect_type')
        extra_exists = await check_column_exists('discount_offers', 'extra_data')

        if effect_exists and extra_exists:
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not effect_exists:
                if db_type == 'sqlite':
                    await conn.execute(text(
                        "ALTER TABLE discount_offers ADD COLUMN effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount'"
                    ))
                elif db_type == 'postgresql':
                    await conn.execute(text(
                        "ALTER TABLE discount_offers ADD COLUMN effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount'"
                    ))
                elif db_type == 'mysql':
                    await conn.execute(text(
                        "ALTER TABLE discount_offers ADD COLUMN effect_type VARCHAR(50) NOT NULL DEFAULT 'percent_discount'"
                    ))
                else:
                    raise ValueError(f"Unsupported database type: {db_type}")

            if not extra_exists:
                if db_type == 'sqlite':
                    await conn.execute(text(
                        "ALTER TABLE discount_offers ADD COLUMN extra_data TEXT NULL"
                    ))
                elif db_type == 'postgresql':
                    await conn.execute(text(
                        "ALTER TABLE discount_offers ADD COLUMN extra_data JSON NULL"
                    ))
                elif db_type == 'mysql':
                    await conn.execute(text(
                        "ALTER TABLE discount_offers ADD COLUMN extra_data JSON NULL"
                    ))
                else:
                    raise ValueError(f"Unsupported database type: {db_type}")

        logger.info("✅ Колонки effect_type и extra_data для discount_offers проверены")
        return True

    except Exception as e:
        logger.error(f"Ошибка обновления колонок discount_offers: {e}")
        return False


async def ensure_user_promo_offer_discount_columns():
    try:
        percent_exists = await check_column_exists('users', 'promo_offer_discount_percent')
        source_exists = await check_column_exists('users', 'promo_offer_discount_source')
        expires_exists = await check_column_exists('users', 'promo_offer_discount_expires_at')

        if percent_exists and source_exists and expires_exists:
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not percent_exists:
                column_def = 'INTEGER NOT NULL DEFAULT 0'
                if db_type == 'mysql':
                    column_def = 'INT NOT NULL DEFAULT 0'
                await conn.execute(text(
                    f"ALTER TABLE users ADD COLUMN promo_offer_discount_percent {column_def}"
                ))

            if not source_exists:
                if db_type == 'sqlite':
                    column_def = 'TEXT NULL'
                elif db_type == 'postgresql':
                    column_def = 'VARCHAR(100) NULL'
                elif db_type == 'mysql':
                    column_def = 'VARCHAR(100) NULL'
                else:
                    raise ValueError(f"Unsupported database type: {db_type}")

                await conn.execute(text(
                    f"ALTER TABLE users ADD COLUMN promo_offer_discount_source {column_def}"
                ))

            if not expires_exists:
                if db_type == 'sqlite':
                    column_def = 'DATETIME NULL'
                elif db_type == 'postgresql':
                    column_def = 'TIMESTAMP NULL'
                elif db_type == 'mysql':
                    column_def = 'DATETIME NULL'
                else:
                    raise ValueError(f"Unsupported database type: {db_type}")

                await conn.execute(text(
                    f"ALTER TABLE users ADD COLUMN promo_offer_discount_expires_at {column_def}"
                ))

        logger.info("✅ Колонки promo_offer_discount_* для users проверены")
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления колонок promo_offer_discount_*: {e}")
        return False


async def ensure_cryptobot_payment_metadata_column() -> bool:
    """cryptobot_payments.metadata_json — snapshot разбивки частичной оплаты тарифа."""
    try:
        if await check_column_exists('cryptobot_payments', 'metadata_json'):
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()
            if db_type == 'sqlite':
                column_def = 'JSON NULL'
            elif db_type == 'postgresql':
                column_def = 'JSON NULL'
            elif db_type == 'mysql':
                column_def = 'JSON NULL'
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            await conn.execute(text(
                f"ALTER TABLE cryptobot_payments ADD COLUMN metadata_json {column_def}"
            ))

        logger.info("✅ Колонка cryptobot_payments.metadata_json добавлена")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления cryptobot_payments.metadata_json: {e}")
        return False


async def add_user_tariff_pricing_cohort_override_column() -> bool:
    try:
        if await check_column_exists('users', 'tariff_pricing_cohort_override'):
            logger.info("ℹ️ Колонка users.tariff_pricing_cohort_override уже существует")
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()
            if db_type == 'sqlite':
                column_def = 'VARCHAR(8) NULL'
            elif db_type == 'postgresql':
                column_def = 'VARCHAR(8) NULL'
            elif db_type == 'mysql':
                column_def = 'VARCHAR(8) NULL'
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            await conn.execute(text(
                f"ALTER TABLE users ADD COLUMN tariff_pricing_cohort_override {column_def}"
            ))

        logger.info("✅ Колонка users.tariff_pricing_cohort_override добавлена")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления колонки users.tariff_pricing_cohort_override: {e}")
        return False


async def ensure_promo_offer_template_active_duration_column() -> bool:
    try:
        column_exists = await check_column_exists('promo_offer_templates', 'active_discount_hours')

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not column_exists:
                if db_type == 'sqlite':
                    column_def = 'INTEGER NULL'
                elif db_type == 'postgresql':
                    column_def = 'INTEGER NULL'
                elif db_type == 'mysql':
                    column_def = 'INT NULL'
                else:
                    raise ValueError(f"Unsupported database type: {db_type}")

                await conn.execute(text(
                    f"ALTER TABLE promo_offer_templates ADD COLUMN active_discount_hours {column_def}"
                ))

            await conn.execute(text(
                "UPDATE promo_offer_templates "
                "SET active_discount_hours = valid_hours "
                "WHERE offer_type IN ('extend_discount', 'purchase_discount') "
                "AND (active_discount_hours IS NULL OR active_discount_hours <= 0)"
            ))

        logger.info("✅ Колонка active_discount_hours в promo_offer_templates актуальна")
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления active_discount_hours в promo_offer_templates: {e}")
        return False


async def migrate_discount_offer_effect_types():
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE discount_offers SET effect_type = 'percent_discount' "
                "WHERE effect_type = 'balance_bonus'"
            ))
        logger.info("✅ Типы эффектов discount_offers обновлены на percent_discount")
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления типов эффектов discount_offers: {e}")
        return False


async def reset_discount_offer_bonuses():
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE discount_offers SET bonus_amount_kopeks = 0 WHERE bonus_amount_kopeks <> 0"
            ))
            await conn.execute(text(
                "UPDATE promo_offer_templates SET bonus_amount_kopeks = 0 WHERE bonus_amount_kopeks <> 0"
            ))
        logger.info("✅ Бонусы промо-предложений сброшены до нуля")
        return True
    except Exception as e:
        logger.error(f"Ошибка обнуления бонусов промо-предложений: {e}")
        return False


async def create_promo_offer_templates_table():
    table_exists = await check_table_exists('promo_offer_templates')
    if table_exists:
        logger.info("Таблица promo_offer_templates уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE promo_offer_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(255) NOT NULL,
                    offer_type VARCHAR(50) NOT NULL,
                    message_text TEXT NOT NULL,
                    button_text VARCHAR(255) NOT NULL,
                    valid_hours INTEGER NOT NULL DEFAULT 24,
                    discount_percent INTEGER NOT NULL DEFAULT 0,
                    bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                    active_discount_hours INTEGER NULL,
                    test_duration_hours INTEGER NULL,
                    test_squad_uuids TEXT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_by INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_promo_offer_templates_type ON promo_offer_templates(offer_type);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS promo_offer_templates (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    offer_type VARCHAR(50) NOT NULL,
                    message_text TEXT NOT NULL,
                    button_text VARCHAR(255) NOT NULL,
                    valid_hours INTEGER NOT NULL DEFAULT 24,
                    discount_percent INTEGER NOT NULL DEFAULT 0,
                    bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
                    active_discount_hours INTEGER NULL,
                    test_duration_hours INTEGER NULL,
                    test_squad_uuids JSON NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS ix_promo_offer_templates_type ON promo_offer_templates(offer_type);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS promo_offer_templates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    offer_type VARCHAR(50) NOT NULL,
                    message_text TEXT NOT NULL,
                    button_text VARCHAR(255) NOT NULL,
                    valid_hours INT NOT NULL DEFAULT 24,
                    discount_percent INT NOT NULL DEFAULT 0,
                    bonus_amount_kopeks INT NOT NULL DEFAULT 0,
                    active_discount_hours INT NULL,
                    test_duration_hours INT NULL,
                    test_squad_uuids JSON NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_promo_offer_templates_type ON promo_offer_templates(offer_type);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            await conn.execute(text(create_sql))

        logger.info("✅ Таблица promo_offer_templates успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы promo_offer_templates: {e}")
        return False


async def create_main_menu_buttons_table() -> bool:
    table_exists = await check_table_exists('main_menu_buttons')
    if table_exists:
        logger.info("Таблица main_menu_buttons уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE main_menu_buttons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text VARCHAR(64) NOT NULL,
                    action_type VARCHAR(20) NOT NULL,
                    action_value TEXT NOT NULL,
                    visibility VARCHAR(20) NOT NULL DEFAULT 'all',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS ix_main_menu_buttons_order ON main_menu_buttons(display_order, id);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS main_menu_buttons (
                    id SERIAL PRIMARY KEY,
                    text VARCHAR(64) NOT NULL,
                    action_type VARCHAR(20) NOT NULL,
                    action_value TEXT NOT NULL,
                    visibility VARCHAR(20) NOT NULL DEFAULT 'all',
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS ix_main_menu_buttons_order ON main_menu_buttons(display_order, id);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS main_menu_buttons (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    text VARCHAR(64) NOT NULL,
                    action_type VARCHAR(20) NOT NULL,
                    action_value TEXT NOT NULL,
                    visibility VARCHAR(20) NOT NULL DEFAULT 'all',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    display_order INT NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                );

                CREATE INDEX ix_main_menu_buttons_order ON main_menu_buttons(display_order, id);
                """
            else:
                logger.error(f"Неподдерживаемый тип БД для таблицы main_menu_buttons: {db_type}")
                return False

            await conn.execute(text(create_sql))

        logger.info("✅ Таблица main_menu_buttons успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы main_menu_buttons: {e}")
        return False


async def create_promo_offer_logs_table() -> bool:
    table_exists = await check_table_exists('promo_offer_logs')
    if table_exists:
        logger.info("Таблица promo_offer_logs уже существует")
        return True

    try:
        db_type = await get_database_type()
        async with engine.begin() as conn:
            if db_type == 'sqlite':
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS promo_offer_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                        offer_id INTEGER NULL REFERENCES discount_offers(id) ON DELETE SET NULL,
                        action VARCHAR(50) NOT NULL,
                        source VARCHAR(100) NULL,
                        percent INTEGER NULL,
                        effect_type VARCHAR(50) NULL,
                        details JSON NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS ix_promo_offer_logs_created_at ON promo_offer_logs(created_at DESC);
                    CREATE INDEX IF NOT EXISTS ix_promo_offer_logs_user_id ON promo_offer_logs(user_id);
                """))
            elif db_type == 'postgresql':
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS promo_offer_logs (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        offer_id INTEGER REFERENCES discount_offers(id) ON DELETE SET NULL,
                        action VARCHAR(50) NOT NULL,
                        source VARCHAR(100),
                        percent INTEGER,
                        effect_type VARCHAR(50),
                        details JSONB,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE INDEX IF NOT EXISTS ix_promo_offer_logs_created_at ON promo_offer_logs(created_at DESC);
                    CREATE INDEX IF NOT EXISTS ix_promo_offer_logs_user_id ON promo_offer_logs(user_id);
                """))
            elif db_type == 'mysql':
                await conn.execute(text("""
                    CREATE TABLE IF NOT EXISTS promo_offer_logs (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NULL,
                        offer_id INT NULL,
                        action VARCHAR(50) NOT NULL,
                        source VARCHAR(100) NULL,
                        percent INT NULL,
                        effect_type VARCHAR(50) NULL,
                        details JSON NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_promo_offer_logs_users FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                        CONSTRAINT fk_promo_offer_logs_offers FOREIGN KEY (offer_id) REFERENCES discount_offers(id) ON DELETE SET NULL
                    );

                    CREATE INDEX ix_promo_offer_logs_created_at ON promo_offer_logs(created_at DESC);
                    CREATE INDEX ix_promo_offer_logs_user_id ON promo_offer_logs(user_id);
                """))
            else:
                logger.warning("Неизвестный тип БД для создания promo_offer_logs: %s", db_type)
                return False

        logger.info("✅ Таблица promo_offer_logs успешно создана")
        return True
    except Exception as e:
        logger.error(f"Ошибка создания таблицы promo_offer_logs: {e}")
        return False


async def create_subscription_temporary_access_table():
    table_exists = await check_table_exists('subscription_temporary_access')
    if table_exists:
        logger.info("Таблица subscription_temporary_access уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE subscription_temporary_access (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    offer_id INTEGER NOT NULL,
                    squad_uuid VARCHAR(255) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    deactivated_at DATETIME NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    was_already_connected BOOLEAN NOT NULL DEFAULT 0,
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
                    FOREIGN KEY(offer_id) REFERENCES discount_offers(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_temp_access_subscription ON subscription_temporary_access(subscription_id);
                CREATE INDEX ix_temp_access_offer ON subscription_temporary_access(offer_id);
                CREATE INDEX ix_temp_access_active ON subscription_temporary_access(is_active, expires_at);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS subscription_temporary_access (
                    id SERIAL PRIMARY KEY,
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    offer_id INTEGER NOT NULL REFERENCES discount_offers(id) ON DELETE CASCADE,
                    squad_uuid VARCHAR(255) NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deactivated_at TIMESTAMP NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    was_already_connected BOOLEAN NOT NULL DEFAULT FALSE
                );

                CREATE INDEX IF NOT EXISTS ix_temp_access_subscription ON subscription_temporary_access(subscription_id);
                CREATE INDEX IF NOT EXISTS ix_temp_access_offer ON subscription_temporary_access(offer_id);
                CREATE INDEX IF NOT EXISTS ix_temp_access_active ON subscription_temporary_access(is_active, expires_at);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS subscription_temporary_access (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    subscription_id INT NOT NULL,
                    offer_id INT NOT NULL,
                    squad_uuid VARCHAR(255) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    deactivated_at DATETIME NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    was_already_connected BOOLEAN NOT NULL DEFAULT FALSE,
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
                    FOREIGN KEY(offer_id) REFERENCES discount_offers(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_temp_access_subscription ON subscription_temporary_access(subscription_id);
                CREATE INDEX ix_temp_access_offer ON subscription_temporary_access(offer_id);
                CREATE INDEX ix_temp_access_active ON subscription_temporary_access(is_active, expires_at);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            await conn.execute(text(create_sql))

        logger.info("✅ Таблица subscription_temporary_access успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы subscription_temporary_access: {e}")
        return False

async def create_user_messages_table():
    table_exists = await check_table_exists('user_messages')
    if table_exists:
        logger.info("Таблица user_messages уже существует")
        return True
    
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE user_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_text TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    sort_order INTEGER DEFAULT 0,
                    created_by INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                
                CREATE INDEX idx_user_messages_active ON user_messages(is_active);
                CREATE INDEX idx_user_messages_sort ON user_messages(sort_order, created_at);
                """
                
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE user_messages (
                    id SERIAL PRIMARY KEY,
                    message_text TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    sort_order INTEGER DEFAULT 0,
                    created_by INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                
                CREATE INDEX idx_user_messages_active ON user_messages(is_active);
                CREATE INDEX idx_user_messages_sort ON user_messages(sort_order, created_at);
                """
                
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE user_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    message_text TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    sort_order INT DEFAULT 0,
                    created_by INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                
                CREATE INDEX idx_user_messages_active ON user_messages(is_active);
                CREATE INDEX idx_user_messages_sort ON user_messages(sort_order, created_at);
                """
            else:
                logger.error(f"Неподдерживаемый тип БД для создания таблицы: {db_type}")
                return False
            
            await conn.execute(text(create_sql))
            logger.info("Таблица user_messages успешно создана")
            return True
            
    except Exception as e:
        logger.error(f"Ошибка создания таблицы user_messages: {e}")
        return False


async def ensure_promo_groups_setup():
    logger.info("=== НАСТРОЙКА ПРОМО ГРУПП ===")

    try:
        promo_table_exists = await check_table_exists("promo_groups")

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not promo_table_exists:
                if db_type == "sqlite":
                    await conn.execute(
                        text(
                            """
                            CREATE TABLE IF NOT EXISTS promo_groups (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                name VARCHAR(255) NOT NULL,
                                server_discount_percent INTEGER NOT NULL DEFAULT 0,
                                traffic_discount_percent INTEGER NOT NULL DEFAULT 0,
                                device_discount_percent INTEGER NOT NULL DEFAULT 0,
                                is_default BOOLEAN NOT NULL DEFAULT 0,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                            )
                        """
                        )
                    )
                    await conn.execute(
                        text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_groups_name ON promo_groups(name)"
                        )
                    )
                elif db_type == "postgresql":
                    await conn.execute(
                        text(
                            """
                            CREATE TABLE IF NOT EXISTS promo_groups (
                                id SERIAL PRIMARY KEY,
                                name VARCHAR(255) NOT NULL,
                                server_discount_percent INTEGER NOT NULL DEFAULT 0,
                                traffic_discount_percent INTEGER NOT NULL DEFAULT 0,
                                device_discount_percent INTEGER NOT NULL DEFAULT 0,
                                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                                created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                                updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                                CONSTRAINT uq_promo_groups_name UNIQUE (name)
                            )
                        """
                        )
                    )
                elif db_type == "mysql":
                    await conn.execute(
                        text(
                            """
                            CREATE TABLE IF NOT EXISTS promo_groups (
                                id INT AUTO_INCREMENT PRIMARY KEY,
                                name VARCHAR(255) NOT NULL,
                                server_discount_percent INT NOT NULL DEFAULT 0,
                                traffic_discount_percent INT NOT NULL DEFAULT 0,
                                device_discount_percent INT NOT NULL DEFAULT 0,
                                is_default TINYINT(1) NOT NULL DEFAULT 0,
                                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                                UNIQUE KEY uq_promo_groups_name (name)
                            ) ENGINE=InnoDB
                        """
                        )
                    )
                else:
                    logger.error(f"Неподдерживаемый тип БД для promo_groups: {db_type}")
                    return False

                logger.info("Создана таблица promo_groups")

            if db_type == "postgresql" and not await check_constraint_exists(
                "promo_groups", "uq_promo_groups_name"
            ):
                try:
                    await conn.execute(
                        text(
                            "ALTER TABLE promo_groups ADD CONSTRAINT uq_promo_groups_name UNIQUE (name)"
                        )
                    )
                except Exception as e:
                    logger.warning(
                        f"Не удалось добавить уникальное ограничение uq_promo_groups_name: {e}"
                    )

            period_discounts_column_exists = await check_column_exists(
                "promo_groups", "period_discounts"
            )

            if not period_discounts_column_exists:
                if db_type == "sqlite":
                    await conn.execute(
                        text("ALTER TABLE promo_groups ADD COLUMN period_discounts JSON")
                    )
                    await conn.execute(
                        text("UPDATE promo_groups SET period_discounts = '{}' WHERE period_discounts IS NULL")
                    )
                elif db_type == "postgresql":
                    await conn.execute(
                        text(
                            "ALTER TABLE promo_groups ADD COLUMN period_discounts JSONB"
                        )
                    )
                    await conn.execute(
                        text(
                            "UPDATE promo_groups SET period_discounts = '{}'::jsonb WHERE period_discounts IS NULL"
                        )
                    )
                elif db_type == "mysql":
                    await conn.execute(
                        text("ALTER TABLE promo_groups ADD COLUMN period_discounts JSON")
                    )
                    await conn.execute(
                        text(
                            "UPDATE promo_groups SET period_discounts = JSON_OBJECT() WHERE period_discounts IS NULL"
                        )
                    )
                else:
                    logger.error(
                        f"Неподдерживаемый тип БД для promo_groups.period_discounts: {db_type}"
                    )
                    return False

                logger.info("Добавлена колонка promo_groups.period_discounts")

            auto_assign_column_exists = await check_column_exists(
                "promo_groups", "auto_assign_total_spent_kopeks"
            )

            if not auto_assign_column_exists:
                if db_type == "sqlite":
                    await conn.execute(
                        text(
                            "ALTER TABLE promo_groups ADD COLUMN auto_assign_total_spent_kopeks INTEGER DEFAULT 0"
                        )
                    )
                elif db_type == "postgresql":
                    await conn.execute(
                        text(
                            "ALTER TABLE promo_groups ADD COLUMN auto_assign_total_spent_kopeks INTEGER DEFAULT 0"
                        )
                    )
                elif db_type == "mysql":
                    await conn.execute(
                        text(
                            "ALTER TABLE promo_groups ADD COLUMN auto_assign_total_spent_kopeks INT DEFAULT 0"
                        )
                    )
                else:
                    logger.error(
                        f"Неподдерживаемый тип БД для promo_groups.auto_assign_total_spent_kopeks: {db_type}"
                    )
                    return False

                logger.info(
                    "Добавлена колонка promo_groups.auto_assign_total_spent_kopeks"
                )

            addon_discount_column_exists = await check_column_exists(
                "promo_groups", "apply_discounts_to_addons"
            )
            priority_column_exists = await check_column_exists(
                "promo_groups", "priority"
            )

            if not addon_discount_column_exists:
                if db_type == "sqlite":
                    await conn.execute(
                        text(
                            "ALTER TABLE promo_groups ADD COLUMN apply_discounts_to_addons BOOLEAN NOT NULL DEFAULT 1"
                        )
                    )
                    await conn.execute(
                        text(
                            "UPDATE promo_groups SET apply_discounts_to_addons = 1 WHERE apply_discounts_to_addons IS NULL"
                        )
                    )
                elif db_type == "postgresql":
                    await conn.execute(
                        text(
                            "ALTER TABLE promo_groups ADD COLUMN apply_discounts_to_addons BOOLEAN NOT NULL DEFAULT TRUE"
                        )
                    )
                    await conn.execute(
                        text(
                            "UPDATE promo_groups SET apply_discounts_to_addons = TRUE WHERE apply_discounts_to_addons IS NULL"
                        )
                    )
                elif db_type == "mysql":
                    await conn.execute(
                        text(
                            "ALTER TABLE promo_groups ADD COLUMN apply_discounts_to_addons TINYINT(1) NOT NULL DEFAULT 1"
                        )
                    )
                    await conn.execute(
                        text(
                            "UPDATE promo_groups SET apply_discounts_to_addons = 1 WHERE apply_discounts_to_addons IS NULL"
                        )
                    )
                else:
                    logger.error(
                        f"Неподдерживаемый тип БД для promo_groups.apply_discounts_to_addons: {db_type}"
                    )
                    return False

                logger.info(
                    "Добавлена колонка promo_groups.apply_discounts_to_addons"
                )
                addon_discount_column_exists = True

            column_exists = await check_column_exists("users", "promo_group_id")

            if not column_exists:
                if db_type == "sqlite":
                    await conn.execute(text("ALTER TABLE users ADD COLUMN promo_group_id INTEGER"))
                elif db_type == "postgresql":
                    await conn.execute(text("ALTER TABLE users ADD COLUMN promo_group_id INTEGER"))
                elif db_type == "mysql":
                    await conn.execute(text("ALTER TABLE users ADD COLUMN promo_group_id INT"))
                else:
                    logger.error(f"Неподдерживаемый тип БД для promo_group_id: {db_type}")
                    return False

                logger.info("Добавлена колонка users.promo_group_id")

            auto_promo_flag_exists = await check_column_exists(
                "users", "auto_promo_group_assigned"
            )

            if not auto_promo_flag_exists:
                if db_type == "sqlite":
                    await conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN auto_promo_group_assigned BOOLEAN DEFAULT 0"
                        )
                    )
                elif db_type == "postgresql":
                    await conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN auto_promo_group_assigned BOOLEAN DEFAULT FALSE"
                        )
                    )
                elif db_type == "mysql":
                    await conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN auto_promo_group_assigned TINYINT(1) DEFAULT 0"
                        )
                    )
                else:
                    logger.error(
                        f"Неподдерживаемый тип БД для users.auto_promo_group_assigned: {db_type}"
                    )
                    return False

                logger.info("Добавлена колонка users.auto_promo_group_assigned")

            threshold_column_exists = await check_column_exists(
                "users", "auto_promo_group_threshold_kopeks"
            )

            if not threshold_column_exists:
                if db_type == "sqlite":
                    await conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN auto_promo_group_threshold_kopeks INTEGER NOT NULL DEFAULT 0"
                        )
                    )
                elif db_type == "postgresql":
                    await conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN auto_promo_group_threshold_kopeks BIGINT NOT NULL DEFAULT 0"
                        )
                    )
                elif db_type == "mysql":
                    await conn.execute(
                        text(
                            "ALTER TABLE users ADD COLUMN auto_promo_group_threshold_kopeks BIGINT NOT NULL DEFAULT 0"
                        )
                    )
                else:
                    logger.error(
                        f"Неподдерживаемый тип БД для users.auto_promo_group_threshold_kopeks: {db_type}"
                    )
                    return False

                logger.info(
                    "Добавлена колонка users.auto_promo_group_threshold_kopeks"
                )

            index_exists = await check_index_exists("users", "ix_users_promo_group_id")

            if not index_exists:
                try:
                    if db_type == "sqlite":
                        await conn.execute(
                            text("CREATE INDEX IF NOT EXISTS ix_users_promo_group_id ON users(promo_group_id)")
                        )
                    elif db_type == "postgresql":
                        await conn.execute(
                            text("CREATE INDEX IF NOT EXISTS ix_users_promo_group_id ON users(promo_group_id)")
                        )
                    elif db_type == "mysql":
                        await conn.execute(
                            text("CREATE INDEX ix_users_promo_group_id ON users(promo_group_id)")
                        )
                    logger.info("Создан индекс ix_users_promo_group_id")
                except Exception as e:
                    logger.warning(f"Не удалось создать индекс ix_users_promo_group_id: {e}")

            default_group_name = "Базовый юзер"
            default_group_id = None

            result = await conn.execute(
                text(
                    "SELECT id, is_default FROM promo_groups WHERE name = :name LIMIT 1"
                ),
                {"name": default_group_name},
            )
            row = result.fetchone()

            if row:
                default_group_id = row[0]
                if not row[1]:
                    await conn.execute(
                        text(
                            "UPDATE promo_groups SET is_default = :is_default WHERE id = :group_id"
                        ),
                        {"is_default": True, "group_id": default_group_id},
                    )
            else:
                result = await conn.execute(
                    text(
                        "SELECT id FROM promo_groups WHERE is_default = :is_default LIMIT 1"
                    ),
                    {"is_default": True},
                )
                existing_default = result.fetchone()

                if existing_default:
                    default_group_id = existing_default[0]
                else:
                    insert_params = {
                        "name": default_group_name,
                        "is_default": True,
                    }

                    if priority_column_exists:
                        insert_params["priority"] = 0

                    if addon_discount_column_exists and priority_column_exists:
                        insert_sql = """
                            INSERT INTO promo_groups (
                                name,
                                priority,
                                server_discount_percent,
                                traffic_discount_percent,
                                device_discount_percent,
                                apply_discounts_to_addons,
                                is_default
                            ) VALUES (:name, :priority, 0, 0, 0, :apply_discounts_to_addons, :is_default)
                        """
                        insert_params["apply_discounts_to_addons"] = True
                    elif addon_discount_column_exists:
                        insert_sql = """
                            INSERT INTO promo_groups (
                                name,
                                server_discount_percent,
                                traffic_discount_percent,
                                device_discount_percent,
                                apply_discounts_to_addons,
                                is_default
                            ) VALUES (:name, 0, 0, 0, :apply_discounts_to_addons, :is_default)
                        """
                        insert_params["apply_discounts_to_addons"] = True
                    elif priority_column_exists:
                        insert_sql = """
                            INSERT INTO promo_groups (
                                name,
                                priority,
                                server_discount_percent,
                                traffic_discount_percent,
                                device_discount_percent,
                                is_default
                            ) VALUES (:name, :priority, 0, 0, 0, :is_default)
                        """
                    else:
                        insert_sql = """
                            INSERT INTO promo_groups (
                                name,
                                server_discount_percent,
                                traffic_discount_percent,
                                device_discount_percent,
                                is_default
                            ) VALUES (:name, 0, 0, 0, :is_default)
                        """

                    await conn.execute(text(insert_sql), insert_params)

                    result = await conn.execute(
                        text(
                            "SELECT id FROM promo_groups WHERE name = :name LIMIT 1"
                        ),
                        {"name": default_group_name},
                    )
                    row = result.fetchone()
                    default_group_id = row[0] if row else None

            if default_group_id is None:
                logger.error("Не удалось определить идентификатор базовой промо-группы")
                return False

            await conn.execute(
                text(
                    """
                    UPDATE users
                    SET promo_group_id = :group_id
                    WHERE promo_group_id IS NULL
                """
                ),
                {"group_id": default_group_id},
            )

            if db_type == "postgresql":
                constraint_exists = await check_constraint_exists(
                    "users", "fk_users_promo_group_id_promo_groups"
                )
                if not constraint_exists:
                    try:
                        await conn.execute(
                            text(
                                """
                                ALTER TABLE users
                                ADD CONSTRAINT fk_users_promo_group_id_promo_groups
                                FOREIGN KEY (promo_group_id)
                                REFERENCES promo_groups(id)
                                ON DELETE RESTRICT
                            """
                            )
                        )
                        logger.info("Добавлен внешний ключ users -> promo_groups")
                    except Exception as e:
                        logger.warning(
                            f"Не удалось добавить внешний ключ users.promo_group_id: {e}"
                        )

                try:
                    await conn.execute(
                        text(
                            "ALTER TABLE users ALTER COLUMN promo_group_id SET NOT NULL"
                        )
                    )
                except Exception as e:
                    logger.warning(
                        f"Не удалось сделать users.promo_group_id NOT NULL: {e}"
                    )

            elif db_type == "mysql":
                constraint_exists = await check_constraint_exists(
                    "users", "fk_users_promo_group_id_promo_groups"
                )
                if not constraint_exists:
                    try:
                        await conn.execute(
                            text(
                                """
                                ALTER TABLE users
                                ADD CONSTRAINT fk_users_promo_group_id_promo_groups
                                FOREIGN KEY (promo_group_id)
                                REFERENCES promo_groups(id)
                                ON DELETE RESTRICT
                            """
                            )
                        )
                        logger.info("Добавлен внешний ключ users -> promo_groups")
                    except Exception as e:
                        logger.warning(
                            f"Не удалось добавить внешний ключ users.promo_group_id: {e}"
                        )

                try:
                    await conn.execute(
                        text(
                            "ALTER TABLE users MODIFY promo_group_id INT NOT NULL"
                        )
                    )
                except Exception as e:
                    logger.warning(
                        f"Не удалось сделать users.promo_group_id NOT NULL: {e}"
                    )

            logger.info("✅ Промо группы настроены")
            return True

    except Exception as e:
        logger.error(f"Ошибка настройки промо групп: {e}")
        return False

async def add_welcome_text_is_enabled_column():
    column_exists = await check_column_exists('welcome_texts', 'is_enabled')
    if column_exists:
        logger.info("Колонка is_enabled уже существует в таблице welcome_texts")
        return True
    
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            if db_type == 'sqlite':
                alter_sql = "ALTER TABLE welcome_texts ADD COLUMN is_enabled BOOLEAN DEFAULT 1 NOT NULL"
            elif db_type == 'postgresql':
                alter_sql = "ALTER TABLE welcome_texts ADD COLUMN is_enabled BOOLEAN DEFAULT TRUE NOT NULL"
            elif db_type == 'mysql':
                alter_sql = "ALTER TABLE welcome_texts ADD COLUMN is_enabled BOOLEAN DEFAULT TRUE NOT NULL"
            else:
                logger.error(f"Неподдерживаемый тип БД для добавления колонки: {db_type}")
                return False
            
            await conn.execute(text(alter_sql))
            logger.info("✅ Поле is_enabled добавлено в таблицу welcome_texts")
            
            if db_type == 'sqlite':
                update_sql = "UPDATE welcome_texts SET is_enabled = 1 WHERE is_enabled IS NULL"
            else:
                update_sql = "UPDATE welcome_texts SET is_enabled = TRUE WHERE is_enabled IS NULL"
            
            result = await conn.execute(text(update_sql))
            updated_count = result.rowcount
            logger.info(f"Обновлено {updated_count} существующих записей welcome_texts")
            
            return True
            
    except Exception as e:
        logger.error(f"Ошибка при добавлении поля is_enabled: {e}")
        return False

async def create_welcome_texts_table():
    table_exists = await check_table_exists('welcome_texts')
    if table_exists:
        logger.info("Таблица welcome_texts уже существует")
        return await add_welcome_text_is_enabled_column()
    
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE welcome_texts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text_content TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    is_enabled BOOLEAN DEFAULT 1 NOT NULL,
                    created_by INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                
                CREATE INDEX idx_welcome_texts_active ON welcome_texts(is_active);
                CREATE INDEX idx_welcome_texts_enabled ON welcome_texts(is_enabled);
                CREATE INDEX idx_welcome_texts_updated ON welcome_texts(updated_at);
                """
                
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE welcome_texts (
                    id SERIAL PRIMARY KEY,
                    text_content TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    is_enabled BOOLEAN DEFAULT TRUE NOT NULL,
                    created_by INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                
                CREATE INDEX idx_welcome_texts_active ON welcome_texts(is_active);
                CREATE INDEX idx_welcome_texts_enabled ON welcome_texts(is_enabled);
                CREATE INDEX idx_welcome_texts_updated ON welcome_texts(updated_at);
                """
                
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE welcome_texts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    text_content TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT TRUE,
                    is_enabled BOOLEAN DEFAULT TRUE NOT NULL,
                    created_by INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                
                CREATE INDEX idx_welcome_texts_active ON welcome_texts(is_active);
                CREATE INDEX idx_welcome_texts_enabled ON welcome_texts(is_enabled);
                CREATE INDEX idx_welcome_texts_updated ON welcome_texts(updated_at);
                """
            else:
                logger.error(f"Неподдерживаемый тип БД для создания таблицы: {db_type}")
                return False
            
            await conn.execute(text(create_sql))
            logger.info("✅ Таблица welcome_texts успешно создана с полем is_enabled")
            return True
            
    except Exception as e:
        logger.error(f"Ошибка создания таблицы welcome_texts: {e}")
        return False


async def create_pinned_messages_table():
    table_exists = await check_table_exists("pinned_messages")
    if table_exists:
        logger.info("Таблица pinned_messages уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE pinned_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL DEFAULT '',
                    media_type VARCHAR(32) NULL,
                    media_file_id VARCHAR(255) NULL,
                    send_before_menu BOOLEAN NOT NULL DEFAULT 1,
                    send_on_every_start BOOLEAN NOT NULL DEFAULT 1,
                    is_active BOOLEAN DEFAULT 1,
                    created_by INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS ix_pinned_messages_active ON pinned_messages(is_active);
                """

            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE pinned_messages (
                    id SERIAL PRIMARY KEY,
                    content TEXT NOT NULL DEFAULT '',
                    media_type VARCHAR(32) NULL,
                    media_file_id VARCHAR(255) NULL,
                    send_before_menu BOOLEAN NOT NULL DEFAULT TRUE,
                    send_on_every_start BOOLEAN NOT NULL DEFAULT TRUE,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS ix_pinned_messages_active ON pinned_messages(is_active);
                """

            elif db_type == "mysql":
                create_sql = """
                CREATE TABLE pinned_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    content TEXT NOT NULL DEFAULT '',
                    media_type VARCHAR(32) NULL,
                    media_file_id VARCHAR(255) NULL,
                    send_before_menu BOOLEAN NOT NULL DEFAULT TRUE,
                    send_on_every_start BOOLEAN NOT NULL DEFAULT TRUE,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_by INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_pinned_messages_active ON pinned_messages(is_active);
                """

            else:
                logger.error(f"Неподдерживаемый тип БД для создания таблицы pinned_messages: {db_type}")
                return False

            await conn.execute(text(create_sql))

        logger.info("✅ Таблица pinned_messages успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы pinned_messages: {e}")
        return False


async def ensure_pinned_message_media_columns():
    table_exists = await check_table_exists("pinned_messages")
    if not table_exists:
        logger.warning("⚠️ Таблица pinned_messages отсутствует — пропускаем обновление медиа полей")
        return False

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not await check_column_exists("pinned_messages", "media_type"):
                await conn.execute(
                    text("ALTER TABLE pinned_messages ADD COLUMN media_type VARCHAR(32)")
                )

            if not await check_column_exists("pinned_messages", "media_file_id"):
                await conn.execute(
                    text("ALTER TABLE pinned_messages ADD COLUMN media_file_id VARCHAR(255)")
                )

            if not await check_column_exists("pinned_messages", "send_before_menu"):
                default_value = "TRUE" if db_type != "sqlite" else "1"
                await conn.execute(
                    text(
                        f"ALTER TABLE pinned_messages ADD COLUMN send_before_menu BOOLEAN NOT NULL DEFAULT {default_value}"
                    )
                )

            if not await check_column_exists("pinned_messages", "send_on_every_start"):
                default_value = "TRUE" if db_type != "sqlite" else "1"
                await conn.execute(
                    text(
                        f"ALTER TABLE pinned_messages ADD COLUMN send_on_every_start BOOLEAN NOT NULL DEFAULT {default_value}"
                    )
                )

            await conn.execute(text("UPDATE pinned_messages SET content = '' WHERE content IS NULL"))

            if db_type == "postgresql":
                await conn.execute(
                    text("ALTER TABLE pinned_messages ALTER COLUMN content SET DEFAULT ''")
                )
            elif db_type == "mysql":
                await conn.execute(
                    text("ALTER TABLE pinned_messages MODIFY content TEXT NOT NULL DEFAULT ''")
                )
            else:
                logger.info("ℹ️ Пропускаем установку DEFAULT для content в SQLite")

        logger.info("✅ Медиа поля pinned_messages приведены в актуальное состояние")
        return True

    except Exception as e:
        logger.error(f"Ошибка обновления медиа полей pinned_messages: {e}")
        return False


async def ensure_user_last_pinned_column():
    try:
        async with engine.begin() as conn:
            if not await check_column_exists("users", "last_pinned_message_id"):
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN last_pinned_message_id INTEGER")
                )
        logger.info("✅ Поле last_pinned_message_id у пользователей готово")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления поля last_pinned_message_id: {e}")
        return False

async def ensure_user_bot_id_column():
    try:
        async with engine.begin() as conn:
            if not await check_column_exists("users", "bot_id"):
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN bot_id BIGINT NULL")
                )
        logger.info("✅ Поле bot_id у пользователей готово")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления поля bot_id: {e}")
        return False


async def ensure_device_link_revoked_at_column():
    try:
        async with engine.begin() as conn:
            if not await check_column_exists("device_links", "revoked_at"):
                await conn.execute(
                    text("ALTER TABLE device_links ADD COLUMN revoked_at TIMESTAMP NULL")
                )
        logger.info("✅ Поле revoked_at у привязок устройств готово")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления поля revoked_at: {e}")
        return False


async def ensure_user_web_auth_columns() -> bool:
    """Колонки для веб-аутентификации личного кабинета (email/пароль)."""

    web_auth_fields = {
        "email": "VARCHAR(255)",
        "password_hash": "VARCHAR(255)",
        "email_verified": "BOOLEAN DEFAULT FALSE",
        "auth_source": "VARCHAR(20) DEFAULT 'telegram'",
    }

    try:
        db_type = await get_database_type()

        for field_name, field_type in web_auth_fields.items():
            if await check_column_exists("users", field_name):
                continue

            column_type = field_type
            if db_type == "sqlite":
                column_type = column_type.replace(
                    "BOOLEAN DEFAULT FALSE", "BOOLEAN DEFAULT 0"
                )

            async with engine.begin() as conn:
                await conn.execute(
                    text(f"ALTER TABLE users ADD COLUMN {field_name} {column_type}")
                )
            logger.info(f"✅ Добавлена колонка {field_name} в таблицу users")

        # Уникальный индекс на email (имя совпадает с автогенерируемым SQLAlchemy)
        if not await check_index_exists("users", "ix_users_email"):
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text("CREATE UNIQUE INDEX ix_users_email ON users (email)")
                    )
                logger.info("✅ Создан уникальный индекс ix_users_email")
            except Exception as index_error:
                logger.warning(
                    f"⚠️ Не удалось создать индекс ix_users_email: {index_error}"
                )

        logger.info("✅ Колонки веб-аутентификации готовы")
        return True

    except Exception as error:
        logger.error(f"Ошибка добавления колонок веб-аутентификации: {error}")
        return False


async def relax_users_telegram_id_nullable() -> bool:
    """Снимает NOT NULL с users.telegram_id (для веб-юзеров без Telegram)."""

    try:
        db_type = await get_database_type()

        if db_type == "postgresql":
            async with engine.begin() as conn:
                result = await conn.execute(
                    text(
                        """
                        SELECT is_nullable
                        FROM information_schema.columns
                        WHERE table_name = 'users' AND column_name = 'telegram_id'
                        """
                    )
                )
                row = result.fetchone()
                if row and row[0] == "NO":
                    await conn.execute(
                        text("ALTER TABLE users ALTER COLUMN telegram_id DROP NOT NULL")
                    )
                    logger.info("✅ users.telegram_id теперь NULLABLE")
                else:
                    logger.info("ℹ️ users.telegram_id уже NULLABLE")
            return True

        if db_type == "mysql":
            async with engine.begin() as conn:
                await conn.execute(
                    text("ALTER TABLE users MODIFY COLUMN telegram_id BIGINT NULL")
                )
            logger.info("✅ users.telegram_id теперь NULLABLE")
            return True

        if db_type == "sqlite":
            # SQLite не умеет DROP NOT NULL без пересоздания таблицы.
            # SQLite используется только в dev; на проде PostgreSQL.
            logger.warning(
                "⚠️ SQLite: пропускаем снятие NOT NULL с telegram_id "
                "(требует пересоздания таблицы; используйте PostgreSQL на проде)"
            )
            return True

        logger.error(f"Неподдерживаемый тип БД для relax telegram_id: {db_type}")
        return False

    except Exception as error:
        logger.error(f"Ошибка снятия NOT NULL с telegram_id: {error}")
        return False


async def add_media_fields_to_broadcast_history():
    logger.info("=== ДОБАВЛЕНИЕ ПОЛЕЙ МЕДИА В BROADCAST_HISTORY ===")
    
    media_fields = {
        'has_media': 'BOOLEAN DEFAULT FALSE',
        'media_type': 'VARCHAR(20)',
        'media_file_id': 'VARCHAR(255)', 
        'media_caption': 'TEXT'
    }
    
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            for field_name, field_type in media_fields.items():
                field_exists = await check_column_exists('broadcast_history', field_name)
                
                if not field_exists:
                    logger.info(f"Добавление поля {field_name} в таблицу broadcast_history")
                    
                    if db_type == 'sqlite':
                        if 'BOOLEAN' in field_type:
                            field_type = field_type.replace('BOOLEAN DEFAULT FALSE', 'BOOLEAN DEFAULT 0')
                    elif db_type == 'postgresql':
                        if 'BOOLEAN' in field_type:
                            field_type = field_type.replace('BOOLEAN DEFAULT FALSE', 'BOOLEAN DEFAULT FALSE')
                    elif db_type == 'mysql':
                        if 'BOOLEAN' in field_type:
                            field_type = field_type.replace('BOOLEAN DEFAULT FALSE', 'BOOLEAN DEFAULT FALSE')
                    
                    alter_sql = f"ALTER TABLE broadcast_history ADD COLUMN {field_name} {field_type}"
                    await conn.execute(text(alter_sql))
                    logger.info(f"✅ Поле {field_name} успешно добавлено")
                else:
                    logger.info(f"Поле {field_name} уже существует в broadcast_history")
            
            logger.info("✅ Все поля медиа в broadcast_history готовы")
            return True
            
    except Exception as e:
        logger.error(f"Ошибка при добавлении полей медиа в broadcast_history: {e}")
        return False


async def add_ticket_reply_block_columns():
    try:
        col_perm_exists = await check_column_exists('tickets', 'user_reply_block_permanent')
        col_until_exists = await check_column_exists('tickets', 'user_reply_block_until')

        if col_perm_exists and col_until_exists:
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not col_perm_exists:
                if db_type == 'sqlite':
                    alter_sql = "ALTER TABLE tickets ADD COLUMN user_reply_block_permanent BOOLEAN DEFAULT 0 NOT NULL"
                elif db_type == 'postgresql':
                    alter_sql = "ALTER TABLE tickets ADD COLUMN user_reply_block_permanent BOOLEAN DEFAULT FALSE NOT NULL"
                elif db_type == 'mysql':
                    alter_sql = "ALTER TABLE tickets ADD COLUMN user_reply_block_permanent BOOLEAN DEFAULT FALSE NOT NULL"
                else:
                    logger.error(f"Неподдерживаемый тип БД для добавления user_reply_block_permanent: {db_type}")
                    return False
                await conn.execute(text(alter_sql))
                logger.info("✅ Добавлена колонка tickets.user_reply_block_permanent")

            if not col_until_exists:
                if db_type == 'sqlite':
                    alter_sql = "ALTER TABLE tickets ADD COLUMN user_reply_block_until DATETIME NULL"
                elif db_type == 'postgresql':
                    alter_sql = "ALTER TABLE tickets ADD COLUMN user_reply_block_until TIMESTAMP NULL"
                elif db_type == 'mysql':
                    alter_sql = "ALTER TABLE tickets ADD COLUMN user_reply_block_until DATETIME NULL"
                else:
                    logger.error(f"Неподдерживаемый тип БД для добавления user_reply_block_until: {db_type}")
                    return False
                await conn.execute(text(alter_sql))
                logger.info("✅ Добавлена колонка tickets.user_reply_block_until")

            return True
    except Exception as e:
        logger.error(f"Ошибка добавления колонок блокировок в tickets: {e}")
        return False


async def add_ticket_sla_columns():
    try:
        col_exists = await check_column_exists('tickets', 'last_sla_reminder_at')
        if col_exists:
            return True
        async with engine.begin() as conn:
            db_type = await get_database_type()
            if db_type == 'sqlite':
                alter_sql = "ALTER TABLE tickets ADD COLUMN last_sla_reminder_at DATETIME NULL"
            elif db_type == 'postgresql':
                alter_sql = "ALTER TABLE tickets ADD COLUMN last_sla_reminder_at TIMESTAMP NULL"
            elif db_type == 'mysql':
                alter_sql = "ALTER TABLE tickets ADD COLUMN last_sla_reminder_at DATETIME NULL"
            else:
                logger.error(f"Неподдерживаемый тип БД для добавления last_sla_reminder_at: {db_type}")
                return False
            await conn.execute(text(alter_sql))
            logger.info("✅ Добавлена колонка tickets.last_sla_reminder_at")
            return True
    except Exception as e:
        logger.error(f"Ошибка добавления SLA колонки в tickets: {e}")
        return False


async def add_subscription_crypto_link_column() -> bool:
    column_exists = await check_column_exists('subscriptions', 'subscription_crypto_link')
    if column_exists:
        logger.info("ℹ️ Колонка subscription_crypto_link уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                await conn.execute(text("ALTER TABLE subscriptions ADD COLUMN subscription_crypto_link TEXT"))
            elif db_type == 'postgresql':
                await conn.execute(text("ALTER TABLE subscriptions ADD COLUMN subscription_crypto_link VARCHAR"))
            elif db_type == 'mysql':
                await conn.execute(text("ALTER TABLE subscriptions ADD COLUMN subscription_crypto_link VARCHAR(512)"))
            else:
                logger.error(f"Неподдерживаемый тип БД для добавления subscription_crypto_link: {db_type}")
                return False

            await conn.execute(text(
                "UPDATE subscriptions SET subscription_crypto_link = subscription_url "
                "WHERE subscription_crypto_link IS NULL OR subscription_crypto_link = ''"
            ))

        logger.info("✅ Добавлена колонка subscription_crypto_link в таблицу subscriptions")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления колонки subscription_crypto_link: {e}")
        return False


async def fix_foreign_keys_for_user_deletion():
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            if db_type == 'postgresql':
                try:
                    await conn.execute(text("""
                        ALTER TABLE user_messages 
                        DROP CONSTRAINT IF EXISTS user_messages_created_by_fkey;
                    """))
                    
                    await conn.execute(text("""
                        ALTER TABLE user_messages 
                        ADD CONSTRAINT user_messages_created_by_fkey 
                        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL;
                    """))
                    logger.info("Обновлен внешний ключ user_messages.created_by")
                except Exception as e:
                    logger.warning(f"Ошибка обновления FK user_messages: {e}")
                
                try:
                    await conn.execute(text("""
                        ALTER TABLE promocodes 
                        DROP CONSTRAINT IF EXISTS promocodes_created_by_fkey;
                    """))
                    
                    await conn.execute(text("""
                        ALTER TABLE promocodes 
                        ADD CONSTRAINT promocodes_created_by_fkey 
                        FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL;
                    """))
                    logger.info("Обновлен внешний ключ promocodes.created_by")
                except Exception as e:
                    logger.warning(f"Ошибка обновления FK promocodes: {e}")
            
            logger.info("Внешние ключи обновлены для безопасного удаления пользователей")
            return True
            
    except Exception as e:
        logger.error(f"Ошибка обновления внешних ключей: {e}")
        return False

async def add_referral_commission_percent_column() -> bool:
    column_exists = await check_column_exists('users', 'referral_commission_percent')
    if column_exists:
        logger.info("ℹ️ Колонка referral_commission_percent уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                alter_sql = "ALTER TABLE users ADD COLUMN referral_commission_percent INTEGER NULL"
            elif db_type == 'postgresql':
                alter_sql = "ALTER TABLE users ADD COLUMN referral_commission_percent INTEGER NULL"
            elif db_type == 'mysql':
                alter_sql = "ALTER TABLE users ADD COLUMN referral_commission_percent INT NULL"
            else:
                logger.error(f"Неподдерживаемый тип БД для добавления referral_commission_percent: {db_type}")
                return False

            await conn.execute(text(alter_sql))
            logger.info("✅ Добавлена колонка referral_commission_percent в таблицу users")
            return True

    except Exception as error:
        logger.error(f"Ошибка добавления referral_commission_percent: {error}")
        return False


async def add_referral_qualified_columns() -> bool:
    """Добавляет колонки для учёта "качественных" рефералов партнёра.

    - users.referral_total_topup_kopeks — суммарные пополнения реферала.
    - users.qualified_referrals_count — счётчик рефералов партнёра, внёсших
      суммарно более 1000₽.

    При первом добавлении выполняется бэкфилл по существующим данным транзакций.
    """
    try:
        total_exists = await check_column_exists('users', 'referral_total_topup_kopeks')
        count_exists = await check_column_exists('users', 'qualified_referrals_count')

        if total_exists and count_exists:
            logger.info("ℹ️ Колонки качественных рефералов уже существуют")
            return True

        threshold_kopeks = 100000  # 1000₽

        async with engine.begin() as conn:
            db_type = await get_database_type()
            true_literal = "1" if db_type == 'sqlite' else "TRUE"

            if not total_exists:
                if db_type == 'mysql':
                    total_def = "BIGINT NOT NULL DEFAULT 0"
                else:
                    total_def = "BIGINT NOT NULL DEFAULT 0"
                await conn.execute(text(
                    f"ALTER TABLE users ADD COLUMN referral_total_topup_kopeks {total_def}"
                ))
                logger.info("✅ Добавлена колонка users.referral_total_topup_kopeks")

            if not count_exists:
                count_def = "INTEGER NOT NULL DEFAULT 0" if db_type != 'mysql' else "INT NOT NULL DEFAULT 0"
                await conn.execute(text(
                    f"ALTER TABLE users ADD COLUMN qualified_referrals_count {count_def}"
                ))
                logger.info("✅ Добавлена колонка users.qualified_referrals_count")

            # Бэкфилл суммарных пополнений рефералов на основе транзакций пополнения
            await conn.execute(text(
                f"""
                UPDATE users
                SET referral_total_topup_kopeks = (
                    SELECT COALESCE(SUM(t.amount_kopeks), 0)
                    FROM transactions t
                    WHERE t.user_id = users.id
                      AND t.type = 'deposit'
                      AND t.is_completed = {true_literal}
                )
                WHERE referred_by_id IS NOT NULL
                """
            ))

            # Бэкфилл счётчика качественных рефералов у каждого партнёра
            await conn.execute(text(
                f"""
                UPDATE users
                SET qualified_referrals_count = (
                    SELECT COUNT(*)
                    FROM users AS ref
                    WHERE ref.referred_by_id = users.id
                      AND ref.referral_total_topup_kopeks > {threshold_kopeks}
                )
                """
            ))

            logger.info("✅ Бэкфилл качественных рефералов завершён")
            return True

    except Exception as error:
        logger.error(f"Ошибка добавления колонок качественных рефералов: {error}")
        return False


async def add_referral_system_columns():
    logger.info("=== МИГРАЦИЯ РЕФЕРАЛЬНОЙ СИСТЕМЫ ===")
    
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            column_exists = await check_column_exists('users', 'has_made_first_topup')
            
            if not column_exists:
                logger.info("Добавление колонки has_made_first_topup в таблицу users")
                
                if db_type == 'sqlite':
                    column_def = 'BOOLEAN DEFAULT 0'
                else:
                    column_def = 'BOOLEAN DEFAULT FALSE'
                
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN has_made_first_topup {column_def}"))
                logger.info("Колонка has_made_first_topup успешно добавлена")
                
                logger.info("Обновление существующих пользователей...")
                
                if db_type == 'sqlite':
                    update_sql = """
                        UPDATE users 
                        SET has_made_first_topup = 1 
                        WHERE balance_kopeks > 0 OR has_had_paid_subscription = 1
                    """
                else:
                    update_sql = """
                        UPDATE users 
                        SET has_made_first_topup = TRUE 
                        WHERE balance_kopeks > 0 OR has_had_paid_subscription = TRUE
                    """
                
                result = await conn.execute(text(update_sql))
                updated_count = result.rowcount
                
                logger.info(f"Обновлено {updated_count} пользователей с has_made_first_topup = TRUE")
                logger.info("✅ Миграция реферальной системы завершена")
                
                return True
            else:
                logger.info("Колонка has_made_first_topup уже существует")
                return True
                
    except Exception as e:
        logger.error(f"Ошибка миграции реферальной системы: {e}")
        return False

async def create_subscription_conversions_table():
    table_exists = await check_table_exists('subscription_conversions')
    if table_exists:
        logger.info("Таблица subscription_conversions уже существует")
        return True
    
    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()
            
            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE subscription_conversions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    converted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    trial_duration_days INTEGER NULL,
                    payment_method VARCHAR(50) NULL,
                    first_payment_amount_kopeks INTEGER NULL,
                    first_paid_period_days INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                
                CREATE INDEX idx_subscription_conversions_user_id ON subscription_conversions(user_id);
                CREATE INDEX idx_subscription_conversions_converted_at ON subscription_conversions(converted_at);
                """
                
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE subscription_conversions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    converted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    trial_duration_days INTEGER NULL,
                    payment_method VARCHAR(50) NULL,
                    first_payment_amount_kopeks INTEGER NULL,
                    first_paid_period_days INTEGER NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                
                CREATE INDEX idx_subscription_conversions_user_id ON subscription_conversions(user_id);
                CREATE INDEX idx_subscription_conversions_converted_at ON subscription_conversions(converted_at);
                """
                
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE subscription_conversions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    converted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    trial_duration_days INT NULL,
                    payment_method VARCHAR(50) NULL,
                    first_payment_amount_kopeks INT NULL,
                    first_paid_period_days INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                
                CREATE INDEX idx_subscription_conversions_user_id ON subscription_conversions(user_id);
                CREATE INDEX idx_subscription_conversions_converted_at ON subscription_conversions(converted_at);
                """
            else:
                logger.error(f"Неподдерживаемый тип БД для создания таблицы: {db_type}")
                return False
            
            await conn.execute(text(create_sql))
            logger.info("✅ Таблица subscription_conversions успешно создана")
            return True
            
    except Exception as e:
        logger.error(f"Ошибка создания таблицы subscription_conversions: {e}")
        return False


async def create_subscription_events_table():
    table_exists = await check_table_exists("subscription_events")
    if table_exists:
        logger.info("Таблица subscription_events уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE subscription_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type VARCHAR(50) NOT NULL,
                    user_id INTEGER NOT NULL,
                    subscription_id INTEGER NULL,
                    transaction_id INTEGER NULL,
                    amount_kopeks INTEGER NULL,
                    currency VARCHAR(16) NULL,
                    message TEXT NULL,
                    occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    extra JSON NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL,
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_subscription_events_event_type ON subscription_events(event_type);
                CREATE INDEX ix_subscription_events_user_id ON subscription_events(user_id);
                """

            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE subscription_events (
                    id SERIAL PRIMARY KEY,
                    event_type VARCHAR(50) NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    subscription_id INTEGER NULL REFERENCES subscriptions(id) ON DELETE SET NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id) ON DELETE SET NULL,
                    amount_kopeks INTEGER NULL,
                    currency VARCHAR(16) NULL,
                    message TEXT NULL,
                    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    extra JSON NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX ix_subscription_events_event_type ON subscription_events(event_type);
                CREATE INDEX ix_subscription_events_user_id ON subscription_events(user_id);
                """

            elif db_type == "mysql":
                create_sql = """
                CREATE TABLE subscription_events (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    event_type VARCHAR(50) NOT NULL,
                    user_id INT NOT NULL,
                    subscription_id INT NULL,
                    transaction_id INT NULL,
                    amount_kopeks INT NULL,
                    currency VARCHAR(16) NULL,
                    message TEXT NULL,
                    occurred_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    extra JSON NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL,
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_subscription_events_event_type ON subscription_events(event_type);
                CREATE INDEX ix_subscription_events_user_id ON subscription_events(user_id);
                """
            else:
                logger.error(f"Неподдерживаемый тип БД для создания таблицы subscription_events: {db_type}")
                return False

            await conn.execute(text(create_sql))
            logger.info("✅ Таблица subscription_events успешно создана")
            return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы subscription_events: {e}")
        return False


async def create_feedbacks_table() -> bool:
    table_exists = await check_table_exists("feedbacks")
    if table_exists:
        logger.info("Таблица feedbacks уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE feedbacks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type VARCHAR(50) NOT NULL,
                    user_id INTEGER NULL,
                    subscription_id INTEGER NULL,
                    event_key VARCHAR(255) NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'sent',
                    message_id INTEGER NULL,
                    selected_option VARCHAR(100) NULL,
                    answer TEXT NULL,
                    context JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
                );

                CREATE UNIQUE INDEX uq_feedbacks_event_key ON feedbacks(event_key);
                CREATE INDEX ix_feedbacks_type ON feedbacks(type);
                CREATE INDEX ix_feedbacks_user_id ON feedbacks(user_id);
                CREATE INDEX ix_feedbacks_subscription_id ON feedbacks(subscription_id);
                """

            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE feedbacks (
                    id SERIAL PRIMARY KEY,
                    type VARCHAR(50) NOT NULL,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    subscription_id INTEGER NULL REFERENCES subscriptions(id) ON DELETE SET NULL,
                    event_key VARCHAR(255) NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'sent',
                    message_id INTEGER NULL,
                    selected_option VARCHAR(100) NULL,
                    answer TEXT NULL,
                    context JSON NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE UNIQUE INDEX uq_feedbacks_event_key ON feedbacks(event_key);
                CREATE INDEX ix_feedbacks_type ON feedbacks(type);
                CREATE INDEX ix_feedbacks_user_id ON feedbacks(user_id);
                CREATE INDEX ix_feedbacks_subscription_id ON feedbacks(subscription_id);
                """

            elif db_type == "mysql":
                create_sql = """
                CREATE TABLE feedbacks (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    type VARCHAR(50) NOT NULL,
                    user_id INT NULL,
                    subscription_id INT NULL,
                    event_key VARCHAR(255) NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'sent',
                    message_id INT NULL,
                    selected_option VARCHAR(100) NULL,
                    answer TEXT NULL,
                    context JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
                );

                CREATE UNIQUE INDEX uq_feedbacks_event_key ON feedbacks(event_key);
                CREATE INDEX ix_feedbacks_type ON feedbacks(type);
                CREATE INDEX ix_feedbacks_user_id ON feedbacks(user_id);
                CREATE INDEX ix_feedbacks_subscription_id ON feedbacks(subscription_id);
                """
            else:
                logger.error(f"Неподдерживаемый тип БД для создания таблицы feedbacks: {db_type}")
                return False

            await conn.execute(text(create_sql))
            logger.info("✅ Таблица feedbacks успешно создана")
            return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы feedbacks: {e}")
        return False


async def create_interactive_notification_logs_table() -> bool:
    table_exists = await check_table_exists("interactive_notification_logs")
    if table_exists:
        logger.info("Таблица interactive_notification_logs уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE interactive_notification_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_key VARCHAR(50) NOT NULL,
                    user_id INTEGER NULL,
                    telegram_id BIGINT NULL,
                    message_id INTEGER NULL,
                    status VARCHAR(50) NOT NULL,
                    error TEXT NULL,
                    payload JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_interactive_notification_logs_slot_key ON interactive_notification_logs(slot_key);
                CREATE INDEX ix_interactive_notification_logs_user_id ON interactive_notification_logs(user_id);
                CREATE INDEX ix_interactive_notification_logs_slot_key_user_id ON interactive_notification_logs(slot_key, user_id);
                CREATE INDEX ix_interactive_notification_logs_status ON interactive_notification_logs(status);
                """
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE IF NOT EXISTS interactive_notification_logs (
                    id SERIAL PRIMARY KEY,
                    slot_key VARCHAR(50) NOT NULL,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    telegram_id BIGINT NULL,
                    message_id INTEGER NULL,
                    status VARCHAR(50) NOT NULL,
                    error TEXT NULL,
                    payload JSON NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS ix_interactive_notification_logs_slot_key ON interactive_notification_logs(slot_key);
                CREATE INDEX IF NOT EXISTS ix_interactive_notification_logs_user_id ON interactive_notification_logs(user_id);
                CREATE INDEX IF NOT EXISTS ix_interactive_notification_logs_slot_key_user_id ON interactive_notification_logs(slot_key, user_id);
                CREATE INDEX IF NOT EXISTS ix_interactive_notification_logs_status ON interactive_notification_logs(status);
                """
            elif db_type == "mysql":
                create_sql = """
                CREATE TABLE IF NOT EXISTS interactive_notification_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    slot_key VARCHAR(50) NOT NULL,
                    user_id INT NULL,
                    telegram_id BIGINT NULL,
                    message_id INT NULL,
                    status VARCHAR(50) NOT NULL,
                    error TEXT NULL,
                    payload JSON NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_interactive_notification_logs_slot_key ON interactive_notification_logs(slot_key);
                CREATE INDEX ix_interactive_notification_logs_user_id ON interactive_notification_logs(user_id);
                CREATE INDEX ix_interactive_notification_logs_slot_key_user_id ON interactive_notification_logs(slot_key, user_id);
                CREATE INDEX ix_interactive_notification_logs_status ON interactive_notification_logs(status);
                """
            else:
                logger.error(f"Неподдерживаемый тип БД для создания interactive_notification_logs: {db_type}")
                return False

            for statement in [s.strip() for s in create_sql.split(";") if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица interactive_notification_logs успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы interactive_notification_logs: {e}")
        return False


async def ensure_interactive_notification_logs_campaign_index() -> bool:
    table_name = "interactive_notification_logs"
    index_name = "ix_interactive_notification_logs_slot_key_user_id"
    if not await check_table_exists(table_name) or await check_index_exists(table_name, index_name):
        return True

    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE INDEX {index_name} "
                    "ON interactive_notification_logs(slot_key, user_id)"
                )
            )
        logger.info("✅ Составной индекс логов интерактивных уведомлений создан")
        return True
    except Exception as e:
        logger.error("Ошибка создания составного индекса логов интерактивных уведомлений: %s", e)
        return False


async def create_platega_unpaid_created_index() -> bool:
    if not await check_table_exists("platega_payments"):
        return True
    index_name = "ix_platega_payments_unpaid_created"
    if await check_index_exists("platega_payments", index_name):
        return True
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE INDEX {index_name} "
                    "ON platega_payments(is_paid, created_at)"
                )
            )
        logger.info("✅ Индекс неоплаченных Platega счетов создан")
        return True
    except Exception as e:
        logger.error("Ошибка создания индекса неоплаченных Platega счетов: %s", e)
        return False


async def create_platega_subscriptions_table() -> bool:
    if await check_table_exists("platega_subscriptions"):
        logger.info("Таблица platega_subscriptions уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE platega_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    active_user_id INTEGER NULL,
                    platega_subscription_id VARCHAR(255) NOT NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
                    redirect_url TEXT NULL,
                    next_charge_at DATETIME NULL,
                    last_callback_payload JSON NULL,
                    cancelled_at DATETIME NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE platega_subscriptions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    active_user_id INTEGER NULL,
                    platega_subscription_id VARCHAR(255) NOT NULL,
                    amount_kopeks INTEGER NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
                    redirect_url TEXT NULL,
                    next_charge_at TIMESTAMP NULL,
                    last_callback_payload JSON NULL,
                    cancelled_at TIMESTAMP NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            elif db_type == "mysql":
                create_sql = """
                CREATE TABLE platega_subscriptions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    active_user_id INT NULL,
                    platega_subscription_id VARCHAR(255) NOT NULL,
                    amount_kopeks INT NOT NULL,
                    currency VARCHAR(10) NOT NULL DEFAULT 'RUB',
                    description TEXT NULL,
                    status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
                    redirect_url TEXT NULL,
                    next_charge_at DATETIME NULL,
                    last_callback_payload JSON NULL,
                    cancelled_at DATETIME NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            else:
                logger.error(
                    "Неподдерживаемый тип БД для создания platega_subscriptions: %s",
                    db_type,
                )
                return False

            await conn.execute(text(create_sql))

        logger.info("✅ Таблица platega_subscriptions успешно создана")
        return True
    except Exception as error:
        logger.error("Ошибка создания таблицы platega_subscriptions: %s", error)
        return False


async def add_platega_payment_subscription_id() -> bool:
    if not await check_table_exists("platega_payments"):
        logger.warning(
            "Таблица platega_payments не найдена, связь с регулярными списаниями пропущена"
        )
        return False

    try:
        column_exists = await check_column_exists("platega_payments", "subscription_id")
        db_type = await get_database_type()
        constraint_exists = (
            await check_constraint_exists(
                "platega_payments", "fk_platega_payments_subscription_id"
            )
            if db_type in {"postgresql", "mysql"}
            else True
        )

        async with engine.begin() as conn:
            if not column_exists:
                await conn.execute(
                    text("ALTER TABLE platega_payments ADD COLUMN subscription_id INTEGER NULL")
                )

            # SQLite does not support adding a foreign key constraint to an existing table.
            if db_type in {"postgresql", "mysql"} and not constraint_exists:
                await conn.execute(
                    text(
                        "ALTER TABLE platega_payments "
                        "ADD CONSTRAINT fk_platega_payments_subscription_id "
                        "FOREIGN KEY (subscription_id) REFERENCES platega_subscriptions(id) "
                        "ON DELETE SET NULL"
                    )
                )

        logger.info("✅ Колонка subscription_id в platega_payments готова")
        return True
    except Exception as error:
        logger.error("Ошибка добавления subscription_id в platega_payments: %s", error)
        return False


async def add_platega_subscription_active_user_id() -> bool:
    if not await check_table_exists("platega_subscriptions"):
        logger.warning("Таблица platega_subscriptions не найдена, активный слот не добавлен")
        return False

    try:
        if not await check_column_exists("platega_subscriptions", "active_user_id"):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "ALTER TABLE platega_subscriptions "
                        "ADD COLUMN active_user_id INTEGER NULL"
                    )
                )

        logger.info("✅ Колонка active_user_id в platega_subscriptions готова")
        return True
    except Exception as error:
        logger.error("Ошибка добавления active_user_id в platega_subscriptions: %s", error)
        return False


async def ensure_platega_subscription_indexes() -> bool:
    indexes = (
        (
            "platega_subscriptions",
            "ix_platega_subscriptions_platega_subscription_id",
            "CREATE UNIQUE INDEX ix_platega_subscriptions_platega_subscription_id "
            "ON platega_subscriptions(platega_subscription_id)",
        ),
        (
            "platega_subscriptions",
            "ix_platega_subscriptions_user_status",
            "CREATE INDEX ix_platega_subscriptions_user_status "
            "ON platega_subscriptions(user_id, status)",
        ),
        (
            "platega_subscriptions",
            "ix_platega_subscriptions_active_user_id",
            "CREATE UNIQUE INDEX ix_platega_subscriptions_active_user_id "
            "ON platega_subscriptions(active_user_id)",
        ),
        (
            "platega_payments",
            "ix_platega_payments_subscription_id",
            "CREATE INDEX ix_platega_payments_subscription_id "
            "ON platega_payments(subscription_id)",
        ),
    )

    try:
        missing_tables = [
            table_name
            for table_name in {item[0] for item in indexes}
            if not await check_table_exists(table_name)
        ]
        if missing_tables:
            logger.warning(
                "Таблицы %s не найдены, индексы регулярных подписок Platega не созданы",
                ", ".join(sorted(missing_tables)),
            )
            return False

        missing_indexes = [
            (index_name, create_sql)
            for table_name, index_name, create_sql in indexes
            if not await check_index_exists(table_name, index_name)
        ]

        async with engine.begin() as conn:
            for _, create_sql in missing_indexes:
                await conn.execute(text(create_sql))

        logger.info("✅ Индексы регулярных подписок Platega готовы")
        return True
    except Exception as error:
        logger.error("Ошибка создания индексов регулярных подписок Platega: %s", error)
        return False


async def create_subscription_short_uuid_index() -> bool:
    """Индекс под вход в приложение по коду/ссылке подписки (/sub/<short_uuid>)."""
    if not await check_table_exists("subscriptions"):
        return True
    index_name = "ix_subscriptions_remnawave_short_uuid"
    if await check_index_exists("subscriptions", index_name):
        return True
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE INDEX {index_name} "
                    "ON subscriptions(remnawave_short_uuid)"
                )
            )
        logger.info("✅ Индекс subscriptions.remnawave_short_uuid создан")
        return True
    except Exception as e:
        logger.error("Ошибка создания индекса subscriptions.remnawave_short_uuid: %s", e)
        return False


async def create_android_rate_request_clicks_table() -> bool:
    table_exists = await check_table_exists("android_rate_request_clicks")
    if table_exists:
        unique_index_exists = await check_index_exists(
            "android_rate_request_clicks",
            "uq_android_rate_request_clicks_sent_notification_id",
        )
        if not unique_index_exists:
            try:
                async with engine.begin() as conn:
                    db_type = await get_database_type()
                    if db_type in {"sqlite", "postgresql"}:
                        await conn.execute(
                            text(
                                "CREATE UNIQUE INDEX IF NOT EXISTS "
                                "uq_android_rate_request_clicks_sent_notification_id "
                                "ON android_rate_request_clicks(sent_notification_id)"
                            )
                        )
                    elif db_type == "mysql":
                        await conn.execute(
                            text(
                                "CREATE UNIQUE INDEX "
                                "uq_android_rate_request_clicks_sent_notification_id "
                                "ON android_rate_request_clicks(sent_notification_id)"
                            )
                        )
                    else:
                        logger.error(
                            f"Неподдерживаемый тип БД для индекса android_rate_request_clicks: {db_type}"
                        )
                        return False
                logger.info(
                    "✅ Создан уникальный индекс uq_android_rate_request_clicks_sent_notification_id"
                )
            except Exception as e:
                logger.error(
                    "Ошибка создания уникального индекса android_rate_request_clicks: %s",
                    e,
                )
                return False

        logger.info("Таблица android_rate_request_clicks уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE android_rate_request_clicks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sent_notification_id INTEGER NULL,
                    user_id INTEGER NULL,
                    telegram_id BIGINT NULL,
                    message_id INTEGER NULL,
                    callback_query_id VARCHAR(255) NULL,
                    review_url TEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (sent_notification_id) REFERENCES sent_notifications(id) ON DELETE SET NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE UNIQUE INDEX uq_android_rate_request_clicks_sent_notification_id ON android_rate_request_clicks(sent_notification_id);
                CREATE INDEX ix_android_rate_request_clicks_user_id ON android_rate_request_clicks(user_id);
                CREATE INDEX ix_android_rate_request_clicks_telegram_id ON android_rate_request_clicks(telegram_id);
                CREATE INDEX ix_android_rate_request_clicks_created_at ON android_rate_request_clicks(created_at);
                """
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE IF NOT EXISTS android_rate_request_clicks (
                    id SERIAL PRIMARY KEY,
                    sent_notification_id INTEGER NULL REFERENCES sent_notifications(id) ON DELETE SET NULL,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    telegram_id BIGINT NULL,
                    message_id INTEGER NULL,
                    callback_query_id VARCHAR(255) NULL,
                    review_url TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_android_rate_request_clicks_sent_notification_id ON android_rate_request_clicks(sent_notification_id);
                CREATE INDEX IF NOT EXISTS ix_android_rate_request_clicks_user_id ON android_rate_request_clicks(user_id);
                CREATE INDEX IF NOT EXISTS ix_android_rate_request_clicks_telegram_id ON android_rate_request_clicks(telegram_id);
                CREATE INDEX IF NOT EXISTS ix_android_rate_request_clicks_created_at ON android_rate_request_clicks(created_at);
                """
            elif db_type == "mysql":
                create_sql = """
                CREATE TABLE IF NOT EXISTS android_rate_request_clicks (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    sent_notification_id INT NULL,
                    user_id INT NULL,
                    telegram_id BIGINT NULL,
                    message_id INT NULL,
                    callback_query_id VARCHAR(255) NULL,
                    review_url TEXT NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (sent_notification_id) REFERENCES sent_notifications(id) ON DELETE SET NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE UNIQUE INDEX uq_android_rate_request_clicks_sent_notification_id ON android_rate_request_clicks(sent_notification_id);
                CREATE INDEX ix_android_rate_request_clicks_user_id ON android_rate_request_clicks(user_id);
                CREATE INDEX ix_android_rate_request_clicks_telegram_id ON android_rate_request_clicks(telegram_id);
                CREATE INDEX ix_android_rate_request_clicks_created_at ON android_rate_request_clicks(created_at);
                """
            else:
                logger.error(f"Неподдерживаемый тип БД для создания android_rate_request_clicks: {db_type}")
                return False

            for statement in [s.strip() for s in create_sql.split(";") if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица android_rate_request_clicks успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы android_rate_request_clicks: {e}")
        return False


async def fix_subscription_duplicates_universal():
    async with engine.begin() as conn:
        db_type = await get_database_type()
        logger.info(f"Обнаружен тип базы данных: {db_type}")
        
        try:
            result = await conn.execute(text("""
                SELECT user_id, COUNT(*) as count 
                FROM subscriptions 
                GROUP BY user_id 
                HAVING COUNT(*) > 1
            """))
            
            duplicates = result.fetchall()
            
            if not duplicates:
                logger.info("Дублирующихся подписок не найдено")
                return 0
                
            logger.info(f"Найдено {len(duplicates)} пользователей с дублирующимися подписками")
            
            total_deleted = 0
            
            for user_id_row, count in duplicates:
                user_id = user_id_row
                
                if db_type == 'sqlite':
                    delete_result = await conn.execute(text("""
                        DELETE FROM subscriptions 
                        WHERE user_id = :user_id AND id NOT IN (
                            SELECT MAX(id) 
                            FROM subscriptions 
                            WHERE user_id = :user_id
                        )
                    """), {"user_id": user_id})
                    
                elif db_type in ['postgresql', 'mysql']:
                    delete_result = await conn.execute(text("""
                        DELETE FROM subscriptions 
                        WHERE user_id = :user_id AND id NOT IN (
                            SELECT max_id FROM (
                                SELECT MAX(id) as max_id
                                FROM subscriptions 
                                WHERE user_id = :user_id
                            ) as subquery
                        )
                    """), {"user_id": user_id})
                
                else:
                    subs_result = await conn.execute(text("""
                        SELECT id FROM subscriptions 
                        WHERE user_id = :user_id 
                        ORDER BY created_at DESC, id DESC
                    """), {"user_id": user_id})
                    
                    sub_ids = [row[0] for row in subs_result.fetchall()]
                    
                    if len(sub_ids) > 1:
                        ids_to_delete = sub_ids[1:]
                        for sub_id in ids_to_delete:
                            await conn.execute(text("""
                                DELETE FROM subscriptions WHERE id = :id
                            """), {"id": sub_id})
                        delete_result = type('Result', (), {'rowcount': len(ids_to_delete)})()
                    else:
                        delete_result = type('Result', (), {'rowcount': 0})()
                
                deleted_count = delete_result.rowcount
                total_deleted += deleted_count
                logger.info(f"Удалено {deleted_count} дублирующихся подписок для пользователя {user_id}")

            logger.info(f"Всего удалено дублирующихся подписок: {total_deleted}")
            return total_deleted

        except Exception as e:
            logger.error(f"Ошибка при очистке дублирующихся подписок: {e}")
            raise


async def ensure_server_promo_groups_setup() -> bool:
    logger.info("=== НАСТРОЙКА ДОСТУПА СЕРВЕРОВ К ПРОМОГРУППАМ ===")

    try:
        table_exists = await check_table_exists("server_squad_promo_groups")

        async with engine.begin() as conn:
            db_type = await get_database_type()

            if not table_exists:
                if db_type == "sqlite":
                    create_table_sql = """
                    CREATE TABLE server_squad_promo_groups (
                        server_squad_id INTEGER NOT NULL,
                        promo_group_id INTEGER NOT NULL,
                        PRIMARY KEY (server_squad_id, promo_group_id),
                        FOREIGN KEY (server_squad_id) REFERENCES server_squads(id) ON DELETE CASCADE,
                        FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                    );
                    """
                    create_index_sql = """
                    CREATE INDEX IF NOT EXISTS idx_server_squad_promo_groups_promo ON server_squad_promo_groups(promo_group_id);
                    """
                elif db_type == "postgresql":
                    create_table_sql = """
                    CREATE TABLE server_squad_promo_groups (
                        server_squad_id INTEGER NOT NULL REFERENCES server_squads(id) ON DELETE CASCADE,
                        promo_group_id INTEGER NOT NULL REFERENCES promo_groups(id) ON DELETE CASCADE,
                        PRIMARY KEY (server_squad_id, promo_group_id)
                    );
                    """
                    create_index_sql = """
                    CREATE INDEX IF NOT EXISTS idx_server_squad_promo_groups_promo ON server_squad_promo_groups(promo_group_id);
                    """
                else:
                    create_table_sql = """
                    CREATE TABLE server_squad_promo_groups (
                        server_squad_id INT NOT NULL,
                        promo_group_id INT NOT NULL,
                        PRIMARY KEY (server_squad_id, promo_group_id),
                        FOREIGN KEY (server_squad_id) REFERENCES server_squads(id) ON DELETE CASCADE,
                        FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                    );
                    """
                    create_index_sql = """
                    CREATE INDEX IF NOT EXISTS idx_server_squad_promo_groups_promo ON server_squad_promo_groups(promo_group_id);
                    """

                await conn.execute(text(create_table_sql))
                await conn.execute(text(create_index_sql))
                logger.info("✅ Таблица server_squad_promo_groups создана")
            else:
                logger.info("ℹ️ Таблица server_squad_promo_groups уже существует")

            default_query = (
                "SELECT id FROM promo_groups WHERE is_default IS TRUE LIMIT 1"
                if db_type == "postgresql"
                else "SELECT id FROM promo_groups WHERE is_default = 1 LIMIT 1"
            )
            default_result = await conn.execute(text(default_query))
            default_row = default_result.fetchone()

            if not default_row:
                logger.warning("⚠️ Не найдена базовая промогруппа для назначения серверам")
                return True

            default_group_id = default_row[0]

            servers_result = await conn.execute(text("SELECT id FROM server_squads"))
            server_ids = [row[0] for row in servers_result.fetchall()]

            assigned_count = 0
            for server_id in server_ids:
                existing = await conn.execute(
                    text(
                        "SELECT 1 FROM server_squad_promo_groups WHERE server_squad_id = :sid LIMIT 1"
                    ),
                    {"sid": server_id},
                )
                if existing.fetchone():
                    continue

                await conn.execute(
                    text(
                        "INSERT INTO server_squad_promo_groups (server_squad_id, promo_group_id) "
                        "VALUES (:sid, :gid)"
                    ),
                    {"sid": server_id, "gid": default_group_id},
                )
                assigned_count += 1

            if assigned_count:
                logger.info(
                    f"✅ Базовая промогруппа назначена {assigned_count} серверам"
                )
            else:
                logger.info("ℹ️ Все серверы уже имеют назначенные промогруппы")

        return True

    except Exception as e:
        logger.error(
            f"Ошибка настройки таблицы server_squad_promo_groups: {e}"
        )
        return False


async def add_server_trial_flag_column() -> bool:
    column_exists = await check_column_exists('server_squads', 'is_trial_eligible')
    if column_exists:
        logger.info("Колонка is_trial_eligible уже существует в server_squads")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                column_def = 'BOOLEAN NOT NULL DEFAULT 0'
            elif db_type == 'postgresql':
                column_def = 'BOOLEAN NOT NULL DEFAULT FALSE'
            else:
                column_def = 'BOOLEAN NOT NULL DEFAULT FALSE'

            await conn.execute(
                text(f"ALTER TABLE server_squads ADD COLUMN is_trial_eligible {column_def}")
            )

            if db_type == 'postgresql':
                await conn.execute(
                    text("ALTER TABLE server_squads ALTER COLUMN is_trial_eligible SET DEFAULT FALSE")
                )

        logger.info("✅ Добавлена колонка is_trial_eligible в server_squads")
        return True

    except Exception as error:
        logger.error(f"Ошибка добавления колонки is_trial_eligible: {error}")
        return False


async def create_system_settings_table() -> bool:
    table_exists = await check_table_exists("system_settings")
    if table_exists:
        logger.info("ℹ️ Таблица system_settings уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE system_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key VARCHAR(255) NOT NULL UNIQUE,
                    value TEXT NULL,
                    description TEXT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE system_settings (
                    id SERIAL PRIMARY KEY,
                    key VARCHAR(255) NOT NULL UNIQUE,
                    value TEXT NULL,
                    description TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            else:
                create_sql = """
                CREATE TABLE system_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    key VARCHAR(255) NOT NULL UNIQUE,
                    value TEXT NULL,
                    description TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """

            await conn.execute(text(create_sql))
            logger.info("✅ Таблица system_settings создана")
            return True

    except Exception as error:
        logger.error(f"Ошибка создания таблицы system_settings: {error}")
        return False


async def create_menu_layout_history_table() -> bool:
    """Создаёт таблицу для хранения истории изменений конфигурации меню."""
    table_exists = await check_table_exists("menu_layout_history")
    if table_exists:
        logger.info("ℹ️ Таблица menu_layout_history уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_table_sql = """
                CREATE TABLE menu_layout_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_json TEXT NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    changes_summary TEXT NULL,
                    user_info VARCHAR(255) NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            elif db_type == "postgresql":
                create_table_sql = """
                CREATE TABLE menu_layout_history (
                    id SERIAL PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    changes_summary TEXT NULL,
                    user_info VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
                """
            else:
                create_table_sql = """
                CREATE TABLE menu_layout_history (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    action VARCHAR(50) NOT NULL,
                    changes_summary TEXT NULL,
                    user_info VARCHAR(255) NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB
                """

            await conn.execute(text(create_table_sql))
            await conn.execute(text(
                "CREATE INDEX ix_menu_layout_history_created ON menu_layout_history(created_at)"
            ))
            logger.info("✅ Таблица menu_layout_history создана")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания таблицы menu_layout_history: {error}")
        return False


async def create_button_click_logs_table() -> bool:
    """Создаёт таблицу для логирования кликов по кнопкам меню."""
    table_exists = await check_table_exists("button_click_logs")
    if table_exists:
        logger.info("ℹ️ Таблица button_click_logs уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_table_sql = """
                CREATE TABLE button_click_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    button_id VARCHAR(100) NOT NULL,
                    user_id BIGINT NULL REFERENCES users(telegram_id) ON DELETE SET NULL,
                    callback_data VARCHAR(255) NULL,
                    clicked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    button_type VARCHAR(20) NULL,
                    button_text VARCHAR(255) NULL
                )
                """
            elif db_type == "postgresql":
                create_table_sql = """
                CREATE TABLE button_click_logs (
                    id SERIAL PRIMARY KEY,
                    button_id VARCHAR(100) NOT NULL,
                    user_id BIGINT NULL REFERENCES users(telegram_id) ON DELETE SET NULL,
                    callback_data VARCHAR(255) NULL,
                    clicked_at TIMESTAMP DEFAULT NOW(),
                    button_type VARCHAR(20) NULL,
                    button_text VARCHAR(255) NULL
                )
                """
            else:
                create_table_sql = """
                CREATE TABLE button_click_logs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    button_id VARCHAR(100) NOT NULL,
                    user_id BIGINT NULL,
                    callback_data VARCHAR(255) NULL,
                    clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    button_type VARCHAR(20) NULL,
                    button_text VARCHAR(255) NULL,
                    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE SET NULL
                ) ENGINE=InnoDB
                """

            await conn.execute(text(create_table_sql))

            # Создаём индексы отдельными запросами
            index_statements = [
                "CREATE INDEX ix_button_click_logs_button_id ON button_click_logs(button_id)",
                "CREATE INDEX ix_button_click_logs_user_id ON button_click_logs(user_id)",
                "CREATE INDEX ix_button_click_logs_clicked_at ON button_click_logs(clicked_at)",
                "CREATE INDEX ix_button_click_logs_button_date ON button_click_logs(button_id, clicked_at)",
                "CREATE INDEX ix_button_click_logs_user_date ON button_click_logs(user_id, clicked_at)",
            ]
            for stmt in index_statements:
                await conn.execute(text(stmt))

            logger.info("✅ Таблица button_click_logs создана")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания таблицы button_click_logs: {error}")
        return False


async def create_payment_routing_log_table() -> bool:
    """Создаёт журнал маршрутизации счетов между универсальными шлюзами."""
    table_exists = await check_table_exists("payment_routing_log")
    if table_exists:
        logger.info("ℹ️ Таблица payment_routing_log уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_table_sql = """
                CREATE TABLE payment_routing_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    source VARCHAR(32) NOT NULL,
                    amount_kopeks INTEGER NOT NULL,
                    requested_gateway VARCHAR(20) NOT NULL,
                    gateway VARCHAR(20) NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    fallback_used BOOLEAN NOT NULL DEFAULT 0,
                    weights_json TEXT NULL,
                    attempts_json TEXT NULL,
                    local_payment_id INTEGER NULL,
                    external_id VARCHAR(255) NULL,
                    payment_url TEXT NULL,
                    expires_at DATETIME NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id) ON DELETE SET NULL,
                    paid_at DATETIME NULL,
                    paid_amount_kopeks INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            elif db_type == "postgresql":
                create_table_sql = """
                CREATE TABLE payment_routing_log (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    source VARCHAR(32) NOT NULL,
                    amount_kopeks INTEGER NOT NULL,
                    requested_gateway VARCHAR(20) NOT NULL,
                    gateway VARCHAR(20) NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
                    weights_json JSONB NULL,
                    attempts_json JSONB NULL,
                    local_payment_id INTEGER NULL,
                    external_id VARCHAR(255) NULL,
                    payment_url TEXT NULL,
                    expires_at TIMESTAMP NULL,
                    transaction_id INTEGER NULL REFERENCES transactions(id) ON DELETE SET NULL,
                    paid_at TIMESTAMP NULL,
                    paid_amount_kopeks INTEGER NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
                """
            else:
                create_table_sql = """
                CREATE TABLE payment_routing_log (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NULL,
                    source VARCHAR(32) NOT NULL,
                    amount_kopeks INT NOT NULL,
                    requested_gateway VARCHAR(20) NOT NULL,
                    gateway VARCHAR(20) NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
                    weights_json JSON NULL,
                    attempts_json JSON NULL,
                    local_payment_id INT NULL,
                    external_id VARCHAR(255) NULL,
                    payment_url TEXT NULL,
                    expires_at TIMESTAMP NULL,
                    transaction_id INT NULL,
                    paid_at TIMESTAMP NULL,
                    paid_amount_kopeks INT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
                ) ENGINE=InnoDB
                """

            await conn.execute(text(create_table_sql))

            index_statements = [
                "CREATE INDEX ix_payment_routing_log_user_id ON payment_routing_log(user_id)",
                "CREATE INDEX ix_payment_routing_log_source ON payment_routing_log(source)",
                "CREATE INDEX ix_payment_routing_log_requested_gateway ON payment_routing_log(requested_gateway)",
                "CREATE INDEX ix_payment_routing_log_gateway ON payment_routing_log(gateway)",
                "CREATE INDEX ix_payment_routing_log_external_id ON payment_routing_log(external_id)",
                "CREATE INDEX ix_payment_routing_log_created_at ON payment_routing_log(created_at)",
                "CREATE INDEX ix_payment_routing_log_gw_created ON payment_routing_log(gateway, created_at)",
                "CREATE INDEX ix_payment_routing_log_user_created ON payment_routing_log(user_id, created_at)",
                "CREATE INDEX ix_payment_routing_log_lookup ON payment_routing_log(gateway, local_payment_id)",
            ]
            for stmt in index_statements:
                await conn.execute(text(stmt))

            logger.info("✅ Таблица payment_routing_log создана")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания таблицы payment_routing_log: {error}")
        return False


async def create_web_api_tokens_table() -> bool:
    table_exists = await check_table_exists("web_api_tokens")
    if table_exists:
        logger.info("ℹ️ Таблица web_api_tokens уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE web_api_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(255) NOT NULL,
                    token_hash VARCHAR(128) NOT NULL UNIQUE,
                    token_prefix VARCHAR(32) NOT NULL,
                    description TEXT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME NULL,
                    last_used_at DATETIME NULL,
                    last_used_ip VARCHAR(64) NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_by VARCHAR(255) NULL
                );
                CREATE INDEX idx_web_api_tokens_active ON web_api_tokens(is_active);
                CREATE INDEX idx_web_api_tokens_prefix ON web_api_tokens(token_prefix);
                CREATE INDEX idx_web_api_tokens_last_used ON web_api_tokens(last_used_at);
                """
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE web_api_tokens (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    token_hash VARCHAR(128) NOT NULL UNIQUE,
                    token_prefix VARCHAR(32) NOT NULL,
                    description TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    expires_at TIMESTAMP NULL,
                    last_used_at TIMESTAMP NULL,
                    last_used_ip VARCHAR(64) NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by VARCHAR(255) NULL
                );
                CREATE INDEX idx_web_api_tokens_active ON web_api_tokens(is_active);
                CREATE INDEX idx_web_api_tokens_prefix ON web_api_tokens(token_prefix);
                CREATE INDEX idx_web_api_tokens_last_used ON web_api_tokens(last_used_at);
                """
            else:
                create_sql = """
                CREATE TABLE web_api_tokens (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    token_hash VARCHAR(128) NOT NULL UNIQUE,
                    token_prefix VARCHAR(32) NOT NULL,
                    description TEXT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NULL,
                    last_used_at TIMESTAMP NULL,
                    last_used_ip VARCHAR(64) NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by VARCHAR(255) NULL
                ) ENGINE=InnoDB;
                CREATE INDEX idx_web_api_tokens_active ON web_api_tokens(is_active);
                CREATE INDEX idx_web_api_tokens_prefix ON web_api_tokens(token_prefix);
                CREATE INDEX idx_web_api_tokens_last_used ON web_api_tokens(last_used_at);
                """

            await conn.execute(text(create_sql))
            logger.info("✅ Таблица web_api_tokens создана")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания таблицы web_api_tokens: {error}")
        return False


async def create_privacy_policies_table() -> bool:
    table_exists = await check_table_exists("privacy_policies")
    if table_exists:
        logger.info("ℹ️ Таблица privacy_policies уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE privacy_policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE privacy_policies (
                    id SERIAL PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            else:
                create_sql = """
                CREATE TABLE privacy_policies (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """

            await conn.execute(text(create_sql))
            logger.info("✅ Таблица privacy_policies создана")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания таблицы privacy_policies: {error}")
        return False


async def create_public_offers_table() -> bool:
    table_exists = await check_table_exists("public_offers")
    if table_exists:
        logger.info("ℹ️ Таблица public_offers уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE public_offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE public_offers (
                    id SERIAL PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            else:
                create_sql = """
                CREATE TABLE public_offers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """

            await conn.execute(text(create_sql))
            logger.info("✅ Таблица public_offers создана")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания таблицы public_offers: {error}")
        return False


async def create_faq_settings_table() -> bool:
    table_exists = await check_table_exists("faq_settings")
    if table_exists:
        logger.info("ℹ️ Таблица faq_settings уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE faq_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    is_enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                """
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE faq_settings (
                    id SERIAL PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            else:
                create_sql = """
                CREATE TABLE faq_settings (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    language VARCHAR(10) NOT NULL UNIQUE,
                    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                """

            await conn.execute(text(create_sql))
            logger.info("✅ Таблица faq_settings создана")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания таблицы faq_settings: {error}")
        return False


async def create_faq_pages_table() -> bool:
    table_exists = await check_table_exists("faq_pages")
    if table_exists:
        logger.info("ℹ️ Таблица faq_pages уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE faq_pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    language VARCHAR(10) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX idx_faq_pages_language ON faq_pages(language);
                """
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE faq_pages (
                    id SERIAL PRIMARY KEY,
                    language VARCHAR(10) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX idx_faq_pages_language ON faq_pages(language);
                CREATE INDEX idx_faq_pages_order ON faq_pages(language, display_order);
                """
            else:
                create_sql = """
                CREATE TABLE faq_pages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    language VARCHAR(10) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    display_order INT NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB;
                CREATE INDEX idx_faq_pages_language ON faq_pages(language);
                CREATE INDEX idx_faq_pages_order ON faq_pages(language, display_order);
                """

            await conn.execute(text(create_sql))
            logger.info("✅ Таблица faq_pages создана")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания таблицы faq_pages: {error}")
        return False


async def ensure_default_web_api_token() -> bool:
    default_token = (settings.WEB_API_DEFAULT_TOKEN or "").strip()
    if not default_token:
        return True

    token_name = (settings.WEB_API_DEFAULT_TOKEN_NAME or "Bootstrap Token").strip()

    try:
        async with AsyncSessionLocal() as session:
            token_hash = hash_api_token(default_token, settings.WEB_API_TOKEN_HASH_ALGORITHM)
            result = await session.execute(
                select(WebApiToken).where(WebApiToken.token_hash == token_hash)
            )
            existing = result.scalar_one_or_none()

            if existing:
                updated = False

                if not existing.is_active:
                    existing.is_active = True
                    updated = True

                if token_name and existing.name != token_name:
                    existing.name = token_name
                    updated = True

                if updated:
                    existing.updated_at = datetime.utcnow()
                    await session.commit()
                return True

            token = WebApiToken(
                name=token_name or "Bootstrap Token",
                token_hash=token_hash,
                token_prefix=default_token[:12],
                description="Автоматически создан при миграции",
                created_by="migration",
                is_active=True,
            )
            session.add(token)
            await session.commit()
            logger.info("✅ Создан дефолтный токен веб-API из конфигурации")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания дефолтного веб-API токена: {error}")
        return False


async def add_promo_group_priority_column() -> bool:
    """Добавляет колонку priority в таблицу promo_groups."""
    column_exists = await check_column_exists('promo_groups', 'priority')
    if column_exists:
        logger.info("Колонка priority уже существует в promo_groups")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                column_def = 'INTEGER NOT NULL DEFAULT 0'
            elif db_type == 'postgresql':
                column_def = 'INTEGER NOT NULL DEFAULT 0'
            else:
                column_def = 'INT NOT NULL DEFAULT 0'

            await conn.execute(
                text(f"ALTER TABLE promo_groups ADD COLUMN priority {column_def}")
            )

            # Создаем индекс для оптимизации сортировки
            if db_type == 'postgresql':
                await conn.execute(
                    text("CREATE INDEX IF NOT EXISTS idx_promo_groups_priority ON promo_groups(priority DESC)")
                )
            elif db_type == 'sqlite':
                await conn.execute(
                    text("CREATE INDEX IF NOT EXISTS idx_promo_groups_priority ON promo_groups(priority DESC)")
                )
            else:  # MySQL
                await conn.execute(
                    text("CREATE INDEX idx_promo_groups_priority ON promo_groups(priority DESC)")
                )

        logger.info("✅ Добавлена колонка priority в promo_groups с индексом")
        return True

    except Exception as error:
        logger.error(f"Ошибка добавления колонки priority: {error}")
        return False


async def create_user_promo_groups_table() -> bool:
    """Создает таблицу user_promo_groups для связи Many-to-Many между users и promo_groups."""
    table_exists = await check_table_exists("user_promo_groups")
    if table_exists:
        logger.info("ℹ️ Таблица user_promo_groups уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == "sqlite":
                create_sql = """
                CREATE TABLE user_promo_groups (
                    user_id INTEGER NOT NULL,
                    promo_group_id INTEGER NOT NULL,
                    assigned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    assigned_by VARCHAR(50) DEFAULT 'system',
                    PRIMARY KEY (user_id, promo_group_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                );
                """
                index_sql = "CREATE INDEX idx_user_promo_groups_user_id ON user_promo_groups(user_id);"
            elif db_type == "postgresql":
                create_sql = """
                CREATE TABLE user_promo_groups (
                    user_id INTEGER NOT NULL,
                    promo_group_id INTEGER NOT NULL,
                    assigned_at TIMESTAMP DEFAULT NOW(),
                    assigned_by VARCHAR(50) DEFAULT 'system',
                    PRIMARY KEY (user_id, promo_group_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                );
                """
                index_sql = "CREATE INDEX idx_user_promo_groups_user_id ON user_promo_groups(user_id);"
            else:  # MySQL
                create_sql = """
                CREATE TABLE user_promo_groups (
                    user_id INT NOT NULL,
                    promo_group_id INT NOT NULL,
                    assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    assigned_by VARCHAR(50) DEFAULT 'system',
                    PRIMARY KEY (user_id, promo_group_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (promo_group_id) REFERENCES promo_groups(id) ON DELETE CASCADE
                );
                """
                index_sql = "CREATE INDEX idx_user_promo_groups_user_id ON user_promo_groups(user_id);"

            await conn.execute(text(create_sql))
            await conn.execute(text(index_sql))
            logger.info("✅ Таблица user_promo_groups создана с индексом")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка создания таблицы user_promo_groups: {error}")
        return False


async def migrate_existing_user_promo_groups_data() -> bool:
    """Переносит существующие связи users.promo_group_id в таблицу user_promo_groups."""
    try:
        table_exists = await check_table_exists("user_promo_groups")
        if not table_exists:
            logger.warning("⚠️ Таблица user_promo_groups не существует, пропускаем миграцию данных")
            return False

        column_exists = await check_column_exists('users', 'promo_group_id')
        if not column_exists:
            logger.warning("⚠️ Колонка users.promo_group_id не существует, пропускаем миграцию данных")
            return True

        async with engine.begin() as conn:
            # Проверяем есть ли уже данные в user_promo_groups
            result = await conn.execute(text("SELECT COUNT(*) FROM user_promo_groups"))
            count = result.scalar()

            if count > 0:
                logger.info(f"ℹ️ В таблице user_promo_groups уже есть {count} записей, пропускаем миграцию")
                return True

            # Переносим данные из users.promo_group_id
            db_type = await get_database_type()

            if db_type == "sqlite":
                migrate_sql = """
                INSERT INTO user_promo_groups (user_id, promo_group_id, assigned_at, assigned_by)
                SELECT id, promo_group_id, CURRENT_TIMESTAMP, 'system'
                FROM users
                WHERE promo_group_id IS NOT NULL
                """
            else:  # PostgreSQL and MySQL
                migrate_sql = """
                INSERT INTO user_promo_groups (user_id, promo_group_id, assigned_at, assigned_by)
                SELECT id, promo_group_id, NOW(), 'system'
                FROM users
                WHERE promo_group_id IS NOT NULL
                """

            result = await conn.execute(text(migrate_sql))
            migrated_count = result.rowcount if hasattr(result, 'rowcount') else 0

            logger.info(f"✅ Перенесено {migrated_count} связей пользователей с промогруппами")
            return True

    except Exception as error:
        logger.error(f"❌ Ошибка миграции данных user_promo_groups: {error}")
        return False


async def add_promocode_promo_group_column() -> bool:
    """Добавляет колонку promo_group_id в таблицу promocodes."""
    column_exists = await check_column_exists('promocodes', 'promo_group_id')
    if column_exists:
        logger.info("Колонка promo_group_id уже существует в promocodes")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            # Add column
            if db_type == 'sqlite':
                await conn.execute(
                    text("ALTER TABLE promocodes ADD COLUMN promo_group_id INTEGER")
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("ALTER TABLE promocodes ADD COLUMN promo_group_id INTEGER")
                )
                # Add foreign key
                await conn.execute(
                    text("""
                        ALTER TABLE promocodes
                        ADD CONSTRAINT fk_promocodes_promo_group
                        FOREIGN KEY (promo_group_id)
                        REFERENCES promo_groups(id)
                        ON DELETE SET NULL
                    """)
                )
                # Add index
                await conn.execute(
                    text("CREATE INDEX IF NOT EXISTS idx_promocodes_promo_group_id ON promocodes(promo_group_id)")
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("""
                        ALTER TABLE promocodes
                        ADD COLUMN promo_group_id INT,
                        ADD CONSTRAINT fk_promocodes_promo_group
                        FOREIGN KEY (promo_group_id)
                        REFERENCES promo_groups(id)
                        ON DELETE SET NULL
                    """)
                )
                await conn.execute(
                    text("CREATE INDEX idx_promocodes_promo_group_id ON promocodes(promo_group_id)")
                )

        logger.info("✅ Добавлена колонка promo_group_id в promocodes")
        return True

    except Exception as error:
        logger.error(f"❌ Ошибка добавления promo_group_id в promocodes: {error}")
        return False


async def add_user_has_connected_to_vpn_column() -> bool:
    """Добавляет колонку has_connected_to_vpn в таблицу users."""
    column_exists = await check_column_exists('users', 'has_connected_to_vpn')
    if column_exists:
        logger.info("Колонка has_connected_to_vpn уже существует в users")
        return True

    try:
        db_type = await get_database_type()
        async with engine.begin() as conn:
            if db_type == 'sqlite':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN has_connected_to_vpn BOOLEAN NOT NULL DEFAULT 0")
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN has_connected_to_vpn BOOLEAN NOT NULL DEFAULT FALSE")
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN has_connected_to_vpn TINYINT(1) NOT NULL DEFAULT 0")
                )

        logger.info("✅ Добавлена колонка has_connected_to_vpn в users")
        return True

    except Exception as error:
        logger.error(f"❌ Ошибка добавления has_connected_to_vpn в users: {error}")
        return False


async def add_user_has_used_mobile_app_column() -> bool:
    """Добавляет колонку has_used_mobile_app в таблицу users."""
    column_exists = await check_column_exists('users', 'has_used_mobile_app')
    if column_exists:
        logger.info("Колонка has_used_mobile_app уже существует в users")
        return True

    try:
        db_type = await get_database_type()
        async with engine.begin() as conn:
            if db_type == 'sqlite':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN has_used_mobile_app BOOLEAN NOT NULL DEFAULT 0")
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN has_used_mobile_app BOOLEAN NOT NULL DEFAULT FALSE")
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN has_used_mobile_app TINYINT(1) NOT NULL DEFAULT 0")
                )

        logger.info("✅ Добавлена колонка has_used_mobile_app в users")
        return True

    except Exception as error:
        logger.error(f"❌ Ошибка добавления has_used_mobile_app в users: {error}")
        return False


async def add_user_acquisition_source_column() -> bool:
    """Добавляет колонку acquisition_source в таблицу users (Install Referrer)."""
    column_exists = await check_column_exists('users', 'acquisition_source')
    if column_exists:
        logger.info("Колонка acquisition_source уже существует в users")
        return True

    try:
        db_type = await get_database_type()
        async with engine.begin() as conn:
            if db_type == 'sqlite':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN acquisition_source VARCHAR(100)")
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN acquisition_source VARCHAR(100) NULL")
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN acquisition_source VARCHAR(100) NULL")
                )

        logger.info("✅ Добавлена колонка acquisition_source в users")
        return True

    except Exception as error:
        logger.error(f"❌ Ошибка добавления acquisition_source в users: {error}")
        return False


async def add_user_tg_user_id_column() -> bool:
    """Добавляет колонку tg_user_id в таблицу users (Install Referrer, связка с Telegram)."""
    column_exists = await check_column_exists('users', 'tg_user_id')
    if column_exists:
        logger.info("Колонка tg_user_id уже существует в users")
        return True

    try:
        db_type = await get_database_type()
        async with engine.begin() as conn:
            if db_type == 'sqlite':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN tg_user_id BIGINT")
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN tg_user_id BIGINT NULL")
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN tg_user_id BIGINT NULL")
                )

        logger.info("✅ Добавлена колонка tg_user_id в users")
        return True

    except Exception as error:
        logger.error(f"❌ Ошибка добавления tg_user_id в users: {error}")
        return False


async def add_user_last_app_name_column() -> bool:
    """Добавляет колонку last_app_name в таблицу users (заголовок x-appName)."""
    column_exists = await check_column_exists('users', 'last_app_name')
    if column_exists:
        logger.info("Колонка last_app_name уже существует в users")
        return True

    try:
        db_type = await get_database_type()
        async with engine.begin() as conn:
            if db_type == 'sqlite':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN last_app_name VARCHAR(100)")
                )
            elif db_type == 'postgresql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN last_app_name VARCHAR(100) NULL")
                )
            elif db_type == 'mysql':
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN last_app_name VARCHAR(100) NULL")
                )

        logger.info("✅ Добавлена колонка last_app_name в users")
        return True

    except Exception as error:
        logger.error(f"❌ Ошибка добавления last_app_name в users: {error}")
        return False


async def create_device_binding_codes_table() -> bool:
    table_exists = await check_table_exists('device_binding_codes')
    if table_exists:
        logger.info("Таблица device_binding_codes уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE device_binding_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    code VARCHAR(16) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME NOT NULL,
                    used_at DATETIME NULL,
                    used_device_id VARCHAR(255) NULL,
                    CONSTRAINT uq_device_binding_codes_code UNIQUE (code),
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_device_binding_codes_subscription_id ON device_binding_codes(subscription_id);
                CREATE INDEX ix_device_binding_codes_code ON device_binding_codes(code);
                CREATE INDEX ix_device_binding_codes_expires_at ON device_binding_codes(expires_at);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS device_binding_codes (
                    id SERIAL PRIMARY KEY,
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    code VARCHAR(16) NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMP NOT NULL,
                    used_at TIMESTAMP NULL,
                    used_device_id VARCHAR(255) NULL,
                    CONSTRAINT uq_device_binding_codes_code UNIQUE (code)
                );

                CREATE INDEX IF NOT EXISTS ix_device_binding_codes_subscription_id ON device_binding_codes(subscription_id);
                CREATE INDEX IF NOT EXISTS ix_device_binding_codes_code ON device_binding_codes(code);
                CREATE INDEX IF NOT EXISTS ix_device_binding_codes_expires_at ON device_binding_codes(expires_at);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS device_binding_codes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    subscription_id INT NOT NULL,
                    code VARCHAR(16) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME NOT NULL,
                    used_at DATETIME NULL,
                    used_device_id VARCHAR(255) NULL,
                    CONSTRAINT uq_device_binding_codes_code UNIQUE (code),
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_device_binding_codes_subscription_id ON device_binding_codes(subscription_id);
                CREATE INDEX ix_device_binding_codes_code ON device_binding_codes(code);
                CREATE INDEX ix_device_binding_codes_expires_at ON device_binding_codes(expires_at);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            await conn.execute(text(create_sql))

        logger.info("✅ Таблица device_binding_codes успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы device_binding_codes: {e}")
        return False


async def create_share_tokens_table() -> bool:
    table_exists = await check_table_exists('share_tokens')
    if table_exists:
        logger.info("Таблица share_tokens уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE share_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    token VARCHAR(64) NOT NULL,
                    share_code VARCHAR(16) NOT NULL,
                    activations_count INTEGER NOT NULL DEFAULT 0,
                    max_activations INTEGER NOT NULL DEFAULT 10,
                    revoked_at DATETIME NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_share_tokens_token UNIQUE (token),
                    CONSTRAINT uq_share_tokens_share_code UNIQUE (share_code),
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_share_tokens_subscription_id ON share_tokens(subscription_id);
                CREATE INDEX ix_share_tokens_token ON share_tokens(token);
                CREATE INDEX ix_share_tokens_share_code ON share_tokens(share_code);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS share_tokens (
                    id SERIAL PRIMARY KEY,
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                    token VARCHAR(64) NOT NULL,
                    share_code VARCHAR(16) NOT NULL,
                    activations_count INTEGER NOT NULL DEFAULT 0,
                    max_activations INTEGER NOT NULL DEFAULT 10,
                    revoked_at TIMESTAMP NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_share_tokens_token UNIQUE (token),
                    CONSTRAINT uq_share_tokens_share_code UNIQUE (share_code)
                );

                CREATE INDEX IF NOT EXISTS ix_share_tokens_subscription_id ON share_tokens(subscription_id);
                CREATE INDEX IF NOT EXISTS ix_share_tokens_token ON share_tokens(token);
                CREATE INDEX IF NOT EXISTS ix_share_tokens_share_code ON share_tokens(share_code);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS share_tokens (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    subscription_id INT NOT NULL,
                    token VARCHAR(64) NOT NULL,
                    share_code VARCHAR(16) NOT NULL,
                    activations_count INT NOT NULL DEFAULT 0,
                    max_activations INT NOT NULL DEFAULT 10,
                    revoked_at DATETIME NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_share_tokens_token UNIQUE (token),
                    CONSTRAINT uq_share_tokens_share_code UNIQUE (share_code),
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_share_tokens_subscription_id ON share_tokens(subscription_id);
                CREATE INDEX ix_share_tokens_token ON share_tokens(token);
                CREATE INDEX ix_share_tokens_share_code ON share_tokens(share_code);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            await conn.execute(text(create_sql))

        logger.info("✅ Таблица share_tokens успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы share_tokens: {e}")
        return False


async def create_user_daily_traffic_usage_table() -> bool:
    table_exists = await check_table_exists('user_daily_traffic_usage')
    if table_exists:
        logger.info("Таблица user_daily_traffic_usage уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE user_daily_traffic_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    date DATE NOT NULL,
                    traffic_bytes BIGINT NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_user_daily_traffic_usage_user_date UNIQUE (user_id, date),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_user_daily_traffic_usage_user_id ON user_daily_traffic_usage(user_id);
                CREATE INDEX ix_user_daily_traffic_usage_date ON user_daily_traffic_usage(date);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS user_daily_traffic_usage (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    date DATE NOT NULL,
                    traffic_bytes BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_user_daily_traffic_usage_user_date UNIQUE (user_id, date)
                );

                CREATE INDEX IF NOT EXISTS ix_user_daily_traffic_usage_user_id ON user_daily_traffic_usage(user_id);
                CREATE INDEX IF NOT EXISTS ix_user_daily_traffic_usage_date ON user_daily_traffic_usage(date);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS user_daily_traffic_usage (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    date DATE NOT NULL,
                    traffic_bytes BIGINT NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_user_daily_traffic_usage_user_date UNIQUE (user_id, date),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_user_daily_traffic_usage_user_id ON user_daily_traffic_usage(user_id);
                CREATE INDEX ix_user_daily_traffic_usage_date ON user_daily_traffic_usage(date);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица user_daily_traffic_usage успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы user_daily_traffic_usage: {e}")
        return False


async def create_daily_subscription_metrics_table() -> bool:
    table_exists = await check_table_exists('daily_subscription_metrics')
    if table_exists:
        logger.info("Таблица daily_subscription_metrics уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE daily_subscription_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL,
                    paid_users_count INTEGER NOT NULL DEFAULT 0,
                    lost_paid_users_count INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_daily_subscription_metrics_date UNIQUE (date)
                );

                CREATE INDEX ix_daily_subscription_metrics_date ON daily_subscription_metrics(date);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS daily_subscription_metrics (
                    id SERIAL PRIMARY KEY,
                    date DATE NOT NULL,
                    paid_users_count INTEGER NOT NULL DEFAULT 0,
                    lost_paid_users_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_daily_subscription_metrics_date UNIQUE (date)
                );

                CREATE INDEX IF NOT EXISTS ix_daily_subscription_metrics_date ON daily_subscription_metrics(date);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS daily_subscription_metrics (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    date DATE NOT NULL,
                    paid_users_count INT NOT NULL DEFAULT 0,
                    lost_paid_users_count INT NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_daily_subscription_metrics_date UNIQUE (date)
                );

                CREATE INDEX ix_daily_subscription_metrics_date ON daily_subscription_metrics(date);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица daily_subscription_metrics успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы daily_subscription_metrics: {e}")
        return False


async def create_user_daily_metrics_table() -> bool:
    table_exists = await check_table_exists('user_daily_metrics')
    if table_exists:
        logger.info("Таблица user_daily_metrics уже существует")
        return True

    columns = """
                    date DATE NOT NULL,
                    snapshot_at {datetime_type} NOT NULL,
                    new_users_count INTEGER NOT NULL DEFAULT 0,
                    new_telegram_users_count INTEGER NOT NULL DEFAULT 0,
                    new_bot_users_count INTEGER NOT NULL DEFAULT 0,
                    new_web_users_count INTEGER NOT NULL DEFAULT 0,
                    new_app_users_count INTEGER NOT NULL DEFAULT 0,
                    new_email_users_count INTEGER NOT NULL DEFAULT 0,
                    new_referred_users_count INTEGER NOT NULL DEFAULT 0,
                    total_users_count INTEGER NOT NULL DEFAULT 0,
                    active_users_count INTEGER NOT NULL DEFAULT 0,
                    blocked_users_count INTEGER NOT NULL DEFAULT 0,
                    deleted_users_count INTEGER NOT NULL DEFAULT 0,
                    telegram_users_count INTEGER NOT NULL DEFAULT 0,
                    bot_users_count INTEGER NOT NULL DEFAULT 0,
                    web_users_count INTEGER NOT NULL DEFAULT 0,
                    app_users_count INTEGER NOT NULL DEFAULT 0,
                    email_users_count INTEGER NOT NULL DEFAULT 0,
                    users_with_remnawave_uuid_count INTEGER NOT NULL DEFAULT 0,
                    users_connected_to_vpn_count INTEGER NOT NULL DEFAULT 0,
                    users_without_vpn_connection_count INTEGER NOT NULL DEFAULT 0,
                    users_with_first_topup_count INTEGER NOT NULL DEFAULT 0,
                    users_with_paid_subscription_history_count INTEGER NOT NULL DEFAULT 0,
                    users_with_positive_balance_count INTEGER NOT NULL DEFAULT 0,
                    total_balance_kopeks BIGINT NOT NULL DEFAULT 0,
                    referred_users_count INTEGER NOT NULL DEFAULT 0,
                    users_with_referral_code_count INTEGER NOT NULL DEFAULT 0,
                    users_with_custom_referral_commission_count INTEGER NOT NULL DEFAULT 0,
                    qualified_referrers_count INTEGER NOT NULL DEFAULT 0,
                    total_qualified_referrals_count INTEGER NOT NULL DEFAULT 0,
                    mobile_app_users_count INTEGER NOT NULL DEFAULT 0,
                    users_with_tg_user_id_count INTEGER NOT NULL DEFAULT 0,
                    users_with_acquisition_source_count INTEGER NOT NULL DEFAULT 0,
                    users_with_attribution_source_count INTEGER NOT NULL DEFAULT 0,
                    users_with_attribution_campaign_count INTEGER NOT NULL DEFAULT 0,
                    users_with_promo_group_count INTEGER NOT NULL DEFAULT 0,
                    users_with_auto_promo_group_count INTEGER NOT NULL DEFAULT 0,
                    users_with_active_promo_offer_count INTEGER NOT NULL DEFAULT 0,
                    legacy_pricing_users_count INTEGER NOT NULL DEFAULT 0,
                    new_pricing_users_count INTEGER NOT NULL DEFAULT 0,
                    created_at {datetime_type} NOT NULL DEFAULT {now_expr},
                    updated_at {datetime_type} NOT NULL DEFAULT {now_expr},
                    CONSTRAINT uq_user_daily_metrics_date UNIQUE (date)
    """

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = f"""
                CREATE TABLE user_daily_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
{columns.format(datetime_type='DATETIME', now_expr='CURRENT_TIMESTAMP')}
                );

                CREATE INDEX ix_user_daily_metrics_date ON user_daily_metrics(date);
                """
            elif db_type == 'postgresql':
                create_sql = f"""
                CREATE TABLE IF NOT EXISTS user_daily_metrics (
                    id SERIAL PRIMARY KEY,
{columns.format(datetime_type='TIMESTAMP', now_expr='NOW()')}
                );

                CREATE INDEX IF NOT EXISTS ix_user_daily_metrics_date ON user_daily_metrics(date);
                """
            elif db_type == 'mysql':
                create_sql = f"""
                CREATE TABLE IF NOT EXISTS user_daily_metrics (
                    id INT AUTO_INCREMENT PRIMARY KEY,
{columns.format(datetime_type='DATETIME', now_expr='CURRENT_TIMESTAMP')}
                );

                CREATE INDEX ix_user_daily_metrics_date ON user_daily_metrics(date);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица user_daily_metrics успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы user_daily_metrics: {e}")
        return False


async def create_trial_expiry_daily_metrics_table() -> bool:
    table_exists = await check_table_exists('trial_expiry_daily_metrics')
    if table_exists:
        logger.info("Таблица trial_expiry_daily_metrics уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE trial_expiry_daily_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL,
                    snapshot_at DATETIME NOT NULL,
                    trial_ended_count INTEGER NOT NULL DEFAULT 0,
                    trial_paid_7d_count INTEGER NOT NULL DEFAULT 0,
                    connected_trial_ended_count INTEGER NOT NULL DEFAULT 0,
                    connected_trial_paid_7d_count INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_trial_expiry_daily_metrics_date UNIQUE (date)
                );

                CREATE INDEX ix_trial_expiry_daily_metrics_date ON trial_expiry_daily_metrics(date);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS trial_expiry_daily_metrics (
                    id SERIAL PRIMARY KEY,
                    date DATE NOT NULL,
                    snapshot_at TIMESTAMP NOT NULL,
                    trial_ended_count INTEGER NOT NULL DEFAULT 0,
                    trial_paid_7d_count INTEGER NOT NULL DEFAULT 0,
                    connected_trial_ended_count INTEGER NOT NULL DEFAULT 0,
                    connected_trial_paid_7d_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_trial_expiry_daily_metrics_date UNIQUE (date)
                );

                CREATE INDEX IF NOT EXISTS ix_trial_expiry_daily_metrics_date ON trial_expiry_daily_metrics(date);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS trial_expiry_daily_metrics (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    date DATE NOT NULL,
                    snapshot_at DATETIME NOT NULL,
                    trial_ended_count INT NOT NULL DEFAULT 0,
                    trial_paid_7d_count INT NOT NULL DEFAULT 0,
                    connected_trial_ended_count INT NOT NULL DEFAULT 0,
                    connected_trial_paid_7d_count INT NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_trial_expiry_daily_metrics_date UNIQUE (date)
                );

                CREATE INDEX ix_trial_expiry_daily_metrics_date ON trial_expiry_daily_metrics(date);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица trial_expiry_daily_metrics успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы trial_expiry_daily_metrics: {e}")
        return False


async def add_subscription_is_partner_column() -> bool:
    """Adds Subscription.is_partner boolean + CHECK constraint enforcing
    that is_trial and is_partner are mutually exclusive.

    Idempotent: re-running on a DB where the column/constraint already exist is a no-op.
    """
    column_exists = await check_column_exists('subscriptions', 'is_partner')

    try:
        if not column_exists:
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN is_partner BOOLEAN NOT NULL DEFAULT 0"
                    ))
                elif db_type == 'postgresql':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN is_partner BOOLEAN NOT NULL DEFAULT FALSE"
                    ))
                elif db_type == 'mysql':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN is_partner BOOLEAN NOT NULL DEFAULT FALSE"
                    ))
                else:
                    logger.error(f"Неподдерживаемый тип БД для добавления is_partner: {db_type}")
                    return False

            logger.info("✅ Добавлена колонка is_partner в таблицу subscriptions")
        else:
            logger.info("ℹ️ Колонка subscriptions.is_partner уже существует")

        constraint_name = 'subscriptions_type_mutex'
        constraint_exists = await check_constraint_exists('subscriptions', constraint_name)
        if not constraint_exists:
            try:
                async with engine.begin() as conn:
                    db_type = await get_database_type()
                    if db_type == 'postgresql':
                        await conn.execute(text(
                            f"ALTER TABLE subscriptions ADD CONSTRAINT {constraint_name} "
                            f"CHECK (NOT (is_trial AND is_partner))"
                        ))
                    elif db_type == 'mysql':
                        await conn.execute(text(
                            f"ALTER TABLE subscriptions ADD CONSTRAINT {constraint_name} "
                            f"CHECK (NOT (is_trial AND is_partner))"
                        ))
                    # SQLite не поддерживает ADD CONSTRAINT после создания таблицы — пропускаем.
                if db_type in ('postgresql', 'mysql'):
                    logger.info("✅ Добавлен CHECK-constraint subscriptions_type_mutex")
            except Exception as e:
                logger.warning(f"Не удалось добавить CHECK-constraint subscriptions_type_mutex: {e}")
        else:
            logger.info("ℹ️ CHECK-constraint subscriptions_type_mutex уже существует")

        return True
    except Exception as e:
        logger.error(f"Ошибка добавления колонки is_partner: {e}")
        return False


async def add_subscription_used_trial_failed_column() -> bool:
    """Adds Subscription.used_trial_failed boolean.

    Set when trial issuance is denied for a device_id that had already been
    linked to a subscription before. Idempotent: re-running on a DB where the
    column already exists is a no-op.
    """
    column_exists = await check_column_exists('subscriptions', 'used_trial_failed')

    try:
        if not column_exists:
            async with engine.begin() as conn:
                db_type = await get_database_type()

                if db_type == 'sqlite':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN used_trial_failed BOOLEAN NOT NULL DEFAULT 0"
                    ))
                elif db_type == 'postgresql':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN used_trial_failed BOOLEAN NOT NULL DEFAULT FALSE"
                    ))
                elif db_type == 'mysql':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN used_trial_failed BOOLEAN NOT NULL DEFAULT FALSE"
                    ))
                else:
                    logger.error(f"Неподдерживаемый тип БД для добавления used_trial_failed: {db_type}")
                    return False

            logger.info("✅ Добавлена колонка used_trial_failed в таблицу subscriptions")
        else:
            logger.info("ℹ️ Колонка subscriptions.used_trial_failed уже существует")

        return True
    except Exception as e:
        logger.error(f"Ошибка добавления колонки used_trial_failed: {e}")
        return False


async def create_partner_link_redemptions_table() -> bool:
    """Creates the partner_link_redemptions table tracking one-time-use VIP-link
    redemptions (UNIQUE(jti) enforces replay protection).
    """
    table_exists = await check_table_exists('partner_link_redemptions')
    if table_exists:
        logger.info("ℹ️ Таблица partner_link_redemptions уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE partner_link_redemptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    jti VARCHAR(32) NOT NULL,
                    user_id INTEGER NOT NULL,
                    subscription_id INTEGER NULL,
                    sub_until DATETIME NOT NULL,
                    redeemed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_partner_link_redemptions_jti UNIQUE (jti),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_partner_link_redemptions_user_id ON partner_link_redemptions(user_id);
                CREATE INDEX ix_partner_link_redemptions_redeemed_at ON partner_link_redemptions(redeemed_at);
                CREATE INDEX ix_partner_link_redemptions_jti ON partner_link_redemptions(jti);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS partner_link_redemptions (
                    id SERIAL PRIMARY KEY,
                    jti VARCHAR(32) NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    subscription_id INTEGER NULL REFERENCES subscriptions(id) ON DELETE SET NULL,
                    sub_until TIMESTAMP NOT NULL,
                    redeemed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_partner_link_redemptions_jti UNIQUE (jti)
                );

                CREATE INDEX IF NOT EXISTS ix_partner_link_redemptions_user_id ON partner_link_redemptions(user_id);
                CREATE INDEX IF NOT EXISTS ix_partner_link_redemptions_redeemed_at ON partner_link_redemptions(redeemed_at);
                CREATE INDEX IF NOT EXISTS ix_partner_link_redemptions_jti ON partner_link_redemptions(jti);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS partner_link_redemptions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    jti VARCHAR(32) NOT NULL,
                    user_id INT NOT NULL,
                    subscription_id INT NULL,
                    sub_until DATETIME NOT NULL,
                    redeemed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_partner_link_redemptions_jti UNIQUE (jti),
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
                );

                CREATE INDEX ix_partner_link_redemptions_user_id ON partner_link_redemptions(user_id);
                CREATE INDEX ix_partner_link_redemptions_redeemed_at ON partner_link_redemptions(redeemed_at);
                CREATE INDEX ix_partner_link_redemptions_jti ON partner_link_redemptions(jti);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица partner_link_redemptions успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы partner_link_redemptions: {e}")
        return False


async def create_subscription_plans_table() -> bool:
    """Creates subscription_plans table for tiered subscription system."""
    table_exists = await check_table_exists('subscription_plans')
    if table_exists:
        logger.info("ℹ️ Таблица subscription_plans уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE subscription_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code VARCHAR(16) NOT NULL,
                    display_name VARCHAR(64) NOT NULL,
                    device_limit INTEGER NOT NULL DEFAULT 1,
                    traffic_limit_gb INTEGER NOT NULL DEFAULT 0,
                    traffic_reset_strategy VARCHAR(16) NOT NULL DEFAULT 'NO_RESET',
                    custom_app_only BOOLEAN NOT NULL DEFAULT 0,
                    priority_support BOOLEAN NOT NULL DEFAULT 0,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    description_md TEXT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_subscription_plans_code UNIQUE (code)
                );

                CREATE INDEX ix_subscription_plans_code ON subscription_plans(code);
                CREATE INDEX ix_subscription_plans_is_active ON subscription_plans(is_active);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS subscription_plans (
                    id SERIAL PRIMARY KEY,
                    code VARCHAR(16) NOT NULL,
                    display_name VARCHAR(64) NOT NULL,
                    device_limit INTEGER NOT NULL DEFAULT 1,
                    traffic_limit_gb INTEGER NOT NULL DEFAULT 0,
                    traffic_reset_strategy VARCHAR(16) NOT NULL DEFAULT 'NO_RESET',
                    custom_app_only BOOLEAN NOT NULL DEFAULT FALSE,
                    priority_support BOOLEAN NOT NULL DEFAULT FALSE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    description_md TEXT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_subscription_plans_code UNIQUE (code)
                );

                CREATE INDEX IF NOT EXISTS ix_subscription_plans_code ON subscription_plans(code);
                CREATE INDEX IF NOT EXISTS ix_subscription_plans_is_active ON subscription_plans(is_active);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS subscription_plans (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    code VARCHAR(16) NOT NULL,
                    display_name VARCHAR(64) NOT NULL,
                    device_limit INT NOT NULL DEFAULT 1,
                    traffic_limit_gb INT NOT NULL DEFAULT 0,
                    traffic_reset_strategy VARCHAR(16) NOT NULL DEFAULT 'NO_RESET',
                    custom_app_only BOOLEAN NOT NULL DEFAULT FALSE,
                    priority_support BOOLEAN NOT NULL DEFAULT FALSE,
                    sort_order INT NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    description_md TEXT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_subscription_plans_code UNIQUE (code)
                );

                CREATE INDEX ix_subscription_plans_code ON subscription_plans(code);
                CREATE INDEX ix_subscription_plans_is_active ON subscription_plans(is_active);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица subscription_plans успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы subscription_plans: {e}")
        return False


async def create_subscription_plan_prices_table() -> bool:
    """Creates subscription_plan_prices table (price per plan × period)."""
    table_exists = await check_table_exists('subscription_plan_prices')
    if table_exists:
        logger.info("ℹ️ Таблица subscription_plan_prices уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE subscription_plan_prices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL,
                    period_days INTEGER NOT NULL,
                    price_kopeks INTEGER NOT NULL,
                    audience VARCHAR(8) NOT NULL DEFAULT 'all',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_plan_period UNIQUE (plan_id, period_days, audience),
                    FOREIGN KEY(plan_id) REFERENCES subscription_plans(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_subscription_plan_prices_plan_id ON subscription_plan_prices(plan_id);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS subscription_plan_prices (
                    id SERIAL PRIMARY KEY,
                    plan_id INTEGER NOT NULL REFERENCES subscription_plans(id) ON DELETE CASCADE,
                    period_days INTEGER NOT NULL,
                    price_kopeks INTEGER NOT NULL,
                    audience VARCHAR(8) NOT NULL DEFAULT 'all',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_plan_period UNIQUE (plan_id, period_days, audience)
                );

                CREATE INDEX IF NOT EXISTS ix_subscription_plan_prices_plan_id ON subscription_plan_prices(plan_id);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS subscription_plan_prices (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    plan_id INT NOT NULL,
                    period_days INT NOT NULL,
                    price_kopeks INT NOT NULL,
                    audience VARCHAR(8) NOT NULL DEFAULT 'all',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_plan_period UNIQUE (plan_id, period_days, audience),
                    FOREIGN KEY(plan_id) REFERENCES subscription_plans(id) ON DELETE CASCADE
                );

                CREATE INDEX ix_subscription_plan_prices_plan_id ON subscription_plan_prices(plan_id);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица subscription_plan_prices успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы subscription_plan_prices: {e}")
        return False


async def add_plan_columns_to_subscriptions() -> bool:
    """Adds Subscription.plan_id (nullable FK) and Subscription.plan_period_days columns.

    plan_id NULL means legacy à-la-carte subscription. Non-null = new tiered plan.
    """
    try:
        db_type = await get_database_type()

        plan_id_exists = await check_column_exists('subscriptions', 'plan_id')
        if not plan_id_exists:
            async with engine.begin() as conn:
                if db_type == 'sqlite':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN plan_id INTEGER NULL "
                        "REFERENCES subscription_plans(id) ON DELETE SET NULL"
                    ))
                elif db_type == 'postgresql':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN plan_id INTEGER NULL "
                        "REFERENCES subscription_plans(id) ON DELETE SET NULL"
                    ))
                elif db_type == 'mysql':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN plan_id INT NULL"
                    ))
                    try:
                        await conn.execute(text(
                            "ALTER TABLE subscriptions ADD CONSTRAINT fk_subscriptions_plan_id "
                            "FOREIGN KEY (plan_id) REFERENCES subscription_plans(id) ON DELETE SET NULL"
                        ))
                    except Exception as fk_err:
                        logger.warning(f"FK fk_subscriptions_plan_id не создан: {fk_err}")
                else:
                    logger.error(f"Неподдерживаемый тип БД для добавления plan_id: {db_type}")
                    return False
            logger.info("✅ Добавлена колонка plan_id в таблицу subscriptions")
        else:
            logger.info("ℹ️ Колонка subscriptions.plan_id уже существует")

        if not await check_index_exists('subscriptions', 'ix_subscriptions_plan_id'):
            try:
                async with engine.begin() as conn:
                    if db_type == 'postgresql':
                        await conn.execute(text(
                            "CREATE INDEX IF NOT EXISTS ix_subscriptions_plan_id ON subscriptions(plan_id)"
                        ))
                    else:
                        await conn.execute(text(
                            "CREATE INDEX ix_subscriptions_plan_id ON subscriptions(plan_id)"
                        ))
                logger.info("✅ Индекс ix_subscriptions_plan_id создан")
            except Exception as ix_err:
                logger.warning(f"Не удалось создать индекс ix_subscriptions_plan_id: {ix_err}")

        period_days_exists = await check_column_exists('subscriptions', 'plan_period_days')
        if not period_days_exists:
            async with engine.begin() as conn:
                if db_type == 'sqlite':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN plan_period_days INTEGER NULL"
                    ))
                elif db_type == 'postgresql':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN plan_period_days INTEGER NULL"
                    ))
                elif db_type == 'mysql':
                    await conn.execute(text(
                        "ALTER TABLE subscriptions ADD COLUMN plan_period_days INT NULL"
                    ))
            logger.info("✅ Добавлена колонка plan_period_days в таблицу subscriptions")
        else:
            logger.info("ℹ️ Колонка subscriptions.plan_period_days уже существует")

        return True
    except Exception as e:
        logger.error(f"Ошибка добавления plan_id/plan_period_days: {e}")
        return False


# New-cohort 1-month prices for the Solo/Plus/Pro repricing (kopeks).
_NEW_MONTHLY_PRICES = {"solo": 32000, "plus": 49000, "pro": 69000}


async def add_audience_to_plan_prices() -> bool:
    """Adds subscription_plan_prices.audience and splits Solo/Plus/Pro 1-month and
    6-month pricing into cohorts: existing users keep the old prices and the 180-day
    period ('legacy'); new users get the higher 1-month price ('new') and no 180.

    Idempotent — the data split runs only when the column is first added (existing
    installs). Fresh installs create the column in the CREATE TABLE and seed both
    cohorts via seed_subscription_plans().
    """
    try:
        db_type = await get_database_type()

        if await check_column_exists('subscription_plan_prices', 'audience'):
            logger.info("ℹ️ Колонка subscription_plan_prices.audience уже существует")
            return True

        async with engine.begin() as conn:
            # 1. Establish the column + 3-column unique constraint.
            if db_type == 'sqlite':
                # sqlite can't drop a named constraint, and keeping the old 2-col UNIQUE
                # would block the second 30-day row — rebuild the table.
                await conn.execute(text("""
                    CREATE TABLE subscription_plan_prices_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        plan_id INTEGER NOT NULL,
                        period_days INTEGER NOT NULL,
                        price_kopeks INTEGER NOT NULL,
                        audience VARCHAR(8) NOT NULL DEFAULT 'all',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT uq_plan_period UNIQUE (plan_id, period_days, audience),
                        FOREIGN KEY(plan_id) REFERENCES subscription_plans(id) ON DELETE CASCADE
                    )
                """))
                await conn.execute(text(
                    "INSERT INTO subscription_plan_prices_new "
                    "(id, plan_id, period_days, price_kopeks, audience, created_at, updated_at) "
                    "SELECT id, plan_id, period_days, price_kopeks, 'all', created_at, updated_at "
                    "FROM subscription_plan_prices"
                ))
                await conn.execute(text("DROP TABLE subscription_plan_prices"))
                await conn.execute(text(
                    "ALTER TABLE subscription_plan_prices_new RENAME TO subscription_plan_prices"
                ))
                await conn.execute(text(
                    "CREATE INDEX ix_subscription_plan_prices_plan_id "
                    "ON subscription_plan_prices(plan_id)"
                ))
            elif db_type == 'postgresql':
                await conn.execute(text(
                    "ALTER TABLE subscription_plan_prices "
                    "ADD COLUMN audience VARCHAR(8) NOT NULL DEFAULT 'all'"
                ))
                await conn.execute(text(
                    "ALTER TABLE subscription_plan_prices DROP CONSTRAINT IF EXISTS uq_plan_period"
                ))
                await conn.execute(text(
                    "ALTER TABLE subscription_plan_prices "
                    "ADD CONSTRAINT uq_plan_period UNIQUE (plan_id, period_days, audience)"
                ))
            elif db_type == 'mysql':
                await conn.execute(text(
                    "ALTER TABLE subscription_plan_prices "
                    "ADD COLUMN audience VARCHAR(8) NOT NULL DEFAULT 'all'"
                ))
                try:
                    await conn.execute(text(
                        "ALTER TABLE subscription_plan_prices DROP INDEX uq_plan_period"
                    ))
                except Exception as drop_err:
                    logger.warning(f"uq_plan_period не удалён (mysql): {drop_err}")
                await conn.execute(text(
                    "ALTER TABLE subscription_plan_prices "
                    "ADD CONSTRAINT uq_plan_period UNIQUE (plan_id, period_days, audience)"
                ))
            else:
                logger.error(f"Неподдерживаемый тип БД для audience: {db_type}")
                return False

            # 2. Grandfather Solo/Plus/Pro 1-month and 6-month rows as 'legacy'.
            await conn.execute(text(
                "UPDATE subscription_plan_prices SET audience='legacy' "
                "WHERE period_days IN (30, 180) AND plan_id IN ("
                "SELECT id FROM subscription_plans WHERE code IN ('solo', 'plus', 'pro'))"
            ))

            # 3. Insert the new-cohort 1-month prices.
            for code, price in _NEW_MONTHLY_PRICES.items():
                row = (await conn.execute(
                    text("SELECT id FROM subscription_plans WHERE code = :code"),
                    {"code": code},
                )).fetchone()
                if not row:
                    continue
                plan_id = row[0]
                exists = (await conn.execute(
                    text(
                        "SELECT id FROM subscription_plan_prices "
                        "WHERE plan_id = :p AND period_days = 30 AND audience = 'new'"
                    ),
                    {"p": plan_id},
                )).fetchone()
                if exists:
                    continue
                await conn.execute(
                    text(
                        "INSERT INTO subscription_plan_prices "
                        "(plan_id, period_days, price_kopeks, audience) "
                        "VALUES (:p, 30, :price, 'new')"
                    ),
                    {"p": plan_id, "price": price},
                )

        logger.info("✅ Колонка audience добавлена, цены Solo/Plus/Pro разделены на когорты")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления audience в subscription_plan_prices: {e}")
        return False


# Default tiered-plan catalogue. Edit before deploy or via SQL afterwards;
# seed_subscription_plans() only inserts plans/prices that are not present yet.
_SUBSCRIPTION_PLAN_SEED = [
    {
        "code": "app",
        "display_name": "App",
        "device_limit": 1,
        "traffic_limit_gb": 30,
        "traffic_reset_strategy": "MONTH",
        "custom_app_only": True,
        "priority_support": False,
        "sort_order": 10,
        "is_active": False,
        "description_md": (
            "App (только для Android)\n"
            "• 1 устройство\n"
            "• VPN только для приложений\n"
            "• 30 ГБ/мес."
        ),
        # (period_days, audience, price_kopeks). 'all' = everyone; 'legacy'/'new' split
        # the cohorts. App is not repriced, so all rows are 'all'.
        "prices": [
            (30, "all", 12000),
            (90, "all", 30000),
            (180, "all", 54000),
            (360, "all", 99000),
            (720, "all", 180000),
        ],
    },
    {
        "code": "solo",
        "display_name": "Solo",
        "device_limit": 1,
        "traffic_limit_gb": 0,
        "traffic_reset_strategy": "NO_RESET",
        "custom_app_only": False,
        "priority_support": False,
        "sort_order": 20,
        "description_md": (
            "Solo\n"
            "• 1 устройство\n"
            "• полный VPN, все обходы\n"
            "• ♾️ трафик"
        ),
        "prices": [
            (30, "legacy", 22000),
            (30, "new", 32000),
            (90, "legacy", 60000),
            (90, "new", 57000),
            (180, "legacy", 108000),
            (180, "new", 89000),
            (360, "legacy", 192000),
            (360, "new", 156000),
            (720, "legacy", 336000),
            (720, "new", 216000),
        ],
    },
    {
        "code": "plus",
        "display_name": "Plus",
        "device_limit": 3,
        "traffic_limit_gb": 0,
        "traffic_reset_strategy": "NO_RESET",
        "custom_app_only": False,
        "priority_support": False,
        "sort_order": 30,
        "description_md": (
            "Plus\n"
            "• 3 устройства\n"
            "• полный VPN, все обходы\n"
            "• Youtube без рекламы\n"
            "• ♾️ трафик"
        ),
        "prices": [
            (30, "legacy", 29000),
            (30, "new", 49000),
            (90, "legacy", 81000),
            (90, "new", 78000),
            (180, "legacy", 150000),
            (180, "new", 119000),
            (360, "legacy", 264000),
            (360, "new", 192000),
            (720, "legacy", 456000),
            (720, "new", 264000),
        ],
    },
    {
        "code": "pro",
        "display_name": "Pro",
        "device_limit": 10,
        "traffic_limit_gb": 0,
        "traffic_reset_strategy": "NO_RESET",
        "custom_app_only": False,
        "priority_support": True,
        "sort_order": 40,
        "description_md": (
            "Pro\n"
            "• 10 устройств\n"
            "• полный VPN, все обходы\n"
            "• Youtube без рекламы\n"
            "• ♾️ трафик\n"
            "• приоритетные серверы и поддержка"
        ),
        "prices": [
            (30, "legacy", 39000),
            (30, "new", 69000),
            (90, "legacy", 108000),
            (90, "new", 99000),
            (180, "legacy", 198000),
            (180, "new", 149000),
            (360, "legacy", 348000),
            (360, "new", 228000),
            (720, "legacy", 600000),
            (720, "new", 360000),
        ],
    },
]


async def seed_subscription_plans() -> bool:
    """Insert default tiered plans + prices if absent. Idempotent."""
    try:
        async with engine.begin() as conn:
            for plan in _SUBSCRIPTION_PLAN_SEED:
                code = plan["code"]
                existing = await conn.execute(
                    text("SELECT id FROM subscription_plans WHERE code = :code"),
                    {"code": code},
                )
                row = existing.fetchone()
                if row:
                    plan_id = row[0]
                else:
                    await conn.execute(
                        text(
                            "INSERT INTO subscription_plans "
                            "(code, display_name, device_limit, traffic_limit_gb, traffic_reset_strategy, "
                            "custom_app_only, priority_support, sort_order, is_active, description_md) "
                            "VALUES (:code, :display_name, :device_limit, :traffic_limit_gb, :traffic_reset_strategy, "
                            ":custom_app_only, :priority_support, :sort_order, :is_active, :description_md)"
                        ),
                        {
                            "code": code,
                            "display_name": plan["display_name"],
                            "device_limit": plan["device_limit"],
                            "traffic_limit_gb": plan["traffic_limit_gb"],
                            "traffic_reset_strategy": plan["traffic_reset_strategy"],
                            "custom_app_only": plan["custom_app_only"],
                            "priority_support": plan["priority_support"],
                            "sort_order": plan["sort_order"],
                            "is_active": plan.get("is_active", True),
                            "description_md": plan["description_md"],
                        },
                    )
                    new_row = await conn.execute(
                        text("SELECT id FROM subscription_plans WHERE code = :code"),
                        {"code": code},
                    )
                    plan_id = new_row.fetchone()[0]
                    logger.info(f"  → План '{code}' создан (id={plan_id})")

                for period_days, audience, price_kopeks in plan["prices"]:
                    existing_price = await conn.execute(
                        text(
                            "SELECT id FROM subscription_plan_prices "
                            "WHERE plan_id = :plan_id AND period_days = :period_days "
                            "AND audience = :audience"
                        ),
                        {
                            "plan_id": plan_id,
                            "period_days": period_days,
                            "audience": audience,
                        },
                    )
                    if existing_price.fetchone():
                        continue
                    await conn.execute(
                        text(
                            "INSERT INTO subscription_plan_prices "
                            "(plan_id, period_days, price_kopeks, audience) "
                            "VALUES (:plan_id, :period_days, :price_kopeks, :audience)"
                        ),
                        {
                            "plan_id": plan_id,
                            "period_days": period_days,
                            "price_kopeks": price_kopeks,
                            "audience": audience,
                        },
                    )
                    logger.info(
                        f"  → Цена '{code}' × {period_days} дн. [{audience}] "
                        f"= {price_kopeks} коп. добавлена"
                    )
        return True
    except Exception as e:
        logger.error(f"Ошибка сидирования subscription_plans: {e}")
        return False


async def update_plus_plan_device_limit_to_three() -> bool:
    """One-off bump of the Plus tier: 2 → 3 devices.

    Updates the plan row (limit + description), bumps existing Plus subscriptions
    that still carry the old limit and pushes the new HWID limit to RemnaWave for
    their owners — otherwise the panel keeps enforcing 2 devices and the
    panel→DB auto-sync would revert subscription.device_limit. Idempotent and
    self-healing: Plus subscriptions left at 2 are retried on every startup.
    """
    try:
        affected: List[Tuple[int, str]] = []
        async with engine.begin() as conn:
            plan_row = (await conn.execute(
                text("SELECT id, device_limit FROM subscription_plans WHERE code = 'plus'")
            )).fetchone()
            if not plan_row:
                return True  # свежая установка — план засеется сразу с лимитом 3

            plan_id, device_limit = plan_row[0], plan_row[1]
            if device_limit == 2:
                await conn.execute(text(
                    "UPDATE subscription_plans SET device_limit = 3 WHERE id = :p"
                ), {"p": plan_id})
                await conn.execute(text(
                    "UPDATE subscription_plans "
                    "SET description_md = REPLACE(description_md, '2 устройства', '3 устройства') "
                    "WHERE id = :p AND description_md LIKE '%2 устройства%'"
                ), {"p": plan_id})
                device_limit = 3
                logger.info("  → План 'plus': лимит устройств 2 → 3")

            if device_limit != 3:
                # Лимит менялся вручную — существующие подписки не трогаем.
                return True

            rows = (await conn.execute(text(
                "SELECT s.id, u.remnawave_uuid FROM subscriptions s "
                "JOIN users u ON u.id = s.user_id "
                "WHERE s.plan_id = :p AND s.device_limit = 2"
            ), {"p": plan_id})).fetchall()
            if rows:
                await conn.execute(text(
                    "UPDATE subscriptions SET device_limit = 3 "
                    "WHERE plan_id = :p AND device_limit = 2"
                ), {"p": plan_id})
                affected = [(row[0], row[1]) for row in rows]
                logger.info(f"  → Подписок Plus обновлено до 3 устройств: {len(rows)}")

        uuids = [uuid for _, uuid in affected if uuid]
        if uuids:
            from app.services.subscription_service import SubscriptionService

            service = SubscriptionService()
            if not service.is_configured:
                logger.warning(
                    "⚠️ RemnaWave API не настроен — лимит 3 устройств для %d подписок Plus "
                    "не отправлен в панель (обновится при следующем запуске)",
                    len(uuids),
                )
                return True

            pushed = 0
            async with service.get_api_client() as api:
                for uuid in uuids:
                    try:
                        await api.update_user(uuid=uuid, hwid_device_limit=3)
                        pushed += 1
                    except Exception as push_error:
                        logger.warning(
                            f"⚠️ Не удалось обновить hwidDeviceLimit в панели для {uuid}: {push_error}"
                        )
            logger.info(f"  → HWID-лимит 3 отправлен в панель: {pushed}/{len(uuids)}")
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления лимита устройств тарифа Plus: {e}")
        return False


async def update_plan_descriptions_vpn_wording() -> bool:
    """One-off wording fix in plan descriptions:
    «полный VPN и все обходы» → «полный VPN, доступ ко всем сервисам».
    Idempotent: REPLACE only touches rows that still contain the old phrase.
    """
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(
                "UPDATE subscription_plans "
                "SET description_md = REPLACE(description_md, "
                "'полный VPN и все обходы', 'полный VPN, доступ ко всем сервисам') "
                "WHERE description_md LIKE '%полный VPN и все обходы%'"
            ))
            if result.rowcount:
                logger.info(f"  → Описания тарифов обновлены: {result.rowcount}")
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления описаний тарифов: {e}")
        return False


_LEGACY_MIGRATION_PLAN_CODES = ("solo", "plus", "pro")
_LEGACY_MIGRATION_PERIOD_DAYS = 30


def _pick_plan_for_legacy_subscription(
    device_limit: Optional[int],
    traffic_limit_gb: Optional[int],
    plans: List[dict],
) -> Tuple[int, int, int]:
    """Map a legacy à-la-carte subscription onto a tiered plan.

    plans — [{'id','code','device_limit','traffic_limit_gb'}, ...] sorted by device_limit.
    Returns (plan_id, new_device_limit, new_traffic_limit_gb).

    Rules:
      * cheapest plan whose device_limit covers the legacy one (1→Solo, 2-3→Plus, 4-10→Pro);
        above every plan's limit (legacy allows up to MAX_DEVICES_LIMIT=20) we take the top
        plan but KEEP the higher legacy limit — a migration must never take devices away.
      * traffic: the plan's value (0 = unlimited for Solo/Plus/Pro), unless that would be a
        downgrade — a legacy subscription that already has unlimited or a bigger quota than
        the plan keeps what it has.
    """
    legacy_devices = device_limit if device_limit and device_limit > 0 else 1

    plan = next((p for p in plans if p["device_limit"] >= legacy_devices), None)
    if plan is None:
        plan = plans[-1]

    new_device_limit = max(legacy_devices, plan["device_limit"])

    current_traffic = traffic_limit_gb or 0
    plan_traffic = plan["traffic_limit_gb"] or 0
    if plan_traffic == 0:
        new_traffic_gb = 0  # план безлимитный — всегда апгрейд
    elif current_traffic == 0 or current_traffic > plan_traffic:
        new_traffic_gb = current_traffic  # у подписки больше/безлимит — не понижаем
    else:
        new_traffic_gb = plan_traffic

    return plan["id"], new_device_limit, new_traffic_gb


async def _load_migration_target_plans(conn) -> Optional[List[dict]]:
    """Active Solo/Plus/Pro rows ordered by device_limit. None if the catalogue is incomplete."""
    true_literal = "1" if await get_database_type() == 'sqlite' else "true"
    rows = (await conn.execute(text(
        "SELECT id, code, device_limit, traffic_limit_gb FROM subscription_plans "
        f"WHERE code IN ('solo', 'plus', 'pro') AND is_active = {true_literal}"
    ))).fetchall()

    plans = [
        {
            "id": row[0],
            "code": row[1],
            "device_limit": int(row[2] or 1),
            "traffic_limit_gb": int(row[3] or 0),
        }
        for row in rows
    ]
    found = {p["code"] for p in plans}
    if not set(_LEGACY_MIGRATION_PLAN_CODES).issubset(found):
        logger.warning(
            "⚠️ Перевод легаси-подписок пропущен: в каталоге нет активных тарифов %s",
            sorted(set(_LEGACY_MIGRATION_PLAN_CODES) - found),
        )
        return None

    plans.sort(key=lambda p: p["device_limit"])
    return plans


async def _push_limits_to_panel(payloads: List[Tuple[str, int, int]]) -> None:
    """Send hwid_device_limit / traffic_limit_bytes to RemnaWave for migrated subscriptions.

    Uses the partial PATCH (api.update_user) on purpose instead of
    SubscriptionService.create_remnawave_user: the latter calls reset_user_devices(), which
    would unbind every migrated user's devices, and it also forces status=ACTIVE and
    rewrites active_internal_squads — none of which this migration should touch.
    Best-effort per user: one failure must not abort the pass (the self-healing sweep on the
    next startup retries it).
    """
    if not payloads:
        return

    from app.services.subscription_service import SubscriptionService

    service = SubscriptionService()
    if not service.is_configured:
        logger.warning(
            "⚠️ RemnaWave API не настроен — лимиты для %d подписок не отправлены в панель "
            "(будут досланы при следующем запуске)",
            len(payloads),
        )
        return

    pushed = 0
    async with service.get_api_client() as api:
        for uuid, device_limit, traffic_gb in payloads:
            try:
                await api.update_user(
                    uuid=uuid,
                    hwid_device_limit=device_limit,
                    traffic_limit_bytes=0 if not traffic_gb else traffic_gb * 1024 * 1024 * 1024,
                )
                pushed += 1
            except Exception as push_error:
                logger.warning(
                    f"⚠️ Не удалось отправить лимиты в панель для {uuid}: {push_error}"
                )
    logger.info(f"  → Лимиты отправлены в панель: {pushed}/{len(payloads)}")


async def _heal_plan_subscriptions_below_plan_limit() -> None:
    """Catch-up sweep: raise plan subscriptions whose device_limit dropped below their plan.

    Happens when the DB update landed but the panel push failed — the panel→DB sync
    (`_update_subscription_from_panel_data`) then reverts device_limit to the panel value.
    Only touches rows strictly BELOW the plan limit, so Pro subscriptions that kept a higher
    legacy limit (11-20 devices) are left alone.

    Side effect worth knowing: if an admin deliberately lowered a tariff user's device limit
    below their plan, this sweep puts it back.
    """
    healed: List[Tuple[str, int, int]] = []
    async with engine.begin() as conn:
        rows = (await conn.execute(text(
            "SELECT s.id, p.device_limit, s.traffic_limit_gb, u.remnawave_uuid "
            "FROM subscriptions s "
            "JOIN subscription_plans p ON p.id = s.plan_id "
            "JOIN users u ON u.id = s.user_id "
            "WHERE s.plan_id IS NOT NULL AND s.device_limit < p.device_limit"
        ))).fetchall()

        for sub_id, plan_device_limit, traffic_gb, remnawave_uuid in rows:
            await conn.execute(
                text("UPDATE subscriptions SET device_limit = :d WHERE id = :id"),
                {"d": int(plan_device_limit), "id": sub_id},
            )
            if remnawave_uuid:
                healed.append((remnawave_uuid, int(plan_device_limit), int(traffic_gb or 0)))

    if rows:
        logger.info(f"  → Восстановлен плановый лимит устройств у подписок: {len(rows)}")
        await _push_limits_to_panel(healed)


async def migrate_legacy_subscriptions_to_plans() -> bool:
    """One-off transfer of active paid legacy subscriptions (plan_id IS NULL) onto tariffs.

    Gated by LEGACY_TARIFF_MIGRATION_MODE (off | dry | apply) and optionally narrowed to a
    pilot via LEGACY_TARIFF_MIGRATION_TELEGRAM_IDS. Idempotent: the WHERE clause only matches
    subscriptions that still have no plan.

    Scope: status='active', end_date in the future, not trial, not partner. end_date,
    start_date and connected_squads are preserved as-is; only plan_id, plan_period_days,
    device_limit and traffic_limit_gb change, and never downwards.

    A DB-only update is not enough — the RemnaWave panel keeps enforcing the old HWID limit
    and the panel→DB sync would revert device_limit — so every changed subscription is also
    pushed to the panel (same hazard documented in update_plus_plan_device_limit_to_three).
    """
    mode = settings.get_legacy_tariff_migration_mode()
    if mode == "off":
        return True

    try:
        pilot_ids = settings.get_legacy_tariff_migration_telegram_ids()
        now = datetime.utcnow()
        db_type = await get_database_type()
        false_literal = "0" if db_type == 'sqlite' else "false"

        updates: List[dict] = []
        payloads: List[Tuple[str, int, int]] = []
        by_plan: dict = {}
        kept_higher_limit = 0

        async with engine.begin() as conn:
            plans = await _load_migration_target_plans(conn)
            if plans is None:
                return True
            plans_by_id = {p["id"]: p["code"] for p in plans}

            query = (
                "SELECT s.id, s.device_limit, s.traffic_limit_gb, u.remnawave_uuid, u.telegram_id "
                "FROM subscriptions s JOIN users u ON u.id = s.user_id "
                "WHERE s.plan_id IS NULL "
                f"AND COALESCE(s.is_trial, {false_literal}) = {false_literal} "
                f"AND COALESCE(s.is_partner, {false_literal}) = {false_literal} "
                "AND s.status = 'active' AND s.end_date > :now"
            )
            params: dict = {"now": now}
            if pilot_ids:
                placeholders = ", ".join(f":tg{i}" for i in range(len(pilot_ids)))
                query += f" AND u.telegram_id IN ({placeholders})"
                params.update({f"tg{i}": tid for i, tid in enumerate(pilot_ids)})

            candidates = (await conn.execute(text(query), params)).fetchall()

            for sub_id, device_limit, traffic_gb, remnawave_uuid, telegram_id in candidates:
                plan_id, new_device_limit, new_traffic_gb = _pick_plan_for_legacy_subscription(
                    device_limit, traffic_gb, plans
                )
                plan_code = plans_by_id[plan_id]
                by_plan[plan_code] = by_plan.get(plan_code, 0) + 1
                legacy_devices = device_limit if device_limit and device_limit > 0 else 1
                if new_device_limit > max(p["device_limit"] for p in plans):
                    kept_higher_limit += 1

                logger.info(
                    "  → tg=%s: %s устр. / %s → тариф %s, %s устр. / %s",
                    telegram_id,
                    legacy_devices,
                    "безлимит" if not traffic_gb else f"{traffic_gb} ГБ",
                    plan_code,
                    new_device_limit,
                    "безлимит" if not new_traffic_gb else f"{new_traffic_gb} ГБ",
                )

                updates.append({
                    "id": sub_id,
                    "plan_id": plan_id,
                    "device_limit": new_device_limit,
                    "traffic_gb": new_traffic_gb,
                })
                if remnawave_uuid:
                    payloads.append((remnawave_uuid, new_device_limit, new_traffic_gb))

            summary = ", ".join(f"{code}: {by_plan.get(code, 0)}" for code in _LEGACY_MIGRATION_PLAN_CODES)
            logger.info(
                "  → Кандидатов на перевод: %d (%s), с сохранённым повышенным лимитом устройств: %d",
                len(updates), summary, kept_higher_limit,
            )

            if mode == "dry":
                logger.info("  → Режим dry — изменения не применяются")
                return True

            for item in updates:
                # plan_id IS NULL в WHERE — защита от гонки с параллельной покупкой тарифа.
                await conn.execute(
                    text(
                        "UPDATE subscriptions SET plan_id = :plan_id, "
                        "plan_period_days = :period, device_limit = :device_limit, "
                        "traffic_limit_gb = :traffic_gb, updated_at = :now "
                        "WHERE id = :id AND plan_id IS NULL"
                    ),
                    {
                        "plan_id": item["plan_id"],
                        "period": _LEGACY_MIGRATION_PERIOD_DAYS,
                        "device_limit": item["device_limit"],
                        "traffic_gb": item["traffic_gb"],
                        "now": now,
                        "id": item["id"],
                    },
                )

        if updates:
            logger.info(f"  → Переведено подписок на тарифы: {len(updates)}")
            await _push_limits_to_panel(payloads)

        await _heal_plan_subscriptions_below_plan_limit()
        return True
    except Exception as e:
        logger.error(f"Ошибка перевода легаси-подписок на тарифы: {e}")
        return False


async def create_advertising_campaigns_table() -> bool:
    if await check_table_exists('advertising_campaigns'):
        logger.info("ℹ️ Таблица advertising_campaigns уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE advertising_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(255) NOT NULL,
                    start_parameter VARCHAR(64) NOT NULL,
                    bonus_type VARCHAR(20) NOT NULL,
                    balance_bonus_kopeks INTEGER NOT NULL DEFAULT 0,
                    subscription_duration_days INTEGER NULL,
                    subscription_traffic_gb INTEGER NULL,
                    subscription_device_limit INTEGER NULL,
                    subscription_squads TEXT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_by INTEGER NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE UNIQUE INDEX ix_advertising_campaigns_start_parameter ON advertising_campaigns(start_parameter);
                CREATE TABLE advertising_campaign_registrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    bonus_type VARCHAR(20) NOT NULL,
                    balance_bonus_kopeks INTEGER NOT NULL DEFAULT 0,
                    subscription_duration_days INTEGER NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_campaign_user UNIQUE (campaign_id, user_id),
                    FOREIGN KEY(campaign_id) REFERENCES advertising_campaigns(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS advertising_campaigns (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    start_parameter VARCHAR(64) NOT NULL,
                    bonus_type VARCHAR(20) NOT NULL,
                    balance_bonus_kopeks INTEGER NOT NULL DEFAULT 0,
                    subscription_duration_days INTEGER NULL,
                    subscription_traffic_gb INTEGER NULL,
                    subscription_device_limit INTEGER NULL,
                    subscription_squads JSON NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ix_advertising_campaigns_start_parameter ON advertising_campaigns(start_parameter);
                CREATE TABLE IF NOT EXISTS advertising_campaign_registrations (
                    id SERIAL PRIMARY KEY,
                    campaign_id INTEGER NOT NULL REFERENCES advertising_campaigns(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    bonus_type VARCHAR(20) NOT NULL,
                    balance_bonus_kopeks INTEGER NOT NULL DEFAULT 0,
                    subscription_duration_days INTEGER NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_campaign_user UNIQUE (campaign_id, user_id)
                )
                """
            else:
                create_sql = """
                CREATE TABLE IF NOT EXISTS advertising_campaigns (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    start_parameter VARCHAR(64) NOT NULL,
                    bonus_type VARCHAR(20) NOT NULL,
                    balance_bonus_kopeks INT NOT NULL DEFAULT 0,
                    subscription_duration_days INT NULL,
                    subscription_traffic_gb INT NULL,
                    subscription_device_limit INT NULL,
                    subscription_squads JSON NULL,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by INT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE UNIQUE INDEX ix_advertising_campaigns_start_parameter ON advertising_campaigns(start_parameter);
                CREATE TABLE IF NOT EXISTS advertising_campaign_registrations (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    campaign_id INT NOT NULL,
                    user_id INT NOT NULL,
                    bonus_type VARCHAR(20) NOT NULL,
                    balance_bonus_kopeks INT NOT NULL DEFAULT 0,
                    subscription_duration_days INT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_campaign_user UNIQUE (campaign_id, user_id),
                    FOREIGN KEY(campaign_id) REFERENCES advertising_campaigns(id) ON DELETE CASCADE,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                )
                """

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблицы advertising_campaigns и advertising_campaign_registrations созданы")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы advertising_campaigns: {e}")
        return False


async def add_ad_attribution_columns() -> bool:
    raw_exists = await check_column_exists('users', 'raw_start_payload')
    source_exists = await check_column_exists('users', 'attribution_source')
    campaign_id_exists = await check_column_exists('users', 'attribution_campaign_id')

    if raw_exists and source_exists and campaign_id_exists:
        logger.info("ℹ️ Колонки ad attribution в users уже существуют")
        return True

    try:
        db_type = await get_database_type()
        async with engine.begin() as conn:
            if not raw_exists:
                await conn.execute(text("ALTER TABLE users ADD COLUMN raw_start_payload VARCHAR(64) NULL"))
            if not source_exists:
                await conn.execute(text("ALTER TABLE users ADD COLUMN attribution_source VARCHAR(100) NULL"))
            if not campaign_id_exists:
                await conn.execute(text("ALTER TABLE users ADD COLUMN attribution_campaign_id VARCHAR(8) NULL"))
                if db_type == 'postgresql':
                    await conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_users_attribution_campaign_id "
                        "ON users(attribution_campaign_id)"
                    ))
                else:
                    await conn.execute(text(
                        "CREATE INDEX ix_users_attribution_campaign_id ON users(attribution_campaign_id)"
                    ))

        logger.info("✅ Колонки raw_start_payload, attribution_source, attribution_campaign_id добавлены в users")
        return True

    except Exception as error:
        logger.error(f"❌ Ошибка добавления колонок ad attribution в users: {error}")
        return False


async def add_user_language_code_column() -> bool:
    if await check_column_exists('users', 'language_code'):
        logger.info("ℹ️ Колонка users.language_code уже существует")
        return True

    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE users ADD COLUMN language_code VARCHAR(10) NULL"))

        logger.info("✅ Колонка language_code добавлена в users")
        return True

    except Exception as error:
        logger.error(f"❌ Ошибка добавления колонки language_code в users: {error}")
        return False


async def create_ad_campaign_visits_table() -> bool:
    if await check_table_exists('ad_campaign_visits'):
        logger.info("ℹ️ Таблица ad_campaign_visits уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE ad_campaign_visits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id BIGINT NOT NULL,
                    user_id INTEGER NULL,
                    raw_payload VARCHAR(64) NOT NULL,
                    source VARCHAR(100) NOT NULL,
                    campaign_id VARCHAR(8) NOT NULL,
                    is_new_user BOOLEAN NOT NULL DEFAULT 0,
                    visited_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE INDEX ix_ad_campaign_visits_telegram_id ON ad_campaign_visits(telegram_id);
                CREATE INDEX ix_ad_campaign_visits_user_id ON ad_campaign_visits(user_id);
                CREATE INDEX ix_ad_campaign_visits_campaign_id ON ad_campaign_visits(campaign_id)
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS ad_campaign_visits (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                    raw_payload VARCHAR(64) NOT NULL,
                    source VARCHAR(100) NOT NULL,
                    campaign_id VARCHAR(8) NOT NULL,
                    is_new_user BOOLEAN NOT NULL DEFAULT FALSE,
                    visited_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS ix_ad_campaign_visits_telegram_id ON ad_campaign_visits(telegram_id);
                CREATE INDEX IF NOT EXISTS ix_ad_campaign_visits_user_id ON ad_campaign_visits(user_id);
                CREATE INDEX IF NOT EXISTS ix_ad_campaign_visits_campaign_id ON ad_campaign_visits(campaign_id)
                """
            else:
                create_sql = """
                CREATE TABLE IF NOT EXISTS ad_campaign_visits (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    user_id INT NULL,
                    raw_payload VARCHAR(64) NOT NULL,
                    source VARCHAR(100) NOT NULL,
                    campaign_id VARCHAR(8) NOT NULL,
                    is_new_user BOOLEAN NOT NULL DEFAULT FALSE,
                    visited_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE INDEX ix_ad_campaign_visits_telegram_id ON ad_campaign_visits(telegram_id);
                CREATE INDEX ix_ad_campaign_visits_user_id ON ad_campaign_visits(user_id);
                CREATE INDEX ix_ad_campaign_visits_campaign_id ON ad_campaign_visits(campaign_id)
                """

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица ad_campaign_visits создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы ad_campaign_visits: {e}")
        return False


async def add_rays_columns() -> bool:
    """Добавляет колонки лучей в users: rays_balance, rays_lifetime_earned, rays_credited."""
    try:
        balance_exists = await check_column_exists('users', 'rays_balance')
        lifetime_exists = await check_column_exists('users', 'rays_lifetime_earned')
        credited_exists = await check_column_exists('users', 'rays_credited')

        if balance_exists and lifetime_exists and credited_exists:
            logger.info("ℹ️ Колонки лучей уже существуют")
            return True

        async with engine.begin() as conn:
            db_type = await get_database_type()
            int_def = "INTEGER NOT NULL DEFAULT 0" if db_type != 'mysql' else "INT NOT NULL DEFAULT 0"
            if db_type == 'sqlite':
                bool_def = "BOOLEAN NOT NULL DEFAULT 0"
            elif db_type == 'mysql':
                bool_def = "TINYINT(1) NOT NULL DEFAULT 0"
            else:
                bool_def = "BOOLEAN NOT NULL DEFAULT FALSE"

            if not balance_exists:
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN rays_balance {int_def}"))
                logger.info("✅ Добавлена колонка users.rays_balance")
            if not lifetime_exists:
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN rays_lifetime_earned {int_def}"))
                logger.info("✅ Добавлена колонка users.rays_lifetime_earned")
            if not credited_exists:
                await conn.execute(text(f"ALTER TABLE users ADD COLUMN rays_credited {bool_def}"))
                logger.info("✅ Добавлена колонка users.rays_credited")

        return True

    except Exception as error:
        logger.error(f"Ошибка добавления колонок лучей: {error}")
        return False


async def create_ray_transactions_table() -> bool:
    """Создаёт append-only журнал лучей ray_transactions."""
    table_exists = await check_table_exists('ray_transactions')
    if table_exists:
        logger.info("Таблица ray_transactions уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE ray_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    type VARCHAR(50) NOT NULL,
                    referral_id INTEGER NULL,
                    source_transaction_id INTEGER NULL,
                    period_days INTEGER NULL,
                    description TEXT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_ray_transactions_source_transaction_id UNIQUE (source_transaction_id),
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(referral_id) REFERENCES users(id),
                    FOREIGN KEY(source_transaction_id) REFERENCES transactions(id)
                );
                CREATE INDEX ix_ray_transactions_user_id ON ray_transactions(user_id);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS ray_transactions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    amount INTEGER NOT NULL,
                    type VARCHAR(50) NOT NULL,
                    referral_id INTEGER NULL REFERENCES users(id),
                    source_transaction_id INTEGER NULL REFERENCES transactions(id),
                    period_days INTEGER NULL,
                    description TEXT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    CONSTRAINT uq_ray_transactions_source_transaction_id UNIQUE (source_transaction_id)
                );
                CREATE INDEX IF NOT EXISTS ix_ray_transactions_user_id ON ray_transactions(user_id);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS ray_transactions (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    amount INT NOT NULL,
                    type VARCHAR(50) NOT NULL,
                    referral_id INT NULL,
                    source_transaction_id INT NULL,
                    period_days INT NULL,
                    description TEXT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_ray_transactions_source_transaction_id UNIQUE (source_transaction_id),
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(referral_id) REFERENCES users(id),
                    FOREIGN KEY(source_transaction_id) REFERENCES transactions(id)
                );
                CREATE INDEX ix_ray_transactions_user_id ON ray_transactions(user_id);
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица ray_transactions успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы ray_transactions: {e}")
        return False


async def create_ray_prize_claims_table() -> bool:
    """Создаёт таблицу заявок на призы Магазина Наград ray_prize_claims."""
    table_exists = await check_table_exists('ray_prize_claims')
    if table_exists:
        logger.info("Таблица ray_prize_claims уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE ray_prize_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    prize_code VARCHAR(32) NOT NULL,
                    prize_title VARCHAR(128) NOT NULL,
                    cost_rays INTEGER NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    spend_transaction_id INTEGER NULL,
                    refund_transaction_id INTEGER NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME NULL,
                    cancelled_at DATETIME NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(spend_transaction_id) REFERENCES ray_transactions(id),
                    FOREIGN KEY(refund_transaction_id) REFERENCES ray_transactions(id)
                );
                CREATE INDEX ix_ray_prize_claims_user_id ON ray_prize_claims(user_id);
                CREATE INDEX ix_ray_prize_claims_status ON ray_prize_claims(status);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS ray_prize_claims (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    prize_code VARCHAR(32) NOT NULL,
                    prize_title VARCHAR(128) NOT NULL,
                    cost_rays INTEGER NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    spend_transaction_id INTEGER NULL REFERENCES ray_transactions(id),
                    refund_transaction_id INTEGER NULL REFERENCES ray_transactions(id),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP NULL,
                    cancelled_at TIMESTAMP NULL
                );
                CREATE INDEX IF NOT EXISTS ix_ray_prize_claims_user_id ON ray_prize_claims(user_id);
                CREATE INDEX IF NOT EXISTS ix_ray_prize_claims_status ON ray_prize_claims(status);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS ray_prize_claims (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    prize_code VARCHAR(32) NOT NULL,
                    prize_title VARCHAR(128) NOT NULL,
                    cost_rays INT NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    spend_transaction_id INT NULL,
                    refund_transaction_id INT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME NULL,
                    cancelled_at DATETIME NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(spend_transaction_id) REFERENCES ray_transactions(id),
                    FOREIGN KEY(refund_transaction_id) REFERENCES ray_transactions(id),
                    INDEX ix_ray_prize_claims_user_id (user_id),
                    INDEX ix_ray_prize_claims_status (status)
                );
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица ray_prize_claims успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы ray_prize_claims: {e}")
        return False


async def add_ray_prize_claim_contact_column() -> bool:
    """Добавляет ray_prize_claims.contact — TG-контакт из формы кабинета сайта."""
    try:
        contact_exists = await check_column_exists('ray_prize_claims', 'contact')
        if contact_exists:
            logger.info("ℹ️ Колонка ray_prize_claims.contact уже существует")
            return True

        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE ray_prize_claims ADD COLUMN contact VARCHAR(255) NULL"))
            logger.info("✅ Добавлена колонка ray_prize_claims.contact")

        return True

    except Exception as error:
        logger.error(f"Ошибка добавления колонки ray_prize_claims.contact: {error}")
        return False


async def create_withdrawal_requests_table() -> bool:
    """Создаёт таблицу заявок на вывод реферальных рублей withdrawal_requests."""
    table_exists = await check_table_exists('withdrawal_requests')
    if table_exists:
        logger.info("Таблица withdrawal_requests уже существует")
        return True

    try:
        async with engine.begin() as conn:
            db_type = await get_database_type()

            if db_type == 'sqlite':
                create_sql = """
                CREATE TABLE withdrawal_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_kopeks INTEGER NOT NULL,
                    details VARCHAR(255) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    debit_transaction_id INTEGER NULL,
                    refund_transaction_id INTEGER NULL,
                    processed_by BIGINT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    processed_at DATETIME NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(debit_transaction_id) REFERENCES transactions(id),
                    FOREIGN KEY(refund_transaction_id) REFERENCES transactions(id)
                );
                CREATE INDEX ix_withdrawal_requests_user_id ON withdrawal_requests(user_id);
                CREATE INDEX ix_withdrawal_requests_status ON withdrawal_requests(status);
                """
            elif db_type == 'postgresql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS withdrawal_requests (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    amount_kopeks INTEGER NOT NULL,
                    details VARCHAR(255) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    debit_transaction_id INTEGER NULL REFERENCES transactions(id),
                    refund_transaction_id INTEGER NULL REFERENCES transactions(id),
                    processed_by BIGINT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    processed_at TIMESTAMP NULL
                );
                CREATE INDEX IF NOT EXISTS ix_withdrawal_requests_user_id ON withdrawal_requests(user_id);
                CREATE INDEX IF NOT EXISTS ix_withdrawal_requests_status ON withdrawal_requests(status);
                """
            elif db_type == 'mysql':
                create_sql = """
                CREATE TABLE IF NOT EXISTS withdrawal_requests (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    amount_kopeks INT NOT NULL,
                    details VARCHAR(255) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    debit_transaction_id INT NULL,
                    refund_transaction_id INT NULL,
                    processed_by BIGINT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    processed_at DATETIME NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(debit_transaction_id) REFERENCES transactions(id),
                    FOREIGN KEY(refund_transaction_id) REFERENCES transactions(id),
                    INDEX ix_withdrawal_requests_user_id (user_id),
                    INDEX ix_withdrawal_requests_status (status)
                );
                """
            else:
                raise ValueError(f"Unsupported database type: {db_type}")

            for statement in [s.strip() for s in create_sql.split(';') if s.strip()]:
                await conn.execute(text(statement))

        logger.info("✅ Таблица withdrawal_requests успешно создана")
        return True

    except Exception as e:
        logger.error(f"Ошибка создания таблицы withdrawal_requests: {e}")
        return False


async def run_universal_migration():
    logger.info("=== НАЧАЛО УНИВЕРСАЛЬНОЙ МИГРАЦИИ ===")
    
    try:
        db_type = await get_database_type()
        logger.info(f"Тип базы данных: {db_type}")

        if db_type == 'postgresql':
            logger.info("=== СИНХРОНИЗАЦИЯ ПОСЛЕДОВАТЕЛЬНОСТЕЙ PostgreSQL ===")
            sequences_synced = await sync_postgres_sequences()
            if sequences_synced:
                logger.info("✅ Последовательности PostgreSQL синхронизированы")
            else:
                logger.warning("⚠️ Не удалось синхронизировать последовательности PostgreSQL")

        revoked_at_ready = await ensure_device_link_revoked_at_column()
        if not revoked_at_ready:
            logger.warning("⚠️ Проблемы с колонкой revoked_at у привязок устройств")

        logger.info("=== ВЕБ-АУТЕНТИФИКАЦИЯ ЛИЧНОГО КАБИНЕТА ===")
        web_auth_columns_ready = await ensure_user_web_auth_columns()
        if web_auth_columns_ready:
            logger.info("✅ Колонки веб-аутентификации готовы")
        else:
            logger.warning("⚠️ Проблемы с колонками веб-аутентификации")

        telegram_id_nullable_ready = await relax_users_telegram_id_nullable()
        if telegram_id_nullable_ready:
            logger.info("✅ users.telegram_id допускает NULL")
        else:
            logger.warning("⚠️ Проблемы со снятием NOT NULL с telegram_id")

        referral_migration_success = await add_referral_system_columns()
        if not referral_migration_success:
            logger.warning("⚠️ Проблемы с миграцией реферальной системы")

        commission_column_ready = await add_referral_commission_percent_column()
        if commission_column_ready:
            logger.info("✅ Колонка referral_commission_percent готова")
        else:
            logger.warning("⚠️ Проблемы с колонкой referral_commission_percent")

        qualified_columns_ready = await add_referral_qualified_columns()
        if qualified_columns_ready:
            logger.info("✅ Колонки качественных рефералов готовы")
        else:
            logger.warning("⚠️ Проблемы с колонками качественных рефералов")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ SYSTEM_SETTINGS ===")
        system_settings_ready = await create_system_settings_table()
        if system_settings_ready:
            logger.info("✅ Таблица system_settings готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей system_settings")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ WEB_API_TOKENS ===")
        web_api_tokens_ready = await create_web_api_tokens_table()
        if web_api_tokens_ready:
            logger.info("✅ Таблица web_api_tokens готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей web_api_tokens")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ MENU_LAYOUT_HISTORY ===")
        menu_layout_history_ready = await create_menu_layout_history_table()
        if menu_layout_history_ready:
            logger.info("✅ Таблица menu_layout_history готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей menu_layout_history")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ BUTTON_CLICK_LOGS ===")
        button_click_logs_ready = await create_button_click_logs_table()
        if button_click_logs_ready:
            logger.info("✅ Таблица button_click_logs готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей button_click_logs")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ PAYMENT_ROUTING_LOG ===")
        payment_routing_log_ready = await create_payment_routing_log_table()
        if payment_routing_log_ready:
            logger.info("✅ Таблица payment_routing_log готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей payment_routing_log")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ ДЛЯ ТРИАЛЬНЫХ СКВАДОВ ===")
        trial_column_ready = await add_server_trial_flag_column()
        if trial_column_ready:
            logger.info("✅ Колонка is_trial_eligible готова")
        else:
            logger.warning("⚠️ Проблемы с колонкой is_trial_eligible")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ PRIVACY_POLICIES ===")
        privacy_policies_ready = await create_privacy_policies_table()
        if privacy_policies_ready:
            logger.info("✅ Таблица privacy_policies готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей privacy_policies")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ PUBLIC_OFFERS ===")
        public_offers_ready = await create_public_offers_table()
        if public_offers_ready:
            logger.info("✅ Таблица public_offers готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей public_offers")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ FAQ_SETTINGS ===")
        faq_settings_ready = await create_faq_settings_table()
        if faq_settings_ready:
            logger.info("✅ Таблица faq_settings готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей faq_settings")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ FAQ_PAGES ===")
        faq_pages_ready = await create_faq_pages_table()
        if faq_pages_ready:
            logger.info("✅ Таблица faq_pages готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей faq_pages")

        logger.info("=== ПРОВЕРКА БАЗОВЫХ ТОКЕНОВ ВЕБ-API ===")
        default_token_ready = await ensure_default_web_api_token()
        if default_token_ready:
            logger.info("✅ Бутстрап токен веб-API готов")
        else:
            logger.warning("⚠️ Не удалось создать бутстрап токен веб-API")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ CRYPTOBOT ===")
        cryptobot_created = await create_cryptobot_payments_table()
        if cryptobot_created:
            logger.info("✅ Таблица CryptoBot payments готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей CryptoBot payments")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ HELEKET ===")
        heleket_created = await create_heleket_payments_table()
        if heleket_created:
            logger.info("✅ Таблица Heleket payments готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей Heleket payments")

        mulenpay_name = settings.get_mulenpay_display_name()
        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ %s ===", mulenpay_name)
        mulenpay_created = await create_mulenpay_payments_table()
        if mulenpay_created:
            logger.info("✅ Таблица %s payments готова", mulenpay_name)
        else:
            logger.warning("⚠️ Проблемы с таблицей %s payments", mulenpay_name)

        mulenpay_schema_ok = await ensure_mulenpay_payment_schema()
        if mulenpay_schema_ok:
            logger.info("✅ Схема %s payments актуальна", mulenpay_name)
        else:
            logger.warning("⚠️ Не удалось обновить схему %s payments", mulenpay_name)

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ PAL24 ===")
        pal24_created = await create_pal24_payments_table()
        if pal24_created:
            logger.info("✅ Таблица Pal24 payments готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей Pal24 payments")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ WATA ===")
        wata_created = await create_wata_payments_table()
        if wata_created:
            logger.info("✅ Таблица Wata payments готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей Wata payments")

        wata_schema_ok = await ensure_wata_payment_schema()
        if wata_schema_ok:
            logger.info("✅ Схема Wata payments актуальна")
        else:
            logger.warning("⚠️ Не удалось обновить схему Wata payments")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ DISCOUNT_OFFERS ===")
        discount_created = await create_discount_offers_table()
        if discount_created:
            logger.info("✅ Таблица discount_offers готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей discount_offers")

        discount_columns_ready = await ensure_discount_offer_columns()
        if discount_columns_ready:
            logger.info("✅ Колонки discount_offers в актуальном состоянии")
        else:
            logger.warning("⚠️ Не удалось обновить колонки discount_offers")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦ ДЛЯ РЕФЕРАЛЬНЫХ КОНКУРСОВ ===")
        contests_table_ready = await create_referral_contests_table()
        if contests_table_ready:
            logger.info("✅ Таблица referral_contests готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей referral_contests")

        contest_events_ready = await create_referral_contest_events_table()
        if contest_events_ready:
            logger.info("✅ Таблица referral_contest_events готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей referral_contest_events")

        contest_type_ready = await ensure_referral_contest_type_column()
        if contest_type_ready:
            logger.info("✅ Колонка contest_type для referral_contests готова")
        else:
            logger.warning("⚠️ Не удалось добавить contest_type в referral_contests")

        contest_summary_ready = await ensure_referral_contest_summary_columns()
        if contest_summary_ready:
            logger.info("✅ Колонки daily_summary_times/last_daily_summary_at готовы")
        else:
            logger.warning("⚠️ Не удалось обновить колонки сводок для referral_contests")

        contest_templates_ready = await create_contest_templates_table()
        if contest_templates_ready:
            logger.info("✅ Таблица contest_templates готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей contest_templates")

        contest_rounds_ready = await create_contest_rounds_table()
        if contest_rounds_ready:
            logger.info("✅ Таблица contest_rounds готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей contest_rounds")

        contest_attempts_ready = await create_contest_attempts_table()
        if contest_attempts_ready:
            logger.info("✅ Таблица contest_attempts готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей contest_attempts")

        user_discount_columns_ready = await ensure_user_promo_offer_discount_columns()
        if user_discount_columns_ready:
            logger.info("✅ Колонки пользовательских промо-скидок готовы")
        else:
            logger.warning("⚠️ Не удалось обновить пользовательские промо-скидки")

        cryptobot_metadata_ready = await ensure_cryptobot_payment_metadata_column()
        if cryptobot_metadata_ready:
            logger.info("✅ Колонка metadata_json в cryptobot_payments готова")
        else:
            logger.warning("⚠️ Не удалось добавить metadata_json в cryptobot_payments")

        effect_types_updated = await migrate_discount_offer_effect_types()
        if effect_types_updated:
            logger.info("✅ Типы эффектов промо-предложений обновлены")
        else:
            logger.warning("⚠️ Не удалось обновить типы эффектов промо-предложений")

        bonuses_reset = await reset_discount_offer_bonuses()
        if bonuses_reset:
            logger.info("✅ Бонусные начисления промо-предложений отключены")
        else:
            logger.warning("⚠️ Не удалось обнулить бонусы промо-предложений")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ PROMO_OFFER_TEMPLATES ===")
        promo_templates_created = await create_promo_offer_templates_table()
        if promo_templates_created:
            logger.info("✅ Таблица promo_offer_templates готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей promo_offer_templates")

        logger.info("=== ДОБАВЛЕНИЕ ПРИОРИТЕТА В ПРОМОГРУППЫ ===")
        priority_column_ready = await add_promo_group_priority_column()
        if priority_column_ready:
            logger.info("✅ Колонка priority в promo_groups готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением priority в promo_groups")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ USER_PROMO_GROUPS ===")
        user_promo_groups_ready = await create_user_promo_groups_table()
        if user_promo_groups_ready:
            logger.info("✅ Таблица user_promo_groups готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей user_promo_groups")

        logger.info("=== МИГРАЦИЯ ДАННЫХ В USER_PROMO_GROUPS ===")
        data_migrated = await migrate_existing_user_promo_groups_data()
        if data_migrated:
            logger.info("✅ Данные перенесены в user_promo_groups")
        else:
            logger.warning("⚠️ Проблемы с миграцией данных в user_promo_groups")

        logger.info("=== ДОБАВЛЕНИЕ PROMO_GROUP_ID В PROMOCODES ===")
        promocode_column_ready = await add_promocode_promo_group_column()
        if promocode_column_ready:
            logger.info("✅ Колонка promo_group_id в promocodes готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением promo_group_id в promocodes")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ MAIN_MENU_BUTTONS ===")
        main_menu_buttons_created = await create_main_menu_buttons_table()
        if main_menu_buttons_created:
            logger.info("✅ Таблица main_menu_buttons готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей main_menu_buttons")

        template_columns_ready = await ensure_promo_offer_template_active_duration_column()
        if template_columns_ready:
            logger.info("✅ Колонка active_discount_hours промо-предложений готова")
        else:
            logger.warning("⚠️ Не удалось обновить колонку active_discount_hours промо-предложений")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ PROMO_OFFER_LOGS ===")
        promo_logs_created = await create_promo_offer_logs_table()
        if promo_logs_created:
            logger.info("✅ Таблица promo_offer_logs готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей promo_offer_logs")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ SUBSCRIPTION_TEMPORARY_ACCESS ===")
        temp_access_created = await create_subscription_temporary_access_table()
        if temp_access_created:
            logger.info("✅ Таблица subscription_temporary_access готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей subscription_temporary_access")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ USER_MESSAGES ===")
        user_messages_created = await create_user_messages_table()
        if user_messages_created:
            logger.info("✅ Таблица user_messages готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей user_messages")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ PINNED_MESSAGES ===")
        pinned_messages_created = await create_pinned_messages_table()
        if pinned_messages_created:
            logger.info("✅ Таблица pinned_messages готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей pinned_messages")

        logger.info("=== СОЗДАНИЕ/ОБНОВЛЕНИЕ ТАБЛИЦЫ WELCOME_TEXTS ===")
        welcome_texts_created = await create_welcome_texts_table()
        if welcome_texts_created:
            logger.info("✅ Таблица welcome_texts готова с полем is_enabled")
        else:
            logger.warning("⚠️ Проблемы с таблицей welcome_texts")

        logger.info("=== ОБНОВЛЕНИЕ СХЕМЫ PINNED_MESSAGES ===")
        pinned_media_ready = await ensure_pinned_message_media_columns()
        if pinned_media_ready:
            logger.info("✅ Медиа поля для pinned_messages готовы")
        else:
            logger.warning("⚠️ Проблемы с медиа полями pinned_messages")

        logger.info("=== ДОБАВЛЕНИЕ СЛЕДА ОТПРАВКИ ЗАКРЕПА ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ===")
        last_pinned_ready = await ensure_user_last_pinned_column()
        if last_pinned_ready:
            logger.info("✅ Колонка last_pinned_message_id добавлена")
        else:
            logger.warning("⚠️ Не удалось обновить колонку last_pinned_message_id")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ BOT_ID ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ===")
        bot_id_ready = await ensure_user_bot_id_column()
        if bot_id_ready:
            logger.info("✅ Колонка bot_id добавлена")
        else:
            logger.warning("⚠️ Не удалось добавить колонку bot_id")

        logger.info("=== ДОБАВЛЕНИЕ МЕДИА ПОЛЕЙ В BROADCAST_HISTORY ===")
        media_fields_added = await add_media_fields_to_broadcast_history()
        if media_fields_added:
            logger.info("✅ Медиа поля в broadcast_history готовы")
        else:
            logger.warning("⚠️ Проблемы с добавлением медиа полей")

        logger.info("=== ДОБАВЛЕНИЕ ПОЛЕЙ БЛОКИРОВКИ В TICKETS ===")
        tickets_block_cols_added = await add_ticket_reply_block_columns()
        if tickets_block_cols_added:
            logger.info("✅ Поля блокировок в tickets готовы")
        else:
            logger.warning("⚠️ Проблемы с добавлением полей блокировок в tickets")

        logger.info("=== ДОБАВЛЕНИЕ ПОЛЕЙ SLA В TICKETS ===")
        sla_cols_added = await add_ticket_sla_columns()
        if sla_cols_added:
            logger.info("✅ Поля SLA в tickets готовы")
        else:
            logger.warning("⚠️ Проблемы с добавлением полей SLA в tickets")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ CRYPTO LINK ДЛЯ ПОДПИСОК ===")
        crypto_link_added = await add_subscription_crypto_link_column()
        if crypto_link_added:
            logger.info("✅ Колонка subscription_crypto_link готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением колонки subscription_crypto_link")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ АУДИТА ПОДДЕРЖКИ ===")
        try:
            async with engine.begin() as conn:
                db_type = await get_database_type()
                if not await check_table_exists('support_audit_logs'):
                    if db_type == 'sqlite':
                        create_sql = """
                        CREATE TABLE support_audit_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            actor_user_id INTEGER NULL,
                            actor_telegram_id BIGINT NOT NULL,
                            is_moderator BOOLEAN NOT NULL DEFAULT 0,
                            action VARCHAR(50) NOT NULL,
                            ticket_id INTEGER NULL,
                            target_user_id INTEGER NULL,
                            details JSON NULL,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (actor_user_id) REFERENCES users(id),
                            FOREIGN KEY (ticket_id) REFERENCES tickets(id),
                            FOREIGN KEY (target_user_id) REFERENCES users(id)
                        );
                        CREATE INDEX idx_support_audit_logs_ticket ON support_audit_logs(ticket_id);
                        CREATE INDEX idx_support_audit_logs_actor ON support_audit_logs(actor_telegram_id);
                        CREATE INDEX idx_support_audit_logs_action ON support_audit_logs(action);
                        """
                    elif db_type == 'postgresql':
                        create_sql = """
                        CREATE TABLE support_audit_logs (
                            id SERIAL PRIMARY KEY,
                            actor_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                            actor_telegram_id BIGINT NOT NULL,
                            is_moderator BOOLEAN NOT NULL DEFAULT FALSE,
                            action VARCHAR(50) NOT NULL,
                            ticket_id INTEGER NULL REFERENCES tickets(id) ON DELETE SET NULL,
                            target_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                            details JSON NULL,
                            created_at TIMESTAMP DEFAULT NOW()
                        );
                        CREATE INDEX idx_support_audit_logs_ticket ON support_audit_logs(ticket_id);
                        CREATE INDEX idx_support_audit_logs_actor ON support_audit_logs(actor_telegram_id);
                        CREATE INDEX idx_support_audit_logs_action ON support_audit_logs(action);
                        """
                    else:
                        create_sql = """
                        CREATE TABLE support_audit_logs (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            actor_user_id INT NULL,
                            actor_telegram_id BIGINT NOT NULL,
                            is_moderator BOOLEAN NOT NULL DEFAULT 0,
                            action VARCHAR(50) NOT NULL,
                            ticket_id INT NULL,
                            target_user_id INT NULL,
                            details JSON NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        CREATE INDEX idx_support_audit_logs_ticket ON support_audit_logs(ticket_id);
                        CREATE INDEX idx_support_audit_logs_actor ON support_audit_logs(actor_telegram_id);
                        CREATE INDEX idx_support_audit_logs_action ON support_audit_logs(action);
                        """
                    await conn.execute(text(create_sql))
                    logger.info("✅ Таблица support_audit_logs создана")
                else:
                    logger.info("ℹ️ Таблица support_audit_logs уже существует")
        except Exception as e:
            logger.warning(f"⚠️ Проблемы с созданием таблицы support_audit_logs: {e}")

        logger.info("=== НАСТРОЙКА ПРОМО ГРУПП ===")
        promo_groups_ready = await ensure_promo_groups_setup()
        if promo_groups_ready:
            logger.info("✅ Промо группы готовы")
        else:
            logger.warning("⚠️ Проблемы с настройкой промо групп")

        server_promo_groups_ready = await ensure_server_promo_groups_setup()
        if server_promo_groups_ready:
            logger.info("✅ Доступ серверов по промогруппам настроен")
        else:
            logger.warning("⚠️ Проблемы с настройкой доступа серверов к промогруппам")

        logger.info("=== ОБНОВЛЕНИЕ ВНЕШНИХ КЛЮЧЕЙ ===")
        fk_updated = await fix_foreign_keys_for_user_deletion()
        if fk_updated:
            logger.info("✅ Внешние ключи обновлены")
        else:
            logger.warning("⚠️ Проблемы с обновлением внешних ключей")
        
        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ КОНВЕРСИЙ ПОДПИСОК ===")
        conversions_created = await create_subscription_conversions_table()
        if conversions_created:
            logger.info("✅ Таблица subscription_conversions готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей subscription_conversions")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ SUBSCRIPTION_EVENTS ===")
        events_created = await create_subscription_events_table()
        if events_created:
            logger.info("✅ Таблица subscription_events готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей subscription_events")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ FEEDBACKS ===")
        feedbacks_created = await create_feedbacks_table()
        if feedbacks_created:
            logger.info("✅ Таблица feedbacks готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей feedbacks")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ INTERACTIVE_NOTIFICATION_LOGS ===")
        interactive_logs_created = await create_interactive_notification_logs_table()
        if interactive_logs_created:
            logger.info("✅ Таблица interactive_notification_logs готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей interactive_notification_logs")

        if not await ensure_interactive_notification_logs_campaign_index():
            logger.warning("⚠️ Проблемы с составным индексом interactive_notification_logs")

        if not await create_platega_unpaid_created_index():
            logger.warning("⚠️ Проблемы с индексом неоплаченных Platega счетов")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ PLATEGA_SUBSCRIPTIONS ===")
        platega_subscriptions_ready = await create_platega_subscriptions_table()
        if platega_subscriptions_ready:
            logger.info("✅ Таблица platega_subscriptions готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей platega_subscriptions")

        platega_active_slot_ready = await add_platega_subscription_active_user_id()
        if platega_active_slot_ready:
            logger.info("✅ Активный слот регулярной подписки Platega готов")
        else:
            logger.warning("⚠️ Проблемы с активным слотом регулярной подписки Platega")

        logger.info("=== СВЯЗЬ PLATEGA_PAYMENTS С РЕГУЛЯРНЫМИ ПОДПИСКАМИ ===")
        platega_payment_subscription_ready = await add_platega_payment_subscription_id()
        if platega_payment_subscription_ready:
            logger.info("✅ Связь platega_payments.subscription_id готова")
        else:
            logger.warning("⚠️ Проблемы со связью platega_payments.subscription_id")

        if not await ensure_platega_subscription_indexes():
            logger.warning("⚠️ Проблемы с индексами регулярных подписок Platega")

        if not await create_subscription_short_uuid_index():
            logger.warning("⚠️ Проблемы с индексом subscriptions.remnawave_short_uuid")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ ANDROID_RATE_REQUEST_CLICKS ===")
        android_rate_clicks_created = await create_android_rate_request_clicks_table()
        if android_rate_clicks_created:
            logger.info("✅ Таблица android_rate_request_clicks готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей android_rate_request_clicks")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ HAS_CONNECTED_TO_VPN В USERS ===")
        vpn_column_ready = await add_user_has_connected_to_vpn_column()
        if vpn_column_ready:
            logger.info("✅ Колонка has_connected_to_vpn в users готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением has_connected_to_vpn в users")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ HAS_USED_MOBILE_APP В USERS ===")
        mobile_app_column_ready = await add_user_has_used_mobile_app_column()
        if mobile_app_column_ready:
            logger.info("✅ Колонка has_used_mobile_app в users готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением has_used_mobile_app в users")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ ACQUISITION_SOURCE В USERS ===")
        acquisition_source_ready = await add_user_acquisition_source_column()
        if acquisition_source_ready:
            logger.info("✅ Колонка acquisition_source в users готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением acquisition_source в users")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ TG_USER_ID В USERS ===")
        tg_user_id_ready = await add_user_tg_user_id_column()
        if tg_user_id_ready:
            logger.info("✅ Колонка tg_user_id в users готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением tg_user_id в users")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ LAST_APP_NAME В USERS ===")
        last_app_name_ready = await add_user_last_app_name_column()
        if last_app_name_ready:
            logger.info("✅ Колонка last_app_name в users готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением last_app_name в users")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ DEVICE_BINDING_CODES ===")
        device_binding_codes_ready = await create_device_binding_codes_table()
        if device_binding_codes_ready:
            logger.info("✅ Таблица device_binding_codes готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей device_binding_codes")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ SHARE_TOKENS ===")
        share_tokens_ready = await create_share_tokens_table()
        if share_tokens_ready:
            logger.info("✅ Таблица share_tokens готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей share_tokens")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ USER_DAILY_TRAFFIC_USAGE ===")
        daily_traffic_ready = await create_user_daily_traffic_usage_table()
        if daily_traffic_ready:
            logger.info("✅ Таблица user_daily_traffic_usage готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей user_daily_traffic_usage")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ DAILY_SUBSCRIPTION_METRICS ===")
        daily_subscription_metrics_ready = await create_daily_subscription_metrics_table()
        if daily_subscription_metrics_ready:
            logger.info("✅ Таблица daily_subscription_metrics готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей daily_subscription_metrics")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ USER_DAILY_METRICS ===")
        user_daily_metrics_ready = await create_user_daily_metrics_table()
        if user_daily_metrics_ready:
            logger.info("✅ Таблица user_daily_metrics готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей user_daily_metrics")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ TRIAL_EXPIRY_DAILY_METRICS ===")
        trial_expiry_daily_metrics_ready = await create_trial_expiry_daily_metrics_table()
        if trial_expiry_daily_metrics_ready:
            logger.info("✅ Таблица trial_expiry_daily_metrics готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей trial_expiry_daily_metrics")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ is_partner В SUBSCRIPTIONS ===")
        is_partner_ready = await add_subscription_is_partner_column()
        if is_partner_ready:
            logger.info("✅ Колонка subscriptions.is_partner готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением колонки is_partner")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ used_trial_failed В SUBSCRIPTIONS ===")
        used_trial_failed_ready = await add_subscription_used_trial_failed_column()
        if used_trial_failed_ready:
            logger.info("✅ Колонка subscriptions.used_trial_failed готова")
        else:
            logger.warning("⚠️ Проблемы с добавлением колонки used_trial_failed")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ PARTNER_LINK_REDEMPTIONS ===")
        partner_redemptions_ready = await create_partner_link_redemptions_table()
        if partner_redemptions_ready:
            logger.info("✅ Таблица partner_link_redemptions готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей partner_link_redemptions")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ SUBSCRIPTION_PLANS ===")
        plans_table_ready = await create_subscription_plans_table()
        if plans_table_ready:
            logger.info("✅ Таблица subscription_plans готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей subscription_plans")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ SUBSCRIPTION_PLAN_PRICES ===")
        plan_prices_ready = await create_subscription_plan_prices_table()
        if plan_prices_ready:
            logger.info("✅ Таблица subscription_plan_prices готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей subscription_plan_prices")

        if plan_prices_ready:
            logger.info("=== РАЗДЕЛЕНИЕ ЦЕН НА КОГОРТЫ (audience) ===")
            if await add_audience_to_plan_prices():
                logger.info("✅ Колонка audience готова")
            else:
                logger.warning("⚠️ Проблемы с колонкой audience")

        logger.info("=== ДОБАВЛЕНИЕ plan_id/plan_period_days В SUBSCRIPTIONS ===")
        plan_columns_ready = await add_plan_columns_to_subscriptions()
        if plan_columns_ready:
            logger.info("✅ Колонки subscriptions.plan_id и plan_period_days готовы")
        else:
            logger.warning("⚠️ Проблемы с колонками plan_id/plan_period_days")

        if plans_table_ready and plan_prices_ready:
            logger.info("=== СИДИРОВАНИЕ ТАРИФНЫХ ПЛАНОВ ===")
            plans_seeded = await seed_subscription_plans()
            if plans_seeded:
                logger.info("✅ Тарифные планы засеяны")
            else:
                logger.warning("⚠️ Проблемы с сидированием тарифных планов")

            logger.info("=== ОБНОВЛЕНИЕ ЛИМИТА УСТРОЙСТВ ТАРИФА PLUS ===")
            if await update_plus_plan_device_limit_to_three():
                logger.info("✅ Тариф Plus: лимит устройств актуален (3)")
            else:
                logger.warning("⚠️ Проблемы с обновлением лимита устройств Plus")

            logger.info("=== ОБНОВЛЕНИЕ ФОРМУЛИРОВКИ ОПИСАНИЙ ТАРИФОВ ===")
            if await update_plan_descriptions_vpn_wording():
                logger.info("✅ Формулировка описаний тарифов актуальна")
            else:
                logger.warning("⚠️ Проблемы с обновлением описаний тарифов")

            if settings.get_legacy_tariff_migration_mode() != "off":
                logger.info("=== ПЕРЕВОД ЛЕГАСИ-ПОДПИСОК НА ТАРИФЫ ===")
                if await migrate_legacy_subscriptions_to_plans():
                    logger.info("✅ Перевод легаси-подписок на тарифы завершён")
                else:
                    logger.warning("⚠️ Проблемы с переводом легаси-подписок на тарифы")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ ADVERTISING_CAMPAIGNS ===")
        ad_campaigns_ready = await create_advertising_campaigns_table()
        if ad_campaigns_ready:
            logger.info("✅ Таблицы advertising_campaigns готовы")
        else:
            logger.warning("⚠️ Проблемы с таблицами advertising_campaigns")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНОК AD ATTRIBUTION В USERS ===")
        ad_attribution_ready = await add_ad_attribution_columns()
        if ad_attribution_ready:
            logger.info("✅ Колонки ad attribution в users готовы")
        else:
            logger.warning("⚠️ Проблемы с колонками ad attribution в users")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ LANGUAGE_CODE В USERS ===")
        language_code_ready = await add_user_language_code_column()
        if language_code_ready:
            logger.info("✅ Колонка language_code в users готова")
        else:
            logger.warning("⚠️ Проблемы с колонкой language_code в users")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНКИ TARIFF_PRICING_COHORT_OVERRIDE В USERS ===")
        tariff_cohort_override_ready = await add_user_tariff_pricing_cohort_override_column()
        if tariff_cohort_override_ready:
            logger.info("✅ Колонка tariff_pricing_cohort_override в users готова")
        else:
            logger.warning("⚠️ Проблемы с колонкой tariff_pricing_cohort_override в users")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ AD_CAMPAIGN_VISITS ===")
        ad_visits_ready = await create_ad_campaign_visits_table()
        if ad_visits_ready:
            logger.info("✅ Таблица ad_campaign_visits готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей ad_campaign_visits")

        logger.info("=== ДОБАВЛЕНИЕ КОЛОНОК ЛУЧЕЙ В USERS ===")
        rays_columns_ready = await add_rays_columns()
        if rays_columns_ready:
            logger.info("✅ Колонки лучей в users готовы")
        else:
            logger.warning("⚠️ Проблемы с колонками лучей в users")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ RAY_TRANSACTIONS ===")
        ray_transactions_ready = await create_ray_transactions_table()
        if ray_transactions_ready:
            logger.info("✅ Таблица ray_transactions готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей ray_transactions")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ RAY_PRIZE_CLAIMS ===")
        ray_prize_claims_ready = await create_ray_prize_claims_table()
        if ray_prize_claims_ready:
            logger.info("✅ Таблица ray_prize_claims готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей ray_prize_claims")

        logger.info("=== КОЛОНКА CONTACT В RAY_PRIZE_CLAIMS ===")
        claim_contact_ready = await add_ray_prize_claim_contact_column()
        if claim_contact_ready:
            logger.info("✅ Колонка ray_prize_claims.contact готова")
        else:
            logger.warning("⚠️ Проблемы с колонкой ray_prize_claims.contact")

        logger.info("=== СОЗДАНИЕ ТАБЛИЦЫ WITHDRAWAL_REQUESTS ===")
        withdrawal_requests_ready = await create_withdrawal_requests_table()
        if withdrawal_requests_ready:
            logger.info("✅ Таблица withdrawal_requests готова")
        else:
            logger.warning("⚠️ Проблемы с таблицей withdrawal_requests")

        async with engine.begin() as conn:
            total_subs = await conn.execute(text("SELECT COUNT(*) FROM subscriptions"))
            unique_users = await conn.execute(text("SELECT COUNT(DISTINCT user_id) FROM subscriptions"))
            
            total_count = total_subs.fetchone()[0]
            unique_count = unique_users.fetchone()[0]
            
            logger.info(f"Всего подписок: {total_count}")
            logger.info(f"Уникальных пользователей: {unique_count}")
            
            if total_count == unique_count:
                logger.info("База данных уже в корректном состоянии")
                logger.info("=== МИГРАЦИЯ ЗАВЕРШЕНА УСПЕШНО ===")
                return True
        
        deleted_count = await fix_subscription_duplicates_universal()
        
        async with engine.begin() as conn:
            final_check = await conn.execute(text("""
                SELECT user_id, COUNT(*) as count 
                FROM subscriptions 
                GROUP BY user_id 
                HAVING COUNT(*) > 1
            """))
            
            remaining_duplicates = final_check.fetchall()
            
            if remaining_duplicates:
                logger.warning(f"Остались дубликаты у {len(remaining_duplicates)} пользователей")
                return False
            else:
                logger.info("=== МИГРАЦИЯ ЗАВЕРШЕНА УСПЕШНО ===")
                logger.info("✅ Реферальная система обновлена")
                logger.info("✅ CryptoBot таблица готова")
                logger.info("✅ Heleket таблица готова")
                logger.info("✅ Таблица конверсий подписок создана")
                logger.info("✅ Таблица событий подписок создана")
                logger.info("✅ Таблица welcome_texts с полем is_enabled готова")
                logger.info("✅ Медиа поля в broadcast_history добавлены")
                logger.info("✅ Дубликаты подписок исправлены")
                return True
                
    except Exception as e:
        logger.error(f"=== ОШИБКА ВЫПОЛНЕНИЯ МИГРАЦИИ: {e} ===")
        return False

async def check_migration_status():
    logger.info("=== ПРОВЕРКА СТАТУСА МИГРАЦИЙ ===")
    
    try:
        status = {
            "has_made_first_topup_column": False,
            "cryptobot_table": False,
            "heleket_table": False,
            "user_messages_table": False,
            "pinned_messages_table": False,
            "welcome_texts_table": False,
            "welcome_texts_is_enabled_column": False,
            "pinned_messages_media_columns": False,
            "pinned_messages_position_column": False,
            "pinned_messages_start_mode_column": False,
            "users_last_pinned_column": False,
            "broadcast_history_media_fields": False,
            "subscription_duplicates": False,
            "subscription_conversions_table": False,
            "subscription_events_table": False,
            "interactive_notification_logs_table": False,
            "platega_subscriptions_table": False,
            "platega_subscriptions_active_user_id_column": False,
            "platega_payments_subscription_id_column": False,
            "platega_subscriptions_provider_id_index": False,
            "platega_subscriptions_user_status_index": False,
            "platega_subscriptions_active_user_id_index": False,
            "platega_payments_subscription_id_index": False,
            "promo_groups_table": False,
            "server_promo_groups_table": False,
            "server_squads_trial_column": False,
            "privacy_policies_table": False,
            "public_offers_table": False,
            "users_promo_group_column": False,
            "promo_groups_period_discounts_column": False,
            "promo_groups_auto_assign_column": False,
            "promo_groups_addon_discount_column": False,
            "users_auto_promo_group_assigned_column": False,
            "users_auto_promo_group_threshold_column": False,
            "users_promo_offer_discount_percent_column": False,
            "users_promo_offer_discount_source_column": False,
            "users_promo_offer_discount_expires_column": False,
            "users_tariff_pricing_cohort_override_column": False,
            "users_referral_commission_percent_column": False,
            "subscription_crypto_link_column": False,
            "discount_offers_table": False,
            "discount_offers_effect_column": False,
            "discount_offers_extra_column": False,
            "referral_contests_table": False,
            "referral_contest_events_table": False,
            "referral_contest_type_column": False,
            "referral_contest_summary_times_column": False,
            "referral_contest_last_summary_at_column": False,
            "contest_templates_table": False,
            "contest_rounds_table": False,
            "contest_attempts_table": False,
            "promo_offer_templates_table": False,
            "promo_offer_templates_active_discount_column": False,
            "promo_offer_logs_table": False,
            "subscription_temporary_access_table": False,
            "user_daily_traffic_usage_table": False,
            "daily_subscription_metrics_table": False,
            "user_daily_metrics_table": False,
            "trial_expiry_daily_metrics_table": False,
            "advertising_campaigns_table": False,
            "advertising_campaign_registrations_table": False,
            "users_raw_start_payload_column": False,
            "users_attribution_campaign_id_column": False,
            "ad_campaign_visits_table": False,
        }
        
        status["has_made_first_topup_column"] = await check_column_exists('users', 'has_made_first_topup')
        
        status["cryptobot_table"] = await check_table_exists('cryptobot_payments')
        status["heleket_table"] = await check_table_exists('heleket_payments')
        status["user_messages_table"] = await check_table_exists('user_messages')
        status["pinned_messages_table"] = await check_table_exists('pinned_messages')
        status["welcome_texts_table"] = await check_table_exists('welcome_texts')
        status["privacy_policies_table"] = await check_table_exists('privacy_policies')
        status["public_offers_table"] = await check_table_exists('public_offers')
        status["subscription_conversions_table"] = await check_table_exists('subscription_conversions')
        status["subscription_events_table"] = await check_table_exists('subscription_events')
        status["interactive_notification_logs_table"] = await check_table_exists('interactive_notification_logs')
        status["platega_subscriptions_table"] = await check_table_exists('platega_subscriptions')
        status["platega_subscriptions_active_user_id_column"] = await check_column_exists(
            'platega_subscriptions', 'active_user_id'
        )
        status["platega_payments_subscription_id_column"] = await check_column_exists(
            'platega_payments', 'subscription_id'
        )
        status["platega_subscriptions_provider_id_index"] = await check_index_exists(
            'platega_subscriptions', 'ix_platega_subscriptions_platega_subscription_id'
        )
        status["platega_subscriptions_user_status_index"] = await check_index_exists(
            'platega_subscriptions', 'ix_platega_subscriptions_user_status'
        )
        status["platega_subscriptions_active_user_id_index"] = await check_index_exists(
            'platega_subscriptions', 'ix_platega_subscriptions_active_user_id'
        )
        status["platega_payments_subscription_id_index"] = await check_index_exists(
            'platega_payments', 'ix_platega_payments_subscription_id'
        )
        status["promo_groups_table"] = await check_table_exists('promo_groups')
        status["server_promo_groups_table"] = await check_table_exists('server_squad_promo_groups')
        status["server_squads_trial_column"] = await check_column_exists('server_squads', 'is_trial_eligible')

        status["discount_offers_table"] = await check_table_exists('discount_offers')
        status["discount_offers_effect_column"] = await check_column_exists('discount_offers', 'effect_type')
        status["discount_offers_extra_column"] = await check_column_exists('discount_offers', 'extra_data')
        status["referral_contests_table"] = await check_table_exists('referral_contests')
        status["referral_contest_events_table"] = await check_table_exists('referral_contest_events')
        status["referral_contest_type_column"] = await check_column_exists('referral_contests', 'contest_type')
        status["referral_contest_summary_times_column"] = await check_column_exists('referral_contests', 'daily_summary_times')
        status["referral_contest_last_summary_at_column"] = await check_column_exists('referral_contests', 'last_daily_summary_at')
        status["contest_templates_table"] = await check_table_exists('contest_templates')
        status["contest_rounds_table"] = await check_table_exists('contest_rounds')
        status["contest_attempts_table"] = await check_table_exists('contest_attempts')
        status["promo_offer_templates_table"] = await check_table_exists('promo_offer_templates')
        status["promo_offer_templates_active_discount_column"] = await check_column_exists('promo_offer_templates', 'active_discount_hours')
        status["promo_offer_logs_table"] = await check_table_exists('promo_offer_logs')
        status["subscription_temporary_access_table"] = await check_table_exists('subscription_temporary_access')
        status["user_daily_traffic_usage_table"] = await check_table_exists('user_daily_traffic_usage')
        status["daily_subscription_metrics_table"] = await check_table_exists('daily_subscription_metrics')
        status["user_daily_metrics_table"] = await check_table_exists('user_daily_metrics')
        status["trial_expiry_daily_metrics_table"] = await check_table_exists('trial_expiry_daily_metrics')
        status["advertising_campaigns_table"] = await check_table_exists('advertising_campaigns')
        status["advertising_campaign_registrations_table"] = await check_table_exists('advertising_campaign_registrations')
        status["users_raw_start_payload_column"] = await check_column_exists('users', 'raw_start_payload')
        status["users_attribution_campaign_id_column"] = await check_column_exists('users', 'attribution_campaign_id')
        status["ad_campaign_visits_table"] = await check_table_exists('ad_campaign_visits')

        status["welcome_texts_is_enabled_column"] = await check_column_exists('welcome_texts', 'is_enabled')
        status["users_promo_group_column"] = await check_column_exists('users', 'promo_group_id')
        status["promo_groups_period_discounts_column"] = await check_column_exists('promo_groups', 'period_discounts')
        status["promo_groups_auto_assign_column"] = await check_column_exists('promo_groups', 'auto_assign_total_spent_kopeks')
        status["promo_groups_addon_discount_column"] = await check_column_exists('promo_groups', 'apply_discounts_to_addons')
        status["users_auto_promo_group_assigned_column"] = await check_column_exists('users', 'auto_promo_group_assigned')
        status["users_auto_promo_group_threshold_column"] = await check_column_exists('users', 'auto_promo_group_threshold_kopeks')
        status["users_promo_offer_discount_percent_column"] = await check_column_exists('users', 'promo_offer_discount_percent')
        status["users_promo_offer_discount_source_column"] = await check_column_exists('users', 'promo_offer_discount_source')
        status["users_promo_offer_discount_expires_column"] = await check_column_exists('users', 'promo_offer_discount_expires_at')
        status["users_tariff_pricing_cohort_override_column"] = await check_column_exists('users', 'tariff_pricing_cohort_override')
        status["users_referral_commission_percent_column"] = await check_column_exists('users', 'referral_commission_percent')
        status["subscription_crypto_link_column"] = await check_column_exists('subscriptions', 'subscription_crypto_link')
        
        media_fields_exist = (
            await check_column_exists('broadcast_history', 'has_media') and
            await check_column_exists('broadcast_history', 'media_type') and
            await check_column_exists('broadcast_history', 'media_file_id') and
            await check_column_exists('broadcast_history', 'media_caption')
        )
        status["broadcast_history_media_fields"] = media_fields_exist

        pinned_media_columns_exist = (
            status["pinned_messages_table"]
            and await check_column_exists('pinned_messages', 'media_type')
            and await check_column_exists('pinned_messages', 'media_file_id')
        )
        status["pinned_messages_media_columns"] = pinned_media_columns_exist

        status["pinned_messages_position_column"] = (
            status["pinned_messages_table"]
            and await check_column_exists('pinned_messages', 'send_before_menu')
        )

        status["pinned_messages_start_mode_column"] = (
            status["pinned_messages_table"]
            and await check_column_exists('pinned_messages', 'send_on_every_start')
        )

        status["users_last_pinned_column"] = await check_column_exists('users', 'last_pinned_message_id')
        status["users_bot_id_column"] = await check_column_exists('users', 'bot_id')
        
        async with engine.begin() as conn:
            duplicates_check = await conn.execute(text("""
                SELECT COUNT(*) FROM (
                    SELECT user_id, COUNT(*) as count 
                    FROM subscriptions 
                    GROUP BY user_id 
                    HAVING COUNT(*) > 1
                ) as dups
            """))
            duplicates_count = duplicates_check.fetchone()[0]
            status["subscription_duplicates"] = (duplicates_count == 0)
        
        check_names = {
            "has_made_first_topup_column": "Колонка реферальной системы",
            "cryptobot_table": "Таблица CryptoBot payments",
            "heleket_table": "Таблица Heleket payments",
            "user_messages_table": "Таблица пользовательских сообщений",
            "pinned_messages_table": "Таблица закреплённых сообщений",
            "welcome_texts_table": "Таблица приветственных текстов",
            "privacy_policies_table": "Таблица политик конфиденциальности",
            "public_offers_table": "Таблица публичных оферт",
            "welcome_texts_is_enabled_column": "Поле is_enabled в welcome_texts",
            "pinned_messages_media_columns": "Медиа поля в pinned_messages",
            "pinned_messages_position_column": "Позиция закрепа (до/после меню)",
            "pinned_messages_start_mode_column": "Режим отправки закрепа при /start",
            "users_last_pinned_column": "Колонка last_pinned_message_id у пользователей",
            "users_bot_id_column": "Колонка bot_id у пользователей",
            "broadcast_history_media_fields": "Медиа поля в broadcast_history",
            "subscription_conversions_table": "Таблица конверсий подписок",
            "subscription_events_table": "Таблица событий подписок",
            "platega_subscriptions_table": "Таблица регулярных подписок Platega",
            "platega_subscriptions_active_user_id_column": "Активный слот регулярной подписки Platega",
            "platega_payments_subscription_id_column": "Связь платежей Platega с регулярными подписками",
            "platega_subscriptions_provider_id_index": "Уникальный индекс ID подписки Platega",
            "platega_subscriptions_user_status_index": "Индекс активных подписок Platega пользователя",
            "platega_subscriptions_active_user_id_index": "Уникальный активный слот подписки Platega",
            "platega_payments_subscription_id_index": "Индекс списаний регулярных подписок Platega",
            "subscription_duplicates": "Отсутствие дубликатов подписок",
            "promo_groups_table": "Таблица промо-групп",
            "server_promo_groups_table": "Связи серверов и промогрупп",
            "server_squads_trial_column": "Колонка триального назначения у серверов",
            "users_promo_group_column": "Колонка promo_group_id у пользователей",
            "promo_groups_period_discounts_column": "Колонка period_discounts у промо-групп",
            "promo_groups_auto_assign_column": "Колонка auto_assign_total_spent_kopeks у промо-групп",
            "promo_groups_addon_discount_column": "Колонка apply_discounts_to_addons у промо-групп",
            "users_auto_promo_group_assigned_column": "Флаг автоназначения промогруппы у пользователей",
            "users_auto_promo_group_threshold_column": "Порог последней авто-промогруппы у пользователей",
            "users_promo_offer_discount_percent_column": "Колонка процента промо-скидки у пользователей",
            "users_promo_offer_discount_source_column": "Колонка источника промо-скидки у пользователей",
            "users_promo_offer_discount_expires_column": "Колонка срока действия промо-скидки у пользователей",
            "users_tariff_pricing_cohort_override_column": "Колонка переопределения тарифной когорты у пользователей",
            "users_referral_commission_percent_column": "Колонка процента реферальной комиссии у пользователей",
            "subscription_crypto_link_column": "Колонка subscription_crypto_link в subscriptions",
            "discount_offers_table": "Таблица discount_offers",
            "discount_offers_effect_column": "Колонка effect_type в discount_offers",
            "discount_offers_extra_column": "Колонка extra_data в discount_offers",
            "referral_contests_table": "Таблица referral_contests",
            "referral_contest_events_table": "Таблица referral_contest_events",
            "referral_contest_type_column": "Колонка contest_type в referral_contests",
            "referral_contest_summary_times_column": "Колонка daily_summary_times в referral_contests",
            "referral_contest_last_summary_at_column": "Колонка last_daily_summary_at в referral_contests",
            "contest_templates_table": "Таблица contest_templates",
            "contest_rounds_table": "Таблица contest_rounds",
            "contest_attempts_table": "Таблица contest_attempts",
            "promo_offer_templates_table": "Таблица promo_offer_templates",
            "promo_offer_templates_active_discount_column": "Колонка active_discount_hours в promo_offer_templates",
            "promo_offer_logs_table": "Таблица promo_offer_logs",
            "subscription_temporary_access_table": "Таблица subscription_temporary_access",
            "user_daily_traffic_usage_table": "Таблица дневного трафика пользователей",
            "daily_subscription_metrics_table": "Таблица дневных snapshot-метрик подписок",
            "user_daily_metrics_table": "Таблица дневных snapshot-метрик пользователей",
            "trial_expiry_daily_metrics_table": "Таблица дневной конверсии истёкших триалов",
            "advertising_campaigns_table": "Таблица advertising_campaigns",
            "advertising_campaign_registrations_table": "Таблица advertising_campaign_registrations",
            "users_raw_start_payload_column": "Колонка raw_start_payload у пользователей",
            "users_attribution_campaign_id_column": "Колонка attribution_campaign_id у пользователей",
            "ad_campaign_visits_table": "Таблица ad_campaign_visits",
        }
        
        for check_key, check_status in status.items():
            check_name = check_names.get(check_key, check_key)
            icon = "✅" if check_status else "❌"
            logger.info(f"{icon} {check_name}: {'OK' if check_status else 'ТРЕБУЕТ ВНИМАНИЯ'}")
        
        all_good = all(status.values())
        if all_good:
            logger.info("🎉 Все миграции выполнены успешно!")
            
            try:
                async with engine.begin() as conn:
                    conversions_count = await conn.execute(text("SELECT COUNT(*) FROM subscription_conversions"))
                    users_count = await conn.execute(text("SELECT COUNT(*) FROM users"))
                    welcome_texts_count = await conn.execute(text("SELECT COUNT(*) FROM welcome_texts"))
                    broadcasts_count = await conn.execute(text("SELECT COUNT(*) FROM broadcast_history"))
                    
                    conv_count = conversions_count.fetchone()[0]
                    usr_count = users_count.fetchone()[0]
                    welcome_count = welcome_texts_count.fetchone()[0]
                    broadcast_count = broadcasts_count.fetchone()[0]
                    
                    logger.info(f"📊 Статистика: {usr_count} пользователей, {conv_count} конверсий, {welcome_count} приветственных текстов, {broadcast_count} рассылок")
            except Exception as stats_error:
                logger.debug(f"Не удалось получить дополнительную статистику: {stats_error}")
                
        else:
            logger.warning("⚠️ Некоторые миграции требуют внимания")
            missing_migrations = [check_names[k] for k, v in status.items() if not v]
            logger.warning(f"Требуют выполнения: {', '.join(missing_migrations)}")
        
        return status
        
    except Exception as e:
        logger.error(f"Ошибка проверки статуса миграций: {e}")
        return None
