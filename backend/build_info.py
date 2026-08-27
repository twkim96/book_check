"""Small, read-only build provenance surface for health and recovery."""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def resolve_build_info(project_root: str) -> dict:
    root = Path(project_root).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[1]
    git_root = root
    commit = os.environ.get("FILE_CHECK_BUILD_COMMIT", "").strip()
    dirty_override = os.environ.get("FILE_CHECK_BUILD_DIRTY")
    if not commit:
        for candidate in dict.fromkeys((root, source_root)):
            try:
                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=candidate,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=3,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
            git_root = candidate
            break
        else:
            commit = "unknown"
    if dirty_override is not None:
        dirty = dirty_override.strip().casefold() in {"1", "true", "yes", "dirty"}
    else:
        try:
            dirty = bool(
                subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=no"],
                    cwd=git_root,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=3,
                ).stdout.strip()
            )
        except (OSError, subprocess.SubprocessError):
            dirty = None
    return {"build_commit": commit, "build_dirty": dirty}
