"""Valuation v2: comp locality, type weighting, winner's-curse shrink, era systems."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from aevorex.valuation.comps import (
    address_looks_attached,
    comp_zip,
    estimate_value_and_arv,
    select_comps,
)
from aevorex.valuation.engine import ValuationEngine
from aevorex.valuation.rehab import estimate_rehab


def comp(address, price, sqft, days=60, beds=3, baths=2.0):
    return SimpleNamespace(
        comp_address=address, price=price, sqft=sqft,
        sold_date=datetime.now() - timedelta(days=days), bedrooms=beds, bathrooms=baths,
    )


def subject(**overrides):
    base = dict(sqft=1800, bedrooms=3, bathrooms=2.0, zip_code="95129",
                property_type="Townhouse", price=1_998_000, avm_value=None,
                is_reo=None, is_foreclosure=None, is_short_sale=None,
                is_probate_or_estate=None, is_auction=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_comp_zip_and_unit_marker_parsing():
    assert comp_zip("11221 Bubb Rd, Cupertino, CA 95014") == "95014"
    assert comp_zip("1018 Lancer Dr, San Jose, CA 95129-1234") == "95129"
    assert comp_zip(None) is None
    assert address_looks_attached("842 Meridian Ave Unit 3D, Miami Beach, FL 33139") is True
    assert address_looks_attached("1018 Lancer Dr, San Jose, CA 95129") is False


def test_other_zip_comps_are_dropped_when_enough_local_ones_exist():
    # The live defect: four Cupertino houses valued a San Jose townhouse at $3.6M.
    comps = [
        comp("11221 Bubb Rd, Cupertino, CA 95014", 3_650_000, 1766),
        comp("7422 Fallenleaf Ln, Cupertino, CA 95014", 3_500_000, 1709),
        comp("873 Cottonwood Dr, Cupertino, CA 95014", 3_350_000, 1569),
        comp("1018 Lancer Dr, San Jose, CA 95129", 2_000_000, 1800),
        comp("1099 Miller Ave, San Jose, CA 95129", 1_950_000, 1750),
        comp("1200 Kiely Blvd, San Jose, CA 95129", 2_050_000, 1850),
    ]
    flags = []
    kept = select_comps(comps, 1800, subject_zip="95129", subject_attached=True, flags=flags)
    assert all(c["zip"] == "95129" for c in kept)
    assert "comps_from_other_zips_dropped" in flags


def test_other_zip_comps_are_downweighted_when_local_set_is_thin():
    comps = [
        comp("11221 Bubb Rd, Cupertino, CA 95014", 3_650_000, 1766),
        comp("7422 Fallenleaf Ln, Cupertino, CA 95014", 3_500_000, 1709),
        comp("1018 Lancer Dr, San Jose, CA 95129", 2_000_000, 1800),
    ]
    flags = []
    kept = select_comps(comps, 1800, subject_zip="95129", subject_attached=False, flags=flags)
    assert len(kept) == 3
    assert {c["locality_weight"] for c in kept if c["zip"] == "95014"} == {0.4}
    assert "comps_include_other_zips_downweighted" in flags


def test_detached_subject_with_unit_comps_is_flagged_as_type_mismatch():
    comps = [
        comp("100 Ocean Dr Unit 5A, Miami Beach, FL 33139", 500_000, 900),
        comp("200 Ocean Dr #12, Miami Beach, FL 33139", 520_000, 950),
        comp("300 Ocean Dr Apt 3, Miami Beach, FL 33139", 480_000, 880),
        comp("400 Palm Ave, Miami Beach, FL 33139", 1_500_000, 1000),
    ]
    flags = []
    select_comps(comps, 1000, subject_zip="33139", subject_attached=False, flags=flags)
    assert "comp_property_type_mismatch_suspected" in flags


def test_avm_gets_at_least_half_the_vote_when_comps_diverge_sharply():
    comps = [comp(f"{i} Norman Ave, San Jose, CA 95126", 1_600_000, 1350) for i in range(6)]
    subj = subject(sqft=1343, zip_code="95126", property_type="Townhouse", price=948_000)
    result = estimate_value_and_arv(comps, subj, source_avm=988_660.0)
    assert "comp_value_diverges_from_avm" in result["flags"]
    # Midpoint of ~1.6M comps and ~0.99M AVM, not comp-dominated.
    assert result["market_value"] <= (1_600_000 + 988_660) / 2 + 20_000


def test_uncorroborated_value_far_above_asking_is_shrunk_toward_asking():
    valuation = {"market_value": 3_600_000, "market_value_method": "comps", "valuation_confidence": 0.5}
    flags = []
    shrunk = ValuationEngine._shrink_uncorroborated_value(
        subject(price=2_000_000), 3_600_000, "standard", valuation, flags)
    assert shrunk == 2_800_000
    assert valuation["market_value_method"] == "comps_shrunk"
    assert valuation["valuation_confidence"] == 0.25
    assert "value_far_above_asking_uncorroborated_shrunk_toward_asking" in flags


def test_corroborated_value_is_left_alone():
    for prop in (
        subject(price=2_000_000, avm_value=3_400_000),     # AVM agrees it is cheap
        subject(price=2_000_000, is_short_sale=True),      # verified distress
    ):
        valuation = {"market_value": 3_000_000, "market_value_method": "comps", "valuation_confidence": 0.5}
        flags = []
        kept = ValuationEngine._shrink_uncorroborated_value(prop, 3_000_000, "standard", valuation, flags)
        assert kept == 3_000_000
        assert "value_far_above_asking_but_corroborated" in flags
    valuation = {"market_value": 3_000_000, "market_value_method": "comps", "valuation_confidence": 0.5}
    assert ValuationEngine._shrink_uncorroborated_value(
        subject(price=2_000_000), 3_000_000, "fixer", valuation, []) == 3_000_000


def test_arv_is_capped_at_the_market_exit_ceiling():
    flags = []
    market = {"p75_sold_ppsf": 250.0}
    capped = ValuationEngine._cap_arv_at_market_exit(subject(sqft=1500), 600_000, market, flags)
    assert capped == 250 * 1.25 * 1500
    assert "arv_capped_at_market_exit_ceiling" in flags
    assert ValuationEngine._cap_arv_at_market_exit(subject(sqft=1500), 400_000, market, []) == 400_000


def test_era_dated_systems_are_priced_into_rehab_unless_stated_done():
    old = estimate_rehab(sqft=1500, year_built=1968, condition_class="standard")
    adders = old["basis"]["adders"]
    assert "cast_iron_drain_repipe" in adders
    assert "electrical_panel_and_wiring" in adders
    assert "polybutylene_repipe" not in adders

    poly = estimate_rehab(sqft=1500, year_built=1988, condition_class="standard")
    assert "polybutylene_repipe" in poly["basis"]["adders"]

    done = estimate_rehab(sqft=1500, year_built=1968, condition_class="standard",
                          description="Fully re-piped in 2021 with a new 200-amp panel.")
    assert "cast_iron_drain_repipe" not in done["basis"]["adders"]
    assert "electrical_panel_and_wiring" not in done["basis"]["adders"]

    renovated = estimate_rehab(sqft=1500, year_built=1968, year_renovated=2020, condition_class="standard")
    assert "cast_iron_drain_repipe" not in renovated["basis"]["adders"]


def test_attached_units_carry_half_the_plumbing_scope():
    house = estimate_rehab(sqft=1500, year_built=1968, condition_class="standard",
                           property_type="Single Family Residential")
    condo = estimate_rehab(sqft=1500, year_built=1968, condition_class="standard",
                           property_type="Condo/Co-op")
    assert condo["basis"]["adders"]["cast_iron_drain_repipe"] == house["basis"]["adders"]["cast_iron_drain_repipe"] / 2


def test_multi_family_never_gets_a_modelled_rent():
    from aevorex.valuation.rent import estimate_rent
    index = {("33130", 4): (18_950.0, 12, 2_600.0)}
    result = estimate_rent(source_rent=None, zip_code="33130", bedrooms=4, price=1_450_000,
                           market={"median_gross_yield": 0.07, "confidence": 1.0},
                           rent_index=index, property_type="Multi-Family (2-4 Unit)", sqft=2_400)
    assert result["monthly"] is None
    assert "multi_family_rent_not_available_excluded_from_income_scoring" in result["flags"]


def test_zip_bedroom_rent_is_size_adjusted_and_clamped():
    from aevorex.valuation.rent import estimate_rent
    index = {("33139", 1): (3_200.0, 30, 900.0)}
    studio = estimate_rent(source_rent=None, zip_code="33139", bedrooms=1, price=172_000,
                           market=None, rent_index=index, property_type="Condo/Co-op", sqft=470)
    typical = estimate_rent(source_rent=None, zip_code="33139", bedrooms=1, price=172_000,
                            market=None, rent_index=index, property_type="Condo/Co-op", sqft=900)
    huge = estimate_rent(source_rent=None, zip_code="33139", bedrooms=1, price=172_000,
                         market=None, rent_index=index, property_type="Condo/Co-op", sqft=5_000)
    assert studio["monthly"] < typical["monthly"] == 3_200.0
    # 470/900 = 0.52 sits below the 0.6 floor, so the clamp applies.
    assert studio["monthly"] == round(3_200.0 * 0.6 ** 0.6, 2)
    assert huge["monthly"] == round(3_200.0 * 1.5 ** 0.6, 2), "clamped at 1.5x"
    assert "rent_size_adjusted_from_zip_bedroom_median" in studio["flags"]
