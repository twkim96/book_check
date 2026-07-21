"""Reusable review-provider contract for the local library server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Protocol, Sequence

from title_review import (
    apply_title_plan,
    build_title_plan,
    get_title_case,
    list_title_cases,
    preview_title_change,
)
from volume_review import (
    apply_volume_plan,
    get_volume_case,
    list_volume_cases,
    preview_volume_group,
)


def _refresh_review_index(*, state_db: Path, house_dir: Path, index_path: Path) -> dict:
    """Refresh review mutation surfaces from DB, with a safe full-scan fallback."""
    from folderling import sync_house_index
    from scanner import (
        IndexSnapshotStale,
        generate_file_list,
        generate_file_list_from_state_db,
    )

    file_list_path = index_path.with_name("file_list.json")
    mode = "state_db_projection"
    fallback_reason = None
    try:
        result = generate_file_list_from_state_db(
            str(house_dir),
            str(file_list_path),
            str(index_path),
            str(state_db),
        )
        updated = bool(result["ok"])
    except IndexSnapshotStale as exc:
        mode = "full_scan_fallback"
        fallback_reason = str(exc)
        updated = bool(
            generate_file_list(
                [str(house_dir)],
                str(file_list_path),
                str(index_path),
                state_db_path=str(state_db),
            )
        )
    return {
        "index_updated": updated,
        "index_mode": mode,
        "index_fallback_reason": fallback_reason,
        "house_index_synced": bool(
            updated and sync_house_index(str(index_path), str(house_dir))
        ),
    }


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    label: str
    enabled: bool
    planned_version: Optional[str] = None

    def as_dict(self) -> dict:
        result = {
            "id": self.provider_id,
            "label": self.label,
            "enabled": self.enabled,
        }
        if self.planned_version:
            result["planned_version"] = self.planned_version
        return result


class ReviewProvider(Protocol):
    descriptor: ProviderDescriptor
    job_type: str

    def list_cases(self, **filters) -> dict: ...

    def get_case(self, case_id: str) -> dict: ...

    def preview(self, payload: Mapping[str, object]) -> dict: ...

    def build_plan(self, changes: Sequence[Mapping[str, object]]) -> dict: ...

    def apply_plan(
        self,
        changes: Sequence[Mapping[str, object]],
        *,
        confirm_count: int,
        confirm_plan_sha256: str,
        progress=None,
    ) -> dict: ...


class ReviewProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ReviewProvider] = {}
        self._planned: list[ProviderDescriptor] = []

    def register(self, provider: ReviewProvider) -> None:
        provider_id = provider.descriptor.provider_id
        if provider_id in self._providers:
            raise ValueError(f"review provider already registered: {provider_id}")
        self._providers[provider_id] = provider

    def register_planned(self, descriptor: ProviderDescriptor) -> None:
        if descriptor.enabled:
            raise ValueError("planned provider must be disabled")
        self._planned.append(descriptor)

    def get(self, provider_id: str) -> ReviewProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(provider_id) from exc

    def descriptors(self) -> list[dict]:
        enabled = [provider.descriptor.as_dict() for provider in self._providers.values()]
        return enabled + [descriptor.as_dict() for descriptor in self._planned]


class TitleCorrectionProvider:
    descriptor = ProviderDescriptor("title_correction", "제목 교정", True)
    job_type = "title_requeue"

    def __init__(
        self,
        *,
        state_db: Path,
        house_dir: Path,
        temp_dir: Path,
        index_path: Path,
    ) -> None:
        self.state_db = Path(state_db)
        self.house_dir = Path(house_dir)
        self.temp_dir = Path(temp_dir)
        self.index_path = Path(index_path)

    def list_cases(self, **filters) -> dict:
        return list_title_cases(self.state_db, **filters)

    def get_case(self, case_id: str) -> dict:
        return get_title_case(self.state_db, case_id)

    def preview(self, payload: Mapping[str, object]) -> dict:
        return preview_title_change(
            self.state_db,
            house_dir=self.house_dir,
            temp_dir=self.temp_dir,
            file_id=str(payload.get("file_id") or ""),
            new_body=payload.get("new_body"),
            source_revision=str(payload.get("source_revision") or ""),
        )

    def build_plan(self, changes: Sequence[Mapping[str, object]]) -> dict:
        return build_title_plan(
            self.state_db,
            house_dir=self.house_dir,
            temp_dir=self.temp_dir,
            changes=changes,
        )

    def apply_plan(
        self,
        changes: Sequence[Mapping[str, object]],
        *,
        confirm_count: int,
        confirm_plan_sha256: str,
        progress=None,
    ) -> dict:
        result = apply_title_plan(
            self.state_db,
            house_dir=self.house_dir,
            temp_dir=self.temp_dir,
            changes=changes,
            confirm_count=confirm_count,
            confirm_plan_sha256=confirm_plan_sha256,
            progress=progress,
        )
        try:
            result.update(
                _refresh_review_index(
                    state_db=self.state_db,
                    house_dir=self.house_dir,
                    index_path=self.index_path,
                )
            )
            if not result["index_updated"]:
                result["warning"] = "제목 교정은 완료됐지만 index 갱신에 실패했습니다"
        except Exception as exc:
            result.update({
                "index_updated": False,
                "index_mode": "failed",
                "house_index_synced": False,
                "warning": f"제목 교정은 완료됐지만 index 갱신 중 오류가 발생했습니다: {exc}",
            })
        return result


class VolumeGroupProvider:
    descriptor = ProviderDescriptor("volume_group", "분권 묶기", True)
    job_type = "volume_group_merge"

    def __init__(
        self, *, state_db: Path, house_dir: Path, temp_dir: Path, index_path: Path
    ) -> None:
        self.state_db = Path(state_db)
        self.house_dir = Path(house_dir)
        self.temp_dir = Path(temp_dir)
        self.index_path = Path(index_path)

    def list_cases(self, **filters) -> dict:
        return list_volume_cases(self.state_db, house_dir=self.house_dir, **filters)

    def get_case(self, case_id: str) -> dict:
        return get_volume_case(self.state_db, house_dir=self.house_dir, case_id=case_id)

    def preview(self, payload: Mapping[str, object]) -> dict:
        selected = payload.get("selected_file_ids")
        if selected is not None and not isinstance(selected, list):
            raise ValueError("selected_file_ids 배열이 필요합니다")
        return preview_volume_group(
            self.state_db,
            house_dir=self.house_dir,
            case_id=str(payload.get("case_id") or ""),
            source_revision=str(payload.get("source_revision") or ""),
            selected_file_ids=selected,
            target_folder_name=(
                str(payload["target_folder_name"])
                if payload.get("target_folder_name") is not None
                else None
            ),
            allow_duplicate_coordinates=(
                payload.get("allow_duplicate_coordinates") is True
            ),
        )

    def apply_plan(
        self,
        payload: Mapping[str, object],
        *,
        confirm_count: int,
        confirm_plan_sha256: str,
        progress=None,
    ) -> dict:
        selected = payload.get("selected_file_ids")
        if selected is not None and not isinstance(selected, list):
            raise ValueError("selected_file_ids 배열이 필요합니다")
        result = apply_volume_plan(
            self.state_db,
            house_dir=self.house_dir,
            temp_dir=self.temp_dir,
            case_id=str(payload.get("case_id") or ""),
            source_revision=str(payload.get("source_revision") or ""),
            selected_file_ids=selected,
            target_folder_name=(
                str(payload["target_folder_name"])
                if payload.get("target_folder_name") is not None
                else None
            ),
            allow_duplicate_coordinates=(
                payload.get("allow_duplicate_coordinates") is True
            ),
            confirm_count=confirm_count,
            confirm_plan_sha256=confirm_plan_sha256,
            progress=progress,
        )
        try:
            result.update(
                _refresh_review_index(
                    state_db=self.state_db,
                    house_dir=self.house_dir,
                    index_path=self.index_path,
                )
            )
            if not result["index_updated"]:
                result["warning"] = "파일 병합은 완료됐지만 index 갱신에 실패했습니다"
        except Exception as exc:
            result["index_updated"] = False
            result["index_mode"] = "failed"
            result["house_index_synced"] = False
            result["warning"] = (
                "파일 병합은 완료됐지만 index 갱신 중 오류가 발생했습니다: "
                f"{exc}"
            )
        return result
