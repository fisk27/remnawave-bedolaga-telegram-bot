"""add raffle_entries table for numbered ticket tracking

Each row in ``raffle_entries`` represents one numbered ticket. The
primary key ``id`` IS the ticket number, so ticket numbering is just
``AUTOINCREMENT`` and stays globally unique across all users without
any per-user sequence. ``source`` distinguishes how the ticket was
earned ('purchase' for paid-subscription rewards, 'referral' for the
referrer/referred bonus pair).

The ``users.raffle_tickets`` counter (added in 0088) remains the cheap
"how many do I have" balance; this table is the audit log used to
list, draw from, and reveal individual ticket numbers.

Revision ID: 0089
Revises: 0088
Create Date: 2026-05-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0089'
down_revision: Union[str, None] = '0088'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'raffle_entries',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'user_id',
            sa.Integer(),
            sa.ForeignKey('users.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column(
            'created_at',
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index('ix_raffle_entries_user_id', 'raffle_entries', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_raffle_entries_user_id', table_name='raffle_entries')
    op.drop_table('raffle_entries')
