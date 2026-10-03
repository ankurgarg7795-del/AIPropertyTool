"""Listing repository + hybrid retrieval.

The in-memory implementation mirrors the production query in
``sql/002_hybrid_search.sql`` step for step:

  1. hard SQL filters (price, BHK, city, facing ...)   -> candidate set
  2. dense vector similarity over the candidate set     -> semantic score
  3. sparse keyword score (BM25)                        -> keyword score
  4. structured preference tags (light, tech-hub ...)   -> preference score
  5. weighted fusion + verified/freshness boosts
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone

from app.core.heuristics import LOCALITIES
from app.schemas import Listing, SearchFilters
from app.search.embeddings import Embedder, cosine, tokens

WEIGHTS = {"semantic": 0.40, "keyword": 0.15, "preference": 0.30, "freshness": 0.05, "verified_boost": 0.10}


def preference_tags(listing: Listing) -> set[str]:
    """Derived facts that back the fuzzy wishes buyers type ("sunlit", "near tech hubs")."""
    d = listing.data
    tags: set[str] = set()
    loc = (d.address.locality or "").lower()
    if (d.natural_light_score or 0) >= 7 or d.facing in ("E", "NE", "N"):
        tags.add("abundant natural light")
    if loc in LOCALITIES and LOCALITIES[loc][1]:
        tags.add("near tech parks")
    area = d.carpet_area_sqft or d.built_up_area_sqft or d.super_built_up_area_sqft
    if d.maintenance_monthly_inr is not None and area and d.maintenance_monthly_inr / area <= 3.0:
        tags.add("low maintenance")
    if d.floor_number is not None and d.floor_number >= 8:
        tags.add("open views")
    am = {a.lower() for a in d.amenities}
    if am & {"children's play area", "park", "gated community"} or (d.bhk or 0) >= 3:
        tags.add("family friendly")
    if am & {"swimming pool", "clubhouse"} and d.furnishing == "fully_furnished":
        tags.add("premium finish")
    if any("metro" in h.lower() for h in d.highlights):
        tags.add("near metro")
    if any("school" in h.lower() for h in d.highlights):
        tags.add("good schools nearby")
    if any(w in " ".join(d.highlights).lower() for w in ("quiet", "peaceful", "green")):
        tags.add("quiet neighbourhood")
    return tags


def document_text(listing: Listing) -> str:
    d = listing.data
    parts = [d.seo_title, d.seo_description, " ".join(d.highlights), " ".join(d.amenities),
             d.address.locality or "", d.address.city or "", d.address.project_name or "",
             f"{d.bhk} bhk" if d.bhk else "", d.property_type.replace("_", " "),
             f"{d.facing} facing" if d.facing else "", " ".join(sorted(preference_tags(listing)))]
    return " ".join(p for p in parts if p)


def area_of(listing: Listing) -> float | None:
    d = listing.data
    return d.carpet_area_sqft or d.built_up_area_sqft or d.super_built_up_area_sqft


def matches(listing: Listing, f: SearchFilters) -> bool:
    d = listing.data
    if listing.status != "live":
        return False
    if f.transaction_type and d.transaction_type != f.transaction_type:
        return False
    if f.property_types and d.property_type not in f.property_types:
        return False
    if f.bhk_min is not None and (d.bhk is None or d.bhk < f.bhk_min):
        return False
    if f.bhk_max is not None and (d.bhk is None or d.bhk > f.bhk_max):
        return False
    if f.price_max_inr is not None and (d.price_inr is None or d.price_inr > f.price_max_inr):
        return False
    if f.price_min_inr is not None and (d.price_inr is None or d.price_inr < f.price_min_inr):
        return False
    if f.carpet_area_min_sqft is not None and (area_of(listing) or 0) < f.carpet_area_min_sqft:
        return False
    if f.cities and (d.address.city or "").lower() not in {c.lower() for c in f.cities}:
        return False
    if f.localities and (d.address.locality or "").lower() not in {x.lower() for x in f.localities}:
        return False
    if f.facing and d.facing not in f.facing:
        return False
    if f.furnishing and d.furnishing not in f.furnishing:
        return False
    if f.possession_status and d.possession_status != f.possession_status:
        return False
    if f.max_maintenance_monthly_inr is not None and (d.maintenance_monthly_inr or 0) > f.max_maintenance_monthly_inr:
        return False
    if f.must_have_amenities and not {a.lower() for a in f.must_have_amenities} <= {a.lower() for a in d.amenities}:
        return False
    if f.verified_only and not listing.is_verified:
        return False
    return True


class ListingStore:
    def __init__(self, embedder: Embedder):
        self.embedder = embedder
        self.listings: dict[str, Listing] = {}
        self._vec: dict[str, list[float]] = {}
        self._tf: dict[str, Counter] = {}
        self._df: Counter = Counter()

    # ---- writes ---------------------------------------------------------- #
    def upsert(self, listing: Listing) -> None:
        if listing.id in self._tf:
            self._df.subtract(set(self._tf[listing.id]))
        text = document_text(listing)
        self.listings[listing.id] = listing
        self._vec[listing.id] = self.embedder.embed([text])[0]
        tf = Counter(tokens(text))
        self._tf[listing.id] = tf
        self._df.update(set(tf))

    def get(self, listing_id: str) -> Listing | None:
        return self.listings.get(listing_id)

    # ---- reads ----------------------------------------------------------- #
    def _bm25(self, q: list[str], lid: str, k1: float = 1.2, b: float = 0.75) -> float:
        tf = self._tf[lid]
        n = len(self._tf) or 1
        avgdl = sum(sum(t.values()) for t in self._tf.values()) / n
        dl = sum(tf.values())
        s = 0.0
        for term in set(q):
            if term not in tf:
                continue
            idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        return s

    def search(self, f: SearchFilters, semantic_query: str, soft_prefs: list[str], limit: int):
        candidates = [lst for lst in self.listings.values() if matches(lst, f)]
        if not candidates:
            return [], 0
        qv = self.embedder.embed([semantic_query + " " + " ".join(soft_prefs)])[0]
        qt = tokens(semantic_query + " " + " ".join(soft_prefs))
        raw_kw = {c.id: self._bm25(qt, c.id) for c in candidates}
        kw_max = max(raw_kw.values()) or 1.0
        now = datetime.now(timezone.utc)
        prefs = {p.lower() for p in soft_prefs}
        scored = []
        for c in candidates:
            tags = preference_tags(c)
            sem = max(0.0, cosine(qv, self._vec[c.id]))
            pref = (len(prefs & tags) / len(prefs)) if prefs else 0.0
            age_days = (now - c.created_at).days
            parts = {
                "semantic": sem,
                "keyword": raw_kw[c.id] / kw_max,
                "preference": pref,
                "freshness": math.exp(-age_days / 45),
                "verified_boost": 1.0 if c.is_verified else 0.0,
            }
            score = sum(WEIGHTS[k] * v for k, v in parts.items())
            scored.append((score, parts, c, sorted(prefs & tags)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:limit], len(candidates)
