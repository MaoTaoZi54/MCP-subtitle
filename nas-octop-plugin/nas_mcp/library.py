"""媒体库智能分析（需求 2.0 §29-§33，第二阶段 P5）：

- media_quality_analyze：单文件/批量质量标签（真实 ffprobe 维度 + 文件名来源）
- duplicate_media_detect：同一作品的不同版本聚合（TMDB ID > 标题+年份+时长，§30）
- media_library_audit：一键体检报告（电影/剧集/无法识别/无字幕/无中字/低清/重复/异常）

共同约束：
- 只读；所有路径过 PathGuard；复用 scan 的目录遍历、文件名跳过、ffprobe 探测预算；
- 单个文件 probe/TMDB 失败只计入 errors/异常，绝不让整次分析崩溃；
- TMDB 未启用时，"无法识别"统计返回 null（不用文件名硬凑识别率），重复检测退化为
  同名+同年+同季集匹配（绝不跨语言凭文件名合并，§30）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import media as media_tools
from . import resolver as resolver_tools
from .config import Limits, MediaConfig, TmdbConfig
from .fs import _flat_records, _walk_records
from .fs_utils import make_skip_matcher
from .release import parse_release, quality_score_from_dimensions
from .security import InvalidArguments, PathGuard, expand_roots
from .subhd import parse_video_filename

logger = logging.getLogger("nas_mcp.library")

# ffprobe codec_name → release 评分使用的视频编码键
_CODEC_FROM_PROBE = {
    "hevc": "HEVC", "h265": "HEVC",
    "h264": "AVC", "avc": "AVC",
    "av1": "AV1",
    "xvid": "XviD", "mpeg4": "XviD", "divx": "XviD",
}
# ffprobe 像素标签（"4K"/"1080p"）→ release 分辨率键
_RES_FROM_PROBE = {"4K": "2160p", "8K": "4320p"}
# ffprobe 音频 codec/profile → release 音频键（按优先级，先具体后宽泛）
_AUDIO_FROM_PROBE = (
    (re.compile(r"truehd", re.I), "TrueHD"),
    (re.compile(r"dts.?hd.?ma", re.I), "DTS-HD MA"),
    (re.compile(r"dts.?hd", re.I), "DTS-HD"),
    (re.compile(r"eac3|e-ac-3", re.I), "E-AC-3"),
    (re.compile(r"dts", re.I), "DTS"),
    (re.compile(r"ac3|ac-3", re.I), "AC-3"),
    (re.compile(r"flac", re.I), "FLAC"),
    (re.compile(r"aac", re.I), "AAC"),
    (re.compile(r"opus", re.I), "Opus"),
)
_PIXELFMT_DEPTH_RE = re.compile(r"p\d{2,3}le|p(\d{2,3})le|yuv\d{3}p(\d{2,3})")
_DURATION_TOLERANCE_RATIO = 0.15   # 同组时长允许 ±15%
_DURATION_TOLERANCE_ABS = 600      # 且绝对差不超过 10 分钟，超出则标记 mismatch
# 未命中 TMDB 做退化归并时，标题里需要剥离的纯技术词（同作品不同版本常见残留）
_TITLE_NOISE_RE = re.compile(r"\b(?:uhd|fhd|4k|8k)\b", re.IGNORECASE)


# ---------------------------------------------------------------- 目标采集


def _collect_targets(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    mcfg: MediaConfig,
    *,
    path: str = "",
    paths: list[str] | None,
    recursive: bool,
) -> tuple[list[Path], dict]:
    """统一采集待分析视频：显式文件列表 / 单路径（文件或目录）/ 全部 media_roots。

    返回 (视频 Path 列表, 元信息{scan_truncated, walk_errors})。
    """
    is_skipped = make_skip_matcher(mcfg.skip_filename_patterns)
    walk_errors: list[dict] = []

    def _on_error(p, msg: str) -> None:
        if len(walk_errors) < limits.max_errors_reported:
            walk_errors.append({"path": str(p), "error": msg})

    # 显式文件列表：逐个严格校验
    if paths is not None:
        if path:
            raise InvalidArguments("path 与 paths 只能二选一")
        out: list[Path] = []
        for raw in paths:
            p = guard.check(raw)
            if not p.exists():
                raise FileNotFoundError(f"文件不存在: {raw}")
            if not p.is_file():
                raise InvalidArguments(f"不是文件: {raw}")
            if p.suffix.lstrip(".").lower() not in video_exts:
                raise InvalidArguments(f"不是支持的视频文件: {raw}")
            out.append(p)
        return out, {"scan_truncated": False, "walk_errors": walk_errors}

    # 单路径：文件直接返回，目录按需遍历
    if path:
        base = guard.check(path)
        if base.is_file():
            if base.suffix.lstrip(".").lower() not in video_exts:
                raise InvalidArguments(f"不是支持的视频文件: {path}")
            return [base], {"scan_truncated": False, "walk_errors": walk_errors}
        if not base.is_dir():
            raise NotADirectoryError(f"路径不存在或不是目录: {path}")
        bases = [base]
    else:
        bases = []
        for r in expand_roots(media_roots):
            b = guard.check(r)
            if b not in bases:
                bases.append(b)

    state = {"truncated": False}
    videos: list[Path] = []
    for base in bases:
        if not base.is_dir():
            _on_error(base, "目录不存在或不可访问，已跳过")
            continue
        if recursive:
            records = _walk_records(
                base, guard, limits.max_scan_files, limits.max_list_depth,
                include_dirs=False, video_exts=video_exts, subtitle_exts=subtitle_exts,
                on_error=_on_error, state=state,
            )
        else:
            records = _flat_records(base, guard, video_exts, subtitle_exts, _on_error)
        for rec in records:
            if rec["media_category"] != "video":
                continue
            if is_skipped(rec["name"]):
                continue
            videos.append(Path(rec["path"]))
    return videos, {"scan_truncated": state["truncated"], "walk_errors": walk_errors}


# ---------------------------------------------------------------- 单文件维度


def _audio_from_probe(audio_tracks: list[dict]) -> dict:
    """从音轨里挑最强一条，映射成评分用的音频维度。"""
    best_codec = None
    has_atmos = False
    channels = None
    for t in audio_tracks:
        blob = f"{t.get('profile') or ''} {t.get('codec') or ''} {t.get('title') or ''}"
        if "atmos" in blob.lower():
            has_atmos = True
        if best_codec is None:
            for pat, label in _AUDIO_FROM_PROBE:
                if pat.search(blob):
                    best_codec = label
                    break
        if channels is None:
            layout = str(t.get("channel_layout") or "")
            m = re.search(r"(\d+(?:\.\d){1,2})", layout)
            if m:
                channels = m.group(1)
            elif t.get("channels"):
                channels = str(t["channels"])
    return {"audio_codec": best_codec, "has_atmos": has_atmos, "audio_channels": channels}


def _bit_depth(pix_fmt: str | None) -> int | None:
    if not pix_fmt:
        return None
    m = re.search(r"p(\d{2,3})le", pix_fmt)
    if m:
        return int(m.group(1))
    return None


def assess_file(video: Path, probe: dict | None) -> dict:
    """把 ffprobe 真实维度 + 文件名来源 token 组合成质量评估（§31）。

    probe 为 None（探测失败）时退化为仅文件名解析，quality_tier 可能为 unknown。
    """
    rel = parse_release(video.name)
    resolution = rel.resolution
    hdr = rel.hdr
    video_codec = rel.video_codec
    bit_depth = rel.bit_depth
    audio_codec = rel.audio_codec
    has_atmos = rel.has_atmos
    audio_channels = rel.audio_channels
    probe_resolution: str | None = None

    if probe and probe.get("video"):
        v = probe["video"]
        probe_resolution = v.get("resolution")
        resolution = _RES_FROM_PROBE.get(probe_resolution, probe_resolution) or resolution
        hdr = v.get("hdr_format") or hdr
        video_codec = _CODEC_FROM_PROBE.get(str(v.get("codec") or "").lower(), video_codec)
        if v.get("pixel_format"):
            bit_depth = _bit_depth(v.get("pixel_format")) or bit_depth
        au = _audio_from_probe(probe.get("audio") or [])
        audio_codec = au["audio_codec"] or audio_codec
        has_atmos = has_atmos or au["has_atmos"]
        audio_channels = au["audio_channels"] or audio_channels

    if probe is None:
        score, tier = (rel.quality_score, rel.quality_tier)
    else:
        score, tier = quality_score_from_dimensions(
            resolution=resolution, source_type=rel.source_type, video_codec=video_codec,
            hdr=hdr, audio_codec=audio_codec, has_atmos=has_atmos, bit_depth=bit_depth,
        )
    low_resolution = resolution in {"720p", "480p", "SD"}
    return {
        "resolution": resolution,
        "source_type": rel.source_type,
        "video_codec": video_codec,
        "hdr": hdr,
        "audio_codec": audio_codec,
        "audio_channels": audio_channels,
        "bit_depth": bit_depth,
        "has_atmos": has_atmos,
        "is_3d": rel.is_3d,
        "quality_score": score,
        "quality_tier": tier,
        "low_quality": bool(low_resolution or tier in ("poor", "bad")),
    }


def _resolve_identity(video: Path, tcfg: TmdbConfig) -> dict:
    """TMDB 身份解析；未启用/无标题/失败一律 resolved=False（不阻断分析）。"""
    q = parse_video_filename(video.name)
    base = {
        "resolved": False, "tmdb_id": None, "media_type": None,
        "canonical_title": None, "original_title": None, "year": q.year,
        "season": q.season, "episode": q.episode, "query_title": q.title,
    }
    if not tcfg.enabled or not q.title:
        return base
    try:
        r = resolver_tools.resolve(tcfg, q.title, year=q.year, media_type="")
    except (resolver_tools.ResolverError, InvalidArguments) as exc:
        logger.warning("library TMDB 解析失败 %s: %s", video.name, exc)
        return base
    if r.get("resolved") and r.get("identity"):
        ident = r["identity"]
        base.update({
            "resolved": True,
            "tmdb_id": ident["tmdb_id"],
            "media_type": ident.get("media_type"),
            "canonical_title": ident["canonical_title"],
            "original_title": ident["original_title"],
            "year": ident.get("year") or q.year,
        })
    return base


def _identity_key(ident: dict, fallback_stem: str) -> tuple:
    """同一作品归并键（§30）。

    已解析 → TMDB ID（剧集再细分到季集）；未解析 → 规范化片名+年份+季集。
    未解析绝不跨名合并（星际穿越 vs Interstellar 无 TMDB 时不能划等号）。
    """
    s, e = ident.get("season"), ident.get("episode")
    if ident["resolved"] and ident["tmdb_id"]:
        return ("tmdb", ident["media_type"] or "unknown", ident["tmdb_id"], s, e)
    raw_title = _TITLE_NOISE_RE.sub(" ", ident.get("query_title") or fallback_stem)
    title = re.sub(r"[^a-z0-9一-鿿]+", "", raw_title.lower())
    return ("name", title, ident.get("year"), s, e)


# ---------------------------------------------------------------- 批量分析内核


def _analyze_all(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    mcfg: MediaConfig,
    tcfg: TmdbConfig,
    *,
    path: str,
    paths: list[str] | None,
    recursive: bool,
) -> dict:
    """采集 → 逐个 probe/身份识别/质量评估，返回明细与预算元信息。"""
    targets, meta = _collect_targets(
        guard, limits, video_exts, subtitle_exts, media_roots, mcfg,
        path=path, paths=paths, recursive=recursive,
    )
    items: list[dict] = []
    probe_attempts = 0
    probed_files = 0
    probe_failures = 0

    for video in targets:
        probe = None
        probe_error: str | None = None
        if probe_attempts < mcfg.max_probe_files:
            probe_attempts += 1
            try:
                probe = media_tools.probe_video(
                    video, mcfg.ffprobe_path, mcfg.ffprobe_timeout_seconds
                )
            except media_tools.ProbeError as exc:
                probe_error = str(exc)
                probe_failures += 1
            else:
                probed_files += 1
        budget_exhausted = probe_attempts >= mcfg.max_probe_files

        ident = _resolve_identity(video, tcfg)
        quality = assess_file(video, probe)

        external = media_tools.find_external_subtitles(video, subtitle_exts) if probe is not None else []
        embedded = probe["embedded_subtitles"] if probe else []
        has_sub = bool(external or embedded)
        has_cn_sub = (
            any(media_tools.is_chinese(s.get("language")) for s in embedded)
            or any(media_tools.is_chinese(lang) for sub in external for lang in sub["languages"])
        )

        items.append({
            "name": video.name,
            "path": str(video),
            "probe_ok": probe is not None,
            "probe_error": probe_error,
            "size_bytes": probe["size_bytes"] if probe else None,
            "duration_seconds": probe["duration_seconds"] if probe else None,
            "identity": ident,
            "quality": quality,
            "has_subtitle": has_sub,
            "has_chinese_subtitle": has_cn_sub,
            "subtitle_checked": probe is not None,
        })

    return {
        "items": items,
        "total_collected": len(targets),
        "probe_attempts": probe_attempts,
        "probed_files": probed_files,
        "probe_failures": probe_failures,
        "probe_budget_exhausted": len(targets) > probe_attempts,
        "scan_truncated": meta["scan_truncated"],
        "walk_errors": meta["walk_errors"],
        "tmdb_enabled": tcfg.enabled,
    }


def _group_duplicates(items: list[dict]) -> list[dict]:
    """按身份键分组，仅返回成员数 ≥2 的"同一作品多版本"组（§30）。"""
    groups: dict[tuple, dict] = {}
    for it in items:
        key = _identity_key(it["identity"], Path(it["name"]).stem)
        g = groups.setdefault(key, {"key": key, "identity": it["identity"], "members": []})
        g["members"].append(it)

    out: list[dict] = []
    for g in groups.values():
        members = g["members"]
        if len(members) < 2:
            continue
        durations = [m["duration_seconds"] for m in members if m["duration_seconds"]]
        mismatch = False
        if len(durations) >= 2:
            spread = max(durations) - min(durations)
            base = max(abs(min(durations)), 1)
            mismatch = spread > _DURATION_TOLERANCE_ABS and spread / base > _DURATION_TOLERANCE_RATIO
        # 最佳版本：质量分高者优先，平分取文件更大者
        best_idx = max(
            range(len(members)),
            key=lambda i: (members[i]["quality"]["quality_score"],
                           members[i]["size_bytes"] or 0),
        )
        ident = g["identity"]
        out.append({
            "tmdb_id": ident["tmdb_id"],
            "media_type": ident["media_type"],
            "title": ident["canonical_title"] or ident["original_title"]
                     or ident["query_title"] or Path(members[0]["name"]).stem,
            "year": ident["year"],
            "resolved": ident["resolved"],
            "season": ident["season"],
            "episode": ident["episode"],
            "duration_mismatch": mismatch,
            "member_count": len(members),
            "best_index": best_idx,
            "members": [
                {
                    "name": m["name"], "path": m["path"],
                    "resolution": m["quality"]["resolution"],
                    "hdr": m["quality"]["hdr"],
                    "source_type": m["quality"]["source_type"],
                    "quality_score": m["quality"]["quality_score"],
                    "quality_tier": m["quality"]["quality_tier"],
                    "duration_seconds": m["duration_seconds"],
                    "size_bytes": m["size_bytes"],
                }
                for m in members
            ],
        })
    out.sort(key=lambda g: g["member_count"], reverse=True)
    return out


# ---------------------------------------------------------------- 三个工具入口


def quality_analyze(
    guard: PathGuard,
    mcfg: MediaConfig,
    video_exts: frozenset[str],
    *,
    path: str = "",
    paths: list[str] | None = None,
) -> dict:
    """media_quality_analyze：对显式给定的视频文件做质量标签分析。"""
    if not path and not paths:
        raise InvalidArguments("必须提供 path（单个视频）或 paths（批量列表）")
    files = [path] if path else list(paths or [])
    if len(files) > mcfg.max_classify_batch:
        raise InvalidArguments(
            f"批量上限 {mcfg.max_classify_batch} 个（media.max_classify_batch），"
            f"得到 {len(files)} 个；整库分析请用 media_library_audit"
        )
    # 质量分析不需要目录遍历/字幕扩展名，给空集合即可；显式列表已限定为文件
    dummy_limits = Limits()  # 仅文件列表时不使用限额
    targets, _ = _collect_targets(
        guard, dummy_limits, video_exts, frozenset(), (), mcfg,
        path=path, paths=paths, recursive=False,
    )
    results = []
    for video in targets:
        try:
            probe = media_tools.probe_video(
                video, mcfg.ffprobe_path, mcfg.ffprobe_timeout_seconds
            )
            probe_error = None
        except media_tools.ProbeError as exc:
            probe, probe_error = None, str(exc)
        q = assess_file(video, probe)
        results.append({
            "name": video.name, "path": str(video),
            "probe_ok": probe is not None, "probe_error": probe_error,
            "duration_seconds": probe["duration_seconds"] if probe else None,
            "size_bytes": probe["size_bytes"] if probe else None,
            **q,
        })
    return {"ok": True, "total": len(results), "results": results}


def duplicate_detect(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    mcfg: MediaConfig,
    tcfg: TmdbConfig,
    *,
    path: str = "",
    paths: list[str] | None = None,
    recursive: bool = True,
) -> dict:
    """duplicate_media_detect：聚合同一作品的不同版本（§30）。"""
    if not path and not paths:
        raise InvalidArguments("必须提供 path（目录）或 paths（文件列表）")
    data = _analyze_all(
        guard, limits, video_exts, subtitle_exts, media_roots, mcfg, tcfg,
        path=path, paths=paths, recursive=recursive,
    )
    groups = _group_duplicates(data["items"])
    return {
        "ok": True,
        "tmdb_enabled": tcfg.enabled,
        "total_files": data["total_collected"],
        "probed_files": data["probed_files"],
        "duplicate_groups": len(groups),
        "duplicate_files": sum(g["member_count"] - 1 for g in groups),
        "probe_budget_exhausted": data["probe_budget_exhausted"],
        "scan_truncated": data["scan_truncated"],
        "errors": data["walk_errors"],
        "groups": groups,
    }


def _media_kind(item: dict) -> str:
    """电影/电视剧归类：TMDB 优先，否则文件名启发式。"""
    ident = item["identity"]
    if ident["resolved"] and ident["media_type"] in ("movie", "tv"):
        return ident["media_type"]
    if ident.get("season") or ident.get("episode"):
        return "tv"
    guess = media_tools.classify_media(Path(item["path"]))["type"]
    return "tv" if guess == "tv" else "movie" if guess != "unknown" else "unknown"


def audit(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    mcfg: MediaConfig,
    tcfg: TmdbConfig,
    *,
    path: str = "",
    paths: list[str] | None = None,
    recursive: bool = True,
) -> dict:
    """media_library_audit：8 项统计的一键体检报告（§29/§33）。"""
    data = _analyze_all(
        guard, limits, video_exts, subtitle_exts, media_roots, mcfg, tcfg,
        path=path, paths=paths, recursive=recursive,
    )
    items = data["items"]
    groups = _group_duplicates(items)
    dup_paths = {m["path"] for g in groups for m in g["members"][1:]}

    movie_count = tv_count = unrecognized = 0
    no_subtitle = no_chinese_subtitle = low_resolution = 0
    low_resolution_files: list[str] = []
    type_breakdown: dict[str, int] = {}

    for it in items:
        kind = _media_kind(it)
        type_breakdown[kind] = type_breakdown.get(kind, 0) + 1
        if kind == "tv":
            tv_count += 1
        elif kind == "movie":
            movie_count += 1
        # TMDB 启用却没认出来（且文件名也无季集/年份信号）→ 无法识别
        if tcfg.enabled and not it["identity"]["resolved"] and kind == "unknown":
            unrecognized += 1

        if it["subtitle_checked"]:
            if not it["has_subtitle"]:
                no_subtitle += 1
            if not it["has_chinese_subtitle"]:
                no_chinese_subtitle += 1
            if it["quality"]["resolution"] in {"720p", "480p", "SD"}:
                low_resolution += 1
                if len(low_resolution_files) < limits.max_errors_reported:
                    low_resolution_files.append(it["path"])

    return {
        "ok": True,
        "tmdb_enabled": tcfg.enabled,
        "scope": {
            "total_collected": data["total_collected"],
            "probed_files": data["probed_files"],
            "probe_failures": data["probe_failures"],
            "scan_truncated": data["scan_truncated"],
            "probe_budget_exhausted": data["probe_budget_exhausted"],
        },
        "summary": {
            "movie_count": movie_count,
            "tv_count": tv_count,
            # TMDB 未启用时无法识别率无意义，返回 null
            "unrecognized_count": unrecognized if tcfg.enabled else None,
            "no_subtitle_count": no_subtitle,
            "no_chinese_subtitle_count": no_chinese_subtitle,
            "low_resolution_count": low_resolution,
            "duplicate_group_count": len(groups),
            "duplicate_file_count": len(dup_paths),
            "abnormal_count": data["probe_failures"],
        },
        "media_type_breakdown": type_breakdown,
        "low_resolution_files": low_resolution_files,
        "duplicate_groups": [
            {"title": g["title"], "year": g["year"], "member_count": g["member_count"],
             "best_path": g["members"][g["best_index"]]["path"]}
            for g in groups
        ],
        "errors": data["walk_errors"],
    }
