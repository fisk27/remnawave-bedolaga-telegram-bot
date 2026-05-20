"""Raffle ticket routes for cabinet."""

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RaffleEntry, User

from ..dependencies import get_cabinet_db, get_current_cabinet_user


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/raffle', tags=['Cabinet Raffle'])


class RaffleTicketItem(BaseModel):
    ticket_number: int
    source: str
    created_at: str


class MyTicketsResponse(BaseModel):
    tickets: list[RaffleTicketItem]


@router.get('/my-tickets', response_model=MyTicketsResponse)
async def get_my_raffle_tickets(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List numbered raffle tickets owned by the current user, oldest first."""
    result = await db.execute(
        select(RaffleEntry)
        .where(RaffleEntry.user_id == user.id)
        .order_by(RaffleEntry.id.asc())
    )
    entries = result.scalars().all()
    return MyTicketsResponse(
        tickets=[
            RaffleTicketItem(
                ticket_number=entry.id,
                source=entry.source,
                created_at=entry.created_at.isoformat() if entry.created_at else '',
            )
            for entry in entries
        ]
    )
