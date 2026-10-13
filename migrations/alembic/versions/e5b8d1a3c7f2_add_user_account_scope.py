"""Отдельные аккаунты пользователей по ботам: users.account_scope_bot_id

telegram_id больше не уникален: уникальны старый общий аккаунт (скоуп NULL)
и пара (telegram_id, скоуп). FK button_click_logs → users.telegram_id снят.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e5b8d1a3c7f2"
down_revision: Union[str, None] = "d2a7c9e4f1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS account_scope_bot_id BIGINT NULL")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tg_legacy ON users (telegram_id) "
        "WHERE account_scope_bot_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_tg_scope ON users (telegram_id, account_scope_bot_id) "
        "WHERE account_scope_bot_id IS NOT NULL"
    )
    op.execute("ALTER TABLE button_click_logs DROP CONSTRAINT IF EXISTS button_click_logs_user_id_fkey")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_telegram_id_key")
    op.execute("DROP INDEX IF EXISTS ix_users_telegram_id")
    op.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users (telegram_id)")


def downgrade() -> None:
    # Обратно уникальность вернётся, только если дублей telegram_id нет.
    op.execute("DROP INDEX IF EXISTS uq_users_tg_scope")
    op.execute("DROP INDEX IF EXISTS uq_users_tg_legacy")
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)
    op.drop_column("users", "account_scope_bot_id")
