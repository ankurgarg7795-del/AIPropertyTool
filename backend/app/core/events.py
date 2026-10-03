"""Event bus, predictive matching and notification fan-out.

In production the bus is Kafka (topics listed in docs/ARCHITECTURE.md §3.2)
and each consumer is its own worker deployment. Here it is an in-process
async pub/sub with the same CloudEvents envelope; every published event is
also appended to the database (``outbox_events`` on Postgres) for relay.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.db.base import Database
from app.schemas import BuyerProfile, Event, Listing, Notification, ParsedQuery
from app.search.rules import matches, preference_tags

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subs[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        await self.db.append_event(event)
        for h in self._subs.get(event.type, []):
            try:
                await h(event)
            except Exception:  # a failing consumer must not break the producer
                log.exception("handler failed for %s", event.type)


class NotificationEngine:
    """Rules: per-user dedup, channel preference, quiet hours (22:00-08:00 IST,
    WhatsApp/SMS deferred; push allowed), WhatsApp only with opt-in."""

    QUIET_START, QUIET_END = time(22, 0), time(8, 0)

    def __init__(self, db: Database) -> None:
        self.db = db

    def deliver_at(self, channel: str, now: datetime) -> datetime:
        local = now.astimezone(IST)
        quiet = local.time() >= self.QUIET_START or local.time() < self.QUIET_END
        if channel in ("whatsapp", "sms") and quiet:
            nxt = local if local.time() < self.QUIET_END else local + timedelta(days=1)
            return nxt.replace(hour=8, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        return now

    async def enqueue(self, profile: BuyerProfile, kind: str, dedup_key: str, payload: dict[str, Any],
                      now: datetime | None = None) -> list[Notification]:
        now = now or datetime.now(timezone.utc)
        out = []
        for ch in profile.channels:
            if ch == "whatsapp" and not (profile.whatsapp_opt_in and profile.phone_e164):
                continue
            n = Notification(user_id=profile.user_id, kind=kind, channel=ch,
                             dedup_key=f"{profile.user_id}:{ch}:{dedup_key}", payload=payload,
                             scheduled_for=self.deliver_at(ch, now))
            if await self.db.add_notification(n):
                out.append(n)
        return out


def whatsapp_template_payload(n: Notification, phone_e164: str) -> dict[str, Any]:
    """WhatsApp Cloud API message body for an approved template."""
    p = n.payload
    if n.kind == "price_drop":
        name, params = "price_drop_alert_v2", [p["title"], p["old_price_fmt"], p["new_price_fmt"], p["drop_pct"]]
    elif n.kind == "new_match":
        name, params = "new_match_alert_v1", [p["title"], p["price_fmt"], ", ".join(p.get("why", [])) or "your search"]
    elif n.kind == "visit_reminder":
        name, params = "site_visit_reminder_v1", [p["title"], p["when_fmt"], p["address"]]
    else:
        name, params = "generic_update_v1", [p.get("text", "")]
    return {
        "messaging_product": "whatsapp",
        "to": phone_e164.lstrip("+"),
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": "en_IN"},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": str(x)} for x in params]},
                {"type": "button", "sub_type": "url", "index": "0",
                 "parameters": [{"type": "text", "text": p.get("listing_id", "")}]},
            ],
        },
    }


def fmt_inr(v: float | None) -> str:
    if v is None:
        return "Price on request"
    if v >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.1f} L"
    return f"₹{v:,.0f}"


class MatchingEngine:
    """Predictive matching: every new or re-priced listing is scored against
    signed-in buyers' profiles (reverse search), and every search refines a profile."""

    def __init__(self, db: Database, bus: EventBus, notifier: NotificationEngine):
        self.db, self.bus, self.notifier = db, bus, notifier
        bus.subscribe("search.performed.v1", self.on_search)
        bus.subscribe("listing.published.v1", self.on_listing)
        bus.subscribe("listing.price_changed.v1", self.on_price_change)

    async def _profile_for(self, user_id: str, parsed: ParsedQuery | None = None) -> BuyerProfile | None:
        prof = await self.db.get_profile(user_id)
        if prof:
            return prof
        user = await self.db.get_user(user_id)
        if not user:
            return None
        return BuyerProfile(user_id=user_id, filters=parsed.filters if parsed else {},
                            phone_e164=user.phone_e164, whatsapp_opt_in=user.whatsapp_opt_in)

    async def update_profile(self, user_id: str, parsed: ParsedQuery) -> BuyerProfile | None:
        prof = await self._profile_for(user_id, parsed)
        if not prof:
            return None
        # Latest explicit constraints win; soft preferences accumulate (bounded).
        prof.filters = parsed.filters
        prof.soft_preferences = list(dict.fromkeys(parsed.soft_preferences + prof.soft_preferences))[:8]
        prof.searches += 1
        prof.updated_at = datetime.now(timezone.utc)
        await self.db.save_profile(prof)
        return prof

    async def record_view(self, user_id: str, listing_id: str) -> None:
        prof = await self.db.get_profile(user_id)
        if prof and listing_id not in prof.viewed_listing_ids:
            prof.viewed_listing_ids = (prof.viewed_listing_ids + [listing_id])[-50:]
            await self.db.save_profile(prof)

    async def on_search(self, e: Event) -> None:
        if e.data.get("user_id"):
            await self.update_profile(e.data["user_id"], ParsedQuery.model_validate(e.data["parsed"]))

    @staticmethod
    def profile_matches(prof: BuyerProfile, listing: Listing) -> tuple[bool, list[str]]:
        if not matches(listing, prof.filters):
            return False, []
        why = sorted({p.lower() for p in prof.soft_preferences} & preference_tags(listing))
        # Require at least half of the soft preferences when the buyer expressed any.
        ok = not prof.soft_preferences or len(why) * 2 >= len(prof.soft_preferences)
        return ok, why

    async def on_listing(self, e: Event) -> None:
        listing = Listing.model_validate(e.data["listing"])
        for prof in await self.db.list_profiles():
            if prof.user_id == listing.owner_id:
                continue
            ok, why = self.profile_matches(prof, listing)
            if ok:
                await self.notifier.enqueue(prof, "new_match", f"match:{listing.id}", {
                    "listing_id": listing.id, "title": listing.data.seo_title,
                    "price_fmt": fmt_inr(listing.data.price_inr), "why": why})

    async def on_price_change(self, e: Event) -> None:
        listing = Listing.model_validate(e.data["listing"])
        old, new = e.data["old_price_inr"], e.data["new_price_inr"]
        if new >= old:
            return
        pct = round((old - new) / old * 100, 1)
        for prof in await self.db.list_profiles():
            if prof.user_id == listing.owner_id:
                continue
            ok, _ = self.profile_matches(prof, listing)
            if ok or listing.id in prof.viewed_listing_ids:
                await self.notifier.enqueue(prof, "price_drop", f"drop:{listing.id}:{int(new)}", {
                    "listing_id": listing.id, "title": listing.data.seo_title,
                    "old_price_fmt": fmt_inr(old), "new_price_fmt": fmt_inr(new), "drop_pct": f"{pct}%"})
