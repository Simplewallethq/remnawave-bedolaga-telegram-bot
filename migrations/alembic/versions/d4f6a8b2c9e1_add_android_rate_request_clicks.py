"""add android rate request clicks table"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f6a8b2c9e1"
down_revision: Union[str, None] = "b2c4d6e8f0a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "android_rate_request_clicks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sent_notification_id",
            sa.Integer(),
            sa.ForeignKey("sent_notifications.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("callback_query_id", sa.String(length=255), nullable=True),
        sa.Column("review_url", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "sent_notification_id",
            name="uq_android_rate_request_clicks_sent_notification_id",
        ),
    )
    op.create_index(
        "ix_android_rate_request_clicks_user_id",
        "android_rate_request_clicks",
        ["user_id"],
    )
    op.create_index(
        "ix_android_rate_request_clicks_telegram_id",
        "android_rate_request_clicks",
        ["telegram_id"],
    )
    op.create_index(
        "ix_android_rate_request_clicks_created_at",
        "android_rate_request_clicks",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_android_rate_request_clicks_created_at",
        table_name="android_rate_request_clicks",
    )
    op.drop_index(
        "ix_android_rate_request_clicks_telegram_id",
        table_name="android_rate_request_clicks",
    )
    op.drop_index(
        "ix_android_rate_request_clicks_user_id",
        table_name="android_rate_request_clicks",
    )
    op.drop_table("android_rate_request_clicks")
