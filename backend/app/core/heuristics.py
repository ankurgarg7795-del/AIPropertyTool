"""Deterministic Indian-real-estate text parsing.

Used (1) as the offline fallback when no LLM key is set, and (2) as a cheap
post-validation pass that cross-checks LLM output (e.g. price units).
"""

from __future__ import annotations

import re

from app.schemas import Address, ListingExtraction, ParsedQuery, SearchFilters

LAKH, CRORE = 100_000, 10_000_000

CITIES = {
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "mumbai": "Mumbai", "pune": "Pune",
    "hyderabad": "Hyderabad", "gurugram": "Gurugram", "gurgaon": "Gurugram", "noida": "Noida",
    "delhi": "Delhi", "chennai": "Chennai", "kolkata": "Kolkata", "ahmedabad": "Ahmedabad",
    "thane": "Thane", "navi mumbai": "Navi Mumbai",
}
# Locality -> (city, is_tech_hub)
LOCALITIES = {
    "whitefield": ("Bengaluru", True), "electronic city": ("Bengaluru", True),
    "sarjapur road": ("Bengaluru", True), "koramangala": ("Bengaluru", True),
    "hsr layout": ("Bengaluru", True), "indiranagar": ("Bengaluru", False),
    "hebbal": ("Bengaluru", True), "jp nagar": ("Bengaluru", False),
    "hinjewadi": ("Pune", True), "kharadi": ("Pune", True), "baner": ("Pune", False),
    "wakad": ("Pune", True), "gachibowli": ("Hyderabad", True), "kondapur": ("Hyderabad", True),
    "hitech city": ("Hyderabad", True), "kokapet": ("Hyderabad", True),
    "powai": ("Mumbai", True), "andheri east": ("Mumbai", True), "bandra": ("Mumbai", False),
    "golf course road": ("Gurugram", True), "sector 150": ("Noida", False),
    "sohna road": ("Gurugram", False), "omr": ("Chennai", True), "velachery": ("Chennai", False),
}
AMENITIES = [
    "gym", "swimming pool", "clubhouse", "power backup", "lift", "security", "park",
    "children's play area", "jogging track", "covered parking", "intercom", "gated community",
    "rainwater harvesting", "ev charging", "co-working", "tennis court", "badminton court",
]
FACING_WORDS = {
    "north east": "NE", "north-east": "NE", "northeast": "NE", "north west": "NW", "north-west": "NW",
    "northwest": "NW", "south east": "SE", "south-east": "SE", "southeast": "SE", "south west": "SW",
    "south-west": "SW", "southwest": "SW", "east": "E", "west": "W", "north": "N", "south": "S",
}

_NUM = r"(\d+(?:\.\d+)?)"
_MONEY_RE = re.compile(
    rf"(?:₹|rs\.?|inr)?\s*{_NUM}\s*(crores?|cr|lakhs?|lacs?|l|k|thousand)\b", re.I)


def parse_money(text: str) -> float | None:
    m = _MONEY_RE.search(text.replace(",", ""))
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith("cr"):
        return value * CRORE
    if unit.startswith("l"):
        return value * LAKH
    return value * 1000


def _all_money(text: str) -> list[tuple[int, float]]:
    out = []
    for m in _MONEY_RE.finditer(text.replace(",", "")):
        out.append((m.start(), parse_money(m.group(0)) or 0.0))
    return out


def find_facing(text: str) -> str | None:
    t = text.lower()
    for word, code in FACING_WORDS.items():  # longest phrases first (dict order)
        if re.search(rf"\b{word}\b[\s-]*(facing|face)|(facing|faces)\s+{word}\b", t):
            return code
    return None


def find_location(text: str) -> tuple[str | None, str | None]:
    t = text.lower()
    locality = next((loc for loc in LOCALITIES if loc in t), None)
    city = next((c for k, c in CITIES.items() if re.search(rf"\b{k}\b", t)), None)
    if locality and not city:
        city = LOCALITIES[locality][0]
    return (locality.title() if locality else None), city


