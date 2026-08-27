"""Immutable content-addressed storage for large fingerprint anchors.

The fingerprint row remains the stable evidence identity used by decisions,
reviews, operations, and recovery.  Large front/tail anchors are serialized
with an explicit boundary, addressed by the SHA-256 of those canonical bytes,
compressed once, and referenced by any number of immutable fingerprints.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import zlib
from collections.abc import Mapping


ANCHOR_PAYLOAD_CODEC = "zlib-6-v1"
ANCHOR_PAYLOAD_FORMAT_VERSION = 1
MAX_ANCHOR_PAYLOAD_RAW_BYTES = 64 * 1024 * 1024

_FRAME_MAGIC = b"file-check-anchor-payload-v1\0"
_FRAME_LENGTHS = struct.Struct(">QQ")


class AnchorPayloadError(RuntimeError):
    """Base error for an invalid or inconsistent anchor payload."""


class AnchorPayloadCorruptionError(AnchorPayloadError):
    """Stored compressed bytes do not satisfy their immutable metadata."""


def _normalize_anchor(value, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str or None")
    return value


def canonical_anchor_payload(front_anchor, tail_anchor) -> bytes:
    """Return boundary-preserving UTF-8 bytes for one front/tail pair."""
    front = _normalize_anchor(front_anchor, "front_anchor").encode("utf-8")
    tail = _normalize_anchor(tail_anchor, "tail_anchor").encode("utf-8")
    raw = _FRAME_MAGIC + _FRAME_LENGTHS.pack(len(front), len(tail)) + front + tail
    if len(raw) > MAX_ANCHOR_PAYLOAD_RAW_BYTES:
        raise AnchorPayloadError(
            f"anchor payload exceeds {MAX_ANCHOR_PAYLOAD_RAW_BYTES} bytes"
        )
    return raw


def _payload_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bounded_decompress(compressed: bytes, expected_length: int) -> bytes:
    if expected_length < len(_FRAME_MAGIC) + _FRAME_LENGTHS.size:
        raise AnchorPayloadCorruptionError("anchor payload raw length is too small")
    if expected_length > MAX_ANCHOR_PAYLOAD_RAW_BYTES:
        raise AnchorPayloadCorruptionError("anchor payload raw length exceeds limit")
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, expected_length + 1)
        if len(raw) > expected_length or decoder.unconsumed_tail:
            raise AnchorPayloadCorruptionError(
                "anchor payload expands beyond its declared length"
            )
        suffix = decoder.flush()
    except zlib.error as exc:
        raise AnchorPayloadCorruptionError(
            f"anchor payload decompression failed: {exc}"
        ) from exc
    raw += suffix
    if len(raw) != expected_length:
        raise AnchorPayloadCorruptionError(
            "anchor payload decompressed length does not match metadata"
        )
    if not decoder.eof or decoder.unused_data:
        raise AnchorPayloadCorruptionError(
            "anchor payload compressed stream is incomplete or has trailing data"
        )
    return raw


def _decode_object(row: Mapping) -> tuple[str, str]:
    codec = row["codec"]
    if codec != ANCHOR_PAYLOAD_CODEC:
        raise AnchorPayloadCorruptionError(
            f"unsupported anchor payload codec: {codec!r}"
        )
    compressed = bytes(row["compressed_payload"])
    raw = _bounded_decompress(compressed, int(row["raw_length"]))
    digest = _payload_hash(raw)
    if digest != row["payload_hash"] or digest != row["raw_checksum"]:
        raise AnchorPayloadCorruptionError("anchor payload checksum mismatch")
    header = len(_FRAME_MAGIC) + _FRAME_LENGTHS.size
    if not raw.startswith(_FRAME_MAGIC) or len(raw) < header:
        raise AnchorPayloadCorruptionError("anchor payload frame is invalid")
    front_length, tail_length = _FRAME_LENGTHS.unpack(
        raw[len(_FRAME_MAGIC):header]
    )
    if (
        front_length != row["front_byte_length"]
        or tail_length != row["tail_byte_length"]
        or header + front_length + tail_length != len(raw)
    ):
        raise AnchorPayloadCorruptionError(
            "anchor payload boundary metadata does not match its frame"
        )
    front_bytes = raw[header:header + front_length]
    tail_bytes = raw[header + front_length:]
    try:
        return front_bytes.decode("utf-8"), tail_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnchorPayloadCorruptionError(
            "anchor payload contains invalid UTF-8"
        ) from exc


def _object_row(conn: sqlite3.Connection, payload_hash: str):
    return conn.execute(
        """
        SELECT payload_hash, codec, front_byte_length, tail_byte_length,
               raw_length, compressed_payload, raw_checksum
        FROM anchor_payload_objects WHERE payload_hash = ?
        """,
        (payload_hash,),
    ).fetchone()


def ensure_anchor_payload_object(
    conn: sqlite3.Connection,
    front_anchor,
    tail_anchor,
) -> str | None:
    """Create or verify one immutable payload object and return its hash."""
    front = _normalize_anchor(front_anchor, "front_anchor")
    tail = _normalize_anchor(tail_anchor, "tail_anchor")
    if not front and not tail:
        return None
    raw = canonical_anchor_payload(front, tail)
    payload_hash = _payload_hash(raw)
    front_length = len(front.encode("utf-8"))
    tail_length = len(tail.encode("utf-8"))
    compressed = zlib.compress(raw, level=6)
    if _bounded_decompress(compressed, len(raw)) != raw:
        raise AnchorPayloadError("anchor payload compression round-trip failed")
    conn.execute(
        """
        INSERT INTO anchor_payload_objects(
            payload_hash, codec, front_byte_length, tail_byte_length,
            raw_length, compressed_payload, raw_checksum
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(payload_hash) DO NOTHING
        """,
        (
            payload_hash,
            ANCHOR_PAYLOAD_CODEC,
            front_length,
            tail_length,
            len(raw),
            sqlite3.Binary(compressed),
            payload_hash,
        ),
    )
    stored = _object_row(conn, payload_hash)
    if stored is None:
        raise AnchorPayloadError("anchor payload insert did not converge")
    if _decode_object(stored) != (front, tail):
        raise AnchorPayloadError(
            "anchor payload hash collision has different canonical bytes"
        )
    return payload_hash


def prepare_fingerprint_anchor_payload(
    conn: sqlite3.Connection,
    front_anchor,
    tail_anchor,
) -> tuple[str, str | None]:
    """Materialize an object before inserting its immutable fingerprint row."""
    payload_hash = ensure_anchor_payload_object(conn, front_anchor, tail_anchor)
    if payload_hash is None:
        return "none", None
    return "present", payload_hash


def _strict_storage_enabled(conn: sqlite3.Connection) -> bool:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version < 19:
        return False
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(fingerprints)")
    }
    return {"anchor_payload_state", "anchor_payload_hash"} <= columns


def store_fingerprint_anchor_payload(
    conn: sqlite3.Connection,
    fingerprint_id: int,
    front_anchor,
    tail_anchor,
) -> str | None:
    """Attach one immutable content-addressed payload to a fingerprint."""
    strict = _strict_storage_enabled(conn)
    expected_columns = (
        ", anchor_payload_state, anchor_payload_hash" if strict else ""
    )
    fingerprint = conn.execute(
        "SELECT fingerprint_id, front_anchor, tail_anchor"
        + expected_columns
        + " FROM fingerprints WHERE fingerprint_id = ?",
        (fingerprint_id,),
    ).fetchone()
    if fingerprint is None:
        raise KeyError(fingerprint_id)
    front = _normalize_anchor(front_anchor, "front_anchor")
    tail = _normalize_anchor(tail_anchor, "tail_anchor")
    existing = conn.execute(
        "SELECT payload_hash FROM fingerprint_anchor_refs WHERE fingerprint_id = ?",
        (fingerprint_id,),
    ).fetchone()
    if existing is not None:
        if load_fingerprint_anchor_payload(conn, fingerprint_id) != (front, tail):
            raise AnchorPayloadError(
                "fingerprint already references different anchor evidence"
            )
        return existing["payload_hash"]
    legacy = (
        _normalize_anchor(fingerprint["front_anchor"], "front_anchor"),
        _normalize_anchor(fingerprint["tail_anchor"], "tail_anchor"),
    )
    if strict and any(legacy):
        raise AnchorPayloadCorruptionError(
            "schema v19 fingerprint contains forbidden legacy anchor evidence"
        )
    if any(legacy) and legacy != (front, tail):
        raise AnchorPayloadError(
            "fingerprint legacy anchor evidence differs from new payload"
        )
    payload_hash = ensure_anchor_payload_object(conn, front, tail)
    if strict:
        expected_state = fingerprint["anchor_payload_state"]
        expected_hash = fingerprint["anchor_payload_hash"]
        if payload_hash is None:
            if expected_state != "none" or expected_hash is not None:
                raise AnchorPayloadError(
                    "empty anchor payload differs from fingerprint expectation"
                )
            return None
        if expected_state != "present" or expected_hash != payload_hash:
            raise AnchorPayloadError(
                "anchor payload differs from fingerprint expected hash"
            )
    if payload_hash is None:
        return None
    conn.execute(
        """
        INSERT INTO fingerprint_anchor_refs(fingerprint_id, payload_hash)
        VALUES (?, ?)
        """,
        (fingerprint_id, payload_hash),
    )
    return payload_hash


def load_fingerprint_anchor_payload(
    conn: sqlite3.Connection,
    fingerprint_id: int,
) -> tuple[str, str]:
    """Load and validate anchors, falling back only for legacy/test rows."""
    strict = _strict_storage_enabled(conn)
    expected_columns = (
        "fp.anchor_payload_state, fp.anchor_payload_hash, " if strict else ""
    )
    row = conn.execute(
        """
        SELECT fp.front_anchor, fp.tail_anchor, """
        + expected_columns
        + """ref.payload_hash,
               obj.codec, obj.front_byte_length, obj.tail_byte_length,
               obj.raw_length, obj.compressed_payload, obj.raw_checksum
        FROM fingerprints AS fp
        LEFT JOIN fingerprint_anchor_refs AS ref
          ON ref.fingerprint_id = fp.fingerprint_id
        LEFT JOIN anchor_payload_objects AS obj
          ON obj.payload_hash = ref.payload_hash
        WHERE fp.fingerprint_id = ?
        """,
        (fingerprint_id,),
    ).fetchone()
    if row is None:
        raise KeyError(fingerprint_id)
    legacy = (
        _normalize_anchor(row["front_anchor"], "front_anchor"),
        _normalize_anchor(row["tail_anchor"], "tail_anchor"),
    )
    if strict:
        if row["front_anchor"] is not None or row["tail_anchor"] is not None:
            raise AnchorPayloadCorruptionError(
                "schema v19 fingerprint contains forbidden legacy anchor evidence"
            )
        expected_state = row["anchor_payload_state"]
        expected_hash = row["anchor_payload_hash"]
        if expected_state == "none":
            if expected_hash is not None or row["payload_hash"] is not None:
                raise AnchorPayloadCorruptionError(
                    "fingerprint declared no anchors but has payload evidence"
                )
            return "", ""
        if expected_state != "present" or expected_hash is None:
            raise AnchorPayloadCorruptionError(
                "fingerprint anchor payload expectation is invalid"
            )
        if row["payload_hash"] is None:
            raise AnchorPayloadCorruptionError(
                "fingerprint expected anchor payload reference is missing"
            )
        if row["payload_hash"] != expected_hash:
            raise AnchorPayloadCorruptionError(
                "fingerprint anchor reference differs from expected hash"
            )
    elif row["payload_hash"] is None:
        return legacy
    if row["compressed_payload"] is None:
        raise AnchorPayloadCorruptionError(
            "fingerprint anchor reference has no payload object"
        )
    decoded = _decode_object(row)
    if any(legacy) and legacy != decoded:
        raise AnchorPayloadCorruptionError(
            "legacy and content-addressed anchor evidence disagree"
        )
    return decoded


def copy_fingerprint_anchor_payload(
    conn: sqlite3.Connection,
    source_fingerprint_id: int,
    target_fingerprint_id: int,
) -> str | None:
    """Copy immutable anchor evidence to a newly cloned fingerprint."""
    front, tail = load_fingerprint_anchor_payload(conn, source_fingerprint_id)
    return store_fingerprint_anchor_payload(
        conn, target_fingerprint_id, front, tail
    )


def migrate_legacy_anchor_payloads(conn: sqlite3.Connection) -> dict:
    """Backfill every non-empty legacy anchor row without changing fingerprints."""
    scanned = referenced = 0
    for row in conn.execute(
        "SELECT fingerprint_id, front_anchor, tail_anchor FROM fingerprints "
        "ORDER BY fingerprint_id"
    ):
        scanned += 1
        front = _normalize_anchor(row["front_anchor"], "front_anchor")
        tail = _normalize_anchor(row["tail_anchor"], "tail_anchor")
        if not front and not tail:
            continue
        payload_hash = ensure_anchor_payload_object(conn, front, tail)
        conn.execute(
            """
            INSERT INTO fingerprint_anchor_refs(fingerprint_id, payload_hash)
            VALUES (?, ?)
            ON CONFLICT(fingerprint_id) DO NOTHING
            """,
            (row["fingerprint_id"], payload_hash),
        )
        stored = conn.execute(
            "SELECT payload_hash FROM fingerprint_anchor_refs WHERE fingerprint_id = ?",
            (row["fingerprint_id"],),
        ).fetchone()
        if stored is None or stored["payload_hash"] != payload_hash:
            raise AnchorPayloadError(
                f"legacy anchor reference mismatch: {row['fingerprint_id']}"
            )
        referenced += 1
    return {
        "fingerprints_scanned": scanned,
        "fingerprints_referenced": referenced,
        **anchor_payload_storage_stats(conn),
    }


def validate_anchor_payload_storage(
    conn: sqlite3.Connection,
    *,
    verify_payloads: bool = True,
) -> None:
    """Fail closed on orphaned, mutable, or corrupt payload storage."""
    strict = _strict_storage_enabled(conn)
    if strict:
        legacy = conn.execute(
            """
            SELECT fingerprint_id FROM fingerprints
            WHERE front_anchor IS NOT NULL OR tail_anchor IS NOT NULL
            LIMIT 1
            """
        ).fetchone()
        if legacy is not None:
            raise AnchorPayloadCorruptionError(
                f"schema v19 fingerprint has legacy anchors: {legacy[0]}"
            )
        invalid_expectation = conn.execute(
            """
            SELECT fingerprint_id FROM fingerprints
            WHERE anchor_payload_state NOT IN ('none', 'present')
               OR (anchor_payload_state = 'none' AND anchor_payload_hash IS NOT NULL)
               OR (anchor_payload_state = 'present' AND anchor_payload_hash IS NULL)
            LIMIT 1
            """
        ).fetchone()
        if invalid_expectation is not None:
            raise AnchorPayloadCorruptionError(
                f"invalid fingerprint anchor expectation: {invalid_expectation[0]}"
            )
        missing_ref = conn.execute(
            """
            SELECT fp.fingerprint_id FROM fingerprints AS fp
            LEFT JOIN fingerprint_anchor_refs AS ref
              ON ref.fingerprint_id = fp.fingerprint_id
            WHERE fp.anchor_payload_state = 'present'
              AND ref.fingerprint_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if missing_ref is not None:
            raise AnchorPayloadCorruptionError(
                f"fingerprint expected anchor reference is missing: {missing_ref[0]}"
            )
        unexpected_ref = conn.execute(
            """
            SELECT fp.fingerprint_id FROM fingerprints AS fp
            JOIN fingerprint_anchor_refs AS ref
              ON ref.fingerprint_id = fp.fingerprint_id
            WHERE fp.anchor_payload_state = 'none'
            LIMIT 1
            """
        ).fetchone()
        if unexpected_ref is not None:
            raise AnchorPayloadCorruptionError(
                f"fingerprint unexpectedly has anchor reference: {unexpected_ref[0]}"
            )
        wrong_ref = conn.execute(
            """
            SELECT fp.fingerprint_id FROM fingerprints AS fp
            JOIN fingerprint_anchor_refs AS ref
              ON ref.fingerprint_id = fp.fingerprint_id
            WHERE fp.anchor_payload_state = 'present'
              AND ref.payload_hash != fp.anchor_payload_hash
            LIMIT 1
            """
        ).fetchone()
        if wrong_ref is not None:
            raise AnchorPayloadCorruptionError(
                f"fingerprint anchor reference differs from expected hash: {wrong_ref[0]}"
            )

    orphan_ref = conn.execute(
        """
        SELECT ref.fingerprint_id FROM fingerprint_anchor_refs AS ref
        LEFT JOIN fingerprints AS fp ON fp.fingerprint_id = ref.fingerprint_id
        LEFT JOIN anchor_payload_objects AS obj
          ON obj.payload_hash = ref.payload_hash
        WHERE fp.fingerprint_id IS NULL OR obj.payload_hash IS NULL
        LIMIT 1
        """
    ).fetchone()
    if orphan_ref is not None:
        raise AnchorPayloadCorruptionError(
            f"orphan fingerprint anchor reference: {orphan_ref[0]}"
        )
    orphan_object = conn.execute(
        """
        SELECT obj.payload_hash FROM anchor_payload_objects AS obj
        LEFT JOIN fingerprint_anchor_refs AS ref
          ON ref.payload_hash = obj.payload_hash
        WHERE ref.fingerprint_id IS NULL LIMIT 1
        """
    ).fetchone()
    if orphan_object is not None:
        raise AnchorPayloadCorruptionError(
            f"unreferenced anchor payload object: {orphan_object[0]}"
        )
    if verify_payloads:
        for row in conn.execute(
            """
            SELECT payload_hash, codec, front_byte_length, tail_byte_length,
                   raw_length, compressed_payload, raw_checksum
            FROM anchor_payload_objects ORDER BY payload_hash
            """
        ):
            _decode_object(row)
        if not strict:
            for row in conn.execute(
                """
                SELECT fingerprint_id FROM fingerprint_anchor_refs
                WHERE fingerprint_id IN (
                    SELECT fingerprint_id FROM fingerprints
                    WHERE COALESCE(front_anchor, '') != ''
                       OR COALESCE(tail_anchor, '') != ''
                )
                """
            ):
                load_fingerprint_anchor_payload(conn, row["fingerprint_id"])


def anchor_payload_storage_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS object_count,
               COALESCE(SUM(raw_length), 0) AS raw_bytes,
               COALESCE(SUM(LENGTH(compressed_payload)), 0) AS compressed_bytes
        FROM anchor_payload_objects
        """
    ).fetchone()
    references = conn.execute(
        "SELECT COUNT(*) FROM fingerprint_anchor_refs"
    ).fetchone()[0]
    return {
        "object_count": int(row["object_count"]),
        "reference_count": int(references),
        "raw_bytes": int(row["raw_bytes"]),
        "compressed_bytes": int(row["compressed_bytes"]),
    }
