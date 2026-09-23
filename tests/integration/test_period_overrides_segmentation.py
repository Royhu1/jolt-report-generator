"""Date-effective settings on real fixture legs: the right settings, for every caller.

``run_segment_detection`` resolves a vehicle's ``period_overrides`` for the UTC
date of the first valid timestamp of the frame it is given, so a leg on or after
an override's ``from`` is segmented with the override's settings and a leg
before it — even one that starts at 23:59 the evening before and runs past
midnight — with the base ones. The generator's own per-leg call and a direct
call on the same frame (an external renderer's) resolve alike, including the
per-segment mass estimator the generator hands to the row builder. A vehicle
without the field is segmented exactly as before, at no extra cost: the goldens
are byte-identical.

The override exercised is the shape the field exists for: the EVSOC01 fixture
(the SOC branch, no telematics speed column its config names) switched to a
speed-branch pipeline with Logger-speed trip detection. The Logger speed trace is
the fixture's own ``speed`` column, so the trips it yields line up with the SOC.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import types
from unittest.mock import Mock

import pandas as pd
import pytest

from report_generator import _generator as gen_mod
from report_generator._generator import JOLTReportGenerator
from report_generator.segment_algorithms import run_segment_detection
from report_generator.segmentation import constants, detection, mass_aggregation
from report_generator.segmentation.timeutil import frame_utc_date

ALIAS = "EVSOC01"
TIME = "eventDatetime"
#: The override under test: Logger-speed trip detection on a speed pipeline.
SWITCH = {
    "pipeline": "evspd01_speed",
    "prefer_logger_speed": True,
    "mass_agg": "median",
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _override(start: dt.date, settings=SWITCH, end=None) -> dict:
    return {
        "from": start.isoformat(),
        "to": end.isoformat() if end else None,
        "reason": "test",
        "set": dict(settings),
    }


def _inject(monkeypatch, frozen_configs, *overrides, alias=ALIAS, **settings):
    """The frozen alias config — with overrides, or flattened with ``settings``."""
    cfg = copy.deepcopy(frozen_configs["vehicles"][alias])
    cfg.update(settings)
    if overrides:
        cfg["period_overrides"] = list(overrides)
    monkeypatch.setitem(constants.VEHICLE_CONFIG, alias, cfg)
    return cfg


def _logger_speed(frame: pd.DataFrame) -> pd.DataFrame:
    """A Logger-style speed channel (UTC index, one column) from the frame itself."""
    index = pd.DatetimeIndex(pd.to_datetime(frame[TIME], utc=True))
    speed = pd.to_numeric(frame["speed"], errors="coerce").to_numpy()
    return pd.DataFrame({"CCVS wheel based vehicle speed": speed}, index=index)


def _shift(frame: pd.DataFrame, first: pd.Timestamp) -> pd.DataFrame:
    """The frame moved in time so that its first timestamp is ``first``."""
    times = pd.to_datetime(frame[TIME], utc=True)
    moved = times + (first - times.iloc[0])
    out = frame.copy()
    out[TIME] = moved.dt.strftime("%Y-%m-%dT%H:%M:%S.%f").str[:-3] + "Z"
    return out


def _segment(frame, frozen_configs, alias=ALIAS, **kwargs):
    nominal = frozen_configs["vehicles"][alias].get("nominal_kwh")
    return run_segment_detection(
        frame,
        reg=alias,
        suffix="test",
        out_dir=kwargs.pop("out_dir", None),
        generate_validation_fig=kwargs.pop("generate_validation_fig", False),
        cap_lo=nominal * 0.5 if nominal else None,
        cap_hi=nominal * 2.0 if nominal else None,
        **kwargs,
    )


@pytest.fixture
def speed_branch_calls(monkeypatch):
    """Record every run of the speed branch (the SOC branch never calls it)."""
    calls = []
    real = detection.find_discharge_segments_by_speed

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(detection, "find_discharge_segments_by_speed", spy)
    return calls


@pytest.fixture
def frame(load_raw_telematics):
    return load_raw_telematics(ALIAS)


@pytest.fixture
def day(frame):
    """The fixture leg's date: 2026-04-24."""
    return frame_utc_date(frame)


def _both(result, serialise):
    charge, discharge = result
    return serialise(charge), serialise(discharge)


# ── Which settings a leg is segmented with ───────────────────────────────────


