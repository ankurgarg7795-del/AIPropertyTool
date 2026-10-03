-- =====================================================================
-- Hybrid search: hard SQL filters -> dense (HNSW) + sparse (FTS) recall
-- -> Reciprocal Rank Fusion + structured preference/verification boosts.
-- Mirrors app/search/store.py.
-- =====================================================================
CREATE OR REPLACE FUNCTION hybrid_search(
    p_query_embedding vector(1024),
    p_query_text      text,
    p_soft_prefs      text[]   DEFAULT '{}',
    p_city            text[]   DEFAULT NULL,
    p_localities      text[]   DEFAULT NULL,
    p_txn             transaction_type DEFAULT NULL,
    p_bhk_min         smallint DEFAULT NULL,
    p_bhk_max         smallint DEFAULT NULL,
    p_price_min       numeric  DEFAULT NULL,
    p_price_max       numeric  DEFAULT NULL,
    p_facing          facing_dir[] DEFAULT NULL,
    p_max_maint       numeric  DEFAULT NULL,
    p_amenities       text[]   DEFAULT NULL,
    p_verified_only   boolean  DEFAULT false,
    p_limit           int      DEFAULT 20,
    p_recall          int      DEFAULT 200
)
RETURNS TABLE (property_id uuid, score double precision, dense_rank_pos int, sparse_rank_pos int,
               pref_overlap int, verified boolean)
LANGUAGE sql STABLE AS $$
WITH candidates AS (                       -- 1. hard filters (B-tree / GIN indexes)
    SELECT p.id, p.preference_tags, p.verification_status = 'verified' AS verified, p.published_at, p.search_tsv
    FROM properties p
    WHERE p.status = 'live'
      AND (p_city        IS NULL OR p.city = ANY (p_city))
      AND (p_localities  IS NULL OR p.locality = ANY (p_localities))
      AND (p_txn         IS NULL OR p.transaction_type = p_txn)
      AND (p_bhk_min     IS NULL OR p.bhk >= p_bhk_min)
      AND (p_bhk_max     IS NULL OR p.bhk <= p_bhk_max)
      AND (p_price_min   IS NULL OR p.price_inr >= p_price_min)
      AND (p_price_max   IS NULL OR p.price_inr <= p_price_max)
      AND (p_facing      IS NULL OR p.facing = ANY (p_facing))
      AND (p_max_maint   IS NULL OR coalesce(p.maintenance_monthly_inr, 0) <= p_max_maint)
      AND (p_amenities   IS NULL OR p.amenities @> p_amenities)
      AND (NOT p_verified_only OR p.verification_status = 'verified')
),
dense AS (                                 -- 2. semantic recall (HNSW, cosine)
    SELECT c.id, row_number() OVER (ORDER BY e.embedding <=> p_query_embedding) AS r
    FROM candidates c JOIN property_embeddings e ON e.property_id = c.id AND e.kind = 'text'
    ORDER BY e.embedding <=> p_query_embedding
    LIMIT p_recall
),
sparse AS (                                -- 3. keyword recall (BM25-like ts_rank_cd)
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.search_tsv, q) DESC) AS r
    FROM candidates c, websearch_to_tsquery('english', p_query_text) q
    WHERE c.search_tsv @@ q
    ORDER BY ts_rank_cd(c.search_tsv, q) DESC
    LIMIT p_recall
),
fused AS (                                 -- 4. RRF (k=60) + boosts
    SELECT c.id,
           coalesce(1.0 / (60 + d.r), 0) + coalesce(1.0 / (60 + s.r), 0) AS rrf,
           d.r AS dr, s.r AS sr,
           cardinality(ARRAY(SELECT unnest(c.preference_tags) INTERSECT SELECT unnest(p_soft_prefs))) AS overlap,
           c.verified, c.published_at
    FROM candidates c
    LEFT JOIN dense d  ON d.id = c.id
    LEFT JOIN sparse s ON s.id = c.id
    WHERE d.id IS NOT NULL OR s.id IS NOT NULL
)
SELECT id,
       rrf * 30                                                           -- ~0..1
     + 0.30 * CASE WHEN cardinality(p_soft_prefs) > 0
                   THEN overlap::float / cardinality(p_soft_prefs) ELSE 0 END
     + 0.10 * verified::int
     + 0.05 * exp(-extract(epoch FROM now() - coalesce(published_at, now())) / 86400 / 45)
       AS score,
       dr::int, sr::int, overlap, verified
FROM fused
ORDER BY score DESC
LIMIT p_limit;
$$;

-- Reverse search for predictive matching: which saved searches does a new listing satisfy?
CREATE OR REPLACE FUNCTION matching_saved_searches(p_property_id uuid, p_min_similarity float DEFAULT 0.55)
RETURNS TABLE (saved_search_id uuid, user_id uuid, similarity double precision)
LANGUAGE sql STABLE AS $$
SELECT ss.id, ss.user_id, 1 - (ss.query_embedding <=> e.embedding) AS similarity
FROM saved_searches ss
JOIN property_embeddings e ON e.property_id = p_property_id AND e.kind = 'text'
JOIN properties p ON p.id = p_property_id
WHERE ss.alert_enabled
  AND (ss.parsed #>> '{filters,price_max_inr}' IS NULL
       OR p.price_inr <= (ss.parsed #>> '{filters,price_max_inr}')::numeric)
  AND (ss.parsed #>> '{filters,bhk_min}' IS NULL OR p.bhk >= (ss.parsed #>> '{filters,bhk_min}')::int)
  AND (ss.parsed #>> '{filters,bhk_max}' IS NULL OR p.bhk <= (ss.parsed #>> '{filters,bhk_max}')::int)
  AND (jsonb_array_length(coalesce(ss.parsed #> '{filters,cities}', '[]')) = 0
       OR ss.parsed #> '{filters,cities}' ? p.city)
  AND 1 - (ss.query_embedding <=> e.embedding) >= p_min_similarity;
$$;
