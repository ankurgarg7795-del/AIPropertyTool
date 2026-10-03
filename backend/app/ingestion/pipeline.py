"""Multimodal listing ingestion.

    upload -> classify -> normalise (keyframes, STT) -> image prep
           -> Claude structured extraction (or heuristic fallback)
           -> deterministic validation & reconciliation
           -> legal document verification
           -> quality score + publish decision
           -> index (vector + keyword) -> emit listing.published.v1
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass, field

from app.core.events import Event, EventBus
from app.core.heuristics import parse_listing_text
from app.core.llm import LLM, LLMError, image_block, pdf_block, text_block
from app.ingestion import verification
from app.ingestion.media import MediaItem, NormalisedMedia, normalise
from app.prompts import LISTING_EXTRACTION_SYSTEM
from app.schemas import Listing, ListingExtraction, PricePoint, VerificationResult
from app.search.store import ListingStore

log = logging.getLogger(__name__)

MAX_IMAGES = 20  # keep request size and cost bounded; extra photos are stored but not analysed
MAX_IMAGE_BYTES = 5 * 1024 * 1024
LONG_EDGE = 1568


@dataclass
class IngestionResult:
    listing: Listing
    used_llm: bool
    warnings: list[str] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)


def prepare_image(item: MediaItem) -> MediaItem | None:
    """Downscale to Claude's sweet spot (long edge 1568px). Auto-enhancement for the
    public gallery (exposure, perspective, de-watermark) runs async in the media
    worker and is not on the extraction path."""
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return item if len(item.data) <= MAX_IMAGE_BYTES else None
    img = Image.open(io.BytesIO(item.data))
    if max(img.size) <= LONG_EDGE and len(item.data) <= MAX_IMAGE_BYTES:
        return item
    img.thumbnail((LONG_EDGE, LONG_EDGE))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return MediaItem(filename=item.filename, media_type="image/jpeg", data=buf.getvalue(), kind="image",
                     derived_from=item.derived_from, label=item.label)


def build_content(media: NormalisedMedia) -> list[dict]:
    content: list[dict] = []
    for i, img in enumerate(media.images):
        content.append(text_block(f"[image {i}] {img.label or img.filename}"))
        content.append(image_block(img.data, img.media_type))
    for p in media.pdfs:
        content.append(pdf_block(p.data, p.filename))
    if media.transcripts:
        content.append(text_block("VOICE / VIDEO TRANSCRIPTS:\n" + "\n---\n".join(
            f"({src}) {txt}" for src, txt in media.transcripts)))
    if media.notes:
        content.append(text_block("SELLER NOTES:\n" + "\n---\n".join(media.notes)))
    content.append(text_block(
        f"{len(media.images)} images, {len(media.pdfs)} PDFs, {len(media.transcripts)} transcripts supplied. "
        "Produce the listing."))
    return content


def reconcile(x: ListingExtraction, text_evidence: str) -> tuple[ListingExtraction, list[str]]:
    """Deterministic guards on top of the model output."""
    warnings: list[str] = []
    h = parse_listing_text(text_evidence) if text_evidence.strip() else None

    # Price unit slips (lakh vs crore) are the most expensive extraction error.
    if h and h.price_inr and x.price_inr and not (0.5 <= x.price_inr / h.price_inr <= 2):
        warnings.append(f"price conflict: model={x.price_inr:.0f} text={h.price_inr:.0f}; using text value")
        x.price_inr = h.price_inr
    if x.transaction_type == "sale" and x.price_inr and x.price_inr < 200_000:
        warnings.append("sale price < ₹2 L looks like a unit error; cleared")
        x.price_inr = None

    c, b, s = x.carpet_area_sqft, x.built_up_area_sqft, x.super_built_up_area_sqft
    if c and b and c > b:
        warnings.append("carpet > built-up; swapped")
        x.carpet_area_sqft, x.built_up_area_sqft = b, c
    if b and s and b > s:
        warnings.append("built-up > super built-up; swapped")
        x.built_up_area_sqft, x.super_built_up_area_sqft = s, b
    area = x.carpet_area_sqft or x.built_up_area_sqft or x.super_built_up_area_sqft
    if area and x.bhk and area / x.bhk < 150:
        warnings.append(f"{area:.0f} sq ft for {x.bhk} BHK is implausible; check units")
    if x.floor_number is not None and x.total_floors is not None and x.floor_number > x.total_floors:
        warnings.append("floor_number > total_floors; total_floors cleared")
        x.total_floors = None

    # Model-reported gaps stay advisory; critical gaps are recomputed from the final values.
    x.missing_fields = sorted(set(critical_missing(x)) | {
        f for f in x.missing_fields if getattr(x, f, None) is None and not f.endswith("area_sqft")})
    return x, warnings


def critical_missing(x: ListingExtraction) -> list[str]:
    area = x.carpet_area_sqft or x.built_up_area_sqft or x.super_built_up_area_sqft
    critical = {"price_inr": x.price_inr, "area": area, "city": x.address.city}
    if x.property_type not in ("plot", "commercial_office", "shop", "warehouse"):
        critical["bhk"] = x.bhk
    return [k for k, v in critical.items() if v is None]


def quality_score(x: ListingExtraction, n_images: int, ver: VerificationResult | None) -> float:
    fields = [x.price_inr, x.bhk, x.facing, x.floor_number, x.furnishing, x.possession_status,
              x.carpet_area_sqft or x.built_up_area_sqft or x.super_built_up_area_sqft,
              x.address.locality, x.address.city, x.maintenance_monthly_inr]
    completeness = sum(v is not None for v in fields) / len(fields)
    photos = min(n_images, 8) / 8
    issues = sum(len(i.quality_issues) for i in x.image_insights)
    photo_quality = max(0.0, 1 - issues / max(1, 2 * len(x.image_insights))) if x.image_insights else 0.5
    legal = {"verified": 1.0, "needs_review": 0.5}.get(ver.status, 0.0) if ver else 0.0
    return round(0.45 * completeness + 0.2 * photos + 0.15 * photo_quality + 0.2 * legal, 3)


async def ingest(
    *,
    files: list[MediaItem],
    documents: list[MediaItem],
    transcript: str | None,
    notes: str | None,
    owner_id: str,
    owner_role: str,
    llm: LLM,
    store: ListingStore,
    bus: EventBus,
    keyframes: int = 6,
    auto_publish: bool = True,
) -> IngestionResult:
    t0 = time.perf_counter()
    timings: dict[str, int] = {}

    def lap(stage: str) -> None:
        timings[stage] = int((time.perf_counter() - t0) * 1000) - sum(timings.values())

    media = await normalise(files, keyframes=keyframes, transcript_hint=transcript)
    if notes:
        media.notes.append(notes)
    warnings = list(media.warnings)
    prepared = []
    for img in media.images[:MAX_IMAGES]:
        p = prepare_image(img)
        if p:
            prepared.append(p)
        else:
            warnings.append(f"{img.filename}: too large and Pillow unavailable; skipped")
    if len(media.images) > MAX_IMAGES:
        warnings.append(f"only the first {MAX_IMAGES} images were analysed")
    media.images = prepared
    lap("normalise")

    text_evidence = "\n".join([t for _, t in media.transcripts] + media.notes)
    used_llm = False
    extraction: ListingExtraction | None = None
    if llm.enabled and (media.images or media.pdfs or text_evidence.strip()):
        try:
            extraction = await llm.extract(system=LISTING_EXTRACTION_SYSTEM, content=build_content(media),
                                           schema=ListingExtraction)
            used_llm = True
        except LLMError as e:
            warnings.append(f"AI extraction failed, used text heuristics: {e}")
    if extraction is None:
        if not text_evidence.strip():
            raise ValueError("Nothing to extract: no API key configured and no transcript/notes supplied")
        extraction = parse_listing_text(text_evidence)
        if media.images or media.pdfs:
            warnings.append("photos/PDFs stored but not analysed (AI disabled)")
    lap("extract")

    extraction, rec_warnings = reconcile(extraction, text_evidence)
    warnings += rec_warnings
    lap("reconcile")

    ver = await verification.verify(llm, documents, extraction) if documents else None
    lap("verify")

    listing = Listing(owner_id=owner_id, owner_role=owner_role, data=extraction,
                      media=[f.filename for f in files] + [d.filename for d in documents], verification=ver)
    listing.quality_score = quality_score(extraction, len(media.images), ver)
    if extraction.price_inr:
        listing.price_history.append(PricePoint(price_inr=extraction.price_inr))
    blocking = critical_missing(extraction)
    if auto_publish and not blocking and not (ver and ver.status == "failed"):
        listing.status = "live"
    else:
        listing.status = "draft" if blocking else "pending_review"
    store.upsert(listing)
    lap("index")

    if listing.status == "live":
        await bus.publish(Event(type="listing.published.v1", source="/svc/listing", subject=listing.id,
                                partitionkey=listing.id, data={"listing": listing.model_dump(mode="json")}))
    return IngestionResult(listing=listing, used_llm=used_llm, warnings=warnings, timings_ms=timings)
