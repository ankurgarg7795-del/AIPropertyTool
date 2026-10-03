"""Listing index: writes (embed + tokenise + tag) and hybrid retrieval over any ``Database``.

Retrieval, identical for both backends:

  1. hard filters (SQL WHERE / ``rules.matches``)        -> candidate set
  2. dense vector similarity (HNSW / in-process cosine)  -> semantic score
  3. sparse keyword score (ts_rank_cd / BM25)            -> keyword score (max-normalised)
  4. structured preference tags (light, tech-hub ...)    -> preference score
  5. weighted fusion + verified/freshness boosts         -> ranked, explainable hits
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from app.db.base import Database
from app.schemas import Listing, SearchFilters
from app.search.embeddings import Embedder, tokens
from app.search.rules import WEIGHTS, document_text, preference_tags

RECALL = 200

Scored = tuple[float, dict[str, float], Listing, list[str]]


class ListingIndex:
    def __init__(self, db: Database, embedder: Embedder):
        self.db = db
        self.embedder = embedder

    async def upsert(self, listing: Listing) -> None:
        text = document_text(listing)
        await self.db.upsert_listing(listing, self.embedder.embed([text])[0], " ".join(tokens(text)),
                                     sorted(preference_tags(listing)))

    async def get(self, listing_id: str) -> Listing | None:
        return await self.db.get_listing(listing_id)

    async def search(self, f: SearchFilters, semantic_query: str, soft_prefs: list[str],
                     limit: int) -> tuple[list[Scored], int]:
        q = semantic_query + " " + " ".join(soft_prefs)
        cands, total = await self.db.listing_candidates(f, self.embedder.embed([q])[0], tokens(q), RECALL)
        if not cands:
            return [], total
        kw_max = max(c.keyword for c in cands) or 1.0
        now = datetime.now(timezone.utc)
        prefs = {p.lower() for p in soft_prefs}
        scored: list[Scored] = []
        for c in cands:
            tags = preference_tags(c.listing)
            parts = {
                "semantic": c.semantic,
                "keyword": c.keyword / kw_max,
                "preference": (len(prefs & tags) / len(prefs)) if prefs else 0.0,
                "freshness": math.exp(-max(0, (now - c.listing.created_at).days) / 45),
                "verified_boost": 1.0 if c.listing.is_verified else 0.0,
            }
            scored.append((sum(WEIGHTS[k] * v for k, v in parts.items()), parts, c.listing, sorted(prefs & tags)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:limit], total
