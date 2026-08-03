"""CLI argument parsing and the debug / raw-only flag mapping.

``tests/test_cli.py`` covers the process-level contract (``--help``, the rc-2
missing-key fast-fail) via a subprocess; this module drives ``_build_parser`` and
``main`` in-process with the generator mocked out, so no SRF client is ever
constructed and no report is ever produced.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from jolt_toolkit.report_generator import cli

# ── _build_parser ────────────────────────────────────────────────────────────


def test_parser_defaults():
    args = cli._build_parser().parse_args([])
    assert args.vehicle_registration is None
    assert args.date_start is None
    assert args.date_end is None
    assert args.debug is False
    assert args.raw_only is False
    assert args.fast is False
    assert args.out_dir is None


def test_parser_short_flags():
    args = cli._build_parser().parse_args(
        ["-veh", "YK73WFN", "-ds", "2025-03-01", "-de", "2025-06-01"]
    )
    assert args.vehicle_registration == "YK73WFN"
    assert args.date_start == "2025-03-01"
    assert args.date_end == "2025-06-01"


def test_parser_long_flags():
    args = cli._build_parser().parse_args(
        [
            "--vehicle_registration",
            "WU70GLV",
            "--date_start",
            "2025-10-01",
            "--date_end",
            "2025-10-31",
        ]
    )
    assert args.vehicle_registration == "WU70GLV"


@pytest.mark.parametrize("flag", ["--debug", "--raw-only", "--fast"])
def test_parser_boolean_flags(flag):
    args = cli._build_parser().parse_args([flag])
    attr = {"--debug": "debug", "--raw-only": "raw_only", "--fast": "fast"}[flag]
    assert getattr(args, attr) is True


def test_parser_out_dir_has_a_legacy_alias():
    assert cli._build_parser().parse_args(["--out-dir", "/x"]).out_dir == "/x"
    assert (
        cli._build_parser().parse_args(["--report-output-folder", "/y"]).out_dir == "/y"
    )


def test_parser_prog_is_the_module_form():
    # Since v3.2.0 there is no console script; the help text must say so.
    assert cli._build_parser().prog == "python -m jolt_toolkit.report_generator.cli"


def test_parser_rejects_an_unknown_flag():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["--no-such-flag"])


# ── main(): flag -> constructor mapping ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """``cli.main`` calls ``logging.basicConfig(force=True)``, which tears down
    every existing root handler (pytest's caplog handler included). Snapshot and
    restore them so this module cannot disturb the rest of the session."""
    import logging

    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


@pytest.fixture
def captured_generator(monkeypatch):
    """Replace ``JOLTReportGenerator`` so ``main`` builds no SRF client."""
    import jolt_toolkit.report_generator._generator as gen_mod

    instance = Mock()
    instance.generate_report.return_value = "/tmp/report.xlsx"
    factory = Mock(return_value=instance)
    monkeypatch.setattr(gen_mod, "JOLTReportGenerator", factory)
    monkeypatch.setenv("SRF_API_KEY", "unit-test-key")
    return factory, instance


BASE_ARGS = ["-veh", "EVSPD01", "-ds", "2025-06-27", "-de", "2025-06-28"]


def test_main_default_flags(captured_generator):
    factory, instance = captured_generator
    assert cli.main(BASE_ARGS) == 0
    kwargs = factory.call_args.kwargs
    assert kwargs["debug_mode"] is False
    assert kwargs["save_figures"] is True
    assert kwargs["fast_mode"] is False
    assert kwargs["overwrite_existing_report"] is True
    instance.generate_report.assert_called_once_with(
        vehicle_registration="EVSPD01", date_start="2025-06-27", date_end="2025-06-28"
    )


def test_main_debug_sets_debug_mode_but_keeps_save_figures(captured_generator):
    factory, _ = captured_generator
    assert cli.main([*BASE_ARGS, "--debug"]) == 0
    kwargs = factory.call_args.kwargs
    assert kwargs["debug_mode"] is True
    assert kwargs["save_figures"] is True


def test_main_raw_only_sets_debug_mode_and_clears_save_figures(captured_generator):
    factory, _ = captured_generator
    assert cli.main([*BASE_ARGS, "--raw-only"]) == 0
    kwargs = factory.call_args.kwargs
    assert kwargs["debug_mode"] is True
    assert kwargs["save_figures"] is False


def test_main_debug_and_raw_only_together(captured_generator):
    factory, _ = captured_generator
    assert cli.main([*BASE_ARGS, "--debug", "--raw-only"]) == 0
    kwargs = factory.call_args.kwargs
    assert (kwargs["debug_mode"], kwargs["save_figures"]) == (True, False)


def test_main_fast_mode_is_passed_through(captured_generator):
    factory, _ = captured_generator
    cli.main([*BASE_ARGS, "--fast"])
    assert factory.call_args.kwargs["fast_mode"] is True


def test_main_out_dir_defaults_to_the_versioned_report_database(captured_generator):
    from jolt_toolkit import __version__

    factory, _ = captured_generator
    cli.main(BASE_ARGS)
    assert factory.call_args.kwargs["report_output_folder"] == (
        f"./excel_report_database/{__version__}"
    )


def test_main_out_dir_override(captured_generator, tmp_path):
    factory, _ = captured_generator
    cli.main([*BASE_ARGS, "--out-dir", str(tmp_path)])
    assert factory.call_args.kwargs["report_output_folder"] == str(tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["-ds", "2025-06-27", "-de", "2025-06-28"],
        ["-veh", "EVSPD01", "-de", "2025-06-28"],
        ["-veh", "EVSPD01", "-ds", "2025-06-27"],
    ],
)
def test_main_missing_required_arguments_exit_2(captured_generator, argv, capsys):
    # ``main`` reconfigures logging with force=True, so the message lands on
    # stderr rather than in caplog.
    factory, _ = captured_generator
    assert cli.main(argv) == 2
    factory.assert_not_called()
    assert "Missing required argument" in capsys.readouterr().err


def test_main_missing_api_key_exits_2_before_building_a_client(
    captured_generator, monkeypatch, capsys
):
    factory, _ = captured_generator
    monkeypatch.setenv("SRF_API_KEY", "")
    assert cli.main(BASE_ARGS) == 2
    factory.assert_not_called()
    assert "SRF_API_KEY" in capsys.readouterr().err


def test_main_returns_3_for_a_vehicle_that_does_not_exist_on_srf(captured_generator):
    from jolt_toolkit.report_generator.general_pipeline import VehicleNotFoundError

    _, instance = captured_generator
    instance.generate_report.side_effect = VehicleNotFoundError("nope")
    assert cli.main(BASE_ARGS) == 3
