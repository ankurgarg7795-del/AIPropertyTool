"""Site-visit slot generation and booking.

Production: availability comes from the seller/agent/developer sales team's
Google/Microsoft calendar via free-busy APIs; bookings write calendar events
with an ICS invite. Here: business-hours slots minus in-memory bookings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

IST = timezone(timedelta(hours=5, minutes=30))
SLOT_HOURS = (10, 12, 15, 17)
VISIT_MINUTES = 45


class Slot(BaseModel):
    slot_id: str
    start: datetime
    end: datetime
    label: str


class Booking(BaseModel):
    booking_id: str = Field(default_factory=lambda: f"bkg_{uuid4().hex[:10]}")
    listing_id: str
    session_id: str
    slot: Slot
    host_id: str
    status: str = "confirmed"


class Scheduler:
    def __init__(self) -> None:
        self.bookings: list[Booking] = []

    def _busy(self, host_id: str) -> set[datetime]:
        return {b.slot.start for b in self.bookings if b.host_id == host_id and b.status == "confirmed"}

    def available(self, host_id: str, now: datetime | None = None, days: int = 3, limit: int = 4) -> list[Slot]:
        now = (now or datetime.now(timezone.utc)).astimezone(IST)
        busy = self._busy(host_id)
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

    def book(self, listing_id: str, session_id: str, host_id: str, slot: Slot) -> Booking:
        if slot.start in self._busy(host_id):
            raise ValueError("slot no longer available")
        b = Booking(listing_id=listing_id, session_id=session_id, slot=slot, host_id=host_id)
        self.bookings.append(b)
        return b
