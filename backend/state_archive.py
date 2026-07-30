#!/usr/bin/env python3
"""Verified cold archive lifecycle for unreferenced state DB backups."""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mutation_io import (
    SourceIdentityChanged,
    ensure_directory_nofollow,
    inspect_regular_file,
    read_json_with_evidence,
    unlink_owned,
)
from project_paths import STATE_DB


ARCHIVE_VERSION = "1.4.9"
ARCHIVE_SCHEMA_VERSION = 1
DEFAULT_KEEP_LATEST_UNREFERENCED = 2
CHUNK_SIZE = 1024 * 1024


def _canonical(path) -> str:
    return str(Path(path).expanduser().resolve())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_payload(payload) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json_write(path: Path, payload) -> None:
    ensure_directory_nofollow(path.parent)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            raise FileExistsError(path)
        os.link(temporary, path)
        os.unlink(temporary)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _archive_lock(state_dir: Path):
    ensure_directory_nofollow(state_dir)
    lock_path = state_dir / ".state_archive.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _connect_readonly(state_db: Path):
    uri = f"file:{state_db.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _referenced_backup_paths(connection) -> set[str]:
    referenced = {
        _canonical(row[0])
        for row in connection.execute(
            "SELECT DISTINCT backup_path FROM actual_runs WHERE backup_path IS NOT NULL"
        )
        if row[0]
    }
    approved = connection.execute(
        "SELECT value FROM settings WHERE key = 'approved_backup'"
    ).fetchone()
    if approved is not None and approved[0]:
        referenced.add(_canonical(approved[0]))
    return referenced


def _maintenance_blockers(connection) -> list[str]:
    blockers = []
    open_runs = connection.execute(
        "SELECT COUNT(*) FROM actual_runs WHERE state IN ('approved', 'active')"
    ).fetchone()[0]
    if open_runs:
        blockers.append(f"open_actual_runs:{open_runs}")
    unfinished_operations = connection.execute(
        "SELECT COUNT(*) FROM operations "
        "WHERE state IN ('planned', 'fs_done', 'db_done')"
    ).fetchone()[0]
    if unfinished_operations:
        blockers.append(f"unfinished_operations:{unfinished_operations}")
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "operation_groups" in tables:
        unfinished_groups = connection.execute(
            "SELECT COUNT(*) FROM operation_groups "
            "WHERE state IN ('planned', 'fs_done', 'db_done')"
        ).fetchone()[0]
        if unfinished_groups:
            blockers.append(f"unfinished_operation_groups:{unfinished_groups}")
    return blockers


def _archive_paths(state_db: Path, source: Path) -> tuple[Path, Path]:
    root = state_db.parent / "cold_archive" / "backups"
    return root / f"{source.name}.gz", root / f"{source.name}.archive.json"


def _plan_hash_payload(plan) -> dict:
    return {
        "schema_version": plan["schema_version"],
        "kind": plan["kind"],
        "archive_version": plan["archive_version"],
        "state_db": plan["state_db"],
        "backup_dir": plan["backup_dir"],
        "keep_latest_unreferenced": plan["keep_latest_unreferenced"],
        "blockers": plan["blockers"],
        "referenced_paths": plan["referenced_paths"],
        "retained_unreferenced_paths": plan["retained_unreferenced_paths"],
        "unsafe_paths": plan["unsafe_paths"],
        "items": plan["items"],
    }


