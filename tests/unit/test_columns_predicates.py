"""Leg-type predicates and row-index helpers over the twelve real Leg Type strings.

``_leg_is_charge`` / ``_leg_is_stop`` / ``is_trip_leg`` decide the Excel row
colour, whether a point reaches a chart, and whether a row costs an OpenWeather
call — so they are exercised against every Leg Type the pipeline can emit
(``row_builder._get_leg_type`` plus the synthesised ``Stop``).
"""

from __future__ import annotations

import numpy as np
import pytest

from report_generator.columns import (
    DIESEL_HEADERS,
    HEADERS,
    _is_nan,
    _leg_is_charge,
    _leg_is_stop,
    _row_col_index,
    is_trip_leg,
)

#: Every Leg Type the generator can write, with its (charge, stop, trip) class.
LEG_TYPES = [
    # trips
    ("Outbound", False, False, True),
    ("Return", False, False, True),
    ("Round Trip", False, False, True),
    ("In Transit", False, False, True),
    ("In House", False, False, True),
    # stop
    ("Stop", False, True, False),
    # charges
    ("AC Home", True, False, False),
    ("AC Away", True, False, False),
    ("DC Home", True, False, False),
    ("DC Away", True, False, False),
    ("Charge Home", True, False, False),
    ("Charge Away", True, False, False),
]


def test_the_table_covers_twelve_leg_types():
    assert len(LEG_TYPES) == 12
    assert len({lt for lt, *_ in LEG_TYPES}) == 12


@pytest.mark.parametrize("leg_type, is_charge, is_stop, is_trip", LEG_TYPES)
def test_leg_type_classification(leg_type, is_charge, is_stop, is_trip):
    assert _leg_is_charge(leg_type) is is_charge
    assert _leg_is_stop(leg_type) is is_stop
    assert is_trip_leg(leg_type) is is_trip


@pytest.mark.parametrize("leg_type", ["AC/DC Home", "AC/DC Away", "Mix charge"])
def test_mixed_charge_labels_are_charges(leg_type):
    assert _leg_is_charge(leg_type) is True
    assert is_trip_leg(leg_type) is False


def test_estimated_prefix_is_a_charge():
    # 'estimated' is in the charge regex because a SOC-estimated charge event can
    # surface with that prefix.
    assert _leg_is_charge("estimated charge") is True


@pytest.mark.parametrize("leg_type", ["ac home", "dc AWAY", "CHARGE Home", "sToP"])
def test_predicates_are_case_insensitive(leg_type):
    assert _leg_is_charge(leg_type) or _leg_is_stop(leg_type)
    assert is_trip_leg(leg_type) is False


@pytest.mark.parametrize("blank", [None, "", "   ", float("nan"), 0, [], object()])
def test_blank_and_non_string_leg_types_are_nothing(blank):
    assert _leg_is_charge(blank) is False
    assert _leg_is_stop(blank) is False
    assert is_trip_leg(blank) is False


def test_stop_tolerates_surrounding_whitespace():
    assert _leg_is_stop("  Stop  ") is True


# ── _row_col_index ───────────────────────────────────────────────────────────


def test_row_col_index_drops_the_leg_number_column():
    assert HEADERS[0] == "Leg Number"
    assert _row_col_index("Leg Type") == 0
    assert _row_col_index("Operator") == len(HEADERS) - 2


def test_row_col_index_supports_the_diesel_layout():
    assert DIESEL_HEADERS[0] == "Leg Number"
    assert _row_col_index("Leg Type", DIESEL_HEADERS) == 0
    assert _row_col_index("Fuel Used (L)", DIESEL_HEADERS) == (
        DIESEL_HEADERS.index("Fuel Used (L)") - 1
    )
    # The same header sits at DIFFERENT row positions in the two layouts — this
    # is exactly why the patchers must not be pointed at the wrong workbook.
    assert _row_col_index("Average Temperature (C)") != _row_col_index(
        "Average Temperature (C)", DIESEL_HEADERS
    )


def test_row_col_index_rejects_an_unknown_header():
    with pytest.raises(ValueError):
        _row_col_index("No Such Column")


# ── _is_nan ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        (float("nan"), True),
        (np.nan, True),
        (np.float64("nan"), True),
        (0.0, False),
        (1, False),
        (None, False),  # None is "empty", not NaN — the writer treats it apart
        ("Stop", False),
        ([1, 2], False),
    ],
)
def test_is_nan(value, expected):
    assert _is_nan(value) is expected
