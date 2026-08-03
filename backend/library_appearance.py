"""Persistent appearance settings for the independent library server."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, TypedDict


class AppearanceSettings(TypedDict):
    backgroundColor: str
    textColor: str
    accentColor: str


class AppearancePreset(TypedDict):
    id: str
    name: str
    settings: AppearanceSettings


DEFAULT_APPEARANCE_SETTINGS: AppearanceSettings = {
    "backgroundColor": "#0a0c10",
    "textColor": "#edf1f7",
    "accentColor": "#3976da",
}

HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$", re.IGNORECASE)
PRESET_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_CUSTOM_PRESETS = 24
MAX_PRESET_NAME_LENGTH = 40
BUILTIN_PRESET_NAMES = {
    "기본 블루",
    "딥 퍼플",
    "포레스트",
    "슬레이트",
    "웜 브라운",
}
_PRESET_STORE_LOCK = threading.Lock()


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


def read_appearance_presets(
    path: str | os.PathLike[str],
) -> tuple[list[AppearancePreset], bool]:
    store_path = Path(path)
    if not store_path.is_file():
        return [], False
    try:
        payload = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], False
    raw_presets = payload.get("presets") if isinstance(payload, dict) else None
    if not isinstance(raw_presets, list):
        return [], False
    presets: list[AppearancePreset] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for raw in raw_presets:
        preset = _normalize_stored_preset(raw)
        if preset is None:
            continue
        folded_name = preset["name"].casefold()
        if preset["id"] in seen_ids or folded_name in seen_names:
            continue
        seen_ids.add(preset["id"])
        seen_names.add(folded_name)
        presets.append(preset)
        if len(presets) >= MAX_CUSTOM_PRESETS:
            break
    return presets, True


def create_appearance_preset(
    path: str | os.PathLike[str], payload: Any
) -> tuple[AppearancePreset, list[AppearancePreset]]:
    if not isinstance(payload, dict):
        raise ValueError("preset 객체가 필요합니다")
    name = _normalize_preset_name(payload.get("name"))
    if not isinstance(payload.get("settings"), dict):
        raise ValueError("preset.settings 객체가 필요합니다")
    with _PRESET_STORE_LOCK:
        presets, _ = read_appearance_presets(path)
        folded_name = name.casefold()
        reserved_names = {item.casefold() for item in BUILTIN_PRESET_NAMES}
        if folded_name in reserved_names or any(
            item["name"].casefold() == folded_name for item in presets
        ):
            raise ValueError("같은 이름의 프리셋이 이미 있습니다")
        if len(presets) >= MAX_CUSTOM_PRESETS:
            raise ValueError(
                f"사용자 프리셋은 최대 {MAX_CUSTOM_PRESETS}개까지 저장할 수 있습니다"
            )
        preset: AppearancePreset = {
            "id": uuid.uuid4().hex,
            "name": name,
            "settings": normalize_appearance(payload["settings"]),
        }
        updated = [*presets, preset]
        _write_appearance_presets(path, updated)
    return preset, updated


def delete_appearance_preset(
    path: str | os.PathLike[str], preset_id: str
) -> list[AppearancePreset]:
    if not PRESET_ID_RE.fullmatch(preset_id):
        raise ValueError("유효하지 않은 preset id입니다")
    with _PRESET_STORE_LOCK:
        presets, _ = read_appearance_presets(path)
        updated = [item for item in presets if item["id"] != preset_id]
        if len(updated) == len(presets):
            raise KeyError("사용자 프리셋을 찾을 수 없습니다")
        store_path = Path(path)
        if updated:
            _write_appearance_presets(store_path, updated)
        else:
            try:
                store_path.unlink()
            except FileNotFoundError:
                pass
    return updated


def _normalize_hex(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    return value.lower() if HEX_COLOR_RE.fullmatch(value) else fallback


def _normalize_preset_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("프리셋 이름이 필요합니다")
    name = " ".join(value.split())
    if not name:
        raise ValueError("프리셋 이름이 필요합니다")
    if len(name) > MAX_PRESET_NAME_LENGTH:
        raise ValueError(
            f"프리셋 이름은 {MAX_PRESET_NAME_LENGTH}자 이하여야 합니다"
        )
    if any(ord(character) < 32 for character in name):
        raise ValueError("프리셋 이름에 제어 문자를 사용할 수 없습니다")
    return name


def _normalize_stored_preset(payload: Any) -> AppearancePreset | None:
    if not isinstance(payload, dict):
        return None
    preset_id = payload.get("id")
    settings = payload.get("settings")
    if not isinstance(preset_id, str) or not PRESET_ID_RE.fullmatch(preset_id):
        return None
    if not isinstance(settings, dict):
        return None
    try:
        name = _normalize_preset_name(payload.get("name"))
    except ValueError:
        return None
    return {
        "id": preset_id,
        "name": name,
        "settings": normalize_appearance(settings),
    }


def _write_appearance_presets(
    path: str | os.PathLike[str], presets: list[AppearancePreset]
) -> None:
    store_path = Path(path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = store_path.with_suffix(store_path.suffix + ".tmp")
    payload = {"version": 1, "presets": presets}
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, store_path)
    try:
        os.chmod(store_path, 0o600)
    except OSError:
        pass
