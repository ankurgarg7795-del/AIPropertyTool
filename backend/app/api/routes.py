"""HTTP API v1. Contracts are documented in docs/API.md."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.concierge.finance import Affordability, assess
from app.container import Container, get_container
from app.core.events import Event, whatsapp_template_payload
from app.ingestion.media import MediaItem, classify
from app.ingestion.pipeline import ingest
from app.schemas import ChatRequest, ChatResponse, Listing, PricePoint, SearchRequest, SearchResponse
from app.search import service as search_service

router = APIRouter(prefix="/api/v1")


async def _read(uploads: list[UploadFile], max_bytes: int) -> list[MediaItem]:
    items = []
    for u in uploads:
        data = await u.read()
        if len(data) > max_bytes:
            raise HTTPException(413, f"{u.filename} exceeds {max_bytes // (1024 * 1024)} MB")
        kind, mt = classify(u.filename or "upload", u.content_type)
        items.append(MediaItem(filename=u.filename or "upload", media_type=mt, data=data, kind=kind))
    return items


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
    owner_id: str = Form(default="anonymous"),
    owner_role: str = Form(default="owner", pattern="^(owner|agent|developer)$"),
    auto_publish: bool = Form(default=True),
    c: Container = Depends(get_container),
):
    max_bytes = c.settings.max_upload_mb * 1024 * 1024
    media, docs = await _read(files, max_bytes), await _read(documents, max_bytes)
    if not media and not docs and not (transcript or notes):
        raise HTTPException(422, "Provide at least one file, a transcript or notes")
    try:
        r = await ingest(files=media, documents=docs, transcript=transcript, notes=notes, owner_id=owner_id,
                         owner_role=owner_role, llm=c.llm, store=c.store, bus=c.bus,
                         keyframes=c.settings.video_keyframes, auto_publish=auto_publish)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return UploadListingResponse(listing=r.listing, status=r.listing.status, ai_used=r.used_llm,
                                 clarifying_questions=r.listing.data.clarifying_questions,
                                 warnings=r.warnings, timings_ms=r.timings_ms)


@router.post("/ai/search", response_model=SearchResponse)
async def ai_search(req: SearchRequest, c: Container = Depends(get_container)):
    return await search_service.search(req, c.store, c.llm, c.bus)


@router.post("/ai/agent-chat", response_model=ChatResponse)
async def agent_chat(req: ChatRequest, c: Container = Depends(get_container)):
    if req.listing_id and not c.store.get(req.listing_id):
        raise HTTPException(404, "listing not found")
    return await c.concierge.handle(req)


class AffordabilityRequest(BaseModel):
    monthly_income_inr: float = Field(gt=0)
    existing_emi_inr: float = Field(default=0, ge=0)
    down_payment_inr: float | None = Field(default=None, ge=0)
    target_price_inr: float | None = Field(default=None, gt=0)


@router.post("/ai/affordability", response_model=Affordability)
async def affordability(req: AffordabilityRequest):
    return assess(req.monthly_income_inr, req.existing_emi_inr, req.down_payment_inr, req.target_price_inr)


@router.get("/listings/{listing_id}", response_model=Listing)
async def get_listing(listing_id: str, c: Container = Depends(get_container)):
    lst = c.store.get(listing_id)
    if not lst:
        raise HTTPException(404, "listing not found")
    return lst


class PriceUpdate(BaseModel):
    price_inr: float = Field(gt=0)


@router.patch("/listings/{listing_id}/price", response_model=Listing)
async def update_price(listing_id: str, body: PriceUpdate, c: Container = Depends(get_container)):
    lst = c.store.get(listing_id)
    if not lst:
        raise HTTPException(404, "listing not found")
    old = lst.data.price_inr
    lst.data.price_inr = body.price_inr
    lst.price_history.append(PricePoint(price_inr=body.price_inr))
    lst.updated_at = datetime.now(timezone.utc)
    c.store.upsert(lst)
    if old and old != body.price_inr:
        await c.bus.publish(Event(type="listing.price_changed.v1", source="/svc/listing", subject=lst.id,
                                  partitionkey=lst.id,
                                  data={"listing": lst.model_dump(mode="json"), "old_price_inr": old,
                                        "new_price_inr": body.price_inr}))
    return lst


@router.get("/notifications")
async def notifications(user_id: str | None = None, c: Container = Depends(get_container)):
    out = []
    for n in c.notifier.outbox:
        if user_id and n.user_id != user_id:
            continue
        item = n.model_dump(mode="json")
        prof = c.matcher.profiles.get(n.user_id)
        if n.channel == "whatsapp" and prof and prof.phone_e164:
            item["provider_payload"] = whatsapp_template_payload(n, prof.phone_e164)
        out.append(item)
    return out


@router.get("/events")
async def events(limit: int = Query(default=50, le=500), c: Container = Depends(get_container)):
    return [e.model_dump(mode="json") for e in c.bus.log[-limit:]]


# --------------------------------------------------------------------------- #
# WhatsApp Cloud API webhook -> concierge
# --------------------------------------------------------------------------- #
@router.get("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_verify(request: Request):
    import os

    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == os.environ.get("WHATSAPP_VERIFY_TOKEN"):
        return q.get("hub.challenge", "")
    raise HTTPException(403, "verification failed")


@router.post("/webhooks/whatsapp")
async def whatsapp_inbound(payload: dict, c: Container = Depends(get_container)):
    """Inbound text messages are routed to the concierge; the reply is returned as
    a Cloud API send-message body (an outbound worker POSTs it to Graph API).
    Production also verifies the X-Hub-Signature-256 header against the app secret."""
    replies = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("type") != "text":
                    continue
                wa_id = msg["from"]
                session_id = f"wa:{wa_id}"
                sess = c.concierge.sessions.get(session_id)
                resp = await c.concierge.handle(ChatRequest(
                    session_id=sess.id if sess else None, message=msg["text"]["body"], channel="whatsapp",
                    user_id=f"wa_{wa_id}", listing_id=(msg.get("referral") or {}).get("ref")))
                c.concierge.sessions[session_id] = c.concierge.sessions[resp.session_id]
                replies.append({"messaging_product": "whatsapp", "to": wa_id, "type": "text",
                                "text": {"body": resp.reply}})
    return {"outbound": replies}