def build_backup_archive_plan(
    state_db=STATE_DB,
    *,
    keep_latest_unreferenced=DEFAULT_KEEP_LATEST_UNREFERENCED,
):
    """Return a read-only, identity-bound plan for unreferenced backups."""
    if keep_latest_unreferenced < 0:
        raise ValueError("keep_latest_unreferenced must be non-negative")
    state_db = Path(state_db).expanduser().resolve()
    if not state_db.is_file():
        raise FileNotFoundError(state_db)
    backup_dir = state_db.parent / "backups"
    connection = _connect_readonly(state_db)
    try:
        blockers = _maintenance_blockers(connection)
        referenced = _referenced_backup_paths(connection)
    finally:
        connection.close()

    safe = []
    unsafe_paths = []
    if backup_dir.is_dir():
        for source in backup_dir.iterdir():
            if source.suffix != ".sqlite3":
                continue
            try:
                info = source.lstat()
            except OSError as exc:
                unsafe_paths.append({"path": str(source), "reason": str(exc)})
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                unsafe_paths.append({
                    "path": str(source),
                    "reason": "not_single_link_regular_file",
                })
                continue
            safe.append((info.st_mtime_ns, source, info))

    safe.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    unreferenced = [item for item in safe if _canonical(item[1]) not in referenced]
    retained = unreferenced[:keep_latest_unreferenced]
    eligible = unreferenced[keep_latest_unreferenced:]
    items = []
    for _mtime, source, info in eligible:
        archive_path, metadata_path = _archive_paths(state_db, source)
        items.append({
            "source_path": _canonical(source),
            "archive_path": str(archive_path.resolve()),
            "metadata_path": str(metadata_path.resolve()),
            "dev": info.st_dev,
            "ino": info.st_ino,
            "ctime_ns": info.st_ctime_ns,
            "mtime_ns": info.st_mtime_ns,
            "size": info.st_size,
        })
    items.sort(key=lambda item: item["source_path"])
    plan = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "kind": "state_backup_archive_plan",
        "archive_version": ARCHIVE_VERSION,
        "generated_at": _utc_now(),
        "state_db": str(state_db),
        "backup_dir": str(backup_dir.resolve()),
        "keep_latest_unreferenced": keep_latest_unreferenced,
        "blockers": sorted(blockers),
        "referenced_paths": sorted(referenced),
        "retained_unreferenced_paths": sorted(
            _canonical(path) for _mtime, path, _info in retained
        ),
        "unsafe_paths": sorted(unsafe_paths, key=lambda item: item["path"]),
        "items": items,
        "eligible_count": len(items),
        "eligible_bytes": sum(item["size"] for item in items),
    }
    plan["plan_sha256"] = _hash_payload(_plan_hash_payload(plan))
    return plan


def _sqlite_integrity(path: Path) -> None:
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    if result != "ok":
        raise RuntimeError(f"backup integrity_check failed: {path}: {result}")


def _inspect_gzip_archive(
    archive_path: Path,
    *,
    expected_archive_sha256=None,
    expected_archive_size=None,
    expected_raw_sha256=None,
    expected_raw_size=None,
) -> dict:
    """Hash compressed and raw bytes from one pinned no-follow descriptor."""
    descriptor = os.open(
        archive_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(f"cold archive is not an owned regular file: {archive_path}")
        archive_digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, CHUNK_SIZE)
            if not chunk:
                break
            archive_digest.update(chunk)
        archive_sha256 = archive_digest.hexdigest()
        if (
            expected_archive_sha256 is not None
            and archive_sha256 != expected_archive_sha256
        ) or (
            expected_archive_size is not None
            and before.st_size != expected_archive_size
        ):
            raise RuntimeError(f"cold archive SHA-256 mismatch: {archive_path}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw_digest = hashlib.sha256()
        raw_size = 0
        duplicate = os.dup(descriptor)
        try:
            with os.fdopen(duplicate, "rb") as raw_stream:
                try:
                    with gzip.GzipFile(fileobj=raw_stream, mode="rb") as compressed:
                        for chunk in iter(lambda: compressed.read(CHUNK_SIZE), b""):
                            raw_size += len(chunk)
                            if (
                                expected_raw_size is not None
                                and raw_size > expected_raw_size
                            ):
                                raise RuntimeError(
                                    f"cold archive expands beyond expected size: {archive_path}"
                                )
                            raw_digest.update(chunk)
                except (OSError, EOFError) as exc:
                    raise RuntimeError(
                        f"cold archive cannot be decompressed: {archive_path}: {exc}"
                    ) from exc
        finally:
            # fdopen owns ``duplicate`` after successful construction.  This
            # branch only matters if construction itself failed.
            try:
                os.close(duplicate)
            except OSError:
                pass
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev, before.st_ino, before.st_ctime_ns,
            before.st_size, before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_ctime_ns,
            after.st_size, after.st_mtime_ns,
        )
        if before_identity != after_identity:
            raise SourceIdentityChanged(f"cold archive changed while read: {archive_path}")
        raw_sha256 = raw_digest.hexdigest()
        if (
            expected_raw_size is not None and raw_size != expected_raw_size
        ) or (
            expected_raw_sha256 is not None and raw_sha256 != expected_raw_sha256
        ):
            raise RuntimeError(f"cold archive raw content mismatch: {archive_path}")
        return {
            "archive_size": after.st_size,
            "archive_sha256": archive_sha256,
            "raw_size": raw_size,
            "raw_sha256": raw_sha256,
        }
    finally:
        os.close(descriptor)


