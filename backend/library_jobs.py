"""Persistent, single-worker job records for the local library server."""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping


TERMINAL_STATES = frozenset({"succeeded", "failed", "needs_review", "interrupted"})
ACTIVE_STATES = frozenset({"queued", "validating", "running", "verifying"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobActiveError(RuntimeError):
    def __init__(self, job_id: str):
        self.job_id = str(job_id)
        super().__init__(f"another job is active: {self.job_id}")


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.jobs_dir = self.root / "jobs"
        self.logs_dir = self.root / "logs"
        self.events_dir = self.root / "events"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        if not job_id or any(char not in "0123456789abcdef-" for char in job_id):
            raise ValueError("invalid job id")
        return self.jobs_dir / f"{job_id}.json"

    def _write(self, record: Mapping[str, object]) -> dict:
        value = dict(record)
        path = self._path(str(value["job_id"]))
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
        return value

    def create(self, job_type: str, payload: Mapping[str, object]) -> dict:
        with self._lock:
            job_id = str(uuid.uuid4())
            created = _now()
            record = {
                "job_id": job_id,
                "job_type": job_type,
                "state": "queued",
                "stage": "queued",
                "message": "실행 대기 중",
                "created_at": created,
                "updated_at": created,
                "started_at": None,
                "finished_at": None,
                "progress": {"current": 0, "total": 0},
                "payload": dict(payload),
                "result": None,
                "error": None,
                "log_path": str(self.logs_dir / f"{job_id}.log"),
                "event_path": str(self.events_dir / f"{job_id}.jsonl"),
                "last_event": None,
            }
            return self._write(record)

    def get(self, job_id: str) -> dict:
        with self._lock:
            path = self._path(job_id)
            if not path.is_file():
                raise KeyError(job_id)
            return json.loads(path.read_text(encoding="utf-8"))

    def update(self, job_id: str, **changes) -> dict:
        with self._lock:
            record = self.get(job_id)
            record.update(changes)
            record["updated_at"] = _now()
            return self._write(record)

    def append_log(self, job_id: str, message: str) -> None:
        record = self.get(job_id)
        line = f"{_now()} {message.rstrip()}\n"
        with self._lock:
            with Path(record["log_path"]).open("a", encoding="utf-8") as stream:
                stream.write(line)

    def append_event(self, job_id: str, event: Mapping[str, object]) -> None:
        record = self.get(job_id)
        value = {"recorded_at": _now(), **dict(event)}
        line = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        with self._lock:
            with Path(record["event_path"]).open("a", encoding="utf-8") as stream:
                stream.write(line)
            self.update(job_id, last_event=value)

    def events(self, job_id: str, *, limit: int = 500) -> list[dict]:
        record = self.get(job_id)
        path = Path(record.get("event_path") or "")
        if not path.is_file():
            return []
        values = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                values.append(value)
        return values[-max(1, min(int(limit), 2000)):]

    def list(self, *, limit: int = 50) -> list[dict]:
        with self._lock:
            records = []
            for path in self.jobs_dir.glob("*.json"):
                try:
                    records.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
            records.sort(key=lambda item: item.get("created_at") or "", reverse=True)
            return records[: max(1, min(int(limit), 200))]

    def mark_interrupted(self) -> int:
        count = 0
        for record in self.list(limit=200):
            if record.get("state") not in ACTIVE_STATES:
                continue
            self.update(
                record["job_id"],
                state="interrupted",
                stage="interrupted",
                message="서버 재시작으로 작업 상태 확인 필요",
                finished_at=_now(),
                error={
                    "code": "server_restarted",
                    "message": "작업 실행 중 서버가 종료되었습니다. operation 복구 상태를 확인하세요.",
                },
            )
            self.append_event(record["job_id"], {
                "phase": "job_interrupted",
                "status": "interrupted",
                "error_code": "server_restarted",
                "error_message": (
                    "작업 실행 중 서버가 종료되었습니다. operation 복구 상태를 확인하세요."
                ),
            })
            count += 1
        return count


class JobRunner:
    def __init__(self, store: JobStore):
        self.store = store
        self.handlers: dict[str, Callable] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="library-job")
        self._start_lock = threading.RLock()

    def register(self, job_type: str, handler: Callable) -> None:
        if job_type in self.handlers:
            raise ValueError(f"job handler already registered: {job_type}")
        self.handlers[job_type] = handler

    def start(self, job_type: str, payload: Mapping[str, object]) -> dict:
        if job_type not in self.handlers:
            raise KeyError(job_type)
        record = self.store.create(job_type, payload)
        self.executor.submit(self._run, record["job_id"])
        return record

    def start_exclusive(self, job_type: str, payload: Mapping[str, object]) -> dict:
        """Start one mutation job only when this server has no active job."""
        with self._start_lock:
            active = [
                record for record in self.store.list(limit=200)
                if record.get("state") in ACTIVE_STATES
            ]
            if active:
                raise JobActiveError(str(active[0]["job_id"]))
            return self.start(job_type, payload)

    def _run(self, job_id: str) -> None:
        record = self.store.get(job_id)
        handler = self.handlers[record["job_type"]]

        def progress(
            current: int,
            total: int,
            message: str,
            *,
            stage: str = "running",
            event: Mapping[str, object] | None = None,
        ) -> None:
            self.store.update(
                job_id,
                state="running",
                stage=stage,
                message=message,
                progress={"current": int(current), "total": int(total)},
            )
            self.store.append_log(job_id, message)
            if event is not None:
                self.store.append_event(job_id, event)

        progress.log = lambda message: self.store.append_log(job_id, str(message))

        started = _now()
        self.store.update(
            job_id,
            state="validating",
            stage="validating",
            message="실행 전 상태 확인 중",
            started_at=started,
        )
        try:
            self.store.append_log(job_id, "job started")
            result = handler(record["payload"], progress)
            terminal_state = str(result.pop("_job_state", "succeeded"))
            terminal_message = str(result.pop("_job_message", "작업 완료"))
            if terminal_state not in {"succeeded", "needs_review"}:
                raise RuntimeError(f"invalid job terminal state: {terminal_state}")
            self.store.update(
                job_id,
                state=terminal_state,
                stage=terminal_state,
                message=terminal_message,
                finished_at=_now(),
                result=result,
                error=None,
            )
            self.store.append_log(job_id, f"job {terminal_state}")
        except Exception as exc:  # noqa: BLE001 - persisted boundary
            self.store.append_log(job_id, traceback.format_exc())
            self.store.append_event(job_id, {
                "phase": "job_failed",
                "status": "failed",
                "error_code": type(exc).__name__,
                "error_message": str(exc),
            })
            self.store.update(
                job_id,
                state="failed",
                stage="failed",
                message="작업 실패",
                finished_at=_now(),
                error={"code": type(exc).__name__, "message": str(exc)},
            )

    def get(self, job_id: str) -> dict:
        return self.store.get(job_id)

    def list(self, *, limit: int = 50) -> list[dict]:
        return self.store.list(limit=limit)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False)
