import shutil
import subprocess
from datetime import date

import pytest

from app.core.llm import FALLBACK_BETA
from app.ingestion.media import MediaItem, normalise
from app.ingestion.pipeline import ingest
from app.ingestion.verification import rera_format_ok, run_checks
from app.schemas import Address, DocumentExtraction, ListingExtraction

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")

LLM_LISTING = {
    "transaction_type": "sale", "property_type": "apartment", "bhk": 3, "bathrooms": 3, "balconies": 2,
    "carpet_area_sqft": 1450, "built_up_area_sqft": None, "super_built_up_area_sqft": 1890,
    "floor_number": 9, "total_floors": 18, "facing": "E", "furnishing": "semi_furnished",
    "price_inr": 1_420_000_000,  # model slipped a unit (142 Cr); text says 1.42 Cr
    "maintenance_monthly_inr": 4200, "possession_status": "ready_to_move", "possession_date": None,
    "age_years": 3, "parking_slots": 1, "amenities": ["gym", "swimming pool"],
    "address": {"project_name": "Prestige Lakeside Habitat", "locality": "Whitefield", "city": "Bengaluru",
                "state": "Karnataka", "pincode": "560066", "landmark": None},
    "rera_number": None, "natural_light_score": 9, "highlights": ["lake view"],
    "seo_title": "3 BHK East-Facing Apartment in Whitefield, Bengaluru", "seo_description": "...",
    "seo_keywords": ["whitefield 3bhk"],
    "image_insights": [{"index": 0, "room_type": "living_room", "caption": "bright living room",
                        "quality_issues": [], "is_cover_candidate": True}],
    "field_confidence": [{"field": "price_inr", "confidence": 0.9, "source": "transcript"}],
    "missing_fields": [], "clarifying_questions": [],
}


async def test_llm_ingestion_request_shape_and_reconciliation(fake_llm, container):
    llm, fake = fake_llm(LLM_LISTING)
    owner = await container.db.create_user("+919811111111", ["owner"])
    r = await ingest(
        files=[MediaItem("living.png", "image/png", PNG_1PX, "image")], documents=[],
        transcript="teen BHK hai Whitefield mein, price 1.42 Cr negotiable", notes=None,
        owner_id=owner.id, owner_role="owner", llm=llm, store=container.store, bus=container.bus,
        storage=container.storage)

    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema"
    kinds = [b["type"] for b in call["messages"][0]["content"]]
    assert "image" in kinds and kinds[-1] == "text"

    assert r.used_llm
    assert r.listing.data.price_inr == 14_200_000  # unit slip corrected from transcript
    assert any("price conflict" in w for w in r.warnings)
    assert r.listing.status == "live"
    assert (await container.db.recent_events(1))[0].type == "listing.published.v1"
    stored = await container.store.get(r.listing.id)
    assert stored.data.price_inr == 14_200_000 and stored.media[0].startswith("public/")
    assert await container.storage.get(stored.media[0]) == PNG_1PX


async def test_heuristic_ingestion_drafts_when_critical_fields_missing(container):
    owner = await container.db.create_user("+919811111112", ["owner"])
    r = await ingest(files=[], documents=[], transcript=None, notes="3 bhk in Baner, east facing",
                     owner_id=owner.id, owner_role="owner", llm=container.llm, store=container.store, bus=container.bus)
    assert r.listing.status == "draft"
    assert {"price_inr", "area"} <= set(r.listing.data.missing_fields)
    assert r.listing.data.clarifying_questions


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
async def test_video_keyframes(tmp_path):
    out = tmp_path / "walk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
                    "-pix_fmt", "yuv420p", str(out)], check=True)
    media = await normalise([MediaItem("walk.mp4", "video/mp4", out.read_bytes(), "video")], keyframes=3)
    assert len(media.images) == 3
    assert all(i.media_type == "image/jpeg" and i.derived_from == "walk.mp4" for i in media.images)


# --------------------------------------------------------------------------- #
def _doc(**kw) -> DocumentExtraction:
    base = dict(doc_type="rera_certificate", rera_number="PRM/KA/RERA/1251/446/PR/171031/000001",
                valid_until="2030-12-31", owner_or_promoter_names=["Prestige Estates"],
                signatures_or_seals_present=True, legibility=0.95, summary="RERA registration.", area_sqft=1450)
    base.update(kw)
    return DocumentExtraction(**base)


def _listing(**kw) -> ListingExtraction:
    base = dict(transaction_type="sale", property_type="apartment", bhk=3, carpet_area_sqft=1450,
                rera_number="PRM/KA/RERA/1251/446/PR/171031/000001", address=Address(city="Bengaluru"),
                seo_title="t", seo_description="d")
    base.update(kw)
    return ListingExtraction(**base)


def test_rera_formats():
    assert rera_format_ok("P52100012345")
    assert rera_format_ok("PRM/KA/RERA/1251/446/PR/171031/000001")
    assert not rera_format_ok("12345")


def test_verification_badge_only_when_rules_pass():
    ok = run_checks([_doc(), _doc(doc_type="title_deed", rera_number=None)], _listing(), None, date(2026, 1, 1))
    assert ok.status == "verified" and ok.badge


def test_verification_fails_on_tampering_and_flags_mismatch():
    bad = run_checks([_doc(tampering_signals=["overwritten digits in area"])], _listing(), None, date(2026, 1, 1))
    assert bad.status == "failed" and bad.badge is None
    mismatch = run_checks([_doc(rera_number="P52100012345")], _listing(), None, date(2026, 1, 1))
    assert mismatch.status == "needs_review"
    assert not next(c for c in mismatch.checks if c.name == "rera_matches_listing").passed


def test_verification_expired():
    r = run_checks([_doc(valid_until="2024-01-01")], _listing(), None, date(2026, 1, 1))
    assert r.status == "needs_review"