def parse_listing_text(text: str) -> ListingExtraction:
    t = text.lower()
    bhk = re.search(r"(\d)\s*-?\s*(?:bhk|bed(?:room)?s?)", t)
    bath = re.search(r"(\d)\s*(?:bath(?:room)?s?|toilets?)", t)
    area = re.search(rf"{_NUM}\s*(sq\.?\s*ft|sqft|square feet|sq\.?\s*m|sqm|sq\.?\s*yd|gaj)", t.replace(",", ""))
    area_kind = re.search(r"(carpet|super\s*built|built[\s-]*up)", t)
    floor = re.search(r"(\d+)(?:st|nd|rd|th)?\s*floor(?:\s*(?:of|out of|/)\s*(\d+))?", t)
    total = re.search(r"(?:of|out of)\s*(\d+)\s*(?:floors|storey)", t)
    maint = re.search(rf"maintenance[^\d₹]*(?:₹|rs\.?)?\s*{_NUM}\s*(k)?", t.replace(",", ""))
    rera = re.search(r"\b(P\d{11}|PR/[A-Z0-9/]+|[A-Z]{2,5}RERA[A-Z0-9/\-]+|RERA[\s:#-]*[A-Z0-9/\-]{6,})", text)
    is_rent = bool(re.search(r"\b(rent|lease|per month|/month|pm)\b", t))

    sqft: float | None = None
    if area:
        v, unit = float(area.group(1)), area.group(2)
        sqft = v * (10.7639 if "m" in unit and "ft" not in unit else 9 if ("yd" in unit or "gaj" in unit) else 1)
        sqft = round(sqft, 1)
    carpet = sqft if area_kind and area_kind.group(1).startswith("carpet") else None
    super_bu = sqft if area_kind and area_kind.group(1).startswith("super") else None
    built = sqft if area_kind and area_kind.group(1).startswith("built") else None
    if sqft and not (carpet or super_bu or built):
        super_bu = sqft

    # Price: biggest money mention that is not the maintenance amount.
    amounts = [v for pos, v in _all_money(t) if "maint" not in t[max(0, pos - 25):pos]]
    price = max(amounts) if amounts else None

    ptype = "apartment"
    for word, pt in (("villa", "villa"), ("plot", "plot"), ("penthouse", "penthouse"), ("studio", "studio"),
                     ("builder floor", "builder_floor"), ("independent house", "independent_house"),
                     ("office", "commercial_office"), ("shop", "shop")):
        if word in t:
            ptype = pt
            break
    furnishing = ("fully_furnished" if re.search(r"fully[\s-]*furnished", t) else
                  "semi_furnished" if re.search(r"semi[\s-]*furnished", t) else
                  "unfurnished" if "unfurnished" in t else None)
    possession = ("under_construction" if re.search(r"under[\s-]*construction|possession in", t) else
                  "ready_to_move" if re.search(r"ready[\s-]*to[\s-]*move|ready possession", t) else None)
    locality, city = find_location(text)
    facing = find_facing(text)
    amenities = [a for a in AMENITIES if a in t]
    floor_no = int(floor.group(1)) if floor else (0 if "ground floor" in t else None)
    total_floors = int(floor.group(2)) if floor and floor.group(2) else (int(total.group(1)) if total else None)
    maintenance = (float(maint.group(1)) * (1000 if maint.group(2) else 1)) if maint else None

    bhk_n = int(bhk.group(1)) if bhk else None
    title_bits = [f"{bhk_n} BHK" if bhk_n else None, f"{facing}-Facing" if facing else None,
                  ptype.replace("_", " ").title(), f"in {locality}," if locality else None, city]
    title = " ".join(b for b in title_bits if b).rstrip(",")[:70]
    missing = [f for f, v in (("price_inr", price), ("area", sqft), ("bhk", bhk_n), ("city", city)) if v is None]
    if ptype in ("plot", "commercial_office", "shop") and "bhk" in missing:
        missing.remove("bhk")

    return ListingExtraction(
        transaction_type="rent" if is_rent else "sale",
        property_type=ptype,
        bhk=bhk_n,
        bathrooms=int(bath.group(1)) if bath else None,
        carpet_area_sqft=carpet, built_up_area_sqft=built, super_built_up_area_sqft=super_bu,
        floor_number=floor_no, total_floors=total_floors,
        facing=facing, furnishing=furnishing, price_inr=price,
        maintenance_monthly_inr=maintenance, possession_status=possession,
        amenities=amenities,
        address=Address(locality=locality, city=city),
        rera_number=rera.group(1).strip() if rera else None,
        highlights=[h for h in (
            f"{bhk_n} BHK" if bhk_n else None,
            f"{int(sqft)} sq ft" if sqft else None,
            f"{facing} facing" if facing else None,
            possession.replace("_", " ") if possession else None,
        ) if h],
        seo_title=title or "Property for sale",
        seo_description=text.strip()[:900],
        seo_keywords=[k for k in (locality, city, f"{bhk_n} bhk" if bhk_n else None, ptype) if k],
        missing_fields=missing,
        clarifying_questions=[f"Could you share the {m.replace('_inr', '').replace('_', ' ')}?" for m in missing[:3]],
    )


