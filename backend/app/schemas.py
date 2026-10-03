"""Pydantic contracts shared by the LLM layer, the pipelines and the HTTP API.

The ``*Extraction`` models double as JSON schemas for Claude structured outputs
(see ``app.core.llm.strict_json_schema``), so every field must be JSON-schema
friendly: no free-form dicts, enums as ``Literal``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

PropertyType = Literal[
    "apartment", "villa", "independent_house", "builder_floor", "penthouse",
    "studio", "plot", "commercial_office", "shop", "warehouse",
]
Facing = Literal["N", "S", "E", "W", "NE", "NW", "SE", "SW"]
Furnishing = Literal["unfurnished", "semi_furnished", "fully_furnished"]
Possession = Literal["ready_to_move", "under_construction"]
TransactionType = Literal["sale", "rent"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Listing extraction (LLM output contract)
# --------------------------------------------------------------------------- #
class Address(BaseModel):
    project_name: str | None = None
    locality: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    landmark: str | None = None


class ImageInsight(BaseModel):
    index: int = Field(description="0-based index of the image in the order supplied")
    room_type: str = Field(description="living_room, bedroom, kitchen, bathroom, balcony, exterior, floor_plan, document, other")
    caption: str
    quality_issues: list[str] = Field(description="e.g. dark, tilted, blurry, cluttered, watermark")
    is_cover_candidate: bool


class FieldConfidence(BaseModel):
    field: str
    confidence: float = Field(description="0.0-1.0")
    source: str = Field(description="image:<i> | pdf:<page> | transcript | text | inferred")


class ListingExtraction(BaseModel):
    transaction_type: TransactionType
    property_type: PropertyType
    bhk: int | None = None
    bathrooms: int | None = None
    balconies: int | None = None
    carpet_area_sqft: float | None = None
    built_up_area_sqft: float | None = None
    super_built_up_area_sqft: float | None = None
    floor_number: int | None = None
    total_floors: int | None = None
    facing: Facing | None = None
    furnishing: Furnishing | None = None
    price_inr: float | None = Field(default=None, description="Total ask in INR (monthly rent if transaction_type=rent)")
    maintenance_monthly_inr: float | None = None
    possession_status: Possession | None = None
    possession_date: str | None = Field(default=None, description="YYYY-MM if known")
    age_years: int | None = None
    parking_slots: int | None = None
    amenities: list[str] = Field(default_factory=list)
    address: Address = Field(default_factory=Address)
    rera_number: str | None = None
    natural_light_score: int | None = Field(default=None, description="0-10 from photos; null if no photos")
    highlights: list[str] = Field(default_factory=list)
    seo_title: str = Field(description="<= 70 chars, e.g. '3 BHK East-Facing Apartment in Whitefield, Bengaluru'")
    seo_description: str = Field(description="120-160 word, factual, no superlatives that are not supported by inputs")
    seo_keywords: list[str] = Field(default_factory=list)
    image_insights: list[ImageInsight] = Field(default_factory=list)
    field_confidence: list[FieldConfidence] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list, description="Max 3 short questions for missing critical fields")


# --------------------------------------------------------------------------- #
# Legal document verification (LLM output contract)
# --------------------------------------------------------------------------- #
DocType = Literal[
    "rera_certificate", "title_deed", "sale_deed", "occupancy_certificate",
    "completion_certificate", "property_tax_receipt", "encumbrance_certificate",
    "khata", "allotment_letter", "other",
]


class DocumentExtraction(BaseModel):
    doc_type: DocType
    issuing_authority: str | None = None
    document_number: str | None = None
    rera_number: str | None = None
    issue_date: str | None = Field(default=None, description="YYYY-MM-DD")
    valid_until: str | None = Field(default=None, description="YYYY-MM-DD")
    owner_or_promoter_names: list[str] = Field(default_factory=list)
    project_name: str | None = None
    property_address: str | None = None
    survey_or_plot_number: str | None = None
    area_sqft: float | None = None
    encumbrances_mentioned: list[str] = Field(default_factory=list)
    signatures_or_seals_present: bool
    tampering_signals: list[str] = Field(default_factory=list, description="font mismatches, overwritten digits, cropped seals")
    legibility: float = Field(description="0.0-1.0")
    summary: str = Field(description="2-3 sentence plain-language summary for a buyer")


class VerificationCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class VerificationResult(BaseModel):
    status: Literal["verified", "needs_review", "failed"]
    badge: str | None = None
    score: float
    documents: list[DocumentExtraction]
    checks: list[VerificationCheck]
    buyer_summary: str


# --------------------------------------------------------------------------- #
# Stored listing
# --------------------------------------------------------------------------- #
class PricePoint(BaseModel):
    price_inr: float
    at: datetime = Field(default_factory=_now)


class Listing(BaseModel):
    id: str = Field(default_factory=lambda: f"lst_{uuid4().hex[:12]}")
    owner_id: str = "anonymous"
    owner_role: Literal["owner", "agent", "developer"] = "owner"
    status: Literal["draft", "pending_review", "live", "sold", "archived"] = "draft"
    data: ListingExtraction
    lat: float | None = None
    lng: float | None = None
    media: list[str] = Field(default_factory=list)
    verification: VerificationResult | None = None
    quality_score: float = 0.0
    price_history: list[PricePoint] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def is_verified(self) -> bool:
        return bool(self.verification and self.verification.status == "verified")


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
class SearchFilters(BaseModel):
    """Hard constraints extracted from a natural-language query (LLM output contract)."""

    transaction_type: TransactionType | None = None
    property_types: list[PropertyType] = Field(default_factory=list)
    bhk_min: int | None = None
    bhk_max: int | None = None
    price_min_inr: float | None = None
    price_max_inr: float | None = None
    carpet_area_min_sqft: float | None = None
    cities: list[str] = Field(default_factory=list)
    localities: list[str] = Field(default_factory=list)
    facing: list[Facing] = Field(default_factory=list)
    furnishing: list[Furnishing] = Field(default_factory=list)
    possession_status: Possession | None = None
    max_maintenance_monthly_inr: float | None = None
    must_have_amenities: list[str] = Field(default_factory=list)
    verified_only: bool = False


class ParsedQuery(BaseModel):
    filters: SearchFilters
    soft_preferences: list[str] = Field(
        default_factory=list,
        description="Fuzzy wishes for semantic ranking: 'sunlit', 'near tech parks', 'quiet', 'low maintenance'",
    )
    semantic_query: str = Field(description="Rewritten query for embedding search, focused on soft preferences")
    clarification: str | None = Field(default=None, description="One question if the query is too ambiguous to search")


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)
    filters_override: SearchFilters | None = None


class ScoreBreakdown(BaseModel):
    semantic: float
    keyword: float
    preference: float
    freshness: float
    verified_boost: float


class SearchHit(BaseModel):
    listing_id: str
    score: float
    breakdown: ScoreBreakdown
    title: str
    price_inr: float | None
    bhk: int | None
    carpet_area_sqft: float | None
    city: str | None
    locality: str | None
    facing: str | None
    verified: bool
    why: list[str]


class SearchResponse(BaseModel):
    query: str
    parsed: ParsedQuery
    total_candidates: int
    hits: list[SearchHit]
    relaxed_filters: list[str] = Field(default_factory=list)
    llm_used: bool


# --------------------------------------------------------------------------- #
# Concierge
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)
    listing_id: str | None = None
    channel: Literal["web", "whatsapp", "app"] = "web"


class ChatAction(BaseModel):
    type: Literal["show_listings", "offer_slots", "booking_confirmed", "handoff_human", "request_document"]
    payload: dict


class ChatResponse(BaseModel):
    session_id: str
    state: str
    reply: str
    lead_score: int
    slots: dict
    actions: list[ChatAction] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Users & auth
# --------------------------------------------------------------------------- #
Role = Literal["buyer", "owner", "agent", "developer", "project_marketer", "admin"]


class User(BaseModel):
    id: str
    phone_e164: str | None = None
    full_name: str | None = None
    roles: list[Role] = Field(default_factory=lambda: ["buyer"])
    whatsapp_opt_in: bool = False
    created_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Events, buyer profiles, notifications
# --------------------------------------------------------------------------- #
class Event(BaseModel):
    """CloudEvents 1.0 compatible envelope."""

    specversion: str = "1.0"
    id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    type: str  # e.g. "listing.price_changed.v1"
    source: str  # e.g. "/svc/listing"
    subject: str | None = None  # aggregate id
    time: datetime = Field(default_factory=_now)
    partitionkey: str | None = None
    data: dict


class BuyerProfile(BaseModel):
    user_id: str
    filters: SearchFilters
    soft_preferences: list[str] = Field(default_factory=list)
    searches: int = 0
    viewed_listing_ids: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=lambda: ["push", "whatsapp"])
    phone_e164: str | None = None
    whatsapp_opt_in: bool = False
    updated_at: datetime = Field(default_factory=_now)


class Notification(BaseModel):
    id: str = Field(default_factory=lambda: f"ntf_{uuid4().hex[:12]}")
    user_id: str
    kind: str  # new_match | price_drop | visit_reminder | lead_routed
    channel: str
    dedup_key: str
    payload: dict
    scheduled_for: datetime
    status: str = "queued"


# --------------------------------------------------------------------------- #
# Scheduling & concierge sessions
# --------------------------------------------------------------------------- #
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
    buyer_id: str | None = None
    lead_score: int = 0
    status: str = "confirmed"


class ConciergeState(str, Enum):
    GREETING = "GREETING"
    INTENT = "INTENT"
    BUDGET = "BUDGET"
    TIMELINE = "TIMELINE"
    FINANCING = "FINANCING"
    QUALIFIED = "QUALIFIED"
    SLOT_OFFERED = "SLOT_OFFERED"
    BOOKED = "BOOKED"
    HANDOFF = "HANDOFF"


class ConciergeSlots(BaseModel):
    intent: str | None = None  # buy | rent | invest
    budget_max_inr: float | None = None
    timeline_months: int | None = None
    needs_loan: bool | None = None
    monthly_income_inr: float | None = None
    existing_emi_inr: float | None = None
    name: str | None = None
    phone: str | None = None
    preapproval: dict | None = None
    offered_slots: list[Slot] = Field(default_factory=list)
    booking_id: str | None = None


class ConciergeSession(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    state: ConciergeState = ConciergeState.GREETING
    listing_id: str | None = None
    user_id: str | None = None
    channel: str = "web"
    external_thread: str | None = None  # e.g. WhatsApp wa_id
    slots: ConciergeSlots = Field(default_factory=ConciergeSlots)
    history: list[dict[str, str]] = Field(default_factory=list)
    misses: int = 0
    lead_score: int = 0
