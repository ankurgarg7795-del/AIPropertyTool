"""Site-visit slot generation and booking.

Availability = business-hours slots minus the host's confirmed appointments.
Double-booking is prevented by the store (an EXCLUDE constraint on Postgres).
Production adds the host's Google/Microsoft free-busy and writes ICS invites.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.base import Database, SlotTaken
from app.schemas import Booking, Slot

IST = timezone(timedelta(hours=5, minutes=30))
SLOT_HOURS = (10, 12, 15, 17)
VISIT_MINUTES = 45

__all__ = ["Scheduler", "SlotTaken", "IST"]


class Scheduler:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def available(self, host_id: str, now: datetime | None = None, days: int = 3, limit: int = 4) -> list[Slot]:
        now = (now or datetime.now(timezone.utc)).astimezone(IST)
        busy = await self.db.busy_slots(host_id)
        out: list[Slot] = []
        for d in range(days + 1):
            day = (now + timedelta(days=d)).date()
            for h in SLOT_HOURS:
                start = datetime(day.year, day.month, day.day, h, tzinfo=IST)
                if start <= now + timedelta(hours=2) or start in busy:
                    continue
                out.append(Slot(slot_id=start.strftime("%Y%m%d%H%M"), start=start,
                                end=start + timedelta(minutes=VISIT_MINUTES),
                                label=start.strftime("%a %d %b, %I:%M %p")))
                if len(out) >= limit:
                    return out
        return out

    async def book(self, listing_id: str, session_id: str, host_id: str, slot: Slot,
                   buyer_id: str | None = None, lead_score: int = 0) -> Booking:
        """Raises ``SlotTaken`` if the host already has an overlapping confirmed visit."""
        return await self.db.create_booking(Booking(listing_id=listing_id, session_id=session_id, slot=slot,
                                                    host_id=host_id, buyer_id=buyer_id, lead_score=lead_score))
