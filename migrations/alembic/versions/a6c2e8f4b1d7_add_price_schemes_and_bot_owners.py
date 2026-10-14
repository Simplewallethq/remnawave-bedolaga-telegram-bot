"""Ценовые схемы витрин и платформа владельцев ботов

price_schemes + price_scheme_tariffs (триал, устройства и цены тарифов),
price_scheme_id у copycat_bots/copycat_groups, bot_owners и owner_payouts.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a6c2e8f4b1d7"
down_revision: Union[str, None] = "e5b8d1a3c7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS price_schemes (
        id SERIAL PRIMARY KEY,
        name VARCHAR(64) NOT NULL UNIQUE,
        trial_days INTEGER NULL,
        enforce_floor BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_scheme_tariffs (
        id SERIAL PRIMARY KEY,
        scheme_id INTEGER NOT NULL REFERENCES price_schemes(id) ON DELETE CASCADE,
        plan_code VARCHAR(16) NOT NULL,
        device_limit INTEGER NULL,
        period_prices JSON NOT NULL,
        CONSTRAINT uq_price_scheme_plan UNIQUE (scheme_id, plan_code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_price_scheme_tariffs_scheme_id ON price_scheme_tariffs (scheme_id)",
    "ALTER TABLE copycat_groups ADD COLUMN IF NOT EXISTS price_scheme_id INTEGER NULL "
    "REFERENCES price_schemes(id) ON DELETE SET NULL",
    "ALTER TABLE copycat_bots ADD COLUMN IF NOT EXISTS price_scheme_id INTEGER NULL "
    "REFERENCES price_schemes(id) ON DELETE SET NULL",
    """
    CREATE TABLE IF NOT EXISTS bot_owners (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
        mode VARCHAR(16) NOT NULL DEFAULT 'owner',
        revshare_percent INTEGER NULL,
        trc20_address VARCHAR(64) NULL,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS owner_payouts (
        id SERIAL PRIMARY KEY,
        owner_id INTEGER NOT NULL REFERENCES bot_owners(id) ON DELETE CASCADE,
        amount_rub_kopeks BIGINT NOT NULL,
        amount_usdt NUMERIC(18, 2) NOT NULL,
        fee_usdt NUMERIC(18, 2) NOT NULL,
        cbr_rate NUMERIC(12, 4) NOT NULL,
        rate NUMERIC(12, 4) NOT NULL,
        trc20_address VARCHAR(64) NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        processed_by BIGINT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        processed_at TIMESTAMP NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_owner_payouts_owner_id ON owner_payouts (owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_owner_payouts_status ON owner_payouts (status)",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS owner_payouts")
    op.execute("DROP TABLE IF EXISTS bot_owners")
    op.execute("ALTER TABLE copycat_bots DROP COLUMN IF EXISTS price_scheme_id")
    op.execute("ALTER TABLE copycat_groups DROP COLUMN IF EXISTS price_scheme_id")
    op.execute("DROP TABLE IF EXISTS price_scheme_tariffs")
    op.execute("DROP TABLE IF EXISTS price_schemes")
