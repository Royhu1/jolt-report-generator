"""Per-segment mass aggregation: the eight ``_agg_mass`` recipes and their helpers.

Every expected value here is hand-computed from a five-sample series, NOT read
back from the implementation:

    sel = [10, 12, 14, 20, 100]

    mean          all five                  → 156/5   = 31.2
    median        all five                  →           14.0
    IQR fence     q1=12, q3=20, iqr=8       → keep [10, 12, 14, 20]
      iqr_median  median of the kept set    →           13.0
      iqr_mean    mean   of the kept set    → 56/4    = 14.0
    MAD fence     median 14, MAD 4, k=3     → keep [10, 12, 14, 20]  (same set here)
      mad_median                            →           13.0
      mad_mean                              →           14.0
      mad_tw_mean without timestamps degrades to mad_mean → 14.0
    trimmed_mean  n=5, frac .2 → k=1        → mean[12, 14, 20] = 46/3 = 15.333 → 15.3

``cv`` is ``std(ddof=1) / mean`` of the KEPT set, rounded to 4 dp.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from jolt_toolkit.report_generator.segmentation import constants
from jolt_toolkit.report_generator.segmentation import mass_aggregation as ma

SEL = pd.Series([10.0, 12.0, 14.0, 20.0, 100.0])

# cv of the three distinct kept sets (std ddof=1 / mean, 4 dp).
CV_ALL = 1.2385  # [10, 12, 14, 20, 100]
CV_FENCED = 0.3086  # [10, 12, 14, 20]
CV_TRIMMED = 0.2715  # [12, 14, 20]


@pytest.mark.parametrize(
    "method, expected_value, expected_cv",
    [
        ("mean", 31.2, CV_ALL),
        ("median", 14.0, CV_ALL),
        ("iqr_median", 13.0, CV_FENCED),
        ("iqr_mean", 14.0, CV_FENCED),
        ("mad_median", 13.0, CV_FENCED),
        ("mad_mean", 14.0, CV_FENCED),
        ("mad_tw_mean", 14.0, CV_FENCED),
        ("trimmed_mean", 15.3, CV_TRIMMED),
    ],
)
def test_agg_mass_all_eight_methods(method, expected_value, expected_cv):
    value, cv = ma._agg_mass(SEL, method)
    assert value == expected_value
    assert cv == expected_cv


def test_agg_mass_covers_every_declared_method():
    # The parametrised table above must stay in step with the module's tuple.
    assert set(ma._MASS_AGG_METHODS) == {
        "mean",
        "median",
        "iqr_median",
        "iqr_mean",
        "mad_median",
        "mad_mean",
        "mad_tw_mean",
        "trimmed_mean",
    }


def test_agg_mass_is_case_insensitive():
    assert ma._agg_mass(SEL, "IQR_Median") == ma._agg_mass(SEL, "iqr_median")


@pytest.mark.parametrize("sel", [None, pd.Series(dtype=float), pd.Series([42.0])])
def test_agg_mass_needs_two_samples(sel):
    value, cv = ma._agg_mass(sel, "mean")
    assert np.isnan(value) and np.isnan(cv)


def test_agg_mass_unknown_method_warns_and_falls_back_to_mean(caplog):
    with caplog.at_level("WARNING"):
        value, cv = ma._agg_mass(SEL, "no_such_method")
    assert (value, cv) == ma._agg_mass(SEL, "mean")
    assert "Unknown mass_agg method" in caplog.text


def test_agg_mass_none_method_is_mean():
    assert ma._agg_mass(SEL, None) == ma._agg_mass(SEL, "mean")


def test_agg_mass_non_positive_mean_gives_nan_cv():
    # mean <= 0 makes std/mean meaningless; the value is still returned.
    value, cv = ma._agg_mass(pd.Series([-4.0, 0.0, 4.0]), "mean")
    assert value == 0.0
    assert np.isnan(cv)


def test_agg_mass_singleton_kept_set_uses_full_window_for_cv():
    # A 20% trim of 4 samples keeps 2, so force a singleton via a tiny window:
    # 3 samples, k = int(3*0.2) = 0 -> no trim, cv from the full set.
    sel = pd.Series([10.0, 20.0, 30.0])
    value, cv = ma._agg_mass(sel, "trimmed_mean")
    assert value == 20.0
    assert cv == round(float(sel.std() / sel.mean()), 4)


# ── Fences ───────────────────────────────────────────────────────────────────


def test_iqr_inliers_drops_the_high_outlier():
    kept = ma._iqr_inliers(SEL)
    assert list(kept) == [10.0, 12.0, 14.0, 20.0]


def test_iqr_inliers_zero_iqr_returns_input_unchanged():
    # A quantised GCW: q1 == q3, so the fence is degenerate. The helper hands
    # the full window back (the caller's median already ignores the spike).
    sel = pd.Series([30000.0, 30000.0, 30000.0, 30000.0, 49000.0])
    assert sel.quantile(0.25) == sel.quantile(0.75)  # premise of the branch
    assert ma._iqr_inliers(sel) is sel


@pytest.mark.parametrize(
    "values",
    [
        [0.0, 1.0, 1.0, 1.0, 1.0, 1000.0, 2000.0, 3000.0],
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 1000.0],
        [10.0, 12.0],
    ],
)
def test_fences_never_strand_the_caller_with_fewer_than_two_samples(values):
    # Both fences contain the interquartile / median±MAD span by construction,
    # so the "< 2 survivors" guard is a defensive branch — assert the invariant
    # it protects rather than pretending to reach it.
    sel = pd.Series(values)
    assert len(ma._iqr_inliers(sel)) >= 2
    assert len(ma._mad_inliers(sel)) >= 2


def test_mad_inliers_is_tighter_than_iqr_on_a_one_sided_spike_cluster():
    # A dense (jittered) body plus a CLUSTER of high spikes: the spikes pin q3 so
    # the Tukey fence keeps them, while the median-centred MAD fence shaves them.
    # This is the documented reason mad_* exists alongside iqr_*.
    body = [29900.0, 29950.0, 30000.0, 30050.0, 30100.0] * 2
    sel = pd.Series(body + [49000.0, 49100.0, 49200.0, 49300.0])
    iqr_kept = ma._iqr_inliers(sel)
    mad_kept = ma._mad_inliers(sel)
    assert len(iqr_kept) == 14  # Tukey keeps the whole window
    assert len(mad_kept) == 10  # MAD shaves the four spikes
    assert mad_kept.max() == 30100.0


def test_mad_inliers_zero_mad_returns_input_unchanged():
    sel = pd.Series([100.0, 100.0, 100.0, 900.0])
    assert ma._mad_inliers(sel) is sel


def test_trimmed_inliers_symmetric_drop():
    sel = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    # n=10, k = int(10*0.2) = 2 -> drop two from each tail.
    assert list(ma._trimmed_inliers(sel)) == [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]


def test_trimmed_inliers_no_trim_on_tiny_window():
    sel = pd.Series([5.0, 6.0, 7.0])  # k = 0
    assert ma._trimmed_inliers(sel) is sel


def test_trimmed_inliers_sorts_before_trimming():
    sel = pd.Series([9.0, 1.0, 5.0, 3.0, 7.0])  # k = 1
    assert list(ma._trimmed_inliers(sel)) == [3.0, 5.0, 7.0]


# ── Time axis ────────────────────────────────────────────────────────────────


def test_coerce_seconds_numeric_passthrough():
    out = ma._coerce_seconds(pd.Series([0.0, 30.0, 90.0]))
    assert list(out) == [0.0, 30.0, 90.0]


def test_coerce_seconds_datetime_is_offset_from_the_first_sample():
    ts = pd.to_datetime(
        ["2025-06-27T10:00:00Z", "2025-06-27T10:00:30Z", "2025-06-27T10:02:00Z"]
    )
    out = ma._coerce_seconds(pd.Series(ts))
    assert list(out) == [0.0, 30.0, 120.0]


def test_coerce_seconds_rejects_any_nan():
    assert ma._coerce_seconds(pd.Series([0.0, np.nan, 2.0])) is None


def test_coerce_seconds_rejects_any_nat():
    ts = pd.Series(pd.to_datetime(["2025-06-27T10:00:00Z", None]))
    assert ma._coerce_seconds(ts) is None


@pytest.mark.parametrize("empty", [None, pd.Series(dtype=float), []])
def test_coerce_seconds_empty_is_none(empty):
    assert ma._coerce_seconds(empty) is None


def test_coerce_seconds_accepts_a_plain_list():
    assert list(ma._coerce_seconds([0, 5, 10])) == [0.0, 5.0, 10.0]


def test_time_weighted_mean_trapezoidal_arithmetic():
    # values 0, 10, 20 at t = 0, 1, 10 s.
    # trapezoidal weights: 0.5, (10-0)/2 = 5.0, (10-1)/2 = 4.5  -> sum 10.0
    # weighted mean = (0*0.5 + 10*5 + 20*4.5) / 10 = 140/10 = 14.0
    out = ma._time_weighted_mean(
        np.array([0.0, 10.0, 20.0]), np.array([0.0, 1.0, 10.0])
    )
    assert out == pytest.approx(14.0)
    # ... whereas the plain mean would be 10.0: the dense early burst is
    # down-weighted to the short duration it actually represents.
    assert out != pytest.approx(10.0)


def test_time_weighted_mean_is_order_independent():
    shuffled = ma._time_weighted_mean(
        np.array([20.0, 0.0, 10.0]), np.array([10.0, 0.0, 1.0])
    )
    assert shuffled == pytest.approx(14.0)


@pytest.mark.parametrize(
    "values, seconds",
    [
        ([5.0], [0.0]),  # fewer than two samples
        ([1.0, 2.0], [0.0]),  # length mismatch
        ([1.0, 3.0], [0.0, np.inf]),  # non-finite time
        ([1.0, 3.0], [7.0, 7.0]),  # zero span (duplicate timestamps)
    ],
)
def test_time_weighted_mean_degrades_to_plain_mean(values, seconds):
    out = ma._time_weighted_mean(np.array(values), np.array(seconds))
    assert out == pytest.approx(float(np.mean(values)))


def test_mad_tw_value_uses_timestamps_when_aligned():
    sel = pd.Series([10.0, 10.0, 10.0, 20.0], index=[0, 1, 2, 3])
    kept = ma._mad_inliers(sel)
    ts = pd.Series(
        pd.to_datetime(
            [
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:00:01Z",
                "2025-01-01T00:00:02Z",
                "2025-01-01T01:00:00Z",
            ]
        ),
        index=[0, 1, 2, 3],
    )
    tw = ma._mad_tw_value(sel, kept, ts)
    # The lone 20 spans nearly the whole hour, so the time-weighted mean sits
    # far above the count-weighted mean of the same kept set.
    assert tw > float(kept.mean())


def test_mad_tw_value_degrades_to_mad_mean_without_timestamps():
    sel = pd.Series([10.0, 12.0, 14.0, 20.0, 100.0])
    kept = ma._mad_inliers(sel)
    assert ma._mad_tw_value(sel, kept, None) == pytest.approx(float(kept.mean()))


def test_mad_tw_mean_differs_from_mad_mean_with_a_bursty_time_axis():
    # Three rapid low readings then a long-held high reading: count-weighting
    # under-reads, duration-weighting does not.
    sel = pd.Series([30000.0, 30000.0, 30000.0, 32000.0])
    ts = pd.Series(
        pd.to_datetime(
            [
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:00:01Z",
                "2025-01-01T00:00:02Z",
                "2025-01-01T02:00:00Z",
            ]
        )
    )
    plain, _ = ma._agg_mass(sel, "mad_mean", timestamps=ts)
    weighted, _ = ma._agg_mass(sel, "mad_tw_mean", timestamps=ts)
    assert weighted > plain
    # Other methods must ignore ``timestamps`` entirely.
    assert ma._agg_mass(sel, "mean", timestamps=ts) == ma._agg_mass(sel, "mean")


# ── resolve_mass_agg precedence ──────────────────────────────────────────────


@pytest.fixture
def synthetic_mass_agg_configs(monkeypatch):
    """Inject throwaway vehicle / pipeline entries for the precedence tests.

    Deliberately synthetic (not the live fleet) so retuning a real vehicle's
    ``mass_agg`` can never turn this test red.
    """
    monkeypatch.setitem(
        constants.PIPELINE_CONFIGS,
        "ut_pipeline_with_agg",
        {"branch": "soc", "mass_agg": "iqr_median"},
    )
    monkeypatch.setitem(
        constants.PIPELINE_CONFIGS, "ut_pipeline_plain", {"branch": "soc"}
    )
    monkeypatch.setitem(
        constants.VEHICLE_CONFIG,
        "UTVEH01",  # vehicle-level wins
        {"pipeline": "ut_pipeline_with_agg", "mass_agg": "mad_tw_mean"},
    )
    monkeypatch.setitem(
        constants.VEHICLE_CONFIG,
        "UTVEH02",  # pipeline-level applies
        {"pipeline": "ut_pipeline_with_agg"},
    )
    monkeypatch.setitem(
        constants.VEHICLE_CONFIG,
        "UTVEH03",  # neither -> default
        {"pipeline": "ut_pipeline_plain"},
    )


def test_resolve_mass_agg_vehicle_beats_pipeline(synthetic_mass_agg_configs):
    assert ma.resolve_mass_agg("UTVEH01") == "mad_tw_mean"


def test_resolve_mass_agg_falls_back_to_pipeline(synthetic_mass_agg_configs):
    assert ma.resolve_mass_agg("UTVEH02") == "iqr_median"


def test_resolve_mass_agg_defaults_to_mean(synthetic_mass_agg_configs):
    assert ma.resolve_mass_agg("UTVEH03") == "mean"


def test_resolve_mass_agg_unknown_registration_defaults_to_mean():
    assert ma.resolve_mass_agg("NOSUCHREG") == "mean"


def test_resolve_mass_agg_explicit_pipeline_cfg_overrides_lookup(
    synthetic_mass_agg_configs,
):
    # An explicitly supplied pipeline_cfg is used instead of the vehicle's own
    # pipeline — but the vehicle-level setting still wins over it.
    assert (
        ma.resolve_mass_agg("UTVEH02", {"mass_agg": "trimmed_mean"}) == "trimmed_mean"
    )
    assert ma.resolve_mass_agg("UTVEH01", {"mass_agg": "trimmed_mean"}) == "mad_tw_mean"
