"""The fixture maker's de-identification, on synthetic frames.

``tests/fixtures/make_fixture.py`` turns a real raw artefact into a committed
fixture, so its guarantees are privacy guarantees and are pinned here:

* the GPS track keeps its geometry exactly — every great-circle distance and
  every angle between two directions — while its location and orientation are
  gone: it is moved onto the synthetic origin (0.5, 0.5) and spun by a random
  angle that is never returned or recorded;
* headings turn with the track, since an untouched heading minus the rotated
  track's bearing would be the angle;
* driver columns are dropped, the vehicle identity becomes the alias, and a
  registration left anywhere in the data stops the fixture being written;
* everything else survives character for character.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "make_fixture.py"
_SPEC = importlib.util.spec_from_file_location("make_fixture", _PATH)
mf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mf)

ALIAS = "EVUT01"
EARTH_RADIUS_KM = 6371.0088


# ── Spherical geometry, computed independently of the module under test ─────


def _xyz(lat, lon):
    phi, lam = np.radians(np.asarray(lat, float)), np.radians(np.asarray(lon, float))
    return np.stack(
        [np.cos(phi) * np.cos(lam), np.cos(phi) * np.sin(lam), np.sin(phi)], axis=-1
    )


def _distance_km(a, b):
    """Great-circle distance between unit vectors (atan2 form: exact for short arcs)."""
    return EARTH_RADIUS_KM * np.arctan2(
        np.linalg.norm(np.cross(a, b), axis=-1), np.sum(a * b, axis=-1)
    )


def _bearing_deg(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing from point 1 to point 2, clockwise from north."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(np.asarray(lon2) - np.asarray(lon1))
    y = np.sin(dl) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl)
    return np.mod(np.degrees(np.arctan2(y, x)), 360.0)


def _angle_gap(a, b):
    """Smallest signed difference a - b of two angles, in degrees."""
    return (np.asarray(a) - np.asarray(b) + 180.0) % 360.0 - 180.0


# ── Synthetic raw frames ─────────────────────────────────────────────────────


def _track(n=60, seed=7):
    """A drive near 54 N / 1 W: 150-900 m legs, wandering heading."""
    rng = np.random.default_rng(seed)
    lat, lon = [53.980364], [-1.076409]
    heading = rng.uniform(0, 360)
    for _ in range(n - 1):
        heading += rng.normal(0, 35)
        step_km = rng.uniform(0.15, 0.9)
        dlat = step_km / 111.2 * np.cos(np.radians(heading))
        dlon = (
            step_km
            / (111.2 * np.cos(np.radians(lat[-1])))
            * np.sin(np.radians(heading))
        )
        lat.append(lat[-1] + dlat)
        lon.append(lon[-1] + dlon)
    return np.array(lat), np.array(lon)


def _ev_frame():
    lat, lon = _track()
    n = len(lat)
    heading = np.append(_bearing_deg(lat[:-1], lon[:-1], lat[1:], lon[1:]), 0.0)
    t0 = pd.Timestamp("2025-06-27T08:00:00Z")
    frame = pd.DataFrame(
        {
            "vehicleId": ["116"] * n,
            "eventDatetime": [
                (t0 + pd.Timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                for i in range(n)
            ],
            "latitude": [repr(float(v)) for v in lat],
            "longitude": [repr(float(v)) for v in lon],
            "odometer": [f"{16552.925 + i * 0.5:.3f}" for i in range(n)],
            "driver1_working_state": ["2"] * n,
            "electricBatteryLevelPercent": [str(90 - i // 6) for i in range(n)],
            "driver1_id": ["DRV-000123"] * n,
            "gnss_heading": [repr(float(v)) for v in heading],
            "gnss_latitude": [repr(float(v)) for v in lat],
            "gnss_longitude": [repr(float(v)) for v in lon],
            "trigger_context": ['a, quoted "value"'] * n,
        }
    )
    # Realistic gaps: a stretch without a fix, and a gnss pair that drops out.
    frame.loc[10:12, ["latitude", "longitude"]] = ""
    frame.loc[20:22, ["gnss_latitude", "gnss_longitude", "gnss_heading"]] = ""
    return frame


def _logger_frame():
    lat, lon = _track(n=40, seed=11)
    n = len(lat)
    index = pd.Index(
        [f"2025-10-07 05:{30 + i // 60:02d}:{i % 60:02d}" for i in range(n)]
    )
    bearing = np.append(_bearing_deg(lat[:-1], lon[:-1], lat[1:], lon[1:]), 0.0)
    return pd.DataFrame(
        {
            "CCVS wheel based vehicle speed": ["52.5"] * n,
            "DI driver 1 identification": ["GB-DRIVER-CARD"] * n,
            "EEC1 driver's demand engine percent torque": ["41"] * n,
            "2 latitude": [repr(float(v)) for v in lat],
            "2 longitude": [repr(float(v)) for v in lon],
            "2 bearing": [repr(float(v)) for v in bearing],
            "VIN vehicle identification number": ["YS2P4X20005752597"] * n,
            "TCO1 driver 2 working state": ["0"] * n,
        },
        index=index,
    )


def _points(frame, lat_col, lon_col):
    ok = (frame[lat_col] != "") & (frame[lon_col] != "")
    return (
        ok,
        frame.loc[ok, lat_col].astype(float).to_numpy(),
        frame.loc[ok, lon_col].astype(float).to_numpy(),
    )


@pytest.fixture
def ev():
    source = _ev_frame()
    return source, mf.anonymise(source, alias=ALIAS, rng=np.random.default_rng(1))


# ── Geometry survives exactly ────────────────────────────────────────────────


def test_every_great_circle_distance_is_preserved(ev):
    source, out = ev
    ok, lat, lon = _points(source, "latitude", "longitude")
    _, lat2, lon2 = _points(out, "latitude", "longitude")
    a, b = _xyz(lat, lon), _xyz(lat2, lon2)
    before = _distance_km(a[:, None, :], a[None, :, :])
    after = _distance_km(b[:, None, :], b[None, :, :])
    assert before.max() > 5.0  # a real track, not a point
    np.testing.assert_allclose(after, before, rtol=0, atol=1e-9)  # < 1 µm


def test_the_angles_between_legs_are_preserved(ev):
    # Turning angles at every point — the shape of the route.
    source, out = ev
    _, lat, lon = _points(source, "latitude", "longitude")
    _, lat2, lon2 = _points(out, "latitude", "longitude")
    turns = _angle_gap(
        _bearing_deg(lat[1:-1], lon[1:-1], lat[2:], lon[2:]),
        _bearing_deg(lat[1:-1], lon[1:-1], lat[:-2], lon[:-2]),
    )
    turns2 = _angle_gap(
        _bearing_deg(lat2[1:-1], lon2[1:-1], lat2[2:], lon2[2:]),
        _bearing_deg(lat2[1:-1], lon2[1:-1], lat2[:-2], lon2[:-2]),
    )
    np.testing.assert_allclose(_angle_gap(turns2, turns), 0.0, atol=1e-7)


def test_the_track_is_moved_onto_the_synthetic_origin(ev):
    _source, out = ev
    xyz = np.concatenate(
        [
            _xyz(*_points(out, "latitude", "longitude")[1:]),
            _xyz(*_points(out, "gnss_latitude", "gnss_longitude")[1:]),
        ]
    )
    c = xyz.sum(axis=0)
    c /= np.linalg.norm(c)
    assert np.degrees(np.arcsin(c[2])) == pytest.approx(0.5, abs=1e-9)
    assert np.degrees(np.arctan2(c[1], c[0])) == pytest.approx(0.5, abs=1e-9)
    # Nothing of the real location is left: the track is now near the equator.
    _ok, lat2, lon2 = _points(out, "latitude", "longitude")
    assert np.abs(lat2 - 0.5).max() < 1.0 and np.abs(lon2 - 0.5).max() < 1.0


def test_both_position_pairs_move_by_one_rotation(ev):
    # Distances ACROSS the two pairs survive, so they were moved together.
    source, out = ev
    rows = source.index[(source["latitude"] != "") & (source["gnss_latitude"] != "")]
    first, later = rows[:-5], rows[5:]

    def cross_distance(frame):
        a = _xyz(
            frame.loc[first, "latitude"].astype(float),
            frame.loc[first, "longitude"].astype(float),
        )
        b = _xyz(
            frame.loc[later, "gnss_latitude"].astype(float),
            frame.loc[later, "gnss_longitude"].astype(float),
        )
        return _distance_km(a, b)

    np.testing.assert_allclose(cross_distance(out), cross_distance(source), atol=1e-9)


def test_headings_turn_with_the_track(ev):
    # The heading's angle to the direction of travel is kept; an untouched
    # heading would expose the rotation as exactly that difference.
    source, out = ev
    rows = [
        r
        for r in range(len(source) - 1)
        if source.loc[r, "gnss_heading"] != ""
        and source.loc[r + 1, "gnss_latitude"] != ""
        and source.loc[r, "gnss_latitude"] != ""
    ]

    def offset(frame):
        lat = frame["gnss_latitude"].replace("", np.nan).astype(float).to_numpy()
        lon = frame["gnss_longitude"].replace("", np.nan).astype(float).to_numpy()
        hdg = frame["gnss_heading"].replace("", np.nan).astype(float).to_numpy()
        r = np.array(rows)
        return _angle_gap(hdg[r], _bearing_deg(lat[r], lon[r], lat[r + 1], lon[r + 1]))

    np.testing.assert_allclose(offset(source), 0.0, atol=1e-7)  # the precondition
    np.testing.assert_allclose(offset(out), 0.0, atol=1e-6)
    # ... while the headings themselves did change.
    assert (
        np.abs(
            _angle_gap(
                out.loc[rows, "gnss_heading"].astype(float),
                source.loc[rows, "gnss_heading"].astype(float),
            )
        ).min()
        > 1.0
    )


def test_the_rotation_is_random_and_never_recorded(capsys):
    source = _ev_frame()
    first = mf.anonymise(source, alias=ALIAS)
    second = mf.anonymise(source, alias=ALIAS)
    assert not first["latitude"].equals(second["latitude"])
    # Only the frame comes back: no angle attribute, no metadata, no output.
    assert isinstance(first, pd.DataFrame) and first.attrs == {}
    assert capsys.readouterr() == ("", "")


# ── Identity ─────────────────────────────────────────────────────────────────


def test_driver_columns_are_dropped_and_the_vehicle_becomes_the_alias(ev):
    source, out = ev
    assert not [c for c in out.columns if c.startswith("driver")]
    assert set(out["vehicleId"]) == {ALIAS}
    assert "DRV-000123" not in out.to_csv()


def test_the_driver_rule_spares_the_torque_demand_signal():
    assert mf.driver_columns(
        [
            "driver1_id",
            "driver2_working_state",
            "DI driver 1 identification",
            "TCO1 driver 2 working state",
            "EEC1 driver's demand engine percent torque",
            "CCVS wheel based vehicle speed",
        ]
    ) == [
        "driver1_id",
        "driver2_working_state",
        "DI driver 1 identification",
        "TCO1 driver 2 working state",
    ]


def test_a_logger_frame_loses_its_driver_and_vin_identity():
    source = _logger_frame()
    out = mf.anonymise(source, alias="DSLUT01", rng=np.random.default_rng(3))
    assert "DI driver 1 identification" not in out.columns
    assert "TCO1 driver 2 working state" not in out.columns
    assert "EEC1 driver's demand engine percent torque" in out.columns
    assert set(out["VIN vehicle identification number"]) == {"DSLUT01"}
    assert list(out.index) == list(source.index)  # the time index is verbatim
    _, lat, lon = _points(source, "2 latitude", "2 longitude")
    _, lat2, lon2 = _points(out, "2 latitude", "2 longitude")
    a, b = _xyz(lat, lon), _xyz(lat2, lon2)
    np.testing.assert_allclose(
        _distance_km(b[:-1], b[1:]), _distance_km(a[:-1], a[1:]), atol=1e-9
    )


def test_a_registration_left_in_the_data_stops_the_fixture():
    source = _ev_frame()
    source.loc[3, "trigger_context"] = "YK73 WFN depot"
    with pytest.raises(ValueError, match="registration"):
        mf.anonymise(source, alias=ALIAS, registration="YK73WFN")


@pytest.mark.parametrize(
    "registration, written",
    [
        # A 3+4 plate (Northern Ireland style) and 3+3 ones, as the fleet has.
        ("CMZ6260", "cmz 6260"),
        ("CMZ6260", "CMZ-6260"),
        ("CMZ6260", "CMZ_6260"),
        ("CMZ6260", "CMZ\t6260"),
        ("CMZ6260", "CMZ 6260"),  # no-break space
        ("CMZ6260", "CMZ  6260"),  # a doubled separator
        ("N88GNW", "N88 GNW"),
        ("N88GNW", "n88-gnw"),
        ("T88RNW", "T88 RNW"),
        # The current 4+3 format, and the compact form of a spaced argument.
        ("YK73WFN", "yk73 wfn"),
        ("YK73WFN", "yk73wfn"),
        ("CMZ 6260", "CMZ6260"),
    ],
)
def test_every_spelling_of_the_registration_stops_the_fixture(registration, written):
    source = _ev_frame()
    source.loc[3, "trigger_context"] = f"depot {written} bay 4"
    with pytest.raises(ValueError, match="registration"):
        mf.anonymise(source, alias=ALIAS, registration=registration)


def test_a_registration_split_across_two_cells_is_not_found():
    # Adjacent cells "CMZ" and "6260" are two values, not the registration.
    frame = pd.DataFrame({"note": ["CMZ", "x"], "code": ["6260", "y"]})
    out = mf.anonymise(frame, alias=ALIAS, registration="CMZ6260")
    assert out.equals(frame)


@pytest.mark.parametrize(
    "text",
    [
        "CMZ,6260",  # two CSV cells
        '"CMZ","6260"',  # two quoted cells
        "CMZ\n6260",  # two rows
        "CMZ\r\n6260",
        "CMZ 6261",  # a different plate
        "CMZ 626",
        "depot 6260 CMZ",
    ],
)
def test_the_registration_pattern_matches_nothing_else(text):
    assert mf.registration_pattern("CMZ6260").search(text) is None


def test_a_registration_needs_letters_or_digits():
    with pytest.raises(ValueError, match="no letters or digits"):
        mf.registration_pattern(" - ")


# ── Everything else verbatim ─────────────────────────────────────────────────


def test_every_other_value_is_kept_character_for_character(ev):
    source, out = ev
    kept = [c for c in source.columns if not c.startswith("driver")]
    assert list(out.columns) == kept  # order kept, names unchanged
    changed = {"vehicleId", "latitude", "longitude", "gnss_latitude", "gnss_longitude"}
    changed.add("gnss_heading")
    for col in set(kept) - changed:
        assert out[col].equals(source[col]), col


def test_missing_positions_stay_missing_and_bad_ones_are_blanked():
    source = _ev_frame()
    source.loc[30, "latitude"] = "0x1F"  # present but not a number
    source.loc[31, "longitude"] = ""  # half a pair
    out = mf.anonymise(source, alias=ALIAS, rng=np.random.default_rng(5))
    assert (out.loc[10:12, ["latitude", "longitude"]] == "").all().all()
    assert (out.loc[30, ["latitude", "longitude"]] == "").all()
    assert (out.loc[31, ["latitude", "longitude"]] == "").all()


@pytest.mark.parametrize("kind", ["ev", "diesel"])
def test_the_written_fixture_reads_back_as_written(tmp_path, kind):
    frame = _ev_frame() if kind == "ev" else _logger_frame()
    out = mf.anonymise(frame, alias=ALIAS, rng=np.random.default_rng(9))
    path = tmp_path / "fixture.csv"
    path.write_text(mf.to_csv_text(out, kind), encoding="utf-8", newline="")
    assert mf.detect_kind(path) == kind
    back = mf.read_raw(path, kind)
    assert back.equals(out)
    # A value holding a comma and quotes survives the CSV quoting too.
    if kind == "ev":
        assert back.loc[0, "trigger_context"] == 'a, quoted "value"'


# ── Paths, names and the frozen config ───────────────────────────────────────


def test_the_registration_is_taken_from_a_standard_artefact_path(tmp_path):
    ev_path = tmp_path / "3.3.0" / "YK73WFN" / "raw_telematics" / "raw_x.csv"
    logger_path = tmp_path / "3.3.0" / "NK74YTA" / "raw_logger_v2" / "logger_x.csv"
    assert mf.infer_registration(ev_path) == "YK73WFN"
    assert mf.infer_registration(logger_path) == "NK74YTA"
    assert mf.infer_registration(tmp_path / "raw_x.csv") is None


def test_the_file_name_loses_every_spelling_of_the_registration():
    source = Path("raw_YK73WFN_2025-01-01_yk73 wfn.csv")
    assert mf.fixture_file_name(source, ALIAS, "YK73WFN") == (
        f"raw_{ALIAS}_2025-01-01_{ALIAS}.csv"
    )


@pytest.mark.parametrize(
    "name, registration, expected",
    [
        ("raw_N88 GNW_2025.csv", "N88GNW", f"raw_{ALIAS}_2025.csv"),
        (
            "logger_cmz-6260_2025-06-27_0001.csv",
            "CMZ6260",
            f"logger_{ALIAS}_2025-06-27_0001.csv",
        ),
        ("raw_2025-06-27_0003.csv", "N88GNW", "raw_2025-06-27_0003.csv"),
    ],
)
def test_the_file_name_loses_a_plate_however_it_is_split(name, registration, expected):
    assert mf.fixture_file_name(Path(name), ALIAS, registration) == expected


_LIVE_PIPELINES = {"ut_speed_01": {"branch": "speed", "mass_agg": "iqr_median"}}
_LIVE_EV = {
    "srf_reg": "UT73 ABC",
    "nominal_kwh": 540,
    "effective_capacity_kwh": 360.6,
    "effective_capacity_quarterly": {"20250101_20250401": {"kwh": 360.6, "n": 9}},
    "make": "Volvo",
    "model": "FM Electric",
    "description": "2024 Volvo artic",
    "vin": "YV2XB40A4RA341763",
    "operator": "SOME_OPERATOR",
    "pipeline": "ut_speed_01",
    "mass_col": "gross_combination_vehicle_weight",
}


def test_the_frozen_entry_keeps_parameters_and_drops_identity_and_state():
    entry, pipeline = mf.frozen_entry(_LIVE_EV, _LIVE_PIPELINES, ALIAS, "ev")
    assert entry["srf_reg"] == ALIAS
    assert entry["pipeline"] == "evut01_speed"
    assert pipeline == ("evut01_speed", _LIVE_PIPELINES["ut_speed_01"])
    for key in ("vin", "description", "operator", "effective_capacity_quarterly"):
        assert key not in entry
    assert (entry["nominal_kwh"], entry["effective_capacity_kwh"]) == (540, 360.6)
    assert entry["mass_col"] == "gross_combination_vehicle_weight"


def test_a_diesel_frozen_entry_gets_a_dispatch_marker_not_a_pipeline():
    diesel = {"srf_reg": "UT74 DSL", "fuel_type": "DIESEL", "pipeline": "x"}
    entry, pipeline = mf.frozen_entry(diesel, {}, "DSLUT01", "diesel")
    assert entry["pipeline"] == "dslut01_diesel_logger"
    assert pipeline is None


def test_the_fixture_kind_must_match_the_vehicle():
    with pytest.raises(ValueError, match="diesel fixture its logger CSV"):
        mf.frozen_entry(_LIVE_EV, _LIVE_PIPELINES, ALIAS, "diesel")


# ── The CLI, against a scratch fixture tree ──────────────────────────────────


@pytest.fixture
def scratch_fixtures(tmp_path, monkeypatch):
    """Point the maker at a scratch fixture tree and synthetic live configs."""
    root = tmp_path / "fixtures"
    (root / "configs").mkdir(parents=True)
    registry = {
        "EVSPD01": {"path": "raw/EVSPD01/raw_2025-06-27_0000.csv", "kind": "ev"}
    }
    (root / "raw_fixtures.json").write_text(json.dumps(registry), "utf-8")
    (root / "configs" / "vehicles.json").write_text(json.dumps({}), "utf-8")
    (root / "configs" / "pipelines.json").write_text(json.dumps({}), "utf-8")
    monkeypatch.setattr(mf, "FIXTURES_DIR", root)
    monkeypatch.setattr(mf, "REGISTRY_PATH", root / "raw_fixtures.json")
    monkeypatch.setattr(mf, "FROZEN_VEHICLES", root / "configs" / "vehicles.json")
    monkeypatch.setattr(mf, "FROZEN_PIPELINES", root / "configs" / "pipelines.json")
    monkeypatch.setattr(
        mf, "_live_configs", lambda: ({"UT73ABC": _LIVE_EV}, _LIVE_PIPELINES)
    )
    source = tmp_path / "db" / "UT73ABC" / "raw_telematics" / "raw_2025-06-27_0003.csv"
    source.parent.mkdir(parents=True)
    source.write_text(mf.to_csv_text(_ev_frame(), "ev"), encoding="utf-8")
    return root, source


def test_the_cli_writes_registers_and_freezes(scratch_fixtures):
    root, source = scratch_fixtures
    argv = [str(source), "--alias", "evut01", "--rows", "5:45"]
    assert mf.main([*argv, "--config-from", "UT73ABC"]) == 0

    fixture = root / "raw" / ALIAS / "raw_2025-06-27_0003.csv"
    written = mf.read_raw(fixture, "ev")
    assert len(written) == 40
    assert set(written["vehicleId"]) == {ALIAS}
    assert "UT73" not in fixture.read_text(encoding="utf-8")
    registry = json.loads((root / "raw_fixtures.json").read_text("utf-8"))
    assert registry[ALIAS] == {
        "path": f"raw/{ALIAS}/raw_2025-06-27_0003.csv",
        "kind": "ev",
    }
    assert "EVSPD01" in registry  # existing entries kept
    frozen = json.loads((root / "configs" / "vehicles.json").read_text("utf-8"))
    assert frozen[ALIAS]["pipeline"] == "evut01_speed"
    assert "vin" not in frozen[ALIAS]
    pipelines = json.loads((root / "configs" / "pipelines.json").read_text("utf-8"))
    assert "evut01_speed" in pipelines


def test_the_cli_refuses_to_overwrite_and_writes_nothing(scratch_fixtures):
    root, source = scratch_fixtures
    assert mf.main([str(source), "--alias", ALIAS]) == 0
    snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(SystemExit, match="already registered"):
        mf.main([str(source), "--alias", ALIAS])
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == snapshot


def test_the_cli_writes_nothing_when_the_registration_survives(scratch_fixtures):
    root, source = scratch_fixtures
    frame = _ev_frame()
    frame.loc[0, "trigger_context"] = "UT73ABC"
    source.write_text(mf.to_csv_text(frame, "ev"), encoding="utf-8")
    with pytest.raises(SystemExit, match="registration"):
        mf.main([str(source), "--alias", ALIAS])
    assert not (root / "raw").exists()
    assert ALIAS not in json.loads((root / "raw_fixtures.json").read_text("utf-8"))
