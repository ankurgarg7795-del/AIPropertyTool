"""HTTP API v1. Contracts are documented in docs/API.md."""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from app.auth.deps import current_user, optional_user, require_role
from app.concierge.finance import Affordability, assess
from app.container import Container, get_container
from app.core.events import whatsapp_template_payload
from app.ingestion.media import MediaItem, classify
from app.ingestion.pipeline import ingest
from app.schemas import (
    ChatRequest, ChatResponse, Event, Listing, PricePoint, SearchRequest, SearchResponse, User,
)
from app.search import service as search_service

router = APIRouter(prefix="/api/v1")
PUBLIC_MEDIA_KEY = re.compile(r"public/[0-9a-f]{2}/[0-9a-f]{64}(\.[a-z0-9]{1,8})?")


async def _read(uploads: list[UploadFile], max_bytes: int) -> list[MediaItem]:
    items = []
    for u in uploads:
        data = await u.read()
        if len(data) > max_bytes:
            raise HTTPException(413, f"{u.filename} exceeds {max_bytes // (1024 * 1024)} MB")
        kind, mt = classify(u.filename or "upload", u.content_type)
        items.append(MediaItem(filename=u.filename or "upload", media_type=mt, data=data, kind=kind))
    return items


def _can_manage(user: User | None, listing: Listing) -> bool:
    return bool(user and (user.id == listing.owner_id or "admin" in user.roles))


class UploadListingResponse(BaseModel):
    listing: Listing
    status: str
    ai_used: bool
    clarifying_questions: list[str]
    warnings: list[str]
    timings_ms: dict[str, int]


@router.post("/ai/upload-listing", response_model=UploadListingResponse, status_code=201)
async def upload_listing(
    files: list[UploadFile] = File(default=[], description="photos, walkthrough videos, floor-plan PDFs, voice notes"),
    documents: list[UploadFile] = File(default=[], description="legal docs: RERA, title deed, OC, tax receipt"),
    transcript: str | None = Form(default=None, description="client-side speech-to-text, if already available"),
    notes: str | None = Form(default=None, max_length=5000),
    owner_role: str = Form(default="owner", pattern="^(owner|agent|developer)$"),
    auto_publish: bool = Form(default=True),
    user: User = Depends(current_user),
    c: Container = Depends(get_container),
):
    if owner_role not in user.roles:
        if owner_role != "owner":
            raise HTTPException(403, f"Listing as '{owner_role}' needs a verified {owner_role} account")
        user = await c.db.add_role(user.id, "owner")  # any signed-in user may list their own property
    max_bytes = c.settings.max_upload_mb * 1024 * 1024
    media, docs = await _read(files, max_bytes), await _read(documents, max_bytes)
    if not media and not docs and not (transcript or notes):
        raise HTTPException(422, "Provide at least one file, a transcript or notes")
    try:
        r = await ingest(files=media, documents=docs, transcript=transcript, notes=notes, owner_id=user.id,
                         owner_role=owner_role, llm=c.llm, store=c.store, bus=c.bus, storage=c.storage,
                         keyframes=c.settings.video_keyframes, auto_publish=auto_publish)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return UploadListingResponse(listing=r.listing, status=r.listing.status, ai_used=r.used_llm,
                                 clarifying_questions=r.listing.data.clarifying_questions,
                                 warnings=r.warnings, timings_ms=r.timings_ms)


@router.post("/ai/search", response_model=SearchResponse)
async def ai_search(req: SearchRequest, user: User | None = Depends(optional_user),
                    c: Container = Depends(get_container)):
    return await search_service.search(req, c.store, c.llm, c.bus, user_id=user.id if user else None)


@router.post("/ai/agent-chat", response_model=ChatResponse)
async def agent_chat(req: ChatRequest, user: User | None = Depends(optional_user),
                     c: Container = Depends(get_container)):
    if req.listing_id:
        lst = await c.store.get(req.listing_id)
        if not lst or (lst.status != "live" and not _can_manage(user, lst)):
            raise HTTPException(404, "listing not found")
    return await c.concierge.handle(req, user=user)


class AffordabilityRequest(BaseModel):
    monthly_income_inr: float = Field(gt=0)
    existing_emi_inr: float = Field(default=0, ge=0)
    down_payment_inr: float | None = Field(default=None, ge=0)
    target_price_inr: float | None = Field(default=None, gt=0)


@router.post("/ai/affordability", response_model=Affordability)
async def affordability(req: AffordabilityRequest):
    return assess(req.monthly_income_inr, req.existing_emi_inr, req.down_payment_inr, req.target_price_inr)


