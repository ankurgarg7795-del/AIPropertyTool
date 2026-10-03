-- =====================================================================
-- AIPropertyTool – core relational schema (PostgreSQL 16 + pgvector)
-- =====================================================================
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()

-- ---------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------
CREATE TYPE user_role        AS ENUM ('buyer','owner','agent','developer','project_marketer','admin');
CREATE TYPE property_type    AS ENUM ('apartment','villa','independent_house','builder_floor','penthouse',
                                      'studio','plot','commercial_office','shop','warehouse');
CREATE TYPE transaction_type AS ENUM ('sale','rent');
CREATE TYPE facing_dir       AS ENUM ('N','S','E','W','NE','NW','SE','SW');
CREATE TYPE furnishing_type  AS ENUM ('unfurnished','semi_furnished','fully_furnished');
CREATE TYPE possession_type  AS ENUM ('ready_to_move','under_construction');
CREATE TYPE listing_status   AS ENUM ('draft','pending_review','live','sold','rented','archived');
CREATE TYPE media_kind       AS ENUM ('image','video','pdf','audio','floor_plan','cad','brochure');
CREATE TYPE doc_type         AS ENUM ('rera_certificate','title_deed','sale_deed','occupancy_certificate',
                                      'completion_certificate','property_tax_receipt','encumbrance_certificate',
                                      'khata','allotment_letter','other');
CREATE TYPE verification_status AS ENUM ('pending','verified','needs_review','failed');
CREATE TYPE job_status       AS ENUM ('queued','running','succeeded','failed');
CREATE TYPE lead_stage       AS ENUM ('new','qualifying','qualified','visit_scheduled','visited',
                                      'negotiating','won','lost','handoff');
CREATE TYPE appt_status      AS ENUM ('proposed','confirmed','rescheduled','cancelled','completed','no_show');
CREATE TYPE txn_status       AS ENUM ('initiated','token_paid','agreement_drafted','agreement_signed',
                                      'loan_sanctioned','registered','cancelled');
CREATE TYPE channel_type     AS ENUM ('push','whatsapp','sms','email','in_app');

-- ---------------------------------------------------------------------
-- Identity
-- ---------------------------------------------------------------------
CREATE TABLE organizations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('brokerage','developer','marketing_agency')),
    rera_agent_no   text,
    gstin           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_e164      text UNIQUE,
    email           text UNIQUE,
    full_name       text,
    roles           user_role[] NOT NULL DEFAULT '{buyer}',
    org_id          uuid REFERENCES organizations(id),
    preferred_lang  text NOT NULL DEFAULT 'en-IN',
    whatsapp_opt_in boolean NOT NULL DEFAULT false,
    kyc_verified    boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Buyer intent profile, maintained by the matching engine from behaviour.
