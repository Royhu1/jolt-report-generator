"""The diesel pipeline's cached-CSV loader, on an inline frame.

``_logger_df_from_csv`` rebuilds the logger frame from a persisted raw CSV. It
must rename the Channel-2 GPS columns to the internal ``_lat`` / ``_lon`` names
exactly as the live SRF path does, or a report regenerated from cached CSVs loses
its origin / destination coordinates. The fixture-driven version of this check
(over the real DSL01 logger CSV) is in
``integration/test_diesel_pipeline_fixture.py``; this one pins the rename itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd

from report_generator.diesel_pipeline import _logger_df_from_csv


def test_cached_csv_gps_columns_reach_the_internal_coordinate_names():
    source = pd.DataFrame(
        {
            "CCVS wheel based vehicle speed": [20.0, 25.0],
            "2 latitude": [0.51, 0.52],
            "2 longitude": [0.49, 0.48],
        },
        index=["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z"],
    )

    with patch("report_generator.diesel_pipeline.pd.read_csv", return_value=source):
        result = _logger_df_from_csv(Path("dummy.csv"), {})

    assert result is not None
    assert list(result["_lat"]) == [0.51, 0.52]
    assert list(result["_lon"]) == [0.49, 0.48]
    assert "2 latitude" not in result
    assert "2 longitude" not in result
