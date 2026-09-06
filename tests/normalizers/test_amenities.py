"""
Tests for the MLS amenity blob parsers.

The amenity dict is 706 free-text keys written by dozens of MLS feeds, so the
risk here is not "does it parse" but "does it parse the *right* thing".
Cases below are real values taken from the live corpus.
"""

from aevorex.normalizers.amenities import AMENITY_FIELDS, parse_amenities


def p(**kw):
    return parse_amenities(kw)


# ------------------------------------------------------------------ shape


def test_empty_input_returns_the_full_key_set_as_none():
    for value in (None, {}, "not a dict"):
        out = parse_amenities(value)
        assert set(out) == set(AMENITY_FIELDS)
        assert all(v is None for v in out.values())


def test_every_parse_returns_the_full_key_set():
    assert set(p(Roof="Shingle")) == set(AMENITY_FIELDS)


def test_em_dash_placeholder_is_treated_as_absent():
    # Redfin writes "—" for an unstated value; taking it literally would make
    # every one of these fields look populated.
    assert p(Roof="—", Furnished="—")["roof_material"] is None


# ------------------------------------------------- Florida insurability


def test_masonry_vs_frame_construction():
    # The biggest single driver of Florida wind premiums.
    assert p(**{"Construction Materials": "Block, Stucco"})["construction_class"] == "masonry"
    assert p(**{"Construction Materials": "CBS"})["construction_class"] == "masonry"
    assert p(**{"Construction Materials": "Frame"})["construction_class"] == "frame"


def test_mixed_construction_is_not_recorded_as_masonry():
    # Wind rating is limited by the weakest element, so a building that is
    # part frame must not be credited as full masonry.
    assert p(**{"Construction Materials": "Concrete Block, Wood Frame"})["construction_class"] == "mixed"


def test_construction_falls_back_across_feed_variants():
    # 'Construction Materials' is 60.3% filled but coalescing with the other
    # spellings lifts real coverage to 94.1%.
    assert p(**{"Construction Details": "Block"})["construction_class"] == "masonry"
    assert p(Construction="Concrete Block, Wood Frame")["construction_class"] == "mixed"


def test_roof_material_and_insurance_class():
    assert p(Roof="Shingle")["roof_class"] == "standard"
    assert p(Roof="Spanish Tile")["roof_class"] == "premium"
    assert p(Roof="Metal")["roof_class"] == "premium"
    assert p(Roof="Flat")["roof_class"] == "flat"


def test_combined_roof_value_records_the_longer_lived_material():
    # "Concrete, Tile" is a concrete roof; recording it as the shorter-lived
    # material would understate both its life and its insurability.
    assert p(Roof="Concrete, Tile")["roof_material"] == "concrete"
    assert p(Roof="Composition, Shingle")["roof_class"] == "standard"


def test_impact_glazing_detected_from_either_field():
    assert p(**{"Window Features": "Blinds, Impact Glass"})["has_impact_glazing"] is True
    assert p(**{"Storm Protection": "Impact Resistant Doors, Impact Resistant Windows"})["has_impact_glazing"] is True
    assert p(**{"Window Features": "Blinds"})["has_impact_glazing"] is False


def test_shutters_are_distinct_from_impact_glazing():
    # Shutters earn a smaller wind-mitigation credit than fixed impact glass
    # and have to be deployed by hand before a storm.
    out = p(**{"Storm Protection": "Shutters Manual"})
    assert out["has_storm_shutters"] is True
    assert out["has_impact_glazing"] is False


def test_glazing_is_none_when_nothing_was_stated():
    assert p(Roof="Tile")["has_impact_glazing"] is None


# ------------------------------------------------------------- condition


def test_condition_classes():
    assert p(**{"Property Condition": "Fixer"})["condition_class"] == "fixer"
    assert p(**{"Property Condition": "Resale, Updated/Remodeled"})["condition_class"] == "updated"
    assert p(**{"Property Condition": "New Construction"})["condition_class"] == "new"
    assert p(**{"Property Condition": "Resale"})["condition_class"] == "standard"


# ------------------------------------------------------- rental strategy


