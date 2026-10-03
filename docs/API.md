# Section 5: API endpoint specifications

Base URL: `https://api.<domain>/api/v1`. In development the backend runs at `http://localhost:8000/api/v1`, and the Next.js app proxies `/api/v1/*` to it.

FastAPI serves the live OpenAPI document at `/openapi.json` and Swagger UI at `/docs`. All request and response models are defined in [`backend/app/schemas.py`](../backend/app/schemas.py) and [`backend/app/api/routes.py`](../backend/app/api/routes.py).

## Conventions

| Concern | Convention |
|---|---|
| Auth | Phone OTP login (see **Authentication** below). Browsers use httpOnly `SameSite=Strict` cookies; native and API clients send `Authorization: Bearer <access token>`. The caller's identity always comes from the token, never from the request body. Partner and developer bulk APIs will use OAuth2 client credentials. |
| Idempotency | `Idempotency-Key` header on every POST that creates a resource. The gateway stores the response in Redis for 24 h. |
| Rate limits | Search: 60/min per user. Chat: 30/min per session. Upload: 10/h per user. Exceeding a limit returns `429` with a `Retry-After` header. |
| Errors | `{"detail": "<message>"}`, or FastAPI's validation array for 422. Codes used: `404`, `413` (file too large), `422`, `429`, `502` (upstream AI failure when no fallback was possible). |
| Money / area | Money is absolute INR as a number (₹1.5 Cr = `15000000`). Area is in sq ft. Floors are 0-based (ground = 0). |
| Streaming (prod) | `Accept: text/event-stream` on `/ai/agent-chat` streams tokens. `/ai/upload-listing?async=true` returns `202 {job_id}`, and the job is tracked at `GET /ai/jobs/{id}/events` (SSE). |

gRPC is used **internally** between services for low-latency paths (search ↔ embedding service). The proto is at the end of this document.

---

## 1. `POST /api/v1/ai/upload-listing`: multimodal ingestion

Turns raw media into a structured, SEO-ready, optionally verified listing. Content type: `multipart/form-data`. **Requires sign-in.** Listing as `owner` grants that role automatically; `agent` and `developer` need a verified account (otherwise `403`).

| Field | Type | Req. | Description |
|---|---|---|---|
| `files` | file[] | one of `files` / `documents` / `transcript` / `notes` | Photos (jpeg/png/webp/gif), videos (any `video/*`), floor-plan or brochure PDFs, voice notes (`audio/*`). Max 200 MB each. |
| `documents` | file[] | | Legal documents (PDF/JPG/PNG): RERA certificate, title or sale deed, OC/CC, EC, khata, tax receipt. Triggers verification. |
| `transcript` | string | | Client-side speech-to-text, if already available (Web Speech API, WhatsApp voice transcript). |
| `notes` | string ≤ 5000 | | Free text from the seller. |
| `owner_role` | `owner`\|`agent`\|`developer` | | Default `owner`. |
| `auto_publish` | bool | | Default `true`. The listing goes `live` if the critical fields (price, area, city, and BHK for residential) are present and legal verification did not fail. |

```bash
curl -X POST http://localhost:8000/api/v1/ai/upload-listing \
  -H "Idempotency-Key: 6f1d…" \
  -F "files=@living.jpg" -F "files=@walkthrough.mp4" -F "files=@floorplan.pdf" -F "files=@voice.m4a" \
  -F "documents=@rera.pdf" -F "documents=@oc.pdf" \
  -F "notes=Price 1.42 Cr negotiable, maintenance 4200/month" \
  -F "owner_role=owner"
```

**`201 Created`**

