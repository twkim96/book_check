"""Portable runtime paths shared by the file_check backend.

Code lives below ``backend/`` while mutable state and generated indexes stay at
the project root for compatibility with the existing control-server actions.
Every machine-specific library path can be overridden through an environment
variable without editing source files.
"""

from __future__ import annotations

import os
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(
    os.environ.get("FILE_CHECK_PROJECT_ROOT", str(BACKEND_DIR.parent))
).expanduser().resolve()


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser().resolve()


HOUSE_DIR = _env_path("FILE_CHECK_HOUSE_DIR", Path.home() / "Documents" / "txt_house")
TEMP_DIR = _env_path("FILE_CHECK_TEMP_DIR", Path.home() / "Documents" / "txt_temp")
STATE_DIR = _env_path("FILE_CHECK_STATE_DIR", PROJECT_ROOT / ".dedup_state")
STATE_DB = STATE_DIR / "dedup_decisions.sqlite3"
FILE_LIST = PROJECT_ROOT / "file_list.json"
FILE_INDEX = PROJECT_ROOT / "file_index.json"
EXTENSION_DIR = PROJECT_ROOT / "extension"
EXTENSION_INDEX = EXTENSION_DIR / "file_index.json"