def _compress_backup(source: Path, archive_path: Path, expected) -> None:
    ensure_directory_nofollow(archive_path.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{archive_path.name}.", suffix=".tmp", dir=archive_path.parent
    )
    source_fd = None
    try:
        source_fd = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(source_fd)
        identity = (
            before.st_dev, before.st_ino, before.st_ctime_ns,
            before.st_size, before.st_mtime_ns,
        )
        expected_identity = (
            expected.dev, expected.ino, expected.ctime_ns,
            expected.size, expected.mtime_ns,
        )
        if identity != expected_identity or not stat.S_ISREG(before.st_mode):
            raise SourceIdentityChanged(f"archive source identity changed: {source}")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "wb", closefd=False) as raw_output:
            with gzip.GzipFile(
                filename="", mode="wb", compresslevel=6,
                fileobj=raw_output, mtime=0,
            ) as compressed:
                while True:
                    chunk = os.read(source_fd, CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    compressed.write(chunk)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        after = os.fstat(source_fd)
        after_identity = (
            after.st_dev, after.st_ino, after.st_ctime_ns,
            after.st_size, after.st_mtime_ns,
        )
        if after_identity != expected_identity or digest.hexdigest() != expected.sha256:
            raise SourceIdentityChanged(f"archive source changed while read: {source}")
        if archive_path.exists():
            raise FileExistsError(archive_path)
        os.link(temporary, archive_path)
        os.unlink(temporary)
        temporary = None
        _fsync_directory(archive_path.parent)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _archive_metadata(state_db: Path, source: Path, archive_path: Path, expected) -> dict:
    archive_evidence = _inspect_gzip_archive(
        archive_path,
        expected_raw_sha256=expected.sha256,
        expected_raw_size=expected.size,
    )
    if (
        archive_evidence["raw_size"] != expected.size
        or archive_evidence["raw_sha256"] != expected.sha256
    ):
        raise RuntimeError(f"archive round-trip verification failed: {source}")
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "kind": "state_backup_archive_object",
        "archive_version": ARCHIVE_VERSION,
        "created_at": _utc_now(),
        "state_db": str(state_db.resolve()),
        "source_path": str(source.resolve()),
        "archive_path": str(archive_path.resolve()),
        "source": {
            "dev": expected.dev,
            "ino": expected.ino,
            "ctime_ns": expected.ctime_ns,
            "mtime_ns": expected.mtime_ns,
            "size": expected.size,
            "sha256": expected.sha256,
        },
        "archive": {
            "size": archive_evidence["archive_size"],
            "sha256": archive_evidence["archive_sha256"],
            "format": "gzip",
        },
    }


def _load_metadata(path: Path) -> dict:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError(f"archive metadata is not an owned regular file: {path}")
    _evidence, payload = read_json_with_evidence(path)
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ARCHIVE_SCHEMA_VERSION
        or payload.get("kind") != "state_backup_archive_object"
    ):
        raise ValueError(f"unsupported archive metadata: {path}")
    return payload