```json
{
  "status": "live",
  "ai_used": true,
  "clarifying_questions": [],
  "warnings": ["price conflict: model=1420000000 text=14200000; using text value"],
  "timings_ms": {"normalise": 4120, "extract": 21850, "reconcile": 2, "verify": 18400, "index": 9},
  "listing": {
    "id": "lst_8f2a91c0d3e4",
    "owner_id": "5b0e7c1a-…", "owner_role": "owner", "status": "live",
    "quality_score": 0.912,
    "data": {
      "transaction_type": "sale", "property_type": "apartment", "bhk": 3, "bathrooms": 3, "balconies": 2,
      "carpet_area_sqft": 1450, "built_up_area_sqft": null, "super_built_up_area_sqft": 1890,
      "floor_number": 9, "total_floors": 18, "facing": "E", "furnishing": "semi_furnished",
      "price_inr": 14200000, "maintenance_monthly_inr": 4200, "possession_status": "ready_to_move",
      "amenities": ["gym", "swimming pool", "clubhouse", "power backup"],
      "address": {"project_name": "Prestige Lakeside Habitat", "locality": "Whitefield", "city": "Bengaluru",
                  "state": "Karnataka", "pincode": "560066", "landmark": null},
      "rera_number": "PRM/KA/RERA/1251/446/PR/171031/000001",
      "natural_light_score": 9,
      "highlights": ["East-facing", "Lake view", "10 min to ITPL"],
      "seo_title": "3 BHK East-Facing Apartment in Whitefield, Bengaluru",
      "seo_description": "…",
      "image_insights": [{"index": 0, "room_type": "living_room", "caption": "Bright living room with lake-view balcony",
                          "quality_issues": [], "is_cover_candidate": true}],
      "field_confidence": [{"field": "facing", "confidence": 0.95, "source": "pdf:1"}],
      "missing_fields": [], "clarifying_questions": []
    },
    "verification": {
      "status": "verified", "badge": "100% AI-Verified Legal Status", "score": 1.0,
      "checks": [{"name": "rera_matches_listing", "passed": true, "detail": "listing=PRM/KA/… document=PRM/KA/…"}],
      "buyer_summary": "RERA registration valid until 2030-12-31 …",
      "documents": ["…DocumentExtraction…"]
    },
    "price_history": [{"price_inr": 14200000, "at": "2026-10-03T09:30:12Z"}]
  }
}
```

Behaviour:

- Originals are stored content-addressed (`listing.media` holds the keys, served at `/api/v1/media/{key}`). Legal documents go to private storage.
- When critical fields are missing, the response has `status: "draft"` and `clarifying_questions[]`. The seller answers, by voice or text, through the same endpoint (prod: `PATCH /listings/{id}`).
- `warnings[]` lists everything that was skipped or auto-corrected (a missing ffmpeg or STT, unit fixes, oversize images).
- Errors: `413` for a file over the size limit. `422` when there is no input, or when there is no LLM key and only images were sent (the offline parser needs text).

---

## 2. `POST /api/v1/ai/search`: natural-language search

**Request** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `query` | string 2–1000 | Any language or style, e.g. *"Show me a sunlit 3BHK near tech hubs under ₹1.5 Cr with low maintenance and east facing"*. |
| `limit` | int 1–50 | Default 10. |
| `filters_override` | `SearchFilters`? | Sent when the user removes or edits a filter chip, so the query is not re-parsed. |

**`200 OK`** (abridged; scores are illustrative):

```json
{
  "query": "Show me a sunlit 3BHK near tech hubs under ₹1.5 Cr with low maintenance and east facing",
  "parsed": {
    "filters": {"transaction_type": null, "property_types": [], "bhk_min": 3, "bhk_max": 3,
                "price_min_inr": null, "price_max_inr": 15000000, "carpet_area_min_sqft": null,
                "cities": [], "localities": [], "facing": ["E"], "furnishing": [], "possession_status": null,
                "max_maintenance_monthly_inr": null, "must_have_amenities": [], "verified_only": false},
    "soft_preferences": ["abundant natural light", "near tech parks", "low maintenance"],
    "semantic_query": "sunlit east-facing 3BHK close to IT parks with low monthly maintenance",
    "clarification": null
  },
  "total_candidates": 3,
  "hits": [
    {"listing_id": "lst_demo00", "score": 0.6952,
     "breakdown": {"semantic": 0.2499, "keyword": 0.968, "preference": 1.0, "freshness": 1.0, "verified_boost": 1.0},
     "title": "3 BHK E-Facing Apartment in Whitefield, Bengaluru", "price_inr": 14200000, "bhk": 3,
     "carpet_area_sqft": 1450, "city": "Bengaluru", "locality": "Whitefield", "facing": "E", "verified": true,
     "why": ["matches 'abundant natural light'", "matches 'low maintenance'", "matches 'near tech parks'",
             "AI-verified legal status"]}
  ],
  "relaxed_filters": [],
  "llm_used": true
}
```

