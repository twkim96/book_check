"""Journaled filesystem mutations for managed dedup mode."""

import json
import re
import unicodedata
import zipfile
from difflib import SequenceMatcher
from pathlib import Path

import decision_store
from dedup_episode_relation import (
    DEDUP_SPECIAL_COORDINATE_MODES,
    classify_dedup_coordinate_relation,
    classify_loose_title_upgrade_relation,
)
from mutation_io import (
    assert_mutation_lock_held,
    contained_anchor_proof_sufficient,
    evidence_matches,
    ensure_directory_nofollow,
    inspect_contained_text,
    inspect_epub_content,
    inspect_epub_reading_payload,
    inspect_epub_spine_text,
    inspect_ordered_text,
    inspect_regular_file,
    inspect_normalized_text,
    mutation_lock,
)
from text_preview import ordered_body_coverage_sufficient
from normalizer import analyze_name, has_legacy_marker


STRONG_QUEUE_CLASSES = frozenset({"text_equivalent", "epub_equivalent"})
NORMALIZED_EQUAL_CLASSES = frozenset({"text_equivalent", "marker_recheck"})
EPUB_EQUAL_CLASSES = frozenset({"epub_equivalent"})
WEAK_QUEUE_CLASSES = frozenset({
    "near_identical",
    "contained_exact",
    "contained_version",
    "ordered_body_match",
    "ordered_body_review",
})
REPORT_ONLY_CLASSES = frozenset({
    "longer_unresolved", "decode_lossy", "metadata_only", "insufficient_text",
})
HUMAN_REVIEW_CLASSES = (
    NORMALIZED_EQUAL_CLASSES | EPUB_EQUAL_CLASSES | WEAK_QUEUE_CLASSES | REPORT_ONLY_CLASSES
    | frozenset({"exact_bytes"})
)
EPUB_SPINE_TEXT_MIN_CHARS = 50_000
HOUSE_NEAR_DUPLICATE_MIN_COVERAGE_PPM = 990_000
_DISTRIBUTION_SUFFIX_RE = re.compile(
    r"(?:^|[-_\s])(?:현|로)?판\d{6}(?=(?:[^0-9]|$))", re.IGNORECASE
)
_EXPLICIT_EDITION_MARKER_RE = re.compile(
    r"개정|수정판|누락\s*수정|외전|특전|후일담|에필로그|"
    r"19\s*(?:n|금|禁)|성인판|무삭제|번역판",
    re.IGNORECASE,
)


class ContainedUpgradeNotProven(RuntimeError):
    pass


class OrderedBodyMatchNotProven(RuntimeError):
    pass


def _legacy_marker_discard_contract(discard, keep):
    """Independent mutation-boundary check for the marker-only exception."""
    return bool(
        discard["assignment_state"] == "legacy_unresolved"
        and keep["assignment_state"] != "legacy_unresolved"
        and has_legacy_marker(Path(discard["canonical_path"]).name)
        and not has_legacy_marker(Path(keep["canonical_path"]).name)
        and discard["variant_id"] is None
        and not discard["protected"]
        and not discard["representative"]
    )


def _house_near_distribution_contract(
    discard, keep, discard_meta, keep_meta, coordinate_relation
):
    """Independently authorize only a dated-distribution house duplicate."""
    discard_name = Path(discard["canonical_path"]).name
    keep_name = Path(keep["canonical_path"]).name
    discard_core = _normalized_metadata_token(discard_meta["core_title"])
    keep_core = _normalized_metadata_token(keep_meta["core_title"])
    return bool(
        discard["source"] == keep["source"] == "house"
        and discard["assignment_state"] in {"unassigned", "decision_required"}
        and keep["assignment_state"] in {"unassigned", "decision_required"}
        and discard["variant_id"] is None
        and keep["variant_id"] is None
        and not discard["protected"]
        and not keep["protected"]
        and not discard["representative"]
        and not keep["representative"]
        and not has_legacy_marker(discard_name)
        and not has_legacy_marker(keep_name)
        and _DISTRIBUTION_SUFFIX_RE.search(discard_name) is not None
        and _DISTRIBUTION_SUFFIX_RE.search(keep_name) is None
        and _EXPLICIT_EDITION_MARKER_RE.search(discard_name) is None
        and _EXPLICIT_EDITION_MARKER_RE.search(keep_name) is None
        and analyze_name(discard_name)["complete"]
        and analyze_name(keep_name)["complete"]
        and coordinate_relation is not None
        and coordinate_relation.mode == "same_coordinates"
        and coordinate_relation.preferred_side is None
        and discard_core
        and keep_core
        and SequenceMatcher(
            None, discard_core, keep_core, autojunk=False
        ).ratio() >= 0.90
    )


