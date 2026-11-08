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

from nas_mcp.progress import bind_control, mark_partial, reset_control

from .toolkit import ToolRegistry

logger = logging.getLogger("nas_app.jobs")

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
# 进度落盘节流：内存更新每次上报都做（廉价），磁盘最多每 2 秒一次
PROGRESS_SAVE_INTERVAL = 2.0
QUEUED_CANCEL_RESULT = {
    "ok": True,
    "canceled": True,
    "partial": True,
    "message": "排队时已取消，未执行任何操作",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JobCancelError(Exception):
    code = "cancel_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class JobNotFound(JobCancelError):
    code = "not_found"


class JobFinished(JobCancelError):
    code = "already_finished"


@dataclass
class Job:
    id: str
    tool: str
    title: str
    args: dict[str, Any]
    status: str = "queued"  # queued / running / succeeded / failed / canceled
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    # 进度：stage/label/done/total/current/updated_at；老任务没有这个字段时读成 None
    progress: dict[str, Any] | None = None
    cancel_requested: bool = False
    canceled_at: str | None = None
    # 运行时对象，不落盘
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    last_progress_save: float = field(default=0.0, repr=False, compare=False)

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
            "progress": self.progress,
            "cancel_requested": self.cancel_requested,
            "canceled_at": self.canceled_at,
        }


class _JobControl:
    """注入给长工具循环的进度/取消句柄；report 只改内存，不直接落盘。"""

    def __init__(self, manager: "JobManager", job: Job):
        self._manager = manager
        self._job = job

    def report(
        self,
        *,
        stage: str,
        label: str,
        done: int | None = None,
        total: int | None = None,
        current: str | None = None,
    ) -> None:
        self._manager._report(self._job, stage, label, done, total, current)

    def canceled(self) -> bool:
        return self._job.cancel_requested or self._job.cancel_event.is_set()


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
            if job.status in TERMINAL_STATUSES:
                return job
            if time.monotonic() >= deadline:
                return job
            time.sleep(0.2)

    def cancel(self, job_id: str) -> Job:
        """合作式取消：排队任务直接结束，运行中任务置标记等工具在边界停下。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"任务不存在: {job_id}")
            if job.status in TERMINAL_STATUSES:
                raise JobFinished(f"任务已结束（{job.status}），不能再取消")
            job.cancel_requested = True
            job.cancel_event.set()
            if job.progress is None:
                job.progress = {
                    "stage": "queued",
                    "label": "排队中",
                    "done": 0,
                    "total": None,
                    "current": None,
                    "updated_at": _now(),
                }
            if job.status == "queued":
                job.status = "canceled"
                job.canceled_at = _now()
                job.finished_at = job.canceled_at
                job.duration_ms = 0
                job.result = dict(QUEUED_CANCEL_RESULT)
                job.progress = {
                    "stage": "canceled",
                    "label": "已取消",
                    "done": 0,
                    "total": None,
                    "current": None,
                    "updated_at": job.canceled_at,
                }
        self._save(job)
        return job

    def _report(
        self,
        job: Job,
        stage: str,
        label: str,
        done: int | None,
        total: int | None,
        current: str | None,
    ) -> None:
        """进度上报：内存更新廉价，磁盘写按 2 秒节流。"""
        save = False
        with self._lock:
            if job.status not in {"queued", "running"}:
                return
            now = _now()
            job.progress = {
                "stage": stage,
                "label": label,
                "done": done,
                "total": total,
                "current": current,
                "updated_at": now,
            }
            monotonic_now = time.monotonic()
            if monotonic_now - job.last_progress_save >= PROGRESS_SAVE_INTERVAL:
                job.last_progress_save = monotonic_now
                save = True
        if save:
            self._save(job)

    # ------------------------------------------------------------------ 内部
    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None:
                continue
            with self._lock:
                if job.status != "queued":
                    # 排队阶段已被取消，直接跳过，不执行工具
                    continue
                job.status = "running"
                job.started_at = _now()
                job.progress = {
                    "stage": "start",
                    "label": "开始执行",
                    "done": 0,
                    "total": None,
                    "current": None,
                    "updated_at": job.started_at,
                }
            self._save(job)
            started = time.monotonic()
            control = _JobControl(self, job)
            token = bind_control(control)
            result: dict[str, Any] | None = None
            error: dict[str, Any] | None = None
            try:
                result = self.registry.call(job.tool, job.args)
            except Exception as exc:  # noqa: BLE001 - 任务线程边界
                logger.exception("任务 %s 执行异常", job.id)
                error = {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}
            finally:
                reset_control(token)
                with self._lock:
                    if job.cancel_requested:
                        # 取消优先：保留已完成部分，结果打上 canceled/partial 标记
                        job.status = "canceled"
                        job.canceled_at = job.canceled_at or _now()
                        if result is None and error is not None:
                            result = {"ok": False, "error": error}
                        mark_partial(result)
                        job.result = result
                        job.error = None
                    elif error is not None:
                        job.status = "failed"
                        job.error = error
                        job.result = result
                    else:
                        job.result = result
                        ok = result.get("ok", True) if isinstance(result, dict) else True
                        job.status = "succeeded" if ok else "failed"
                        if job.status == "failed" and isinstance(result, dict):
                            job.error = result.get("error")
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
                    progress=data.get("progress") if isinstance(data.get("progress"), dict) else None,
                    cancel_requested=bool(data.get("cancel_requested")),
                    canceled_at=data.get("canceled_at"),
                )
            except (OSError, ValueError, KeyError):
                continue
            if job.status in {"queued", "running"}:
                if job.cancel_requested:
                    # 取消已请求但进程先退出：落为已取消，不回到进行中
                    job.status = "canceled"
                    job.canceled_at = job.canceled_at or job.finished_at or _now()
                    job.finished_at = job.canceled_at
                    mark_partial(job.result)
                else:
                    job.status = "failed"
                    job.error = {"code": "interrupted", "message": "应用重启，任务已中断"}
                    job.finished_at = _now()
                self._save(job)
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._order.reverse()
