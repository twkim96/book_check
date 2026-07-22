"""Owned, no-clobber filesystem primitives for managed mutations."""

from __future__ import annotations

import fcntl
import codecs
import hashlib
import json
import os
import re
import stat
import struct
import tempfile
import threading
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path


class MutationLockBusy(RuntimeError):
    pass


class SourceIdentityChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class FileEvidence:
    dev: int
    ino: int
    ctime_ns: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class CopiedFile:
    source: Path
    destination: Path
    source_evidence: FileEvidence
    destination_evidence: FileEvidence


@dataclass(frozen=True)
class EpubContentEvidence:
    file_evidence: FileEvidence
    content_sha256: str
    member_count: int
    uncompressed_size: int


_WHITESPACE_RE = re.compile(r"\s+")
_LOCK_REGISTRY = {}
_LOCK_REGISTRY_GUARD = threading.RLock()


def _safe_nfc_split(value):
    for index in range(len(value) - 1, -1, -1):
        if unicodedata.combining(value[index]) == 0:
            return value[:index], value[index:]
    return "", value


def _db_path(conn) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        raise RuntimeError("mutation lock requires a file-backed state DB")
    return Path(row[2]).resolve()


def _lock_roots(conn, run_id=None):
    if run_id:
        row = conn.execute(
            "SELECT house_root, temp_root FROM actual_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT house_root, temp_root FROM actual_runs
            ORDER BY CASE state WHEN 'active' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                     approved_at DESC LIMIT 1
            """
        ).fetchone()
    if row is not None:
        return tuple(sorted((str(Path(row[0]).resolve()), str(Path(row[1]).resolve()))))
    return (str(_db_path(conn)),)


def _lock_path_for_roots(roots):
    key = hashlib.sha256("\0".join(roots).encode("utf-8")).hexdigest()
    directory = Path(tempfile.gettempdir()) / "file-check-dedup-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(directory)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError(f"unsafe mutation lock directory: {directory}")
    return directory / f"{key}.lock"


def _open_lock_file(lock_path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_path, flags, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise RuntimeError(f"unsafe mutation lock file: {lock_path}")
    return fd


@contextmanager
def mutation_lock(conn, owner: str, *, run_id=None):
    """Acquire the one non-blocking lock shared by every DB for the same roots."""
    lock_path = _lock_path_for_roots(_lock_roots(conn, run_id=run_id))
    key = str(lock_path)
    reentrant = None
    with _LOCK_REGISTRY_GUARD:
        held = _LOCK_REGISTRY.get(key)
        if (
            held and held["pid"] == os.getpid()
            and held["thread"] == threading.get_ident()
        ):
            if not held.get("command"):
                raise MutationLockBusy(
                    f"mutation lock is busy: {lock_path}; holder={held['owner']}"
                )
            held["depth"] += 1
            reentrant = held
    if reentrant is not None:
        try:
            yield lock_path
        finally:
            with _LOCK_REGISTRY_GUARD:
                reentrant["depth"] -= 1
        return
    fd = _open_lock_file(lock_path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.lseek(fd, 0, os.SEEK_SET)
            holder = os.read(fd, 4096).decode("utf-8", "replace")
            raise MutationLockBusy(
                f"mutation lock is busy: {lock_path}; holder={holder or 'unknown'}"
            ) from exc
        payload = json.dumps(
            {"pid": os.getpid(), "owner": owner}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)
        with _LOCK_REGISTRY_GUARD:
            _LOCK_REGISTRY[key] = {
                "pid": os.getpid(), "fd": fd, "depth": 1,
                "thread": threading.get_ident(), "owner": owner, "command": False,
            }
        yield lock_path
    finally:
        with _LOCK_REGISTRY_GUARD:
            _LOCK_REGISTRY.pop(key, None)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def mutation_lock_for_roots(house_root, temp_root, owner):
    """Acquire the command-wide lock before an actual run has been activated."""
    roots = tuple(sorted((str(Path(house_root).resolve()), str(Path(temp_root).resolve()))))
    lock_path = _lock_path_for_roots(roots)
    key = str(lock_path)
    reentrant = None
    with _LOCK_REGISTRY_GUARD:
        held = _LOCK_REGISTRY.get(key)
        if (
            held and held["pid"] == os.getpid()
            and held["thread"] == threading.get_ident()
        ):
            if not held.get("command"):
                raise MutationLockBusy(
                    f"mutation lock is busy: {lock_path}; holder={held['owner']}"
                )
            held["depth"] += 1
            reentrant = held
    if reentrant is not None:
        try:
            yield lock_path
        finally:
            with _LOCK_REGISTRY_GUARD:
                reentrant["depth"] -= 1
        return
    fd = _open_lock_file(lock_path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MutationLockBusy(f"mutation lock is busy: {lock_path}") from exc
        payload = json.dumps(
            {"pid": os.getpid(), "owner": owner}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)
        with _LOCK_REGISTRY_GUARD:
            _LOCK_REGISTRY[key] = {
                "pid": os.getpid(), "fd": fd, "depth": 1,
                "thread": threading.get_ident(), "owner": owner, "command": True,
            }
        yield lock_path
    finally:
        with _LOCK_REGISTRY_GUARD:
            _LOCK_REGISTRY.pop(key, None)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _identity(info, sha256: str) -> FileEvidence:
    return FileEvidence(
        dev=info.st_dev,
        ino=info.st_ino,
        ctime_ns=info.st_ctime_ns,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        sha256=sha256,
    )


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _canonical_absolute(path):
    # Normalize only macOS's stable top-level aliases.  A general realpath()
    # here would follow an attacker-replaced intermediate symlink before the
    # component-wise O_NOFOLLOW walk gets a chance to reject it.
    absolute_text = os.path.abspath(os.fspath(path))
    if absolute_text == "/var" or absolute_text.startswith("/var/"):
        absolute_text = "/private" + absolute_text
    elif absolute_text == "/tmp" or absolute_text.startswith("/tmp/"):
        absolute_text = "/private" + absolute_text
    return Path(absolute_text)


def _open_directory_nofollow(path, *, create=False, mode=0o755):
    """Open/create a directory one component at a time without following links."""
    parts = _canonical_absolute(path).parts
    fd = os.open(
        parts[0],
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        for component in parts[1:]:
            flags = (
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                next_fd = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=mode, dir_fd=fd)
                next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def ensure_directory_nofollow(path, *, mode=0o755):
    fd = _open_directory_nofollow(path, create=True, mode=mode)
    os.close(fd)


def _open_parent_nofollow(path, *, create=False):
    """Open every ancestor with openat/O_NOFOLLOW and return (parent_fd, leaf)."""
    absolute = _canonical_absolute(path)
    return _open_directory_nofollow(absolute.parent, create=create), absolute.name


def _open_regular_nofollow(path, flags=os.O_RDONLY):
    parent_fd, leaf = _open_parent_nofollow(path)
    try:
        fd = os.open(
            leaf,
            flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        return parent_fd, fd, leaf
    except Exception:
        os.close(parent_fd)
        raise


def inspect_regular_file(path) -> FileEvidence:
    parent_fd, fd, _ = _open_regular_nofollow(path)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"source is not a regular file: {path}")
        return _identity(info, _hash_fd(fd))
    finally:
        os.close(fd)
        os.close(parent_fd)


def read_json_with_evidence(path):
    """Hash and parse JSON from the same pinned no-follow file descriptor."""
    parent_fd, fd, _ = _open_regular_nofollow(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"JSON evidence is not a regular file: {path}")
        raw = bytearray()
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            raw.extend(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev, before.st_ino, before.st_ctime_ns,
            before.st_size, before.st_mtime_ns,
        ) != (
            after.st_dev, after.st_ino, after.st_ctime_ns,
            after.st_size, after.st_mtime_ns,
        ):
            raise SourceIdentityChanged(f"JSON evidence changed while read: {path}")
        evidence = _identity(after, digest.hexdigest())
        return evidence, json.loads(raw.decode("utf-8"))
    finally:
        os.close(fd)
        os.close(parent_fd)


def inspect_normalized_text(path):
    """Return source evidence and normalized SHA from one pinned no-follow fd."""
    parent_fd, fd, _ = _open_regular_nofollow(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"source is not a regular file: {path}")
        raw_sha = _hash_fd(fd)
        normalized_sha = None
        decode_errors = []
        for encoding in ("utf-8-sig", "utf-8", "cp949"):
            os.lseek(fd, 0, os.SEEK_SET)
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            digest = hashlib.sha256()
            carry = ""
            first = True
            try:
                while True:
                    raw = os.read(fd, 1024 * 1024)
                    if not raw:
                        break
                    normalized = unicodedata.normalize("NFC", carry + decoder.decode(raw))
                    emit, carry = _safe_nfc_split(normalized)
                    if first:
                        emit = emit.lstrip("\ufeff")
                        first = False
                    cleaned = _WHITESPACE_RE.sub("", emit)
                    if cleaned:
                        digest.update(cleaned.encode("utf-8"))
                final = unicodedata.normalize("NFC", carry + decoder.decode(b"", final=True))
                if first:
                    final = final.lstrip("\ufeff")
                final = _WHITESPACE_RE.sub("", final)
                if final:
                    digest.update(final.encode("utf-8"))
                normalized_sha = digest.hexdigest()
                break
            except UnicodeDecodeError as exc:
                decode_errors.append(f"{encoding}: {exc}")
        if normalized_sha is None:
            raise RuntimeError("text decode failed: " + "; ".join(decode_errors))
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_ctime_ns, before.st_size) != (
            after.st_dev, after.st_ino, after.st_ctime_ns, after.st_size
        ):
            raise SourceIdentityChanged(f"source changed during normalized hash: {path}")
        return _identity(after, raw_sha), normalized_sha
    finally:
        os.close(fd)
        os.close(parent_fd)


def inspect_epub_content(
    path,
    *,
    max_members=10_000,
    max_file_bytes=None,
    max_uncompressed_bytes=1024 * 1024 * 1024,
    budget=None,
):
    """Hash normalized EPUB member names and uncompressed bytes, not ZIP headers.

    ZIP timestamps, compression levels, central-directory layout, and comments do
    not affect this digest.  The archive is never extracted.  Duplicate member
    names, encrypted members, symlinks, and expansion-limit violations fail
    closed so this digest can be used as mutation evidence.
    """

    parent_fd, fd, _ = _open_regular_nofollow(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError(f"source is not a regular file: {path}")
        if max_file_bytes is not None and before.st_size > int(max_file_bytes):
            raise RuntimeError(
                f"EPUB file limit exceeded: {before.st_size}>{int(max_file_bytes)}"
            )
        if budget is not None:
            budget.reserve_pass(before.st_size)
        raw_sha = _hash_fd(fd)
        if budget is not None:
            budget.consume(before.st_size)
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb", closefd=True) as stream:
            with zipfile.ZipFile(stream) as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                if len(infos) > int(max_members):
                    raise RuntimeError(
                        f"EPUB member limit exceeded: {len(infos)}>{max_members}"
                    )
                normalized_names = [
                    unicodedata.normalize("NFC", info.filename) for info in infos
                ]
                if len(normalized_names) != len(set(normalized_names)):
                    raise RuntimeError("EPUB contains duplicate normalized member names")
                uncompressed_size = sum(int(info.file_size) for info in infos)
                if uncompressed_size > int(max_uncompressed_bytes):
                    raise RuntimeError(
                        "EPUB uncompressed limit exceeded: "
                        f"{uncompressed_size}>{max_uncompressed_bytes}"
                    )
                if budget is not None:
                    budget.reserve_pass(uncompressed_size)

                digest = hashlib.sha256()
                ordered = sorted(zip(normalized_names, infos), key=lambda item: item[0])
                for normalized_name, info in ordered:
                    if info.flag_bits & 0x1:
                        raise RuntimeError("encrypted EPUB member is unsupported")
                    member_mode = (info.external_attr >> 16) & 0o170000
                    if member_mode == stat.S_IFLNK:
                        raise RuntimeError("EPUB symlink member is unsupported")
                    name_bytes = normalized_name.encode("utf-8", "surrogatepass")
                    digest.update(struct.pack(">Q", len(name_bytes)))
                    digest.update(name_bytes)
                    digest.update(struct.pack(">Q", int(info.file_size)))
                    consumed = 0
                    with archive.open(info, "r") as member:
                        while True:
                            chunk = member.read(1024 * 1024)
                            if not chunk:
                                break
                            consumed += len(chunk)
                            if budget is not None:
                                budget.consume(len(chunk))
                            digest.update(chunk)
                    if consumed != int(info.file_size):
                        raise RuntimeError(
                            f"EPUB member size changed while read: {normalized_name}"
                        )

        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_ctime_ns,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_ctime_ns,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SourceIdentityChanged(f"source changed during EPUB hash: {path}")
        return EpubContentEvidence(
            file_evidence=_identity(after, raw_sha),
            content_sha256=digest.hexdigest(),
            member_count=len(infos),
            uncompressed_size=uncompressed_size,
        )
    finally:
        os.close(fd)
        os.close(parent_fd)


def evidence_matches(left: FileEvidence, right: FileEvidence) -> bool:
    return (
        left.dev, left.ino, left.ctime_ns, left.size, left.mtime_ns, left.sha256
    ) == (
        right.dev, right.ino, right.ctime_ns, right.size, right.mtime_ns, right.sha256
    )


def copy_no_clobber(source, destination, *, expected: FileEvidence | None = None):
    """Exclusively copy and verify a destination while leaving source intact."""
    source = Path(source)
    destination = Path(destination)
    source_parent_fd, source_fd, source_leaf = _open_regular_nofollow(source)
    parent_fd = None
    destination_fd = None
    created = False
    created_identity = None
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise RuntimeError(f"source is not a regular file: {source}")
        source_evidence = _identity(source_stat, _hash_fd(source_fd))
        if expected is not None and not evidence_matches(source_evidence, expected):
            raise SourceIdentityChanged(f"source identity changed: {source}")

        parent_fd, destination_leaf = _open_parent_nofollow(destination, create=True)
        parent_stat = os.fstat(parent_fd)

        def assert_parent_path():
            current_parent = os.stat(destination.parent, follow_symlinks=False)
            if (current_parent.st_dev, current_parent.st_ino) != (
                parent_stat.st_dev, parent_stat.st_ino,
            ):
                raise SourceIdentityChanged(
                    f"destination parent identity changed: {destination.parent}"
                )

        assert_parent_path()
        destination_fd = os.open(
            destination_leaf,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(source_stat.st_mode),
            dir_fd=parent_fd,
        )
        created = True
        created_identity = os.fstat(destination_fd)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
        os.utime(
            destination_leaf,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        destination_stat = os.fstat(destination_fd)
        destination_evidence = _identity(destination_stat, _hash_fd(destination_fd))
        if destination_evidence.sha256 != source_evidence.sha256:
            raise RuntimeError(f"destination verification failed: {destination}")

        assert_parent_path()
        current_destination = os.stat(
            destination_leaf, dir_fd=parent_fd, follow_symlinks=False
        )
        if (current_destination.st_dev, current_destination.st_ino) != (
            destination_evidence.dev, destination_evidence.ino,
        ):
            raise SourceIdentityChanged(f"destination pathname identity changed: {destination}")
        current_source = os.stat(source_leaf, dir_fd=source_parent_fd, follow_symlinks=False)
        if (current_source.st_dev, current_source.st_ino, current_source.st_ctime_ns) != (
            source_evidence.dev, source_evidence.ino, source_evidence.ctime_ns,
        ):
            raise SourceIdentityChanged(f"source pathname identity changed: {source}")
        os.close(destination_fd)
        destination_fd = None
        return CopiedFile(source, destination, source_evidence, destination_evidence)
    except Exception:
        if destination_fd is not None:
            os.close(destination_fd)
        if created and created_identity is not None:
            try:
                if parent_fd is None:
                    raise FileNotFoundError(destination)
                current = os.stat(
                    destination_leaf, dir_fd=parent_fd, follow_symlinks=False
                )
                if (current.st_dev, current.st_ino) == (
                    created_identity.st_dev, created_identity.st_ino
                ):
                    os.unlink(destination_leaf, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(source_fd)
        os.close(source_parent_fd)


def consume_copied_source(copied: CopiedFile, *, guard=None):
    """Consume source only after the durable journal owns the copied destination.

    The application contract forbids external changes to managed roots while an
    actual command holds the global lock.  These checks additionally fail closed
    if a pathname replacement is observed at either endpoint.
    """
    destination_now = inspect_regular_file(copied.destination)
    if not evidence_matches(destination_now, copied.destination_evidence):
        raise SourceIdentityChanged(
            f"destination pathname identity changed: {copied.destination}"
        )
    source_now = inspect_regular_file(copied.source)
    if not evidence_matches(source_now, copied.source_evidence):
        raise SourceIdentityChanged(f"source identity changed: {copied.source}")
    if guard is not None:
        guard()
    unlink_owned(copied.source, expected=copied.source_evidence)
    destination_after = inspect_regular_file(copied.destination)
    if not evidence_matches(destination_after, copied.destination_evidence):
        raise SourceIdentityChanged(
            f"destination changed while source was consumed: {copied.destination}"
        )


def move_no_clobber(source, destination, *, expected: FileEvidence | None = None):
    """Compatibility wrapper for journaled recovery paths.

    New mutation sinks must call copy_no_clobber(), durably record destination
    evidence, then call consume_copied_source().
    """
    copied = copy_no_clobber(source, destination, expected=expected)
    consume_copied_source(copied)
    return copied.source_evidence, copied.destination_evidence


def unlink_owned(path, *, expected: FileEvidence):
    """Unlink only while the pathname still names the inspected inode/content."""
    path = Path(path)
    current = inspect_regular_file(path)
    if not evidence_matches(current, expected):
        raise SourceIdentityChanged(f"unlink source identity changed: {path}")
    parent_fd, leaf = _open_parent_nofollow(path)
    final = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    if (final.st_dev, final.st_ino, final.st_ctime_ns) != (
        expected.dev, expected.ino, expected.ctime_ns
    ):
        os.close(parent_fd)
        raise SourceIdentityChanged(f"unlink pathname identity changed: {path}")
    try:
        os.unlink(leaf, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def evidence_dict(prefix: str, evidence: FileEvidence):
    return {f"{prefix}_{key}": value for key, value in asdict(evidence).items()}
