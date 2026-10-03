"""Process-wide service graph. ``DATABASE_URL`` set -> Postgres, otherwise in-memory."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.auth.service import AuthService
from app.concierge.agent import Concierge
from app.concierge.scheduler import Scheduler
from app.core.config import Settings, get_settings
from app.core.events import EventBus, MatchingEngine, NotificationEngine
from app.core.llm import LLM, get_llm
from app.db.base import Database
from app.db.memory import MemoryDatabase
from app.ingestion.storage import LocalMediaStorage
from app.schemas import BuyerProfile, Event, SearchFilters
from app.search.embeddings import get_embedder
from app.search.store import ListingIndex

log = logging.getLogger(__name__)


class Container:
    def __init__(self, llm: LLM | None = None, settings: Settings | None = None, db: Database | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.validate_for_env()
        self.llm = llm or get_llm()
        if db is None:
            if self.settings.database_url:
                from app.db.postgres import PostgresDatabase

                db = PostgresDatabase(self.settings.database_url, embedding_model=self.settings.embedder)
            else:
                db = MemoryDatabase()
        self.db = db
        self.bus = EventBus(self.db)
        self.store = ListingIndex(self.db, get_embedder())
        self.storage = LocalMediaStorage(self.settings.media_dir)
        self.auth = AuthService(self.db, self.settings)
        self.notifier = NotificationEngine(self.db)
        self.matcher = MatchingEngine(self.db, self.bus, self.notifier)
        self.scheduler = Scheduler(self.db)
        self.concierge = Concierge(self.db, self.store, self.llm, self.bus, self.scheduler)
        self.bus.subscribe("visit.scheduled.v1", self._on_visit)
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        if hasattr(self.db, "open"):
            if self.settings.auto_migrate:
                from app.db.migrate import migrate

                applied = await migrate(self.settings.database_url)
                if applied:
                    log.info("migrations applied: %s", applied)
            await self.db.open()
        if self.settings.seed_demo_data:
            await self.seed()
        self._started = True

    async def stop(self) -> None:
        await self.db.close()
        self._started = False

    async def _on_visit(self, e: Event) -> None:
        user_id = e.data.get("user_id")
        if not user_id:
            return
        prof = await self.db.get_profile(user_id)
        if not prof:
            user = await self.db.get_user(user_id)
            if not user:
                return
            prof = BuyerProfile(user_id=user_id, filters=SearchFilters(), phone_e164=user.phone_e164,
                                whatsapp_opt_in=user.whatsapp_opt_in)
        b = e.data["booking"]
        listing = await self.store.get(b["listing_id"])
        await self.notifier.enqueue(prof, "visit_reminder", f"visit:{b['booking_id']}", {
            "listing_id": b["listing_id"], "title": listing.data.seo_title if listing else "",
            "when_fmt": b["slot"]["label"], "address": (listing.data.address.locality or "") if listing else ""},
            now=datetime.fromisoformat(e.data["remind_at"]).astimezone(timezone.utc))

    async def seed(self) -> None:
        """Insert demo listings that don't exist yet (owners are real users, as FKs require)."""
        from app.seed import demo_listings

        owners: dict[str, str] = {}
        for listing in demo_listings():
            if await self.store.get(listing.id):
                continue
            seed_owner = listing.owner_id
            if seed_owner not in owners:
                owners[seed_owner] = (await self.db.create_user(None, ["owner"], full_name=seed_owner)).id
            listing.owner_id = owners[seed_owner]
            await self.store.upsert(listing)


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = Container()
    return _container
