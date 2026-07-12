"""Folderling-integrated user action inboxes.

The user moves a managed queue file or an ordinary file into one of these folders:

* ``txt_temp/review_actions/house``: keep/accept it in house
* ``txt_temp/review_actions/delete``: quarantine it as user-approved discard

Tracked renames are claimed by strong evidence and new files receive stable IDs
before doctor runs; the disposition then executes under Folderling's one-time run.
"""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

import decision_store
from dedup_mutations import (
    ingest_to_house,
    user_action_quarantine,
    user_quarantine,
    user_queue_accept_to_house,
)
from mutation_io import ensure_directory_nofollow, inspect_regular_file
from normalizer import (
    get_chosung,
    normalize_filename,
    should_exclude_file,
    strip_trash_suffix,
)


ACTION_ROOT_NAME = "review_actions"
HOUSE_ACTION_NAME = "house"
DELETE_ACTION_NAME = "delete"
ORIGIN_ACTIONS = ("suspected_move", "warning_move", "house_review_move")


@dataclass
class ActionClaim:
    disposition: str
    path: Path
    evidence: object
    row: object
    queue_origin: bool
    already_tracked: bool = False


def action_directories(temp_dir):
    root = Path(temp_dir) / ACTION_ROOT_NAME
    return {
        "house": root / HOUSE_ACTION_NAME,
        "delete": root / DELETE_ACTION_NAME,
    }


def ensure_action_directories(temp_dir):
    directories = action_directories(temp_dir)
    ensure_directory_nofollow(Path(temp_dir) / ACTION_ROOT_NAME)
    for path in directories.values():
        ensure_directory_nofollow(path)
    return directories


def has_action_files(temp_dir):
    """Cheaply report whether either inbox has anything to process."""
    for directory in action_directories(temp_dir).values():
        if directory.is_dir() and any(
            not should_exclude_file(path.name) for path in directory.iterdir()
        ):
            return True
    return False


def _action_files(temp_dir):
    directories = ensure_action_directories(temp_dir)
    found = []
    for disposition, directory in directories.items():
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            # Finder metadata is not a human disposition.  Keep it untouched,
            # but exclude it through the same hidden-file rule as normal intake.
            if should_exclude_file(path.name):
                continue
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(
                    f"review action folder accepts regular files only: {path}"
                )
            found.append((disposition, path, inspect_regular_file(path)))
    return found


def _queue_origins(conn):
    placeholders = ",".join("?" for _ in ORIGIN_ACTIONS)
    return conn.execute(
        f"""
        SELECT f.*, fp.raw_sha256 AS fingerprint_raw_sha256,
               op.operation_id AS origin_operation_id,
               op.action AS origin_action, op.source_path AS origin_source_path,
               op.keep_file_id AS origin_keep_file_id,
               op.destination_dev, op.destination_ino, op.destination_size,
               op.destination_sha256
        FROM files AS f
        JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
        JOIN operations AS op ON op.operation_id = (
            SELECT latest.operation_id FROM operations AS latest
            WHERE latest.file_id = f.file_id
              AND latest.action IN ({placeholders})
              AND latest.state = 'committed'
            ORDER BY latest.operation_id DESC LIMIT 1
        )
        WHERE f.active = 1 AND f.source = 'queue'
        """,
        ORIGIN_ACTIONS,
    ).fetchall()


def _match_action_file(rows, path, evidence, already_claimed):
    inode_matches = [
        row for row in rows
        if row["file_id"] not in already_claimed
        and not Path(row["canonical_path"]).exists()
        and row["destination_dev"] == evidence.dev
        and row["destination_ino"] == evidence.ino
    ]
    if len(inode_matches) == 1:
        return inode_matches[0]
    if len(inode_matches) > 1:
        raise RuntimeError(f"ambiguous review action inode identity: {path}")

    candidates = []
    for row in rows:
        if row["file_id"] in already_claimed:
            continue
        old_path = Path(row["canonical_path"])
        if old_path.exists():
            continue  # copied, rather than moved: fail closed
        if (
            row["destination_size"] != evidence.size
            or row["destination_sha256"] != evidence.sha256
            or row["fingerprint_raw_sha256"] != evidence.sha256
        ):
            continue
        candidates.append(row)

    same_name = [
        row for row in candidates
        if unicodedata.normalize("NFC", Path(row["canonical_path"]).name)
        == unicodedata.normalize("NFC", path.name)
    ]
    if len(same_name) == 1:
        return same_name[0]
    if len(candidates) == 1:
        return candidates[0]
    raise RuntimeError(
        f"review action file must match exactly one missing managed queue file: "
        f"{path} (matches={len(candidates)})"
    )