Behaviour:

- `relaxed_filters` is non-empty when fewer than 3 homes matched exactly. Constraints are loosened in a fixed order: facing → locality → budget +15% → BHK ±1. The UI tells the user.
- `parsed.clarification` is set only when nothing searchable could be derived. The UI shows it as the assistant's reply.
- Auth: optional. When the caller is signed in, the search updates their buyer profile, which drives proactive alerts.
- Side effect: emits `search.performed.v1`.

---

## 3. `POST /api/v1/ai/agent-chat`: 24/7 virtual concierge

**Request**

| Field | Type | Description |
|---|---|---|
| `session_id` | string? | Omit on the first message. Echo the returned value afterwards. |
| `message` | string 1–4000 | User text. |
| `listing_id` | string? | The property the chat is about. Enables grounded Q&A and booking. |
| `channel` | `web`\|`whatsapp`\|`app` | |

**`200 OK`**

```json
{
  "session_id": "ses_318d5786a9b5",
  "state": "SLOT_OFFERED",
  "reply": "Great, this home fits your requirements. Indicative loan eligibility: up to ₹1.70 Cr (EMI ~₹94,115/month for this home). Pick a site-visit slot:\n1. Sun 04 Oct, 10:00 AM\n2. Sun 04 Oct, 12:00 PM\n3. Sun 04 Oct, 03:00 PM\n4. Sun 04 Oct, 05:00 PM",
  "lead_score": 85,
  "slots": {
    "intent": "buy", "budget_max_inr": 15000000, "timeline_months": 1, "needs_loan": true,
    "monthly_income_inr": 300000, "existing_emi_inr": null, "name": null, "phone": null,
    "preapproval": {"max_emi_inr": 150000, "max_loan_inr": 16973881, "max_property_price_inr": 22631841,
                    "emi_for_target_inr": 94115, "target_price_inr": 14200000, "verdict": "comfortable"},
    "booking_id": null
  },
  "actions": [
    {"type": "offer_slots", "payload": {"slots": [
      {"slot_id": "202610041000", "start": "2026-10-04T10:00:00+05:30", "end": "2026-10-04T10:45:00+05:30",
       "label": "Sun 04 Oct, 10:00 AM"}]}}
  ]
}
```

That is the response to the single message *"Looking to buy within 1 month, budget 1.5 Cr, need loan, salary 3 lakh per month"*. Every qualification slot was filled from one message.

| `actions[].type` | Client behaviour |
|---|---|
| `offer_slots` | Render the slots as buttons. A tap sends `"1"`…`"4"`. |
| `booking_confirmed` | Payload is the `Booking` object. Show a confirmation and an add-to-calendar link. |
| `handoff_human` | Payload `{reason}`. Show "relationship manager notified". |
| `show_listings` | Payload `{budget_max_inr}`. Open search with the budget applied. |
| `request_document` | Prod: ask the buyer for KYC or a pre-approval letter. |

`state` is one of `GREETING, INTENT, BUDGET, TIMELINE, FINANCING, QUALIFIED, SLOT_OFFERED, BOOKED, HANDOFF`. The state machine is described in ARCHITECTURE §4.1.

Side effects:

- Every turn emits `lead.updated.v1`.
- A booking emits `visit.scheduled.v1`, which schedules a reminder at T−2h.
- A hot lead or a stuck conversation emits `lead.routed.v1`.

---

## Authentication

Phone number + one-time code. There are no passwords.

| Method & path | Body | Result |
|---|---|---|
| `POST /api/v1/auth/otp/request` | `{phone}` (any Indian format; bare 10-digit numbers get `+91`) | `{phone_e164, expires_in, dev_code}`. `dev_code` is `null` unless `APT_DEV_OTP_IN_RESPONSE=true` (local development only; refused when `APT_ENV=prod`). Otherwise the code is only sent by SMS. |
| `POST /api/v1/auth/otp/verify` | `{phone, code, full_name?}` | `{user, access_expires_in}` and sets cookies. Creates the account on first login. |
| `POST /api/v1/auth/refresh` | none (cookie) or `{refresh_token}` | Rotates the refresh token and issues a new access token. |
| `POST /api/v1/auth/logout` | none (cookie) or `{refresh_token}` | `204`. Revokes the refresh token's whole chain and clears cookies. |
| `GET /api/v1/auth/me` | | The signed-in `User` (`id`, `phone_e164`, `full_name`, `roles`). |
| `POST /api/v1/auth/roles` | `{role}` | Self-service `buyer` / `owner`. `agent`, `developer` and `admin` return `403` (they need KYC / org verification). |

