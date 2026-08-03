"""End-to-end segmentation over the committed anonymised raw fixtures.

Two layers of assertion:

1. **Contract** — the shape a downstream consumer relies on: segment counts,
   chronological order, the required keys of a segment dict, the allowed
   ``energy_source`` values, the SOC sign convention and the anchor ordering.
2. **Golden** — every field of every segment is compared against a frozen JSON
   snapshot in ``tests/fixtures/expected/``. That is what catches an unintended
   change to the segmentation maths. Regenerate deliberately with
   ``python tests/fixtures/regenerate_goldens.py`` and review the diff.

Fully offline: the fixtures are committed CSVs and the configs are the frozen
alias copies, so neither an SRF outage nor a live-config retune can move these
numbers.
"""

from __future__ import annotations

import pandas as pd
import pytest

from jolt_toolkit.report_generator.segmentation.mass_clustering import (
    _enforce_anchor_ordering,
)

# Keys every segment of a kind must carry (the intersection over the fleet
# fixtures; ``motion_duration_s`` is deliberately excluded because the
# mass-cluster split re-creates segments without it).
CHARGE_KEYS = {
    "start_time",
    "end_time",
    "start_soc",
    "end_soc",
    "delta_soc_pct",
    "delta_energy_kwh",
    "energy_source",
    "delta_moving_kwh",
    "effective_capacity_kwh",
    "charge_type",
    "ac_start_wh",
    "ac_end_wh",
    "dc_start_wh",
    "dc_end_wh",
    "_anchor_start_time",
    "_anchor_end_time",
    "_anchor_start_rel_kwh",
    "_anchor_end_rel_kwh",
}
DISCHARGE_KEYS = {
    "start_time",
    "end_time",
    "start_soc",
    "end_soc",
    "delta_soc_pct",
    "delta_energy_kwh",
    "energy_source",
    "delta_moving_kwh",
    "effective_capacity_kwh",
    "odo_start_km",
    "odo_end_km",
    "lat_start",
    "lon_start",
    "lat_end",
    "lon_end",
    "_anchor_start_time",
    "_anchor_end_time",
    "_anchor_start_rel_kwh",
    "_anchor_end_rel_kwh",
}

CHARGE_SOURCES = {"ac_dc", "soc_estimate"}
DISCHARGE_SOURCES = {"total_energy", "moving_energy", "soc_estimate"}

#: alias -> (branch, expected charge count, expected discharge count,
#:           the single energy source each kind resolves to on this fixture)
EXPECTATIONS = {
    "EVSPD01": ("speed", 4, 12, "ac_dc", "total_energy"),
    "EVSOC01": ("soc", 3, 6, "soc_estimate", "soc_estimate"),
    "EVMAD01": ("speed", 3, 10, "soc_estimate", "moving_energy"),
}


