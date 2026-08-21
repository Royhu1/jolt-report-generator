"""The code version and the report-data namespace are two independent facts.

``__version__`` names the code revision; ``DATA_NAMESPACE`` names the report
tree the defaults write into. A release proven to change no cell bumps the
former and leaves the latter pointing at the tree that already holds the
reports, so every default consumer must resolve the namespace — and resolve it
at CALL time, or a test monkeypatch (or a deployer's override) would be frozen
at import.
"""

from __future__ import annotations

import re

from jolt_toolkit import DATA_NAMESPACE, __version__, report_generator
from jolt_toolkit.report_generator import cli, paths
from jolt_toolkit.report_generator._generator import JOLTReportGenerator

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class _FakeGenerator:
    """Records the kwargs the caller resolved, without touching SRF."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeGenerator.last = self

    def generate_report(self, **kwargs):
        self.generate_kwargs = kwargs


def test_both_constants_are_semver_and_the_namespace_never_leads():
    assert SEMVER_RE.fullmatch(__version__)
    assert SEMVER_RE.fullmatch(DATA_NAMESPACE)
    version_parts = tuple(int(part) for part in __version__.split("."))
    namespace_parts = tuple(int(part) for part in DATA_NAMESPACE.split("."))
    assert namespace_parts <= version_parts


def test_default_report_root_is_read_at_call_time(monkeypatch):
    assert paths.default_report_root() == f"./excel_report_database/{DATA_NAMESPACE}"
    monkeypatch.setattr("jolt_toolkit.DATA_NAMESPACE", "9.9.9")
    assert paths.default_report_root() == "./excel_report_database/9.9.9"


def test_the_public_generator_defaults_to_the_namespace(monkeypatch):
    monkeypatch.setattr(
        JOLTReportGenerator, "_make_srf_data", staticmethod(lambda **_kwargs: object())
    )
    assert JOLTReportGenerator().report_output_folder == (
        f"./excel_report_database/{DATA_NAMESPACE}"
    )


def test_an_explicit_output_folder_still_wins(monkeypatch):
    monkeypatch.setattr(
        JOLTReportGenerator, "_make_srf_data", staticmethod(lambda **_kwargs: object())
    )
    gen = JOLTReportGenerator(report_output_folder="/data/reports")
    assert gen.report_output_folder == "/data/reports"


def test_the_convenience_wrapper_defaults_to_the_namespace(monkeypatch):
    monkeypatch.setattr(report_generator, "JOLTReportGenerator", _FakeGenerator)
    report_generator.generate_report("YK73WFN", "2026-01-01", "2026-01-02")
    assert _FakeGenerator.last.kwargs["report_output_folder"] == (
        f"./excel_report_database/{DATA_NAMESPACE}"
    )


def test_the_module_cli_defaults_to_the_namespace(monkeypatch):
    monkeypatch.setenv("SRF_API_KEY", "test-only")
    monkeypatch.setattr(
        "jolt_toolkit.report_generator._generator.JOLTReportGenerator", _FakeGenerator
    )
    assert cli.main(["-veh", "YK73WFN", "-ds", "2026-01-01", "-de", "2026-01-02"]) == 0
    assert _FakeGenerator.last.kwargs["report_output_folder"] == (
        f"./excel_report_database/{DATA_NAMESPACE}"
    )