CREATE TABLE buyer_profiles (
    user_id             uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    hard_filters        jsonb NOT NULL DEFAULT '{}',      -- SearchFilters
    soft_preferences    text[] NOT NULL DEFAULT '{}',
    intent_embedding    vector(1024),                     -- centroid of searched/viewed listings
    budget_max_inr      numeric(14,2),
    monthly_income_inr  numeric(14,2),
    preapproval         jsonb,                            -- Affordability snapshot
    lead_score          smallint NOT NULL DEFAULT 0 CHECK (lead_score BETWEEN 0 AND 100),
    channels            channel_type[] NOT NULL DEFAULT '{push,whatsapp}',
    quiet_start         time NOT NULL DEFAULT '22:00',        -- local time; wraps midnight
    quiet_end           time NOT NULL DEFAULT '08:00',
    last_active_at      timestamptz,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE saved_searches (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    query_text      text NOT NULL,
    parsed          jsonb NOT NULL,                        -- ParsedQuery
    query_embedding vector(1024),
    alert_enabled   boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Developer projects & inventory (bulk upload from CAD / master plans)
-- ---------------------------------------------------------------------
CREATE TABLE projects (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          uuid NOT NULL REFERENCES organizations(id),
    name            text NOT NULL,
    rera_number     text,
    city            text NOT NULL,
    locality        text,
    geo             point,
    launch_date     date,
    possession_date date,
    master_plan_url text,
    total_units     int,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE unit_inventory (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    tower           text,
    unit_no         text NOT NULL,
    floor_number    smallint,
    bhk             smallint,
    carpet_area_sqft numeric(9,2),
    facing          facing_dir,
    base_price_inr  numeric(14,2),
    status          text NOT NULL DEFAULT 'available' CHECK (status IN ('available','blocked','booked','sold')),
    source_ref      text,                                  -- CAD layer / plan sheet reference
    UNIQUE (project_id, tower, unit_no)
);

-- ---------------------------------------------------------------------
-- Properties (listings)
-- ---------------------------------------------------------------------
CREATE TABLE properties (
    id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id                 uuid NOT NULL REFERENCES users(id),
    listed_by_role           user_role NOT NULL,
    org_id                   uuid REFERENCES organizations(id),
    project_id               uuid REFERENCES projects(id),
    unit_id                  uuid REFERENCES unit_inventory(id),
    status                   listing_status NOT NULL DEFAULT 'draft',
    transaction_type         transaction_type NOT NULL,
    property_type            property_type NOT NULL,
    bhk                      smallint CHECK (bhk BETWEEN 0 AND 20),
    bathrooms                smallint,
    balconies                smallint,
    carpet_area_sqft         numeric(9,2),
    built_up_area_sqft       numeric(9,2),
    super_built_up_area_sqft numeric(9,2),
    floor_number             smallint,
    total_floors             smallint,
    facing                   facing_dir,
    furnishing               furnishing_type,
    price_inr                numeric(14,2) CHECK (price_inr > 0),
    maintenance_monthly_inr  numeric(10,2),
    possession_status        possession_type,
    possession_date          date,
    age_years                smallint,
    parking_slots            smallint,
    amenities                text[] NOT NULL DEFAULT '{}',
    rera_number              text,
    project_name             text,
    locality                 text,
    city                     text,
    state                    text,
    pincode                  char(6),
    geo                      point,
    natural_light_score      smallint CHECK (natural_light_score BETWEEN 0 AND 10),
    preference_tags          text[] NOT NULL DEFAULT '{}',   -- derived: 'near tech parks', 'low maintenance'
    title                    text NOT NULL,
    description              text NOT NULL,
    highlights               text[] NOT NULL DEFAULT '{}',
    seo_keywords             text[] NOT NULL DEFAULT '{}',
    ai_extraction            jsonb NOT NULL,                 -- full ListingExtraction incl. confidences
    ai_model                 text,
    quality_score            numeric(4,3) NOT NULL DEFAULT 0,
    verification_status      verification_status NOT NULL DEFAULT 'pending',
    avm_estimate_inr         numeric(14,2),
    avm_confidence           numeric(4,3),
    search_tsv               tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(title,'')), 'A') ||
        setweight(to_tsvector('simple', coalesce(locality,'') || ' ' || coalesce(city,'') || ' ' ||
                                        coalesce(project_name,'')), 'A') ||
        setweight(to_tsvector('english', coalesce(description,'')), 'B')) STORED,
    published_at             timestamptz,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX properties_live_filter_idx ON properties (city, transaction_type, bhk, price_inr)
    WHERE status = 'live';
CREATE INDEX properties_locality_trgm_idx ON properties USING gin (locality gin_trgm_ops);
CREATE INDEX properties_amenities_idx ON properties USING gin (amenities);
CREATE INDEX properties_tags_idx ON properties USING gin (preference_tags);
CREATE INDEX properties_tsv_idx ON properties USING gin (search_tsv);

-- One row per embedding "view" of a listing; lets text and image vectors evolve independently.
CREATE TABLE property_embeddings (
    property_id     uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    kind            text NOT NULL CHECK (kind IN ('text','image_cover','image_mean')),
    model           text NOT NULL,                         -- e.g. 'bge-m3'
    embedding       vector(1024) NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (property_id, kind)
);
CREATE INDEX property_embeddings_hnsw ON property_embeddings
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 128);

CREATE TABLE property_media (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    kind            media_kind NOT NULL,
    storage_key     text NOT NULL,                         -- s3://bucket/key (original)
    enhanced_key    text,                                  -- auto-enhanced rendition
    derived_from    uuid REFERENCES property_media(id),    -- keyframe -> video
    room_type       text,
    caption         text,
    quality_issues  text[] NOT NULL DEFAULT '{}',
    is_cover        boolean NOT NULL DEFAULT false,
    sort_order      smallint NOT NULL DEFAULT 0,
    transcript      text,                                  -- audio / video narration
    sha256          char(64) NOT NULL,                     -- dedup + duplicate-listing detection
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX property_media_one_cover ON property_media (property_id) WHERE is_cover;
CREATE INDEX property_media_sha_idx ON property_media (sha256);

CREATE TABLE property_price_history (
    property_id     uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    price_inr       numeric(14,2) NOT NULL,
    changed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (property_id, changed_at)
);

-- ---------------------------------------------------------------------
-- Legal documents & AI verification
-- ---------------------------------------------------------------------
CREATE TABLE property_documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    doc_type        doc_type,
    storage_key     text NOT NULL,                         -- encrypted bucket, short-lived signed URLs
    sha256          char(64) NOT NULL,
    extraction      jsonb,                                 -- DocumentExtraction
    legibility      numeric(3,2),
    tampering_signals text[] NOT NULL DEFAULT '{}',
    uploaded_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE verifications (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     uuid NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    status          verification_status NOT NULL,
    score           numeric(4,3) NOT NULL,
    checks          jsonb NOT NULL,                        -- [{name, passed, detail}]
    registry_lookup jsonb,                                 -- state RERA portal response
    buyer_summary   text,
    badge           text,
    reviewed_by     uuid REFERENCES users(id),             -- human reviewer for needs_review
    model           text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX verifications_latest_idx ON verifications (property_id, created_at DESC);

-- ---------------------------------------------------------------------
-- AI ingestion jobs (async pipeline bookkeeping)
-- ---------------------------------------------------------------------
CREATE TABLE ingestion_jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     uuid REFERENCES properties(id),
    requested_by    uuid NOT NULL REFERENCES users(id),
    status          job_status NOT NULL DEFAULT 'queued',
    stage           text,                                  -- normalise|extract|reconcile|verify|index
    input_keys      text[] NOT NULL,
    warnings        text[] NOT NULL DEFAULT '{}',
    timings_ms      jsonb NOT NULL DEFAULT '{}',
    llm_usage       jsonb,                                 -- tokens in/out, cache reads, cost
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

-- ---------------------------------------------------------------------
-- AI conversations (concierge, search chat, agent copilots)
-- ---------------------------------------------------------------------
CREATE TABLE ai_conversations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid REFERENCES users(id),
    property_id     uuid REFERENCES properties(id),
    channel         channel_type NOT NULL DEFAULT 'in_app',
    external_thread text,                                  -- WhatsApp wa_id etc.
    agent_kind      text NOT NULL CHECK (agent_kind IN ('concierge','search','seller_copilot','agent_crm')),
    fsm_state       text NOT NULL DEFAULT 'GREETING',
    slots           jsonb NOT NULL DEFAULT '{}',
    lead_score      smallint NOT NULL DEFAULT 0,
    handed_off_to   uuid REFERENCES users(id),
    started_at      timestamptz NOT NULL DEFAULT now(),
    last_message_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel, external_thread, property_id)
);

