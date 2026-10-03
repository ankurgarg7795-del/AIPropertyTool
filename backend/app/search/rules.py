"""Pure listing rules shared by every storage backend: hard-filter matching,
derived preference tags and the text that gets embedded / keyword-indexed."""

from __future__ import annotations

from app.core.heuristics import LOCALITIES
from app.schemas import Listing, SearchFilters

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
