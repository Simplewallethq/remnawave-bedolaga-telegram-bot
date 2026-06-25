"""add feedbacks table"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "6a4d9f2e8c1b"
down_revision: Union[str, None] = "a9f3c2d1e8b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = "feedbacks"


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
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("event_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="sent"),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("selected_option", sa.String(length=100), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("event_key", name="uq_feedbacks_event_key"),
    )
    op.create_index("ix_feedbacks_type", TABLE_NAME, ["type"])
    op.create_index("ix_feedbacks_user_id", TABLE_NAME, ["user_id"])
    op.create_index("ix_feedbacks_subscription_id", TABLE_NAME, ["subscription_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector):
        return

    op.drop_index("ix_feedbacks_subscription_id", table_name=TABLE_NAME)
    op.drop_index("ix_feedbacks_user_id", table_name=TABLE_NAME)
    op.drop_index("ix_feedbacks_type", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
