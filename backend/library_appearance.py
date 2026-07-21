"""Persistent appearance settings for the independent library server."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, TypedDict


class AppearanceSettings(TypedDict):
    backgroundColor: str
    textColor: str
    accentColor: str


DEFAULT_APPEARANCE_SETTINGS: AppearanceSettings = {
    "backgroundColor": "#0a0c10",
    "textColor": "#edf1f7",
    "accentColor": "#3976da",
}

HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$", re.IGNORECASE)


def normalize_appearance(payload: Any) -> AppearanceSettings:
    if not isinstance(payload, dict):
        return DEFAULT_APPEARANCE_SETTINGS.copy()
    return {
        key: _normalize_hex(payload.get(key), fallback)
        for key, fallback in DEFAULT_APPEARANCE_SETTINGS.items()
    }


def read_appearance(path: str | os.PathLike[str]) -> tuple[AppearanceSettings, bool]:
    store_path = Path(path)
    if not store_path.is_file():
        return DEFAULT_APPEARANCE_SETTINGS.copy(), False
    try:
        payload = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_APPEARANCE_SETTINGS.copy(), False
    return normalize_appearance(payload), True


def write_appearance(
    path: str | os.PathLike[str], payload: Any
) -> AppearanceSettings:
    settings = normalize_appearance(payload)
    store_path = Path(path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = store_path.with_suffix(store_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, store_path)
    try:
        os.chmod(store_path, 0o600)
    except OSError:
        pass
    return settings


def reset_appearance(path: str | os.PathLike[str]) -> AppearanceSettings:
    store_path = Path(path)
    try:
        store_path.unlink()
    except FileNotFoundError:
        pass
    return DEFAULT_APPEARANCE_SETTINGS.copy()


def _normalize_hex(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    return value.lower() if HEX_COLOR_RE.fullmatch(value) else fallback
