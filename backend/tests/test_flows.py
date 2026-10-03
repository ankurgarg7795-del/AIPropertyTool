import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from app.concierge.finance import assess, emi
from app.core.events import NotificationEngine, whatsapp_template_payload
from app.db.memory import MemoryDatabase
from app.schemas import BuyerProfile, SearchFilters
from tests.conftest import login

SPEC_QUERY = "Show me a sunlit 3BHK near tech hubs under ₹1.5 Cr with low maintenance and east facing"
KHARADI = "2 bhk Kharadi Pune 900 sq ft carpet 3rd floor of 10, 76 lakh, east facing"


def test_spec_query_ranks_east_facing_tech_hub_home_first(client):
    r = client.post("/api/v1/ai/search", json={"query": SPEC_QUERY}).json()
    assert r["hits"][0]["listing_id"] == "lst_demo00"  # Whitefield, E-facing, verified
    assert all(h["price_inr"] <= 15_000_000 and h["bhk"] == 3 and h["facing"] == "E" for h in r["hits"])
    assert r["relaxed_filters"] == []


def test_small_page_does_not_relax_filters(client):
    r = client.post("/api/v1/ai/search", json={"query": "sunlit 3BHK under 1.5 Cr east facing", "limit": 1}).json()
    assert r["relaxed_filters"] == [] and r["total_candidates"] == 3 and len(r["hits"]) == 1


def test_search_respects_hard_filters(client):
    r = client.post("/api/v1/ai/search", json={"query": "3 bhk in Pune under 1.2 cr"}).json()
    assert r["hits"][0]["listing_id"] == "lst_demo06"
    assert all(h["city"] == "Pune" and h["price_inr"] <= 12_000_000 * 1.15 for h in r["hits"])
    # Too few exact matches -> constraints are relaxed in a fixed order and reported to the UI.
    assert r["relaxed_filters"][-1] == "BHK ±1"


def test_upload_requires_sign_in(client):
    assert client.post("/api/v1/ai/upload-listing", data={"notes": KHARADI}).status_code == 401


def test_new_listing_triggers_predictive_match_and_price_drop(client):
    buyer, seller = login(client, "9800000001"), login(client, "9800000002")
    client.post("/api/v1/ai/search", json={"query": "2 bhk in Kharadi pune under 80 lakh"}, headers=buyer)

    r = client.post("/api/v1/ai/upload-listing", data={"notes": KHARADI}, headers=seller)
    assert r.status_code == 201 and r.json()["status"] == "live"
    lid = r.json()["listing"]["id"]
    assert [n["kind"] for n in client.get("/api/v1/notifications", headers=buyer).json()] == ["new_match"]
    assert client.get("/api/v1/notifications", headers=seller).json() == []  # no alert about your own listing

    assert client.patch(f"/api/v1/listings/{lid}/price", json={"price_inr": 1}, headers=buyer).status_code == 403
    assert client.patch(f"/api/v1/listings/{lid}/price", json={"price_inr": 7_200_000},
                        headers=seller).status_code == 200
    kinds = [n["kind"] for n in client.get("/api/v1/notifications", headers=buyer).json()]
    assert kinds == ["new_match", "price_drop"]
    # identical update does not re-notify
    client.patch(f"/api/v1/listings/{lid}/price", json={"price_inr": 7_200_000}, headers=seller)
    assert len(client.get("/api/v1/notifications", headers=buyer).json()) == 2
    # persisted: price history survives a fresh read
    lst = client.get(f"/api/v1/listings/{lid}").json()
    assert [p["price_inr"] for p in lst["price_history"]] == [7_600_000, 7_200_000, 7_200_000]


def test_notifications_are_private(client):
    buyer, other = login(client, "9800000001"), login(client, "9800000003")
    me = client.get("/api/v1/auth/me", headers=buyer).json()
    assert client.get("/api/v1/notifications", params={"user_id": me["id"]}, headers=other).status_code == 403
    assert client.get("/api/v1/notifications").status_code == 401


