"""The ``figure_hook`` seam.

Since v3.1.0 the package neither imports matplotlib nor paints validation
figures: an external painter (the report-visuals skill) is handed in as
``figure_hook``. The contract is that it is invoked exactly once per leg, at the
point the old inline painter sat, with the documented positional and keyword
arguments — and that with no hook NOTHING is written to disk.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

DOCUMENTED_KWARGS = {
    "ac_col",
    "dc_col",
    "panel3_col",
    "mass_col",
    "speed_col",
    "logger_speed_df",
    "logger_mass_df",
    "charger_meter_df",
    "mass_from_logger",
    "mass_agg",
    "export_dsoc_overlay",
}


class Recorder:
    """A painter stand-in that records its calls and writes nothing."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


@pytest.fixture
def hook_run(frozen_configs, load_raw_telematics):
    from report_generator.segment_algorithms import run_segment_detection

    def _run(alias, out_dir, **overrides):
        nominal = frozen_configs["vehicles"][alias].get("nominal_kwh")
        kwargs = dict(
            reg=alias,
            suffix="2025-06-27_0000",
            out_dir=out_dir,
            generate_validation_fig=True,
            cap_lo=nominal * 0.5 if nominal else None,
            cap_hi=nominal * 2.0 if nominal else None,
        )
        kwargs.update(overrides)
        return run_segment_detection(load_raw_telematics(alias), **kwargs)

    return _run


def test_hook_is_invoked_exactly_once_per_leg(hook_run, tmp_path):
    hook = Recorder()
    hook_run("EVSPD01", tmp_path, figure_hook=hook)
    assert len(hook.calls) == 1


def test_hook_receives_the_documented_positional_arguments(hook_run, tmp_path):
    hook = Recorder()
    charge, discharge = hook_run("EVSPD01", tmp_path, figure_hook=hook)
    args, _kwargs = hook.calls[0]

    df_raw, charge_segs, discharge_segs, reg, suffix, out_path = args
    assert isinstance(df_raw, pd.DataFrame)
    assert charge_segs == charge
    assert discharge_segs == discharge
    assert reg == "EVSPD01"
    assert suffix == "2025-06-27_0000"
    assert isinstance(out_path, Path)


def test_hook_out_path_follows_the_documented_naming(hook_run, tmp_path):
    hook = Recorder()
    hook_run("EVSPD01", tmp_path, figure_hook=hook)
    out_path = hook.calls[0][0][5]
    assert out_path == tmp_path / "validation_figures" / (
        "validation_EVSPD01_2025-06-27_0000.png"
    )


def test_hook_receives_the_documented_keyword_arguments(
    hook_run, tmp_path, frozen_configs
):
    hook = Recorder()
    hook_run("EVSPD01", tmp_path, figure_hook=hook)
    _args, kwargs = hook.calls[0]
    assert set(kwargs) == DOCUMENTED_KWARGS

    cfg = frozen_configs["vehicles"]["EVSPD01"]
    assert kwargs["ac_col"] == cfg["ac_col"]
    assert kwargs["dc_col"] == cfg["dc_col"]
    assert kwargs["mass_col"] == cfg["mass_col"]
    assert kwargs["speed_col"] == cfg["speed_col"]
    assert kwargs["mass_from_logger"] is False
    assert kwargs["export_dsoc_overlay"] is False
    assert kwargs["logger_speed_df"] is None
    assert kwargs["logger_mass_df"] is None
    assert kwargs["charger_meter_df"] is None


def test_hook_gets_the_resolved_mass_aggregator(hook_run, tmp_path):
    hook = Recorder()
    hook_run("EVMAD01", tmp_path, figure_hook=hook)
    # Vehicle-level mass_agg beats the pipeline's iqr_median.
    assert hook.calls[0][1]["mass_agg"] == "mad_tw_mean"


def test_hook_panel3_column_follows_the_discharge_energy_source(
    hook_run, tmp_path, frozen_configs
):
    hook = Recorder()
    hook_run("EVSPD01", tmp_path, figure_hook=hook)
    cfg = frozen_configs["vehicles"]["EVSPD01"]
    # EVSPD01's trips resolve to 'total_energy' -> panel 3 plots the total column.
    assert hook.calls[0][1]["panel3_col"] == cfg["total_energy_col"]

    hook_mad = Recorder()
    hook_run("EVMAD01", tmp_path, figure_hook=hook_mad)
    cfg_mad = frozen_configs["vehicles"]["EVMAD01"]
    # EVMAD01's trips resolve to 'moving_energy' -> panel 3 plots the moving column.
    assert hook_mad.calls[0][1]["panel3_col"] == cfg_mad["moving_energy_col"]


def test_hook_forwards_export_dsoc_overlay(hook_run, tmp_path):
    hook = Recorder()
    hook_run("EVSPD01", tmp_path, figure_hook=hook, export_dsoc_overlay=True)
    assert hook.calls[0][1]["export_dsoc_overlay"] is True


def test_hook_sees_the_augmented_frame_with_the_mass_cluster_columns(
    hook_run, tmp_path
):
    hook = Recorder()
    hook_run("EVSPD01", tmp_path, figure_hook=hook)
    df_raw = hook.calls[0][0][0]
    assert "mass_cluster" in df_raw.columns
    assert "mass_moving" in df_raw.columns


def test_no_hook_writes_nothing_to_disk(hook_run, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    hook_run("EVSPD01", out_dir, figure_hook=None)
    assert list(out_dir.rglob("*")) == []


def test_the_package_never_creates_the_figure_directory_itself(hook_run, tmp_path):
    # The hook OWNS creating out_path.parent; the package must not pre-create it.
    hook = Recorder()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    hook_run("EVSPD01", out_dir, figure_hook=hook)
    assert not (out_dir / "validation_figures").exists()
    assert list(out_dir.rglob("*")) == []


def test_hook_is_skipped_when_figures_are_disabled(hook_run, tmp_path):
    hook = Recorder()
    hook_run("EVSPD01", tmp_path, figure_hook=hook, generate_validation_fig=False)
    assert hook.calls == []


def test_hook_is_skipped_without_an_output_directory(hook_run, tmp_path):
    hook = Recorder()
    hook_run("EVSPD01", None, figure_hook=hook)
    assert hook.calls == []


def test_hook_does_not_change_the_returned_segments(hook_run, tmp_path, serialise):
    with_hook = hook_run("EVSPD01", tmp_path, figure_hook=Recorder())
    without = hook_run("EVSPD01", None, figure_hook=None)
    assert serialise(with_hook[0]) == serialise(without[0])
    assert serialise(with_hook[1]) == serialise(without[1])