CREATE TABLE ai_messages (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    role            text NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content         text NOT NULL,
    tool_calls      jsonb,
    fsm_state_after text,
    model           text,
    input_tokens    int,
    output_tokens   int,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_messages_conv_idx ON ai_messages (conversation_id, id);

-- ---------------------------------------------------------------------
-- Leads, appointments, transactions
-- ---------------------------------------------------------------------
CREATE TABLE leads (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     uuid NOT NULL REFERENCES properties(id),
    buyer_id        uuid REFERENCES users(id),
    conversation_id uuid REFERENCES ai_conversations(id),
    assigned_to     uuid REFERENCES users(id),             -- agent / developer sales rep
    stage           lead_stage NOT NULL DEFAULT 'new',
    score           smallint NOT NULL DEFAULT 0 CHECK (score BETWEEN 0 AND 100),
    score_factors   jsonb NOT NULL DEFAULT '{}',
    source          text NOT NULL,                         -- search|whatsapp|campaign:<id>|referral
    next_followup_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX leads_assignee_idx ON leads (assigned_to, stage, score DESC);
CREATE INDEX leads_followup_idx ON leads (next_followup_at) WHERE stage NOT IN ('won','lost');

CREATE TABLE appointments (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id         uuid NOT NULL REFERENCES leads(id),
    property_id     uuid NOT NULL REFERENCES properties(id),
    host_id         uuid NOT NULL REFERENCES users(id),
    slot            tstzrange NOT NULL,
    mode            text NOT NULL DEFAULT 'in_person' CHECK (mode IN ('in_person','video')),
    status          appt_status NOT NULL DEFAULT 'confirmed',
    calendar_event_id text,
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- No double-booking of a host (needs btree_gist).
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE appointments ADD CONSTRAINT appointments_no_overlap
    EXCLUDE USING gist (host_id WITH =, slot WITH &&) WHERE (status IN ('proposed','confirmed','rescheduled'));

CREATE TABLE transactions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    property_id     uuid NOT NULL REFERENCES properties(id),
    buyer_id        uuid NOT NULL REFERENCES users(id),
    seller_id       uuid NOT NULL REFERENCES users(id),
    agent_id        uuid REFERENCES users(id),
    status          txn_status NOT NULL DEFAULT 'initiated',
    agreed_price_inr numeric(14,2),
    token_amount_inr numeric(14,2),
    loan_partner    text,
    loan_amount_inr numeric(14,2),
    agreement_draft_key text,                              -- AI-drafted agreement-of-sale (docx/pdf)
    stamp_duty_inr  numeric(12,2),
    registration_date date,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Eventing: transactional outbox (Debezium -> Kafka) and notifications
-- ---------------------------------------------------------------------
CREATE TABLE outbox_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    aggregate_type  text NOT NULL,                         -- listing|lead|visit|search
    aggregate_id    text NOT NULL,                         -- Kafka partition key
    type            text NOT NULL,                         -- listing.price_changed.v1
    payload         jsonb NOT NULL,                        -- CloudEvents data
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notifications (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users(id),
    kind            text NOT NULL,                         -- new_match|price_drop|visit_reminder|lead_routed
    channel         channel_type NOT NULL,
    dedup_key       text NOT NULL UNIQUE,
    template        text,
    payload         jsonb NOT NULL,
    scheduled_for   timestamptz NOT NULL,
    status          text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sent','delivered','read','failed','suppressed')),
    provider_msg_id text,
    source_event_id uuid,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz
);
CREATE INDEX notifications_due_idx ON notifications (scheduled_for) WHERE status = 'queued';