def _generic_missing_match(conn, path, evidence, already_claimed):
    rows = conn.execute(
        """
        SELECT f.*, fp.raw_sha256 AS fingerprint_raw_sha256,
               CASE WHEN rep.file_id IS NULL THEN 0 ELSE 1 END AS representative
        FROM files AS f
        JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
        LEFT JOIN representatives AS rep ON rep.file_id = f.file_id
        WHERE f.active = 1 AND f.size = ? AND fp.raw_sha256 = ?
        """,
        (evidence.size, evidence.sha256),
    ).fetchall()
    existing = [row for row in rows if Path(row["canonical_path"]).exists()]
    if existing:
        raise RuntimeError(
            f"review action appears copied from an active tracked file: {path}"
        )
    missing = [row for row in rows if row["file_id"] not in already_claimed]
    if len(missing) > 1:
        raise RuntimeError(f"ambiguous missing file for review action: {path}")
    if not missing:
        return None
    row = missing[0]
    if row["protected"] or row["representative"]:
        raise RuntimeError(f"protected/representative action source refused: {path}")
    return row


def claim_external_action_moves(conn, temp_dir):
    """Bind action files to stable IDs, or register genuinely new inputs.

    The operation is idempotent: a prior run may have completed this DB claim
    and then stopped at doctor/activation without moving the action file.
    """
    action_files = _action_files(temp_dir)
    if not action_files:
        return []
    rows = _queue_origins(conn)
    claims = []
    claimed_ids = set()
    claimed_paths = set()
    for disposition, path, evidence in action_files:
        canonical = decision_store.canonicalize_path(path)
        if canonical in claimed_paths:
            raise RuntimeError(f"duplicate review action path: {path}")
        existing = conn.execute(
            """
            SELECT f.*, fp.raw_sha256 AS fingerprint_raw_sha256,
                   CASE WHEN rep.file_id IS NULL THEN 0 ELSE 1 END AS representative
            FROM files AS f
            JOIN fingerprints AS fp ON fp.fingerprint_id = f.current_fingerprint_id
            LEFT JOIN representatives AS rep ON rep.file_id = f.file_id
            WHERE f.canonical_path = ? AND f.active = 1
            """,
            (canonical,),
        ).fetchone()
        if existing is not None:
            expected = (
                existing["dev"], existing["ino"], existing["ctime_ns"],
                existing["size"], existing["mtime_ns"],
                existing["fingerprint_raw_sha256"],
            )
            actual = (
                evidence.dev, evidence.ino, evidence.ctime_ns,
                evidence.size, evidence.mtime_ns, evidence.sha256,
            )
            if existing["source"] not in {"queue", "temp"} or expected != actual:
                raise RuntimeError(f"tracked review action snapshot is stale: {path}")
            if existing["protected"] or existing["representative"]:
                raise RuntimeError(f"protected/representative action source refused: {path}")
            row = existing
            queue_origin = row["source"] == "queue"
            already_tracked = True
        else:
            try:
                row = _match_action_file(rows, path, evidence, claimed_ids)
                queue_origin = True
            except RuntimeError as exc:
                if "matches=0" not in str(exc):
                    raise
                row = _generic_missing_match(conn, path, evidence, claimed_ids)
                queue_origin = False
            already_tracked = False
        claims.append(ActionClaim(
            disposition, path, evidence, row, queue_origin, already_tracked
        ))
        if row is not None:
            claimed_ids.add(row["file_id"])
        claimed_paths.add(canonical)

    with decision_store.transaction(conn):
        for claim in claims:
            disposition, path, evidence = (
                claim.disposition, claim.path, claim.evidence
            )
            row, queue_origin = claim.row, claim.queue_origin
            canonical = decision_store.canonicalize_path(path)
            if claim.already_tracked:
                continue
            if row is None:
                row = decision_store.reconcile_file_metadata(
                    conn, path, source="temp"
                )
                from normalizer import NORMALIZER_VERSION
                fingerprint_id = conn.execute(
                    """
                    INSERT INTO fingerprints(
                        file_id, canonical_path, size, mtime_ns, dev, ino, ctime_ns,
                        normalizer_version, fingerprint_version, raw_sha256, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'review-action-raw-v1', ?, 'raw_only')
                    """,
                    (row["file_id"], canonical, evidence.size, evidence.mtime_ns,
                     evidence.dev, evidence.ino, evidence.ctime_ns,
                     NORMALIZER_VERSION, evidence.sha256),
                ).lastrowid
                origin_path = canonical
            elif row["fingerprint_raw_sha256"] == evidence.sha256:
                fingerprint_id = conn.execute(
                    """
                    INSERT INTO fingerprints(
                        file_id, canonical_path, size, mtime_ns, dev, ino, ctime_ns,
                        normalizer_version, fingerprint_version, analysis_policy_hash,
                        raw_sha256, normalized_sha256, normalized_length, encoding,
                        status, front_anchor, tail_anchor, anchors_json
                    )
                    SELECT file_id, ?, ?, ?, ?, ?, ?, normalizer_version,
                           fingerprint_version || ?, analysis_policy_hash,
                           raw_sha256, normalized_sha256, normalized_length, encoding,
                           status, front_anchor, tail_anchor, anchors_json
                    FROM fingerprints WHERE fingerprint_id = ? AND file_id = ?
                    """,
                    (
                        canonical, evidence.size, evidence.mtime_ns, evidence.dev,
                        evidence.ino, evidence.ctime_ns,
                        f":review-action:{uuid.uuid4().hex}",
                        row["current_fingerprint_id"], row["file_id"],
                    ),
                ).lastrowid
                origin_path = row["canonical_path"]
            else:
                from normalizer import NORMALIZER_VERSION
                fingerprint_id = conn.execute(
                    """
                    INSERT INTO fingerprints(
                        file_id, canonical_path, size, mtime_ns, dev, ino, ctime_ns,
                        normalizer_version, fingerprint_version, raw_sha256, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'review-action-rebaseline-v1', ?, 'raw_only')
                    """,
                    (row["file_id"], canonical, evidence.size, evidence.mtime_ns,
                     evidence.dev, evidence.ino, evidence.ctime_ns,
                     NORMALIZER_VERSION, evidence.sha256),
                ).lastrowid
                origin_path = row["canonical_path"]
            if not fingerprint_id:
                raise RuntimeError(f"review action fingerprint clone failed: {path}")
            source = "queue" if queue_origin else "temp"
            updated = conn.execute(
                """
                UPDATE files SET canonical_path = ?, source = ?,
                    dev = ?, ino = ?, ctime_ns = ?,
                    size = ?, mtime_ns = ?, current_fingerprint_id = ?,
                    last_seen_at = CURRENT_TIMESTAMP
                WHERE file_id = ? AND active = 1
                """,
                (
                    canonical, source, evidence.dev, evidence.ino, evidence.ctime_ns,
                    evidence.size, evidence.mtime_ns, fingerprint_id, row["file_id"],
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"review action stable file update lost: {path}")
            conn.execute(
                """
                UPDATE review_items SET queue_path = ?, updated_at = CURRENT_TIMESTAMP
                WHERE (candidate_file_id = ? OR reference_file_id = ?)
                  AND queue_path = ?
                """,
                (canonical, row["file_id"], row["file_id"], origin_path),
            )
            claim.row = conn.execute(
                "SELECT * FROM files WHERE file_id = ?", (row["file_id"],)
            ).fetchone()
    return [
        {
            "disposition": claim.disposition,
            "file_id": claim.row["file_id"],
            "path": decision_store.canonicalize_path(claim.path),
            "origin_action": (
                _latest_origin(conn, claim.row["file_id"])["action"]
                if claim.queue_origin else None
            ),
            "origin_source_path": (
                _latest_origin(conn, claim.row["file_id"])["source_path"]
                if claim.queue_origin else None
            ),
            "already_tracked": claim.already_tracked,
        }
        for claim in claims
    ]


def _unique_house_destination(house_dir, filename, reserved):
    clean_name = normalize_filename(strip_trash_suffix(filename))
    if not clean_name:
        raise RuntimeError(f"review action filename normalizes to empty: {filename}")
    first = clean_name[0]
    directory = Path(house_dir) / get_chosung(first)
    ensure_directory_nofollow(directory)
    base = directory / clean_name
    if not base.exists() and str(base) not in reserved:
        return base
    stem, suffix = base.stem, base.suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_dup_{counter}{suffix}"
        if not candidate.exists() and str(candidate) not in reserved:
            return candidate
        counter += 1


def _latest_origin(conn, file_id):
    placeholders = ",".join("?" for _ in ORIGIN_ACTIONS)
    row = conn.execute(
        f"""
        SELECT * FROM operations
        WHERE file_id = ? AND action IN ({placeholders}) AND state = 'committed'
        ORDER BY operation_id DESC LIMIT 1
        """,
        (file_id, *ORIGIN_ACTIONS),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"review action queue origin missing: {file_id}")
    return row


def _root_keep(conn, file_id, deleting, future_house=frozenset()):
    current = file_id
    seen = set()
    while current in deleting:
        if current in seen:
            raise RuntimeError(f"review delete keep cycle: {file_id}")
        seen.add(current)
        origin = _latest_origin(conn, current)
        if not origin["keep_file_id"]:
            raise RuntimeError(f"review delete keep is missing: {current}")
        current = origin["keep_file_id"]
    keep = conn.execute(
        "SELECT file_id, source, active FROM files WHERE file_id = ?", (current,)
    ).fetchone()
    if (
        keep is None or not keep["active"]
        or (keep["source"] != "house" and current not in future_house)
    ):
        raise RuntimeError(f"review delete keep is not active in house: {current}")
    return current


def process_claimed_actions(conn, *, temp_dir, house_dir, run_id):
    """Execute claimed action inbox files before the normal dedup scan."""
    directories = action_directories(temp_dir)
    rows = conn.execute(
        """
        SELECT file_id, canonical_path, source FROM files
        WHERE active = 1 AND source IN ('queue', 'temp')
        """
    ).fetchall()
    house_root = decision_store.canonicalize_path(directories["house"])
    delete_root = decision_store.canonicalize_path(directories["delete"])
    house_rows = []
    delete_rows = []
    for row in rows:
        parent = decision_store.canonicalize_path(Path(row["canonical_path"]).parent)
        if parent == house_root:
            house_rows.append(row)
        elif parent == delete_root:
            delete_rows.append(row)

    accepted = []
    discarded = []
    reserved = set()
    house_plans = []
    from folderling import ensure_recent_link_slot
    recent_dir = Path(house_dir) / "_최근"
    for row in sorted(house_rows, key=lambda item: item["canonical_path"]):
        origin = _latest_origin(conn, row["file_id"]) if row["source"] == "queue" else None
        if origin is not None and origin["action"] == "house_review_move":
            destination = Path(origin["source_path"])
            if destination.exists() or str(destination) in reserved:
                raise RuntimeError(f"review house restore destination occupied: {destination}")
        else:
            destination = _unique_house_destination(
                house_dir,
                Path(origin["source_path"]).name if origin is not None
                else Path(row["canonical_path"]).name,
                reserved,
            )
        reserved.add(str(destination))
        needs_recent = origin is None or origin["action"] != "house_review_move"
        if needs_recent:
            ensure_recent_link_slot(destination.name, recent_dir)
        house_plans.append((row, destination, needs_recent))

    deleting = {row["file_id"] for row in delete_rows}
    future_house = {row["file_id"] for row in house_rows}
    delete_plans = []
    for row in sorted(delete_rows, key=lambda item: item["canonical_path"]):
        origin = _latest_origin(conn, row["file_id"]) if row["source"] == "queue" else None
        keep_id = None
        if origin is not None and origin["keep_file_id"]:
            keep_id = _root_keep(
                conn, origin["keep_file_id"], deleting, future_house
            )
        delete_plans.append((row, keep_id))

    from folderling import create_recent_link
    for row, destination, needs_recent in house_plans:
        if row["source"] == "queue":
            result = user_queue_accept_to_house(
                conn, file_id=row["file_id"], destination=destination, run_id=run_id
            )
        else:
            result = ingest_to_house(
                conn, source_file_id=row["file_id"],
                destination=destination, run_id=run_id
            )
        decision_store.record_human_restore_disposition(conn, row["file_id"])
        if needs_recent:
            create_recent_link(destination, destination.name, recent_dir)
        accepted.append(result)

    quarantine_dir = Path(temp_dir) / "trash_bin" / "user_discard_quarantine"
    for row, keep_id in delete_plans:
        if keep_id is not None:
            discarded.append(user_quarantine(
                conn,
                source_file_id=row["file_id"], keep_file_id=keep_id,
                quarantine_dir=quarantine_dir, run_id=run_id,
                reason="review_action_delete",
            ))
        else:
            discarded.append(user_action_quarantine(
                conn, source_file_id=row["file_id"],
                quarantine_dir=quarantine_dir, run_id=run_id,
            ))
    return {"accepted": accepted, "discarded": discarded}