def test_draft_listing_only_visible_to_owner(client):
    seller, other = login(client, "9800000002"), login(client, "9800000003")
    r = client.post("/api/v1/ai/upload-listing", data={"notes": "3 bhk in Baner, east facing"}, headers=seller)
    lid = r.json()["listing"]["id"]
    assert r.json()["status"] == "draft"
    assert client.get(f"/api/v1/listings/{lid}", headers=seller).status_code == 200
    assert client.get(f"/api/v1/listings/{lid}", headers=other).status_code == 404
    assert client.get(f"/api/v1/listings/{lid}").status_code == 404


def test_listing_as_agent_requires_verified_account(client):
    user = login(client, "9800000004")
    r = client.post("/api/v1/ai/upload-listing", data={"notes": KHARADI, "owner_role": "agent"}, headers=user)
    assert r.status_code == 403


def test_concierge_qualifies_and_books(client):
    buyer = login(client, "9800000001")
    sid, last = None, None
    for msg in ["Hi", "I want to buy for self use", "budget 1.5 cr", "in 2 months",
                "yes need loan, salary 3 lakh per month, no emi", "1"]:
        last = client.post("/api/v1/ai/agent-chat", headers=buyer,
                           json={"session_id": sid, "message": msg, "listing_id": "lst_demo00"}).json()
        sid = last["session_id"]
    assert last["state"] == "BOOKED"
    assert last["lead_score"] >= 80
    assert any(a["type"] == "booking_confirmed" for a in last["actions"])
    assert last["slots"]["preapproval"]["verdict"] == "comfortable"

    admin = login(client, "9800000009", roles=("admin",))
    types = [e["type"] for e in client.get("/api/v1/events", headers=admin).json()]
    assert "visit.scheduled.v1" in types and "lead.routed.v1" in types
    assert client.get("/api/v1/events", headers=buyer).status_code == 403
    # the reminder notification was scheduled for the buyer
    assert [n["kind"] for n in client.get("/api/v1/notifications", headers=buyer).json()] == ["visit_reminder"]


def test_slot_cannot_be_double_booked(client):
    a, b = login(client, "9800000001"), login(client, "9800000003")

    def qualify(headers):
        r = client.post("/api/v1/ai/agent-chat", headers=headers, json={
            "message": "buy within 1 month, budget 1.6 Cr, paying cash", "listing_id": "lst_demo00"}).json()
        assert r["state"] == "SLOT_OFFERED"
        return r["session_id"]

    sa, sb = qualify(a), qualify(b)
    first = client.post("/api/v1/ai/agent-chat", headers=a, json={"session_id": sa, "message": "1"}).json()
    assert first["state"] == "BOOKED"
    second = client.post("/api/v1/ai/agent-chat", headers=b, json={"session_id": sb, "message": "1"}).json()
    assert second["state"] == "SLOT_OFFERED" and "just taken" in second["reply"]
    taken = first["actions"][0]["payload"]["slot"]["slot_id"]
    reoffered = next(x for x in second["actions"] if x["type"] == "offer_slots")["payload"]["slots"]
    assert taken not in [s["slot_id"] for s in reoffered]


def test_signed_in_session_cannot_be_hijacked(client):
    owner, other = login(client, "9800000001"), login(client, "9800000003")
    r = client.post("/api/v1/ai/agent-chat", headers=owner, json={"message": "I want to buy",
                                                                   "listing_id": "lst_demo00"}).json()
    for headers in (other, {}):
        r2 = client.post("/api/v1/ai/agent-chat", headers=headers,
                         json={"session_id": r["session_id"], "message": "budget 1 cr"}).json()
        assert r2["session_id"] != r["session_id"]


def test_concierge_multi_slot_answer_and_handoff(client):
    r = client.post("/api/v1/ai/agent-chat", json={
        "message": "Looking to buy within 1 month, budget 1.6 Cr, paying cash", "listing_id": "lst_demo00"}).json()
    assert r["state"] == "SLOT_OFFERED"  # every slot filled from one message
    r = client.post("/api/v1/ai/agent-chat", json={"session_id": r["session_id"],
                                                   "message": "can I talk to a human?"}).json()
    assert r["state"] == "HANDOFF"


