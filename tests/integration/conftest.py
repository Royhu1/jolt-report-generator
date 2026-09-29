"""Shared helpers for the fixture-driven integration tests."""

from __future__ import annotations

import json

import pandas as pd
import pytest


@pytest.fixture
def run_fixture_segmentation(frozen_configs, load_raw_telematics):
    """Run ``run_segment_detection`` on an EV fixture exactly as the generator does.

    Mirrors ``_generator.generate_report``: the capacity bounds come from the
    vehicle's ``nominal_kwh`` (x0.5 / x2.0), no figure hook is passed and no
    output directory is given, so the call is pure computation.
    """
    from report_generator.segment_algorithms import run_segment_detection

    def _run(alias: str, **overrides):
        nominal = frozen_configs["vehicles"][alias].get("nominal_kwh")
        kwargs = dict(
            reg=alias,
            suffix="fixture",
            out_dir=None,
            generate_validation_fig=False,
            cap_lo=nominal * 0.5 if nominal else None,
            cap_hi=nominal * 2.0 if nominal else None,
        )
        kwargs.update(overrides)
        return run_segment_detection(load_raw_telematics(alias), **kwargs)

    return _run


@pytest.fixture(scope="session")
def load_golden(fixtures_dir):
    """Load a frozen golden payload from ``tests/fixtures/expected/``."""

    def _load(name: str) -> dict:
        path = fixtures_dir / "expected" / name
        assert path.exists(), (
            f"missing golden {path}; regenerate with "
            f"`python tests/fixtures/regenerate_goldens.py`"
        )
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    return _load


@pytest.fixture
def diesel_fixture_frame(frozen_configs, raw_fixture_path):
    """The DSL01 logger CSV rebuilt into the diesel pipeline's DataFrame shape."""
    from report_generator import diesel_pipeline as dp

    cfg = frozen_configs["vehicles"]["DSL01"]
    frame = dp._logger_df_from_csv(raw_fixture_path("DSL01"), cfg)
    assert frame is not None, "the DSL01 fixture must rebuild into a logger frame"
    return frame, cfg


# ── The consumer contract of an EV leg's segments ────────────────────────────

#: Keys every charge segment carries.
CHARGE_KEYS = {
    "start_time",
    "end_time",
    "start_soc",
    "end_soc",
    "delta_soc_pct",
    "delta_energy_kwh",
    "energy_source",
    "effective_capacity_kwh",
    "charge_type",
}
#: Keys every discharge segment carries, a distance-only trip included.
DISCHARGE_KEYS = {
    "start_time",
    "end_time",
    "start_soc",
    "end_soc",
    "delta_soc_pct",
    "delta_energy_kwh",
    "energy_source",
    "effective_capacity_kwh",
    "odo_start_km",
    "odo_end_km",
    "ep_audit",
}
#: The energy sources a charge and a measured trip resolve to.
CHARGE_SOURCES = {"ac_dc", "soc_estimate"}
DISCHARGE_SOURCES = {"total_energy", "moving_energy", "soc_estimate"}


def _naive(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_convert(None) if ts.tzinfo is not None else ts


def _check_consumer_contract(charge: list[dict], discharge: list[dict]) -> None:
    """Assert what a consumer of an EV leg's segments relies on.

    Every segment carries the keys of its kind, each kind is in chronological
    order, and no segment ends before it starts. A charge resolves to one of the
    charge energy sources, with a positive SOC change and energy; a measured trip
    to one of the discharge energy sources, with both negative (the sign
    convention).

    A distance-only trip (``speed_params.keep_odometer_confirmed_trips``) is a trip
    over which nothing measured an energy, kept because the odometer confirmed it,
    and is held to what it promises instead: its ``energy_source`` is
    :data:`~report_generator.columns.DISTANCE_ONLY_SOURCE`; its energy is NaN, which
    the row builder writes as ``=NA()`` energy and EP cells (a ``None`` would break
    its arithmetic); it carries no capacity; and both odometer readings are there,
    the end beyond the start, since that distance is what its row reports. Its SOC
    change is whatever the readings show — a fall below the floor, no change, or a
    rise no charge accounts for (NaN without a reading) — so the sign convention
    does not apply to it.
    """
    from report_generator.columns import DISTANCE_ONLY_SOURCE, _is_nan

    for seg in charge:
        assert CHARGE_KEYS <= set(seg), CHARGE_KEYS - set(seg)
        assert seg["energy_source"] in CHARGE_SOURCES
        assert seg["delta_soc_pct"] > 0 and seg["delta_energy_kwh"] > 0
    for seg in discharge:
        assert DISCHARGE_KEYS <= set(seg), DISCHARGE_KEYS - set(seg)
        if seg["energy_source"] == DISTANCE_ONLY_SOURCE:
            assert _is_nan(seg["delta_energy_kwh"]), seg["start_time"]
            capacity = seg["effective_capacity_kwh"]
            assert capacity is None or _is_nan(capacity), seg["start_time"]
            odo_start, odo_end = seg["odo_start_km"], seg["odo_end_km"]
            assert odo_start is not None and odo_end is not None, seg["start_time"]
            assert odo_end > odo_start, seg["start_time"]
        else:
            assert seg["energy_source"] in DISCHARGE_SOURCES
            assert seg["delta_soc_pct"] < 0 and seg["delta_energy_kwh"] < 0
    for group in (charge, discharge):
        starts = [_naive(s["start_time"]) for s in group]
        assert starts == sorted(starts)
        for seg in group:
            assert _naive(seg["start_time"]) <= _naive(seg["end_time"])


@pytest.fixture(scope="session")
def check_consumer_contract():
    """The consumer contract of an EV leg's segments, as a check to call.

    ``check_consumer_contract(charge, discharge)`` fails the calling test on the
    first segment that breaks it (see :func:`_check_consumer_contract`).
    """
    return _check_consumer_contract
