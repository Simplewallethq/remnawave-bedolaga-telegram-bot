"""add share_tokens table"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "e5c8a2b7d9f1"
down_revision: Union[str, None] = "6a4d9f2e8c1b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "share_tokens"


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
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("share_code", sa.String(length=16), nullable=False),
        sa.Column("activations_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_activations", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("token", name="uq_share_tokens_token"),
        sa.UniqueConstraint("share_code", name="uq_share_tokens_share_code"),
    )
    op.create_index(
        "ix_share_tokens_subscription_id",
        TABLE_NAME,
        ["subscription_id"],
    )
    op.create_index(
        "ix_share_tokens_token",
        TABLE_NAME,
        ["token"],
    )
    op.create_index(
        "ix_share_tokens_share_code",
        TABLE_NAME,
        ["share_code"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector):
        op.drop_table(TABLE_NAME)