def _review_evidence(review):
    try:
        return json.loads(review["evidence_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _revalidate_epub_equivalent(first, second, first_path, second_path, review):
    """Return current file evidence after replaying the persisted EPUB proof."""

    evidence = _review_evidence(review)
    mode = evidence.get("epub_equivalence_mode")
    if mode == "reading_payload":
        first_epub = inspect_epub_reading_payload(first_path)
        second_epub = inspect_epub_reading_payload(second_path)
        expected = {
            evidence.get("left_reading_payload_sha256"),
            evidence.get("right_reading_payload_sha256"),
        }
        valid = (
            None not in expected
            and len(expected) == 1
            and first_epub.content_sha256 == second_epub.content_sha256
            and first_epub.content_sha256 in expected
        )
    elif mode == "spine_text":
        first_epub = inspect_epub_spine_text(first_path)
        second_epub = inspect_epub_spine_text(second_path)
        expected_hashes = {
            evidence.get("left_spine_text_sha256"),
            evidence.get("right_spine_text_sha256"),
        }
        expected_chars = {
            evidence.get("left_spine_text_chars"),
            evidence.get("right_spine_text_chars"),
        }
        valid = (
            None not in expected_hashes
            and len(expected_hashes) == 1
            and None not in expected_chars
            and len(expected_chars) == 1
            and first_epub.text_sha256 == second_epub.text_sha256
            and first_epub.text_sha256 in expected_hashes
            and first_epub.text_chars == second_epub.text_chars
            and first_epub.text_chars in expected_chars
            and first_epub.text_chars >= EPUB_SPINE_TEXT_MIN_CHARS
            and bool(set(first_epub.identifiers) & set(second_epub.identifiers))
        )
    else:
        first_epub = inspect_epub_content(first_path)
        second_epub = inspect_epub_content(second_path)
        valid = (
            first["normalized_sha256"]
            and first["normalized_sha256"] == second["normalized_sha256"]
            and first_epub.content_sha256 == second_epub.content_sha256
            and first_epub.content_sha256 == first["normalized_sha256"]
            and second_epub.content_sha256 == second["normalized_sha256"]
        )
    if not valid:
        raise RuntimeError("EPUB equivalence revalidation failed")
    return first_epub.file_evidence, second_epub.file_evidence


def _copy_record_consume(conn, operation_id, source, destination, evidence, *, guard=None):
    return decision_store.copy_record_consume_operation(
        conn, operation_id, source, destination, evidence, guard=guard
    )


def _ensure_intake_fingerprint(conn, source):
    """Create a raw-only immutable snapshot for a unique intake file."""
    if source["current_fingerprint_id"] is not None:
        return source
    path = Path(source["canonical_path"])
    evidence = inspect_regular_file(path)
    _assert_row_identity(source, evidence, path)
    from normalizer import NORMALIZER_VERSION

    with decision_store.transaction(conn):
        existing = conn.execute(
            """
            SELECT fingerprint_id FROM fingerprints
            WHERE file_id = ? AND canonical_path = ? AND size = ? AND mtime_ns = ?
              AND dev = ? AND ino = ? AND ctime_ns = ? AND raw_sha256 = ?
            ORDER BY fingerprint_id DESC LIMIT 1
            """,
            (
                source["file_id"], source["canonical_path"], source["size"],
                source["mtime_ns"], evidence.dev, evidence.ino,
                evidence.ctime_ns, evidence.sha256,
            ),
        ).fetchone()
        if existing is not None:
            fingerprint_id = existing["fingerprint_id"]
        else:
            fingerprint_id = conn.execute(
                """
                INSERT INTO fingerprints(
                    file_id, canonical_path, size, mtime_ns, normalizer_version,
                    fingerprint_version, dev, ino, ctime_ns, raw_sha256, status
                ) VALUES (?, ?, ?, ?, ?, 'intake-raw-v1', ?, ?, ?, ?, 'raw_only')
                """,
                (
                    source["file_id"], source["canonical_path"], source["size"],
                    source["mtime_ns"], NORMALIZER_VERSION,
                    evidence.dev, evidence.ino, evidence.ctime_ns, evidence.sha256,
                ),
            ).lastrowid
        conn.execute(
            "UPDATE files SET current_fingerprint_id = ? WHERE file_id = ?",
            (fingerprint_id, source["file_id"]),
        )
    return _file_state(conn, source["file_id"])


def refresh_user_approved_snapshot(conn, file_id):
    """Rebaseline one externally touched file after explicit user approval.

    The old fingerprint remains immutable provenance.  A new raw fingerprint is
    attached to the stable file id and all mutation guards use the new identity.
    """
    row = _file_state(conn, file_id)
    path = Path(row["canonical_path"])
    with decision_store.transaction(conn):
        decision_store.reconcile_file_metadata(
            conn,
            path,
            source=row["source"],
            legacy_marker=row["assignment_state"] == "legacy_unresolved",
        )
    return _ensure_intake_fingerprint(conn, _file_state(conn, file_id))


def user_queue_restore(conn, *, file_id, run_id):
    """Restore a currently approved queue snapshot to its original house path."""
    with mutation_lock(conn, f"user_queue_restore:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        source = _file_state(conn, file_id)
        if source["source"] != "queue":
            raise RuntimeError("user restore source must be an active queue file")
        original = conn.execute(
            """
            SELECT * FROM operations
            WHERE file_id = ? AND action = 'house_review_move' AND state = 'committed'
            ORDER BY operation_id DESC LIMIT 1
            """,
            (file_id,),
        ).fetchone()
        if original is None:
            raise RuntimeError("user restore origin not found")
        source_path = _preflight(source)
        destination = Path(original["source_path"])
        decision_store.assert_actual_run_path(actual_run, source_path, "temp_root")
        decision_store.assert_actual_run_path(actual_run, destination, "house_root")
        if destination.exists():
            raise RuntimeError(f"user restore destination already exists: {destination}")
        source_evidence = inspect_regular_file(source_path)
        decision_store.assert_manifest_or_same_run_queue_source(
            conn,
            actual_run,
            source_path,
            source_evidence,
            file_id=file_id,
        )
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="user_queue_restore",
                source_path=str(source_path),
                dest_path=str(destination),
                file_id=file_id,
                expected_size=source["size"],
                expected_mtime_ns=source["mtime_ns"],
                expected_fingerprint_id=source["current_fingerprint_id"],
                source_dev=source_evidence.dev,
                source_ino=source_evidence.ino,
                source_ctime_ns=source_evidence.ctime_ns,
                source_sha256=source_evidence.sha256,
            )

        def guard():
            decision_store.assert_active_actual_run(conn, run_id)
            current = _file_state(conn, file_id)
            if current["current_fingerprint_id"] != source["current_fingerprint_id"]:
                raise RuntimeError("user restore fingerprint changed")

        destination_evidence = _copy_record_consume(
            conn,
            operation_id,
            source_path,
            destination,
            source_evidence,
            guard=guard,
        )
        moved_stat = destination.lstat()
        with decision_store.transaction(conn):
            conn.execute(
                """
                UPDATE files SET canonical_path = ?, source = 'house',
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?
                WHERE file_id = ?
                """,
                (
                    str(destination), destination_evidence.dev, destination_evidence.ino,
                    destination_evidence.ctime_ns, destination_evidence.size,
                    destination_evidence.mtime_ns, file_id,
                ),
            )
            decision_store.upsert_file_analysis(
                conn, file_id, destination, stat_result=moved_stat
            )
            conn.execute(
                """
                UPDATE review_items SET queue_path = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE (candidate_file_id = ? OR reference_file_id = ?)
                  AND queue_path = ?
                """,
                (file_id, file_id, str(source_path)),
            )
            decision_store.transition_operation(conn, operation_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, operation_id, "committed")
        return {
            "operation_id": operation_id,
            "action": "user_queue_restore",
            "file_id": file_id,
            "source_path": str(source_path),
            "dest_path": str(destination),
        }


def user_queue_accept_to_house(
    conn, *, file_id, destination, run_id, operation_group_id=None
):
    """Accept a human-reviewed queue file into house in one journaled move.

    Unlike ``user_queue_restore``, this also supports files that originally came
    from temp.  The caller chooses the final house path after applying the same
    filename/folder rules as normal Folderling intake.
    """
    with mutation_lock(conn, f"user_queue_accept:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        source = _ensure_intake_fingerprint(conn, _file_state(conn, file_id))
        if source["source"] != "queue":
            raise RuntimeError("user accept source must be an active queue file")
        if source["protected"] or source["representative"]:
            raise RuntimeError("protected/representative queue file cannot be accepted")

        source_path = _preflight(source)
        destination = Path(decision_store.canonicalize_path(destination))
        decision_store.assert_actual_run_path(actual_run, source_path, "temp_root")
        decision_store.assert_actual_run_path(actual_run, destination, "house_root")
        if destination.exists():
            raise RuntimeError(f"user accept destination already exists: {destination}")

        source_evidence = inspect_regular_file(source_path)
        decision_store.assert_manifest_or_same_run_queue_source(
            conn,
            actual_run,
            source_path,
            source_evidence,
            file_id=file_id,
        )
        coordinates = decision_store.coordinate_fields_from_name(destination.name)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="user_queue_accept",
                source_path=str(source_path),
                dest_path=str(destination),
                file_id=file_id,
                expected_size=source["size"],
                expected_mtime_ns=source["mtime_ns"],
                expected_fingerprint_id=source["current_fingerprint_id"],
                operation_group_id=operation_group_id,
                source_dev=source_evidence.dev,
                source_ino=source_evidence.ino,
                source_ctime_ns=source_evidence.ctime_ns,
                source_sha256=source_evidence.sha256,
            )

        def guard():
            decision_store.assert_active_actual_run(conn, run_id)
            current = _file_state(conn, file_id)
            if current["current_fingerprint_id"] != source["current_fingerprint_id"]:
                raise RuntimeError("user accept fingerprint changed")

        destination_evidence = _copy_record_consume(
            conn,
            operation_id,
            source_path,
            destination,
            source_evidence,
            guard=guard,
        )
        moved_stat = destination.lstat()
        with decision_store.transaction(conn):
            conn.execute(
                """
                UPDATE files SET canonical_path = ?, source = 'house',
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?,
                    coordinate_kind = ?, part_num = ?, part_den = ?,
                    volume_num = ?, volume_den = ?, coordinate_symbol = ?,
                    coordinate_sort_key = ?, episode_start = ?, episode_end = ?,
                    coordinate_raw = ?, span_ambiguous = ?
                WHERE file_id = ?
                """,
                (
                    str(destination), destination_evidence.dev,
                    destination_evidence.ino, destination_evidence.ctime_ns,
                    destination_evidence.size, destination_evidence.mtime_ns,
                    coordinates["coordinate_kind"], coordinates["part_num"],
                    coordinates["part_den"], coordinates["volume_num"],
                    coordinates["volume_den"], coordinates["coordinate_symbol"],
                    coordinates["coordinate_sort_key"], coordinates["episode_start"],
                    coordinates["episode_end"], coordinates["coordinate_raw"],
                    coordinates["span_ambiguous"], file_id,
                ),
            )
            decision_store.upsert_file_analysis(
                conn, file_id, destination, stat_result=moved_stat
            )
            conn.execute(
                """
                UPDATE review_items SET queue_path = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE (candidate_file_id = ? OR reference_file_id = ?)
                  AND queue_path = ?
                """,
                (file_id, file_id, str(source_path)),
            )
            decision_store.transition_operation(conn, operation_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, operation_id, "committed")
        return {
            "operation_id": operation_id,
            "action": "user_queue_accept",
            "file_id": file_id,
            "source_path": str(source_path),
            "dest_path": str(destination),
        }


def house_review_move(
    conn, *, review_id, move_file_id, keep_file_id, classification, queue_dir, run_id
):
    """Move a house endpoint while preserving its directly related keep endpoint.

    The keep may already be in house or may be a more complete incoming temp file
    that Folderling will ingest after this review move commits.
    """
    with mutation_lock(conn, f"house_review_move:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        move = _file_state(conn, move_file_id)
        keep = _file_state(conn, keep_file_id)
        if move["source"] != "house" or keep["source"] not in {"house", "temp"}:
            raise RuntimeError(
                "house review move requires a house source and a house/temp keep"
            )
        if move["protected"]:
            raise RuntimeError("protected house file cannot enter cleanup queue")
        review = conn.execute(
            "SELECT * FROM review_items WHERE review_id = ?", (review_id,)
        ).fetchone()
        if (
            review is None
            or review["classification"] != classification
            or classification not in HUMAN_REVIEW_CLASSES
        ):
            raise RuntimeError("house cleanup requires a queueable persisted review")
        if review["state"] not in {"pending", "deferred"}:
            raise RuntimeError("house cleanup review is already closed")
        pair_ids = {review["candidate_file_id"], review["reference_file_id"]}
        if pair_ids != {move_file_id, keep_file_id}:
            raise RuntimeError("house cleanup review pair mismatch")
        expected_fingerprints = {
            review["candidate_file_id"]: review["left_fingerprint_id"],
            review["reference_file_id"]: review["right_fingerprint_id"],
        }
        if (
            move["current_fingerprint_id"] != expected_fingerprints[move_file_id]
            or keep["current_fingerprint_id"] != expected_fingerprints[keep_file_id]
        ):
            raise RuntimeError("house cleanup fingerprint changed")

        move_path = Path(move["canonical_path"])
        keep_path = Path(keep["canonical_path"])
        if classification in NORMALIZED_EQUAL_CLASSES:
            move_evidence, move_normalized = inspect_normalized_text(move_path)
            keep_evidence, keep_normalized = inspect_normalized_text(keep_path)
            if (
                not move["normalized_sha256"]
                or move["normalized_sha256"] != keep["normalized_sha256"]
                or move_normalized != keep_normalized
                or move_normalized != move["normalized_sha256"]
            ):
                raise RuntimeError("house cleanup normalized SHA revalidation failed")
        elif classification in EPUB_EQUAL_CLASSES:
            move_evidence, keep_evidence = _revalidate_epub_equivalent(
                move, keep, move_path, keep_path, review
            )
        else:
            move_evidence = inspect_regular_file(move_path)
            keep_evidence = inspect_regular_file(keep_path)
        decision_store.assert_manifest_source(
            actual_run, move_path, "house_root", move_evidence
        )
        decision_store.assert_manifest_source(
            actual_run,
            keep_path,
            "house_root" if keep["source"] == "house" else "temp_root",
            keep_evidence,
        )
        destination = _unique_destination(conn, queue_dir, move_path.name)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn, run_id=run_id, action="house_review_move",
                source_path=str(move_path), dest_path=str(destination),
                file_id=move_file_id, keep_file_id=keep_file_id,
                expected_size=move["size"], expected_mtime_ns=move["mtime_ns"],
                expected_fingerprint_id=move["current_fingerprint_id"],
                expected_keep_fingerprint_id=keep["current_fingerprint_id"],
                source_dev=move_evidence.dev, source_ino=move_evidence.ino,
                source_ctime_ns=move_evidence.ctime_ns,
                source_sha256=move_evidence.sha256,
            )

        def guard():
            decision_store.assert_active_actual_run(conn, run_id)
            current_move = _file_state(conn, move_file_id)
            current_keep = _file_state(conn, keep_file_id)
            if current_move["current_fingerprint_id"] != move["current_fingerprint_id"]:
                raise RuntimeError("house cleanup source changed before consume")
            if current_keep["current_fingerprint_id"] != keep["current_fingerprint_id"]:
                raise RuntimeError("house cleanup keep changed before consume")
            if not evidence_matches(inspect_regular_file(keep_path), keep_evidence):
                raise RuntimeError("house cleanup keep identity changed")

        destination_evidence = _copy_record_consume(
            conn, operation_id, move_path, destination, move_evidence, guard=guard
        )
        with decision_store.transaction(conn):
            conn.execute(
                """UPDATE files SET canonical_path = ?, source = 'queue',
                    assignment_state = 'decision_required', assignment_origin = NULL,
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?
                    WHERE file_id = ?""",
                (str(destination), destination_evidence.dev, destination_evidence.ino,
                 destination_evidence.ctime_ns, destination_evidence.size,
                 destination_evidence.mtime_ns, move_file_id),
            )
            conn.execute(
                "UPDATE review_items SET queue_path = ?, updated_at = CURRENT_TIMESTAMP WHERE review_id = ?",
                (str(destination), review_id),
            )
            decision_store.transition_operation(conn, operation_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, operation_id, "committed")
        return {"operation_id": operation_id, "destination": str(destination)}


def apply_strong_equivalent_quarantine(
    conn,
    *,
    review_id,
    discard_file_id,
    keep_file_id,
    classification,
    quarantine_dir,
    run_id,
):
    """Finalize a revalidated TXT/EPUB equivalent as recoverable quarantine."""
    if classification not in STRONG_QUEUE_CLASSES:
        raise ValueError("strong-equivalent quarantine requires a strong class")
    discard = _file_state(conn, discard_file_id)
    keep = _file_state(conn, keep_file_id)
    if discard_file_id == keep_file_id:
        raise ValueError("strong-equivalent endpoints must differ")
    if discard["source"] not in {"house", "temp", "queue"}:
        raise RuntimeError("strong-equivalent discard source is unsupported")
    if keep["source"] != "house":
        raise RuntimeError("strong-equivalent keep must be an active house file")
    if discard["protected"] or discard["representative"]:
        raise RuntimeError("strong-equivalent discard is protected")

    review = conn.execute(
        "SELECT * FROM review_items WHERE review_id = ?", (review_id,)
    ).fetchone()
    if (
        review is None
        or review["classification"] != classification
        or review["state"] not in {"pending", "deferred"}
        or {review["candidate_file_id"], review["reference_file_id"]}
        != {discard_file_id, keep_file_id}
    ):
        raise RuntimeError("strong-equivalent review is missing or stale")
    expected_fingerprints = {
        review["candidate_file_id"]: review["left_fingerprint_id"],
        review["reference_file_id"]: review["right_fingerprint_id"],
    }
    if (
        discard["current_fingerprint_id"]
        != expected_fingerprints[discard_file_id]
        or keep["current_fingerprint_id"] != expected_fingerprints[keep_file_id]
    ):
        raise RuntimeError("strong-equivalent fingerprint changed")

    discard_path = Path(discard["canonical_path"])
    keep_path = Path(keep["canonical_path"])
    if classification == "text_equivalent":
        discard_evidence, discard_normalized = inspect_normalized_text(discard_path)
        keep_evidence, keep_normalized = inspect_normalized_text(keep_path)
        if (
            not discard["normalized_sha256"]
            or discard["normalized_sha256"] != keep["normalized_sha256"]
            or discard_normalized != keep_normalized
            or discard_normalized != discard["normalized_sha256"]
        ):
            raise RuntimeError("strong TXT normalized SHA-256 revalidation failed")
    else:
        discard_evidence, keep_evidence = _revalidate_epub_equivalent(
            discard, keep, discard_path, keep_path, review
        )

    if not evidence_matches(inspect_regular_file(discard_path), discard_evidence):
        raise RuntimeError("strong-equivalent discard identity changed")
    if not evidence_matches(inspect_regular_file(keep_path), keep_evidence):
        raise RuntimeError("strong-equivalent keep identity changed")
    return user_quarantine(
        conn,
        source_file_id=discard_file_id,
        keep_file_id=keep_file_id,
        quarantine_dir=quarantine_dir,
        run_id=run_id,
        reason=f"strong_{classification}_auto_duplicate",
    )


def ingest_to_house(
    conn, *, source_file_id, destination, run_id, routing=None,
    operation_group_id=None,
):
    with mutation_lock(conn, f"house_ingest:{run_id}", run_id=run_id):
        return _ingest_to_house(
            conn,
            source_file_id=source_file_id,
            destination=destination,
            run_id=run_id,
            routing=routing,
            operation_group_id=operation_group_id,
        )


def _ingest_to_house(
    conn, *, source_file_id, destination, run_id, routing=None,
    operation_group_id=None,
):
    """Journal a temp-to-house intake while preserving the stable file_id."""
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    source = _file_state(conn, source_file_id)
    decision_store.assert_actual_run_path(
        actual_run, source["canonical_path"], "temp_root"
    )
    decision_store.assert_actual_run_path(actual_run, destination, "house_root")
    source = _ensure_intake_fingerprint(conn, source)
    source_path = _preflight(source)
    if source["source"] != "temp":
        raise RuntimeError("house intake source must be temp")
    destination = Path(decision_store.canonicalize_path(destination))
    if destination.exists():
        raise RuntimeError(f"house intake destination already exists: {destination}")
    coordinates = decision_store.coordinate_fields_from_name(destination.name)
    destination_analysis = None
    source_analysis = conn.execute(
        "SELECT * FROM file_analysis WHERE file_id = ?", (source_file_id,)
    ).fetchone()
    if source_analysis is not None and source["coordinate_kind"] == "volume":
        from bare_volume_context import parse_bare_volume_candidate

        baseline_analysis = decision_store.build_file_analysis(destination.name)
        candidate = parse_bare_volume_candidate(
            destination.name,
            analysis=baseline_analysis,
            title_override=bool(source_analysis["title_override_json"]),
        )
        if (
            candidate is not None
            and int(source["volume_den"] or 1) == 1
            and int(source["volume_num"]) == candidate.volume_number
            and str(source_analysis["core_title"] or "") == candidate.core_title
        ):
            coordinates = candidate.coordinate_fields()
            destination_analysis = candidate.apply_to_analysis(baseline_analysis)
            destination_analysis["analyzed_name"] = destination.name
    source_evidence = inspect_regular_file(source_path)
    decision_store.assert_manifest_source(
        actual_run, source_path, "temp_root", source_evidence
    )
    routing_result = None
    with decision_store.transaction(conn):
        decision_store.retire_legacy_title_requeue_path_owners(
            conn, canonical_path=destination
        )
        reserved = conn.execute(
            "SELECT file_id, active, source FROM files WHERE canonical_path = ?",
            (str(destination),),
        ).fetchone()
        if reserved is not None and reserved["file_id"] != source_file_id:
            raise RuntimeError(
                "house intake destination is reserved in state DB: "
                f"{destination} (file_id={reserved['file_id']}, "
                f"active={reserved['active']}, source={reserved['source']})"
            )
        operation_id = decision_store.create_operation(
            conn,
            run_id=run_id,
            action="house_ingest",
            source_path=str(source_path),
            dest_path=str(destination),
            file_id=source_file_id,
            expected_size=source["size"],
            expected_mtime_ns=source["mtime_ns"],
            expected_fingerprint_id=source["current_fingerprint_id"],
            operation_group_id=operation_group_id,
            source_dev=source_evidence.dev,
            source_ino=source_evidence.ino,
            source_ctime_ns=source_evidence.ctime_ns,
            source_sha256=source_evidence.sha256,
        )
    def intake_guard():
        decision_store.assert_active_actual_run(conn, run_id)
        current = _file_state(conn, source_file_id)
        if current["current_fingerprint_id"] != source["current_fingerprint_id"]:
            raise RuntimeError("intake source fingerprint changed before consume")

    destination_evidence = _copy_record_consume(
        conn, operation_id, source_path, destination, source_evidence, guard=intake_guard
    )
    moved_stat = destination.stat()
    current_fingerprint_id = source["current_fingerprint_id"] if (
        moved_stat.st_size == source["size"] and moved_stat.st_mtime_ns == source["mtime_ns"]
    ) else None
    with decision_store.transaction(conn):
        conn.execute(
            """
            UPDATE files SET canonical_path = ?, source = 'house', size = ?, mtime_ns = ?,
                dev = ?, ino = ?, ctime_ns = ?, current_fingerprint_id = ?,
                coordinate_kind = ?, part_num = ?, part_den = ?, volume_num = ?, volume_den = ?,
                coordinate_symbol = ?, coordinate_sort_key = ?, episode_start = ?, episode_end = ?,
                coordinate_raw = ?, span_ambiguous = ?
            WHERE file_id = ?
            """,
            (
                str(destination), moved_stat.st_size, moved_stat.st_mtime_ns,
                destination_evidence.dev, destination_evidence.ino,
                destination_evidence.ctime_ns, current_fingerprint_id,
                coordinates["coordinate_kind"], coordinates["part_num"], coordinates["part_den"],
                coordinates["volume_num"], coordinates["volume_den"],
                coordinates["coordinate_symbol"], coordinates["coordinate_sort_key"],
                coordinates["episode_start"], coordinates["episode_end"],
                coordinates["coordinate_raw"], coordinates["span_ambiguous"], source_file_id,
            ),
        )
        decision_store.upsert_file_analysis(
            conn,
            source_file_id,
            destination,
            analysis=destination_analysis,
            stat_result=moved_stat,
        )
        if routing is not None:
            from library_work_management import attach_routed_file

            routing_result = attach_routed_file(
                conn,
                file_id=source_file_id,
                work_bucket_id=int(routing["work_bucket_id"]),
                alias_id=int(routing["alias_id"]),
            )
        decision_store.transition_operation(conn, operation_id, "db_done")
    with decision_store.transaction(conn):
        decision_store.transition_operation(conn, operation_id, "committed")
    return {
        "operation_id": operation_id,
        "file_id": source_file_id,
        "dest_path": str(destination),
        "routing": routing_result,
    }


def _unique_destination(conn, directory, filename):
    """Return a no-clobber path unowned by both disk and the state DB.

    Inactive rows intentionally retain quarantine/queue provenance after their
    physical files are purged.  Those canonical paths still participate in the
    ``files.canonical_path`` UNIQUE constraint, so a filesystem-only vacancy is
    not sufficient.  Skipping every reserved DB path prevents a copy from
    reaching ``fs_done`` only to fail during the subsequent file-row update.
    """
    directory = Path(directory)
    ensure_directory_nofollow(directory)

    def available(candidate):
        # ``Path.exists()`` is false for a dangling symlink.  Treat it as
        # occupied as well so the later no-follow copy never targets it.
        if candidate.exists() or candidate.is_symlink():
            return False
        reserved = conn.execute(
            "SELECT 1 FROM files WHERE canonical_path = ? LIMIT 1",
            (decision_store.canonicalize_path(candidate),),
        ).fetchone()
        return reserved is None

    candidate = directory / filename
    if available(candidate):
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if available(candidate):
            return candidate
        counter += 1


def _file_state(conn, file_id):
    row = conn.execute(
        """
        SELECT f.*, fp.raw_sha256, fp.normalized_sha256,
               CASE WHEN r.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f
        LEFT JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
        LEFT JOIN representatives AS r ON r.file_id = f.file_id
        WHERE f.file_id = ? AND f.active = 1
        """,
        (file_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"active file not found: {file_id}")
    return row


def _preflight(row):
    path = Path(row["canonical_path"])
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"stale source path: {path}")
    stat = path.stat()
    _assert_row_identity(
        row,
        (stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_size, stat.st_mtime_ns),
        path,
    )
    if row["current_fingerprint_id"] is None:
        raise RuntimeError(f"current fingerprint missing: {row['file_id']}")
    return path


def _assert_row_identity(row, evidence, path):
    if hasattr(evidence, "dev"):
        actual = (
            evidence.dev, evidence.ino, evidence.ctime_ns,
            evidence.size, evidence.mtime_ns,
        )
    else:
        actual = tuple(evidence)
    expected = (
        row["dev"], row["ino"], row["ctime_ns"], row["size"], row["mtime_ns"]
    )
    # Legacy rows are populated by reconcile before actual execution. Keeping
    # nullable identity compatible here still prevents a partial legacy row
    # from authorizing a mutation by size/mtime alone.
    identity_fields = expected[:3]
    if any(value is not None for value in identity_fields):
        if any(value is None for value in identity_fields) or actual != expected:
            raise RuntimeError(f"stale source identity: {path}")
    elif actual[3:] != expected[3:]:
        raise RuntimeError(f"stale source snapshot: {path}")


def _ensure_mutable_source(row, *, allow_unassigned_house_exact=False):
    if row["protected"] or row["representative"]:
        raise RuntimeError("protected/representative file cannot be a mutation source")
    if (
        row["source"] == "house"
        and row["assignment_state"] != "managed"
        and not (
            allow_unassigned_house_exact
            and row["assignment_state"] in {
                "unassigned", "legacy_unresolved", "decision_required"
            }
        )
    ):
        raise RuntimeError("unassigned house file cannot be mutated")
    if (
        row["assignment_state"] in {"legacy_unresolved", "decision_required"}
        and not allow_unassigned_house_exact
    ):
        raise RuntimeError(f"assignment state blocks mutation: {row['assignment_state']}")


def _managed_representative_identities_for_raw_sha(
    conn, raw_sha256, size, *, known_sha_by_file_id=None
):
    """Return managed work/variant identities whose current representative matches bytes.

    The orchestrator normally detects this conflict before exact cleanup.  This
    second check keeps the mutation API fail-closed when it is called directly
    or a future caller forgets to carry the conflict set forward.  Same-size
    representatives are inspected from their current no-follow paths so stale
    or legacy fingerprint caches cannot hide an identity conflict.
    """
    rows = conn.execute(
        """
        SELECT v.work_bucket_id, f.variant_id, f.file_id, f.canonical_path,
               f.dev, f.ino, f.ctime_ns, f.size, f.mtime_ns
        FROM representatives AS r
        JOIN files AS f ON f.file_id = r.file_id
        JOIN variants AS v ON v.variant_id = f.variant_id
        WHERE f.active = 1 AND f.assignment_state = 'managed'
          AND f.size = ?
        """,
        (size,),
    ).fetchall()
    identities = set()
    known_hashes = known_sha_by_file_id or {}
    for row in rows:
        representative_sha = known_hashes.get(row["file_id"])
        if representative_sha is None:
            path = Path(row["canonical_path"])
            evidence = inspect_regular_file(path)
            _assert_row_identity(row, evidence, path)
            representative_sha = evidence.sha256
        if representative_sha == raw_sha256:
            identities.add((row["work_bucket_id"], row["variant_id"]))
    return identities


def exact_quarantine(
    conn,
    *,
    source_file_id,
    keep_file_id,
    quarantine_dir,
    run_id,
):
    with mutation_lock(conn, f"exact_quarantine:{run_id}", run_id=run_id):
        return _exact_quarantine(
            conn,
            source_file_id=source_file_id,
            keep_file_id=keep_file_id,
            quarantine_dir=quarantine_dir,
            run_id=run_id,
        )


def queue_exact_review_relationships_preserved(
    conn, source_file_id, keep_file_id
):
    """Return whether one exact queue copy can be removed without losing review edges.

    Queue files are not canonical keeps.  Two byte-identical queue rows may still
    carry different review relationships, so exact bytes alone do not authorize
    collapsing them.  The source is disposable only when both rows have the same
    queue state and every current external review incident to the source already
    has a current, role-preserving counterpart incident to the keep.  The direct
    source/keep edge is intentionally ignored because it becomes meaningless once
    the duplicate source is quarantined.
    """

    if source_file_id == keep_file_id:
        return False
    try:
        source = _file_state(conn, source_file_id)
        keep = _file_state(conn, keep_file_id)
    except ValueError:
        return False
    if (
        source["source"] != "queue"
        or keep["source"] != "queue"
        or source["assignment_state"] != keep["assignment_state"]
        or source["assignment_state"] not in {"unassigned", "decision_required"}
        or source["variant_id"] is not None
        or keep["variant_id"] is not None
        or source["protected"]
        or keep["protected"]
        or source["representative"]
        or keep["representative"]
    ):
        return False

    def signatures(focal_file_id, other_exact_file_id, focal_fingerprint_id):
        rows = conn.execute(
            """
            SELECT review_id, candidate_file_id, reference_file_id,
                   left_fingerprint_id, right_fingerprint_id, classification
            FROM review_items
            WHERE state IN ('pending', 'deferred')
              AND (candidate_file_id = ? OR reference_file_id = ?)
            ORDER BY review_id
            """,
            (focal_file_id, focal_file_id),
        ).fetchall()
        result = []
        for row in rows:
            focal_is_candidate = row["candidate_file_id"] == focal_file_id
            other_file_id = (
                row["reference_file_id"]
                if focal_is_candidate else row["candidate_file_id"]
            )
            if other_file_id == other_exact_file_id:
                continue
            focal_review_fingerprint = (
                row["left_fingerprint_id"]
                if focal_is_candidate else row["right_fingerprint_id"]
            )
            other_review_fingerprint = (
                row["right_fingerprint_id"]
                if focal_is_candidate else row["left_fingerprint_id"]
            )
            other = conn.execute(
                "SELECT active, current_fingerprint_id FROM files WHERE file_id = ?",
                (other_file_id,),
            ).fetchone()
            if (
                focal_review_fingerprint != focal_fingerprint_id
                or other is None
                or not other["active"]
                or other_review_fingerprint != other["current_fingerprint_id"]
            ):
                return None
            result.append((
                "candidate" if focal_is_candidate else "reference",
                other_file_id,
                other_review_fingerprint,
                row["classification"],
            ))
        return result

    source_signatures = signatures(
        source_file_id, keep_file_id, source["current_fingerprint_id"]
    )
    keep_signatures = signatures(
        keep_file_id, source_file_id, keep["current_fingerprint_id"]
    )
    if source_signatures is None or keep_signatures is None:
        return False
    from collections import Counter

    source_counts = Counter(source_signatures)
    keep_counts = Counter(keep_signatures)
    return all(keep_counts[key] >= count for key, count in source_counts.items())


def _exact_quarantine(
    conn,
    *,
    source_file_id,
    keep_file_id,
    quarantine_dir,
    run_id,
):
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    source = _file_state(conn, source_file_id)
    keep = _file_state(conn, keep_file_id)
    # Some legacy files and analyses that could not decode the body have no
    # current fingerprint.  Exact cleanup still has stronger evidence
    # available: both regular files are hashed in full immediately below.
    # Attach an immutable raw-only fingerprint first so the journal and guards
    # remain identity-bound instead of weakening the mutation contract.
    source = _ensure_intake_fingerprint(conn, source)
    keep = _ensure_intake_fingerprint(conn, keep)
    source_root = "house_root" if source["source"] == "house" else "temp_root"
    decision_store.assert_actual_run_path(actual_run, source["canonical_path"], source_root)
    keep_root = "house_root" if keep["source"] == "house" else "temp_root"
    decision_store.assert_actual_run_path(
        actual_run, keep["canonical_path"], keep_root
    )
    decision_store.assert_actual_run_path(actual_run, quarantine_dir, "temp_root")
    # Exact cleanup is the only automatic mutation allowed to consume an
    # unassigned or unresolved house file.  It is still bound to the approved
    # manifest, never consumes a protected/representative file, and both files
    # are raw-SHA revalidated immediately below.  Other mutation paths keep the
    # stricter managed-only house rule.
    _ensure_mutable_source(source, allow_unassigned_house_exact=True)
    source_path = _preflight(source)
    keep_path = _preflight(keep)
    if source_file_id == keep_file_id:
        raise ValueError("source and keep must differ")
    queue_keep = keep["source"] == "queue"
    if keep["source"] != "house" and not (
        source["source"] == "queue"
        and queue_keep
        and queue_exact_review_relationships_preserved(
            conn, source_file_id, keep_file_id
        )
    ):
        raise RuntimeError(
            "exact keep must be house or a relationship-preserving queue peer"
        )
    if keep["assignment_state"] not in {
        "managed", "unassigned", "legacy_unresolved", "decision_required"
    }:
        raise RuntimeError("exact keep assignment state blocks mutation")
    if keep["assignment_state"] == "managed" and not keep["representative"]:
        raise RuntimeError("managed exact keep must be representative")
    if source["assignment_state"] == "managed":
        if (
            keep["assignment_state"] != "managed"
            or source["variant_id"] != keep["variant_id"]
        ):
            raise RuntimeError("managed exact files belong to different variants")
    coordinate_relation = classify_dedup_coordinate_relation(
        source_path.name,
        keep_path.name,
        left_span_ambiguous=bool(source["span_ambiguous"]),
        right_span_ambiguous=bool(keep["span_ambiguous"]),
    )
    same_core_title = (
        analyze_name(source_path.name).get("core_title")
        == analyze_name(keep_path.name).get("core_title")
    )
    if (
        same_core_title
        and coordinate_relation is None
        and not decision_store.coordinates_compatible(
            _coordinate_view(source), _coordinate_view(keep)
        )
    ):
        raise RuntimeError("exact files have incompatible canonical coordinates")
    # Recompute both raw hashes immediately before the move. Cached hashes are
    # also checked so stale/corrupt cache cannot authorize a logical deletion.
    source_evidence = inspect_regular_file(source_path)
    keep_evidence = inspect_regular_file(keep_path)
    if source["source"] == "queue":
        decision_store.assert_manifest_or_same_run_queue_source(
            conn,
            actual_run,
            source_path,
            source_evidence,
            file_id=source_file_id,
        )
    else:
        decision_store.assert_manifest_source(
            actual_run, source_path, source_root, source_evidence
        )
    if queue_keep:
        # The queue peer may have existed at activation or may be an exact,
        # durably recorded destination of this same run's review move.
        decision_store.assert_manifest_or_same_run_queue_source(
            conn,
            actual_run,
            keep_path,
            keep_evidence,
            file_id=keep_file_id,
        )
    else:
        # A house keep must either be part of the activation snapshot or be an
        # exact destination durably produced by this still-active run.
        decision_store.assert_manifest_or_same_run_house_source(
            conn, actual_run, keep_path, keep_evidence
        )
    source_hash = source_evidence.sha256
    keep_hash = keep_evidence.sha256
    if source_hash != keep_hash:
        raise RuntimeError("exact raw SHA revalidation failed")
    representative_identities = _managed_representative_identities_for_raw_sha(
        conn,
        source_hash,
        source_evidence.size,
        known_sha_by_file_id={keep_file_id: keep_hash},
    )
    if len(representative_identities) > 1:
        raise RuntimeError(
            "exact source matches multiple managed representative identities"
        )
    # decode_lossy/epub_error fingerprints intentionally may not contain a raw
    # cache.  Absence is not a mismatch: the two current files were just read
    # in full and matched.  A populated cache, however, must still agree.
    if source["raw_sha256"] is not None and source["raw_sha256"] != source_hash:
        raise RuntimeError(
            f"cached source raw SHA does not match current bytes: {source_path}"
        )
    if keep["raw_sha256"] is not None and keep["raw_sha256"] != keep_hash:
        raise RuntimeError(
            f"cached keep raw SHA does not match current bytes: {keep_path}"
        )

    destination = _unique_destination(conn, quarantine_dir, source_path.name)
    with decision_store.transaction(conn):
        operation_id = decision_store.create_operation(
            conn,
            run_id=run_id,
            action="exact_quarantine",
            source_path=str(source_path),
            quarantine_path=str(destination),
            file_id=source_file_id,
            keep_file_id=keep_file_id,
            expected_size=source["size"],
            expected_mtime_ns=source["mtime_ns"],
            expected_fingerprint_id=source["current_fingerprint_id"],
            expected_keep_fingerprint_id=keep["current_fingerprint_id"],
            source_dev=source_evidence.dev,
            source_ino=source_evidence.ino,
            source_ctime_ns=source_evidence.ctime_ns,
            source_sha256=source_evidence.sha256,
        )

    def exact_guard():
        decision_store.assert_active_actual_run(conn, run_id)
        current_source = _file_state(conn, source_file_id)
        current_keep = _file_state(conn, keep_file_id)
        if current_source["current_fingerprint_id"] != source["current_fingerprint_id"]:
            raise RuntimeError("exact source fingerprint changed before consume")
        if current_keep["current_fingerprint_id"] != keep["current_fingerprint_id"]:
            raise RuntimeError("exact keep guard changed before consume")
        if current_keep["source"] != keep["source"]:
            raise RuntimeError("exact keep source changed before consume")
        if queue_keep and not queue_exact_review_relationships_preserved(
            conn, source_file_id, keep_file_id
        ):
            raise RuntimeError("queue exact review relationships changed before consume")
        if current_keep["assignment_state"] == "managed":
            if not current_keep["representative"]:
                raise RuntimeError("managed exact keep is no longer representative")
        elif current_keep["assignment_state"] not in {
            "unassigned", "legacy_unresolved", "decision_required"
        }:
            raise RuntimeError("exact keep assignment changed before consume")
        if not evidence_matches(inspect_regular_file(keep_path), keep_evidence):
            raise RuntimeError("exact keep identity changed before consume")

    destination_evidence = _copy_record_consume(
        conn, operation_id, source_path, destination, source_evidence, guard=exact_guard
    )

    with decision_store.transaction(conn):
        keep_is_managed = keep["assignment_state"] == "managed"
        variant_id = keep["variant_id"] if keep_is_managed else None
        assignment_state = "managed" if keep_is_managed else "unassigned"
        assignment_origin = "strong_match" if keep_is_managed else None
        conn.execute(
            """
            UPDATE files
            SET canonical_path = ?, source = 'quarantine', active = 0,
                dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?,
                variant_id = ?, assignment_state = ?, assignment_origin = ?
            WHERE file_id = ?
            """,
            (
                str(destination), destination_evidence.dev, destination_evidence.ino,
                destination_evidence.ctime_ns, destination_evidence.size,
                destination_evidence.mtime_ns, variant_id, assignment_state,
                assignment_origin, source_file_id,
            ),
        )
        decision_store.supersede_open_reviews_for_inactive_file(
            conn, source_file_id, reason="exact_quarantine"
        )
        decision_store.transition_operation(conn, operation_id, "db_done")
    with decision_store.transaction(conn):
        decision_store.transition_operation(conn, operation_id, "committed")
    return {
        "operation_id": operation_id,
        "action": "exact_quarantine",
        "source_file_id": source_file_id,
        "keep_file_id": keep_file_id,
        "dest_path": str(destination),
    }


def user_quarantine(
    conn,
    *,
    source_file_id,
    keep_file_id=None,
    replacement_file_id=None,
    quarantine_dir,
    run_id,
    reason="user_approved_discard",
    keep_origin_operation_id=None,
):
    """Journal an explicit user-approved discard without asserting byte equality.

    This is intentionally separate from ``exact_quarantine`` and
    ``same_content`` decisions.  It records a file-level disposition while
    preserving the named keep file and superseding stale open review edges.
    """
    with mutation_lock(conn, f"user_quarantine:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        source = _file_state(conn, source_file_id)
        keep = _file_state(conn, keep_file_id) if keep_file_id else None
        if keep_file_id and source_file_id == keep_file_id:
            raise ValueError("discard source and keep must differ")
        if keep is not None and keep["source"] != "house":
            raise RuntimeError("user-discard keep file must already be in house")
        replacement = _file_state(conn, replacement_file_id) if replacement_file_id else None
        if source["representative"]:
            if replacement is not None:
                if (
                    replacement["source"] != "house"
                    or replacement["variant_id"] != source["variant_id"]
                    or replacement_file_id == source_file_id
                ):
                    raise RuntimeError("replacement representative must be another active house file in the same variant")
            else:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM files WHERE variant_id = ? AND active = 1 AND file_id != ?",
                    (source["variant_id"], source_file_id),
                ).fetchone()[0]
                if remaining:
                    raise RuntimeError("representative replacement is required while the variant still has active files")

        source_root = "house_root" if source["source"] == "house" else "temp_root"
        decision_store.assert_actual_run_path(
            actual_run, source["canonical_path"], source_root
        )
        if keep is not None:
            decision_store.assert_actual_run_path(
                actual_run, keep["canonical_path"], "house_root"
            )
        if replacement is not None:
            decision_store.assert_actual_run_path(
                actual_run, replacement["canonical_path"], "house_root"
            )
        decision_store.assert_actual_run_path(actual_run, quarantine_dir, "temp_root")
        source_path = _preflight(source)
        keep_path = _preflight(keep) if keep is not None else None
        replacement_path = _preflight(replacement) if replacement is not None else None
        source_evidence = inspect_regular_file(source_path)
        keep_evidence = inspect_regular_file(keep_path) if keep_path is not None else None
        replacement_evidence = (
            inspect_regular_file(replacement_path) if replacement_path is not None else None
        )
        if source["source"] == "queue":
            decision_store.assert_manifest_or_same_run_queue_source(
                conn,
                actual_run,
                source_path,
                source_evidence,
                file_id=source_file_id,
            )
        else:
            decision_store.assert_manifest_source(
                actual_run, source_path, source_root, source_evidence
            )
        if keep_path is not None:
            if keep_origin_operation_id is None:
                decision_store.assert_manifest_source(
                    actual_run, keep_path, "house_root", keep_evidence
                )
            else:
                keep_origin = conn.execute(
                    "SELECT * FROM operations WHERE operation_id = ?",
                    (int(keep_origin_operation_id),),
                ).fetchone()
                expected_destination = (
                    keep_evidence.dev,
                    keep_evidence.ino,
                    keep_evidence.ctime_ns,
                    keep_evidence.size,
                    keep_evidence.mtime_ns,
                    keep_evidence.sha256,
                )
                recorded_destination = (
                    keep_origin["destination_dev"],
                    keep_origin["destination_ino"],
                    keep_origin["destination_ctime_ns"],
                    keep_origin["destination_size"],
                    keep_origin["destination_mtime_ns"],
                    keep_origin["destination_sha256"],
                ) if keep_origin is not None else None
                if (
                    keep_origin is None
                    or keep_origin["run_id"] != run_id
                    or keep_origin["action"] not in {
                        "house_ingest", "user_queue_accept",
                        "user_quarantine_restore",
                    }
                    or keep_origin["state"] != "committed"
                    or keep_origin["file_id"] != keep_file_id
                    or keep_origin["dest_path"] != str(keep_path)
                    or recorded_destination != expected_destination
                ):
                    raise RuntimeError(
                        "user-discard keep is not owned by the current ingest"
                    )
        if replacement_path is not None and replacement_path != keep_path:
            decision_store.assert_manifest_source(
                actual_run, replacement_path, "house_root", replacement_evidence
            )
        destination = _unique_destination(conn, quarantine_dir, source_path.name)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="user_quarantine",
                source_path=str(source_path),
                quarantine_path=str(destination),
                file_id=source_file_id,
                keep_file_id=keep_file_id,
                expected_size=source["size"],
                expected_mtime_ns=source["mtime_ns"],
                expected_fingerprint_id=source["current_fingerprint_id"],
                expected_keep_fingerprint_id=(
                    keep["current_fingerprint_id"] if keep is not None else None
                ),
                source_dev=source_evidence.dev,
                source_ino=source_evidence.ino,
                source_ctime_ns=source_evidence.ctime_ns,
                source_sha256=source_evidence.sha256,
            )

        def guard():
            decision_store.assert_active_actual_run(conn, run_id)
            current_source = _file_state(conn, source_file_id)
            current_keep = _file_state(conn, keep_file_id) if keep_file_id else None
            if current_source["current_fingerprint_id"] != source["current_fingerprint_id"]:
                raise RuntimeError("user-discard source fingerprint changed")
            if keep is not None and current_keep["current_fingerprint_id"] != keep["current_fingerprint_id"]:
                raise RuntimeError("user-discard keep fingerprint changed")
            if keep_path is not None and not evidence_matches(inspect_regular_file(keep_path), keep_evidence):
                raise RuntimeError("user-discard keep identity changed")
            if replacement is not None:
                current_replacement = _file_state(conn, replacement_file_id)
                if current_replacement["current_fingerprint_id"] != replacement["current_fingerprint_id"]:
                    raise RuntimeError("replacement representative fingerprint changed")
                if not evidence_matches(inspect_regular_file(replacement_path), replacement_evidence):
                    raise RuntimeError("replacement representative identity changed")

        destination_evidence = _copy_record_consume(
            conn,
            operation_id,
            source_path,
            destination,
            source_evidence,
            guard=guard,
        )
        with decision_store.transaction(conn):
            if source["representative"]:
                if replacement is None:
                    conn.execute(
                        "DELETE FROM representatives WHERE variant_id = ? AND file_id = ?",
                        (source["variant_id"], source_file_id),
                    )
                else:
                    conn.execute(
                        "UPDATE representatives SET file_id = ?, updated_at = CURRENT_TIMESTAMP WHERE variant_id = ? AND file_id = ?",
                        (replacement_file_id, source["variant_id"], source_file_id),
                    )
                    conn.execute(
                        "UPDATE files SET protected = 1 WHERE file_id = ?",
                        (replacement_file_id,),
                    )
            conn.execute(
                """
                UPDATE files SET canonical_path = ?, source = 'quarantine', active = 0,
                    protected = 0, dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?
                WHERE file_id = ?
                """,
                (
                    str(destination), destination_evidence.dev, destination_evidence.ino,
                    destination_evidence.ctime_ns, destination_evidence.size,
                    destination_evidence.mtime_ns, source_file_id,
                ),
            )
            decision_store.supersede_open_reviews_for_file(
                conn, source_file_id, reason=reason
            )
            decision_store.transition_operation(conn, operation_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, operation_id, "committed")
        return {
            "operation_id": operation_id,
            "action": "user_quarantine",
            "source_file_id": source_file_id,
            "keep_file_id": keep_file_id,
            "dest_path": str(destination),
        }


def hold_epub_analysis_error(
    conn,
    *,
    source_file_id,
    temp_root,
    run_id,
    analysis_error,
    max_file_bytes,
    max_uncompressed_bytes,
):
    """Journal one ambiguous incoming EPUB into a recoverable warning queue.

    This is not a duplicate decision or a discard. The move is allowed only
    when the same bounded inspection reproduces the exact auditor error and the
    actual-run manifest still owns the incoming file identity.
    """

    with mutation_lock(conn, f"epub-analysis-hold:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        source = _file_state(conn, source_file_id)
        if source["source"] != "temp":
            raise RuntimeError("EPUB analysis hold source must be temp")
        if (
            source["variant_id"] is not None
            or source["protected"]
            or source["representative"]
            or source["assignment_state"] == "managed"
        ):
            raise RuntimeError(
                "managed EPUB analysis error requires relationship-preserving review"
            )
        source = _ensure_intake_fingerprint(conn, source)
        inspection_options = {"max_file_bytes": max_file_bytes}
        if max_uncompressed_bytes is not None:
            inspection_options["max_uncompressed_bytes"] = max_uncompressed_bytes
        try:
            inspect_epub_content(
                source["canonical_path"], **inspection_options
            )
        except (RuntimeError, zipfile.BadZipFile) as exc:
            current_error = str(exc)
        else:
            current_error = None
        if not analysis_error or current_error != analysis_error:
            raise RuntimeError("EPUB analysis hold evidence is not current")

        source_path = _preflight(source)
        decision_store.assert_actual_run_path(
            actual_run, source_path, "temp_root"
        )
        source_evidence = inspect_regular_file(source_path)
        decision_store.assert_manifest_source(
            actual_run, source_path, "temp_root", source_evidence
        )
        destination_dir = (
            Path(temp_root) / "trash_bin" / "warning" / "epub_analysis_errors"
        )
        destination = _unique_destination(conn, destination_dir, source_path.name)
        decision_store.assert_actual_run_path(
            actual_run, destination, "temp_root"
        )
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="epub_analysis_hold",
                source_path=str(source_path),
                dest_path=str(destination),
                file_id=source_file_id,
                expected_size=source["size"],
                expected_mtime_ns=source["mtime_ns"],
                expected_fingerprint_id=source["current_fingerprint_id"],
                source_dev=source_evidence.dev,
                source_ino=source_evidence.ino,
                source_ctime_ns=source_evidence.ctime_ns,
                source_sha256=source_evidence.sha256,
            )

        def guard():
            decision_store.assert_active_actual_run(conn, run_id)
            current = _file_state(conn, source_file_id)
            if current["current_fingerprint_id"] != source["current_fingerprint_id"]:
                raise RuntimeError("EPUB analysis hold source changed before consume")

        destination_evidence = _copy_record_consume(
            conn,
            operation_id,
            source_path,
            destination,
            source_evidence,
            guard=guard,
        )
        with decision_store.transaction(conn):
            conn.execute(
                """
                UPDATE files
                SET canonical_path = ?, source = 'queue',
                    assignment_state = 'decision_required',
                    assignment_origin = NULL, variant_id = NULL, protected = 0,
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE file_id = ?
                """,
                (
                    str(destination),
                    destination_evidence.dev,
                    destination_evidence.ino,
                    destination_evidence.ctime_ns,
                    destination_evidence.size,
                    destination_evidence.mtime_ns,
                    source_file_id,
                ),
            )
            decision_store.transition_operation(conn, operation_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, operation_id, "committed")
        return {
            "operation_id": operation_id,
            "action": "epub_analysis_hold",
            "file_id": source_file_id,
            "source_path": str(source_path),
            "dest_path": str(destination),
            "analysis_error": analysis_error,
        }


def record_user_approved_purge_revalidation(
    conn,
    *,
    origin_operation_id,
    keep_file_id,
    run_id,
    operation_group_id,
):
    """Record fresh owned evidence for an explicitly reviewed old quarantine.

    This operation does not move bytes.  It binds the still-owned quarantine
    snapshot to a current active house reference so a later irreversible purge
    does not rely on stale fingerprint IDs or a superseded original keep.
    """
    with mutation_lock(conn, f"user_purge_revalidation:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        origin = conn.execute(
            """
            SELECT o.*, f.canonical_path AS file_path, f.source AS file_source,
                   f.active AS file_active,
                   f.current_fingerprint_id AS file_fingerprint_id
            FROM operations AS o
            JOIN files AS f ON f.file_id = o.file_id
            WHERE o.operation_id = ?
            """,
            (int(origin_operation_id),),
        ).fetchone()
        if (
            origin is None
            or origin["action"] not in {"user_quarantine", "exact_quarantine"}
            or origin["state"] != "committed"
            or origin["purged_at"] is not None
            or origin["file_active"]
            or origin["file_source"] != "quarantine"
        ):
            raise RuntimeError("purge revalidation requires a live quarantine")
        group = conn.execute(
            "SELECT action, state FROM operation_groups WHERE group_id = ?",
            (int(operation_group_id),),
        ).fetchone()
        if (
            group is None
            or group["action"] != "quarantine_cleanup_1_4_4_revalidation"
            or group["state"] != "planned"
        ):
            raise RuntimeError("purge revalidation requires its approved plan group")
        keep = _file_state(conn, keep_file_id)
        if keep["source"] != "house" or keep_file_id == origin["file_id"]:
            raise RuntimeError("purge revalidation keep must be another house file")

        quarantine_path = Path(origin["quarantine_path"] or origin["dest_path"] or "")
        keep_path = Path(keep["canonical_path"])
        quarantine_evidence = inspect_regular_file(quarantine_path)
        keep_evidence = inspect_regular_file(keep_path)
        # Finder/open-in-place activity and later folder maintenance may change
        # inode/ctime/mtime without changing the book bytes.  The current
        # actual-run manifest owns the fresh identity, while the old journal
        # only needs to prove that it originally owned the same path/content.
        if (
            origin["file_path"] != str(quarantine_path)
            or origin["destination_sha256"] != quarantine_evidence.sha256
            or origin["destination_size"] != quarantine_evidence.size
        ):
            raise RuntimeError("purge revalidation quarantine ownership is stale")
        decision_store.assert_actual_run_path(
            actual_run, quarantine_path, "temp_root"
        )
        decision_store.assert_manifest_source(
            actual_run, quarantine_path, "temp_root", quarantine_evidence
        )
        decision_store.assert_actual_run_path(actual_run, keep_path, "house_root")
        decision_store.assert_manifest_source(
            actual_run, keep_path, "house_root", keep_evidence
        )

        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn,
                run_id=run_id,
                action="user_approved_purge_revalidation",
                source_path=str(quarantine_path),
                dest_path=str(keep_path),
                file_id=origin["file_id"],
                keep_file_id=keep_file_id,
                expected_size=quarantine_evidence.size,
                expected_mtime_ns=quarantine_evidence.mtime_ns,
                expected_fingerprint_id=origin["file_fingerprint_id"],
                expected_keep_fingerprint_id=keep["current_fingerprint_id"],
                parent_operation_id=origin["operation_id"],
                operation_group_id=operation_group_id,
                source_dev=quarantine_evidence.dev,
                source_ino=quarantine_evidence.ino,
                source_ctime_ns=quarantine_evidence.ctime_ns,
                source_sha256=quarantine_evidence.sha256,
            )
            decision_store.record_operation_destination(
                conn, operation_id, keep_evidence
            )
            decision_store.transition_operation(conn, operation_id, "fs_done")
            decision_store.transition_operation(conn, operation_id, "db_done")
            decision_store.transition_operation(conn, operation_id, "committed")
        return {
            "operation_id": operation_id,
            "origin_operation_id": int(origin_operation_id),
            "file_id": origin["file_id"],
            "keep_file_id": keep_file_id,
            "quarantine_path": str(quarantine_path),
            "keep_path": str(keep_path),
        }


