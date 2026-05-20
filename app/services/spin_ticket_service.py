"""Helper for awarding fortune-wheel spin tickets.

Tickets are earned when a user purchases (renews or extends) a *paid*
subscription — 1 ticket per 30 days of subscription time. Trial
subscriptions intentionally award nothing so the bonus is gated to
paying customers.

This module owns the +N mutation on ``User.spin_tickets``. Spending is
owned by the wheel service.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User


logger = structlog.get_logger(__name__)


TICKET_PERIOD_DAYS = 30


async def award_spin_tickets(
    db: AsyncSession,
    user: User,
    period_days: int,
    is_trial: bool = False,
) -> int:
    """Add spin tickets to ``user.spin_tickets`` for a subscription purchase.

    Returns the number of tickets actually awarded (may be 0 for trials
    or for paid periods shorter than ``TICKET_PERIOD_DAYS``).
    """
    if is_trial:
        logger.debug(
            '🎟️ Trial subscription — no spin tickets awarded',
            user_id=user.id,
            period_days=period_days,
        )
        return 0

    if period_days <= 0:
        return 0

    tickets = period_days // TICKET_PERIOD_DAYS
    if tickets <= 0:
        return 0

    user.spin_tickets = (user.spin_tickets or 0) + tickets
    await db.flush()
    await db.commit()

    logger.info(
        '🎟️ Spin tickets awarded',
        user_id=user.id,
        period_days=period_days,
        tickets_awarded=tickets,
        new_balance=user.spin_tickets,
    )
    return tickets
