"""add onepayment tables (1Payment СБП: платежи и привязки токенов)

Revision ID: c7d9e1f3a5b7
Revises: 2b3c1d4e5f6a
Create Date: 2026-09-07

Реально таблицы создаёт Base.metadata.create_all и идемпотентная
app/database/universal_migration.py (create_onepayment_*_table); этот файл —
для истории alembic.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7d9e1f3a5b7"
down_revision: Union[str, None] = "2b3c1d4e5f6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "onepayment_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("active_user_id", sa.Integer(), nullable=True, unique=True),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
        sa.Column("source_payment_id", sa.Integer(), nullable=True),
        sa.Column("last_charge_at", sa.DateTime(), nullable=True),
        sa.Column("last_charge_status", sa.String(length=20), nullable=True),
        sa.Column("last_charged_period_end", sa.DateTime(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_onepayment_bindings_user_status",
        "onepayment_bindings",
        ["user_id", "status"],
    )

    op.create_table(
        "onepayment_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("user_data", sa.String(length=255), nullable=False, unique=True),
        sa.Column("provider_order_id", sa.String(length=255), nullable=True, unique=True),
        sa.Column("amount_kopeks", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="RUB"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="INIT"),
        sa.Column("status_code", sa.String(length=50), nullable=True),
        sa.Column("is_paid", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("is_recurring", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("binding_id", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("callback_payload", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["binding_id"], ["onepayment_bindings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
    )
    op.create_index("ix_onepayment_payments_binding_id", "onepayment_payments", ["binding_id"])
    op.create_index(
        "ix_onepayment_payments_unpaid_created",
        "onepayment_payments",
        ["is_paid", "created_at"],
    )

    op.create_foreign_key(
        "fk_onepayment_bindings_source_payment_id",
        "onepayment_bindings",
        "onepayment_payments",
        ["source_payment_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_onepayment_bindings_source_payment_id", "onepayment_bindings", type_="foreignkey"
    )
    op.drop_index("ix_onepayment_payments_unpaid_created", table_name="onepayment_payments")
    op.drop_index("ix_onepayment_payments_binding_id", table_name="onepayment_payments")
    op.drop_table("onepayment_payments")
    op.drop_index("ix_onepayment_bindings_user_status", table_name="onepayment_bindings")
    op.drop_table("onepayment_bindings")