def archive_backup_path(state_db, item) -> dict:
    """Archive and consume one currently unreferenced backup."""
    state_db = Path(state_db).expanduser().resolve()
    source = Path(item["source_path"])
    archive_path = Path(item["archive_path"])
    metadata_path = Path(item["metadata_path"])
    expected_backup_root = state_db.parent / "backups"
    expected_archive_root = state_db.parent / "cold_archive" / "backups"
    if (
        source.parent.resolve() != expected_backup_root.resolve()
        or archive_path.parent.resolve() != expected_archive_root.resolve()
        or metadata_path.parent.resolve() != expected_archive_root.resolve()
        or source.suffix != ".sqlite3"
        or archive_path.name != f"{source.name}.gz"
        or metadata_path.name != f"{source.name}.archive.json"
    ):
        raise RuntimeError("archive item is outside managed state roots")
    current = source.lstat()
    identity = (
        current.st_dev, current.st_ino, current.st_ctime_ns,
        current.st_mtime_ns, current.st_size,
    )
    planned = (
        item["dev"], item["ino"], item["ctime_ns"],
        item["mtime_ns"], item["size"],
    )
    if identity != planned or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise SourceIdentityChanged(f"archive plan source changed: {source}")

    connection = _connect_readonly(state_db)
    try:
        if _canonical(source) in _referenced_backup_paths(connection):
            raise RuntimeError(f"backup became referenced after planning: {source}")
    finally:
        connection.close()

    source_evidence = inspect_regular_file(source)
    _sqlite_integrity(source)
    if archive_path.exists():
        if not metadata_path.is_file():
            raise RuntimeError(f"archive exists without metadata: {archive_path}")
        metadata = _load_metadata(metadata_path)
        archive_evidence = _inspect_gzip_archive(
            archive_path,
            expected_archive_sha256=metadata.get("archive", {}).get("sha256"),
            expected_archive_size=metadata.get("archive", {}).get("size"),
            expected_raw_sha256=source_evidence.sha256,
            expected_raw_size=source_evidence.size,
        )
        if (
            metadata.get("state_db") != str(state_db)
            or metadata.get("source_path") != str(source.resolve())
            or metadata.get("archive_path") != str(archive_path.resolve())
            or metadata.get("source", {}).get("sha256") != source_evidence.sha256
            or metadata.get("source", {}).get("size") != source_evidence.size
            or metadata.get("archive", {}).get("sha256")
            != archive_evidence["archive_sha256"]
            or metadata.get("archive", {}).get("size")
            != archive_evidence["archive_size"]
        ):
            raise RuntimeError(f"existing archive evidence mismatch: {archive_path}")
        if (
            archive_evidence["raw_size"] != source_evidence.size
            or archive_evidence["raw_sha256"] != source_evidence.sha256
        ):
            raise RuntimeError(f"existing archive round-trip mismatch: {archive_path}")
    else:
        if metadata_path.exists():
            raise RuntimeError(f"archive metadata exists without object: {metadata_path}")
        _compress_backup(source, archive_path, source_evidence)
        metadata = _archive_metadata(
            state_db, source, archive_path, source_evidence
        )
        _atomic_json_write(metadata_path, metadata)

    # Serialize the last reference check and source consumption with every
    # actual-run approval writer.  issue_actual_run_token() revalidates its
    # backup inside that same writer boundary, closing both race directions.
    connection = sqlite3.connect(str(state_db), timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        blockers = _maintenance_blockers(connection)
        if blockers:
            raise RuntimeError(
                "archive maintenance became blocked: " + ", ".join(blockers)
            )
        if _canonical(source) in _referenced_backup_paths(connection):
            raise RuntimeError(f"backup became referenced before consume: {source}")
        unlink_owned(source, expected=source_evidence)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "source_path": str(source),
        "archive_path": str(archive_path),
        "metadata_path": str(metadata_path),
        "source_bytes": source_evidence.size,
        "archive_bytes": metadata["archive"]["size"],
        "saved_bytes": source_evidence.size - metadata["archive"]["size"],
        "source_sha256": source_evidence.sha256,
        "archive_sha256": metadata["archive"]["sha256"],
    }