@pytest.mark.parametrize("offset_days", [0, -30], ids=["on from", "after from"])
def test_a_leg_on_or_after_from_is_segmented_with_the_override(
    monkeypatch, frozen_configs, frame, day, serialise, speed_branch_calls, offset_days
):
    start = day + dt.timedelta(days=offset_days)
    logger_speed = _logger_speed(frame)
    _inject(monkeypatch, frozen_configs, _override(start))
    resolved = _both(
        _segment(frame, frozen_configs, logger_speed_df=logger_speed), serialise
    )

    assert len(speed_branch_calls) == 1
    assert "trips" in speed_branch_calls[0]  # the Logger-speed trips were used
    # Exactly what the override's settings give when configured outright ...
    _inject(monkeypatch, frozen_configs, **SWITCH)
    flattened = _both(
        _segment(frame, frozen_configs, logger_speed_df=logger_speed), serialise
    )
    assert resolved == flattened
    # ... which is not what the base settings give on this leg.
    _inject(monkeypatch, frozen_configs)
    base = _both(
        _segment(frame, frozen_configs, logger_speed_df=logger_speed), serialise
    )
    assert flattened != base


def test_a_leg_before_from_is_segmented_with_the_base_settings(
    monkeypatch, frozen_configs, frame, day, serialise, speed_branch_calls, load_golden
):
    _inject(monkeypatch, frozen_configs, _override(day + dt.timedelta(days=1)))
    resolved = _both(
        _segment(frame, frozen_configs, logger_speed_df=_logger_speed(frame)),
        serialise,
    )
    assert speed_branch_calls == []
    golden = load_golden(f"segments_{ALIAS}.json")
    assert resolved == (golden["charge"], golden["discharge"])


@pytest.mark.parametrize(
    "first, expected_branch",
    [
        # Starts one minute before midnight on the eve of 'from', runs well past
        # it: the leg's date is the eve, so the base settings apply.
        ("23:59:00", "soc"),
        # Starts at midnight on 'from' itself.
        ("00:00:00", "speed"),
    ],
    ids=["23:59 the day before", "00:00 on the day"],
)
def test_the_leg_date_is_taken_from_its_first_timestamp(
    monkeypatch,
    frozen_configs,
    frame,
    serialise,
    speed_branch_calls,
    first,
    expected_branch,
):
    start = dt.date(2026, 7, 7)
    eve = start - dt.timedelta(days=1)
    stamp = eve if first == "23:59:00" else start
    moved = _shift(frame, pd.Timestamp(f"{stamp.isoformat()}T{first}Z"))
    last = pd.to_datetime(moved[TIME], utc=True).iloc[-1]
    assert last.date() == start  # the leg does run into 'from'
    logger_speed = _logger_speed(moved)

    _inject(monkeypatch, frozen_configs, _override(start))
    resolved = _both(
        _segment(moved, frozen_configs, logger_speed_df=logger_speed), serialise
    )
    assert bool(speed_branch_calls) == (expected_branch == "speed")

    reference = SWITCH if expected_branch == "speed" else {}
    _inject(monkeypatch, frozen_configs, **reference)
    assert resolved == _both(
        _segment(moved, frozen_configs, logger_speed_df=logger_speed), serialise
    )


def test_a_window_that_has_closed_is_no_longer_applied(
    monkeypatch, frozen_configs, frame, day, serialise, speed_branch_calls
):
    window = _override(day - dt.timedelta(days=10), end=day)  # 'to' exclusive
    _inject(monkeypatch, frozen_configs, window)
    _segment(frame, frozen_configs, logger_speed_df=_logger_speed(frame))
    assert speed_branch_calls == []


# ── The generator and a direct caller resolve alike ──────────────────────────


def _fake_leg(csv_text: str, frame: pd.DataFrame):
    times = pd.to_datetime(frame[TIME], utc=True)
    return types.SimpleNamespace(
        uri="https://srf.example/api/legs/fixture-evsoc01/",
        start_time=times.iloc[0].to_pydatetime(),
        end_time=times.iloc[-1].to_pydatetime(),
        get_raw_data=lambda: [csv_text],
    )


