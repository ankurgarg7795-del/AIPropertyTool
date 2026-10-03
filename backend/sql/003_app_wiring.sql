-- =====================================================================
-- 003: columns and tables the application layer needs on top of 001/002.
-- =====================================================================

-- Public, URL-safe listing ids (lst_...) separate from the uuid PK.
ALTER TABLE properties
    ADD COLUMN public_id text UNIQUE NOT NULL
        DEFAULT ('lst_' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 12)),
    ADD COLUMN media_keys text[] NOT NULL DEFAULT '{}',
    -- Tokens produced by the app's tokenizer (synonyms applied), so keyword
    -- scoring matches the in-memory backend's vocabulary.
    ADD COLUMN search_tokens text NOT NULL DEFAULT '',
    ADD COLUMN search_tokens_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', search_tokens)) STORED;
CREATE INDEX properties_search_tokens_idx ON properties USING gin (search_tokens_tsv);

-- Full VerificationResult (checks + per-document extractions) as returned to clients.
ALTER TABLE verifications ADD COLUMN result jsonb;

ALTER TABLE buyer_profiles
    ADD COLUMN searches int NOT NULL DEFAULT 0,
    ADD COLUMN viewed_listing_ids text[] NOT NULL DEFAULT '{}';

-- Concierge FSM state (everything except the message log, which is ai_messages).
ALTER TABLE ai_conversations ADD COLUMN session_state jsonb NOT NULL DEFAULT '{}';
CREATE INDEX ai_conversations_thread_idx ON ai_conversations (channel, external_thread, started_at DESC);

ALTER TABLE appointments ADD COLUMN public_id text UNIQUE;

-- ---------------------------------------------------------------------
-- Auth: phone OTP + rotating refresh tokens
-- ---------------------------------------------------------------------
CREATE TABLE auth_otps (
    phone_e164      text PRIMARY KEY,
    code_hash       text NOT NULL,                      -- HMAC-SHA256(server secret, phone|code)
    expires_at      timestamptz NOT NULL,
    attempts        smallint NOT NULL DEFAULT 0,
    sent_count      smallint NOT NULL DEFAULT 1,        -- within window_start + 1h
    window_start    timestamptz NOT NULL
);

CREATE TABLE auth_refresh_tokens (
    token_hash      text PRIMARY KEY,                   -- SHA-256 of the opaque token
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id       uuid NOT NULL,                      -- rotation chain; reuse revokes the family
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auth_refresh_family_idx ON auth_refresh_tokens (family_id);
