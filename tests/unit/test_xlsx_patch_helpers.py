"""Shared xlsx-patcher scaffolding and the weather patcher's pure helpers.

None of these touch the network: ``make_srf_client`` is never called here, and
the OpenWeather cache is exercised entirely inside ``tmp_path``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from openpyxl import Workbook

from jolt_toolkit.report_generator import weather_patcher as wp
from jolt_toolkit.report_generator import xlsx_patch_common as xpc
from jolt_toolkit.report_generator.columns import DIESEL_HEADERS, HEADERS
from jolt_toolkit.report_generator.weather_fetcher.openweather import (
    KeyManager,
    WeatherCache,
)

# ── _parse_report_filename ───────────────────────────────────────────────────


def test_parse_report_filename_valid():
    assert xpc._parse_report_filename(
        Path("/x/y/jolt_report_YK73WFN_20250301_20250601.xlsx")
    ) == ("YK73WFN", "20250301", "20250601")


def test_parse_report_filename_accepts_a_numeric_registration():
    assert (
        xpc._parse_report_filename(Path("jolt_report_CMZ6260_20250101_20250401.xlsx"))[
            0
        ]
        == "CMZ6260"
    )


@pytest.mark.parametrize(
    "name",
    [
        "jolt_report_YK73WFN_20250301_20250601_finetuned.xlsx",  # post-processed
        "jolt_report_YK73WFN_2025030_20250601.xlsx",  # 7-digit date
        "jolt_report_YK73WFN_20250301.xlsx",  # single date
        "report_YK73WFN_20250301_20250601.xlsx",  # wrong prefix
        "jolt_report_YK73WFN_20250301_20250601.xls",  # wrong extension
        "jolt_report_YK73 WFN_20250301_20250601.xlsx",  # space in the reg
    ],
)
def test_parse_report_filename_rejects_non_standard_names(name):
    assert xpc._parse_report_filename(Path(name)) is None


# ── _cell_is_empty ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("", True),
        ("   ", True),
        ("\t\n", True),
        (0, False),
        (0.0, False),
        ("x", False),
        ("=NA()", False),  # a formula string is NOT "empty" for this helper
    ],
)
def test_cell_is_empty(value, expected):
    assert xpc._cell_is_empty(SimpleNamespace(value=value)) is expected


# ── _to_timestamp ────────────────────────────────────────────────────────────


def test_to_timestamp_labels_a_naive_datetime_utc():
    out = xpc._to_timestamp(datetime(2025, 6, 27, 8, 30))
    assert out == pd.Timestamp("2025-06-27T08:30:00Z")
    assert out.tzinfo is not None


def test_to_timestamp_preserves_an_aware_datetime():
    aware = datetime(2025, 6, 27, 8, 30, tzinfo=timezone.utc)
    assert xpc._to_timestamp(aware) == pd.Timestamp("2025-06-27T08:30:00Z")


@pytest.mark.parametrize("value", [None, "not-a-date", object()])
def test_to_timestamp_returns_none_for_unusable_input(value):
    assert xpc._to_timestamp(value) is None


# ── weather_patcher._parse_point ─────────────────────────────────────────────


def test_parse_point_round_trips_the_report_format():
    assert wp._parse_point("Point(52.036800 -0.657200)") == (52.0368, -0.6572)


def test_parse_point_accepts_integers_and_signs():
    assert wp._parse_point("Point(+1 -2)") == (1.0, -2.0)


@pytest.mark.parametrize(
    "text", [None, "", "52.0368, -0.6572", "POINT(1 2)", "Point(1)", 42, float("nan")]
)
def test_parse_point_rejects_anything_else(text):
    assert wp._parse_point(text) == (None, None)


# ── weather_patcher._deg_to_cardinal ─────────────────────────────────────────


@pytest.mark.parametrize(
    "deg, cardinal",
    [
        (0, "N"),
        (45, "NE"),
        (90, "E"),
        (135, "SE"),
        (180, "S"),
        (225, "SW"),
        (270, "W"),
        (315, "NW"),
    ],
)
def test_deg_to_cardinal_eight_points(deg, cardinal):
    assert wp._deg_to_cardinal(deg) == cardinal


@pytest.mark.parametrize("deg", [360, 720, 359.9])
def test_deg_to_cardinal_wraps_past_north(deg):
    assert wp._deg_to_cardinal(deg) == "N"


def test_deg_to_cardinal_rounds_to_the_nearest_point():
    # Sector boundaries sit at multiples of 22.5 deg.
    assert wp._deg_to_cardinal(80) == "E"
    assert wp._deg_to_cardinal(100) == "E"
    assert wp._deg_to_cardinal(60) == "NE"
    assert wp._deg_to_cardinal(200) == "S"


# ── weather_patcher._cell_needs_patch ────────────────────────────────────────


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("", True),
        ("   ", True),
        ("=NA()", True),  # the writer's "no data" convention
        ("=na()", True),
        ("  =NA()  ", True),
        (0, False),
        (12.5, False),
        ("SW", False),
    ],
)
def test_cell_needs_patch(value, expected):
    assert wp._cell_needs_patch(SimpleNamespace(value=value)) is expected


# ── weather_patcher._to_unix_utc ─────────────────────────────────────────────


def test_to_unix_utc_treats_a_naive_datetime_as_utc():
    assert wp._to_unix_utc(datetime(2025, 6, 27, 0, 0)) == 1750982400


def test_to_unix_utc_respects_an_explicit_offset():
    aware = datetime(2025, 6, 27, 0, 0, tzinfo=timezone.utc)
    assert wp._to_unix_utc(aware) == wp._to_unix_utc(datetime(2025, 6, 27, 0, 0))


@pytest.mark.parametrize("value", [None, "2025-06-27", 1750982400])
def test_to_unix_utc_rejects_non_datetimes(value):
    assert wp._to_unix_utc(value) is None


# ── weather_patcher._is_ev_layout (the diesel-corruption guard) ──────────────


def _sheet_with_header(headers):
    wb = Workbook()
    ws = wb.active
    for ci, h in enumerate(headers, start=1):
        ws.cell(1, ci).value = h
    return ws


def test_is_ev_layout_accepts_the_ev_header_row():
    assert wp._is_ev_layout(_sheet_with_header(HEADERS)) is True


def test_is_ev_layout_rejects_a_diesel_workbook():
    # This guard is why the coarse patcher cannot silently write temperature into
    # a diesel report's wrong column.
    assert wp._is_ev_layout(_sheet_with_header(DIESEL_HEADERS)) is False


def test_is_ev_layout_rejects_a_reordered_header_row():
    reordered = (HEADERS[1], HEADERS[0]) + HEADERS[2:]
    assert wp._is_ev_layout(_sheet_with_header(reordered)) is False


def test_is_ev_layout_rejects_an_empty_sheet():
    assert wp._is_ev_layout(Workbook().active) is False


def test_weather_column_indices_track_the_headers():
    # Mirrors the module-level asserts; restated so a HEADERS reorder shows up as
    # a named test failure rather than a bare ImportError.
    for col, name in (
        (wp._COL_LEG_TYPE, "Leg Type"),
        (wp._COL_START_TIME, "Start Time (UTC)"),
        (wp._COL_ORIGIN, "Origin (Lat, Lon)"),
        (wp._COL_END_TIME, "End Time (UTC)"),
        (wp._COL_DEST, "Destination (Lat, Lon)"),
        (wp._COL_TEMP, "Average Temperature (C)"),
        (wp._COL_PRESSURE, "Average Pressure (hPa)"),
        (wp._COL_HUMIDITY, "Average Humidity (%)"),
        (wp._COL_WIND_SPEED, "Average Wind Speed (m/s)"),
        (wp._COL_WIND_DIR, "Average Wind Direction"),
        (wp._COL_WEATHER_TYPE, "Weather Type"),
    ):
        assert col == HEADERS.index(name) + 1


# ── WeatherCache: key quantisation + round trip ──────────────────────────────


def test_weather_cache_key_format_is_pinned(tmp_path):
    # PINNED ON PURPOSE: changing this string invalidates every existing
    # cache/.weather_cache.json entry and silently re-spends the paid quota.
    cache = WeatherCache(tmp_path / "w.json", precision=2, time_bucket_s=3600)
    assert cache._key(52.036812, -0.657234, 1750982461) == "52.04,-0.66,1750982400"


def test_weather_cache_quantises_nearby_points_to_one_key(tmp_path):
    cache = WeatherCache(tmp_path / "w.json")
    # ~1 km grid at precision 2.
    assert cache._key(52.0361, -0.6571, 0) == cache._key(52.0364, -0.6572, 0)


def test_weather_cache_floors_the_timestamp_into_hour_buckets(tmp_path):
    cache = WeatherCache(tmp_path / "w.json")
    base = 1750982400  # exactly on the hour
    assert cache._key(1.0, 2.0, base) == cache._key(1.0, 2.0, base + 3599)
    assert cache._key(1.0, 2.0, base) != cache._key(1.0, 2.0, base + 3600)


def test_weather_cache_honours_custom_precision_and_bucket(tmp_path):
    cache = WeatherCache(tmp_path / "w.json", precision=4, time_bucket_s=600)
    assert cache._key(52.036812, -0.657234, 1750982461) == "52.0368,-0.6572,1750982400"


def test_weather_cache_round_trip(tmp_path):
    path = tmp_path / "w.json"
    cache = WeatherCache(path)
    assert path.exists()  # created on init

    loc = (52.0368, -0.6572, 1750982461)
    weather = (12.5, 1013, 80, 3.4, 225, "Clouds")

    hits, misses = cache.get_batch([loc])
    assert hits == {} and misses == [loc]

    cache.put_batch({loc: weather})
    assert cache.size == 1

    hits, misses = cache.get_batch([loc])
    assert misses == []
    assert hits[loc] == weather  # tuples survive the JSON round trip


def test_weather_cache_hit_serves_a_neighbouring_point_in_the_same_hour(tmp_path):
    cache = WeatherCache(tmp_path / "w.json")
    stored = (52.0368, -0.6572, 1750982400)
    nearby = (52.0364, -0.6571, 1750985999)  # same grid cell, same hour bucket
    cache.put_batch({stored: (10.0, 1000, 70, 1.0, 90, "Clear")})
    hits, misses = cache.get_batch([nearby])
    assert misses == []
    assert hits[nearby][0] == 10.0


def test_weather_cache_fresh_file_has_the_legacy_coarse_shape(tmp_path):
    import json

    path = tmp_path / "w.json"
    WeatherCache(path)  # metadata=None -> the coarse patcher's historical format
    assert json.loads(path.read_text(encoding="utf-8")) == {"cache": {}}


def test_weather_cache_fine_variant_writes_a_metadata_block(tmp_path):
    import json

    path = tmp_path / "fine.json"
    WeatherCache(path, metadata={"description": "fine"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["metadata"] == {"description": "fine"}
    assert payload["cache"] == {}


def test_weather_cache_does_not_rewrite_an_existing_file(tmp_path):
    path = tmp_path / "w.json"
    path.write_text('{"cache": {"1.00,2.00,0": [1, 2, 3, 4, 5, "Clear"]}}', "utf-8")
    cache = WeatherCache(path)
    assert cache.size == 1


# ── KeyManager (no keys configured -> no request can be built) ───────────────


def test_key_manager_without_configured_keys(monkeypatch, caplog):
    monkeypatch.setenv("OPENWEATHER_API_KEYS", "")
    with caplog.at_level("WARNING"):
        keys = KeyManager("UnitTest")
    assert keys.get_key() is None
    assert keys.summary() == {"total_keys": 0, "active": 0, "total_usage": 0}
    assert "OPENWEATHER_API_KEYS not set" in caplog.text


# ── make_srf_client (construction only; the class itself is stubbed) ────────


def test_make_srf_client_wires_the_cache_root_and_api_root(monkeypatch, tmp_path):
    captured = {}

    def _fake_srf_data(**kwargs):
        captured.update(kwargs)
        return "client"

    monkeypatch.setattr(xpc.srf_client, "SRFData", _fake_srf_data)
    monkeypatch.setenv("SRF_API_ROOT", "https://staging.example.org/api/")

    out = xpc.make_srf_client(str(tmp_path), api_key="k")
    assert out == "client"
    assert captured["api_key"] == "k"
    assert captured["root"] == "https://staging.example.org/api/"
    assert captured["verify"] is True
    # The on-disk HTTP body cache lands under <cache_dir>/srf_http.
    assert (tmp_path / "srf_http").is_dir()
    assert captured["cache"] is not None


def test_make_srf_client_defaults_the_cache_dir_to_the_env_override(
    monkeypatch, tmp_path
):
    captured = {}
    monkeypatch.setattr(
        xpc.srf_client, "SRFData", lambda **kw: captured.update(kw) or "client"
    )
    monkeypatch.setenv("JOLT_CACHE_DIR", str(tmp_path / "cache-root"))
    xpc.make_srf_client(None, api_key=None)
    assert (tmp_path / "cache-root" / "srf_http").is_dir()


def test_make_srf_client_without_a_cache_dir_passes_no_cache(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        xpc.srf_client, "SRFData", lambda **kw: captured.update(kw) or "client"
    )
    xpc.make_srf_client("", api_key=None, root="https://x/api/")
    assert captured["cache"] is None


def test_key_manager_rotation_and_disable(monkeypatch):
    monkeypatch.setenv("OPENWEATHER_API_KEYS", "key-one, key-two ,")
    keys = KeyManager("UnitTest")
    assert keys.summary()["total_keys"] == 2
    first = keys.get_key()
    assert first == "key-one"
    keys.increment(first)
    keys.disable(first)
    assert keys.get_key() == "key-two"
    assert keys.summary() == {"total_keys": 2, "active": 1, "total_usage": 1}
