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
from .fs_utils import ErrorSink, make_skip_matcher, mark_incomplete, normalize_list_arg
from .release import parse_release, quality_score_from_dimensions, bitrate_tiebreaker
from .security import InvalidArguments, PathGuard, expand_roots
from .subhd import parse_video_filename

logger = logging.getLogger("nas_mcp.library")

# ffprobe codec_name → release 评分使用的视频编码键
_SEASON_ROOT_RE = re.compile(r"^Season\s+\d+$", re.IGNORECASE)


def _show_root_path(video: str) -> str:
    """视频所属剧集根（路径中最后一个 Season XX 的父目录；无 Season 取父目录）。

    用于 audit 统计同一实体是否分居多个剧集根（NEW-18）。
    """
    p = Path(video)
    season_idxs = [i for i, part in enumerate(p.parts)
                   if _SEASON_ROOT_RE.match(part.strip())]
    if season_idxs and season_idxs[-1] >= 1:
        i = season_idxs[-1]
        return str(Path(*p.parts[:i]))
    return str(p.parent)


# 根目录名里的按季/按集发布标记（"Cold Case 2003 Season 2"、"Show - S01E01 - …"）
_SEASON_TOKEN_RE = re.compile(r"season\s*\d+|第\s*\d+\s*季", re.IGNORECASE)
_SXXEXX_IN_NAME_RE = re.compile(
    r"(?:^|[^a-z0-9])s\d{1,2}\s*e\d{1,3}(?:[^a-z0-9]|$)", re.IGNORECASE
)


def _show_root_key(root: str) -> str:
    """多根聚合键：剥离根目录名中的季/集发布标记后比较（#1.1）。

    "Cold Case 2003 Season 2/3/…" 是完全正常的按季发布结构，不应报成
    "同一作品散落多根"假阳性——比较用剥离后的键，展示仍用原始根路径。
    """
    p = Path(root)
    name = p.name
    m = _SXXEXX_IN_NAME_RE.search(name)
    if m:
        cut = name[:m.start()]
    else:
        m2 = _SEASON_TOKEN_RE.search(name)
        cut = name[:m2.start()] if m2 else name
    normalized = re.sub(r"[\s\-_.]+$", "", cut).strip()
    if not normalized:
        # 整个目录名就是季标记（如 "Season 03"）：无法剥离出片名，原样返回
        return root
    # 键统一 casefold：无季信号的兄弟目录与剥离后的目录才能归并到同一键
    return str(p.parent / normalized.casefold())


