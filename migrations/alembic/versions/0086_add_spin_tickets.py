"""add spin ticket currency for fortune wheel

Introduces a new in-bot currency — "spin tickets" — earned by purchasing
paid subscriptions and spent to spin the fortune wheel. Trial
subscriptions intentionally award nothing (paying-customer perk).

Schema changes:

* ``users.spin_tickets`` — integer balance, default 0.
* ``wheel_configs.spin_cost_tickets`` — cost (in tickets) per spin,
  default 1. Mirrors the existing ``spin_cost_days``/``spin_cost_stars``
  triple of cost columns.
* ``wheel_configs.spin_cost_tickets_enabled`` — admin toggle for the
  ticket-payment path, default TRUE. Backfilled on, so existing
  installs immediately accept the new payment method once part 2 of
  the rollout ships the routing logic.

Revision ID: 0086
Revises: 0085
Create Date: 2026-05-17

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0086'
down_revision: Union[str, None] = '0085'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'spin_tickets',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )

    op.add_column(
        'wheel_configs',
        sa.Column(
            'spin_cost_tickets',
            sa.Integer(),
            nullable=False,
            server_default='1',
        ),
    )

    op.add_column(
        'wheel_configs',
        sa.Column(
            'spin_cost_tickets_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column('wheel_configs', 'spin_cost_tickets_enabled')
    op.drop_column('wheel_configs', 'spin_cost_tickets')
    op.drop_column('users', 'spin_tickets')
