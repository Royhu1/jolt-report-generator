"""Shared helpers for the fixture-driven integration tests."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def run_fixture_segmentation(frozen_configs, load_raw_telematics):
    """Run ``run_segment_detection`` on an EV fixture exactly as the generator does.

    Mirrors ``_generator.generate_report``: the capacity bounds come from the
    vehicle's ``nominal_kwh`` (x0.5 / x2.0), no figure hook is passed and no
    output directory is given, so the call is pure computation.
    """
    from jolt_toolkit.report_generator.segment_algorithms import run_segment_detection

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
    from jolt_toolkit.report_generator import diesel_pipeline as dp

    cfg = frozen_configs["vehicles"]["DSL01"]
    frame = dp._logger_df_from_csv(raw_fixture_path("DSL01"), cfg)
    assert frame is not None, "the DSL01 fixture must rebuild into a logger frame"
    return frame, cfg
