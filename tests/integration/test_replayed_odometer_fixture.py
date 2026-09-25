"""A value sent again on a fixture's wake-up row no longer reaches a distance.

On the speed fixture the trip of 06:21:09 leaves from the ``MOVEMENT`` row, whose
odometer is the trip's start anchor. Giving that row the value the leg read at
the start of its 05:00:40 trip — 35574.465 km, 46.1 km behind — reproduces the
replayed-odometer defect on real data: the trip took the old value as its start
and its distance grew by 46.1 km. Blanked before the detectors read it, the trip
starts at the reading before the row instead, and nothing else changes.
"""

from __future__ import annotations

import pandas as pd

ALIAS = "EVSPD01"
WAKE_UP = "2025-06-27T06:21:09.000Z"
REPLAYED = "35574.465"
TRIP_START = "2025-06-27T06:21:09"


def _segment(frame, frozen_configs):
    """``run_segment_detection`` exactly as the fixture runner calls it."""
    from report_generator.segment_algorithms import run_segment_detection

    nominal = frozen_configs["vehicles"][ALIAS]["nominal_kwh"]
    return run_segment_detection(
        frame,
        reg=ALIAS,
        suffix="fixture",
        out_dir=None,
        generate_validation_fig=False,
        cap_lo=nominal * 0.5,
        cap_hi=nominal * 2.0,
    )


def _replayed_frame(load_raw_telematics) -> pd.DataFrame:
    frame = load_raw_telematics(ALIAS)
    at_wake_up = frame["eventDatetime"] == WAKE_UP
    assert at_wake_up.sum() == 1
    assert frame.loc[at_wake_up, "odometer"].tolist() == ["35620.56"]
    frame.loc[at_wake_up, "odometer"] = REPLAYED
    return frame


def test_the_trip_leaving_from_the_row_starts_at_the_reading_before_it(
    frozen_configs, load_raw_telematics, load_golden, serialise
):
    _, trips = _segment(_replayed_frame(load_raw_telematics), frozen_configs)
    golden = {
        seg["start_time"]: seg
        for seg in load_golden(f"segments_{ALIAS}.json")["discharge"]
    }
    trip = next(seg for seg in serialise(trips) if seg["start_time"] == TRIP_START)
    # The reading before the row is the ignition-on row at 06:20:55, 35620.555 km;
    # the golden start was the row's own 35620.56 km, 5 m on. The end anchor, the
    # energy and the SOC are the golden's.
    assert golden[TRIP_START]["odo_start_km"] == 35620.56
    assert trip["odo_start_km"] == 35620.555
    unchanged = set(trip) - {"odo_start_km", "ep_audit"}
    assert {k: trip[k] for k in unchanged} == {
        k: golden[TRIP_START][k] for k in unchanged
    }


def test_every_other_segment_matches_its_golden(
    frozen_configs, load_raw_telematics, load_golden, serialise
):
    charges, trips = _segment(_replayed_frame(load_raw_telematics), frozen_configs)
    golden = load_golden(f"segments_{ALIAS}.json")
    assert serialise(charges) == golden["charge"]
    others = [seg for seg in serialise(trips) if seg["start_time"] != TRIP_START]
    assert others == [
        seg for seg in golden["discharge"] if seg["start_time"] != TRIP_START
    ]
    assert len(others) == len(golden["discharge"]) - 1
