"""Per-leg operator resolution: the round-robin regex, static SRF org mapping and
the four-step cascade.

The operator code is written into the last column of every report row and is what
downstream per-operator splits key on, so the cascade order (trial > srf_org >
config > none) and the ``source`` tag are contract, not detail. Legs are
``unittest.mock.Mock`` objects — no SRF call is made (``leg.trip.uri`` is a stored
URI, and the memoisation cache is exercised directly).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from report_generator import operators as ops


def _leg(description=None, trip_uri="trip://1", start_time=None):
    """A minimal SRF leg stand-in exposing only what the cascade reads."""
    leg = Mock()
    leg.trip.uri = trip_uri
    leg.trip.trial.description = description
    leg.start_time = start_time
    return leg


# ── operator_from_trial_description ──────────────────────────────────────────


@pytest.mark.parametrize(
    "description, expected",
    [
        ("JOLT Round Robin: DP World-Scania", "DP_WORLD"),
        ("JOLT Round Robin: WJF-DAF", "WJF"),
        ("JOLT Round Robin: Welch-Volvo", "WELCH_TRANSPORT"),
        ("JOLT Round Robin: JLP-Daimler", "JLP"),
        ("JOLT Round Robin: WS-Mercedes", "WS"),
        ("JOLT Round Robin: SJG-Scania", "SJG"),
        ("JOLT Round Robin: Port Express-Daimler", "PORT_EXPRESS_DAIMLER"),
        ("JOLT Round Robin: HTL-Volvo", "HTL"),
    ],
)
def test_round_robin_tokens_map_to_curated_codes(description, expected):
    assert ops.operator_from_trial_description(description) == expected


def test_round_robin_match_is_case_and_whitespace_tolerant():
    assert (
        ops.operator_from_trial_description("  jolt round robin: jlp-volvo  ") == "JLP"
    )
    assert (
        ops.operator_from_trial_description("JOLT  Round  Robin: JLP - Volvo") == "JLP"
    )


def test_uncurated_token_is_upper_snaked():
    code = ops.operator_from_trial_description(
        "JOLT Round Robin: New Haulier Ltd-Volvo"
    )
    assert code == "NEW_HAULIER_LTD"
    assert code not in ops.KNOWN_OPERATOR_CODES


@pytest.mark.parametrize(
    "description",
    [
        None,
        "",
        "JOLT Nestle-Volvo",  # dedicated vehicle: not a round-robin description
        "JOLT Round Robin: JLP-Tesla",  # OEM outside the enumerated set
        "Round Robin: JLP-Volvo",  # missing the JOLT prefix
        123,
    ],
)
def test_non_round_robin_descriptions_do_not_resolve(description):
    assert ops.operator_from_trial_description(description) is None


# ── normalize_srf_org / is_generic_srf_org ───────────────────────────────────


@pytest.mark.parametrize(
    "org, expected",
    [
        ("JOLT Nestle-Volvo", "NESTLE"),
        ("JOLT Knowles-Volvo", "KNOWLES"),
        ("John Lewis Partnership", "JLP"),
        ("JOLT JLP-Volvo", "JLP"),
        ("Welch Group", "WELCH_TRANSPORT"),
        ("JOLT Welch-Volvo", "WELCH_TRANSPORT"),
        ("DP World", "DP_WORLD"),
        ("William Jackson Food", "WJF"),
        ("  william jackson food  ", "WJF"),
    ],
)
def test_normalize_srf_org_maps_dedicated_vehicles(org, expected):
    assert ops.normalize_srf_org(org) == expected


@pytest.mark.parametrize("org", [None, "", "   ", "JOLT Partners", "jolt partners"])
def test_normalize_srf_org_returns_none_for_generic_or_missing(org):
    assert ops.normalize_srf_org(org) is None


def test_normalize_srf_org_unknown_company_is_not_invented():
    # An uncurated org must NOT be auto-coded: onboarding curates the code.
    assert ops.normalize_srf_org("Some New Haulier Ltd") is None


@pytest.mark.parametrize(
    "org, expected",
    [
        ("JOLT Partners", True),
        ("  jolt partners ", True),
        ("", True),
        (None, True),
        ("DP World", False),
    ],
)
def test_is_generic_srf_org(org, expected):
    assert ops.is_generic_srf_org(org) is expected


def test_every_curated_mapping_target_is_a_known_code():
    for code in {**ops._TRIAL_OP_TO_CODE, **ops._SRF_ORG_TO_CODE}.values():
        assert code in ops.KNOWN_OPERATOR_CODES


# ── derive_leg_operator cascade ──────────────────────────────────────────────


def test_cascade_step_1_trial_description_wins():
    code, source, unknown = ops.derive_leg_operator(
        _leg("JOLT Round Robin: SJG-Scania"),
        "EVSPD01",
        srf_org_raw="JOLT Nestle-Volvo",  # would resolve to NESTLE if reached
        vehicles={"EVSPD01": {"operator": "HTL"}},
    )
    assert (code, source, unknown) == ("SJG", "trial", False)


def test_cascade_step_2_static_srf_org():
    code, source, unknown = ops.derive_leg_operator(
        _leg("JOLT Nestle-Volvo"),  # not a round-robin description
        "EVSPD01",
        srf_org_raw="JOLT Nestle-Volvo",
        vehicles={"EVSPD01": {"operator": "HTL"}},
    )
    assert (code, source, unknown) == ("NESTLE", "srf_org", False)


def test_cascade_step_3_config_single_operator():
    code, source, unknown = ops.derive_leg_operator(
        _leg(None),
        "EVSPD01",
        srf_org_raw="JOLT Partners",  # generic umbrella, no signal
        vehicles={"EVSPD01": {"operator": " HTL "}},
    )
    assert (code, source, unknown) == ("HTL", "config", False)


def test_cascade_step_4_undeterminable():
    code, source, unknown = ops.derive_leg_operator(
        _leg(None), "EVSPD01", srf_org_raw="JOLT Partners", vehicles={}
    )
    assert (code, source, unknown) == (None, "none", True)


def test_cascade_flags_an_uncurated_code_as_unknown():
    code, source, unknown = ops.derive_leg_operator(
        _leg("JOLT Round Robin: New Haulier-Volvo"), "EVSPD01"
    )
    assert (code, source, unknown) == ("NEW_HAULIER", "trial", True)


def test_config_time_ranged_operators_pick_the_covering_window():
    import pandas as pd

    vehicles = {
        "EVSPD01": {
            "operators": [
                {"code": "JLP", "from": None, "to": "2025-01-01"},
                {"code": "HTL", "from": "2025-01-01", "to": None},
            ]
        }
    }
    leg = _leg(None, start_time=pd.Timestamp("2025-06-27T08:00:00Z"))
    code, source, _ = ops.derive_leg_operator(
        leg, "EVSPD01", srf_org_raw=None, vehicles=vehicles
    )
    assert (code, source) == ("HTL", "config")


def test_config_time_ranged_operators_fall_back_to_the_first_entry():
    # A leg whose start_time cannot be parsed takes the first valid entry.
    vehicles = {"EVSPD01": {"operators": [{"operator": "JLP"}, {"code": "HTL"}]}}
    code, source, _ = ops.derive_leg_operator(_leg(None), "EVSPD01", vehicles=vehicles)
    assert (code, source) == ("JLP", "config")


def test_config_lookup_is_skipped_without_a_registration_or_vehicles():
    assert (
        ops.derive_leg_operator(_leg(None), None, vehicles={"X": {"operator": "JLP"}})[
            1
        ]
        == "none"
    )
    assert ops.derive_leg_operator(_leg(None), "EVSPD01", vehicles=None)[1] == "none"


def test_trial_description_is_memoised_by_trip_uri():
    cache: dict = {}
    leg = _leg("JOLT Round Robin: JLP-Volvo", trip_uri="trip://shared")
    ops.derive_leg_operator(leg, "EVSPD01", trial_cache=cache)
    assert cache == {"trip://shared": "JOLT Round Robin: JLP-Volvo"}

    # A second leg on the same trip reads the cache; its own (different)
    # description is never consulted.
    other = _leg("JOLT Round Robin: HTL-Volvo", trip_uri="trip://shared")
    code, source, _ = ops.derive_leg_operator(other, "EVSPD01", trial_cache=cache)
    assert (code, source) == ("JLP", "trial")


def test_cascade_survives_a_leg_that_raises_on_attribute_access():
    leg = Mock()
    type(leg).trip = property(
        lambda self: (_ for _ in ()).throw(AttributeError("no trip"))
    )
    code, source, unknown = ops.derive_leg_operator(leg, "EVSPD01")
    assert (code, source, unknown) == (None, "none", True)
