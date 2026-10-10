"""Копикеты в БД: copycat_groups и copycat_bots вместо mirror_bots.yaml."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4e8a1f6b2d9"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "copycat_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("support", sa.String(length=255), nullable=True),
        sa.Column("subscription_domain", sa.String(length=255), nullable=True),
        sa.Column("happ_redirect_template", sa.String(length=512), nullable=True),
        sa.Column("privacy_url", sa.String(length=512), nullable=True),
        sa.Column("terms_url", sa.String(length=512), nullable=True),
        sa.Column("referral_terms_url", sa.String(length=512), nullable=True),
        sa.Column("payment_gateway_weights", sa.JSON(), nullable=True),
        sa.Column("owner_share_percent", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "copycat_bots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(length=128), nullable=False, unique=True),
        sa.Column("telegram_bot_id", sa.BigInteger(), nullable=True, unique=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column(
            "group_id",
            sa.Integer(),
            sa.ForeignKey("copycat_groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_copycat", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("logo", sa.String(length=255), nullable=True),
        sa.Column("image_url", sa.String(length=1024), nullable=True),
        sa.Column("privacy_url", sa.String(length=512), nullable=True),
        sa.Column("terms_url", sa.String(length=512), nullable=True),
        sa.Column("channel_link", sa.String(length=512), nullable=True),
        sa.Column("channel_id", sa.String(length=128), nullable=True),
        sa.Column("support", sa.String(length=255), nullable=True),
        sa.Column("subscription_domain", sa.String(length=255), nullable=True),
        sa.Column("happ_redirect_template", sa.String(length=512), nullable=True),
        sa.Column("referral_terms_url", sa.String(length=512), nullable=True),
        sa.Column("has_own_app", sa.Boolean(), nullable=True),
        sa.Column("rays_enabled", sa.Boolean(), nullable=True),
        sa.Column("payment_gateway_weights", sa.JSON(), nullable=True),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("owner_share_percent", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_copycat_bots_username", "copycat_bots", ["username"])
    op.create_index("ix_copycat_bots_group_id", "copycat_bots", ["group_id"])
    op.create_index("ix_copycat_bots_owner_user_id", "copycat_bots", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_copycat_bots_owner_user_id", table_name="copycat_bots")
    op.drop_index("ix_copycat_bots_group_id", table_name="copycat_bots")
    op.drop_index("ix_copycat_bots_username", table_name="copycat_bots")
    op.drop_table("copycat_bots")
    op.drop_table("copycat_groups")
