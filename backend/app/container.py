"""Process-wide service graph (swap implementations here for production adapters)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.concierge.agent import Concierge
from app.concierge.scheduler import Scheduler
from app.core.config import get_settings
from app.core.events import BuyerProfile, Event, EventBus, MatchingEngine, NotificationEngine
from app.core.llm import LLM, get_llm
from app.search.embeddings import get_embedder
from app.search.store import ListingStore


class Container:
    def __init__(self, llm: LLM | None = None) -> None:
        self.settings = get_settings()
        self.llm = llm or get_llm()
        self.bus = EventBus()
        self.store = ListingStore(get_embedder())
        self.notifier = NotificationEngine()
        self.matcher = MatchingEngine(self.bus, self.notifier)
        self.scheduler = Scheduler()
        self.concierge = Concierge(self.store, self.llm, self.bus, self.scheduler)
        self.bus.subscribe("visit.scheduled.v1", self._on_visit)

    async def _on_visit(self, e: Event) -> None:
        user = e.data.get("user_id")
        if not user:
            return
        prof = self.matcher.profiles.get(user) or BuyerProfile(user_id=user, filters={})
        b = e.data["booking"]
        listing = self.store.get(b["listing_id"])
        self.notifier.enqueue(prof, "visit_reminder", f"visit:{b['booking_id']}", {
            "listing_id": b["listing_id"], "title": listing.data.seo_title if listing else "",
            "when_fmt": b["slot"]["label"], "address": (listing.data.address.locality or "") if listing else ""},
            now=datetime.fromisoformat(e.data["remind_at"]).astimezone(timezone.utc))

    def seed(self) -> None:
        from app.seed import demo_listings

        for listing in demo_listings():
            self.store.upsert(listing)


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = Container()
    return _container
