#!/usr/bin/env python3
"""Read-only 1.2.10 audit for duplicate same-coordinate house entries."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import duplicate_auditor  # noqa: E402


def main():
    return duplicate_auditor.main([
        "--index", str(ROOT / "file_index.json"),
        "--house", str(duplicate_auditor.HOUSE_DIR),
        "--temp", str(duplicate_auditor.TEMP_DIR),
        "--house-only",
        "--same-coordinate-only",
        "--max-candidate-files", "80",
        "--write-report",
    ])


if __name__ == "__main__":
    raise SystemExit(main())
