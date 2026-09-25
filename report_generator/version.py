"""Version and report-data-namespace constants.

Read straight from source: the package is vendored, not installed, so there is
no dist metadata to look up. Bump ``__version__`` here and append a section to
``doc/versions.md`` on every release.
"""

from __future__ import annotations

__version__ = "3.8.1"

# Active report-data namespace under ``excel_report_database/``. A release that
# can change report cells advances this alongside ``__version__``; a release
# proven output-identical leaves it pointing at the most recent populated data
# tree. Keeping this mapping machine-readable prevents version-defaulted tools
# from selecting an absent directory after a behaviour-preserving release.
DATA_NAMESPACE = "3.3.0"
