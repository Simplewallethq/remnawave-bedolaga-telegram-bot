"""add user daily traffic usage table"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "e7b3c9d1a4f2"
down_revision: Union[str, None] = "c3e7a1b9d2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "user_daily_traffic_usage"


def _table_exists(inspector: Inspector) -> bool:
    return TABLE_NAME in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector):
        return

    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("traffic_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "user_id",
            "date",
            name="uq_user_daily_traffic_usage_user_date",
        ),
    )
    op.create_index("ix_user_daily_traffic_usage_user_id", TABLE_NAME, ["user_id"])
    op.create_index("ix_user_daily_traffic_usage_date", TABLE_NAME, ["date"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector):
        return

    op.drop_index("ix_user_daily_traffic_usage_date", table_name=TABLE_NAME)
    op.drop_index("ix_user_daily_traffic_usage_user_id", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