@router.get("/listings/{listing_id}", response_model=Listing)
async def get_listing(listing_id: str, user: User | None = Depends(optional_user),
                      c: Container = Depends(get_container)):
    lst = await c.store.get(listing_id)
    if not lst or (lst.status != "live" and not _can_manage(user, lst)):
        raise HTTPException(404, "listing not found")
    if user and user.id != lst.owner_id:
        await c.matcher.record_view(user.id, lst.id)
    return lst


class PriceUpdate(BaseModel):
    price_inr: float = Field(gt=0)


@router.patch("/listings/{listing_id}/price", response_model=Listing)
async def update_price(listing_id: str, body: PriceUpdate, user: User = Depends(current_user),
                       c: Container = Depends(get_container)):
    lst = await c.store.get(listing_id)
    if not lst:
        raise HTTPException(404, "listing not found")
    if not _can_manage(user, lst):
        raise HTTPException(403, "Only the listing owner can change its price")
    old = lst.data.price_inr
    lst.data.price_inr = body.price_inr
    lst.price_history.append(PricePoint(price_inr=body.price_inr))
    lst.updated_at = datetime.now(timezone.utc)
    await c.store.upsert(lst)
    if old and old != body.price_inr and lst.status == "live":
        await c.bus.publish(Event(type="listing.price_changed.v1", source="/svc/listing", subject=lst.id,
                                  partitionkey=lst.id,
                                  data={"listing": lst.model_dump(mode="json"), "old_price_inr": old,
                                        "new_price_inr": body.price_inr}))
    return lst


@router.get("/notifications")
async def notifications(user_id: str | None = None, user: User = Depends(current_user),
                        c: Container = Depends(get_container)):
    """Your notifications. Admins may pass ``user_id`` (or omit it for everyone's)."""
    target = user.id
    if "admin" in user.roles:
        target = user_id
    elif user_id and user_id != user.id:
        raise HTTPException(403, "Not allowed")
    out = []
    for n in await c.db.list_notifications(target):
        item = n.model_dump(mode="json")
        if n.channel == "whatsapp":
            owner = await c.db.get_user(n.user_id)
            if owner and owner.phone_e164:
                item["provider_payload"] = whatsapp_template_payload(n, owner.phone_e164)
        out.append(item)
    return out


@router.get("/events")
async def events(limit: int = Query(default=50, ge=1, le=500), _: User = Depends(require_role("admin")),
                 c: Container = Depends(get_container)):
    return [e.model_dump(mode="json") for e in await c.db.recent_events(limit)]


@router.get("/media/{key:path}")
async def media(key: str, c: Container = Depends(get_container)):
    """Public listing media. Private (legal) documents are never served here."""
    if not PUBLIC_MEDIA_KEY.fullmatch(key):  # exact shape: no "..", no other prefixes
        raise HTTPException(404, "not found")
    try:
        data = await c.storage.get(key)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(404, "not found") from e
    return Response(data, media_type=mimetypes.guess_type(key)[0] or "application/octet-stream",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# --------------------------------------------------------------------------- #
# WhatsApp Cloud API webhook -> concierge
# --------------------------------------------------------------------------- #
@router.get("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_verify(request: Request):
    q = request.query_params
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if expected and q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), expected):
        return q.get("hub.challenge", "")
    raise HTTPException(403, "verification failed")


def _check_signature(raw: bytes, header: str | None, c: Container) -> None:
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    if not secret:
        if c.settings.env == "prod":
            raise HTTPException(503, "WHATSAPP_APP_SECRET not configured")
        return  # dev/test: unsigned payloads accepted
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not header or not hmac.compare_digest(header, expected):
        raise HTTPException(401, "bad signature")


@router.post("/webhooks/whatsapp")
async def whatsapp_inbound(request: Request, c: Container = Depends(get_container)):
    """Inbound text messages are routed to the concierge; the reply is returned as a
    Cloud API send-message body (an outbound worker POSTs it to the Graph API)."""
    raw = await request.body()
    _check_signature(raw, request.headers.get("x-hub-signature-256"), c)
    payload = await request.json()
    replies = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("type") != "text":
                    continue
                wa_id = msg["from"]
                phone = "+" + wa_id
                # The sender's number is verified by WhatsApp, so it identifies the user.
                user = await c.db.get_user_by_phone(phone) or await c.db.create_user(phone, ["buyer"])
                session = await c.db.find_session("whatsapp", wa_id)
                ref = (msg.get("referral") or {}).get("ref")
                if ref and not await c.store.get(ref):
                    ref = None
                resp = await c.concierge.handle(
                    ChatRequest(message=msg["text"]["body"], channel="whatsapp", listing_id=ref),
                    user=user, external_thread=wa_id, session=session)
                replies.append({"messaging_product": "whatsapp", "to": wa_id, "type": "text",
                                "text": {"body": resp.reply}, "session_id": resp.session_id})
    return {"outbound": replies}
