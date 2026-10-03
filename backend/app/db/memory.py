"""In-process implementation of ``Database`` (tests, zero-setup dev)."""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from uuid import uuid4

from app.db.base import Candidate, Database, OtpRecord, RefreshRecord, SlotTaken
from app.schemas import (
    Booking, BuyerProfile, ConciergeSession, Event, Listing, Notification, SearchFilters, User,
)
from app.search.rules import matches


class MemoryDatabase(Database):
    def __init__(self) -> None:
        self.listings: dict[str, Listing] = {}
        self._vec: dict[str, list[float]] = {}
        self._tf: dict[str, Counter] = {}
        self._df: Counter = Counter()
        self.users: dict[str, User] = {}
        self._otps: dict[str, OtpRecord] = {}
        self._refresh: dict[str, RefreshRecord] = {}
        self.profiles: dict[str, BuyerProfile] = {}
        self.notifications: list[Notification] = []
        self._dedup: set[str] = set()
        self.bookings: list[Booking] = []
        self.sessions: dict[str, ConciergeSession] = {}
        self.events: list[Event] = []

    # ---- listings ------------------------------------------------------- #
    async def upsert_listing(self, listing, embedding, search_tokens, tags):
        if listing.id in self._tf:
            self._df.subtract(set(self._tf[listing.id]))
        tf = Counter(search_tokens.split())
        self.listings[listing.id] = listing.model_copy(deep=True)
        self._vec[listing.id] = embedding
        self._tf[listing.id] = tf
        self._df.update(set(tf))

    async def get_listing(self, listing_id):
        lst = self.listings.get(listing_id)
        return lst.model_copy(deep=True) if lst else None

    def _bm25(self, q: list[str], lid: str, k1: float = 1.2, b: float = 0.75) -> float:
        tf = self._tf[lid]
        n = len(self._tf) or 1
        avgdl = sum(sum(t.values()) for t in self._tf.values()) / n
        dl = sum(tf.values())
        s = 0.0
        for term in set(q):
            if term not in tf:
                continue
            idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        return s

    async def listing_candidates(self, filters: SearchFilters, query_vec, query_tokens, recall):
        cands = [lst for lst in self.listings.values() if matches(lst, filters)]
        scored = [Candidate(lst.model_copy(deep=True),
                            max(0.0, sum(a * b for a, b in zip(query_vec, self._vec[lst.id]))),
                            self._bm25(query_tokens, lst.id)) for lst in cands]
        scored.sort(key=lambda c: c.semantic, reverse=True)
        return scored[:recall], len(cands)

    async def count_listings(self):
        return len(self.listings)

    # ---- users & auth --------------------------------------------------- #
    async def get_user(self, user_id):
        return self.users.get(user_id)

    async def get_user_by_phone(self, phone_e164):
        return next((u for u in self.users.values() if u.phone_e164 == phone_e164), None)

    async def create_user(self, phone_e164, roles, full_name=None):
        u = User(id=str(uuid4()), phone_e164=phone_e164, roles=roles, full_name=full_name)
        self.users[u.id] = u
        return u

    async def add_role(self, user_id, role):
        u = self.users[user_id]
        if role not in u.roles:
            u.roles.append(role)
        return u

    async def get_otp(self, phone_e164):
        return self._otps.get(phone_e164)

    async def put_otp(self, rec):
        self._otps[rec.phone_e164] = rec.model_copy()

    async def delete_otp(self, phone_e164):
        self._otps.pop(phone_e164, None)

    async def put_refresh(self, rec):
        self._refresh[rec.token_hash] = rec.model_copy()

    async def get_refresh(self, token_hash):
        r = self._refresh.get(token_hash)
        return r.model_copy() if r else None

    async def revoke_refresh(self, token_hash):
        if token_hash in self._refresh:
            self._refresh[token_hash].revoked_at = datetime.now().astimezone()

    async def revoke_refresh_family(self, family_id):
        for r in self._refresh.values():
            if r.family_id == family_id and r.revoked_at is None:
                r.revoked_at = datetime.now().astimezone()

    # ---- profiles & notifications --------------------------------------- #
    async def get_profile(self, user_id):
        p = self.profiles.get(user_id)
        return p.model_copy(deep=True) if p else None

    async def save_profile(self, profile):
        self.profiles[profile.user_id] = profile.model_copy(deep=True)

    async def list_profiles(self):
        return [p.model_copy(deep=True) for p in self.profiles.values()]

    async def add_notification(self, n):
        if n.dedup_key in self._dedup:
            return False
        self._dedup.add(n.dedup_key)
        self.notifications.append(n)
        return True

    async def list_notifications(self, user_id):
        return [n for n in self.notifications if user_id is None or n.user_id == user_id]

    # ---- scheduling ----------------------------------------------------- #
    async def busy_slots(self, host_id):
        return {b.slot.start for b in self.bookings if b.host_id == host_id and b.status == "confirmed"}

    async def create_booking(self, booking):
        for b in self.bookings:
            if (b.host_id == booking.host_id and b.status == "confirmed"
                    and b.slot.start < booking.slot.end and booking.slot.start < b.slot.end):
                raise SlotTaken(booking.slot.slot_id)
        self.bookings.append(booking)
        return booking

    # ---- sessions ------------------------------------------------------- #
    async def get_session(self, session_id):
        s = self.sessions.get(session_id)
        return s.model_copy(deep=True) if s else None

    async def find_session(self, channel, external_thread):
        matches_ = [s for s in self.sessions.values() if s.channel == channel and s.external_thread == external_thread]
        return matches_[-1].model_copy(deep=True) if matches_ else None

    async def save_session(self, session, new_messages):
        self.sessions[session.id] = session.model_copy(deep=True)

    # ---- events --------------------------------------------------------- #
    async def append_event(self, event):
        self.events.append(event)
        self.events = self.events[-1000:]

    async def recent_events(self, limit):
        return self.events[-limit:]
