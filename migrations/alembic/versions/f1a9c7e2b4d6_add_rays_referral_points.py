"""add rays (referral points) wallet, lifetime counter, credited flag and ledger"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "f1a9c7e2b4d6"
down_revision: Union[str, None] = "e7b3c9d1a4f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "ray_transactions"


def _table_exists(inspector: Inspector) -> bool:
    return TABLE_NAME in inspector.get_table_names()


def _user_columns(inspector: Inspector) -> set:
    return {col["name"] for col in inspector.get_columns("users")}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_cols = _user_columns(inspector)
    if "rays_balance" not in user_cols:
        op.add_column(
            "users",
            sa.Column("rays_balance", sa.Integer(), nullable=False, server_default="0"),
        )
    if "rays_lifetime_earned" not in user_cols:
        op.add_column(
            "users",
            sa.Column("rays_lifetime_earned", sa.Integer(), nullable=False, server_default="0"),
        )
    if "rays_credited" not in user_cols:
        op.add_column(
            "users",
            sa.Column("rays_credited", sa.Boolean(), nullable=False, server_default="false"),
        )

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
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("type", sa.String(length=50), nullable=False),
            sa.Column(
                "referral_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
            sa.Column(
                "source_transaction_id",
                sa.Integer(),
                sa.ForeignKey("transactions.id"),
                nullable=True,
            ),
            sa.Column("period_days", sa.Integer(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "source_transaction_id",
                name="uq_ray_transactions_source_transaction_id",
            ),
        )
        op.create_index("ix_ray_transactions_user_id", TABLE_NAME, ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector):
        op.drop_index("ix_ray_transactions_user_id", table_name=TABLE_NAME)
        op.drop_table(TABLE_NAME)

    user_cols = _user_columns(inspector)
    if "rays_credited" in user_cols:
        op.drop_column("users", "rays_credited")
    if "rays_lifetime_earned" in user_cols:
        op.drop_column("users", "rays_lifetime_earned")
    if "rays_balance" in user_cols:
        op.drop_column("users", "rays_balance")
