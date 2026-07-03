"""add interactive notification logs table"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c4d6e8f0a2"
down_revision: Union[str, None] = "f1a9c7e2b4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "interactive_notification_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slot_key", sa.String(length=50), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_interactive_notification_logs_slot_key",
        "interactive_notification_logs",
        ["slot_key"],
    )
    op.create_index(
        "ix_interactive_notification_logs_user_id",
        "interactive_notification_logs",
        ["user_id"],
    )
    op.create_index(
        "ix_interactive_notification_logs_status",
        "interactive_notification_logs",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_interactive_notification_logs_status",
        table_name="interactive_notification_logs",
    )
    op.drop_index(
        "ix_interactive_notification_logs_user_id",
        table_name="interactive_notification_logs",
    )
    op.drop_index(
        "ix_interactive_notification_logs_slot_key",
        table_name="interactive_notification_logs",
    )
    op.drop_table("interactive_notification_logs")