def _validate_plan_confirmation(plan, confirm_count, confirm_plan_sha256) -> None:
    if int(confirm_count) != plan["eligible_count"]:
        raise RuntimeError("archive confirmation count mismatch")
    if str(confirm_plan_sha256) != plan["plan_sha256"]:
        raise RuntimeError("archive confirmation plan SHA-256 mismatch")


def apply_backup_archive_plan(
    state_db,
    plan,
    *,
    confirm_count,
    confirm_plan_sha256,
):
    """Rebuild and apply the exact confirmed archive plan under one lock."""
    state_db = Path(state_db).expanduser().resolve()
    if _canonical(state_db) != plan.get("state_db"):
        raise RuntimeError("archive plan state DB mismatch")
    _validate_plan_confirmation(plan, confirm_count, confirm_plan_sha256)
    with _archive_lock(state_db.parent):
        current = build_backup_archive_plan(
            state_db,
            keep_latest_unreferenced=plan["keep_latest_unreferenced"],
        )
        if current["plan_sha256"] != plan["plan_sha256"]:
            raise RuntimeError("archive plan is stale; rebuild and reconfirm")
        if current["blockers"]:
            raise RuntimeError(
                "archive maintenance is blocked: " + ", ".join(current["blockers"])
            )
        integrity = _connect_readonly(state_db)
        try:
            state_integrity = integrity.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            integrity.close()
        if state_integrity != "ok":
            raise RuntimeError(f"state DB integrity_check failed: {state_integrity}")

        report_dir = state_db.parent / "reports"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        intent_path = report_dir / f"state_archive_1_4_9_intent_{stamp}.json"
        _atomic_json_write(intent_path, {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "kind": "state_backup_archive_intent",
            "archive_version": ARCHIVE_VERSION,
            "created_at": _utc_now(),
            "state_db": str(state_db),
            "plan_sha256": current["plan_sha256"],
            "eligible_count": current["eligible_count"],
            "eligible_bytes": current["eligible_bytes"],
            "items": current["items"],
        })
        archived = [archive_backup_path(state_db, item) for item in current["items"]]
        report = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "kind": "state_backup_archive_execution",
            "archive_version": ARCHIVE_VERSION,
            "completed_at": _utc_now(),
            "state_db": str(state_db),
            "plan_sha256": current["plan_sha256"],
            "intent_path": str(intent_path),
            "archived": archived,
            "archived_count": len(archived),
            "source_bytes": sum(item["source_bytes"] for item in archived),
            "archive_bytes": sum(item["archive_bytes"] for item in archived),
            "saved_bytes": sum(item["saved_bytes"] for item in archived),
        }
        report_path = report_dir / f"state_archive_1_4_9_{stamp}.json"
        _atomic_json_write(report_path, report)
        report["report_path"] = str(report_path)
        return report


