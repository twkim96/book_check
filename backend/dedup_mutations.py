"""Journaled filesystem mutations for managed dedup mode."""

from pathlib import Path

import decision_store
from mutation_io import (
    evidence_matches,
    ensure_directory_nofollow,
    inspect_epub_content,
    inspect_regular_file,
    inspect_normalized_text,
    mutation_lock,
)


STRONG_QUEUE_CLASSES = frozenset({"text_equivalent", "epub_equivalent"})
NORMALIZED_EQUAL_CLASSES = frozenset({"text_equivalent", "marker_recheck"})
EPUB_EQUAL_CLASSES = frozenset({"epub_equivalent"})
WEAK_QUEUE_CLASSES = frozenset({
    "near_identical",
    "contained_exact",
    "contained_version",
})
REPORT_ONLY_CLASSES = frozenset({
    "longer_unresolved", "decode_lossy", "metadata_only", "insufficient_text",
})
HUMAN_REVIEW_CLASSES = (
    NORMALIZED_EQUAL_CLASSES | EPUB_EQUAL_CLASSES | WEAK_QUEUE_CLASSES | REPORT_ONLY_CLASSES
    | frozenset({"exact_bytes"})
)


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
        decision_store.assert_manifest_source(
            actual_run, source_path, "temp_root", source_evidence
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


def user_queue_accept_to_house(conn, *, file_id, destination, run_id):
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
        decision_store.assert_manifest_source(
            actual_run, source_path, "temp_root", source_evidence
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
            move_epub = inspect_epub_content(move_path)
            keep_epub = inspect_epub_content(keep_path)
            move_evidence = move_epub.file_evidence
            keep_evidence = keep_epub.file_evidence
            if (
                not move["normalized_sha256"]
                or move["normalized_sha256"] != keep["normalized_sha256"]
                or move_epub.content_sha256 != keep_epub.content_sha256
                or move_epub.content_sha256 != move["normalized_sha256"]
                or keep_epub.content_sha256 != keep["normalized_sha256"]
            ):
                raise RuntimeError("house cleanup EPUB SHA revalidation failed")
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
        destination = _unique_destination(queue_dir, move_path.name)
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


def ingest_to_house(conn, *, source_file_id, destination, run_id, routing=None):
    with mutation_lock(conn, f"house_ingest:{run_id}", run_id=run_id):
        return _ingest_to_house(
            conn,
            source_file_id=source_file_id,
            destination=destination,
            run_id=run_id,
            routing=routing,
        )


def _ingest_to_house(conn, *, source_file_id, destination, run_id, routing=None):
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
    source_evidence = inspect_regular_file(source_path)
    decision_store.assert_manifest_source(
        actual_run, source_path, "temp_root", source_evidence
    )
    routing_result = None
    with decision_store.transaction(conn):
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


def _unique_destination(directory, filename):
    directory = Path(directory)
    ensure_directory_nofollow(directory)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
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


def _ensure_mutable_source(row):
    if row["protected"] or row["representative"]:
        raise RuntimeError("protected/representative file cannot be a mutation source")
    if row["source"] == "house" and row["assignment_state"] != "managed":
        raise RuntimeError("unassigned house file cannot be mutated")
    if row["assignment_state"] in {"legacy_unresolved", "decision_required"}:
        raise RuntimeError(f"assignment state blocks mutation: {row['assignment_state']}")


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
    source_root = "temp_root" if source["source"] == "temp" else "house_root"
    decision_store.assert_actual_run_path(actual_run, source["canonical_path"], source_root)
    decision_store.assert_actual_run_path(actual_run, keep["canonical_path"], "house_root")
    decision_store.assert_actual_run_path(actual_run, quarantine_dir, "temp_root")
    _ensure_mutable_source(source)
    source_path = _preflight(source)
    keep_path = _preflight(keep)
    if source_file_id == keep_file_id:
        raise ValueError("source and keep must differ")
    if keep["assignment_state"] != "managed":
        raise RuntimeError("exact keep must be managed")
    if source["assignment_state"] == "managed" and source["variant_id"] != keep["variant_id"]:
        raise RuntimeError("managed exact files belong to different variants")
    if not decision_store.coordinates_compatible(source, keep):
        raise RuntimeError("exact files have incompatible canonical coordinates")

    # Recompute both raw hashes immediately before the move. Cached hashes are
    # also checked so stale/corrupt cache cannot authorize a logical deletion.
    source_evidence = inspect_regular_file(source_path)
    keep_evidence = inspect_regular_file(keep_path)
    decision_store.assert_manifest_source(
        actual_run, source_path, source_root, source_evidence
    )
    source_hash = source_evidence.sha256
    keep_hash = keep_evidence.sha256
    if source_hash != keep_hash:
        raise RuntimeError("exact raw SHA revalidation failed")
    if source["raw_sha256"] != source_hash or keep["raw_sha256"] != keep_hash:
        raise RuntimeError("cached raw SHA does not match current bytes")

    destination = _unique_destination(quarantine_dir, source_path.name)
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
        if (
            current_keep["current_fingerprint_id"] != keep["current_fingerprint_id"]
            or not current_keep["representative"]
            or current_keep["assignment_state"] != "managed"
        ):
            raise RuntimeError("exact keep guard changed before consume")
        if not evidence_matches(inspect_regular_file(keep_path), keep_evidence):
            raise RuntimeError("exact keep identity changed before consume")

    destination_evidence = _copy_record_consume(
        conn, operation_id, source_path, destination, source_evidence, guard=exact_guard
    )

    with decision_store.transaction(conn):
        variant_id = source["variant_id"] or keep["variant_id"]
        assignment_origin = source["assignment_origin"] or "strong_match"
        conn.execute(
            """
            UPDATE files
            SET canonical_path = ?, source = 'quarantine', active = 0,
                dev = ?, ino = ?, ctime_ns = ?, size = ?, mtime_ns = ?,
                variant_id = ?, assignment_state = 'managed', assignment_origin = ?
            WHERE file_id = ?
            """,
            (
                str(destination), destination_evidence.dev, destination_evidence.ino,
                destination_evidence.ctime_ns, destination_evidence.size,
                destination_evidence.mtime_ns, variant_id, assignment_origin, source_file_id,
            ),
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
        decision_store.assert_manifest_source(
            actual_run, source_path, source_root, source_evidence
        )
        if keep_path is not None:
            decision_store.assert_manifest_source(
                actual_run, keep_path, "house_root", keep_evidence
            )
        if replacement_path is not None and replacement_path != keep_path:
            decision_store.assert_manifest_source(
                actual_run, replacement_path, "house_root", replacement_evidence
            )
        destination = _unique_destination(quarantine_dir, source_path.name)
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
        destination = _unique_destination(quarantine_dir, source_path.name)
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
    if not decision_store.coordinates_compatible(candidate, reference):
        raise RuntimeError("queue pair has incompatible canonical coordinates")

    review = conn.execute(
        """
        SELECT candidate_file_id, reference_file_id, left_fingerprint_id,
               right_fingerprint_id, classification, state
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
            candidate_epub = inspect_epub_content(candidate_path)
            reference_epub = inspect_epub_content(reference["canonical_path"])
            candidate_evidence = candidate_epub.file_evidence
            reference_evidence = reference_epub.file_evidence
            if (
                not candidate["normalized_sha256"]
                or candidate["normalized_sha256"] != reference["normalized_sha256"]
                or candidate_epub.content_sha256 != reference_epub.content_sha256
                or candidate_epub.content_sha256 != candidate["normalized_sha256"]
                or reference_epub.content_sha256 != reference["normalized_sha256"]
            ):
                raise RuntimeError("strong queue EPUB SHA-256 revalidation failed")
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
    action = "suspected_move" if strong and managed_reference else "warning_move"
    destination = _unique_destination(queue_dir, candidate_path.name)
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
