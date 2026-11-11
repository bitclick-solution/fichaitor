from dataclasses import replace
from datetime import date

from app.adapters.places.google import to_snapshot
from app.domain.diagnosis import Impact, PlaceSnapshot, diagnose

TODAY = date(2026, 11, 11)

COMPLETE = PlaceSnapshot(
    place_id="p1",
    name="Pizzería Completa",
    primary_type="pizza_restaurant",
    primary_type_label="Pizzería",
    types=("pizza_restaurant", "restaurant", "point_of_interest", "establishment"),
    has_regular_hours=True,
    photo_count=10,
    rating=4.6,
    review_count=120,
    latest_review_date=date(2026, 11, 1),
    website="https://ejemplo.es",
    phone="958 00 00 00",
    service_attributes=3,
)


def codes(place: PlaceSnapshot) -> list[str]:
    return [f.code for f in diagnose(place, TODAY).findings]


def test_complete_profile_scores_100_with_no_findings():
    d = diagnose(COMPLETE, TODAY)
    assert d.score == 100
    assert d.label == "Bien"
    assert d.findings == ()


def test_locked_checks_are_always_listed_and_never_scored():
    d = diagnose(COMPLETE, TODAY)
    assert {c.code for c in d.locked_checks} == {
        "unanswered_reviews",
        "holiday_hours",
        "owner_description",
        "google_edits",
    }
    assert d.score == 100


def test_missing_hours_is_high_impact_and_costs_20():
    d = diagnose(replace(COMPLETE, has_regular_hours=False), TODAY)
    assert d.score == 80
    assert d.findings[0].code == "no_hours"
    assert d.findings[0].impact is Impact.HIGH


def test_photo_tiers():
    assert "no_photos" in codes(replace(COMPLETE, photo_count=0))
    assert "few_photos" in codes(replace(COMPLETE, photo_count=3))
    assert "some_photos" in codes(replace(COMPLETE, photo_count=7))
    assert not any("photos" in c for c in codes(replace(COMPLETE, photo_count=10)))


def test_low_rating_uses_spanish_decimal_comma():
    d = diagnose(replace(COMPLETE, rating=3.7), TODAY)
    finding = next(f for f in d.findings if f.code == "low_rating")
    assert "3,7" in finding.title


def test_no_rating_finding_without_reviews():
    assert "low_rating" not in codes(replace(COMPLETE, rating=None, review_count=0))
    assert "few_reviews" in codes(replace(COMPLETE, rating=None, review_count=0))


def test_stale_reviews_only_when_there_are_enough_reviews():
    old = date(2026, 1, 1)
    assert "stale_reviews" in codes(replace(COMPLETE, latest_review_date=old))
    few = replace(COMPLETE, review_count=4, latest_review_date=old)
    assert "few_reviews" in codes(few)
    assert "stale_reviews" not in codes(few)


def test_generic_types_do_not_count_as_secondary_categories():
    only_generic = replace(
        COMPLETE, types=("pizza_restaurant", "point_of_interest", "establishment")
    )
    assert "no_secondary_categories" in codes(only_generic)
    assert "no_secondary_categories" not in codes(COMPLETE)


def test_service_attributes_only_apply_to_food_businesses():
    bar = replace(COMPLETE, primary_type="bar", types=("bar", "restaurant"), service_attributes=0)
    assert "no_service_attributes" in codes(bar)
    shop = replace(
        COMPLETE,
        primary_type="clothing_store",
        types=("clothing_store", "shoe_store"),
        service_attributes=0,
    )
    assert "no_service_attributes" not in codes(shop)


def test_at_most_five_findings_sorted_by_impact_and_score_never_negative():
    empty = PlaceSnapshot(
        place_id="p2",
        name="Vacío",
        operational=False,
        primary_type="bar",
        rating=3.0,
        review_count=5,
    )
    d = diagnose(empty, TODAY)
    assert d.score == 0  # penalties add up to 112; the score is clamped
    assert d.label == "Necesita atención"
    assert len(d.findings) == 5
    impacts = [f.impact for f in d.findings]
    assert impacts == sorted(impacts, key=[Impact.HIGH, Impact.MEDIUM, Impact.LOW].index)
    assert d.findings[0].code == "not_operational"


def test_labels_by_threshold():
    assert diagnose(replace(COMPLETE, photo_count=3), TODAY).label == "Bien"  # 90
    assert diagnose(replace(COMPLETE, has_regular_hours=False), TODAY).label == "Mejorable"  # 80
    worst = replace(COMPLETE, has_regular_hours=False, photo_count=0, website="", phone="")
    assert diagnose(worst, TODAY).label == "Necesita atención"  # 49


def test_google_payload_maps_to_snapshot():
    raw = {
        "id": "ChIJ123",
        "displayName": {"text": "Bar Ejemplo"},
        "formattedAddress": "Calle 1, Granada",
        "businessStatus": "OPERATIONAL",
        "rating": 4.4,
        "userRatingCount": 57,
        "regularOpeningHours": {"periods": [{"open": {"day": 1}}]},
        "photos": [{"name": "a"}, {"name": "b"}],
        "websiteUri": "https://bar.es",
        "primaryType": "bar",
        "primaryTypeDisplayName": {"text": "Bar"},
        "types": ["bar", "establishment"],
        "reviews": [
            {"publishTime": "2026-05-01T10:00:00Z"},
            {"publishTime": "2026-09-20T08:00:00Z"},
        ],
        "outdoorSeating": True,
        "delivery": False,
        "googleMapsUri": "https://maps.google.com/?cid=1",
    }
    s = to_snapshot(raw)
    assert s.name == "Bar Ejemplo"
    assert s.has_regular_hours is True
    assert s.photo_count == 2
    assert s.latest_review_date == date(2026, 9, 20)
    assert s.service_attributes == 1
    assert s.phone == ""


def test_google_payload_with_missing_fields_does_not_crash():
    s = to_snapshot({"id": "x"})
    assert s.review_count == 0
    assert s.has_regular_hours is False
    assert s.operational is True
