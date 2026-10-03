"""24/7 virtual concierge: buyer pre-qualification + automated appointment booking.

Control flow is a deterministic finite-state machine (auditable, testable,
no LLM can skip qualification or double-book); Claude is used only to
(a) phrase replies naturally in the user's language and (b) answer property
questions grounded in the listing facts.

States
  GREETING -> INTENT -> BUDGET -> TIMELINE -> FINANCING -> QUALIFIED
           -> SLOT_OFFERED -> BOOKED
  any state -> HANDOFF   (asks for a human, repeated non-answers, or hot lead >= 80)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.concierge.finance import assess
from app.concierge.scheduler import Booking, Scheduler, Slot
from app.core.events import Event, EventBus, fmt_inr
from app.core.heuristics import parse_money
from app.core.llm import LLM, LLMError
from app.prompts import CONCIERGE_SYSTEM
from app.schemas import ChatAction, ChatRequest, ChatResponse, Listing
from app.search.store import ListingStore


class State(str, Enum):
    GREETING = "GREETING"
    INTENT = "INTENT"
    BUDGET = "BUDGET"
    TIMELINE = "TIMELINE"
    FINANCING = "FINANCING"
    QUALIFIED = "QUALIFIED"
    SLOT_OFFERED = "SLOT_OFFERED"
    BOOKED = "BOOKED"
    HANDOFF = "HANDOFF"


class Slots(BaseModel):
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


class Session(BaseModel):
    id: str = Field(default_factory=lambda: f"ses_{uuid4().hex[:12]}")
    state: State = State.GREETING
    listing_id: str | None = None
    user_id: str | None = None
    channel: str = "web"
    slots: Slots = Field(default_factory=Slots)
    history: list[dict[str, str]] = Field(default_factory=list)
    misses: int = 0


GOALS = {
    State.INTENT: "Find out whether they want to buy, rent or invest.",
    State.BUDGET: "Ask for their maximum budget.",
    State.TIMELINE: "Ask when they plan to move in or purchase.",
    State.FINANCING: "Ask if they need a home loan; if yes, their approximate net monthly income and existing EMIs.",
    State.QUALIFIED: "Summarise fit and offer to book a site visit.",
    State.SLOT_OFFERED: "Ask them to pick one of the offered visit slots (reply 1-4).",
    State.BOOKED: "Confirm the booking and answer follow-up questions.",
    State.HANDOFF: "Tell them a human relationship manager will call shortly.",
}

HUMAN_RE = re.compile(r"\b(human|agent|call me|talk to (someone|a person)|real person|manager)\b", re.I)
QUESTION_RE = re.compile(r"\?|^(is|are|does|do|what|how|when|where|which|can|kya|kitna)\b", re.I)


# --------------------------------------------------------------------------- #
# Slot extractors (deterministic; Hinglish-aware where cheap)
# --------------------------------------------------------------------------- #
def extract_intent(t: str) -> str | None:
    t = t.lower()
    if re.search(r"\b(rent|lease|kiraye)\b", t):
        return "rent"
    if re.search(r"\b(invest|investment|rental yield|roi)\b", t):
        return "invest"
    if re.search(r"\b(buy|purchase|own|lena|kharid|self[- ]use|live in)\b", t):
        return "buy"
    return None


def extract_timeline(t: str) -> int | None:
    t = t.lower()
    if re.search(r"\b(immediate|asap|right away|this month|abhi|urgent)\b", t):
        return 0
    m = re.search(r"(\d+)\s*(month|months|mahine)", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*(year|years|saal)", t)
    if m:
        return int(m.group(1)) * 12
    if "next year" in t:
        return 12
    if re.search(r"\b(just looking|exploring|not sure)\b", t):
        return 24
    return None


def extract_yes_no(t: str) -> bool | None:
    t = t.lower().strip()
    if re.search(r"\b(no|nope|not needed|self[- ]funded|cash|full payment|nahi)\b", t):
        return False
    if re.search(r"\b(yes|yeah|yep|need (a )?loan|haan|ha|sure|required)\b", t):
        return True
    return None


def extract_income(t: str) -> tuple[float | None, float | None]:
    t = t.lower().replace(",", "")
    income = emi = None
    lpa = re.search(r"(\d+(?:\.\d+)?)\s*(lpa|lakhs? per annum|l p a|lakh(?:s)? (?:a|per) year)", t)
    if lpa:
        income = float(lpa.group(1)) * 100000 / 12 * 0.8  # rough in-hand
    else:
        m = re.search(r"(?:income|salary|earn|take home|in[- ]hand)[^\d]*(\d+(?:\.\d+)?)\s*(k|l|lakh|lakhs)?", t)
        if m:
            v = float(m.group(1))
            unit = m.group(2) or ""
            income = v * 1000 if unit == "k" else v * 100000 if unit.startswith("l") else v
    e = re.search(r"emi[^\d]*(\d+(?:\.\d+)?)\s*(k)?", t)
    if e:
        emi = float(e.group(1)) * (1000 if e.group(2) else 1)
    elif re.search(r"\bno (existing )?emi", t):
        emi = 0.0
    return income, emi


def extract_slot_choice(t: str, offered: list[Slot]) -> Slot | None:
    m = re.search(r"\b([1-9])\b", t)
    if m and 1 <= int(m.group(1)) <= len(offered):
        return offered[int(m.group(1)) - 1]
    tl = t.lower()
    for s in offered:
        if s.start.strftime("%a").lower() in tl and s.start.strftime("%I").lstrip("0") in tl:
            return s
    return None


# --------------------------------------------------------------------------- #
def lead_score(s: Slots, listing: Listing | None) -> int:
    score = 10
    if s.intent in ("buy", "invest"):
        score += 15
    elif s.intent == "rent":
        score += 8
    if s.budget_max_inr:
        score += 10
        if listing and listing.data.price_inr:
            ratio = s.budget_max_inr / listing.data.price_inr
            score += 15 if ratio >= 1 else 8 if ratio >= 0.9 else 0
    if s.timeline_months is not None:
        score += 20 if s.timeline_months <= 3 else 10 if s.timeline_months <= 6 else 3
    if s.needs_loan is False:
        score += 15
    elif s.preapproval:
        score += {"comfortable": 15, "stretch": 6}.get(s.preapproval.get("verdict", ""), 0)
    if s.booking_id:
        score += 10
    return min(score, 100)


def listing_facts(listing: Listing | None) -> str:
    if not listing:
        return "No specific listing selected."
    d = listing.data
    facts: dict[str, Any] = {
        "title": d.seo_title, "price": fmt_inr(d.price_inr), "bhk": d.bhk, "type": d.property_type,
        "carpet_area_sqft": d.carpet_area_sqft, "super_built_up_area_sqft": d.super_built_up_area_sqft,
        "floor": f"{d.floor_number}/{d.total_floors}" if d.floor_number is not None else None,
        "facing": d.facing, "furnishing": d.furnishing, "possession": d.possession_status,
        "maintenance_monthly": fmt_inr(d.maintenance_monthly_inr) if d.maintenance_monthly_inr else None,
        "amenities": ", ".join(d.amenities), "locality": d.address.locality, "city": d.address.city,
        "project": d.address.project_name, "rera": d.rera_number,
        "legal_status": listing.verification.status if listing.verification else "not verified",
        "legal_summary": listing.verification.buyer_summary if listing.verification else None,
    }
    return "\n".join(f"- {k}: {v}" for k, v in facts.items() if v not in (None, ""))


class Concierge:
    def __init__(self, store: ListingStore, llm: LLM, bus: EventBus, scheduler: Scheduler):
        self.store, self.llm, self.bus, self.scheduler = store, llm, bus, scheduler
        self.sessions: dict[str, Session] = {}

    # ---- state machine --------------------------------------------------- #
    def _advance(self, s: Session, text: str, listing: Listing | None
                 ) -> tuple[str, list[ChatAction], Booking | None]:
        """Consume the user message, fill slots, transition. Returns a template reply."""
        sl = s.slots
        actions: list[ChatAction] = []
        # Opportunistic slot filling: users often answer several questions at once.
        sl.intent = sl.intent or extract_intent(text)
        if sl.budget_max_inr is None and s.state in (State.BUDGET, State.INTENT, State.GREETING):
            sl.budget_max_inr = parse_money(text)
        if sl.timeline_months is None:
            sl.timeline_months = extract_timeline(text)
        income, emi_ = extract_income(text)
        if income:
            sl.monthly_income_inr = income
            sl.needs_loan = True if sl.needs_loan is None else sl.needs_loan
        if emi_ is not None:
            sl.existing_emi_inr = emi_
        if sl.needs_loan is None and re.search(r"\b(cash|self[- ]funded|no loan|without (a )?loan|full payment)\b",
                                               text, re.I):
            sl.needs_loan = False
        if s.state == State.FINANCING and sl.needs_loan is None:
            sl.needs_loan = extract_yes_no(text)

        if HUMAN_RE.search(text):
            s.state = State.HANDOFF
            return "Sure, connecting you to a relationship manager. They will call you within 15 minutes.", [
                ChatAction(type="handoff_human", payload={"reason": "user_request"})], None

        if s.state == State.SLOT_OFFERED:
            choice = extract_slot_choice(text, sl.offered_slots)
            if choice and listing:
                try:
                    booking = self.scheduler.book(listing.id, s.id, listing.owner_id, choice)
                except ValueError:
                    sl.offered_slots = self.scheduler.available(listing.owner_id)
                    return "That slot was just taken. Here are the next available ones.", [ChatAction(
                        type="offer_slots", payload={"slots": [x.model_dump(mode="json") for x in sl.offered_slots]})], None
                sl.booking_id = booking.booking_id
                s.state = State.BOOKED
                return (f"Booked! Site visit on {choice.label}. You'll get a WhatsApp confirmation and a reminder "
                        "2 hours before."), [ChatAction(type="booking_confirmed",
                                                        payload=booking.model_dump(mode="json"))], booking
            s.misses += 1
            return "Please reply with the slot number (1-4) that works for you.", [], None

        # Linear qualification: move to the first unfilled slot.
        prev = s.state
        if sl.intent is None:
            s.state = State.INTENT
        elif sl.budget_max_inr is None:
            s.state = State.BUDGET
        elif sl.timeline_months is None:
            s.state = State.TIMELINE
        elif sl.needs_loan is None or (sl.needs_loan and sl.monthly_income_inr is None):
            s.state = State.FINANCING
        elif s.state not in (State.BOOKED, State.HANDOFF):
            s.state = State.QUALIFIED
        s.misses = s.misses + 1 if s.state == prev and prev != State.GREETING else 0

        if sl.needs_loan and sl.monthly_income_inr and not sl.preapproval:
            a = assess(sl.monthly_income_inr, sl.existing_emi_inr or 0.0,
                       target_price=listing.data.price_inr if listing else sl.budget_max_inr)
            sl.preapproval = a.model_dump()

        if s.state == State.QUALIFIED:
            if listing:
                sl.offered_slots = self.scheduler.available(listing.owner_id)
                s.state = State.SLOT_OFFERED
                lines = "\n".join(f"{i + 1}. {x.label}" for i, x in enumerate(sl.offered_slots))
                pre = ""
                if sl.preapproval:
                    pre = (f" Indicative loan eligibility: up to {fmt_inr(sl.preapproval['max_loan_inr'])} "
                           f"(EMI ~{fmt_inr(sl.preapproval.get('emi_for_target_inr'))}/month for this home).")
                actions.append(ChatAction(type="offer_slots",
                                          payload={"slots": [x.model_dump(mode="json") for x in sl.offered_slots]}))
                return f"Great, this home fits your requirements.{pre} Pick a site-visit slot:\n{lines}", actions, None
            return "Thanks! Based on this I'll shortlist homes for you.", [
                ChatAction(type="show_listings", payload={"budget_max_inr": sl.budget_max_inr})], None

        templates = {
            State.INTENT: "Hi! Are you looking to buy, rent or invest?",
            State.BUDGET: "What's your maximum budget? (e.g. 1.2 Cr or 85 L)",
            State.TIMELINE: "When are you planning to move in or purchase?",
            State.FINANCING: ("Will you need a home loan? If yes, share your approximate monthly take-home "
                              "income and any existing EMIs so I can estimate eligibility."
                              if sl.needs_loan is None else
                              "What's your approximate monthly take-home income, and any existing EMIs?"),
        }
        return templates.get(s.state, "How can I help?"), actions, None

    async def _phrase(self, s: Session, user_text: str, template: str, listing: Listing | None,
                      is_question: bool) -> str:
        if not self.llm.enabled:
            if is_question and listing:
                return f"Here's what I know about this home:\n{listing_facts(listing)}\n\n{template}"
            return template
        state_note = (f"LISTING FACTS:\n{listing_facts(listing)}\n\nCONVERSATION STATE: {s.state.value}\n"
                      f"NEXT GOAL: {GOALS.get(s.state, '')}\n"
                      f"REQUIRED CONTENT (keep every number/date/slot exactly): {template}")
        msgs = s.history[-10:] + [{"role": "user", "content": f"{user_text}\n\n<context>\n{state_note}\n</context>"}]
        try:
            return await self.llm.reply(system=CONCIERGE_SYSTEM, messages=msgs)
        except LLMError:
            return template

    async def handle(self, req: ChatRequest) -> ChatResponse:
        s = self.sessions.get(req.session_id or "")
        if not s:
            s = Session(listing_id=req.listing_id, user_id=req.user_id, channel=req.channel)
            self.sessions[s.id] = s
        if req.listing_id:
            s.listing_id = req.listing_id
        listing = self.store.get(s.listing_id) if s.listing_id else None

        is_question = bool(QUESTION_RE.search(req.message.strip())) and s.state != State.SLOT_OFFERED
        template, actions, booking = self._advance(s, req.message, listing)
        score = lead_score(s.slots, listing)
        if s.misses >= 3 or (score >= 80 and s.state == State.BOOKED):
            reason = "stuck" if s.misses >= 3 else "hot_lead"
            if s.misses >= 3:
                s.state = State.HANDOFF
                template = "Let me get a relationship manager to help you directly; they'll call shortly."
            actions.append(ChatAction(type="handoff_human", payload={"reason": reason}))
            await self.bus.publish(Event(type="lead.routed.v1", source="/svc/concierge", subject=s.id,
                                         data={"session_id": s.id, "listing_id": s.listing_id, "reason": reason,
                                               "lead_score": score, "slots": s.slots.model_dump(mode="json")}))

        reply = await self._phrase(s, req.message, template, listing, is_question)
        s.history += [{"role": "user", "content": req.message}, {"role": "assistant", "content": reply}]

        if booking:
            b = booking
            await self.bus.publish(Event(
                type="visit.scheduled.v1", source="/svc/concierge", subject=b.booking_id, partitionkey=b.listing_id,
                data={"booking": b.model_dump(mode="json"), "lead_score": score, "user_id": s.user_id,
                      "remind_at": (b.slot.start - timedelta(hours=2)).astimezone(timezone.utc).isoformat()}))
        await self.bus.publish(Event(type="lead.updated.v1", source="/svc/concierge", subject=s.id,
                                     data={"session_id": s.id, "state": s.state.value, "lead_score": score,
                                           "listing_id": s.listing_id, "at": datetime.now(timezone.utc).isoformat()}))
        return ChatResponse(session_id=s.id, state=s.state.value, reply=reply, lead_score=score,
                            slots=s.slots.model_dump(mode="json", exclude={"offered_slots"}), actions=actions)