def _wa(body: str, ref: str | None = "lst_demo01") -> dict:
    msg = {"from": "919800000000", "type": "text", "text": {"body": body}}
    if ref:
        msg["referral"] = {"ref": ref}
    return {"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}


def test_whatsapp_webhook_roundtrip(client):
    out = client.post("/api/v1/webhooks/whatsapp", json=_wa("I want to buy")).json()["outbound"]
    assert out[0]["to"] == "919800000000" and "budget" in out[0]["text"]["body"].lower()
    out2 = client.post("/api/v1/webhooks/whatsapp", json=_wa("1.4 Cr", ref=None)).json()["outbound"]
    assert "when" in out2[0]["text"]["body"].lower()  # same session advanced to TIMELINE
    assert out2[0]["session_id"] == out[0]["session_id"]


def test_whatsapp_webhook_signature(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")
    import json

    raw = json.dumps(_wa("hi")).encode()
    assert client.post("/api/v1/webhooks/whatsapp", content=raw,
                       headers={"content-type": "application/json"}).status_code == 401
    sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    assert client.post("/api/v1/webhooks/whatsapp", content=raw, headers={
        "content-type": "application/json", "x-hub-signature-256": sig}).status_code == 200


def test_media_served_publicly_but_legal_docs_are_not(client):
    seller = login(client, "9800000002")
    r = client.post("/api/v1/ai/upload-listing", headers=seller, data={"notes": KHARADI},
                    files=[("files", ("note.txt", b"corner unit", "text/plain")),
                           ("documents", ("deed.pdf", b"%PDF-1.4 fake", "application/pdf"))]).json()
    key = r["listing"]["media"][0]
    assert key.startswith("public/")
    assert client.get(f"/api/v1/media/{key}").content == b"corner unit"
    digest = hashlib.sha256(b"%PDF-1.4 fake").hexdigest()
    private_key = f"private/{digest[:2]}/{digest}.pdf"
    assert client.portal.call(client.container.storage.get, private_key) == b"%PDF-1.4 fake"  # it exists...
    assert client.get(f"/api/v1/media/{private_key}").status_code == 404  # ...but is never served
    for sneaky in (f"public/../{private_key}", f"public/%2e%2e/{private_key}", f"public/{digest[:2]}/../../{private_key}"):
        assert client.get(f"/api/v1/media/{sneaky}").status_code == 404


def test_affordability_math():
    assert emi(5_000_000) == pytest.approx(44_186, rel=1e-3)
    a = assess(150_000, 10_000, target_price=9_000_000)
    assert a.max_emi_inr == 65_000
    assert a.verdict == "comfortable"
    assert assess(60_000, target_price=15_000_000).verdict == "over_budget"


async def test_quiet_hours_defer_whatsapp_but_not_push():
    eng = NotificationEngine(MemoryDatabase())
    prof = BuyerProfile(user_id="u", filters=SearchFilters(), phone_e164="+919800000000", whatsapp_opt_in=True)
    late = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)  # 23:30 IST
    payload = {"title": "t", "price_fmt": "x", "listing_id": "l"}
    out = {n.channel: n for n in await eng.enqueue(prof, "new_match", "k", payload, now=late)}
    assert out["push"].scheduled_for == late
    assert out["whatsapp"].scheduled_for == datetime(2026, 10, 4, 2, 30, tzinfo=timezone.utc)  # 08:00 IST
    body = whatsapp_template_payload(out["whatsapp"], prof.phone_e164)
    assert body["template"]["name"] == "new_match_alert_v1" and body["to"] == "919800000000"
    assert await eng.enqueue(prof, "new_match", "k", payload, now=late) == []  # dedup


async def test_concierge_keeps_listing_facts_when_llm_call_fails(fake_llm, container):
    from app.concierge.agent import Concierge
    from app.core.llm import LLMError
    from app.schemas import ChatRequest

    llm, _ = fake_llm()

    async def boom(**_):
        raise LLMError("unreachable")

    llm.reply = boom
    concierge = Concierge(container.db, container.store, llm, container.bus, container.scheduler)
    r = await concierge.handle(ChatRequest(message="Is it east facing?", listing_id="lst_demo00"))
    assert "facing: E" in r.reply and "₹4,200" in r.reply
