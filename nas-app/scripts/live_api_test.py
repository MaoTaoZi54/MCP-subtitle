#!/usr/bin/env python3
"""已安装应用的真实 API 冒烟测试（在 NAS 上运行，使用 stdlib）。

用法：
    LIVE_API_TOKEN=... python scripts/live_api_test.py [http://127.0.0.1:8669]

只读工具 + dry-run 写工具 + 写门拒绝 + 异步任务落盘；不会真正改动媒体文件。
"""

from __future__ import annotations

import http.cookiejar as http_cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LIVE_API_BASE", "http://127.0.0.1:8669")
TOKEN = os.environ["LIVE_API_TOKEN"]
ROOT = "/vol1/1000/move"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    tag = "PASS" if ok else "FAIL"
    print(f"{tag} {name}" + (f" :: {detail[:500]}" if detail else ""), flush=True)


def info(name: str, detail: str = "") -> None:
    print(f"INFO {name}" + (f" :: {detail[:800]}" if detail else ""), flush=True)


def http(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    auth: bool = True,
    headers: dict[str, str] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: float = 120,
) -> tuple[int, object]:
    hdrs = dict(headers or {})
    if auth and TOKEN:
        hdrs["Authorization"] = "Bearer " + TOKEN
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    send = opener.open if opener else urllib.request.urlopen
    try:
        with send(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw.decode("utf-8", "replace")


def tool(name: str, args: dict | None = None, timeout: float = 120) -> tuple[int, dict]:
    status, payload = http("POST", f"/api/tools/{name}", args or {}, timeout=timeout)
    return status, payload if isinstance(payload, dict) else {"ok": False, "raw": payload}


def err_of(payload: object) -> str:
    if not isinstance(payload, dict):
        return str(payload)
    err = payload.get("error") or {}
    if isinstance(err, dict):
        return f"{err.get('code')}: {err.get('message')}"
    return str(err)


def submit_job(name: str, args: dict) -> dict | None:
    status, payload = http("POST", "/api/jobs", {"tool": name, "args": args}, timeout=30)
    if status != 202 or not isinstance(payload, dict):
        check(f"任务提交 {name}", False, f"HTTP {status} {err_of(payload)}")
        return None
    job = payload.get("job") or {}
    info(f"任务提交 {name}", f"{job.get('id')} status={job.get('status')}")
    return job


def wait_job(job: dict, timeout: float = 240) -> dict | None:
    jid = job.get("id")
    deadline = time.time() + timeout
    latest: dict | None = None
    while time.time() < deadline:
        status, payload = http("GET", f"/api/jobs/{jid}?wait=20", timeout=45)
        if status != 200 or not isinstance(payload, dict):
            return None
        latest = payload.get("job")
        if latest and latest.get("status") in {"succeeded", "failed"}:
            return latest
    return latest


def job_line(job: dict | None) -> str:
    if not job:
        return "no-job"
    res = job.get("result") or {}
    bits = [f"status={job.get('status')}", f"{job.get('duration_ms')}ms"]
    if job.get("error"):
        bits.append("error=" + err_of(job))
    for key in ("returned", "total_matched", "files", "scanned", "summary", "plan", "operations"):
        if key in res:
            val = res[key]
            bits.append(f"{key}={len(val) if isinstance(val, (list, dict)) else val}")
    return " ".join(bits)


def expect_job(name: str, args: dict, expect: str = "succeeded", timeout: float = 240) -> dict | None:
    job = submit_job(name, args)
    if not job:
        return None
    done = wait_job(job, timeout=timeout)
    state = (done or {}).get("status")
    if expect == "any":
        check(f"任务 {name}", done is not None, job_line(done))
    else:
        check(f"任务 {name}", state == expect, job_line(done))
    return done


def pick_media() -> tuple[str, str, str, str, str]:
    status, payload = tool(
        "nas_find_files",
        {"root": ROOT, "extensions": [".mkv", ".mp4"], "recursive": True, "limit": 100},
    )
    matches = payload.get("matches") or []
    if status != 200 or not matches:
        raise SystemExit(f"无法发现媒体文件: HTTP {status} {err_of(payload)}")
    episode = next((m["path"] for m in matches if "Dark Matter" in m["path"]), matches[0]["path"])
    small = next((m["path"] for m in matches if m["size_bytes"] < 1 << 30), matches[-1]["path"])
    season_dir = os.path.dirname(episode)
    series_dir = os.path.dirname(season_dir) if os.path.basename(season_dir).lower().startswith("season") else season_dir
    counts: dict[str, int] = {}
    for m in matches:
        parent = os.path.dirname(m["path"])
        counts[parent] = counts.get(parent, 0) + 1
    single_dir = next((d for d, n in counts.items() if n == 1), season_dir)
    return episode, small, season_dir, series_dir, single_dir


def series_title(path: str) -> str:
    base = os.path.basename(path)
    return base.split(" (")[0].strip() or base


def main() -> None:
    # ---------------- 鉴权 / CSRF ----------------
    status, payload = http("GET", "/api/meta", auth=False)
    check("未带令牌访问 /api/meta 返回 401", status == 401, f"HTTP {status}")
    status, _ = http("GET", "/api/meta", auth=False, headers={"Authorization": "Bearer wrong-token"})
    check("错误令牌返回 401", status == 401, f"HTTP {status}")
    status, payload = http("GET", "/api/meta")
    check("Bearer 令牌访问 /api/meta", status == 200 and isinstance(payload, dict) and payload.get("ok") is True, f"HTTP {status}")

    status, payload = http("GET", "/api/tools")
    names = [t["name"] for t in (payload or {}).get("tools", [])] if status == 200 else []
    check("工具目录包含 28 个工具", len(names) == 28, f"count={len(names)}")

    status, payload = http("GET", "/api/config")
    cfg = payload or {}
    check(
        "读取配置与白名单",
        status == 200 and cfg.get("allowed_paths") == [ROOT] and cfg.get("enable_write") is False,
        f"HTTP {status} paths={cfg.get('allowed_paths')} write={cfg.get('enable_write')}",
    )

    # Cookie 登录 + CSRF 头校验
    jar = http_cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    form = urllib.parse.urlencode({"token": TOKEN}).encode("utf-8")
    req = urllib.request.Request(BASE + "/login", data=form, method="POST")
    try:
        with opener.open(req, timeout=30) as resp:
            login_status = resp.status
    except urllib.error.HTTPError as exc:
        login_status = exc.code
    status, _ = http("GET", "/api/config", auth=False, opener=opener)
    check("表单登录后 Cookie 可访问 API", login_status in (200, 303) and status == 200, f"login={login_status} api={status}")
    status, payload = http(
        "POST", "/api/tools/nas_list_directory", {"path": ROOT, "limit": 5}, auth=False, opener=opener
    )
    check("Cookie 写请求缺少 X-Nas-App 被拒", status == 403 and "csrf" in err_of(payload), f"HTTP {status} {err_of(payload)}")
    status, payload = http(
        "POST",
        "/api/tools/nas_list_directory",
        {"path": ROOT, "limit": 5},
        auth=False,
        opener=opener,
        headers={"X-Nas-App": "1"},
    )
    check("Cookie 写请求带 X-Nas-App 成功", status == 200 and payload.get("ok") is True, f"HTTP {status} {err_of(payload)}")

    # 未知工具
    status, payload = http("POST", "/api/tools/not_a_tool", {})
    check("未知工具返回 404", status == 404, f"HTTP {status} {err_of(payload)}")

    # ---------------- 发现媒体 ----------------
    episode, small, season_dir, series_dir, single_dir = pick_media()
    info("测试媒体", f"episode={episode} small={small} season={season_dir} series={series_dir} single={single_dir}")

    # ---------------- 只读工具 ----------------
    status, payload = tool("nas_list_directory", {"path": ROOT, "limit": 20})
    check("目录浏览根目录", status == 200 and payload.get("ok") is True and payload.get("returned", 0) > 0,
          f"returned={payload.get('returned')}")

    status, payload = tool("nas_find_files", {"root": ROOT, "extensions": [".mkv"], "recursive": True, "limit": 10})
    check("文件搜索（扩展名）", status == 200 and payload.get("ok") is True and payload.get("total_matched", 0) > 0,
          f"total={payload.get('total_matched')}")

    status, payload = tool("nas_disk_usage", {"path": ROOT})
    check("存储空间统计", status == 200 and payload.get("ok") is True, f"keys={list(payload)[:8]}")

    status, payload = tool("nas_get_config", {})
    check("工具读取配置", status == 200 and payload.get("ok") is True, f"HTTP {status}")

    status, payload = tool("nas_check_subtitle", {"path": episode})
    check("字幕检测", status == 200 and payload.get("ok") is True, f"subtitles={payload.get('subtitles')}")

    status, payload = tool("nas_classify_region", {"path": episode})
    check("区域分类统计", status == 200 and payload.get("ok") is True, f"HTTP {status} {err_of(payload)}")

    status, payload = tool("nas_list_directory", {"path": "/vol1/1000"})
    check("白名单越界被拒", status == 200 and payload.get("ok") is False and "access_denied" in err_of(payload),
          f"HTTP {status} {err_of(payload)}")

    status, payload = tool("resolve_media", {"query": "Dark Matter", "year": 2024})
    info("片名解析（TMDB 已关闭）", f"HTTP {status} ok={payload.get('ok')} {err_of(payload) if payload.get('ok') is False else ''}")

    # ---------------- 外部搜索（网络/站点可用性，失败不算应用缺陷） ----------------
    for name, args in (
        ("seedhub_search", {"keyword": "沙丘"}),
        ("dysbk_search", {"query": "沙丘", "limit": 5}),
    ):
        status, payload = tool(name, args, timeout=90)
        info(f"外部搜索 {name}", f"HTTP {status} ok={payload.get('ok')} {'' if payload.get('ok') else err_of(payload)}")

    # ---------------- 写门拒绝（真实写、未开闸） ----------------
    gate_cases = [
        ("organize_media", {"path": series_dir, "dry_run": False}),
        ("media_fill_subtitles", {"path": season_dir, "dry_run": False}),
        ("media_mux_subtitles", {"video_path": episode, "dry_run": False}),
        ("subhd_download", {"video_path": episode, "sid": "1"}),
    ]
    gate_marks = ("未启用", "enable_", "write_disabled", "permission_denied")
    for name, args in gate_cases:
        status, payload = tool(name, args, timeout=60)
        err = err_of(payload)
        check(
            f"写门拒绝 {name}",
            payload.get("ok") is False and any(mark in err for mark in gate_marks),
            f"HTTP {status} {err}",
        )

    # 删除门是软拒绝：返回计划但 execution.performed=false + blocked_reason，绝不删文件
    status, payload = tool("media_cleanup", {"path": season_dir, "dry_run": False}, timeout=60)
    execution = payload.get("execution") or {}
    check(
        "删除门拒绝 media_cleanup",
        payload.get("ok") is True
        and execution.get("performed") is False
        and execution.get("blocked_reason") == "enable_delete_disabled"
        and execution.get("deleted") == [],
        f"HTTP {status} blocked={execution.get('blocked_reason')} deleted={execution.get('deleted')}",
    )

    # ---------------- 异步长任务 ----------------
    title = series_title(series_dir)
    expect_job("nas_scan_media", {"path": season_dir, "recursive": True, "probe": True, "limit": 3})
    expect_job("media_quality_analyze", {"paths": [small]})
    expect_job("duplicate_media_detect", {"paths": [small, episode]})
    expect_job("media_library_audit", {"path": season_dir})
    missing = expect_job("media_missing_episodes", {"path": series_dir, "limit": 50}, expect="any")
    if missing and missing.get("status") == "failed":
        info("缺集分析（TMDB 关闭时允许降级失败）", err_of(missing))
    expect_job("media_integrity_check", {"paths": [small], "mode": "basic"})
    expect_job("find_media", {"title": title}, expect="any")
    expect_job("media_search", {"query": title}, expect="any")
    expect_job("subhd_search", {"video_path": episode}, expect="any")
    jackett = expect_job("jackett_search", {"query": "Dune", "limit": 5}, expect="any")
    if jackett and jackett.get("status") == "failed":
        info("Jackett（配置未启用）", err_of(jackett))
    expect_job("media_cleanup", {"path": season_dir, "dry_run": True})
    expect_job("organize_media", {"path": series_dir, "dry_run": True, "mode": "all"})
    expect_job("media_fill_subtitles", {"path": single_dir, "dry_run": True})
    expect_job("media_mux_subtitles", {"video_path": episode, "dry_run": True})
    expect_job("media_rollback", {"dry_run": True}, expect="any")

    # ---------------- 并发任务落盘压力 ----------------
    rapid = [submit_job("nas_list_directory", {"path": ROOT, "limit": 5}) for _ in range(6)]
    ids = [j["id"] for j in rapid if j]
    ok = True
    for job in rapid:
        if not job:
            ok = False
            continue
        done = wait_job(job, timeout=60)
        if not done or done.get("status") != "succeeded":
            ok = False
            info("并发任务", job_line(done))
    check("并发提交 6 个任务全部成功", ok and len(ids) == 6, f"ids={ids}")
    status, payload = http("GET", "/api/jobs?limit=50")
    listed = {j["id"] for j in (payload or {}).get("jobs", [])} if status == 200 else set()
    check("任务列表包含并发任务", set(ids) <= listed, f"listed={len(listed)}")

    # ---------------- 日志接口 ----------------
    status, payload = http("GET", "/api/logs?file=app&lines=100")
    check(
        "日志接口可用",
        status == 200 and isinstance(payload, dict) and payload.get("ok") is True and isinstance(payload.get("lines"), list),
        f"HTTP {status} keys={list(payload)[:6] if isinstance(payload, dict) else ''}",
    )

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\nSUMMARY total={len(RESULTS)} passed={len(RESULTS) - len(failed)} failed={len(failed)}", flush=True)
    if failed:
        print("FAILED: " + "; ".join(failed), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
