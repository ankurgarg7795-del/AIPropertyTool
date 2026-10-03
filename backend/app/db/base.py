"""Storage contract.

Every service talks to ``Database``; ``MemoryDatabase`` backs tests and
zero-setup local runs, ``PostgresDatabase`` backs production
(``DATABASE_URL``). Both pass the same test-suite.
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import NamedTuple

from pydantic import BaseModel

from app.schemas import (
    Booking, BuyerProfile, ConciergeSession, Event, Listing, Notification, SearchFilters, User,
)


class SlotTaken(Exception):
    """The host already has an overlapping confirmed appointment."""


class Candidate(NamedTuple):
    listing: Listing
    semantic: float  # cosine similarity, 0..1
    keyword: float  # backend-specific raw keyword score (normalised by the ranker)


class OtpRecord(BaseModel):
    phone_e164: str
    code_hash: str
    expires_at: datetime
    attempts: int = 0
    sent_count: int = 1
    window_start: datetime


class RefreshRecord(BaseModel):
    token_hash: str
    user_id: str
    family_id: str
    expires_at: datetime
    revoked_at: datetime | None = None


class Database(abc.ABC):
    # ---- listings ------------------------------------------------------- #
    @abc.abstractmethod
    async def upsert_listing(self, listing: Listing, embedding: list[float], search_tokens: str,
                             tags: list[str]) -> None: ...

    @abc.abstractmethod
    async def get_listing(self, listing_id: str) -> Listing | None: ...

    @abc.abstractmethod
    async def listing_candidates(self, filters: SearchFilters, query_vec: list[float], query_tokens: list[str],
                                 recall: int) -> tuple[list[Candidate], int]:
        """Hard-filtered candidates with semantic + keyword scores, and the total filtered count."""

    @abc.abstractmethod
    async def count_listings(self) -> int: ...

    # ---- users & auth --------------------------------------------------- #
    @abc.abstractmethod
    async def get_user(self, user_id: str) -> User | None: ...

    @abc.abstractmethod
    async def get_user_by_phone(self, phone_e164: str) -> User | None: ...

    @abc.abstractmethod
    async def create_user(self, phone_e164: str | None, roles: list[str], full_name: str | None = None) -> User: ...

    @abc.abstractmethod
    async def add_role(self, user_id: str, role: str) -> User: ...

    @abc.abstractmethod
    async def get_otp(self, phone_e164: str) -> OtpRecord | None: ...

    @abc.abstractmethod
    async def put_otp(self, rec: OtpRecord) -> None: ...

    @abc.abstractmethod
    async def delete_otp(self, phone_e164: str) -> None: ...

    @abc.abstractmethod
    async def put_refresh(self, rec: RefreshRecord) -> None: ...

    @abc.abstractmethod
    async def get_refresh(self, token_hash: str) -> RefreshRecord | None: ...

    @abc.abstractmethod
    async def revoke_refresh(self, token_hash: str) -> None: ...

    @abc.abstractmethod
    async def revoke_refresh_family(self, family_id: str) -> None: ...

    # ---- buyer profiles & notifications --------------------------------- #
    @abc.abstractmethod
    async def get_profile(self, user_id: str) -> BuyerProfile | None: ...

    @abc.abstractmethod
    async def save_profile(self, profile: BuyerProfile) -> None: ...

    @abc.abstractmethod
    async def list_profiles(self) -> list[BuyerProfile]: ...

    @abc.abstractmethod
    async def add_notification(self, n: Notification) -> bool:
        """Insert unless ``dedup_key`` exists. Returns True if inserted."""

    @abc.abstractmethod
    async def list_notifications(self, user_id: str | None) -> list[Notification]: ...

    # ---- scheduling ----------------------------------------------------- #
    @abc.abstractmethod
    async def busy_slots(self, host_id: str) -> set[datetime]: ...

    @abc.abstractmethod
    async def create_booking(self, booking: Booking) -> Booking:
        """Raises ``SlotTaken`` on overlap with a confirmed booking of the same host."""

    # ---- concierge sessions --------------------------------------------- #
    @abc.abstractmethod
    async def get_session(self, session_id: str) -> ConciergeSession | None: ...

    @abc.abstractmethod
    async def find_session(self, channel: str, external_thread: str) -> ConciergeSession | None: ...

    @abc.abstractmethod
    async def save_session(self, session: ConciergeSession, new_messages: list[dict[str, str]]) -> None: ...

    # ---- events --------------------------------------------------------- #
    @abc.abstractmethod
    async def append_event(self, event: Event) -> None: ...

    @abc.abstractmethod
    async def recent_events(self, limit: int) -> list[Event]: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None
