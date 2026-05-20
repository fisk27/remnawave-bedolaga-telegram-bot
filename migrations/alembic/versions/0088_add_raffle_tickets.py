"""add raffle ticket currency for giveaway contests

Introduces a second in-bot currency — "raffle tickets" — separate from
``spin_tickets``. Raffle tickets are spent on giveaway/contest entries
rather than on the fortune wheel. They are earned alongside spin tickets
on paid subscription purchases, and additionally awarded for the
referred-user / referrer pair on a first paid purchase.

Schema changes:

* ``users.raffle_tickets`` — integer balance, default 0.

Revision ID: 0088
Revises: 0087
Create Date: 2026-05-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0088'
down_revision: Union[str, None] = '0087'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'raffle_tickets',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'raffle_tickets')
