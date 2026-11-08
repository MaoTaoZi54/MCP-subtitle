import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_concurrent_saves_are_serialized(jobs, monkeypatch):
    from nas_app.jobs import Job

    job = Job(id="J-20260101-000000-abcd", tool="nas_list_directory", title="并发保存", args={})
    original = Path.write_text
    state = {"active": 0, "peak": 0}
    guard = threading.Lock()

    def slow_write(self, data, **kwargs):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(0.01)
            return original(self, data, **kwargs)
        finally:
            with guard:
                state["active"] -= 1

    monkeypatch.setattr(Path, "write_text", slow_write)
    threads = [
        threading.Thread(target=lambda: [jobs._save(job) for _ in range(5)])
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["peak"] == 1
    saved = jobs.dir / f"{job.id}.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["id"] == job.id
    assert not [p for p in jobs.dir.iterdir() if p.name.endswith(".tmp")]


def test_job_succeeds_and_persists(jobs, settings):
    media = settings.config_path.parent.parent / "media"
    job = jobs.submit("nas_list_directory", {"path": str(media)})
    done = jobs.wait(job.id, timeout=10)
    assert done is not None
    assert done.status == "succeeded", done.error
    assert done.result["ok"] is True
    assert done.duration_ms is not None
    saved = settings.data_dir / "jobs" / f"{job.id}.json"
    assert saved.is_file()
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["status"] == "succeeded"


def test_job_failure_is_recorded(jobs, settings):
    job = jobs.submit("nas_list_directory", {"path": str(settings.data_dir)})
    done = jobs.wait(job.id, timeout=10)
    assert done.status == "failed"
    assert done.error and done.error["code"]


def test_job_normalizes_legacy_string_error(jobs, settings):
    media = settings.config_path.parent.parent / "media"
    job = jobs.submit(
        "media_mux_subtitles",
        {"video_path": str(media / "movie.mkv"), "dry_run": False},
    )
    done = jobs.wait(job.id, timeout=10)
    assert done.status == "failed"
    assert done.error["code"] == "write_disabled"
    saved = json.loads(
        (settings.data_dir / "jobs" / f"{job.id}.json").read_text(encoding="utf-8")
    )
    assert saved["error"]["code"] == "write_disabled"


def test_jobs_list_newest_first(jobs, settings):
    media = settings.config_path.parent.parent / "media"
    first = jobs.submit("nas_list_directory", {"path": str(media)})
    jobs.wait(first.id, 10)
    time.sleep(0.01)
    second = jobs.submit("nas_list_directory", {"path": str(media)})
    jobs.wait(second.id, 10)
    listed = jobs.list(10)
    assert listed[0].id == second.id


def test_as_payload_matches_plugin_flat_shape(jobs, settings):
    media = settings.config_path.parent.parent / "media"
    job = jobs.submit("nas_list_directory", {"path": str(media)})
    done = jobs.wait(job.id, timeout=10)
    assert done is not None and done.status == "succeeded"

    full = jobs.as_payload(done)
    assert full["pending"] is False
    assert full["ok"] is True
    assert full["partial"] is False
    assert full["result_stored"] is False
    assert isinstance(full["result"], dict)

    listed = jobs.as_payload(done, include_result=False)
    assert listed["result"] is None, "列表接口不得内联 result"
    assert listed["result_stored"] is False


def test_large_result_stored_to_file(jobs):
    from nas_app.jobs import RESULT_STORE_THRESHOLD, Job

    blob = "x" * (RESULT_STORE_THRESHOLD + 100)
    job = Job(
        id="J-20260101-000000-big1",
        tool="nas_scan_media",
        title="大结果",
        args={},
        status="succeeded",
        result={"ok": True, "blob": blob},
    )
    jobs._save(job)

    assert job.result_file, "超阈值结果应落盘"
    assert job.result_size and job.result_size > RESULT_STORE_THRESHOLD
    saved = json.loads((jobs.dir / f"{job.id}.json").read_text(encoding="utf-8"))
    assert saved["result"] is None
    assert saved["result_file"] and Path(saved["result_file"]).exists()

    payload = jobs.as_payload(job)
    assert payload["result_stored"] is True
    assert payload["result"]["blob"] == blob

    listed = jobs.as_payload(job, include_result=False)
    assert listed["result"] is None
    assert listed["result_stored"] is True


def test_interrupted_jobs_marked_interrupted(jobs, settings, registry):
    from nas_app.jobs import JobManager

    path = settings.data_dir / "jobs" / "J-20260101-000000-dead.json"
    path.write_text(json.dumps({
        "id": "J-20260101-000000-dead",
        "tool": "nas_list_directory",
        "title": "读取目录",
        "args": {},
        "status": "running",
    }), encoding="utf-8")
    reloaded = JobManager(registry, settings.data_dir, max_workers=1)
    job = reloaded.get("J-20260101-000000-dead")
    assert job is not None
    assert job.status == "interrupted"
    assert job.error["code"] == "interrupted"


def test_legacy_job_without_progress_loads(jobs, settings, registry):
    from nas_app.jobs import JobManager

    path = jobs.dir / "J-20260101-000000-old1.json"
    path.write_text(json.dumps({
        "id": "J-20260101-000000-old1",
        "tool": "nas_list_directory",
        "title": "历史任务",
        "args": {},
        "status": "succeeded",
        "created_at": "2026-01-01T00:00:00+08:00",
        "finished_at": "2026-01-01T00:00:01+08:00",
        "duration_ms": 1000,
        "result": {"ok": True, "entries": []},
    }), encoding="utf-8")
    reloaded = JobManager(registry, settings.data_dir, max_workers=1)
    job = reloaded.get("J-20260101-000000-old1")
    assert job is not None
    assert job.status == "succeeded"
    assert job.progress is None
    public = job.public()
    assert public["cancel_requested"] is False
    assert public["canceled_at"] is None
    assert public["result"]["ok"] is True


def test_progress_reported_and_persisted(jobs, settings, monkeypatch):
    from nas_mcp.progress import check_canceled, report

    def fake_call(name, args):
        for i in range(1, 5):
            if check_canceled():
                return {"ok": True, "done": i - 1}
            report("work", "处理条目", done=i - 1, total=4, current=f"item{i}")
            time.sleep(0.15)
        report("work", "处理条目", done=4, total=4)
        return {"ok": True, "done": 4}

    monkeypatch.setattr(jobs.registry, "call", fake_call)
    job = jobs.submit("nas_list_directory", {"path": "/tmp"})
    deadline = time.monotonic() + 5
    seen = None
    while time.monotonic() < deadline:
        current = jobs.get(job.id)
        if current and current.progress and current.progress["total"] == 4:
            seen = current.progress
            break
        time.sleep(0.03)
    assert seen is not None, "没有在运行中看到进度"
    assert seen["label"] == "处理条目"

    done = jobs.wait(job.id, timeout=10)
    assert done.status == "succeeded"
    assert done.progress["done"] == 4
    saved = json.loads((jobs.dir / f"{job.id}.json").read_text(encoding="utf-8"))
    assert saved["progress"]["done"] == 4
    assert saved["progress"]["stage"] == "work"


def test_cancel_running_job_keeps_partial_result(jobs, settings, monkeypatch):
    from nas_mcp.progress import check_canceled, report

    def fake_call(name, args):
        for i in range(1, 11):
            if check_canceled():
                return {"ok": True, "done": i - 1}
            report("work", "处理条目", done=i - 1, total=10, current=f"item{i}")
            time.sleep(0.1)
        return {"ok": True, "done": 10}

    monkeypatch.setattr(jobs.registry, "call", fake_call)
    job = jobs.submit("nas_list_directory", {"path": "/tmp"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = jobs.get(job.id)
        if current and current.progress and current.progress["done"]:
            break
        time.sleep(0.03)
    queued_or_running = jobs.get(job.id)
    assert queued_or_running.status in {"running", "queued"}

    returned = jobs.cancel(job.id)
    assert returned.cancel_requested is True

    done = jobs.wait(job.id, timeout=10)
    assert done.status == "canceled"
    assert done.canceled_at
    assert done.result["canceled"] is True
    assert done.result["partial"] is True
    assert done.result["done"] < 10
    saved = json.loads((jobs.dir / f"{job.id}.json").read_text(encoding="utf-8"))
    assert saved["status"] == "canceled"
    assert saved["cancel_requested"] is True


def test_cancel_queued_job_is_immediate(settings):
    from nas_app.jobs import JobManager

    class BlockingRegistry:
        def get_spec(self, name):
            return SimpleNamespace(title=name)

        def call(self, name, args):
            time.sleep(1.2)
            return {"ok": True}

    data_dir = settings.data_dir / "queued-cancel"
    manager = JobManager(BlockingRegistry(), data_dir, max_workers=1)
    first = manager.submit("block", {})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if manager.get(first.id).status == "running":
            break
        time.sleep(0.03)
    second = manager.submit("block2", {})
    canceled = manager.cancel(second.id)
    assert canceled.status == "canceled"
    assert canceled.result["partial"] is True
    assert canceled.duration_ms == 0
    saved = json.loads((data_dir / "jobs" / f"{second.id}.json").read_text(encoding="utf-8"))
    assert saved["status"] == "canceled"


def test_cancel_finished_job_is_error(jobs, settings):
    from nas_app.jobs import JobFinished

    media = settings.config_path.parent.parent / "media"
    job = jobs.submit("nas_list_directory", {"path": str(media)})
    done = jobs.wait(job.id, timeout=10)
    assert done.status == "succeeded"
    with pytest.raises(JobFinished):
        jobs.cancel(job.id)
    assert jobs.get(job.id).status == "succeeded"


def test_cancel_requested_survives_restart(settings, registry):
    from nas_app.jobs import JobManager

    jobs_dir = settings.data_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    path = jobs_dir / "J-20260101-000000-cnl1.json"
    path.write_text(json.dumps({
        "id": "J-20260101-000000-cnl1",
        "tool": "nas_list_directory",
        "title": "取消中重启",
        "args": {},
        "status": "running",
        "cancel_requested": True,
        "progress": {"stage": "work", "label": "处理中", "done": 3, "total": 10,
                     "current": "a.mkv", "updated_at": "2026-01-01T00:00:00+08:00"},
    }), encoding="utf-8")
    reloaded = JobManager(registry, settings.data_dir, max_workers=1)
    job = reloaded.get("J-20260101-000000-cnl1")
    assert job.status == "canceled"
    assert job.canceled_at


def test_progress_noop_without_job_context():
    from nas_mcp.progress import check_canceled, report

    report("work", "处理条目", done=1, total=3, current="x")
    assert check_canceled() is False

