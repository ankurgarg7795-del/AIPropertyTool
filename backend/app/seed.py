"""Demo inventory so search and concierge work out of the box."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.schemas import Address, Listing, ListingExtraction, PricePoint, VerificationCheck, VerificationResult

_ROWS = [
    # bhk, area(carpet), price, locality, city, facing, floor, total, maint, light, furnishing, possession, amenities, project, verified, highlights
    (3, 1450, 14_200_000, "Whitefield", "Bengaluru", "E", 9, 18, 4200, 9, "semi_furnished", "ready_to_move",
     ["gym", "swimming pool", "clubhouse", "power backup"], "Prestige Lakeside Habitat", True,
     ["10 min to ITPL tech park", "large east-facing balcony", "lake view"]),
    (3, 1380, 13_500_000, "Sarjapur Road", "Bengaluru", "NE", 6, 14, 3500, 8, "unfurnished", "ready_to_move",
     ["gym", "park", "children's play area", "power backup"], "Sobha Dream Acres", True,
     ["near Wipro & RMZ Ecospace", "good schools nearby", "quiet green campus"]),
    (3, 1600, 16_800_000, "Koramangala", "Bengaluru", "W", 3, 5, 6500, 5, "fully_furnished", "ready_to_move",
     ["lift", "security", "covered parking"], None, False, ["walk to startup offices", "premium interiors"]),
    (2, 1050, 9_200_000, "Electronic City", "Bengaluru", "E", 11, 20, 2400, 8, "semi_furnished", "ready_to_move",
     ["gym", "swimming pool", "jogging track"], "Godrej E-City", True, ["next to Infosys campus", "metro 1 km"]),
    (3, 1520, 14_800_000, "Hebbal", "Bengaluru", "N", 14, 22, 5200, 9, "semi_furnished", "under_construction",
     ["gym", "swimming pool", "clubhouse", "ev charging"], "Brigade Insignia", False,
     ["Manyata tech park 10 min", "airport road", "possession Dec 2026"]),
    (3, 1400, 12_900_000, "Indiranagar", "Bengaluru", "S", 2, 4, 3000, 4, "unfurnished", "ready_to_move",
     ["security", "covered parking"], None, False, ["metro 500 m", "cafe street"]),
    (3, 1350, 11_500_000, "Hinjewadi", "Pune", "E", 7, 15, 3200, 8, "semi_furnished", "ready_to_move",
     ["gym", "swimming pool", "clubhouse"], "Kolte Patil Life Republic", True, ["Rajiv Gandhi Infotech Park 5 min"]),
    (2, 980, 7_800_000, "Kharadi", "Pune", "NE", 5, 12, 2600, 7, "fully_furnished", "ready_to_move",
     ["gym", "power backup", "security"], "Gera World of Joy", False, ["EON IT park walkable"]),
    (3, 1700, 14_900_000, "Gachibowli", "Hyderabad", "E", 12, 30, 4500, 9, "semi_furnished", "ready_to_move",
     ["gym", "swimming pool", "clubhouse", "tennis court"], "My Home Avatar", True,
     ["Financial District 5 min", "open views", "gated community"]),
    (4, 2400, 24_500_000, "Kokapet", "Hyderabad", "NE", 20, 40, 7800, 9, "unfurnished", "under_construction",
     ["gym", "swimming pool", "clubhouse", "co-working"], "Rajapushpa Provincia", False, ["skyline views"]),
    (3, 1250, 32_000_000, "Powai", "Mumbai", "E", 16, 25, 9000, 8, "semi_furnished", "ready_to_move",
     ["gym", "swimming pool", "clubhouse"], "Hiranandani Gardens", True, ["lake view", "near Powai IT park"]),
    (2, 900, 45_000, "Whitefield", "Bengaluru", "E", 4, 12, 3000, 7, "fully_furnished", "ready_to_move",
     ["gym", "power backup"], "Brigade Metropolis", False, ["for rent", "walk to ITPL"]),
]


def demo_listings() -> list[Listing]:
    now = datetime.now(timezone.utc)
    out = []
    for i, (bhk, area, price, loc, city, facing, floor, total, maint, light, furn, poss, am, proj, ver, hl) in enumerate(_ROWS):
        is_rent = price < 200_000
        title = f"{bhk} BHK {facing}-Facing Apartment in {loc}, {city}"
        x = ListingExtraction(
            transaction_type="rent" if is_rent else "sale", property_type="apartment", bhk=bhk, bathrooms=bhk,
            carpet_area_sqft=area, super_built_up_area_sqft=round(area * 1.3), floor_number=floor, total_floors=total,
            facing=facing, furnishing=furn, price_inr=price, maintenance_monthly_inr=maint, possession_status=poss,
            amenities=am, address=Address(project_name=proj, locality=loc, city=city),
            rera_number="PRM/KA/RERA/1251/446/PR/171031/000001" if ver and city == "Bengaluru" else None,
            natural_light_score=light, highlights=hl, seo_title=title,
            seo_description=f"{title}. {area} sq ft carpet on floor {floor} of {total}. {', '.join(hl)}. "
                            f"Amenities: {', '.join(am)}.",
            seo_keywords=[loc, city, f"{bhk} bhk"],
        )
        verification = None
        if ver:
            verification = VerificationResult(
                status="verified", badge="100% AI-Verified Legal Status", score=1.0, documents=[],
                checks=[VerificationCheck(name="seed", passed=True, detail="demo data")],
                buyer_summary="RERA registration, title deed and occupancy certificate checked; no encumbrances found.")
        created = now - timedelta(days=i * 4)
        out.append(Listing(id=f"lst_demo{i:02d}", owner_id=f"seller_{i:02d}", status="live", data=x,
                           verification=verification, quality_score=0.8, created_at=created, updated_at=created,
                           price_history=[PricePoint(price_inr=price, at=created)]))
    return out
