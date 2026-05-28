from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a9f3c2d1e8b4"
down_revision: Union[str, None] = "f4a8c2d6e1b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("raw_start_payload", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("attribution_source", sa.String(100), nullable=True))
    op.add_column("users", sa.Column("attribution_campaign_id", sa.String(8), nullable=True))
    op.create_index(
        "ix_users_attribution_campaign_id",
        "users",
        ["attribution_campaign_id"],
    )

    op.create_table(
        "ad_campaign_visits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("raw_payload", sa.String(64), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("campaign_id", sa.String(8), nullable=False),
        sa.Column("is_new_user", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("visited_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ad_campaign_visits_telegram_id", "ad_campaign_visits", ["telegram_id"])
    op.create_index("ix_ad_campaign_visits_user_id", "ad_campaign_visits", ["user_id"])
    op.create_index("ix_ad_campaign_visits_campaign_id", "ad_campaign_visits", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_ad_campaign_visits_campaign_id", "ad_campaign_visits")
    op.drop_index("ix_ad_campaign_visits_user_id", "ad_campaign_visits")
    op.drop_index("ix_ad_campaign_visits_telegram_id", "ad_campaign_visits")
    op.drop_table("ad_campaign_visits")

    op.drop_index("ix_users_attribution_campaign_id", "users")
    op.drop_column("users", "attribution_campaign_id")
    op.drop_column("users", "attribution_source")
    op.drop_column("users", "raw_start_payload")