def test_the_generators_per_leg_call_resolves_as_a_direct_call(
    monkeypatch, tmp_path, frozen_configs, frame, day, raw_fixture_path, serialise
):
    _inject(monkeypatch, frozen_configs, _override(day))
    logger_speed = _logger_speed(frame)

    # The generator's own per-leg path, on the fixture as the SRF raw data.
    monkeypatch.setattr(gen_mod, "make_srf_client", Mock(return_value=Mock()))
    monkeypatch.setattr(gen_mod, "get_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(
        gen_mod, "derive_leg_operator", lambda *a, **k: ("OPX", "config", False)
    )
    calls, row_mass_aggs = [], []
    real_detection, real_row = gen_mod.run_segment_detection, gen_mod._seg_to_row

    def detection_spy(df, **kwargs):
        result = real_detection(df, **kwargs)
        calls.append((df, kwargs, result))
        return result

    def row_spy(*args, **kwargs):
        row_mass_aggs.append(kwargs["mass_agg"])
        return real_row(*args, **kwargs)

    monkeypatch.setattr(gen_mod, "run_segment_detection", detection_spy)
    monkeypatch.setattr(gen_mod, "_seg_to_row", row_spy)
    generator = JOLTReportGenerator(report_output_folder=str(tmp_path / "out"))
    generator.srf_data = None  # no postcode lookups
    nominal = frozen_configs["vehicles"][ALIAS]["nominal_kwh"]
    leg = _fake_leg(raw_fixture_path(ALIAS).read_text(encoding="utf-8"), frame)
    all_rows = []
    generator._process_fps_legs(
        [leg],
        ALIAS,
        tmp_path / "out",
        None,
        nominal * 0.5,
        nominal * 2.0,
        logger_speed,
        None,
        None,
        None,
        None,
        [],
        None,
        "wheel_based_speed",
        "mean",  # the report-wide estimator — overridden for this leg
        "gross_combination_vehicle_weight",
        None,
        {},
        {},
        None,
        0.0,
        all_rows,
        {},
    )
    ((gen_frame, gen_kwargs, gen_result),) = calls
    assert all_rows, "the fixture leg must produce rows"

    # A direct call on the same frame, as an external renderer makes it, with a
    # painter to see what it was given.
    painted = {}

    def painter(*_args, **kwargs):
        painted.update(kwargs)

    direct = run_segment_detection(
        gen_frame,
        ALIAS,
        "direct",
        out_dir=tmp_path / "figures",
        cap_lo=gen_kwargs["cap_lo"],
        cap_hi=gen_kwargs["cap_hi"],
        logger_speed_df=gen_kwargs["logger_speed_df"],
        figure_hook=painter,
    )

    assert _both(direct, serialise) == _both(gen_result, serialise)
    # The override's estimator reached both the rows and the painter.
    assert set(row_mass_aggs) == {painted["mass_agg"]} == {"median"}


# ── A vehicle without the field is untouched ─────────────────────────────────

EV_ALIASES = ("EVSPD01", "EVSOC01", "EVMAD01")


@pytest.mark.parametrize("value", [None, []], ids=["null", "empty"])
@pytest.mark.parametrize("alias", EV_ALIASES)
def test_an_unset_field_leaves_the_golden_byte_identical(
    monkeypatch,
    frozen_configs,
    load_raw_telematics,
    fixtures_dir,
    serialise,
    alias,
    value,
):
    cfg = _inject(monkeypatch, frozen_configs, alias=alias)
    cfg["period_overrides"] = value
    charge, discharge = _segment(
        load_raw_telematics(alias), frozen_configs, alias=alias
    )
    golden_path = fixtures_dir / "expected" / f"segments_{alias}.json"
    payload = {
        "alias": alias,
        "source": json.loads(golden_path.read_text(encoding="utf-8"))["source"],
        "charge": serialise(charge),
        "discharge": serialise(discharge),
    }
    # The very text regenerate_goldens.py would write.
    assert json.dumps(payload, indent=2) + "\n" == golden_path.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("alias", EV_ALIASES)
def test_a_vehicle_without_the_field_does_no_extra_work(
    monkeypatch, tmp_path, frozen_configs, load_raw_telematics, alias
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("nothing may be resolved for a vehicle without the field")

    for module in (detection, mass_aggregation):
        monkeypatch.setattr(module, "effective_vehicle_config", forbidden)
    monkeypatch.setattr(detection, "frame_utc_date", forbidden)
    # Through the painter seam too, where the mass estimator is resolved.
    _segment(
        load_raw_telematics(alias),
        frozen_configs,
        alias=alias,
        out_dir=tmp_path,
        generate_validation_fig=True,
        figure_hook=lambda *a, **k: None,
    )
