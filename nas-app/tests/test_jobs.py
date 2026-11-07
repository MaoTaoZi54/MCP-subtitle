import json
import threading
import time
from pathlib import Path


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


def test_interrupted_jobs_marked_failed(jobs, settings, registry):
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
    assert job.status == "failed"
    assert job.error["code"] == "interrupted"
