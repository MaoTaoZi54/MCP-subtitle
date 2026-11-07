# -*- coding: utf-8 -*-
"""媒体完整性检查（v1.7.2 阶段一，只读）。

media_integrity_check：按 basic/deep 两种模式检查视频文件是否可播放、
容器/视频流是否健全。复用 media.probe_video，不得重写 ffprobe。

状态机（仅允许以下取值）：
  healthy     文件存在/可读/容器合法/有视频流（deep 还会做更深的健全性检查）
  warning     可解析但有次要问题（无音轨/时长异常等，仅 deep 模式产生）
  corrupted   ffprobe 无法解析 / 无视频流 / 容器非法
  incomplete  下载残留（.part/.crdownload 等扩展名）；不得因 mkv 正在增长就判 incomplete
  unknown     无法判定（probe 预算耗尽、basic 未探测到流级问题等）

权限错误（PermissionError）只进 errors，绝不当作 corrupted/missing。
probe 预算耗尽 → 该项 status=unknown + reason=probe_budget_exhausted，
顶层 probe_budget_exhausted=true。

assess_integrity 为共享逻辑，供 media_library_audit 等复用。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import media as media_tools
from .config import Limits, MediaConfig
from .fs_utils import normalize_list_arg
from .library import _collect_targets
from .security import InvalidArguments, PathGuard

logger = logging.getLogger("nas_mcp.integrity")

# 下载残留扩展名：命中即 incomplete（不能因 mkv 正在增长就判 incomplete，
# 只认显式的下载临时扩展名）
_PARTIAL_DOWNLOAD_EXTS = frozenset({"part", "crdownload", "download", "tmp"})
# 极短时长阈值：< 1s 视为异常（deep 模式 warning）
_VERY_SHORT_DURATION = 1.0


def assess_integrity(
    video: Path,
    probe: dict | None,
    probe_error: str | None,
    *,
    mode: str,
) -> dict:
    """共享完整性评估（不直接调 ffprobe，由调用方先探测再传入结果）。

    probe 为 None 且 probe_error 为 None 时（未探测，如预算耗尽）→ unknown。
    probe_error 非空（ffprobe 失败）→ corrupted。
    .part/.crdownload 等扩展名 → incomplete（优先于 probe 结果）。
    """
    suffix = video.suffix.lower().lstrip(".")
    name = video.name.lower()
    # incomplete：仅认显式下载残留扩展名；mkv 正在增长不在此列
    if suffix in _PARTIAL_DOWNLOAD_EXTS or name.endswith(".part"):
        return {
            "status": "incomplete",
            "reasons": ["partial_download_extension"],
            "container": None,
            "has_video_stream": None,
            "duration_seconds": None,
            "size_bytes": None,
            "probe_ok": False,
        }

    if probe_error:
        return {
            "status": "corrupted",
            "reasons": [probe_error],
            "container": None,
            "has_video_stream": None,
            "duration_seconds": None,
            "size_bytes": None,
            "probe_ok": False,
        }

    if probe is None:
        return {
            "status": "unknown",
            "reasons": ["not_probed"],
            "container": None,
            "has_video_stream": None,
            "duration_seconds": None,
            "size_bytes": None,
            "probe_ok": False,
        }

    container = probe.get("container")
    v = probe.get("video")
    has_video = v is not None
    audio = probe.get("audio") or []
    duration = probe.get("duration_seconds")
    size = probe.get("size_bytes")

    if not has_video:
        # 有流但缺视频流 → corrupted（如纯音频文件被当 mkv）；
        # 完全无流且无容器 → unknown（空文件/占位，无法判定）
        if not container and not audio:
            return {
                "status": "unknown",
                "reasons": ["empty_probe_result"],
                "container": container,
                "has_video_stream": False,
                "duration_seconds": duration,
                "size_bytes": size,
                "probe_ok": True,
            }
        return {
            "status": "corrupted",
            "reasons": ["no_video_stream"],
            "container": container,
            "has_video_stream": False,
            "duration_seconds": duration,
            "size_bytes": size,
            "probe_ok": True,
        }

    # 有视频流：基本 healthy；deep 模式做更深的健全性检查可降为 warning
    issues: list[str] = []
    if mode == "deep":
        if not audio:
            issues.append("no_audio_track")
        if duration is not None and duration < _VERY_SHORT_DURATION:
            issues.append("very_short_duration")
        # 容器名缺失（ffprobe 解析出流但 format 段为空）→ 警告
        if not container:
            issues.append("missing_container_format")

    if issues:
        return {
            "status": "warning",
            "reasons": issues,
            "container": container,
            "has_video_stream": True,
            "duration_seconds": duration,
            "size_bytes": size,
            "probe_ok": True,
        }

    return {
        "status": "healthy",
        "reasons": [],
        "container": container,
        "has_video_stream": True,
        "duration_seconds": duration,
        "size_bytes": size,
        "probe_ok": True,
    }


def integrity_check(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    mcfg: MediaConfig,
    *,
    path: str = "",
    paths: list[str] | None = None,
    mode: str = "basic",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """media_integrity_check 入口（只读）。

    path+paths 合并去重；都为空默认扫 media_roots。所有路径过 PathGuard。
    basic: 文件存在/可读/容器/视频流；deep: 更深 probe，遵守 max_probe_files。
    """
    paths = normalize_list_arg(paths, "paths")
    if mode not in ("basic", "deep"):
        raise InvalidArguments(f"mode 只能是 basic/deep，得到: {mode!r}")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise InvalidArguments(f"limit 必须是正整数，得到: {limit!r}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidArguments(f"offset 必须是非负整数，得到: {offset!r}")

    targets, meta = _collect_targets(
        guard, limits, video_exts, subtitle_exts, media_roots, mcfg,
        path=path, paths=paths, recursive=True,
    )

    total_collected = len(targets)
    # 分页：offset 跳过，limit 取本页
    page = targets[offset:offset + limit]
    next_offset = offset + len(page) if offset + len(page) < total_collected else None
    truncated = next_offset is not None

    errors: list[dict] = []
    items: list[dict] = []
    probe_attempts = 0
    probed_files = 0
    probe_failures = 0

    for video in page:
        # 权限/可读性：先 stat；PermissionError 只进 errors，不当 status
        try:
            st = video.stat()
        except PermissionError as exc:
            errors.append({"path": str(video), "error": f"permission_denied: {exc}"})
            continue
        except OSError as exc:
            errors.append({"path": str(video), "error": f"stat_failed: {exc}"})
            continue

        # 下载残留（.part/.crdownload 等）：直接判 incomplete，不消耗 probe 预算
        suffix = video.suffix.lower().lstrip(".")
        if suffix in _PARTIAL_DOWNLOAD_EXTS or video.name.lower().endswith(".part"):
            assessment = assess_integrity(video, None, None, mode=mode)
            items.append({
                "name": video.name,
                "path": str(video),
                "status": assessment["status"],
                "reasons": assessment["reasons"],
                "container": assessment["container"],
                "has_video_stream": assessment["has_video_stream"],
                "duration_seconds": assessment["duration_seconds"],
                "size_bytes": st.st_size,
                "probe_ok": assessment["probe_ok"],
            })
            continue

        # probe 预算（basic 与 deep 都遵守 max_probe_files；deep 是主要受约束场景）
        probe = None
        probe_error: str | None = None
        budget_exhausted = probe_attempts >= mcfg.max_probe_files
        if not budget_exhausted:
            probe_attempts += 1
            try:
                probe = media_tools.probe_video(
                    video, mcfg.ffprobe_path, mcfg.ffprobe_timeout_seconds
                )
            except media_tools.ProbeError as exc:
                probe_error = str(exc)
                probe_failures += 1
            except PermissionError as exc:
                errors.append({"path": str(video), "error": f"permission_denied: {exc}"})
                continue
            else:
                probed_files += 1
        else:
            probe_error = None  # 预算耗尽：不是 probe 错误，而是未探测

        assessment = assess_integrity(video, probe, probe_error, mode=mode)
        # 预算耗尽的项：标记 reason 便于追溯
        if budget_exhausted and assessment["status"] == "unknown":
            assessment["reasons"] = ["probe_budget_exhausted"]

        items.append({
            "name": video.name,
            "path": str(video),
            "status": assessment["status"],
            "reasons": assessment["reasons"],
            "container": assessment["container"],
            "has_video_stream": assessment["has_video_stream"],
            "duration_seconds": assessment["duration_seconds"],
            "size_bytes": assessment["size_bytes"] if assessment["size_bytes"] is not None else (
                st.st_size if budget_exhausted else assessment["size_bytes"]
            ),
            "probe_ok": assessment["probe_ok"],
        })

    probe_budget_exhausted = probe_attempts >= mcfg.max_probe_files and any(
        it["status"] == "unknown" and "probe_budget_exhausted" in it["reasons"]
        for it in items
    )

    return {
        "ok": True,
        "mode": mode,
        "scope": {
            "total_collected": total_collected,
            "page_count": len(page),
            "probed_files": probed_files,
            "probe_attempts": probe_attempts,
            "probe_failures": probe_failures,
            "probe_budget_exhausted": probe_budget_exhausted,
            "scan_truncated": meta["scan_truncated"],
        },
        "pagination": {
            "limit": limit,
            "offset": offset,
            "truncated": truncated,
            "next_offset": next_offset,
        },
        "items": items,
        "errors": errors + meta["walk_errors"],
    }
