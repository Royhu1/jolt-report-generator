"""EVMAD01: mass clustering, the ``mad_tw_mean`` aggregator and ``merge_by_mass``.

EVMAD01 is the fixture whose real vehicle needed BOTH of the non-default mass
behaviours:

* ``mass_agg: "mad_tw_mean"`` at the VEHICLE level (beating the pipeline's
  ``iqr_median``), because its telematics samples arrive in bursts and a
  count-weighted mean over-reads the transient plateaus;
* ``merge_by_mass: false`` at the PIPELINE level, because its mass cluster is
  unchanged all day and the merge would fuse every trip into one long leg.

Both are asserted against the alternative, so a regression that silently ignores
either flag is caught.
"""

from __future__ import annotations

import copy

import pandas as pd
import pytest

from jolt_toolkit.report_generator.segmentation import constants
from jolt_toolkit.report_generator.segmentation.mass_aggregation import resolve_mass_agg
from jolt_toolkit.report_generator.segmentation.mass_clustering import cluster_mass_data


@pytest.fixture
def merge_enabled_alias(monkeypatch, frozen_configs):
    """A clone of EVMAD01 whose only difference is ``merge_by_mass: true``."""
    pipeline = copy.deepcopy(frozen_configs["pipelines"]["evmad01_speed"])
    pipeline["merge_by_mass"] = True
    monkeypatch.setitem(constants.PIPELINE_CONFIGS, "ut_evmad01_merge_on", pipeline)

    vehicle = copy.deepcopy(frozen_configs["vehicles"]["EVMAD01"])
    vehicle["pipeline"] = "ut_evmad01_merge_on"
    monkeypatch.setitem(constants.VEHICLE_CONFIG, "EVMAD01_MERGEON", vehicle)
    return "EVMAD01_MERGEON"


def test_merge_by_mass_false_really_keeps_the_split(
    run_fixture_segmentation, merge_enabled_alias, load_raw_telematics, frozen_configs
):
    from jolt_toolkit.report_generator.segment_algorithms import run_segment_detection

    _charge_off, discharge_off = run_fixture_segmentation("EVMAD01")

    nominal = frozen_configs["vehicles"]["EVMAD01"]["nominal_kwh"]
    _charge_on, discharge_on = run_segment_detection(
        load_raw_telematics("EVMAD01"),
        reg=merge_enabled_alias,
        suffix="fixture",
        out_dir=None,
        generate_validation_fig=False,
        cap_lo=nominal * 0.5,
        cap_hi=nominal * 2.0,
    )

    assert len(discharge_off) == 10
    # With the merge ON the same-cluster neighbours are fused into far fewer,
    # much longer legs — exactly the failure mode merge_by_mass: false prevents.
    assert len(discharge_on) < len(discharge_off)
    assert len(discharge_on) == 4


def test_merge_on_produces_longer_legs(
    run_fixture_segmentation, merge_enabled_alias, load_raw_telematics, frozen_configs
):
    from jolt_toolkit.report_generator.segment_algorithms import run_segment_detection

    _c_off, discharge_off = run_fixture_segmentation("EVMAD01")
    nominal = frozen_configs["vehicles"]["EVMAD01"]["nominal_kwh"]
    _c_on, discharge_on = run_segment_detection(
        load_raw_telematics("EVMAD01"),
        reg=merge_enabled_alias,
        suffix="fixture",
        out_dir=None,
        generate_validation_fig=False,
        cap_lo=nominal * 0.5,
        cap_hi=nominal * 2.0,
    )

    def _naive(value):
        # NOTE: a merged segment can carry a tz-AWARE start_time next to a
        # tz-NAIVE end_time (the merge takes each endpoint from a different
        # source). ``_seg_to_row`` normalises them downstream, so normalise each
        # endpoint independently here rather than assuming they agree.
        ts = pd.Timestamp(value)
        return ts.tz_convert(None) if ts.tzinfo is not None else ts

    def _span_hours(seg):
        start = _naive(seg["start_time"])
        end = _naive(seg["end_time"])
        return (end - start).total_seconds() / 3600.0

    longest_off = max(_span_hours(s) for s in discharge_off)
    longest_on = max(_span_hours(s) for s in discharge_on)
    assert longest_on > longest_off


def test_vehicle_level_mass_agg_beats_the_pipeline(frozen_configs):
    pipeline_agg = frozen_configs["pipelines"]["evmad01_speed"]["mass_agg"]
    vehicle_agg = frozen_configs["vehicles"]["EVMAD01"]["mass_agg"]
    assert pipeline_agg == "iqr_median"
    assert vehicle_agg == "mad_tw_mean"
    assert resolve_mass_agg("EVMAD01") == "mad_tw_mean"


def test_mad_tw_mean_and_iqr_median_disagree_on_the_real_fixture(
    frozen_configs, load_raw_telematics, run_fixture_segmentation
):
    """The two aggregators must actually produce different masses here —
    otherwise the vehicle-level override would be untested in practice."""
    from jolt_toolkit.report_generator.row_builder import _get_vehicle_mass

    frame = load_raw_telematics("EVMAD01")
    _charge, discharge = run_fixture_segmentation("EVMAD01")
    seg = discharge[0]

    tw_mass, _ = _get_vehicle_mass(
        frame, seg["start_time"], seg["end_time"], method="mad_tw_mean"
    )
    iqr_mass, _ = _get_vehicle_mass(
        frame, seg["start_time"], seg["end_time"], method="iqr_median"
    )
    assert tw_mass == pytest.approx(tw_mass)  # a real number, not NaN
    assert not pd.isna(tw_mass) and not pd.isna(iqr_mass)
    assert tw_mass != iqr_mass


def test_mass_clustering_labels_the_fixture(frozen_configs, load_raw_telematics):
    cfg = frozen_configs["vehicles"]["EVMAD01"]
    frame = load_raw_telematics("EVMAD01")
    clustered = cluster_mass_data(
        frame,
        mass_col=cfg["mass_col"],
        min_cluster_gap_kg=cfg["min_cluster_gap_kg"],
        speed_col=cfg["speed_col"],
        speed_threshold_kmh=1.0,
    )
    assert "mass_cluster" in clustered.columns
    assert "mass_moving" in clustered.columns
    # A copy, not an in-place mutation of the caller's frame.
    assert "mass_cluster" not in frame.columns
    labels = clustered["mass_cluster"].dropna().unique()
    assert len(labels) >= 1
    assert all(float(v).is_integer() for v in labels)


def test_mass_clustering_only_labels_positive_readings(
    frozen_configs, load_raw_telematics
):
    cfg = frozen_configs["vehicles"]["EVMAD01"]
    frame = load_raw_telematics("EVMAD01")
    clustered = cluster_mass_data(
        frame, mass_col=cfg["mass_col"], speed_col=cfg["speed_col"]
    )
    mass = pd.to_numeric(clustered[cfg["mass_col"]], errors="coerce")
    labelled = clustered["mass_cluster"].notna()
    assert (mass[labelled] > 0).all()


def test_mass_moving_flag_requires_motion(frozen_configs, load_raw_telematics):
    cfg = frozen_configs["vehicles"]["EVMAD01"]
    frame = load_raw_telematics("EVMAD01")
    clustered = cluster_mass_data(
        frame,
        mass_col=cfg["mass_col"],
        speed_col=cfg["speed_col"],
        speed_threshold_kmh=1.0,
    )
    speed = pd.to_numeric(clustered[cfg["speed_col"]], errors="coerce")
    moving = clustered["mass_moving"].astype(bool)
    assert (speed[moving] > 1.0).all()
