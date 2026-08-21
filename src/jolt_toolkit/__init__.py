"""
JOLT Toolkit — telemetry data toolkit for electric heavy goods vehicles.

Sub-packages:
  report_generator              report-generation pipeline
  analysis                      shared analysis utilities promoted from the
                                data_analysis_workspace sub-projects
"""

# Version of the jolt_toolkit workspace. Read straight from source — the folder
# is vendored, not installed, so there is no dist metadata to look up. Bump here
# and append a section to versions.md on every release (see git-workflow.md).
__version__ = "3.4.0"

# Active report-data namespace under ``excel_report_database/``. A release that
# can change report cells advances this alongside ``__version__``; a release
# proven output-identical leaves it pointing at the most recent populated data
# tree. Keeping this mapping machine-readable prevents version-defaulted tools
# from selecting an absent directory after a behaviour-preserving release.
DATA_NAMESPACE = "3.3.0"
