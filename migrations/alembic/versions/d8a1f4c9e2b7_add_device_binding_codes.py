"""add device_binding_codes table"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "d8a1f4c9e2b7"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "device_binding_codes"


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
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("used_device_id", sa.String(length=255), nullable=True),
        sa.UniqueConstraint("code", name="uq_device_binding_codes_code"),
    )
    op.create_index(
        "ix_device_binding_codes_subscription_id",
        TABLE_NAME,
        ["subscription_id"],
    )
    op.create_index(
        "ix_device_binding_codes_code",
        TABLE_NAME,
        ["code"],
    )
    op.create_index(
        "ix_device_binding_codes_expires_at",
        TABLE_NAME,
        ["expires_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _table_exists(inspector):
        op.drop_table(TABLE_NAME)
