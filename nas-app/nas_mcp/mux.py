"""字幕混流封装（media_mux_subtitles）：将外挂字幕直接无损封装入视频容器。

设计理念与降级策略（Graceful Degradation）：
- SRT 作为保底，有 ASS 更好（双轨/单轨自适应）：
  - 若有外挂 ASS/SSA 字幕：封装双轨：
    * Track 1: ASS 特效（标题: 简体中文 (特效)，语言: chi，disposition: default）
    * Track 2: SRT 纯文本（若有独立同名 SRT 则封装该 SRT，否则由 ASS 实时转码；标题: 简体中文 (兼容保底)，语言: chi，disposition: 0）
  - 若仅有外挂 SRT/VTT 字幕：封装单轨：
    * Track 1: SRT 纯文本（标题: 简体中文，语言: chi，disposition: default）
- 流拷贝（Stream Copy）：
  - 视频 (-c:v copy) 与音频 (-c:a copy) 绝不重编码，秒级完成且零画质损失；
  - 现有音视频轨道与原有非中文字幕完整保留，原有附件/字体 (-map 0:t? -c:t copy) 完整保留；
  - 容器统一采用 .mkv（多轨、ASS 特效与高清音频容器标准）；
- 工业级安全防护（五重门与原子操作）：
  - 默认 dry_run=true 只返回拟执行计划，不发起 ffmpeg 子进程，不写磁盘；
  - dry_run=false 必须满足 enable_write=true 门控；
  - 原子替换：先写入同目录临时隐藏文件（.{name}.mux_tmp.mkv）；
  - 探针校验（Verification）：封装完成后，先用 ffprobe 检验临时文件（时长相符、且内嵌中文字幕流存在），校验失败绝不覆盖原文件；
  - 校验通过后原子覆盖原视频（或在 mp4 场景生成新 mkv 并根据 replace_original 安全清理）；
  - delete_external=true 时可在封装成功且校验通过后删除原外挂字幕；
  - 写入审计日志（action="MUX"），附带 operation_id。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Limits, MediaConfig, NasConfig, OrganizerConfig
from .fs import _flat_records, _walk_records
from .fs_utils import ErrorSink, ensure_mode, make_skip_matcher
from .platform_hints import permission_hint
from .media import (
    ProbeError,
    find_external_subtitles,
    is_chinese,
    probe_video,
    stream_has_chinese,
)
from .organizer import _audit, new_operation_id
from .progress import check_canceled, mark_partial, report
from .security import AccessDenied, InvalidArguments, PathGuard, expand_roots

logger = logging.getLogger(__name__)

# 中文字符正则（Unicode CJK 统一表意文字）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 常见中文语言代号/缩写
_CHINESE_TOKENS = frozenset({
    "chs", "cht", "sc", "tc", "gb", "big5", "hans", "hant", "zh", "chi", "zho",
    "zh-hans", "zh-hant", "zh-cn", "zh-tw", "zh-hk", "simplified", "traditional",
    "简中", "繁中", "简体", "繁體", "中字", "双语", "雙語",
})


def is_chinese_sub_candidate(sub: dict) -> bool:
    """综合语言标签与文件名判定外挂字幕是否为中文字幕。"""
    langs = sub.get("languages", [])
    if any(is_chinese(l) for l in langs):
        return True
    name = str(sub.get("name", "")).lower()
    if _CJK_RE.search(name):
        return True
    # 分词检查
    tokens = re.split(r"[-_. ]+", name)
    return any(t in _CHINESE_TOKENS for t in tokens)


@dataclass(frozen=True)
class SubtitleTrackPlan:
    source_path: Path
    codec: str               # "copy" 或 "srt"
    title: str               # 轨道名称，如 "简体中文 (特效)" 或 "简体中文 (兼容保底)"
    language: str            # "chi"
    is_default: bool         # 是否设为默认轨道
    is_converted: bool       # 是否由 ASS 实时转换而来


@dataclass(frozen=True)
class MuxItemPlan:
    video_path: Path
    target_path: Path
    strategy: str            # "dual_track" | "single_track"
    tracks: tuple[SubtitleTrackPlan, ...]
    external_subs: tuple[Path, ...]
    existing_subtitle_count: int
    ffmpeg_cmd: tuple[str, ...]


def plan_mux_item(
    video_path: Path,
    probe_info: dict,
    external_subs: list[dict],
    ffmpeg_path: str = "ffmpeg",
    force: bool = False,
) -> MuxItemPlan | None:
    """根据视频信息及外挂字幕列表生成混流计划。若无需混流则返回 None。"""
    embedded_subs = probe_info.get("embedded_subtitles", [])
    has_embedded_chinese = any(stream_has_chinese(s) for s in embedded_subs)
    if has_embedded_chinese and not force:
        return None

    # 筛选中文字幕
    chinese_subs = [s for s in external_subs if is_chinese_sub_candidate(s)]
    if not chinese_subs:
        return None

    ass_candidates: list[Path] = []
    srt_candidates: list[Path] = []

    for sub in chinese_subs:
        p = Path(sub["path"])
        ext = p.suffix.lower()
        if ext in (".ass", ".ssa"):
            ass_candidates.append(p)
        elif ext in (".srt", ".vtt"):
            srt_candidates.append(p)

    if not ass_candidates and not srt_candidates:
        return None

    # 独立 SRT 优先于 VTT：VTT 需要转码为 SRT（MKV 内置 webvtt 兼容性差）
    srt_candidates.sort(key=lambda p: p.suffix.lower() != ".srt")

    # 输出目标始终为 .mkv
    target_path = video_path.with_suffix(".mkv")
    existing_count = len(embedded_subs)

    tracks: list[SubtitleTrackPlan] = []
    inputs: list[Path] = [video_path]
    external_used: list[Path] = []

    if ass_candidates:
        # 策略 A: ASS 存在 -> 双轨封装（ASS特效轨 + SRT保底轨）
        strategy = "dual_track"
        ass_sub = ass_candidates[0]
        external_used.append(ass_sub)
        inputs.append(ass_sub)
        ass_input_idx = 1

        # Track 1: ASS 特效（默认优先）
        tracks.append(SubtitleTrackPlan(
            source_path=ass_sub,
            codec="copy",
            title="简体中文 (特效)",
            language="chi",
            is_default=True,
            is_converted=False,
        ))

        # Track 2: SRT 兼容保底
        if srt_candidates:
            srt_sub = srt_candidates[0]
            srt_is_vtt = srt_sub.suffix.lower() == ".vtt"
            external_used.append(srt_sub)
            inputs.append(srt_sub)
            srt_input_idx = 2
            tracks.append(SubtitleTrackPlan(
                source_path=srt_sub,
                codec="srt" if srt_is_vtt else "copy",
                title="简体中文 (兼容保底)",
                language="chi",
                is_default=False,
                is_converted=srt_is_vtt,
            ))
            srt_input_ref = (srt_input_idx, "srt" if srt_is_vtt else "copy")
        else:
            # 无独立 SRT，由同一 ASS 在内存中直接转为 SRT 文本轨
            tracks.append(SubtitleTrackPlan(
                source_path=ass_sub,
                codec="srt",
                title="简体中文 (兼容保底)",
                language="chi",
                is_default=False,
                is_converted=True,
            ))
            srt_input_ref = (ass_input_idx, "srt")

        cmd = [ffmpeg_path, "-v", "error", "-y", "-i", str(video_path), "-i", str(ass_sub)]
        if srt_candidates:
            cmd.extend(["-i", str(srt_candidates[0])])

        # 视频、音频、附件无损拷贝；保留已有字幕并清除其 default 标志
        cmd.extend([
            "-map", "0:v", "-c:v", "copy",
            "-map", "0:a", "-c:a", "copy",
            "-map", "0:s?",
            "-map", "0:t?", "-c:t", "copy",
            "-c:s", "copy",
        ])
        for idx in range(existing_count):
            cmd.extend([f"-disposition:s:{idx}", "0"])
            # MUX-8: 若原内嵌字幕为 MKV 不兼容的编码（如 MP4 的 mov_text），转为 srt
            sub_codec = str(embedded_subs[idx].get("codec") or "").lower()
            if sub_codec in ("mov_text", "tx3g", "timed_id3", "eia_608"):
                cmd.extend([f"-c:s:{idx}", "srt"])

        # 映射 Track 1 (ASS)
        k = existing_count
        cmd.extend([
            "-map", f"{ass_input_idx}:s",
            f"-c:s:{k}", "copy",
            f"-metadata:s:s:{k}", "language=chi",
            f"-metadata:s:s:{k}", "title=简体中文 (特效)",
            f"-disposition:s:{k}", "default",
        ])

        # 映射 Track 2 (SRT)
        k2 = existing_count + 1
        src_idx, codec = srt_input_ref
        cmd.extend([
            "-map", f"{src_idx}:s",
            f"-c:s:{k2}", codec,
            f"-metadata:s:s:{k2}", "language=chi",
            f"-metadata:s:s:{k2}", "title=简体中文 (兼容保底)",
            f"-disposition:s:{k2}", "0",
        ])

    else:
        # 策略 B: 仅 SRT -> 单轨封装
        strategy = "single_track"
        srt_sub = srt_candidates[0]
        srt_is_vtt = srt_sub.suffix.lower() == ".vtt"
        external_used.append(srt_sub)
        inputs.append(srt_sub)

        tracks.append(SubtitleTrackPlan(
            source_path=srt_sub,
            codec="srt" if srt_is_vtt else "copy",
            title="简体中文",
            language="chi",
            is_default=True,
            is_converted=srt_is_vtt,
        ))

        cmd = [
            ffmpeg_path, "-v", "error", "-y",
            "-i", str(video_path),
            "-i", str(srt_sub),
            "-map", "0:v", "-c:v", "copy",
            "-map", "0:a", "-c:a", "copy",
            "-map", "0:s?",
            "-map", "0:t?", "-c:t", "copy",
            "-c:s", "copy",
        ]
        for idx in range(existing_count):
            cmd.extend([f"-disposition:s:{idx}", "0"])
            # MUX-8: 若原内嵌字幕为 MKV 不兼容的编码（如 MP4 的 mov_text），转为 srt
            sub_codec = str(embedded_subs[idx].get("codec") or "").lower()
            if sub_codec in ("mov_text", "tx3g", "timed_id3", "eia_608"):
                cmd.extend([f"-c:s:{idx}", "srt"])

        k = existing_count
        cmd.extend([
            "-map", "1:s",
            f"-c:s:{k}", "srt" if srt_is_vtt else "copy",
            f"-metadata:s:s:{k}", "language=chi",
            f"-metadata:s:s:{k}", "title=简体中文",
            f"-disposition:s:{k}", "default",
        ])

    # 占位符在实际执行时替换为临时文件路径
    cmd.append("{output_placeholder}")

    return MuxItemPlan(
        video_path=video_path,
        target_path=target_path,
        strategy=strategy,
        tracks=tuple(tracks),
        external_subs=tuple(external_used),
        existing_subtitle_count=existing_count,
        ffmpeg_cmd=tuple(cmd),
    )


def execute_mux_plan(
    plan: MuxItemPlan,
    media_cfg: MediaConfig,
    org_cfg: OrganizerConfig,
    delete_external: bool = False,
    replace_original: bool = False,
) -> dict:
    """实际执行单个混流操作：调用 ffmpeg、校验产物、原子替换与审计。"""
    video = plan.video_path
    target = plan.target_path

    # mp4 → mkv 时目标文件可能已存在：绝不覆盖用户的既有文件
    if target != video and target.exists():
        return {
            "ok": False,
            "code": "target_exists",
            "error": f"目标文件已存在，拒绝覆盖: {target}",
        }
    if not os.access(target.parent, os.W_OK):
        hint = permission_hint(target.parent)
        return {
            "ok": False,
            "code": "permission_denied",
            "error": hint["message"],
            "denied_dir": hint["denied_dir"],
            "platform": hint["platform"],
            "fix_steps": hint["steps"],
        }

    # 临时文件放在同一父目录下，确保在同一文件系统便于原子 rename
    temp_file = video.with_name(f".{video.stem}.mux_tmp.mkv")
    # fnOS 会把新建文件裁成 0555：残留的旧临时文件先放开写位，否则 ffmpeg -y 写不进去
    if temp_file.exists():
        ensure_mode(temp_file, 0o644)

    # 构建完整 ffmpeg 命令
    cmd = list(plan.ffmpeg_cmd[:-1]) + [str(temp_file)]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=media_cfg.ffmpeg_timeout_seconds,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": f"ffmpeg 不可用（{media_cfg.ffmpeg_path!r}）：请确保 NAS 系统已安装 ffmpeg",
        }
    except subprocess.TimeoutExpired:
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)
        return {
            "ok": False,
            "error": f"ffmpeg 混流超时（>{media_cfg.ffmpeg_timeout_seconds}s）",
        }
    except OSError as exc:
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)
        return {"ok": False, "error": f"ffmpeg 启动失败: {exc}"}

    if proc.returncode != 0:
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)
        lines = (proc.stderr or "").strip().splitlines()
        tail = lines[-1] if lines else "unknown ffmpeg error"
        return {"ok": False, "error": f"ffmpeg 退出码 {proc.returncode}: {tail[:200]}"}

    # 产物存在性与尺寸初检
    if not temp_file.exists() or temp_file.stat().st_size == 0:
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)
        return {"ok": False, "error": "ffmpeg 完成但未产出有效文件或文件大小为 0"}

    # fnOS 会把新建的混流产物裁成 0555：补回 0644，避免后续移动/再次混流失败
    ensure_mode(temp_file, 0o644)

    # 深度探针校验：调用 ffprobe 检查混流产物
    try:
        probed = probe_video(
            temp_file,
            media_cfg.ffprobe_path,
            media_cfg.ffprobe_timeout_seconds,
        )
    except ProbeError as exc:
        temp_file.unlink(missing_ok=True)
        return {"ok": False, "error": f"混流产物探针校验失败: {exc}"}

    # 验证是否包含中文字幕流
    new_embedded = probed.get("embedded_subtitles", [])
    if not any(stream_has_chinese(s) for s in new_embedded):
        temp_file.unlink(missing_ok=True)
        return {"ok": False, "error": "混流产物未检测到中文字幕流，取消替换"}

    # 原子替换
    operation_id = new_operation_id(org_cfg)
    backup_file: Path | None = None
    deleted_original = ""

    try:
        if target == video:
            # 原视频本身即为 .mkv：先做备份再原子移动覆盖
            backup_file = video.with_name(f".{video.name}.bak")
            video.rename(backup_file)
            temp_file.rename(target)
            backup_file.unlink(missing_ok=True)
        else:
            # 原视频为 .mp4 等格式，新封装生成 .mkv
            temp_file.rename(target)
            if replace_original and video.exists():
                try:
                    orig_stat = video.stat()
                except OSError:
                    orig_stat = None
                video.unlink(missing_ok=True)
                deleted_original = str(video)
                # 删除类动作单独审计（MUX 本身不可回滚，删除更不可恢复）
                if orig_stat is not None:
                    try:
                        _audit(
                            org_cfg,
                            action="MUX_DELETE_ORIGINAL",
                            source=str(video),
                            target=str(target),
                            result="SUCCESS",
                            operation_id=operation_id,
                            size=orig_stat.st_size,
                            mtime=orig_stat.st_mtime_ns,
                            channel="mux",
                        )
                    except OSError as e:
                        logger.warning("混流删除原视频审计写入失败: %s", e)

        # 产物落到最终路径后同样补回写位（fnOS 新建文件为 0555）
        ensure_mode(target, 0o644)

        # 清理外挂字幕（如果指定）
        deleted_subs: list[str] = []
        if delete_external:
            for sub_p in plan.external_subs:
                try:
                    if sub_p.exists():
                        try:
                            sub_stat = sub_p.stat()
                        except OSError:
                            sub_stat = None
                        sub_p.unlink(missing_ok=True)
                        deleted_subs.append(str(sub_p))
                        if sub_stat is not None:
                            try:
                                _audit(
                                    org_cfg,
                                    action="MUX_DELETE_EXTERNAL",
                                    source=str(sub_p),
                                    target=str(target),
                                    result="SUCCESS",
                                    operation_id=operation_id,
                                    size=sub_stat.st_size,
                                    mtime=sub_stat.st_mtime_ns,
                                    channel="mux",
                                )
                            except OSError as e:
                                logger.warning("删除外挂字幕审计写入失败: %s (%s)", sub_p, e)
                except OSError as e:
                    logger.warning("清理外挂字幕失败: %s (%s)", sub_p, e)

        # 写入审计记录
        try:
            st = target.stat()
            _audit(
                org_cfg,
                action="MUX",
                source=str(video),
                target=str(target),
                result="SUCCESS",
                operation_id=operation_id,
                size=st.st_size,
                mtime=st.st_mtime_ns,
                channel="mux",
            )
        except OSError as e:
            logger.warning("混流审计写入失败: %s", e)

        return {
            "ok": True,
            "operation_id": operation_id,
            "rollback": "unsupported",
            "video_path": str(video),
            "target_path": str(target),
            "strategy": plan.strategy,
            "tracks": [
                {
                    "title": t.title,
                    "codec": t.codec,
                    "language": t.language,
                    "default": t.is_default,
                    "converted": t.is_converted,
                    "source": str(t.source_path),
                }
                for t in plan.tracks
            ],
            "deleted_external_subtitles": deleted_subs,
            "deleted_original_video": deleted_original or None,
        }

    except OSError as exc:
        # 异常恢复：若原视频已更名备份，尝试恢复
        if backup_file and backup_file.exists() and not video.exists():
            try:
                backup_file.rename(video)
            except OSError:
                pass
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)
        return {"ok": False, "error": f"原子替换文件失败: {exc}"}


def media_mux_subtitles(
    guard: PathGuard,
    cfg: NasConfig,
    path: str = "",
    video_path: str = "",
    dry_run: bool = True,
    delete_external: bool = False,
    replace_original: bool = False,
    force: bool = False,
    limit: int = 50,
    budget_seconds: float = 0.0,
) -> dict:
    """扫描并批量/单文件将外挂字幕混流封装入视频（MKV，支持双轨/单轨自动降级）。"""
    if not dry_run and not cfg.enable_write:
        return {
            "ok": False,
            "error": {
                "code": "write_disabled",
                "message": "配置 enable_write=false，禁止执行实际混流写入。设 dry_run=true 可查看拟执行计划。",
            },
        }

    # 删除类动作单独门控：replace_original / delete_external 都是永久删除，
    # 与 enable_write（字幕落盘）分离，必须 enable_delete=true 才允许。
    if not dry_run and (delete_external or replace_original) and not cfg.enable_delete:
        return {
            "ok": False,
            "error": {
                "code": "delete_disabled",
                "message": (
                    "replace_original / delete_external 属于永久删除，需要 enable_delete=true；"
                    "当前 enable_delete=false，已拒绝执行。可改用 replace_original=false、"
                    "delete_external=false 仅新增 mkv 并保留原文件，"
                    "或由管理员在 config.yaml 中开启删除权限。"
                ),
            },
        }

    limit = max(1, min(limit, cfg.limits.max_search_results))
    sink = ErrorSink(cfg.limits.max_errors_reported)
    is_skipped = make_skip_matcher(cfg.media.skip_filename_patterns)

    # 收集待检测视频列表
    videos_to_check: list[Path] = []
    if video_path:
        vp = guard.check(video_path)
        if not vp.is_file():
            raise InvalidArguments(f"指定的视频文件不存在或不是普通文件: {video_path}")
        ext = vp.suffix.lstrip(".").lower()
        if ext not in cfg.video_extensions:
            raise InvalidArguments(f"不支持的视频扩展名: {ext}")
        videos_to_check.append(vp)
    elif path:
        base = guard.check(path)
        if not base.is_dir():
            raise NotADirectoryError(f"不是目录: {path}")
        records = _walk_records(
            base, guard, cfg.limits.max_scan_files, cfg.limits.max_list_depth,
            include_dirs=False, video_exts=cfg.video_extensions,
            subtitle_exts=cfg.subtitle_extensions,
            on_error=lambda p, msg: sink.add(p, msg),
            state={"truncated": False},
        )
        for r in records:
            if r["media_category"] == "video" and not is_skipped(r["name"]):
                videos_to_check.append(Path(r["path"]))
    else:
        for root_str in expand_roots(cfg.media_roots):
            try:
                base = guard.check(root_str)
                if not base.is_dir():
                    continue
                records = _walk_records(
                    base, guard, cfg.limits.max_scan_files, cfg.limits.max_list_depth,
                    include_dirs=False, video_exts=cfg.video_extensions,
                    subtitle_exts=cfg.subtitle_extensions,
                    on_error=lambda p, msg: sink.add(p, msg),
                    state={"truncated": False},
                )
                for r in records:
                    if r["media_category"] == "video" and not is_skipped(r["name"]):
                        videos_to_check.append(Path(r["path"]))
            except (AccessDenied, OSError):
                continue

    planned_items: list[dict] = []
    executed_items: list[dict] = []
    errors: list[dict] = []
    skipped_count = 0
    budget_exhausted = False
    canceled = False
    processed = 0
    total_videos = len(videos_to_check)
    deadline = (
        time.monotonic() + float(budget_seconds) if budget_seconds and budget_seconds > 0 else None
    )
    report("mux", "扫描待混流的视频", done=0, total=total_videos)

    for idx, v in enumerate(videos_to_check, 1):
        processed = idx - 1
        # 合作式取消：只在条目边界停下，已完成的正式文件全部保留
        if check_canceled():
            canceled = True
            break
        report("mux", "混流字幕", done=processed, total=total_videos, current=v.name)
        # P0-4：wall-clock 预算——超时后不再探测/混流新文件，返回已完成部分
        if deadline is not None and time.monotonic() >= deadline:
            budget_exhausted = True
            break
        if len(planned_items) >= limit:
            break

        # 探针获取内置字幕流
        try:
            pv = probe_video(v, cfg.media.ffprobe_path, cfg.media.ffprobe_timeout_seconds)
        except ProbeError as exc:
            sink.add(v, f"ffprobe 分析失败: {exc}")
            continue

        # 查找同目录下外挂字幕
        ext_subs = find_external_subtitles(v, cfg.subtitle_extensions)
        if not ext_subs:
            skipped_count += 1
            continue

        plan = plan_mux_item(
            v, pv, ext_subs,
            ffmpeg_path=cfg.media.ffmpeg_path,
            force=force,
        )
        if not plan:
            skipped_count += 1
            continue

        # 目标已存在（典型：mp4 → 同名 mkv 已存在）：拒绝覆盖并报告
        if plan.target_path != plan.video_path and plan.target_path.exists():
            errors.append({
                "video_path": str(plan.video_path),
                "target_path": str(plan.target_path),
                "code": "target_exists",
                "error": "目标文件已存在，拒绝覆盖（请先移动或删除后重试）",
            })
            continue

        plan_summary = {
            "video_path": str(plan.video_path),
            "target_path": str(plan.target_path),
            "strategy": plan.strategy,
            "existing_subtitles": plan.existing_subtitle_count,
            "external_subtitles": [str(p) for p in plan.external_subs],
            "tracks": [
                {
                    "title": t.title,
                    "codec": t.codec,
                    "language": t.language,
                    "default": t.is_default,
                    "converted": t.is_converted,
                    "source": str(t.source_path),
                }
                for t in plan.tracks
            ],
            "ffmpeg_cmd": list(plan.ffmpeg_cmd),
        }
        planned_items.append(plan_summary)

        if not dry_run:
            exec_res = execute_mux_plan(
                plan,
                media_cfg=cfg.media,
                org_cfg=cfg.organizer,
                delete_external=delete_external,
                replace_original=replace_original,
            )
            if exec_res.get("ok"):
                executed_items.append(exec_res)
            else:
                errors.append({
                    "video_path": str(v),
                    "error": exec_res.get("error", "未知错误"),
                })

    else:
        processed = total_videos

    if canceled:
        # 取消清理：只删混流中断留下的 .mux_tmp.mkv，已完成并落盘的正式文件保留
        for item in videos_to_check[:processed + 1]:
            tmp = item.with_name(f".{item.stem}.mux_tmp.mkv")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        report("mux", "已取消，保留已完成部分", done=processed, total=total_videos)
    else:
        report("mux", "混流字幕", done=processed, total=total_videos)

    result = {
        "ok": True,
        "dry_run": dry_run,
        "scanned_count": len(videos_to_check),
        "skipped_count": skipped_count,
        "matched_count": len(planned_items),
        "plans": planned_items,
        "executed": executed_items if not dry_run else [],
        "errors": errors + sink.errors,
        "budget_seconds": float(budget_seconds or 0),
        "budget_exhausted": budget_exhausted,
        "rollback_supported": False,
        "rollback_note": (
            "混流（MUX）不支持回滚；replace_original / delete_external 触发的删除不可恢复，"
            "执行前请确认 dry_run 计划"
        ),
    }
    return mark_partial(result) if canceled else result
