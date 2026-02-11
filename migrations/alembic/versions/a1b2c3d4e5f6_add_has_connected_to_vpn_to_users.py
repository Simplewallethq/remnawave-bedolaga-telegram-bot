from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "e3c1e0b5b4a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("has_connected_to_vpn", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("users", "has_connected_to_vpn")
