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
from pathlib import Path

import report_generator
from report_generator import DATA_NAMESPACE, __version__, cli, paths
from report_generator._generator import JOLTReportGenerator

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
VERSIONS_MD = Path(__file__).resolve().parents[2] / "doc" / "versions.md"


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


def test_the_newest_version_history_section_names_the_active_namespace():
    """``doc/versions.md`` and ``version.py`` cannot drift apart unnoticed.

    Every release appends a section in the same change as the bump, and that
    section states which data namespace the release writes into — so the newest
    section must open with ``__version__`` and name ``DATA_NAMESPACE``.
    """
    history = VERSIONS_MD.read_text(encoding="utf-8")
    latest_section = history.rsplit("\n## ", maxsplit=1)[-1]

    assert latest_section.startswith(f"{__version__} ")
    assert "Data namespace:" in latest_section
    assert f"`{DATA_NAMESPACE}/`" in latest_section


def test_the_version_history_is_append_forward():
    """Every release heading is SemVer, in ascending order, newest last, none twice.

    Together with the test above this is the release discipline: bumping
    ``report_generator/version.py`` without appending its section — or appending
    it anywhere but at the bottom — fails the suite.
    """
    from report_generator import version

    history = VERSIONS_MD.read_text(encoding="utf-8")
    headings = re.findall(r"^## (\S+) ", history, flags=re.MULTILINE)
    assert headings, "doc/versions.md has no release sections"
    assert all(SEMVER_RE.fullmatch(h) for h in headings), headings
    releases = [tuple(int(part) for part in h.split(".")) for h in headings]
    assert releases == sorted(set(releases))
    assert headings[-1] == version.__version__ == __version__


def test_default_report_root_is_read_at_call_time(monkeypatch):
    assert paths.default_report_root() == f"./excel_report_database/{DATA_NAMESPACE}"
    monkeypatch.setattr("report_generator.DATA_NAMESPACE", "9.9.9")
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
        "report_generator._generator.JOLTReportGenerator", _FakeGenerator
    )
    assert cli.main(["-veh", "YK73WFN", "-ds", "2026-01-01", "-de", "2026-01-02"]) == 0
    assert _FakeGenerator.last.kwargs["report_output_folder"] == (
        f"./excel_report_database/{DATA_NAMESPACE}"
    )
