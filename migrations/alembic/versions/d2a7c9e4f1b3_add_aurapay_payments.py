"""add aurapay_payments (счета AuraPay)

Revision ID: d2a7c9e4f1b3
Revises: c4e8a1f6b2d9
Create Date: 2026-10-12

Реально таблицу создаёт Base.metadata.create_all и идемпотентная
app/database/universal_migration.py (create_aurapay_payments_table); этот файл —
для истории alembic.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2a7c9e4f1b3"
down_revision: Union[str, None] = "c4e8a1f6b2d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "aurapay_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("order_id", sa.String(length=255), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), nullable=True),
        sa.Column("amount_kopeks", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="RUB"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("is_paid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("callback_payload", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "transaction_id", sa.Integer(), sa.ForeignKey("transactions.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_aurapay_payments_order_id", "aurapay_payments", ["order_id"], unique=True)
    op.create_index(
        "ix_aurapay_payments_invoice_id", "aurapay_payments", ["invoice_id"], unique=True
    )
    op.create_index(
        "ix_aurapay_payments_unpaid_created", "aurapay_payments", ["is_paid", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_aurapay_payments_unpaid_created", table_name="aurapay_payments")
    op.drop_index("ix_aurapay_payments_invoice_id", table_name="aurapay_payments")
    op.drop_index("ix_aurapay_payments_order_id", table_name="aurapay_payments")
    op.drop_table("aurapay_payments")