def _normalized_metadata_token(value):
    return unicodedata.normalize("NFC", str(value or "")).strip().casefold()


def _author_tokens(value):
    return {
        token for token in re.split(r"[\s,/&·]+", _normalized_metadata_token(value))
        if token
    }


def _contained_metadata(conn, file_id):
    row = conn.execute(
        "SELECT core_title, author, unit FROM file_analysis WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    if row is not None:
        return row
    file_row = conn.execute(
        "SELECT canonical_path FROM files WHERE file_id = ? AND active = 1",
        (file_id,),
    ).fetchone()
    if file_row is None:
        raise RuntimeError("contained upgrade endpoint is no longer active")
    return decision_store.build_effective_file_analysis(
        conn, file_id, Path(file_row["canonical_path"]).name
    )


def _coordinate_view(row):
    if row["episode_start"] is not None or row["episode_end"] is not None:
        return row
    return decision_store.coordinate_fields_from_name(Path(row["canonical_path"]).name)


def apply_contained_upgrade(
    conn,
    *,
    review_id,
    shorter_file_id,
    longer_file_id,
    quarantine_dir,
    run_id,
    house_destination=None,
    classification="contained_exact",
):
    """Adopt a strictly longer TXT version and quarantine its complete prefix.

    The command-level root lock held by managed Folderling spans this orchestration.
    Each filesystem move is additionally journaled by ``house_ingest`` or
    ``user_quarantine``.  Intermediate crash states remain safe: the old
    representative stays valid until the new file is in house, and a later run
    can finish quarantining an already-ingested longer endpoint.
    """
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    assert_mutation_lock_held(conn, run_id=run_id)
    shorter = _file_state(conn, shorter_file_id)
    longer = _file_state(conn, longer_file_id)
    if shorter_file_id == longer_file_id:
        raise ValueError("contained upgrade endpoints must differ")
    if shorter["source"] not in {"house", "temp", "queue"} or longer["source"] not in {
        "house", "temp", "queue"
    }:
        raise RuntimeError(
            "contained upgrade endpoints must be active house/temp/queue files"
        )
    if "house" not in {shorter["source"], longer["source"]}:
        raise RuntimeError("contained upgrade requires an established house endpoint")
    if Path(shorter["canonical_path"]).suffix.lower() != ".txt" or Path(
        longer["canonical_path"]
    ).suffix.lower() != ".txt":
        raise RuntimeError("contained upgrade currently supports TXT only")

    review = conn.execute(
        "SELECT * FROM review_items WHERE review_id = ?", (review_id,)
    ).fetchone()
    if (
        review is None
        or review["classification"] != classification
        or classification not in {"contained_exact", "contained_version"}
        or review["state"] not in {"pending", "deferred"}
        or {review["candidate_file_id"], review["reference_file_id"]}
        != {shorter_file_id, longer_file_id}
    ):
        raise RuntimeError("contained upgrade requires a current contained review")
    expected_fingerprints = {
        review["candidate_file_id"]: review["left_fingerprint_id"],
        review["reference_file_id"]: review["right_fingerprint_id"],
    }
    if (
        shorter["current_fingerprint_id"] != expected_fingerprints[shorter_file_id]
        or longer["current_fingerprint_id"] != expected_fingerprints[longer_file_id]
    ):
        raise RuntimeError("contained upgrade fingerprint changed")

    short_meta = _contained_metadata(conn, shorter_file_id)
    long_meta = _contained_metadata(conn, longer_file_id)
    coordinate_relation = classify_dedup_coordinate_relation(
        Path(shorter["canonical_path"]).name,
        Path(longer["canonical_path"]).name,
        left_span_ambiguous=bool(shorter["span_ambiguous"]),
        right_span_ambiguous=bool(longer["span_ambiguous"]),
    )
    special_coordinates = bool(
        coordinate_relation is not None
        and coordinate_relation.mode in DEDUP_SPECIAL_COORDINATE_MODES
    )
    if (
        coordinate_relation is None
        or coordinate_relation.preferred_side != "right"
    ):
        raise RuntimeError("contained upgrade declared coverage is not strictly wider")
    if (
        not special_coordinates
        and (
            not _normalized_metadata_token(short_meta["core_title"])
            or _normalized_metadata_token(short_meta["core_title"])
            != _normalized_metadata_token(long_meta["core_title"])
        )
    ):
        raise RuntimeError("contained upgrade core title mismatch")
    short_authors = _author_tokens(short_meta["author"])
    long_authors = _author_tokens(long_meta["author"])
    if short_authors and long_authors and not (short_authors & long_authors):
        raise RuntimeError("contained upgrade author mismatch")
    if not special_coordinates and short_meta["unit"] != long_meta["unit"]:
        raise RuntimeError("contained upgrade unit mismatch")

    if not special_coordinates:
        short_coordinates = _coordinate_view(shorter)
        long_coordinates = _coordinate_view(longer)
        if (
            short_coordinates["span_ambiguous"]
            or long_coordinates["span_ambiguous"]
            or short_coordinates["episode_start"] is None
            or short_coordinates["episode_end"] is None
            or long_coordinates["episode_start"] is None
            or long_coordinates["episode_end"] is None
            or short_coordinates["episode_start"] != long_coordinates["episode_start"]
            or long_coordinates["episode_end"] <= short_coordinates["episode_end"]
        ):
            raise RuntimeError(
                "contained upgrade requires a strict matching episode span"
            )

    short_path = _preflight(shorter)
    long_path = _preflight(longer)
    proof = inspect_contained_text(short_path, long_path)
    _assert_row_identity(shorter, proof.short_file_evidence, short_path)
    _assert_row_identity(longer, proof.long_file_evidence, long_path)
    anchors_prove_same_body = contained_anchor_proof_sufficient(proof)
    prefix_proves_same_body = (
        proof.long_prefix_sha256 == proof.short_normalized_sha256
    )
    if (
        not shorter["normalized_sha256"]
        or not longer["normalized_sha256"]
        or proof.short_normalized_sha256 != shorter["normalized_sha256"]
        or proof.long_normalized_sha256 != longer["normalized_sha256"]
        or proof.short_normalized_length >= proof.long_normalized_length
    ):
        raise RuntimeError("contained upgrade normalized-prefix revalidation failed")
    if classification == "contained_exact" and not prefix_proves_same_body:
        raise RuntimeError("contained_exact prefix proof changed")
    if classification == "contained_version" and not anchors_prove_same_body:
        raise ContainedUpgradeNotProven(
            "contained_version distributed-anchor proof is insufficient"
        )
    short_root = "house_root" if shorter["source"] == "house" else "temp_root"
    long_root = "house_root" if longer["source"] == "house" else "temp_root"
    if shorter["source"] == "queue":
        decision_store.assert_manifest_or_same_run_queue_source(
            conn,
            actual_run,
            short_path,
            proof.short_file_evidence,
            file_id=shorter_file_id,
        )
    else:
        decision_store.assert_manifest_source(
            actual_run, short_path, short_root, proof.short_file_evidence
        )
    if longer["source"] == "queue":
        decision_store.assert_manifest_or_same_run_queue_source(
            conn,
            actual_run,
            long_path,
            proof.long_file_evidence,
            file_id=longer_file_id,
        )
    else:
        decision_store.assert_manifest_source(
            actual_run, long_path, long_root, proof.long_file_evidence
        )

    ingest_result = None
    if longer["source"] in {"temp", "queue"}:
        if house_destination is None:
            raise RuntimeError("contained upgrade house destination is required")
        destination = Path(decision_store.canonicalize_path(house_destination))
        decision_store.assert_actual_run_path(actual_run, destination, "house_root")
        if longer["source"] == "queue":
            ingest_result = user_queue_accept_to_house(
                conn,
                file_id=longer_file_id,
                destination=destination,
                run_id=run_id,
            )
        else:
            ingest_result = ingest_to_house(
                conn,
                source_file_id=longer_file_id,
                destination=destination,
                run_id=run_id,
            )
        longer = _file_state(conn, longer_file_id)
    elif house_destination is not None and decision_store.canonicalize_path(
        house_destination
    ) != longer["canonical_path"]:
        raise RuntimeError("contained upgrade destination disagrees with current house path")

    shorter = _file_state(conn, shorter_file_id)
    longer = _file_state(conn, longer_file_id)
    if longer["source"] != "house":
        raise RuntimeError("contained upgrade keep endpoint is not in house")

    with decision_store.transaction(conn):
        decision_store.mark_actual_run_mutation_started(conn, run_id)
        if shorter["source"] == "house" and shorter["variant_id"] is not None:
            if shorter["assignment_state"] != "managed":
                raise RuntimeError("contained upgrade source has an unresolved relationship")
            if shorter["representative"]:
                if longer["variant_id"] not in {None, shorter["variant_id"]}:
                    raise RuntimeError("contained upgrade crosses managed variants")
                if longer["representative"] and longer_file_id != shorter_file_id:
                    raise RuntimeError("contained upgrade keep is already another representative")
                conn.execute(
                    """
                    UPDATE files SET variant_id = ?, assignment_state = 'managed',
                        assignment_origin = 'strong_match', protected = 1
                    WHERE file_id = ?
                    """,
                    (shorter["variant_id"], longer_file_id),
                )
                replaced = conn.execute(
                    """
                    UPDATE representatives SET file_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE variant_id = ? AND file_id = ?
                    """,
                    (longer_file_id, shorter["variant_id"], shorter_file_id),
                )
                if replaced.rowcount != 1:
                    raise RuntimeError("contained upgrade representative changed")
                conn.execute(
                    "UPDATE files SET protected = 0 WHERE file_id = ?",
                    (shorter_file_id,),
                )
            elif not (
                longer["representative"]
                and longer["variant_id"] == shorter["variant_id"]
                and not shorter["protected"]
            ):
                raise RuntimeError("contained upgrade managed relationship is ambiguous")
        elif shorter["source"] == "house" and (
            shorter["protected"] or shorter["representative"]
        ):
            raise RuntimeError("contained upgrade cannot consume a protected loose file")
        elif shorter["source"] == "house" and shorter["assignment_state"] not in {
            "unassigned", "managed"
        }:
            raise RuntimeError("contained upgrade source requires a prior decision")
        elif shorter["source"] == "temp" and (
            shorter["protected"] or shorter["representative"]
        ):
            raise RuntimeError("contained upgrade temp source is protected")

    quarantine_result = user_quarantine(
        conn,
        source_file_id=shorter_file_id,
        keep_file_id=longer_file_id,
        quarantine_dir=quarantine_dir,
        run_id=run_id,
        reason=f"{classification}_auto_superseded",
        keep_origin_operation_id=(
            ingest_result["operation_id"] if ingest_result is not None else None
        ),
    )
    return {
        "classification": classification,
        "shorter_file_id": shorter_file_id,
        "longer_file_id": longer_file_id,
        "short_normalized_sha256": proof.short_normalized_sha256,
        "long_normalized_sha256": proof.long_normalized_sha256,
        "short_normalized_length": proof.short_normalized_length,
        "long_normalized_length": proof.long_normalized_length,
        "ordered_anchor_count": proof.ordered_anchor_count,
        "anchor_chars": proof.anchor_chars,
        "anchor_offset_span": proof.anchor_offset_span,
        "ingest_operation_id": (
            ingest_result["operation_id"] if ingest_result is not None else None
        ),
        "ingested_path": (
            ingest_result["dest_path"] if ingest_result is not None
            else longer["canonical_path"]
        ),
        "quarantine_operation_id": quarantine_result["operation_id"],
        "quarantine_path": quarantine_result["dest_path"],
    }


def apply_ordered_body_quarantine(
    conn,
    *,
    review_id,
    discard_file_id,
    keep_file_id,
    quarantine_dir,
    run_id,
    house_destination=None,
    classification="ordered_body_match",
):
    """Finalize a same-core edition after a current 95% body proof.

    Queue/house ``near_identical`` and ``longer_unresolved`` reviews additionally
    need the same proof in reverse, so a shared fragment cannot consume a
    distinct edition.  House -> house is limited to the near-identical dated-
    distribution contract and requires 99% in both directions.
    """
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    assert_mutation_lock_held(conn, run_id=run_id)
    discard = _file_state(conn, discard_file_id)
    keep = _file_state(conn, keep_file_id)
    if discard_file_id == keep_file_id:
        raise ValueError("ordered body endpoints must differ")
    if discard["source"] not in {"house", "temp", "queue"} or keep["source"] not in {
        "house", "temp", "queue"
    }:
        raise RuntimeError(
            "ordered body endpoints must be active house/temp/queue files"
        )
    if "house" not in {discard["source"], keep["source"]}:
        raise RuntimeError("ordered body quarantine requires a house endpoint")
    if Path(discard["canonical_path"]).suffix.lower() != ".txt" or Path(
        keep["canonical_path"]
    ).suffix.lower() != ".txt":
        raise RuntimeError("ordered body quarantine supports TXT only")
    legacy_marker_discard = _legacy_marker_discard_contract(discard, keep)
    if (
        discard["assignment_state"] == "legacy_unresolved"
        or keep["assignment_state"] == "legacy_unresolved"
    ) and not legacy_marker_discard:
        raise RuntimeError(
            "ordered body legacy marker is allowed only on the loose discard"
        )

    review = conn.execute(
        "SELECT * FROM review_items WHERE review_id = ?", (review_id,)
    ).fetchone()
    if (
        review is None
        or review["classification"] != classification
        or classification not in {
            "ordered_body_match", "near_identical", "longer_unresolved"
        }
        or review["state"] not in {"pending", "deferred"}
        or {review["candidate_file_id"], review["reference_file_id"]}
        != {discard_file_id, keep_file_id}
    ):
        raise RuntimeError("ordered body quarantine requires a current review")
    expected_fingerprints = {
        review["candidate_file_id"]: review["left_fingerprint_id"],
        review["reference_file_id"]: review["right_fingerprint_id"],
    }
    if (
        discard["current_fingerprint_id"]
        != expected_fingerprints[discard_file_id]
        or keep["current_fingerprint_id"] != expected_fingerprints[keep_file_id]
    ):
        raise RuntimeError("ordered body quarantine fingerprint changed")

    discard_meta = _contained_metadata(conn, discard_file_id)
    keep_meta = _contained_metadata(conn, keep_file_id)
    discard_core = _normalized_metadata_token(discard_meta["core_title"])
    keep_core = _normalized_metadata_token(keep_meta["core_title"])
    bidirectional_review = classification in {
        "near_identical", "longer_unresolved"
    }
    loose_upgrade_relation = classify_loose_title_upgrade_relation(
        Path(discard["canonical_path"]).name,
        Path(keep["canonical_path"]).name,
        left_span_ambiguous=bool(discard["span_ambiguous"]),
        right_span_ambiguous=bool(keep["span_ambiguous"]),
    )
    house_near_core = bool(
        classification == "near_identical"
        and discard["source"] == keep["source"] == "house"
        and discard_core
        and keep_core
        and SequenceMatcher(
            None, discard_core, keep_core, autojunk=False
        ).ratio() >= 0.90
    )
    declared_coordinate_relation = classify_dedup_coordinate_relation(
        Path(discard["canonical_path"]).name,
        Path(keep["canonical_path"]).name,
        left_span_ambiguous=bool(discard["span_ambiguous"]),
        right_span_ambiguous=bool(keep["span_ambiguous"]),
    )
    shorter_core = min((discard_core, keep_core), key=len)
    longer_core = max((discard_core, keep_core), key=len)
    queue_near_core = bool(
        classification == "near_identical"
        and discard["source"] == "queue"
        and keep["source"] == "house"
        and declared_coordinate_relation is not None
        and declared_coordinate_relation.mode == "same_coordinates"
        and declared_coordinate_relation.preferred_side is None
        and len(shorter_core) >= 4
        and shorter_core in longer_core
    )
    if not discard_core or (
        discard_core != keep_core
        and not house_near_core
        and not queue_near_core
        and not (
            classification == "ordered_body_match"
            and loose_upgrade_relation is not None
        )
    ):
        raise RuntimeError("ordered body quarantine core title mismatch")
    discard_authors = _author_tokens(discard_meta["author"])
    keep_authors = _author_tokens(keep_meta["author"])
    if (
        not bidirectional_review
        and discard_authors
        and keep_authors
        and not (discard_authors & keep_authors)
    ):
        raise RuntimeError("ordered body quarantine author mismatch")

    coordinate_relation = (
        loose_upgrade_relation
        if classification == "ordered_body_match"
        and discard_core != keep_core
        else declared_coordinate_relation
    )
    if classification == "ordered_body_match":
        if coordinate_relation is None:
            raise RuntimeError("ordered body quarantine coordinate relation changed")
        if coordinate_relation.preferred_side not in {None, "right"}:
            raise RuntimeError("ordered body quarantine would discard wider coverage")
        coordinate_mode = coordinate_relation.mode
    else:
        queue_house = (
            discard["source"] == "queue" and keep["source"] == "house"
        )
        house_distribution = _house_near_distribution_contract(
            discard, keep, discard_meta, keep_meta, coordinate_relation
        )
        if not queue_house and not house_distribution:
            raise RuntimeError("near-identical endpoint contract is unsafe")
        if queue_house:
            if any(
                row["variant_id"] is not None
                or row["protected"]
                or row["representative"]
                or row["assignment_state"] not in {
                    "unassigned", "decision_required",
                }
                or has_legacy_marker(Path(row["canonical_path"]).name)
                for row in (discard, keep)
            ):
                raise RuntimeError("near-identical endpoint relationships are unsafe")
        if coordinate_relation is not None:
            if coordinate_relation.preferred_side is not None:
                raise RuntimeError("near-identical coordinates prefer one edition")
            coordinate_mode = coordinate_relation.mode
        else:
            if not queue_house:
                raise RuntimeError("house near-identical coordinates are required")
            from dedup_episode_relation import episode_profile

            profiles = (
                episode_profile(
                    Path(discard["canonical_path"]).name,
                    span_ambiguous=bool(discard["span_ambiguous"]),
                ),
                episode_profile(
                    Path(keep["canonical_path"]).name,
                    span_ambiguous=bool(keep["span_ambiguous"]),
                ),
            )
            if (profiles[0] is None) == (profiles[1] is None):
                raise RuntimeError("near-identical coordinates are ambiguous")
            coordinate_mode = "one_sided_unknown_coordinates"

    discard_path = _preflight(discard)
    keep_path = _preflight(keep)
    proof = inspect_ordered_text(discard_path, keep_path)
    _assert_row_identity(discard, proof.source_file_evidence, discard_path)
    _assert_row_identity(keep, proof.target_file_evidence, keep_path)
    if (
        not discard["normalized_sha256"]
        or not keep["normalized_sha256"]
        or proof.source_normalized_sha256 != discard["normalized_sha256"]
        or proof.target_normalized_sha256 != keep["normalized_sha256"]
    ):
        raise RuntimeError("ordered body normalized SHA revalidation failed")
    if not ordered_body_coverage_sufficient(proof.coverage):
        raise OrderedBodyMatchNotProven(
            "ordered body coverage fell below the current 1.4.1 contract"
        )
    reverse_proof = None
    if bidirectional_review:
        reverse_proof = inspect_ordered_text(keep_path, discard_path)
        _assert_row_identity(keep, reverse_proof.source_file_evidence, keep_path)
        _assert_row_identity(
            discard, reverse_proof.target_file_evidence, discard_path
        )
        if (
            reverse_proof.source_normalized_sha256
            != keep["normalized_sha256"]
            or reverse_proof.target_normalized_sha256
            != discard["normalized_sha256"]
            or not ordered_body_coverage_sufficient(reverse_proof.coverage)
        ):
            raise OrderedBodyMatchNotProven(
                "near-identical reverse coverage fell below the current contract"
            )
        if discard["source"] == keep["source"] == "house":
            lengths = (
                proof.source_normalized_length,
                proof.target_normalized_length,
            )
            if (
                proof.coverage.coverage_ppm
                < HOUSE_NEAR_DUPLICATE_MIN_COVERAGE_PPM
                or reverse_proof.coverage.coverage_ppm
                < HOUSE_NEAR_DUPLICATE_MIN_COVERAGE_PPM
                or abs(lengths[0] - lengths[1]) / max(lengths) > 0.01
            ):
                raise OrderedBodyMatchNotProven(
                    "house near-identical proof is below the bidirectional 99% contract"
                )

    discard_root = "house_root" if discard["source"] == "house" else "temp_root"
    keep_root = "house_root" if keep["source"] == "house" else "temp_root"
    if discard["source"] == "queue":
        decision_store.assert_manifest_or_same_run_queue_source(
            conn,
            actual_run,
            discard_path,
            proof.source_file_evidence,
            file_id=discard_file_id,
        )
    else:
        decision_store.assert_manifest_source(
            actual_run, discard_path, discard_root, proof.source_file_evidence
        )
    if keep["source"] == "queue":
        decision_store.assert_manifest_or_same_run_queue_source(
            conn,
            actual_run,
            keep_path,
            proof.target_file_evidence,
            file_id=keep_file_id,
        )
    else:
        decision_store.assert_manifest_source(
            actual_run, keep_path, keep_root, proof.target_file_evidence
        )

    ingest_result = None
    if keep["source"] in {"temp", "queue"}:
        if house_destination is None:
            raise RuntimeError("ordered body keep destination is required")
        destination = Path(decision_store.canonicalize_path(house_destination))
        decision_store.assert_actual_run_path(actual_run, destination, "house_root")
        if keep["source"] == "queue":
            ingest_result = user_queue_accept_to_house(
                conn,
                file_id=keep_file_id,
                destination=destination,
                run_id=run_id,
            )
        else:
            ingest_result = ingest_to_house(
                conn,
                source_file_id=keep_file_id,
                destination=destination,
                run_id=run_id,
            )
        keep = _file_state(conn, keep_file_id)
    elif house_destination is not None and decision_store.canonicalize_path(
        house_destination
    ) != keep["canonical_path"]:
        raise RuntimeError("ordered body destination disagrees with keep path")

    discard = _file_state(conn, discard_file_id)
    keep = _file_state(conn, keep_file_id)
    if keep["source"] != "house":
        raise RuntimeError("ordered body keep endpoint is not in house")

    with decision_store.transaction(conn):
        decision_store.mark_actual_run_mutation_started(conn, run_id)
        if discard["source"] == "house" and discard["variant_id"] is not None:
            if discard["assignment_state"] != "managed":
                raise RuntimeError("ordered body discard has unresolved relationships")
            if discard["representative"]:
                if keep["variant_id"] not in {None, discard["variant_id"]}:
                    raise RuntimeError("ordered body quarantine crosses managed variants")
                if keep["representative"] and keep_file_id != discard_file_id:
                    raise RuntimeError("ordered body keep is another representative")
                conn.execute(
                    """
                    UPDATE files SET variant_id = ?, assignment_state = 'managed',
                        assignment_origin = 'strong_match', protected = 1
                    WHERE file_id = ?
                    """,
                    (discard["variant_id"], keep_file_id),
                )
                replaced = conn.execute(
                    """
                    UPDATE representatives SET file_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE variant_id = ? AND file_id = ?
                    """,
                    (keep_file_id, discard["variant_id"], discard_file_id),
                )
                if replaced.rowcount != 1:
                    raise RuntimeError("ordered body representative changed")
                conn.execute(
                    "UPDATE files SET protected = 0 WHERE file_id = ?",
                    (discard_file_id,),
                )
            elif not (
                keep["representative"]
                and keep["variant_id"] == discard["variant_id"]
                and not discard["protected"]
            ):
                raise RuntimeError("ordered body managed relationship is ambiguous")
        elif discard["source"] == "house" and (
            discard["protected"] or discard["representative"]
        ):
            raise RuntimeError("ordered body cannot consume a protected loose file")
        elif discard["source"] == "house" and discard["assignment_state"] not in {
            "unassigned", "managed", "decision_required"
        } and not legacy_marker_discard:
            raise RuntimeError("ordered body discard requires a prior decision")
        elif discard["source"] in {"temp", "queue"} and (
            discard["protected"] or discard["representative"]
        ):
            raise RuntimeError("ordered body temp/queue discard is protected")

    quarantine_result = user_quarantine(
        conn,
        source_file_id=discard_file_id,
        keep_file_id=keep_file_id,
        quarantine_dir=quarantine_dir,
        run_id=run_id,
        reason=(
            "near_identical_house_bidirectional_99_auto_duplicate"
            if classification == "near_identical"
            and discard["source"] == keep["source"] == "house"
            else "bidirectional_95_auto_duplicate"
            if bidirectional_review
            else "ordered_body_95_auto_duplicate"
        ),
        keep_origin_operation_id=(
            ingest_result["operation_id"] if ingest_result is not None else None
        ),
    )
    coverage = proof.coverage
    return {
        "classification": classification,
        "discard_file_id": discard_file_id,
        "keep_file_id": keep_file_id,
        "source_normalized_sha256": proof.source_normalized_sha256,
        "target_normalized_sha256": proof.target_normalized_sha256,
        "source_normalized_length": proof.source_normalized_length,
        "target_normalized_length": proof.target_normalized_length,
        "coverage_ppm": coverage.coverage_ppm,
        "matched_chars": coverage.matched_chars,
        "source_chars": coverage.source_chars,
        "max_unmatched_chars": coverage.max_unmatched_chars,
        "coordinate_mode": coordinate_mode,
        "reverse_coverage_ppm": (
            reverse_proof.coverage.coverage_ppm
            if reverse_proof is not None else None
        ),
        "reverse_max_unmatched_chars": (
            reverse_proof.coverage.max_unmatched_chars
            if reverse_proof is not None else None
        ),
        "ingest_operation_id": (
            ingest_result["operation_id"] if ingest_result is not None else None
        ),
        "ingested_path": (
            ingest_result["dest_path"] if ingest_result is not None
            else keep["canonical_path"]
        ),
        "quarantine_operation_id": quarantine_result["operation_id"],
        "quarantine_path": quarantine_result["dest_path"],
    }


def user_action_quarantine(
    conn, *, source_file_id, quarantine_dir, run_id,
    reason="review_action_delete",
):
    """Quarantine an explicitly submitted action-inbox file without a keep pair."""
    with mutation_lock(conn, f"user_action_quarantine:{run_id}", run_id=run_id):
        actual_run = decision_store.assert_active_actual_run(conn, run_id)
        source = _ensure_intake_fingerprint(conn, _file_state(conn, source_file_id))
        if source["source"] not in {"temp", "queue"}:
            raise RuntimeError("action discard source must be under temp")
        if source["protected"] or source["representative"]:
            raise RuntimeError("protected/representative file cannot be discarded")
        source_path = _preflight(source)
        decision_store.assert_actual_run_path(actual_run, source_path, "temp_root")
        decision_store.assert_actual_run_path(actual_run, quarantine_dir, "temp_root")
        source_evidence = inspect_regular_file(source_path)
        decision_store.assert_manifest_source(
            actual_run, source_path, "temp_root", source_evidence
        )
        destination = _unique_destination(conn, quarantine_dir, source_path.name)
        with decision_store.transaction(conn):
            operation_id = decision_store.create_operation(
                conn, run_id=run_id, action="user_quarantine",
                source_path=str(source_path), quarantine_path=str(destination),
                file_id=source_file_id, expected_size=source["size"],
                expected_mtime_ns=source["mtime_ns"],
                expected_fingerprint_id=source["current_fingerprint_id"],
                source_dev=source_evidence.dev, source_ino=source_evidence.ino,
                source_ctime_ns=source_evidence.ctime_ns,
                source_sha256=source_evidence.sha256,
            )

        def guard():
            current = _file_state(conn, source_file_id)
            if current["current_fingerprint_id"] != source["current_fingerprint_id"]:
                raise RuntimeError("action discard fingerprint changed")

        destination_evidence = _copy_record_consume(
            conn, operation_id, source_path, destination, source_evidence, guard=guard
        )
        with decision_store.transaction(conn):
            conn.execute(
                """UPDATE files SET canonical_path = ?, source = 'quarantine',
                    active = 0, protected = 0, dev = ?, ino = ?, ctime_ns = ?,
                    size = ?, mtime_ns = ? WHERE file_id = ?""",
                (str(destination), destination_evidence.dev, destination_evidence.ino,
                 destination_evidence.ctime_ns, destination_evidence.size,
                 destination_evidence.mtime_ns, source_file_id),
            )
            decision_store.supersede_open_reviews_for_file(
                conn, source_file_id, reason=reason
            )
            decision_store.transition_operation(conn, operation_id, "db_done")
        with decision_store.transaction(conn):
            decision_store.transition_operation(conn, operation_id, "committed")
        return {
            "operation_id": operation_id, "action": "user_quarantine",
            "source_file_id": source_file_id, "keep_file_id": None,
            "dest_path": str(destination),
        }


def queue_candidate(
    conn,
    *,
    candidate_file_id,
    reference_file_id,
    classification,
    queue_dir,
    run_id,
    review_id=None,
    allow_unassigned_reference=False,
    exact_sha256=None,
):
    with mutation_lock(conn, f"queue_candidate:{run_id}", run_id=run_id):
        return _queue_candidate(
            conn,
            candidate_file_id=candidate_file_id,
            reference_file_id=reference_file_id,
            classification=classification,
            queue_dir=queue_dir,
            run_id=run_id,
            review_id=review_id,
            allow_unassigned_reference=allow_unassigned_reference,
            exact_sha256=exact_sha256,
        )


def _queue_candidate(
    conn,
    *,
    candidate_file_id,
    reference_file_id,
    classification,
    queue_dir,
    run_id,
    review_id=None,
    allow_unassigned_reference=False,
    exact_sha256=None,
):
    actual_run = decision_store.assert_active_actual_run(conn, run_id)
    allowed_classes = (
        HUMAN_REVIEW_CLASSES if allow_unassigned_reference
        else STRONG_QUEUE_CLASSES | WEAK_QUEUE_CLASSES
    )
    if classification not in allowed_classes:
        raise ValueError(f"classification is not queueable: {classification}")
    if review_id is None:
        raise RuntimeError("persisted review evidence is required for queue mutation")
    candidate = _file_state(conn, candidate_file_id)
    reference = _file_state(conn, reference_file_id)
    decision_store.assert_actual_run_path(
        actual_run, candidate["canonical_path"], "temp_root"
    )
    reference_root = "house_root" if reference["source"] == "house" else "temp_root"
    decision_store.assert_actual_run_path(
        actual_run, reference["canonical_path"], reference_root
    )
    decision_store.assert_actual_run_path(actual_run, queue_dir, "temp_root")
    _ensure_mutable_source(candidate)
    candidate_path = _preflight(candidate)
    _preflight(reference)
    if candidate["source"] != "temp":
        raise RuntimeError("only a new temp candidate may enter an automatic queue")
    managed_reference = (
        reference["source"] == "house"
        and reference["assignment_state"] == "managed"
        and reference["representative"]
    )
    if not managed_reference and not allow_unassigned_reference:
        raise RuntimeError("queue reference must be a managed representative")
    if classification not in STRONG_QUEUE_CLASSES and not decision_store.coordinates_compatible(candidate, reference):
        raise RuntimeError("queue pair has incompatible canonical coordinates")

    review = conn.execute(
        """
        SELECT candidate_file_id, reference_file_id, left_fingerprint_id,
               right_fingerprint_id, classification, state, evidence_json
        FROM review_items WHERE review_id = ?
        """,
        (review_id,),
    ).fetchone()
    if review is None or review["state"] not in {"pending", "deferred"}:
        raise RuntimeError("queue review is missing or closed")
    expected = (
        candidate_file_id, reference_file_id,
        candidate["current_fingerprint_id"], reference["current_fingerprint_id"],
        classification,
    )
    actual = (
        review["candidate_file_id"], review["reference_file_id"],
        review["left_fingerprint_id"], review["right_fingerprint_id"],
        review["classification"],
    )
    if actual != expected:
        raise RuntimeError("queue review evidence does not match current pair")

    strong = classification in STRONG_QUEUE_CLASSES
    source_evidence = None
    if exact_sha256 is not None:
        candidate_evidence = inspect_regular_file(candidate_path)
        reference_evidence = inspect_regular_file(reference["canonical_path"])
        if (
            candidate_evidence.sha256 != exact_sha256
            or reference_evidence.sha256 != exact_sha256
        ):
            raise RuntimeError("exact review current raw SHA-256 revalidation failed")
        source_evidence = candidate_evidence
    elif strong:
        if classification in EPUB_EQUAL_CLASSES:
            candidate_evidence, reference_evidence = _revalidate_epub_equivalent(
                candidate,
                reference,
                candidate_path,
                reference["canonical_path"],
                review,
            )
            source_evidence = candidate_evidence
        else:
            candidate_evidence, candidate_normalized = inspect_normalized_text(candidate_path)
            reference_evidence, reference_normalized = inspect_normalized_text(
                reference["canonical_path"]
            )
            if (
                not candidate["normalized_sha256"]
                or candidate["normalized_sha256"] != reference["normalized_sha256"]
                or candidate_normalized != reference_normalized
                or candidate_normalized != candidate["normalized_sha256"]
                or reference_normalized != reference["normalized_sha256"]
            ):
                raise RuntimeError("strong queue current normalized SHA-256 revalidation failed")
            source_evidence = candidate_evidence
    action = "suspected_move" if strong else "warning_move"
    destination = _unique_destination(conn, queue_dir, candidate_path.name)
    source_evidence = source_evidence or inspect_regular_file(candidate_path)
    decision_store.assert_manifest_source(
        actual_run, candidate_path, "temp_root", source_evidence
    )
    with decision_store.transaction(conn):
        operation_id = decision_store.create_operation(
            conn,
            run_id=run_id,
            action=action,
            source_path=str(candidate_path),
            dest_path=str(destination),
            file_id=candidate_file_id,
            keep_file_id=reference_file_id,
            expected_size=candidate["size"],
            expected_mtime_ns=candidate["mtime_ns"],
            expected_fingerprint_id=candidate["current_fingerprint_id"],
            expected_keep_fingerprint_id=reference["current_fingerprint_id"],
            source_dev=source_evidence.dev,
            source_ino=source_evidence.ino,
            source_ctime_ns=source_evidence.ctime_ns,
            source_sha256=source_evidence.sha256,
        )
    reference_guard_evidence = (
        reference_evidence if strong else inspect_regular_file(reference["canonical_path"])
    )

    def queue_guard():
        decision_store.assert_active_actual_run(conn, run_id)
        current_candidate = _file_state(conn, candidate_file_id)
        current_reference = _file_state(conn, reference_file_id)
        if current_candidate["current_fingerprint_id"] != candidate["current_fingerprint_id"]:
            raise RuntimeError("queue candidate fingerprint changed before consume")
        if current_reference["current_fingerprint_id"] != reference["current_fingerprint_id"]:
            raise RuntimeError("queue reference fingerprint changed before consume")
        if managed_reference and (
            not current_reference["representative"]
            or current_reference["assignment_state"] != "managed"
        ):
            raise RuntimeError("queue representative guard changed before consume")
        if not evidence_matches(
            inspect_regular_file(reference["canonical_path"]), reference_guard_evidence
        ):
            raise RuntimeError("queue representative identity changed before consume")

    destination_evidence = _copy_record_consume(
        conn, operation_id, candidate_path, destination, source_evidence, guard=queue_guard
    )

    with decision_store.transaction(conn):
        if strong and managed_reference:
            conn.execute(
                """
                UPDATE files
                SET canonical_path = ?, source = 'queue', variant_id = ?,
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?,
                    assignment_state = 'managed', assignment_origin = 'strong_match'
                WHERE file_id = ?
                """,
                (
                    str(destination), reference["variant_id"], destination_evidence.dev,
                    destination_evidence.ino, destination_evidence.ctime_ns,
                    destination_evidence.size, destination_evidence.mtime_ns,
                    candidate_file_id,
                ),
            )
        else:
            conn.execute(
                """UPDATE files SET canonical_path = ?, source = 'queue',
                    dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?
                    WHERE file_id = ?""",
                (
                    str(destination), destination_evidence.dev, destination_evidence.ino,
                    destination_evidence.ctime_ns, destination_evidence.size,
                    destination_evidence.mtime_ns, candidate_file_id,
                ),
            )
        if review_id is not None:
            conn.execute(
                "UPDATE review_items SET queue_path = ?, updated_at = CURRENT_TIMESTAMP WHERE review_id = ?",
                (str(destination), review_id),
            )
        decision_store.transition_operation(conn, operation_id, "db_done")
    with decision_store.transaction(conn):
        decision_store.transition_operation(conn, operation_id, "committed")
    return {
        "operation_id": operation_id,
        "action": action,
        "candidate_file_id": candidate_file_id,
        "reference_file_id": reference_file_id,
        "classification": classification,
        "dest_path": str(destination),
    }
