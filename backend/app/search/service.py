"""Natural-language search: NL -> (hard filters, soft preferences) -> hybrid retrieval."""

from __future__ import annotations

import logging

from app.core.events import Event, EventBus
from app.core.heuristics import parse_query
from app.core.llm import LLM, LLMError, text_block
from app.prompts import QUERY_PARSER_SYSTEM
from app.schemas import ParsedQuery, ScoreBreakdown, SearchHit, SearchRequest, SearchResponse
from app.search.store import ListingStore

log = logging.getLogger(__name__)
MIN_RESULTS = 3


async def parse(llm: LLM, query: str) -> tuple[ParsedQuery, bool]:
    if llm.enabled:
        try:
            return await llm.extract(system=QUERY_PARSER_SYSTEM, content=[text_block(query)],
                                     schema=ParsedQuery, effort="low", max_tokens=2000), True
        except LLMError as e:
            log.warning("query parse fell back to heuristics: %s", e)
    return parse_query(query), False


def _relaxations(parsed: ParsedQuery):
    """Progressively loosen the least important constraints; yields (label, filters)."""
    f = parsed.filters.model_copy(deep=True)
    if f.facing:
        f.facing = []
        yield "facing", f.model_copy(deep=True)
    if f.localities:
        f.localities = []
        yield "locality (kept city)", f.model_copy(deep=True)
    if f.price_max_inr:
        f.price_max_inr *= 1.15
        yield "budget +15%", f.model_copy(deep=True)
    if f.bhk_min and f.bhk_min == f.bhk_max:
        f.bhk_max = f.bhk_min + 1
        f.bhk_min = max(1, f.bhk_min - 1)
        yield "BHK ±1", f.model_copy(deep=True)


async def search(req: SearchRequest, store: ListingStore, llm: LLM, bus: EventBus) -> SearchResponse:
    parsed, used_llm = await parse(llm, req.query)
    if req.filters_override:
        parsed.filters = req.filters_override
    results, total = store.search(parsed.filters, parsed.semantic_query, parsed.soft_preferences, req.limit)
    relaxed: list[str] = []
    # Relax on the candidate count, not the page size, so a small `limit` never loosens filters.
    if total < MIN_RESULTS:
        for label, f in _relaxations(parsed):
            relaxed.append(label)
            results, total = store.search(f, parsed.semantic_query, parsed.soft_preferences, req.limit)
            if total >= MIN_RESULTS:
                break

    hits = []
    for score, parts, lst, why in results:
        d = lst.data
        reasons = [f"matches '{w}'" for w in why]
        if lst.is_verified:
            reasons.append("AI-verified legal status")
        hits.append(SearchHit(
            listing_id=lst.id, score=round(score, 4),
            breakdown=ScoreBreakdown(**{k: round(v, 4) for k, v in parts.items()}),
            title=d.seo_title, price_inr=d.price_inr, bhk=d.bhk,
            carpet_area_sqft=d.carpet_area_sqft or d.built_up_area_sqft or d.super_built_up_area_sqft,
            city=d.address.city, locality=d.address.locality, facing=d.facing,
            verified=lst.is_verified, why=reasons))

    await bus.publish(Event(type="search.performed.v1", source="/svc/search", subject=req.user_id,
                            partitionkey=req.user_id,
                            data={"user_id": req.user_id, "query": req.query, "parsed": parsed.model_dump(),
                                  "result_ids": [h.listing_id for h in hits]}))
    return SearchResponse(query=req.query, parsed=parsed, total_candidates=total, hits=hits,
                          relaxed_filters=relaxed, llm_used=used_llm)
