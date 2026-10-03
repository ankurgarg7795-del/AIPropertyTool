# AIPropertyTool

An AI-native, zero-friction, free-to-list real-estate portal for India.

- **Sellers** upload photos, a walkthrough video or a voice note, and the AI builds the listing.
- **Buyers** describe the home they want in plain language.
- **A 24/7 concierge** pre-qualifies buyers and books site visits.
- **Event-driven alerts** tell buyers about new matches and price drops before they search again.

| Blueprint section | Where |
|---|---|
| 1. System architecture and tech stack | [docs/ARCHITECTURE.md §1](docs/ARCHITECTURE.md#section-1-system-architecture-and-tech-stack) |
| 2. AI pipeline, multimodal processing, vector search, prompts | [docs/ARCHITECTURE.md §2](docs/ARCHITECTURE.md#section-2-ai-pipeline-and-multimodal-processing) · [backend/app/prompts.py](backend/app/prompts.py) |
| 3. Database and event schema | [docs/ARCHITECTURE.md §3](docs/ARCHITECTURE.md#section-3-database-and-event-schema-design) · [backend/sql/](backend/sql) |
| 4. Communication and notification engine | [docs/ARCHITECTURE.md §4](docs/ARCHITECTURE.md#section-4-automated-communication-and-notification-engine) |
| 5. API specifications | [docs/API.md](docs/API.md) |
| 6. Full-stack MVP code | [backend/](backend) (FastAPI) · [frontend/](frontend) (Next.js 15) — map below |

## Quick start

```bash
# Backend: runs fully offline with heuristic parsing; set ANTHROPIC_API_KEY for Claude.
cd backend
pip install -e ".[dev]"            # add ".[media]" for Pillow + faster-whisper
export ANTHROPIC_API_KEY=...       # optional
uvicorn app.main:app --reload      # http://localhost:8000/docs
pytest                             # 29 tests, no network needed

# Frontend
cd frontend
npm install
npm run dev                        # http://localhost:3000  (proxies /api/v1 → :8000)
```

Or run everything with `cp .env.example .env && docker compose up`. This also starts Postgres with pgvector, with `backend/sql` applied, plus Redis and Kafka.

### Offline vs. AI mode

| | No API key | With `ANTHROPIC_API_KEY` |
|---|---|---|
| Listing extraction | Regex/heuristic parser over notes and transcripts. Photos and PDFs are stored but not analysed. | Claude reads photos, video keyframes, PDFs and transcripts → `ListingExtraction` via structured outputs. |
| Search parsing | Heuristic NL parser (BHK, lakh/crore budgets, facing, city/locality, soft preferences). | Claude (`effort: low`) → `ParsedQuery`. |
| Legal verification | Queued as `needs_review`. | Claude reads each document, then the rule engine issues the badge. |
| Concierge | Deterministic FSM with template replies. | Same FSM, with replies phrased by Claude and property Q&A grounded in listing facts. |

The default model is `claude-opus-5-5`, with server-side refusal fallbacks (`fallbacks: "default"`) enabled. Disable them with `APT_LLM_SERVER_FALLBACKS=false`, for example on Bedrock or Vertex, where the parameter is unavailable.

## Section 6: code map

### Backend (FastAPI / Python)

| File | What it does |
|---|---|
| `app/core/llm.py` | Claude wrapper: multimodal content blocks and Pydantic → strict JSON schema. Handles refusals, truncation and API errors. |
| `app/ingestion/media.py` | MIME classification, ffmpeg keyframe sampling, audio-track extraction, Whisper STT. |
| `app/ingestion/pipeline.py` | The ingestion pipeline: normalise → extract → reconcile → verify → score → publish → index → event. |
| `app/ingestion/verification.py` | Per-document extraction and rule-engine checks (RERA formats, expiry, tampering, encumbrances, area consistency). Issues the badge. |
| `app/core/heuristics.py` | Indian real-estate text parsing (₹/lakh/crore, sq ft/sq m/gaj, facing, localities, tech hubs). Offline fallback and cross-check. |
| `app/search/embeddings.py` | Hashing embedder (offline) or bge-m3 via sentence-transformers. |
| `app/search/store.py` | Hybrid retrieval: hard filters → dense + BM25 + preference tags + boosts. Mirrors `sql/002_hybrid_search.sql`. |
| `app/search/service.py` | NL search orchestration, filter relaxation, `search.performed` event. |
| `app/concierge/agent.py` | Pre-qualification and booking state machine, slot extractors (Hinglish-aware), lead scoring, LLM phrasing. |
| `app/concierge/finance.py` | FOIR/LTV affordability and EMI maths. |
| `app/concierge/scheduler.py` | Visit slot generation and conflict-checked booking. |
| `app/core/events.py` | CloudEvents bus, predictive matching (reverse search), notification engine (dedup, quiet hours, opt-in), WhatsApp template payloads. |
| `app/api/routes.py` | `/ai/upload-listing`, `/ai/search`, `/ai/agent-chat`, `/ai/affordability`, listings, notifications, WhatsApp webhook. |
| `sql/001_schema.sql`, `sql/002_hybrid_search.sql` | Production Postgres schema plus the hybrid-search and reverse-match functions (validated on PG16 + pgvector). |

### Frontend (Next.js 15 / React 19)

| File | What it does |
|---|---|
| `components/ConversationalSearch.tsx` | Chat-style search thread. Parsed filters appear as removable chips (re-query with `filters_override`), alongside soft-preference chips, explainable result cards and relaxation notices. |
| `components/ListingUploader.tsx` | Drag-and-drop media and legal-doc zones with previews. Voice-note recording (MediaRecorder) with a live Web Speech transcript. Upload progress. Extracted-listing card with clarifying questions. |
| `components/ConciergeChat.tsx` | Concierge drawer with state and lead-score readout, and slot buttons for booking. |
| `lib/api.ts` | Typed API client (XHR for upload progress). |
