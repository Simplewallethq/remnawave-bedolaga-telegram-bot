"""A/B «доступ за 1 ₽ вместо триала»: users.trial_offer_variant, subscriptions.is_paid_trial."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d8e2f4a6b1c3"
down_revision: Union[str, None] = "c7d9e1f3a5b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("trial_offer_variant", sa.String(length=16), nullable=True))
    op.create_index("ix_users_trial_offer_variant", "users", ["trial_offer_variant"])
    op.add_column(
        "subscriptions",
        sa.Column("is_paid_trial", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "is_paid_trial")
    op.drop_index("ix_users_trial_offer_variant", table_name="users")
    op.drop_column("users", "trial_offer_variant")
