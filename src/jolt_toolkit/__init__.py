"""
JOLT Toolkit — telemetry data toolkit for electric heavy goods vehicles.

Sub-packages:
  report_generator              report-generation pipeline
  analysis                      shared analysis utilities promoted from the
                                data_analysis_workspace sub-projects

Analysis figures:
  data_analysis_workspace/scripts/generate_figures.py  — Excel report analysis
  figures (standalone script, replacing the former excel_plotter)
"""

# Version of the jolt_toolkit workspace. Read straight from source — the folder
# is vendored, not installed, so there is no dist metadata to look up. Bump here
# and append a section to versions.md on every release (see git-workflow.md).
__version__ = "3.2.0"