def _naive(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_convert(None) if ts.tzinfo is not None else ts


@pytest.fixture(params=sorted(EXPECTATIONS))
def alias(request):
    return request.param


@pytest.fixture
def segments(alias, run_fixture_segmentation):
    charge, discharge = run_fixture_segmentation(alias)
    return alias, charge, discharge


# ── Contract ─────────────────────────────────────────────────────────────────


def test_segment_counts(segments):
    alias, charge, discharge = segments
    _branch, n_charge, n_discharge, _cs, _ds = EXPECTATIONS[alias]
    assert len(charge) == n_charge
    assert len(discharge) == n_discharge


def test_the_pipeline_branch_is_the_one_the_frozen_config_selects(
    alias, frozen_configs
):
    branch = EXPECTATIONS[alias][0]
    pipeline = frozen_configs["vehicles"][alias]["pipeline"]
    assert frozen_configs["pipelines"][pipeline]["branch"] == branch


def test_charge_segments_carry_every_required_key(segments):
    _alias, charge, _discharge = segments
    for seg in charge:
        assert CHARGE_KEYS <= set(seg), f"missing: {CHARGE_KEYS - set(seg)}"


def test_discharge_segments_carry_every_required_key(segments):
    _alias, _charge, discharge = segments
    for seg in discharge:
        assert DISCHARGE_KEYS <= set(seg), f"missing: {DISCHARGE_KEYS - set(seg)}"


def test_segments_are_in_chronological_order(segments):
    _alias, charge, discharge = segments
    for group in (charge, discharge):
        starts = [_naive(s["start_time"]) for s in group]
        assert starts == sorted(starts)


def test_every_segment_spans_forward_in_time(segments):
    _alias, charge, discharge = segments
    for seg in [*charge, *discharge]:
        assert _naive(seg["start_time"]) <= _naive(seg["end_time"])


def test_energy_sources_come_from_the_allowed_sets(segments):
    alias, charge, discharge = segments
    _b, _nc, _nd, charge_src, discharge_src = EXPECTATIONS[alias]
    assert {s["energy_source"] for s in charge} <= CHARGE_SOURCES
    assert {s["energy_source"] for s in discharge} <= DISCHARGE_SOURCES
    # ... and this fixture resolves to one specific source per kind.
    assert {s["energy_source"] for s in charge} == {charge_src}
    assert {s["energy_source"] for s in discharge} == {discharge_src}


def test_soc_and_energy_signs_follow_the_charge_discharge_convention(segments):
    _alias, charge, discharge = segments
    for seg in charge:
        assert seg["delta_soc_pct"] > 0
        assert seg["delta_energy_kwh"] > 0
        assert seg["end_soc"] > seg["start_soc"]
    for seg in discharge:
        assert seg["delta_soc_pct"] < 0
        assert seg["delta_energy_kwh"] < 0
        assert seg["end_soc"] < seg["start_soc"]


def test_effective_capacity_is_physically_plausible(segments):
    _alias, charge, discharge = segments
    for seg in [*charge, *discharge]:
        cap = seg["effective_capacity_kwh"]
        if cap is None:
            continue
        assert 50.0 < cap < 1500.0, f"implausible implied capacity {cap} kWh"


def test_odometer_never_runs_backwards_within_a_trip(segments):
    _alias, _charge, discharge = segments
    for seg in discharge:
        o_s, o_e = seg["odo_start_km"], seg["odo_end_km"]
        if o_s is not None and o_e is not None:
            assert o_e >= o_s


def test_anchor_ordering_is_already_enforced(segments):
    """Re-running the clamp finds nothing left to do.

    ``_enforce_anchor_ordering`` runs at the end of ``run_segment_detection``; a
    second pass over the SAME list must report zero further clamps. (Note it
    deliberately SKIPS a clamp that would collapse a segment to a non-discharge,
    so "zero remaining overlaps" is not the invariant — idempotence is.)
    """
    alias, _charge, discharge = segments
    assert _enforce_anchor_ordering(discharge, alias) == 0


def test_anchor_windows_bracket_their_segment(segments):
    _alias, _charge, discharge = segments
    for seg in discharge:
        a_s, a_e = seg["_anchor_start_time"], seg["_anchor_end_time"]
        if a_s is None or a_e is None:
            continue
        assert _naive(a_s) <= _naive(a_e)


def test_charge_and_discharge_windows_do_not_interleave_on_the_speed_branch(segments):
    # A charge event happens while parked, so it cannot overlap a driving trip.
    alias, charge, discharge = segments
    if EXPECTATIONS[alias][0] != "speed":
        pytest.skip("SOC-branch charge/discharge blocks can share a boundary sample")
    for c in charge:
        c_s, c_e = _naive(c["start_time"]), _naive(c["end_time"])
        for d in discharge:
            d_s, d_e = _naive(d["start_time"]), _naive(d["end_time"])
            assert (
                c_e <= d_s or d_e <= c_s
            ), f"{alias}: charge {c_s}..{c_e} overlaps trip {d_s}..{d_e}"


# ── Golden comparison ────────────────────────────────────────────────────────


def test_matches_the_frozen_golden(segments, load_golden, serialise):
    """Any change to a single segment field fails here.

    Regenerate deliberately:  python tests/fixtures/regenerate_goldens.py
    """
    alias, charge, discharge = segments
    golden = load_golden(f"segments_{alias}.json")
    assert golden["alias"] == alias
    assert serialise(charge) == golden["charge"]
    assert serialise(discharge) == golden["discharge"]


def test_golden_files_describe_the_fixture_they_were_built_from(
    alias, load_golden, raw_fixture_map
):
    golden = load_golden(f"segments_{alias}.json")
    assert golden["source"] == raw_fixture_map[alias]


def test_segmentation_is_deterministic(alias, run_fixture_segmentation, serialise):
    first = run_fixture_segmentation(alias)
    second = run_fixture_segmentation(alias)
    assert serialise(first[0]) == serialise(second[0])
    assert serialise(first[1]) == serialise(second[1])


def test_segmentation_does_not_mutate_the_callers_frame(
    alias, frozen_configs, load_raw_telematics
):
    """The augmented ``mass_cluster`` / ``mass_moving`` columns land on an
    internal copy, never on the DataFrame the caller handed in."""
    from jolt_toolkit.report_generator.segment_algorithms import run_segment_detection

    frame = load_raw_telematics(alias)
    snapshot = frame.copy(deep=True)
    nominal = frozen_configs["vehicles"][alias].get("nominal_kwh")
    run_segment_detection(
        frame,
        reg=alias,
        suffix="fixture",
        out_dir=None,
        generate_validation_fig=False,
        cap_lo=nominal * 0.5 if nominal else None,
        cap_hi=nominal * 2.0 if nominal else None,
    )
    assert list(frame.columns) == list(snapshot.columns)
    assert frame.equals(snapshot)
