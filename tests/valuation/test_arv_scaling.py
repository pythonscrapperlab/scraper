"""
ARV must be earned by an actual renovation.

Found in live output: ARV exceeded as-is market value by ~7% even for
properties in "new" condition, and a 2022-built townhouse showed a 15.6%
uplift and a 73% "return" on work that does not exist. The comp p75 was being
applied unconditionally as ARV.
"""

import pytest

from aevorex.valuation.comps import (
    DEFAULT_RENOVATION_CAPTURE,
    RENOVATION_CAPTURE,
    scale_arv_by_condition,
)

VALUE = 500_000.0
CEILING = 600_000.0   # comp-set p75


def test_a_new_property_gains_almost_nothing():
    arv = scale_arv_by_condition(VALUE, CEILING, "new")
    assert arv < VALUE * 1.06, "there is nothing to renovate in a new build"


def test_a_fixer_can_reach_the_full_comp_ceiling():
    assert scale_arv_by_condition(VALUE, CEILING, "fixer") == pytest.approx(CEILING, abs=1)


def test_capture_is_ordered_by_how_much_work_is_needed():
    arvs = [
        scale_arv_by_condition(VALUE, CEILING, c)
        for c in ("new", "updated", "standard", "fixer")
    ]
    assert arvs == sorted(arvs), "more work to do means more upside available"


def test_unknown_condition_is_conservative():
    unknown = scale_arv_by_condition(VALUE, CEILING, None)
    standard = scale_arv_by_condition(VALUE, CEILING, "standard")
    assert unknown < standard, "absent evidence of work needed, assume less upside"
    assert DEFAULT_RENOVATION_CAPTURE < RENOVATION_CAPTURE["standard"]


def test_arv_never_falls_below_as_is_value():
    # A comp set whose p75 sits below our as-is estimate must not imply that
    # renovating makes the property worth less.
    assert scale_arv_by_condition(500_000.0, 450_000.0, "fixer") == 500_000


def test_missing_value_falls_back_to_the_ceiling():
    assert scale_arv_by_condition(None, CEILING, "fixer") == CEILING


def test_missing_ceiling_yields_none():
    assert scale_arv_by_condition(VALUE, None, "fixer") is None
