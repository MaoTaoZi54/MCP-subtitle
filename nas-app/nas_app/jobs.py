"""后台任务：长耗时工具（扫描/体检/整理/批量补字幕）异步执行。

Web UI 通过 jobs 接口提交并轮询，避免浏览器/反向代理在长请求上超时。
任务按完成顺序落盘到 <data>/jobs/<id>.json，重启后仍能查看最近结果。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .toolkit import ToolRegistry

logger = logging.getLogger("nas_app.jobs")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    tool: str
    title: str
    args: dict[str, Any]
    status: str = "queued"  # queued / running / succeeded / failed
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "title": self.title,
            "args": self.args,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "result": self.result,
            "error": self.error,
        }


class JobManager:
    def __init__(self, registry: ToolRegistry, data_dir: Path, max_workers: int = 2):
        self.registry = registry
        self.dir = Path(data_dir) / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._save_lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._workers = [
            threading.Thread(target=self._worker, name=f"nas-job-{i}", daemon=True)
            for i in range(max(1, max_workers))
        ]
        for worker in self._workers:
            worker.start()
        self._load_recent()

    # ------------------------------------------------------------------ API
    def submit(self, tool: str, args: dict[str, Any] | None = None) -> Job:
        spec = self.registry.get_spec(tool)
        job = Job(
            id=f"J-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}",
            tool=tool,
            title=spec.title,
            args=args or {},
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._queue.put(job.id)
        self._save(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            ids = self._order[-limit:][::-1]
            return [self._jobs[i] for i in ids if i in self._jobs]

    def wait(self, job_id: str, timeout: float = 25.0) -> Job | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            job = self.get(job_id)
            if job is None:
                return None
            if job.status in {"succeeded", "failed"}:
                return job
            if time.monotonic() >= deadline:
                return job
            time.sleep(0.2)

    # ------------------------------------------------------------------ 内部
    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            job.status = "running"
            job.started_at = _now()
            self._save(job)
            started = time.monotonic()
            try:
                result = self.registry.call(job.tool, job.args)
                job.result = result
                job.status = "succeeded" if result.get("ok", True) else "failed"
                if job.status == "failed":
                    job.error = result.get("error")
            except Exception as exc:  # noqa: BLE001 - 任务线程边界
                logger.exception("任务 %s 执行异常", job.id)
                job.status = "failed"
                job.error = {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
            finally:
                job.duration_ms = int((time.monotonic() - started) * 1000)
                job.finished_at = _now()
                self._save(job)

    def _save(self, job: Job) -> None:
        # 提交线程与工作线程会同时保存同一个任务：串行化并各用独立临时文件，
        # 避免共享 .json.tmp 时 replace 竞争导致 ENOENT。
        with self._save_lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                text = json.dumps(job.public(), ensure_ascii=False, default=str)
                path = self.dir / f"{job.id}.json"
                tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(path)
            except OSError as exc:
                logger.warning("任务落盘失败 %s: %s", job.id, exc)

    def _load_recent(self, limit: int = 100) -> None:
        try:
            files = sorted(self.dir.glob("J-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for path in files[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(
                    id=data["id"],
                    tool=data["tool"],
                    title=data.get("title", data["tool"]),
                    args=data.get("args") or {},
                    status=data.get("status", "failed"),
                    created_at=data.get("created_at") or _now(),
                    started_at=data.get("started_at"),
                    finished_at=data.get("finished_at"),
                    duration_ms=data.get("duration_ms"),
                    result=data.get("result"),
                    error=data.get("error"),
                )
            except (OSError, ValueError, KeyError):
                continue
            if job.status in {"queued", "running"}:
                job.status = "failed"
                job.error = {"code": "interrupted", "message": "应用重启，任务已中断"}
                job.finished_at = _now()
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._order.reverse()
