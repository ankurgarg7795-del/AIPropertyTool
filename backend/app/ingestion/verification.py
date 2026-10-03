"""AI legal-document verification -> "AI-Verified Legal Status" badge.

Two stages:
1. Per-document OCR+LLM extraction (Claude reads scanned PDFs/images natively).
2. Deterministic rule checks across documents and the listing. The badge is
   only issued by rules, never by the model's say-so, so a prompt-injected
   document cannot award itself a badge.

Registry cross-checks (state RERA portals, land records) sit behind
``RegistryClient``; the MVP ships a no-op client.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date
from typing import Protocol

from app.core.llm import LLM, LLMError, image_block, pdf_block, text_block
from app.ingestion.media import MediaItem
from app.prompts import DOCUMENT_VERIFICATION_SYSTEM
from app.schemas import DocumentExtraction, ListingExtraction, VerificationCheck, VerificationResult

# Common state RERA registration formats (non-exhaustive).
RERA_PATTERNS = {
    "Maharashtra": r"^P5\d{10}$|^P\d{11}$",
    "Karnataka": r"^PRM/KA/RERA/\d{4}/\d{3}/P[RL]/\d{6}/\d{6}$",
    "Telangana": r"^P\d{8}\d*$",
    "Haryana": r"^(RC/REP/HARERA/GGM/\d+/\d+/\d{4}/\d+|HRERA-[A-Z]+-\d+-\d{4})$",
    "Uttar Pradesh": r"^UPRERAPRJ\d+$",
    "Tamil Nadu": r"^TN/\d+/Building/\d+/\d{4}$",
    "Gujarat": r"^PR/GJ/[A-Z]+/[A-Z ]+/[A-Z]+/[A-Z0-9]+/\d+$",
}
CORE_TITLE_DOCS = {"title_deed", "sale_deed", "occupancy_certificate", "rera_certificate", "allotment_letter"}


class RegistryClient(Protocol):
    async def rera_lookup(self, rera_number: str) -> dict | None: ...


class NullRegistry:
    async def rera_lookup(self, rera_number: str) -> dict | None:
        return None


def rera_format_ok(rera: str) -> bool:
    rera = rera.strip().upper()
    return any(re.match(p, rera) for p in RERA_PATTERNS.values())


def _parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


async def extract_document(llm: LLM, doc: MediaItem) -> DocumentExtraction:
    block = pdf_block(doc.data, doc.filename) if doc.kind == "pdf" else image_block(doc.data, doc.media_type)
    return await llm.extract(
        system=DOCUMENT_VERIFICATION_SYSTEM,
        content=[block, text_block(f"File name: {doc.filename}. Extract the fields.")],
        schema=DocumentExtraction,
        effort="high",
    )


def run_checks(docs: list[DocumentExtraction], listing: ListingExtraction, registry_hit: dict | None,
               today: date | None = None) -> VerificationResult:
    today = today or date.today()
    checks: list[VerificationCheck] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(VerificationCheck(name=name, passed=passed, detail=detail))

    types = {d.doc_type for d in docs}
    add("core_document_present", bool(types & CORE_TITLE_DOCS),
        f"found: {', '.join(sorted(types)) or 'none'}")

    rera_docs = [d for d in docs if d.rera_number]
    listing_rera = (listing.rera_number or "").strip().upper()
    if listing.possession_status == "under_construction" or rera_docs or listing_rera:
        doc_rera = (rera_docs[0].rera_number or "").strip().upper() if rera_docs else ""
        add("rera_format_valid", bool(doc_rera) and rera_format_ok(doc_rera), doc_rera or "no RERA number on documents")
        add("rera_matches_listing", not listing_rera or listing_rera == doc_rera,
            f"listing={listing_rera or '-'} document={doc_rera or '-'}")
        if registry_hit is not None:
            add("rera_registry_match", registry_hit.get("status") == "registered", str(registry_hit.get("status")))

    expired = [d for d in docs if (_parse_date(d.valid_until) or date.max) < today]
    add("not_expired", not expired, ", ".join(f"{d.doc_type} expired {d.valid_until}" for d in expired) or "ok")

    add("owners_identified", any(d.owner_or_promoter_names for d in docs),
        "; ".join(n for d in docs for n in d.owner_or_promoter_names) or "no owner/promoter names found")

    tamper = [s for d in docs for s in d.tampering_signals]
    add("no_tampering_signals", not tamper, "; ".join(tamper) or "none detected")

    low_legibility = [d.doc_type for d in docs if d.legibility < 0.6]
    add("legible", not low_legibility, f"low legibility: {low_legibility}" if low_legibility else "ok")

    enc = [e for d in docs for e in d.encumbrances_mentioned]
    add("no_encumbrances", not enc, "; ".join(enc) or "none mentioned")

    seals = all(d.signatures_or_seals_present for d in docs) if docs else False
    add("signed_or_sealed", seals, "all documents carry signatures/seals" if seals else "missing on at least one")

    listing_area = listing.carpet_area_sqft or listing.built_up_area_sqft or listing.super_built_up_area_sqft
    doc_areas = [d.area_sqft for d in docs if d.area_sqft]
    if listing_area and doc_areas:
        # Documents may quote carpet while the listing quotes super built-up (loading ~25-35%).
        ok = any(0.65 <= listing_area / a <= 1.45 for a in doc_areas)
        add("area_consistent", ok, f"listing {listing_area:.0f} vs documents {[round(a) for a in doc_areas]}")

    critical = {"core_document_present", "no_tampering_signals", "rera_matches_listing", "rera_format_valid",
                "not_expired", "rera_registry_match"}
    critical_fail = any(c.name in critical and not c.passed for c in checks)
    score = round(sum(c.passed for c in checks) / len(checks), 3) if checks else 0.0
    if not docs:
        status = "failed"
    elif critical_fail:
        status = "failed" if any(c.name == "no_tampering_signals" and not c.passed for c in checks) else "needs_review"
    elif score >= 0.85:
        status = "verified"
    else:
        status = "needs_review"
    summary = " ".join(d.summary for d in docs)[:1500] or "No documents were provided."
    return VerificationResult(
        status=status,
        badge="100% AI-Verified Legal Status" if status == "verified" else None,
        score=score, documents=docs, checks=checks, buyer_summary=summary,
    )


async def verify(llm: LLM, docs: list[MediaItem], listing: ListingExtraction,
                 registry: RegistryClient | None = None) -> VerificationResult:
    registry = registry or NullRegistry()
    if not docs:
        return run_checks([], listing, None)
    if not llm.enabled:
        return VerificationResult(
            status="needs_review", score=0.0, documents=[],
            checks=[VerificationCheck(name="llm_available", passed=False,
                                      detail="Document AI disabled (no ANTHROPIC_API_KEY); queued for manual review")],
            buyer_summary="Documents received; verification pending.")
    results = await asyncio.gather(*(extract_document(llm, d) for d in docs), return_exceptions=True)
    extracted = [r for r in results if isinstance(r, DocumentExtraction)]
    errors = [str(r) for r in results if isinstance(r, (LLMError, Exception)) and not isinstance(r, DocumentExtraction)]
    rera = next((d.rera_number for d in extracted if d.rera_number), None)
    hit = await registry.rera_lookup(rera) if rera else None
    result = run_checks(extracted, listing, hit)
    if errors:
        result.checks.append(VerificationCheck(name="all_documents_read", passed=False, detail="; ".join(errors)[:500]))
        if result.status == "verified":
            result.status, result.badge = "needs_review", None
    return result
