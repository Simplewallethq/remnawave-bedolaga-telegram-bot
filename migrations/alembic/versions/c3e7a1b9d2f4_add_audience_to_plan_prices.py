"""add audience column to subscription_plan_prices (cohort pricing)

Splits Solo/Plus/Pro 1-month and 6-month pricing into cohorts: existing users
keep the old prices and the 180-day period ('legacy'); new users get the higher
1-month price ('new') and no 180-day option. Rows tagged 'all' apply to everyone.

NOTE: the live boot path applies this via app/database/universal_migration.py
(add_audience_to_plan_prices); this revision exists for Alembic-history parity.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c3e7a1b9d2f4"
down_revision: Union[str, None] = "b7d2f9a1c3e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_MONTHLY = {"solo": 32000, "plus": 49000, "pro": 69000}


def upgrade() -> None:
    op.add_column(
        "subscription_plan_prices",
        sa.Column(
            "audience",
            sa.String(length=8),
            nullable=False,
            server_default="all",
        ),
    )

    # Grandfather Solo/Plus/Pro 1-month and 6-month rows as 'legacy'.
    op.execute(
        "UPDATE subscription_plan_prices SET audience='legacy' "
        "WHERE period_days IN (30, 180) AND plan_id IN ("
        "SELECT id FROM subscription_plans WHERE code IN ('solo', 'plus', 'pro'))"
    )

    # Insert the new-cohort 1-month prices.
    for code, price in _NEW_MONTHLY.items():
        op.execute(
            "INSERT INTO subscription_plan_prices "
            "(plan_id, period_days, price_kopeks, audience) "
            f"SELECT id, 30, {price}, 'new' FROM subscription_plans WHERE code='{code}'"
        )

    # Swap the unique constraint to include audience.
    with op.batch_alter_table("subscription_plan_prices") as batch:
        batch.drop_constraint("uq_plan_period", type_="unique")
        batch.create_unique_constraint(
            "uq_plan_period", ["plan_id", "period_days", "audience"]
        )


def downgrade() -> None:
    op.execute("DELETE FROM subscription_plan_prices WHERE audience='new'")
    with op.batch_alter_table("subscription_plan_prices") as batch:
        batch.drop_constraint("uq_plan_period", type_="unique")
        batch.create_unique_constraint("uq_plan_period", ["plan_id", "period_days"])
    op.drop_column("subscription_plan_prices", "audience")
