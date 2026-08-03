"""
Weather data fetching module for JOLT Report.

Exposes the fine-grained weather patcher used by the report pipeline. The coarse
patcher lives in ``report_generator.weather_patcher``.
"""

from .fine_grained_patcher import FineGrainedWeatherPatcher

__all__ = [
    "FineGrainedWeatherPatcher",
]
