from datetime import datetime, timezone

import pytest

from app.concierge.finance import assess, emi
from app.core.events import BuyerProfile, NotificationEngine, whatsapp_template_payload
from app.schemas import SearchFilters


def test_spec_query_ranks_east_facing_tech_hub_home_first(client):
    r = client.post("/api/v1/ai/search", json={
        "query": "Show me a sunlit 3BHK near tech hubs under ₹1.5 Cr with low maintenance and east facing"}).json()
    top = r["hits"][0]
    assert top["listing_id"] == "lst_demo00"  # Whitefield, E-facing, verified
    assert all(h["price_inr"] <= 15_000_000 * 1.15 for h in r["hits"])
    assert all(h["bhk"] == 3 for h in r["hits"])


def test_small_page_does_not_relax_filters(client):
    r = client.post("/api/v1/ai/search", json={"query": "sunlit 3BHK under 1.5 Cr east facing", "limit": 1}).json()
    assert r["relaxed_filters"] == [] and r["total_candidates"] == 3 and len(r["hits"]) == 1


def test_search_respects_hard_filters(client):
    r = client.post("/api/v1/ai/search", json={"query": "3 bhk in Pune under 1.2 cr"}).json()
    assert r["hits"][0]["listing_id"] == "lst_demo06"
    assert all(h["city"] == "Pune" and h["price_inr"] <= 12_000_000 * 1.15 for h in r["hits"])
    # Too few exact matches -> constraints are relaxed in a fixed order and reported to the UI.
    assert r["relaxed_filters"][-1] == "BHK ±1"


def test_new_listing_triggers_predictive_match_and_price_drop(client):
    client.post("/api/v1/ai/search", json={"query": "2 bhk in Kharadi pune under 80 lakh", "user_id": "buyer9"})
    r = client.post("/api/v1/ai/upload-listing", data={
        "notes": "2 bhk Kharadi Pune 900 sq ft carpet 3rd floor of 10, 76 lakh, east facing"})
    assert r.status_code == 201 and r.json()["status"] == "live"
    lid = r.json()["listing"]["id"]
    notes = client.get("/api/v1/notifications", params={"user_id": "buyer9"}).json()
    assert [n["kind"] for n in notes] == ["new_match"]

    client.patch(f"/api/v1/listings/{lid}/price", json={"price_inr": 7_200_000})
    kinds = [n["kind"] for n in client.get("/api/v1/notifications", params={"user_id": "buyer9"}).json()]
    assert kinds == ["new_match", "price_drop"]
    # identical update does not re-notify
    client.patch(f"/api/v1/listings/{lid}/price", json={"price_inr": 7_200_000})
    assert len(client.get("/api/v1/notifications", params={"user_id": "buyer9"}).json()) == 2


def test_concierge_qualifies_and_books(client):
    sid, last = None, None
    for msg in ["Hi", "I want to buy for self use", "budget 1.5 cr", "in 2 months",
                "yes need loan, salary 3 lakh per month, no emi", "1"]:
        last = client.post("/api/v1/ai/agent-chat",
                           json={"session_id": sid, "message": msg, "listing_id": "lst_demo00"}).json()
        sid = last["session_id"]
    assert last["state"] == "BOOKED"
    assert last["lead_score"] >= 80
    assert any(a["type"] == "booking_confirmed" for a in last["actions"])
    assert last["slots"]["preapproval"]["verdict"] == "comfortable"
    types = [e["type"] for e in client.get("/api/v1/events").json()]
    assert "visit.scheduled.v1" in types and "lead.routed.v1" in types


def test_concierge_multi_slot_answer_and_handoff(client):
    r = client.post("/api/v1/ai/agent-chat", json={
        "message": "Looking to buy within 1 month, budget 1.6 Cr, paying cash", "listing_id": "lst_demo00"}).json()
    assert r["state"] == "SLOT_OFFERED"  # every slot filled from one message
    r = client.post("/api/v1/ai/agent-chat", json={"session_id": r["session_id"],
                                                   "message": "can I talk to a human?"}).json()
    assert r["state"] == "HANDOFF"


def test_whatsapp_webhook_roundtrip(client):
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "919800000000", "type": "text", "text": {"body": "I want to buy"},
         "referral": {"ref": "lst_demo01"}}]}}]}]}
    out = client.post("/api/v1/webhooks/whatsapp", json=payload).json()["outbound"]
    assert out[0]["to"] == "919800000000" and "budget" in out[0]["text"]["body"].lower()
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "1.4 Cr"
    out = client.post("/api/v1/webhooks/whatsapp", json=payload).json()["outbound"]
    assert "when" in out[0]["text"]["body"].lower()  # same session advanced to TIMELINE


def test_affordability_math():
    assert emi(5_000_000) == pytest.approx(44_186, rel=1e-3)
    a = assess(150_000, 10_000, target_price=9_000_000)
    assert a.max_emi_inr == 65_000
    assert a.verdict == "comfortable"
    assert assess(60_000, target_price=15_000_000).verdict == "over_budget"


def test_quiet_hours_defer_whatsapp_but_not_push():
    eng = NotificationEngine()
    prof = BuyerProfile(user_id="u", filters=SearchFilters(), phone_e164="+919800000000", whatsapp_opt_in=True)
    late = datetime(2026, 10, 3, 18, 0, tzinfo=timezone.utc)  # 23:30 IST
    out = {n.channel: n for n in eng.enqueue(prof, "new_match", "k", {"title": "t", "price_fmt": "x",
                                                                       "listing_id": "l"}, now=late)}
    assert out["push"].scheduled_for == late
    assert out["whatsapp"].scheduled_for == datetime(2026, 10, 4, 2, 30, tzinfo=timezone.utc)  # 08:00 IST
    body = whatsapp_template_payload(out["whatsapp"], prof.phone_e164)
    assert body["template"]["name"] == "new_match_alert_v1" and body["to"] == "919800000000"
