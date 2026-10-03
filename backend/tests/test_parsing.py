import pytest

from app.core.heuristics import parse_listing_text, parse_money, parse_query
from app.core.llm import strict_json_schema
from app.schemas import DocumentExtraction, ListingExtraction, ParsedQuery


@pytest.mark.parametrize("text,expected", [
    ("₹1.5 Cr", 15_000_000), ("85 lakh", 8_500_000), ("95L", 9_500_000), ("rent 45k", 45_000),
    ("Rs. 2.25 crore", 22_500_000), ("1,20 lacs", 12_000_000),
])
def test_parse_money(text, expected):
    assert parse_money(text) == pytest.approx(expected)


def test_parse_listing_text_hinglish_notes():
    x = parse_listing_text("2 bhk flat in Kharadi Pune, 950 sq ft carpet, 5th floor out of 12 floors, "
                           "north-east facing, 78 lakh, maintenance 2.5k, fully furnished, ready to move")
    assert (x.bhk, x.carpet_area_sqft, x.floor_number, x.total_floors) == (2, 950, 5, 12)
    assert x.facing == "NE" and x.price_inr == 7_800_000 and x.maintenance_monthly_inr == 2500
    assert x.address.city == "Pune" and x.address.locality == "Kharadi"
    assert x.furnishing == "fully_furnished" and x.possession_status == "ready_to_move"
    assert x.missing_fields == []


def test_parse_listing_area_units():
    assert parse_listing_text("plot of 200 sq yd in Noida 1 cr").built_up_area_sqft is None
    x = parse_listing_text("villa 100 sqm built-up, Pune, 2 Cr, 3 bhk")
    assert x.built_up_area_sqft == pytest.approx(1076.4, rel=1e-3)


def test_parse_query_spec_example():
    q = parse_query("Show me a sunlit 3BHK near tech hubs under ₹1.5 Cr with low maintenance and east facing")
    f = q.filters
    assert (f.bhk_min, f.bhk_max, f.price_max_inr, f.facing) == (3, 3, 15_000_000, ["E"])
    assert set(q.soft_preferences) == {"abundant natural light", "near tech parks", "low maintenance"}


def test_parse_query_range_and_rent():
    f = parse_query("2-3 bhk for rent in Gachibowli between 40k and 60k").filters
    assert (f.bhk_min, f.bhk_max, f.transaction_type) == (2, 3, "rent")
    assert (f.price_min_inr, f.price_max_inr) == (40_000, 60_000)
    assert f.cities == ["Hyderabad"] and f.localities == ["Gachibowli"]


@pytest.mark.parametrize("model", [ListingExtraction, DocumentExtraction, ParsedQuery])
def test_strict_schema_is_structured_output_compatible(model):
    schema = strict_json_schema(model)

    def walk(node):
        if isinstance(node, dict):
            assert "$ref" not in node and "default" not in node
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