def restore_archived_backup(state_db, metadata_path, *, confirm_raw_sha256):
    """Restore a verified hot SQLite copy without deleting the cold object."""
    state_db = Path(state_db).expanduser().resolve()
    metadata_path = Path(metadata_path).expanduser().absolute()
    metadata = _load_metadata(metadata_path)
    if metadata.get("state_db") != str(state_db):
        raise RuntimeError("archive metadata state DB mismatch")
    source = Path(metadata["source_path"])
    archive_path = Path(metadata["archive_path"])
    expected_backup_root = state_db.parent / "backups"
    expected_archive_root = state_db.parent / "cold_archive" / "backups"
    if (
        not state_db.is_file()
        or metadata_path.parent.resolve() != expected_archive_root.resolve()
        or archive_path.parent.resolve() != expected_archive_root.resolve()
        or source.parent.resolve() != expected_backup_root.resolve()
        or metadata_path.name != f"{source.name}.archive.json"
        or archive_path.name != f"{source.name}.gz"
        or source.suffix != ".sqlite3"
    ):
        raise RuntimeError("archive metadata is outside managed state roots")
    expected_raw = metadata["source"]["sha256"]
    if confirm_raw_sha256 != expected_raw:
        raise RuntimeError("restore confirmation raw SHA-256 mismatch")
    if source.exists():
        raise FileExistsError(source)
    archive_evidence = _inspect_gzip_archive(
        archive_path,
        expected_archive_sha256=metadata["archive"]["sha256"],
        expected_archive_size=metadata["archive"]["size"],
        expected_raw_sha256=expected_raw,
        expected_raw_size=metadata["source"]["size"],
    )
    if (
        archive_evidence["archive_sha256"] != metadata["archive"]["sha256"]
        or archive_evidence["archive_size"] != metadata["archive"]["size"]
    ):
        raise RuntimeError("cold archive SHA-256 mismatch")
    ensure_directory_nofollow(source.parent)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{source.name}.", suffix=".restore", dir=source.parent
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(descriptor, "wb") as output:
            archive_descriptor = os.open(
                archive_path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                with os.fdopen(archive_descriptor, "rb") as archive_stream:
                    with gzip.GzipFile(fileobj=archive_stream, mode="rb") as compressed:
                        for chunk in iter(lambda: compressed.read(CHUNK_SIZE), b""):
                            total += len(chunk)
                            if total > metadata["source"]["size"]:
                                raise RuntimeError(
                                    "cold archive expands beyond expected restore size"
                                )
                            digest.update(chunk)
                            output.write(chunk)
            finally:
                try:
                    os.close(archive_descriptor)
                except OSError:
                    pass
            output.flush()
            os.fsync(output.fileno())
        if total != metadata["source"]["size"] or digest.hexdigest() != expected_raw:
            raise RuntimeError("restored backup content mismatch")
        _sqlite_integrity(Path(temporary))
        if source.exists():
            raise FileExistsError(source)
        os.link(temporary, source)
        os.unlink(temporary)
        temporary = None
        _fsync_directory(source.parent)
        restored = inspect_regular_file(source)
        if restored.sha256 != expected_raw:
            raise RuntimeError("restored backup verification failed")
        result = {
            "restored_path": str(source),
            "source_bytes": restored.size,
            "source_sha256": restored.sha256,
            "archive_preserved": str(archive_path),
        }
        report_dir = state_db.parent / "reports"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        report_path = report_dir / f"state_archive_1_4_9_restore_{stamp}.json"
        _atomic_json_write(report_path, {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "kind": "state_backup_archive_restore",
            "archive_version": ARCHIVE_VERSION,
            "completed_at": _utc_now(),
            "state_db": str(state_db),
            "metadata_path": str(metadata_path),
            **result,
        })
        result["report_path"] = str(report_path)
        return result
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def build_parser():
    parser = argparse.ArgumentParser(description="verified state backup cold archive")
    parser.add_argument("--state-db", default=str(STATE_DB))
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument(
        "--keep-latest-unreferenced", type=int,
        default=DEFAULT_KEEP_LATEST_UNREFERENCED,
    )
    apply = subparsers.add_parser("apply")
    apply.add_argument(
        "--keep-latest-unreferenced", type=int,
        default=DEFAULT_KEEP_LATEST_UNREFERENCED,
    )
    apply.add_argument("--confirm-count", type=int, required=True)
    apply.add_argument("--confirm-plan-sha256", required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--metadata", required=True)
    restore.add_argument("--confirm-raw-sha256", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "restore":
        result = restore_archived_backup(
            args.state_db,
            args.metadata,
            confirm_raw_sha256=args.confirm_raw_sha256,
        )
    else:
        plan = build_backup_archive_plan(
            args.state_db,
            keep_latest_unreferenced=args.keep_latest_unreferenced,
        )
        if args.command == "plan":
            result = plan
        else:
            result = apply_backup_archive_plan(
                args.state_db,
                plan,
                confirm_count=args.confirm_count,
                confirm_plan_sha256=args.confirm_plan_sha256,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
