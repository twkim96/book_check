#!/usr/bin/env python3
"""Compatibility entry point for the deduplicator action."""

from pathlib import Path
import importlib.util
import runpy
import sys


BACKEND = Path(__file__).resolve().parent / "backend"
sys.path.insert(0, str(BACKEND))
IMPLEMENTATION = BACKEND / "deduplicator.py"

if __name__ == "__main__":
    runpy.run_path(str(IMPLEMENTATION), run_name="__main__")
else:
    spec = importlib.util.spec_from_file_location(__name__, IMPLEMENTATION)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load backend entry point: {IMPLEMENTATION}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[__name__] = module
    spec.loader.exec_module(module)
