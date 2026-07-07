"""add ray_prize_claims — заявки на призы Магазина Наград"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "a2d4f6b8c1e3"
down_revision: Union[str, None] = "f1a9c7e2b4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "ray_prize_claims"


def _table_exists(inspector: Inspector) -> bool:
    return TABLE_NAME in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector):
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("prize_code", sa.String(length=32), nullable=False),
            sa.Column("prize_title", sa.String(length=128), nullable=False),
            sa.Column("cost_rays", sa.Integer(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column(
                "spend_transaction_id",
                sa.Integer(),
                sa.ForeignKey("ray_transactions.id"),
                nullable=True,
            ),
            sa.Column(
                "refund_transaction_id",
                sa.Integer(),
                sa.ForeignKey("ray_transactions.id"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_ray_prize_claims_user_id", TABLE_NAME, ["user_id"])
        op.create_index("ix_ray_prize_claims_status", TABLE_NAME, ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector):
        op.drop_index("ix_ray_prize_claims_status", table_name=TABLE_NAME)
        op.drop_index("ix_ray_prize_claims_user_id", table_name=TABLE_NAME)
        op.drop_table(TABLE_NAME)