def test_furnished_levels_are_ranked():
    assert p(Furnished="Turnkey")["furnished_level"] == "turnkey"
    assert p(Furnished="Unfurnished")["furnished_rank"] == 0.0
    assert p(Furnished="Turnkey")["furnished_rank"] > p(Furnished="Negotiable")["furnished_rank"]


def test_lease_restriction_flag_parses_mls_booleans():
    assert p(**{"Lease Restrictions YN": "1"})["lease_restricted"] is True
    assert p(**{"Lease Restrictions YN": "0"})["lease_restricted"] is False
    assert p()["lease_restricted"] is None


def test_unstated_flag_is_none_not_false():
    # "not stated" and "stated as no" must stay distinguishable, or a silent
    # listing gets penalised as if it had declared a restriction.
    out = p(Roof="Tile")
    assert out["pets_allowed"] is None
    assert out["has_cdd"] is None


def test_pets_allowed_handles_qualified_yes():
    assert p(**{"Pets Allowed": "Yes, Breed Restrictions"})["pets_allowed"] is True
    assert p(**{"Pets Allowed": "No"})["pets_allowed"] is False


def test_minimum_lease_term_units_are_converted_to_months():
    assert p(**{"Minimum Lease Term": "6 Months"})["min_lease_months"] == 6
    assert p(**{"Minimum Lease Term": "1 Year"})["min_lease_months"] == 12
    assert p(**{"Minimum Lease Term": "7 Days"})["min_lease_months"] == 1


# --------------------------------------------------------- pool / water


def test_association_pool_is_not_a_private_pool():
    # 708 live properties say "Association" here. A shared pool is not a
    # nightly-rental booking driver and must not be scored as one.
    out = p(**{"Pool Features": "Association"})
    assert out["has_private_pool"] is False
    assert out["has_community_pool"] is True


def test_in_ground_pool_is_private():
    out = p(**{"Pool Features": "Heated, In Ground"})
    assert out["has_private_pool"] is True
    assert out["has_community_pool"] is False


def test_explicit_private_pool_flag_wins_over_text():
    out = p(**{"Pool Features": "Association", "POOL_PRIVATE_YN": "Has Private Pool"})
    assert out["has_private_pool"] is True


def test_waterfront_and_view_flags():
    assert p(**{"Waterfront YN": "Yes"})["is_waterfront"] is True
    assert p(**{"WATERFRONT_YN": "On Waterfront"})["is_waterfront"] is True
    assert p(**{"Water View Y/N": "1"})["has_water_view"] is True


# ------------------------------------------------------- carrying costs


def test_hoa_frequency_is_converted_to_monthly():
    assert p(**{"Association Fee": "$1,200", "Association Fee Frequency": "Annually"})["hoa_monthly_amenity"] == 100
    assert p(**{"Association Fee": "$300", "Association Fee Frequency": "Quarterly"})["hoa_monthly_amenity"] == 100
    assert p(**{"Association Fee": "$250", "Association Fee Frequency": "Monthly"})["hoa_monthly_amenity"] == 250


def test_monthly_specific_field_is_preferred():
    out = p(**{"Monthly HOA Amount": "662", "Association Fee": "$7944",
               "Association Fee Frequency": "Annually"})
    assert out["hoa_monthly_amenity"] == 662


def test_unstated_frequency_declines_rather_than_guessing():
    # Assuming monthly on an annual fee overstates carrying cost 12x, which is
    # worse than having no number. ~8.7% of fee-bearing rows omit frequency.
    assert p(**{"Association Fee": "$1,200"})["hoa_monthly_amenity"] is None


def test_implausible_hoa_is_rejected():
    # One live row reads $215,567/mo against a $1.325M condo.
    assert p(**{"Monthly HOA Amount": "215567"})["hoa_monthly_amenity"] is None


def test_cdd_flag():
    # A Florida CDD bond is an annual assessment on top of taxes and HOA.
    assert p(**{"CDD Y/N": "1"})["has_cdd"] is True
    assert p(**{"CDD Y/N": "0"})["has_cdd"] is False