def parse_query(query: str) -> ParsedQuery:
    t = query.lower().replace(",", "")
    f = SearchFilters()
    bhk = re.search(r"(\d)\s*(?:-|to)?\s*(\d)?\s*bhk", t)
    if bhk:
        f.bhk_min = int(bhk.group(1))
        f.bhk_max = int(bhk.group(2)) if bhk.group(2) else f.bhk_min

    money = _all_money(t)
    if money:
        pos, amt = money[-1]
        before = t[max(0, pos - 20):pos]
        if re.search(r"(under|below|less than|upto|up to|within|max|budget)\s*$", before.strip() + " ") or "under" in before:
            f.price_max_inr = amt
        elif re.search(r"(above|over|more than|min|at least)", before):
            f.price_min_inr = amt
        elif re.search(r"(around|about|approx|~)", before):
            f.price_min_inr, f.price_max_inr = amt * 0.9, amt * 1.1
        elif len(money) >= 2:
            f.price_min_inr, f.price_max_inr = sorted([money[-2][1], amt])
        else:
            f.price_max_inr = amt * 1.05
    maint = re.search(rf"maintenance\s*(?:under|below|<)\s*(?:₹|rs\.?)?\s*{_NUM}\s*(k)?", t)
    if maint:
        f.max_maintenance_monthly_inr = float(maint.group(1)) * (1000 if maint.group(2) else 1)

    facing = find_facing(query)
    if facing:
        f.facing = [facing]
    locality, city = find_location(query)
    if city:
        f.cities = [city]
    if locality:
        f.localities = [locality]
    if re.search(r"\b(rent|rental|lease)\b", t):
        f.transaction_type = "rent"
    elif re.search(r"\b(buy|purchase|for sale)\b", t):
        f.transaction_type = "sale"
    if "ready to move" in t or "ready-to-move" in t:
        f.possession_status = "ready_to_move"
    if "verified" in t:
        f.verified_only = True
    for word, pt in (("villa", "villa"), ("plot", "plot"), ("penthouse", "penthouse"), ("studio", "studio")):
        if word in t:
            f.property_types.append(pt)
    f.furnishing = [x for x, pat in (("fully_furnished", r"fully[\s-]*furnished"),
                                    ("semi_furnished", r"semi[\s-]*furnished")) if re.search(pat, t)]
    f.must_have_amenities = [a for a in AMENITIES if a in t]

    soft = []
    for pat, label in ((r"sun\s*lit|sunny|natural light|bright|airy", "abundant natural light"),
                       (r"tech (hub|park)|it park|office|it corridor", "near tech parks"),
                       (r"low maintenance", "low maintenance"), (r"quiet|peaceful", "quiet neighbourhood"),
                       (r"school", "good schools nearby"), (r"metro", "near metro"),
                       (r"view|lake|park facing", "open views"), (r"family", "family friendly"),
                       (r"luxury|premium", "premium finish")):
        if re.search(pat, t):
            soft.append(label)
    return ParsedQuery(filters=f, soft_preferences=soft, semantic_query=query.strip())
