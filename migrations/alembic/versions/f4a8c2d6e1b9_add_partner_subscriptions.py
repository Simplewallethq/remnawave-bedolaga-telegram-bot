"""add is_partner flag and partner_link_redemptions table"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "f4a8c2d6e1b9"
down_revision: Union[str, None] = "d8a1f4c9e2b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUBSCRIPTIONS_TABLE = "subscriptions"
IS_PARTNER_COLUMN = "is_partner"
TYPE_MUTEX_CONSTRAINT = "subscriptions_type_mutex"
REDEMPTIONS_TABLE = "partner_link_redemptions"


def _column_exists(inspector: Inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def _constraint_exists(inspector: Inspector, table: str, name: str) -> bool:
    return any(c.get("name") == name for c in inspector.get_check_constraints(table))


def _table_exists(inspector: Inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, SUBSCRIPTIONS_TABLE, IS_PARTNER_COLUMN):
        op.add_column(
            SUBSCRIPTIONS_TABLE,
            sa.Column(
                IS_PARTNER_COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    inspector = sa.inspect(bind)
    if not _constraint_exists(inspector, SUBSCRIPTIONS_TABLE, TYPE_MUTEX_CONSTRAINT):
        op.create_check_constraint(
            TYPE_MUTEX_CONSTRAINT,
            SUBSCRIPTIONS_TABLE,
            "NOT (is_trial AND is_partner)",
        )

    if not _table_exists(inspector, REDEMPTIONS_TABLE):
        op.create_table(
            REDEMPTIONS_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("jti", sa.String(length=32), nullable=False),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "subscription_id",
                sa.Integer(),
                sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("sub_until", sa.DateTime(), nullable=False),
            sa.Column(
                "redeemed_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("jti", name="uq_partner_link_redemptions_jti"),
        )
        op.create_index(
            "ix_partner_link_redemptions_user_id",
            REDEMPTIONS_TABLE,
            ["user_id"],
        )
        op.create_index(
            "ix_partner_link_redemptions_redeemed_at",
            REDEMPTIONS_TABLE,
            ["redeemed_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector, REDEMPTIONS_TABLE):
        op.drop_table(REDEMPTIONS_TABLE)

    inspector = sa.inspect(bind)
    if _constraint_exists(inspector, SUBSCRIPTIONS_TABLE, TYPE_MUTEX_CONSTRAINT):
        op.drop_constraint(
            TYPE_MUTEX_CONSTRAINT,
            SUBSCRIPTIONS_TABLE,
            type_="check",
        )

    inspector = sa.inspect(bind)
    if _column_exists(inspector, SUBSCRIPTIONS_TABLE, IS_PARTNER_COLUMN):
        op.drop_column(SUBSCRIPTIONS_TABLE, IS_PARTNER_COLUMN)