_CODEC_FROM_PROBE = {
    "hevc": "HEVC", "h265": "HEVC",
    "h264": "AVC", "avc": "AVC",
    "av1": "AV1",
    "xvid": "XviD", "mpeg4": "XviD", "divx": "XviD",
}
# ffprobe 像素标签（"4K"/"1080p"）→ release 分辨率键
_RES_FROM_PROBE = {
    "4K": "2160p",
    "8K": "4320p",
    "1080p": "1080p",
    "720p": "720p",
    "480p": "480p",
}
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
_DURATION_TOLERANCE_RATIO = 0.15    # 同组时长允许 ±15%
# v0.11.8 NEW-5：绝对差至少 2s 才可能 mismatch——旧规则要求 spread>600s，
# 导致 1s vs 4s 这类短文件真冲突漏标。
_DURATION_MISMATCH_MIN_SPREAD = 2.0
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

    返回 (视频 Path 列表, 元信息{scan_truncated, walk_errors, walk_errors_summary})。
    """
    is_skipped = make_skip_matcher(mcfg.skip_filename_patterns)
    sink = ErrorSink(limits.max_errors_reported)

    def _on_error(p, msg: str) -> None:
        sink.add(p, msg)

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
        return out, {
            "scan_truncated": False,
            "walk_errors": sink.errors,
            "walk_errors_summary": sink.summary(),
            "walk_denied_sample": sink.denied_sample(),  # T3
        }

    # 单路径：文件直接返回，目录按需遍历
    if path:
        base = guard.check(path)
        if base.is_file():
            if base.suffix.lstrip(".").lower() not in video_exts:
                raise InvalidArguments(f"不是支持的视频文件: {path}")
            return [base], {
                "scan_truncated": False,
                "walk_errors": sink.errors,
                "walk_errors_summary": sink.summary(),
                "walk_denied_sample": sink.denied_sample(),  # T3
            }
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
    return videos, {
        "scan_truncated": state["truncated"],
        "walk_errors": sink.errors,
        "walk_errors_summary": sink.summary(),
        "walk_denied_sample": sink.denied_sample(),  # T3
    }


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
    # T9：码率仅展示 + 同档 tiebreaker 输入，绝不进 0-100 主分
    bit_rate = (
        (probe.get("video") or {}).get("bit_rate") or probe.get("bit_rate")
        if probe else None
    )
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
        "bit_rate": bit_rate,           # T9：同档 tiebreaker（不参与主分）
    }


def _resolve_identity(video: Path, tcfg: TmdbConfig) -> dict:
    """TMDB 身份解析；未启用/无标题/失败一律 resolved=False（不阻断分析）。

    文件名/路径含 SxxExx、Season N 等剧集标记时强制走 /search/tv（#1：避免
    同名电影抢实体，如 Cold Case 剧集被解析成同名电影），并经进程级缓存保证
    与 nas_classify_region / media_missing_episodes 拿到同一实体。
    """
    q = parse_video_filename(video.name)
    base = {
        "resolved": False, "tmdb_id": None, "media_type": None,
        "canonical_title": None, "original_title": None, "year": q.year,
        "season": q.season, "episode": q.episode, "query_title": q.title,
    }
    if not tcfg.enabled or not q.title:
        return base
    hint = resolver_tools.infer_media_type(
        title=q.title, path=str(video), season=q.season, episode=q.episode
    )
    try:
        r = resolver_tools.resolve_cached(tcfg, q.title, year=q.year, media_type=hint)
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
            any(media_tools.stream_has_chinese(s) for s in embedded)
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
        "walk_errors_summary": meta.get("walk_errors_summary"),
        "walk_denied_sample": meta.get("walk_denied_sample", []),  # T3
        "tmdb_enabled": tcfg.enabled,
    }


def _is_duration_mismatch(durations: list[float]) -> bool:
    """同组时长是否互相矛盾（NEW-5）：差 >2s 且占比 >15%。

    占比以较短时长为基（4s 文件差 3s = 75%）；长片版本差（如 3600s 差 10s）
    占比极小，不会误标。
    """
    if len(durations) < 2:
        return False
    spread = max(durations) - min(durations)
    base = max(abs(min(durations)), 1.0)
    return (spread > _DURATION_MISMATCH_MIN_SPREAD
            and spread / base > _DURATION_TOLERANCE_RATIO)


def _group_duplicates(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """按身份键分组，返回 (重复组, 可疑组)（§30；NEW-5/NEW-11）。

    成员数 ≥2 的身份键组先做时长一致性校验：
    - TMDB 身份键：组始终返回，时长矛盾时 duration_mismatch=true（不产出
      删除建议语义由调用方按此标志处理）；
    - 退化名片键（未解析身份）：时长矛盾 → 降为 suspect 组，不计入重复、
      不参与删除建议——防止"泛化同名（video.mkv 等）+ 内容完全不同"
      的文件被跨作品误并（NEW-11）。
    """
    groups: dict[tuple, dict] = {}
    for it in items:
        key = _identity_key(it["identity"], Path(it["name"]).stem)
        g = groups.setdefault(key, {"key": key, "identity": it["identity"], "members": []})
        g["members"].append(it)

    out: list[dict] = []
    suspects: list[dict] = []
    for g in groups.values():
        members = g["members"]
        if len(members) < 2:
            continue
        durations = [m["duration_seconds"] for m in members if m["duration_seconds"]]
        mismatch = _is_duration_mismatch(durations)
        # 最佳版本：质量分高者优先；T9：同分且"分辨率+来源"同档内码率高者优先
        # （码率跨档不参与——WEB-DL 码率噪声大）；仍并列取文件更大者
        best_idx = max(
            range(len(members)),
            key=lambda i: (
                members[i]["quality"]["quality_score"],
                *bitrate_tiebreaker(
                    members[i]["quality"].get("resolution"),
                    members[i]["quality"].get("source_type"),
                    members[i]["quality"].get("bit_rate"),
                ),
                members[i]["size_bytes"] or 0,
            ),
        )
        ident = g["identity"]
        record = {
            "tmdb_id": ident["tmdb_id"],
            "media_type": ident["media_type"],
            "title": ident["canonical_title"] or ident["original_title"]
                     or ident["query_title"] or Path(members[0]["name"]).stem,
            "year": ident["year"],
            "resolved": ident["resolved"],
            "season": ident["season"],
            "episode": ident["episode"],
            "duration_mismatch": mismatch,
            "suspect": False,
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
        }
        # NEW-11：未解析身份（名片退化键）且时长矛盾 → 可疑组
        if g["key"][0] == "name" and mismatch:
            record["suspect"] = True
            record["suspect_reason"] = (
                "未解析出 TMDB 身份且组内时长差异超过阈值，可能是泛化同名的"
                "不同作品文件；不计入重复、不建议删除，请人工核对"
            )
            suspects.append(record)
        else:
            out.append(record)
    out.sort(key=lambda x: x["member_count"], reverse=True)
    suspects.sort(key=lambda x: x["member_count"], reverse=True)
    return out, suspects


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
    paths = normalize_list_arg(paths, "paths")  # NEW-6：兼容 JSON 字符串
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
    targets, meta = _collect_targets(
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
    return mark_incomplete(
        {"ok": True, "total": len(results), "results": results},
        permission_denied=(meta.get("walk_errors_summary") or {}).get(
            "permission_denied", 0),
        denied_sample=meta.get("walk_denied_sample", []),
        empty=not results,
    )


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
    paths = normalize_list_arg(paths, "paths")  # NEW-6：兼容 JSON 字符串
    if not path and not paths:
        raise InvalidArguments("必须提供 path（目录）或 paths（文件列表）")
    data = _analyze_all(
        guard, limits, video_exts, subtitle_exts, media_roots, mcfg, tcfg,
        path=path, paths=paths, recursive=recursive,
    )
    groups, suspects = _group_duplicates(data["items"])
    result = {
        "ok": True,
        "tmdb_enabled": tcfg.enabled,
        "total_files": data["total_collected"],
        "probed_files": data["probed_files"],
        "duplicate_groups": len(groups),
        "duplicate_files": sum(g["member_count"] - 1 for g in groups),
        "suspect_groups": len(suspects),
        "probe_budget_exhausted": data["probe_budget_exhausted"],
        # #4：分组基于全量文件（文件名 + TMDB 身份，不依赖 ffprobe），未探测
        # 只影响时长交叉校验——让 duplicate_groups=[] 可被解读为"确认无重复"
        "coverage": {
            "files_total": data["total_collected"],
            "files_probed": data["probed_files"],
            "partial": data["probe_budget_exhausted"],
            "grouping_scope": "all_files",
            "duration_cross_check_partial": data["probe_budget_exhausted"],
        },
        "scan_truncated": data["scan_truncated"],
        "errors": data["walk_errors"],
        "errors_summary": data.get("walk_errors_summary"),
        "groups": groups,
        "suspects": suspects,
    }
    # T3：一个文件都没采到 + 权限错误 → "无重复"结论不可信
    return mark_incomplete(
        result,
        permission_denied=(data.get("walk_errors_summary") or {}).get(
            "permission_denied", 0),
        denied_sample=data.get("walk_denied_sample", []),
        empty=data["total_collected"] == 0,
    )


def _media_kind(item: dict) -> str:
    """电影/电视剧归类：文件名季集信号 > TMDB > 文件名启发式。

    季集信号（SxxExx）优先于 TMDB 的 movie 判定（#1）：文件名含季集却解析成
    movie 即为同名实体误判，此时相信文件名。
    """
    ident = item["identity"]
    if ident.get("season") or ident.get("episode"):
        return "tv"
    if ident["resolved"] and ident["media_type"] in ("movie", "tv"):
        return ident["media_type"]
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
    paths = normalize_list_arg(paths, "paths")  # NEW-6：兼容 JSON 字符串
    data = _analyze_all(
        guard, limits, video_exts, subtitle_exts, media_roots, mcfg, tcfg,
        path=path, paths=paths, recursive=recursive,
    )
    items = data["items"]
    groups, suspect_groups = _group_duplicates(items)
    dup_paths = {m["path"] for g in groups for m in g["members"][1:]}

    # NEW-18：同一实体（TMDB id）分居多个剧集根的统计（复用已收集 items，
    # 不额外扫盘）；TMDB 未启用时身份不可确认，count 返回 null。
    # 根比较用剥离季/集标记的归并键（#1.1）：按季发布的一排兄弟目录不算分居。
    multi_root_shows: list[dict] = []
    multi_root_count: int | None = None
    if tcfg.enabled:
        entity_roots: dict[tuple, dict[str, str]] = {}
        entity_title: dict[tuple, str] = {}
        for it in items:
            ident = it["identity"]
            if ident.get("resolved") and ident.get("tmdb_id"):
                key = (ident.get("media_type") or "unknown", ident["tmdb_id"])
                root = _show_root_path(it["path"])
                entity_roots.setdefault(key, {})[_show_root_key(root)] = root
                entity_title.setdefault(
                    key, ident.get("canonical_title")
                    or ident.get("original_title") or "")
        raw_multi = [
            {"title": entity_title[k], "media_type": k[0], "tmdb_id": k[1],
             "roots": sorted(v.values())}
            for k, v in entity_roots.items() if len(v) > 1
        ]
        raw_multi.sort(key=lambda x: x["tmdb_id"])
        multi_root_count = len(raw_multi)
        multi_root_shows = raw_multi[:limits.max_errors_reported]

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

    # #4：探测预算耗尽时 summary 里依赖 ffprobe 的字段只是 200/261 样本口径，
    # 必须随 summary 一起标注，避免下游把部分统计当全库数字。
    probe_scoped_fields = [
        "no_subtitle_count", "no_chinese_subtitle_count",
        "low_resolution_count", "abnormal_count",
    ]
    coverage = {
        "files_total": data["total_collected"],
        "files_probed": data["probed_files"],
        "partial": data["probe_budget_exhausted"],
        "probe_scoped_fields": probe_scoped_fields,
        # 分类/重复/多根统计基于全量文件（文件名 + TMDB，不依赖 ffprobe）
        "classification_scope": "all_files",
    }

    out = {
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
            "suspect_group_count": len(suspect_groups),
            "abnormal_count": data["probe_failures"],
            "multi_root_show_count": multi_root_count,
            "coverage": coverage,
        },
        "media_type_breakdown": type_breakdown,
        "low_resolution_files": low_resolution_files,
        "multi_root_shows": multi_root_shows,
        "duplicate_groups": [
            {"title": g["title"], "year": g["year"], "member_count": g["member_count"],
             "best_path": g["members"][g["best_index"]]["path"]}
            for g in groups
        ],
        "errors": data["walk_errors"],
        "errors_summary": data.get("walk_errors_summary"),
    }
    # T3：全库扫描 0 文件 + 权限错误 → 审计统计不可信
    return mark_incomplete(
        out,
        permission_denied=(data.get("walk_errors_summary") or {}).get(
            "permission_denied", 0),
        denied_sample=data.get("walk_denied_sample", []),
        empty=data["total_collected"] == 0,
    )