How it works:

- **Codes**: 6 digits, valid 5 minutes, single use. Stored only as an HMAC. At most 5 codes per number per hour and 5 wrong guesses per code (`429` after that).
- **Access token**: HS256 JWT, 15 minutes, carries `sub` and `roles`. Roles are re-read from the database on every request, so a role change takes effect immediately.
- **Refresh token**: random 256-bit value, 30 days, stored as a SHA-256 hash. Each refresh replaces it. If an already-replaced token is presented again, the whole chain is revoked and every device on it must sign in again (protection against a stolen token).
- **Web clients** get `apt_at` (path `/api`) and `apt_rt` (path `/api/v1/auth`) as httpOnly, `SameSite=Strict` cookies, `Secure` in prod. The frontend client refreshes automatically on a `401` and retries once.
- **Native/API clients** send `X-Auth-Mode: token` on verify/refresh to get `access_token` and `refresh_token` in the body instead of cookies.
- A request with an **invalid or expired token gets `401` even on endpoints that allow guests**, so clients refresh rather than silently continuing as a guest.
- `APT_ENV=prod` refuses to start without a strong `APT_JWT_SECRET` and a `DATABASE_URL`, or with `APT_DEV_OTP_IN_RESPONSE` enabled.

## Supporting endpoints (MVP)

| Method & path | Purpose |
|---|---|
| `POST /api/v1/ai/affordability` | `{monthly_income_inr, existing_emi_inr?, down_payment_inr?, target_price_inr?}` → FOIR/LTV `Affordability` (max EMI, max loan, max price, EMI for target, verdict). |
| `GET /api/v1/listings/{id}` | Full listing. Drafts are visible only to their owner (`404` for everyone else). A signed-in viewer's visit is recorded for price-drop alerts. |
| `PATCH /api/v1/listings/{id}/price` | **Owner or admin.** `{price_inr}`. Records history and emits `listing.price_changed.v1`, which drives price-drop alerts. |
| `GET /api/v1/notifications` | **Signed in.** The caller's notifications. Admins may pass `?user_id=`. WhatsApp items include the exact Cloud API `provider_payload`. |
| `GET /api/v1/events?limit=` | **Admin.** Recent domain events (from `outbox_events` on Postgres). |
| `GET /api/v1/media/{key}` | Public listing media by content-addressed key. Legal documents are stored privately and never served here. |
| `GET/POST /api/v1/webhooks/whatsapp` | Meta verification handshake. Inbound messages must carry a valid `X-Hub-Signature-256` (HMAC with `WHATSAPP_APP_SECRET`; required in prod). The sender's number identifies the user, the conversation continues across messages, and `referral.ref` from Click-to-WhatsApp ads selects the listing. |
| `GET /healthz` | Liveness, storage backend, LLM-enabled flag and listing count. |

---

## Internal gRPC (search ↔ embedding / ranking)

```proto
syntax = "proto3";
package aipt.v1;

service Embedding {
  rpc Embed (EmbedRequest) returns (EmbedResponse);            // batch ≤ 64
}
message EmbedRequest  { repeated string texts = 1; string model = 2; bool sparse = 3; }
message EmbedResponse { repeated Vector vectors = 1; }
message Vector        { repeated float dense = 1; map<uint32, float> sparse = 2; }

service Ranker {
  rpc Rank (RankRequest) returns (RankResponse);               // LTR re-rank of top-50
}
message RankRequest  { string user_id = 1; repeated Candidate candidates = 2; repeated string soft_prefs = 3; }
message Candidate    { string listing_id = 1; float hybrid_score = 2; map<string, float> features = 3; }
message RankResponse { repeated Scored ranked = 1; }
message Scored       { string listing_id = 1; float score = 2; }
```
