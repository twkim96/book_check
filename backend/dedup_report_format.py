"""Human-readable rendering for structured Folderling dedup reports."""

from __future__ import annotations

from io import StringIO
from typing import Any, Mapping


def _format_entry(entry: Mapping[str, Any] | None) -> str:
    entry = entry or {}
    author = entry.get("author") or "-"
    source = entry.get("source", "house")
    size = entry.get("size") or 0
    try:
        size_kb = float(size) / 1024
    except (TypeError, ValueError):
        size_kb = 0.0
    return (
        f"[{source}] {entry.get('name', '-')} | {entry.get('rel_path', '-')} | "
        f"{size_kb:.1f} KB | 편수 {entry.get('max_number', '-')} | "
        f"완결 {entry.get('complete', '-')} | 작가 {author}"
    )


def dedup_report_summary_line(payload: Mapping[str, Any]) -> str:
    """Return the compact human summary without rendering report entries."""
    if payload.get("kind") != "folderling_dedup":
        raise ValueError("Folderling dedup 구조화 보고서가 아닙니다")
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        summary = {}
    exact_records = payload.get("exact_records") or []
    suspect_groups = payload.get("suspect_groups") or []
    suspect_move_records = payload.get("suspect_move_records") or []
    exact_action = "quarantine" if summary.get("managed_mode") else (
        "삭제" if summary.get("delete_exact") else "격리"
    )
    exact_count = summary.get("exact_count", len(exact_records))
    suspect_group_count = summary.get("suspect_group_count", len(suspect_groups))
    suspect_move_count = summary.get("suspect_move_count", len(suspect_move_records))
    return (
        f"모드: {'미리보기/Dry-run' if summary.get('dry_run') else '실제 실행'} | "
        f"스캔 범위: {'house + temp' if summary.get('include_temp') else 'house만'} | "
        f"정확 중복 {exact_action} {summary.get('exact_mutation_count', exact_count)}개 | "
        f"legacy report-only {summary.get('legacy_report_only_count', 0)}개 | "
        f"managed report-only {summary.get('managed_report_only_count', 0)}개 | "
        f"검토 큐 그룹 {suspect_group_count}개 | "
        f"검토 큐 이동 {summary.get('review_queue_move_count', suspect_move_count)}개 "
        f"(같은 작가/미상 {summary.get('same_author_count', 0)}, "
        f"작가 충돌 {summary.get('author_conflict_count', 0)}) | "
        f"애매 보류(warning) {summary.get('warning_count', 0)}개 | "
        f"본문 증거 없음(metadata_only) {summary.get('metadata_only_count', 0)}개"
    )


def render_dedup_report_text(payload: Mapping[str, Any]) -> str:
    """Render a schema-versioned dedup JSON payload as the legacy TXT view."""
    if payload.get("kind") != "folderling_dedup":
        raise ValueError("Folderling dedup 구조화 보고서가 아닙니다")

    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        summary = {}
    exact_records = payload.get("exact_records") or []
    suspect_groups = payload.get("suspect_groups") or []
    suspect_move_records = payload.get("suspect_move_records") or []
    disambig_records = payload.get("disambig_records") or []
    blocked_strong_relations = payload.get("blocked_strong_relations") or []

    output = StringIO()
    output.write("[중복/검토 큐 정리 로그]\n")
    output.write("완전 중복은 raw SHA 재검증 뒤 quarantine으로 논리 삭제하며 즉시 unlink하지 않습니다.\n")
    output.write("검토 큐는 핵심 제목이 같아 사람 검토가 필요한 항목입니다. 자동 삭제 대상이 아닙니다.\n")
    output.write("- 별개 작품/판본 판정은 dedup_decisions.py에 기록하세요. pass/는 판정 입력이 아닙니다.\n")
    output.write("- 잘못 이동된 경우 restore_suspects.py --dry-run 으로 먼저 확인 후 --run 으로 복원하세요.\n\n")
    output.write("======================================================================\n")
    output.write(dedup_report_summary_line(payload) + "\n")
    output.write("======================================================================\n\n")

    if summary.get("unsafe_legacy_bridge"):
        output.write("[안전 차단] unsafe_legacy_bridge=true\n")
        output.write("- 이 auditor 연결 결과는 1.2.1 Phase D 전까지 dry-run 참고용이며 actual mutation에 사용할 수 없습니다.\n\n")

    output.write("[1.5단계] 본문 유사도 기반 분리 마커 부여 (별개 작품 〔Dn〕)\n")
    if not disambig_records:
        output.write("- 분리 마커 부여 없음\n")
    else:
        for record in disambig_records:
            if record.get("status") == "skipped_collision":
                tag = "[건너뜀: 파일명 충돌]"
            elif record.get("status") == "house_conflict_logged":
                tag = "[house 제자리: 분리 마커 미부여]"
            elif record.get("dry_run"):
                tag = "[부여 예정]"
            else:
                tag = "[부여]"
            new_disambig = record.get("new_disambig")
            output.write(
                f"  {tag} core_title: {record.get('core_title', '-')} | "
                f"D{record.get('old_disambig', '-')} → "
                f"{('D' + str(new_disambig)) if new_disambig else '-'} | "
                f"{record.get('old_name', '-')} → {record.get('new_name', '-')}\n"
            )
    output.write("\n")

    output.write("[2단계] 제목 기반 검토 큐 (자동 삭제 아님, 사람 검토 필요)\n")
    if not suspect_groups:
        output.write("- 검토 큐 그룹 없음\n")
    else:
        paths_by_status: dict[str, set[str]] = {}
        for record in suspect_move_records:
            entry = record.get("entry") or {}
            path = entry.get("path")
            if path:
                paths_by_status.setdefault(str(record.get("status") or ""), set()).add(path)
        for group in suspect_groups:
            if group.get("origin") == "auditor_aux":
                reason = "auditor " + ", ".join(group.get("audit_classifications", []))
            elif group.get("distinct_authors"):
                reason = "핵심 제목 동일·작가 후보 다름 → byte-exact가 아니면 author_conflicts 수동 판별"
            else:
                reason = "핵심 제목 동일 → 본문 same은 suspected_duplicates, 애매는 warning 보류"
            output.write(f"- core_title: {group.get('core_title', '-')} ({reason})\n")
            keep = group.get("keep") or {}
            output.write(f"  [유지 후보] {_format_entry(keep)}\n")
            for entry in group.get("entries") or []:
                if entry.get("path") == keep.get("path"):
                    continue
                path = entry.get("path")
                if path in paths_by_status.get("moved", set()):
                    marker = "[중복 확정 → suspected]"
                elif path in paths_by_status.get("author_review", set()):
                    marker = "[애매·작가충돌 → author_conflicts]"
                elif path in paths_by_status.get("warning", set()):
                    marker = "[애매 → warning 보류]"
                elif path in paths_by_status.get("metadata_only", set()):
                    marker = "[metadata_only → warning 보류]"
                else:
                    marker = "[검토 후보]"
                output.write(f"  {marker} {_format_entry(entry)}\n")
    output.write("\n")

    output.write("[2.5단계] mutation 차단 strong 관계 (report-only)\n")
    if not blocked_strong_relations:
        output.write("- 차단된 strong 관계 없음\n")
    else:
        for relation in blocked_strong_relations:
            output.write(
                f"- [{relation.get('classification', '-')}] reason={relation.get('reason', '-')}\n"
                f"  left : {_format_entry(relation.get('left'))}\n"
                f"  right: {_format_entry(relation.get('right'))}\n"
            )
    output.write("\n")
    return output.getvalue()
