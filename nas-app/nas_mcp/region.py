"""国产/非国产识别（需求分析 §18）：独立工具 nas_classify_region。

设计原则：
- 音频语言是最强本机信号；内嵌/外挂字幕语言与文件名字幕标签（chs/简体 等）
  绝不能作为产地证据——任何片都可能挂中字（这正是本项目要解决的问题）；
- 文件名配音提示（国语/中配/台配/粤配/mandarin...）只作弱证据，且只能指向国产；
- tmdb.enabled 时用 TMDB original_language/origin_country 做权威判断（§18 元数据源）；
- 判定冲突（本机音轨与 TMDB 相反）→ region="unknown" 并同时列出双方依据，不强行猜测；
- 输出带 confidence（0-1），低置信度交回 Agent 决策；
- 批量调用受 media.max_classify_batch 限制，防止一次塞爆上下文/TMDB 预算。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import media as media_tools
from . import resolver as resolver_tools
from .config import MediaConfig, TmdbConfig
from .security import InvalidArguments, PathGuard
from .subhd import parse_video_filename

logger = logging.getLogger("nas_mcp.region")

# 文件名配音提示（指向中文音频，弱证据）：仅这些词可用于"国产"方向
_DUB_HINT_RE = re.compile(r"国语|普通话|中配|台配|粤配|国配|mandarin|cantonese", re.IGNORECASE)

# TMDB 产出地区代码中视为"国产"的集合（中国大陆/台湾/香港/澳门）
_CN_REGION_CODES = frozenset({"CN", "TW", "HK", "MO"})

# 判定结果取值
REGION_DOMESTIC = "domestic"
REGION_NON_DOMESTIC = "non_domestic"
REGION_UNKNOWN = "unknown"

# 置信度档位（§18 示例风格：0.96/0.92/0.48）
_CONF_TMDB_ONLY = 0.90       # 仅 TMDB 依据
_CONF_AGREE = 0.95           # 本机音轨与 TMDB 一致
_CONF_LOCAL_AUDIO = 0.85     # 仅本机音轨依据
_CONF_FNAME_HINT = 0.60      # 仅文件名配音提示（弱）
_CONF_CONFLICT = 0.55        # 本机与 TMDB 冲突
_CONF_NOTHING = 0.30         # 完全无信号


def _judge(
    audio_langs: list[str],
    fname_hint: bool,
    tmdb: dict | None,
) -> tuple[str, float, list[str]]:
    """综合判定核心（纯函数，便于单测）。返回 (region, confidence, basis)。"""
    basis: list[str] = []
    local_side: str | None = None
    if audio_langs:
        # 音轨产地判定按“中文（含繁体）”算，与字幕补全的“只认简中”不同：
        # 繁体音轨同样属于国内/华语产地
        has_zh = any(
            media_tools.is_chinese(l) or media_tools.is_traditional(l) for l in audio_langs
        )
        foreign = [
            l for l in audio_langs
            if not (media_tools.is_chinese(l) or media_tools.is_traditional(l))
        ]
        basis.append(f"音轨语言: {', '.join(audio_langs)}")
        if has_zh and not foreign:
            local_side = REGION_DOMESTIC
        elif foreign and not has_zh:
            local_side = REGION_NON_DOMESTIC
        else:
            basis.append("音轨为多语言混合，单凭音轨无法定产地")

    tmdb_side: str | None = None
    if tmdb:
        lang = str(tmdb.get("original_language") or "")
        countries = set(tmdb.get("origin_country") or [])
        basis.append(
            f"TMDB 原语言: {lang or '未知'}"
            + (f"，产出地区: {','.join(sorted(countries))}" if countries else "")
        )
        if lang or countries:
            tmdb_side = (
                REGION_DOMESTIC
                if (lang.startswith("zh") or (countries & _CN_REGION_CODES))
                else REGION_NON_DOMESTIC
            )

    if local_side and tmdb_side:
        if local_side == tmdb_side:
            return local_side, _CONF_AGREE, basis
        basis.append("本机音轨与 TMDB 元数据冲突，需要人工确认")
        return REGION_UNKNOWN, _CONF_CONFLICT, basis
    if tmdb_side:
        return tmdb_side, _CONF_TMDB_ONLY, basis
    if local_side:
        return local_side, _CONF_LOCAL_AUDIO, basis
    if fname_hint:
        basis.append("文件名含中文配音提示（国语/中配等）")
        return REGION_DOMESTIC, _CONF_FNAME_HINT, basis
    if audio_langs:
        basis.append("证据不足（音轨多语言混合且无 TMDB 元数据）")
    else:
        basis.append("无可用语言信号（音轨无语言标签、TMDB 未启用或未命中）")
    return REGION_UNKNOWN, _CONF_NOTHING, basis


def _classify_one(video: Path, mcfg: MediaConfig, tcfg: TmdbConfig) -> dict:
    """对单个视频做产地判定；probe 失败或 TMDB 失败都降级继续，不让单文件失败断路。"""
    pv = None
    probe_error: str | None = None
    try:
        pv = media_tools.probe_video(video, mcfg.ffprobe_path, mcfg.ffprobe_timeout_seconds)
    except media_tools.ProbeError as exc:
        probe_error = str(exc)

    audio_langs = (
        [s["language"] for s in pv["audio"] if s.get("language")] if pv else []
    )
    fname_hint = bool(_DUB_HINT_RE.search(video.name))

    tmdb: dict | None = None
    if tcfg.enabled:
        q = parse_video_filename(video.name)
        if q.title:
            # #1：含 SxxExx/Season 标记强制走 /search/tv（防同名电影抢实体），
            # 并与 audit/missing 共享进程级解析缓存，保证同一片名同一实体。
            hint = resolver_tools.infer_media_type(
                title=q.title, path=str(video), season=q.season, episode=q.episode
            )
            # NEW-E: 剧集文件名里的年份（如 2026）经常是本季播出年或压制年，
            # TMDB /search/tv 的 year 限制的是首播年（first_air_date_year），
            # 带上非首季年份会导致首季在早年播出的剧集无法匹配，甚至错配成同名播客等衍生节目。
            # 因此当推断为 TV 剧集（含 season/episode）时，不把单集文件名年份当首播年传入。
            search_year = None if hint == "tv" and (q.season is not None or q.episode is not None) else q.year
            try:
                r = resolver_tools.resolve_cached(
                    tcfg, q.title, year=search_year, media_type=hint
                )
            except (resolver_tools.ResolverError, InvalidArguments) as exc:
                logger.warning("region TMDB 解析失败 %s: %s", video.name, exc)
            else:
                if r.get("resolved") and r.get("identity"):
                    ident = r["identity"]
                    tmdb = {
                        "query": f"{q.title} {q.year or ''}".strip(),
                        "canonical_title": ident["canonical_title"],
                        "original_language": ident["original_language"],
                        "origin_country": ident["origin_country"],
                        "tmdb_id": ident["tmdb_id"],
                    }

    region, confidence, basis = _judge(audio_langs, fname_hint, tmdb)
    return {
        "name": video.name,
        "path": str(video),
        "probe_ok": pv is not None,
        "probe_error": probe_error,
        "region": region,
        "confidence": confidence,
        "audio_languages": audio_langs,
        "tmdb": tmdb,
        "basis": basis,
    }


def classify(
    guard: PathGuard,
    mcfg: MediaConfig,
    tcfg: TmdbConfig,
    video_exts: frozenset[str],
    *,
    path: str = "",
    paths: list[str] | None = None,
) -> dict:
    """nas_classify_region 入口：path（单个）或 paths（小批量，受上限约束）二选一。"""
    if path and paths:
        raise InvalidArguments("path 与 paths 只能二选一")
    if not path and not paths:
        raise InvalidArguments("必须提供 path（单个视频）或 paths（批量列表）")

    files = [path] if path else list(paths or [])
    max_batch = mcfg.max_classify_batch
    if len(files) > max_batch:
        raise InvalidArguments(
            f"批量上限 {max_batch} 个（media.max_classify_batch），得到 {len(files)} 个；请分多次调用"
        )

    results: list[dict] = []
    for raw in files:
        resolved = guard.check(raw)
        if not resolved.exists():
            raise FileNotFoundError(f"文件不存在: {raw}")
        if not resolved.is_file():
            raise InvalidArguments(f"不是文件: {raw}")
        ext = resolved.suffix.lstrip(".").lower()
        if ext not in video_exts:
            raise InvalidArguments(
                f"不是支持的视频文件（.{ext}）；支持的扩展名: {sorted(video_exts)}"
            )
        results.append(_classify_one(resolved, mcfg, tcfg))

    counts = {
        REGION_DOMESTIC: 0,
        REGION_NON_DOMESTIC: 0,
        REGION_UNKNOWN: 0,
    }
    for r in results:
        counts[r["region"]] += 1
    return {
        "ok": True,
        "tmdb_enabled": tcfg.enabled,
        "total": len(results),
        "summary": counts,
        "results": results,
    }
