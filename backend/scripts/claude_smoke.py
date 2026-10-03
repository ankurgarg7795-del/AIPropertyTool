"""Live smoke test of every Claude-backed path. Needs ANTHROPIC_API_KEY.

    cd backend && ANTHROPIC_API_KEY=... python scripts/claude_smoke.py

Checks that each structured-output schema is accepted by the API and that
the model extracts the facts we planted. Exits non-zero on any failure.
Costs a few cents per run.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.concierge.agent import Concierge  # noqa: E402
from app.concierge.scheduler import Scheduler  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.events import EventBus  # noqa: E402
from app.core.llm import LLM, LLMError  # noqa: E402
from app.db.memory import MemoryDatabase  # noqa: E402
from app.ingestion.media import MediaItem  # noqa: E402
from app.ingestion.pipeline import ingest  # noqa: E402
from app.ingestion.verification import extract_document  # noqa: E402
from app.schemas import ChatRequest  # noqa: E402
from app.search.embeddings import HashingEmbedder  # noqa: E402
from app.search.service import parse  # noqa: E402
from app.search.store import ListingIndex  # noqa: E402


def text_pdf(lines: list[str]) -> bytes:
    """Minimal single-page PDF with Helvetica text (no dependencies)."""
    stream = "BT /F1 14 Tf 60 780 Td 18 TL " + " ".join(
        "(" + ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


RERA = "PRM/KA/RERA/1251/446/PR/171031/000001"
FLOOR_PLAN = text_pdf([
    "PRESTIGE LAKESIDE HABITAT - TOWER 4 - UNIT 902", "Typical floor plan, 9th floor of 18",
    "3 BHK | 3 Toilets | 2 Balconies", "Carpet area: 1450 sq ft | Super built-up area: 1890 sq ft",
    "Main entrance faces EAST (north arrow at top)", "Whitefield, Bengaluru 560066", f"K-RERA: {RERA}",
])
CERTIFICATE = text_pdf([
    "KARNATAKA REAL ESTATE REGULATORY AUTHORITY", "FORM C - REGISTRATION CERTIFICATE OF PROJECT",
    f"Registration No.: {RERA}", "Project: Prestige Lakeside Habitat, Whitefield, Bengaluru",
    "Promoter: Prestige Estates Projects Ltd.", "Valid from 2017-10-31 to 2030-12-31",
    "Signed and sealed: Secretary, K-RERA",
])
TRANSCRIPT = ("Namaste, main apna teen BHK flat bechna chahta hoon, Prestige Lakeside Habitat, Whitefield mein. "
              "Price 1.42 crore hai, thoda negotiable. Maintenance 4200 per month. Gym, pool, clubhouse sab hai. "
              "Ready to move, semi furnished.")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail else ''}")
    if not ok:
        failures.append(name)


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set")
        return 2
    settings = Settings(anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
                        llm_model=os.environ.get("APT_LLM_MODEL", "claude-opus-5-5"),
                        llm_server_fallbacks=os.environ.get("APT_LLM_SERVER_FALLBACKS", "true") == "true")
    llm = LLM(settings)
    db = MemoryDatabase()
    store, bus = ListingIndex(db, HashingEmbedder(1024)), EventBus(db)
    print(f"model={settings.llm_model} fallbacks={settings.llm_server_fallbacks}")

    t = time.perf_counter()
    print("1. natural-language query parsing")
    parsed, used = await parse(llm, "Show me a sunlit 3BHK near tech hubs under ₹1.5 Cr with low maintenance "
                                    "and east facing")
    f = parsed.filters
    check("LLM path used (no heuristic fallback)", used)
    check("BHK = 3", f.bhk_min == 3 and f.bhk_max in (3, None), f"{f.bhk_min}-{f.bhk_max}")
    check("budget <= 1.5 Cr", f.price_max_inr == 15_000_000, str(f.price_max_inr))
    check("facing E", f.facing == ["E"], str(f.facing))
    check("soft preferences captured", len(parsed.soft_preferences) >= 2, str(parsed.soft_preferences))
    print(f"   {time.perf_counter() - t:.1f}s")

    t = time.perf_counter()
    print("2. multimodal listing extraction (floor-plan PDF + Hinglish transcript)")
    owner = await db.create_user("+919800000000", ["owner"])
    r = await ingest(files=[MediaItem("floorplan.pdf", "application/pdf", FLOOR_PLAN, "pdf")], documents=[],
                     transcript=TRANSCRIPT, notes=None, owner_id=owner.id, owner_role="owner", llm=llm,
                     store=store, bus=bus)
    x = r.listing.data
    check("LLM path used", r.used_llm, "; ".join(r.warnings))
    check("bhk 3", x.bhk == 3, str(x.bhk))
    check("carpet 1450", x.carpet_area_sqft == 1450, str(x.carpet_area_sqft))
    check("price 1.42 Cr", x.price_inr == 14_200_000, str(x.price_inr))
    check("facing E (from plan)", x.facing == "E", str(x.facing))
    check("floor 9 of 18", (x.floor_number, x.total_floors) == (9, 18), f"{x.floor_number}/{x.total_floors}")
    check("city Bengaluru", (x.address.city or "").lower() in ("bengaluru", "bangalore"), str(x.address.city))
    check("RERA verbatim", x.rera_number == RERA, str(x.rera_number))
    check("published live", r.listing.status == "live", r.listing.status)
    print(f"   title: {x.seo_title}\n   {time.perf_counter() - t:.1f}s")

    t = time.perf_counter()
    print("3. legal document reading")
    try:
        doc = await extract_document(llm, MediaItem("rera.pdf", "application/pdf", CERTIFICATE, "pdf"))
    except LLMError as e:
        check("document extraction call", False, str(e))
    else:
        check("doc_type rera_certificate", doc.doc_type == "rera_certificate", doc.doc_type)
        check("RERA number verbatim", doc.rera_number == RERA, str(doc.rera_number))
        check("valid_until 2030-12-31", doc.valid_until == "2030-12-31", str(doc.valid_until))
        check("no tampering flagged on clean PDF", not doc.tampering_signals, str(doc.tampering_signals))
    print(f"   {time.perf_counter() - t:.1f}s")

    t = time.perf_counter()
    print("4. concierge phrasing + grounded Q&A")
    concierge = Concierge(db, store, llm, bus, Scheduler(db))
    resp = await concierge.handle(ChatRequest(message="Is it east facing? What is the maintenance?",
                                              listing_id=r.listing.id))
    # The concierge falls back to its template reply if the API call fails, so require LLM-specific content.
    check("reply produced", len(resp.reply) > 20, resp.reply[:200].replace("\n", " "))
    check("grounded in facts (mentions 4,200 maintenance)", "4,200" in resp.reply or "4200" in resp.reply)
    print(f"   {time.perf_counter() - t:.1f}s")

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
