"""Helper for awarding fortune-wheel spin tickets.

Two separate ticket systems:
- spin_tickets: spent on fortune wheel spins. Earned from subscription purchase/renewal only.
- raffle_tickets: accumulated for giveaway drawing. Earned from purchases AND referrals. Never spent, only counted.
Both are awarded on purchase/renewal. Only raffle_tickets are awarded for referrals.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import and_, func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RaffleEntry, User


logger = structlog.get_logger(__name__)


TICKET_PERIOD_DAYS = 30
REFERRAL_TICKETS_DAILY_LIMIT = 10


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
        logger.debug('No tickets to award', period_days=period_days, user_id=user.id)
        return 0

    tickets = period_days // TICKET_PERIOD_DAYS
    if tickets <= 0:
        logger.debug('No tickets to award', period_days=period_days, user_id=user.id)
        return 0

    await db.execute(
        sql_update(User)
        .where(User.id == user.id)
        .values(
            spin_tickets=User.spin_tickets + tickets,
            raffle_tickets=User.raffle_tickets + tickets,
        )
    )
    db.add_all([RaffleEntry(user_id=user.id, source='purchase') for _ in range(tickets)])
    # Caller must commit — we only flush to get values into the session
    await db.flush()
    await db.refresh(user)

    logger.info(
        '🎟️ Spin tickets awarded',
        user_id=user.id,
        period_days=period_days,
        tickets_awarded=tickets,
        new_balance=user.spin_tickets,
        raffle_balance=user.raffle_tickets,
    )
    return tickets


async def award_referral_tickets(
    db: AsyncSession,
    user: User,
    is_first_purchase: bool,
) -> None:
    """Award 1 ticket to referrer and 1 extra to the referred user on first purchase."""
    if not is_first_purchase or not user.referred_by_id:
        return

    if user.referred_by_id == user.id:
        logger.warning('Self-referral detected, skipping', user_id=user.id)
        return

    # Rate limit: cap referral tickets per referrer per day to make farming uneconomic.
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    today_referral_count = (await db.execute(
        select(func.count()).select_from(RaffleEntry).where(
            and_(
                RaffleEntry.user_id == user.referred_by_id,
                RaffleEntry.source == 'referral',
                RaffleEntry.created_at >= today_start,
            )
        )
    )).scalar() or 0

    if today_referral_count >= REFERRAL_TICKETS_DAILY_LIMIT:
        logger.warning(
            'Referral ticket daily limit reached',
            referrer_id=user.referred_by_id,
            count=today_referral_count,
        )
        return

    result = await db.execute(
        sql_update(User)
        .where(User.id == user.referred_by_id)
        .values(raffle_tickets=User.raffle_tickets + 1)
    )
    if result.rowcount == 0:
        logger.warning('Referrer not found, skipping', referrer_id=user.referred_by_id)
        return

    await db.execute(
        sql_update(User)
        .where(User.id == user.id)
        .values(raffle_tickets=User.raffle_tickets + 1)
    )
    db.add_all([
        RaffleEntry(user_id=user.referred_by_id, source='referral'),
        RaffleEntry(user_id=user.id, source='referral'),
    ])
    # Caller must commit — we only flush to get values into the session
    await db.flush()

    logger.info(
        '🎟️ Referral tickets awarded',
        referrer_id=user.referred_by_id,
        referred_user_id=user.id,
    )
