"""add ray_prize_claims.contact + withdrawal_requests — кабинет сайта.

ВНИМАНИЕ: на проде схему реально накатывает app/database/universal_migration.py
(add_ray_prize_claim_contact_column, create_withdrawal_requests_table) при
старте бота. Этот файл — паритет для alembic-цепочки; в репозитории несколько
heads, «alembic upgrade head» вслепую не запускать.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "b7e3d5a9c2f4"
down_revision: Union[str, None] = "a2d4f6b8c1e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CLAIMS_TABLE = "ray_prize_claims"
WITHDRAWALS_TABLE = "withdrawal_requests"


def _column_exists(inspector: Inspector, table: str, column: str) -> bool:
    if table not in inspector.get_table_names():
        return False
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table(CLAIMS_TABLE) and not _column_exists(inspector, CLAIMS_TABLE, "contact"):
        op.add_column(CLAIMS_TABLE, sa.Column("contact", sa.String(length=255), nullable=True))

    if not inspector.has_table(WITHDRAWALS_TABLE):
        op.create_table(
            WITHDRAWALS_TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("amount_kopeks", sa.Integer(), nullable=False),
            sa.Column("details", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("debit_transaction_id", sa.Integer(), sa.ForeignKey("transactions.id"), nullable=True),
            sa.Column("refund_transaction_id", sa.Integer(), sa.ForeignKey("transactions.id"), nullable=True),
            sa.Column("processed_by", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("processed_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_withdrawal_requests_user_id", WITHDRAWALS_TABLE, ["user_id"])
        op.create_index("ix_withdrawal_requests_status", WITHDRAWALS_TABLE, ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table(WITHDRAWALS_TABLE):
        op.drop_index("ix_withdrawal_requests_status", table_name=WITHDRAWALS_TABLE)
        op.drop_index("ix_withdrawal_requests_user_id", table_name=WITHDRAWALS_TABLE)
        op.drop_table(WITHDRAWALS_TABLE)

    if _column_exists(inspector, CLAIMS_TABLE, "contact"):
        op.drop_column(CLAIMS_TABLE, "contact")
