# -*- coding: utf-8 -*-
"""三路并行合并搜索（S7）：SeedHub + 网盘资源分享社 + Jackett → media_search。

设计（2026-10-11 与用户确认）：
- 三路**并行 fan-out**（ThreadPoolExecutor），不是串行顺延：某一路失败只记
  site_errors，绝不影响其他路、绝不整批重试；
- 单路预算（方案 A）：dysbk 10s / SeedHub 20s / Jackett 沿用
  jackett.timeout_seconds——总耗时 = 最慢路封顶；
- 语言路由（前置过滤）：查询以 ASCII 字母为主（英文原名）→ 跳过 SeedHub
  （它只吃中文，查了必空），且不计入 site_errors；中文为主 → 三路全发；
- 三路 raw 结果绝不同进上下文：插件内存里合并（infohash/链接去重 →
  每部作品 top3 → 总闸 20 条）后才出闸，附 sources/merged/truncated 统计。
"""

from __future__ import annotations

import concurrent.futures as futures
import logging
import re
import time
from types import SimpleNamespace
from typing import Any

from . import dysbk as dysbk_mod
from . import jackett as jackett_mod
from . import seedhub as seedhub_mod
from . import pan_probe
from .config import NasConfig
from .fs_utils import human_size
from .jackett import JackettError
from .organizer import _SEASON_WORD_RE
from .release import parse_release
from .security import InvalidArguments
from .subhd import is_season_pack, parse_video_filename, _norm_release

logger = logging.getLogger("nas_mcp.federated")

MAX_MERGED_RESULTS = 20          # 总闸：合并后最多出闸条数
TOP_PER_WORK = 3                 # 每部作品只保留 top3 版本
_SEEDHUB_TOP_MOVIES = 3          # SeedHub 分支最多展开的搜索结果数

# 标题里能直接看到的画质标签（不臆造：只在标题真实命中时才有值）
_QUALITY_TAG_RE = re.compile(
    r"4K|2160p|1080p|1080i|720p|蓝光|BluRay|REMUX|杜比|无损|HDR10\+?|"
    r"WEB-DL|WEBRip|HDTV",
    re.I,
)
# 分组归一化时要剔除的画质/编码噪声词（不同版本同属一部作品）
_GROUP_STRIP_RE = re.compile(
    r"4k|2160p|1080p|1080i|720p|remux|blu-?ray|web-?dl|webrip|hdtv|uhd|"
    r"x26[45]|hevc|avc|hdr10?\+?|dolby|atmos|truehd|dts-?(?:hd)?|aac|"
    r"无损|蓝光|杜比|中字|中英|特效|双语|国语|粤语|字幕|合集|全集",
    re.I,
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_NON_WORD_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_INFOHASH_RE = re.compile(r"xt=urn:btih:([A-Fa-f0-9]{40}|[A-Za-z2-7]{32})", re.I)

# 合并后展示的源优先级（只决定排序，不决定发不发请求）
_SOURCE_PRIORITY = {"dysbk": 0, "seedhub": 1, "jackett": 2}
_CONFIDENCE_RANK = {
    "high": 0, "suspicious": 1, "partial": 2, "unverified": 3, "mismatch": 4,
}


def _blank_item(**fields: Any) -> dict:
    """统一 schema：字段缺失置 null，不臆造。"""
    item = {
        "title": "",
        "magnet": None,
        "pan_url": None,
        "size": None,
        "seeders": None,
        "quality_tag": None,
        "source": "",
        "updated": None,
        "verified": False,
        "confidence": "unverified",
        "mismatch": False,
        "warning": None,
        "real_files": None,
    }
    item.update(fields)
    return item


def _quality_from_title(title: str) -> str | None:
    """标题命中画质词才给标签（最多两个），否则 None。"""
    tags = list(dict.fromkeys(m.group(0) for m in _QUALITY_TAG_RE.finditer(title)))
    return " ".join(tags[:2]) if tags else None


def _is_ascii_dominant(query: str) -> bool:
    """英文原名（ASCII 字母为主）→ True，此时 SeedHub 被路由跳过。"""
    ascii_letters = sum(1 for c in query if c.isascii() and c.isalpha())
    cjk = sum(1 for c in query if "\u4e00" <= c <= "\u9fff")
    return ascii_letters > cjk


def _err_entry(source: str, code: str, message: str, **extra: Any) -> dict:
    return {"source": source, "code": code, "message": message, **extra}


# --------------------------------------------------------------------------- #
# 相关性可信度（R1）：磁力结果必须基于实际证据判定，不能一律 verified/high。
#
# 复用现有能力，不另写一套解析：
#   - 片名/年份/季集：subhd.parse_video_filename（与字幕匹配同一解析器）；
#   - 整季包识别：subhd.is_season_pack；
#   - 片名归一化：subhd._norm_release（点号/空格/大小写不敏感）；
#   - 画质维度：release.parse_release（分辨率/来源/编码）。
#
# 字段语义：
#   confidence: high       标题完整命中、季集/年份一致、元数据无明显异常
#               partial    部分命中（缺季/集/年份或只命中部分片名词）
#               suspicious 标题命中但做种/体积等元数据明显异常
#               mismatch   标题/年份/季集明确冲突
#               unverified 标题为空或无法解析，无法判定
#   verified:   磁力只做标题/元数据核对，未探测内容 → 恒为 False；
#               网盘由 pan_probe 实时探测，仍按它的结果（口径分开）。
#   mismatch:   仅表示标题/年份/季集冲突，不把体积异常当标题不匹配。
#   warning:    始终写明判定依据；磁力条目末尾标明「未校验实际内容」。
# --------------------------------------------------------------------------- #

_SIZE_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT]?B|[KMGT])\b", re.I)
_SIZE_UNITS = {
    "B": 1,
    "KB": 1024, "K": 1024,
    "MB": 1024 ** 2, "M": 1024 ** 2,
    "GB": 1024 ** 3, "G": 1024 ** 3,
    "TB": 1024 ** 4, "T": 1024 ** 4,
}
_RELEVANCE_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
# organizer._SEASON_WORD_RE 覆盖 Season 10 / S10 / 第10季；补上「第六季」这类中文数字。
_CN_SEASON_RE = re.compile(r"第\s*[一二三四五六七八九十百零两]+\s*季", re.I)

# 与 pan_probe 同口径的体积下限（4K 单集 < 1.2GB / 4K 电影 < 3GB）。
_4K_EPISODE_MIN_BYTES = 1_200_000_000
_4K_MOVIE_MIN_BYTES = 3_000_000_000
_1080P_EPISODE_MIN_BYTES = 200_000_000
_1080P_MOVIE_MIN_BYTES = 500_000_000
_720P_MOVIE_MIN_BYTES = 250_000_000
_ABSURD_SIZE_BYTES = 50_000_000


def _size_to_bytes(value: Any) -> int | None:
    """把站点给的大小（int 字节或 "12GB"/"[49.61G]" 字符串）统一成字节。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    match = _SIZE_UNIT_RE.search(text)
    if match:
        try:
            return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).upper()])
        except (KeyError, ValueError):
            return None
    return int(text) if text.isdigit() else None


def _relevance_tokens(text: str) -> list[str]:
    """片名切词：中英文按边界拆开，供「部分命中」逐词核对。"""
    return [
        token for token in _RELEVANCE_TOKEN_RE.findall((text or "").lower())
        if len(token) >= 2 or token.isdigit()
    ]


def _title_tokens(text: str) -> list[str]:
    """片名切词前先剥离季标记（第X季 / Season X / SXX），避免「第六季」泛匹配。"""
    cleaned = _SEASON_WORD_RE.sub(" ", text or "")
    cleaned = _CN_SEASON_RE.sub(" ", cleaned)
    return _relevance_tokens(cleaned)


def _title_match(query: str, title: str,
                 aliases: list[tuple[str, str]]) -> dict:
    """片名匹配（复用 subhd 标题解析/归一化）。

    aliases 为 [(别名, 来源)]；来源 "query"=用户输入，"resolved"=TMDB 解析别名。
    返回 state ∈ full/partial/mismatch，以及命中的别名与缺失词。
    """
    parsed_query = parse_video_filename(query)
    parsed_result = parse_video_filename(title)
    result_title = _norm_release(parsed_result.title or title)
    result_tokens = set(_title_tokens(result_title))

    candidates: list[tuple[str, str]] = []
    if parsed_query.title:
        candidates.append((parsed_query.title, "query"))
    candidates.append((query, "query"))
    candidates.extend(aliases)

    seen: set[str] = set()
    best_partial: tuple[float, str, str, list[str]] | None = None
    for text, basis in candidates:
        normalized = _norm_release(text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        tokens = _title_tokens(normalized)
        if tokens and all(token in result_tokens for token in tokens):
            return {"state": "full", "alias": text.strip(), "basis": basis,
                    "missing": []}
        if not tokens and normalized == result_title:
            return {"state": "full", "alias": text.strip(), "basis": basis,
                    "missing": []}
        if tokens:
            hits = [token for token in tokens if token in result_tokens]
            if hits:
                coverage = len(hits) / len(tokens)
                missing = [token for token in tokens if token not in result_tokens]
                if best_partial is None or coverage > best_partial[0]:
                    best_partial = (coverage, text.strip(), basis, missing)
    if best_partial is not None:
        return {"state": "partial", "alias": best_partial[1],
                "basis": best_partial[2], "missing": best_partial[3]}
    return {"state": "mismatch", "alias": "", "basis": "", "missing": []}


def _year_season_evidence(query: str, title: str) -> tuple[list[str], list[str]]:
    """年份/季集核对（复用 parse_video_filename 的季集识别）。

    返回 (冲突说明, 缺失说明)：冲突 → mismatch；缺失 → partial。
    """
    parsed_query = parse_video_filename(query)
    parsed_result = parse_video_filename(title)
    conflicts: list[str] = []
    missing: list[str] = []

    if parsed_query.year is not None:
        if parsed_result.year is None:
            missing.append(f"结果未标年份（查询 {parsed_query.year}）")
        elif parsed_result.year != parsed_query.year:
            conflicts.append(
                f"年份不一致（查询 {parsed_query.year} / 结果 {parsed_result.year}）")

    if parsed_query.season is not None:
        if parsed_result.season is None:
            missing.append(f"结果未标季（查询 S{parsed_query.season:02d}）")
        elif parsed_result.season != parsed_query.season:
            conflicts.append(
                f"季不一致（查询 S{parsed_query.season:02d} / "
                f"结果 S{parsed_result.season:02d}）")
        elif parsed_query.episode is not None:
            if parsed_result.episode is None:
                kind = "整季包未标集" if is_season_pack(title) else "未标注集号"
                missing.append(
                    f"{kind}（查询 S{parsed_query.season:02d}"
                    f"E{parsed_query.episode:02d}）")
            elif parsed_result.episode != parsed_query.episode:
                conflicts.append(
                    f"集不一致（查询 E{parsed_query.episode:02d} / "
                    f"结果 E{parsed_result.episode:02d}）")
    return conflicts, missing


def _metadata_evidence(title: str, size: Any, seeders: int | None,
                       media_type: str) -> tuple[list[str], list[str]]:
    """做种数/体积 vs 分辨率合理性核对。

    返回 (明显异常, 缺失/弱提示)。异常会把 confidence 降到 suspicious，
    缺失信息只进 warning，不假装「没问题」。
    """
    issues: list[str] = []
    notes: list[str] = []
    size_bytes = _size_to_bytes(size)
    parsed = parse_video_filename(title)
    is_tv = (
        media_type == "tv"
        or parsed.season is not None
        or parsed.episode is not None
        or is_season_pack(title)
    )
    resolution = parse_release(title).resolution
    single_episode = parsed.episode is not None

    if size_bytes is None:
        notes.append("体积未知")
    else:
        size_text = human_size(size_bytes)
        if resolution in ("4320p", "2160p"):
            if is_tv and single_episode and size_bytes < _4K_EPISODE_MIN_BYTES:
                issues.append(
                    f"{resolution} 单集体积仅 {size_text}，低于正常码率下限")
            elif not is_tv and size_bytes < _4K_MOVIE_MIN_BYTES:
                issues.append(
                    f"{resolution} 电影体积仅 {size_text}，低于正常码率下限")
        elif resolution == "1080p":
            if is_tv and single_episode and size_bytes < _1080P_EPISODE_MIN_BYTES:
                issues.append(f"1080p 单集体积仅 {size_text}，明显偏低")
            elif not is_tv and size_bytes < _1080P_MOVIE_MIN_BYTES:
                issues.append(f"1080p 电影体积仅 {size_text}，明显偏低")
        elif resolution == "720p" and not is_tv and size_bytes < _720P_MOVIE_MIN_BYTES:
            issues.append(f"720p 电影体积仅 {size_text}，明显偏低")
        if not issues and size_bytes < _ABSURD_SIZE_BYTES:
            issues.append(f"体积仅 {size_text}，明显异常")

    if seeders is None:
        notes.append("站点未提供做种数")
    elif seeders == 0:
        issues.append("做种数为 0，可能无可用下载源")
    elif seeders <= 2:
        notes.append(f"做种数仅 {seeders}")
    return issues, notes


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def _relation_evidence(query: str, title: str, *, source_title: str | None = None,
                       aliases: list[tuple[str, str]] | None = None,
                       alias_low_confidence: bool = False) -> dict:
    """只看「与查询是否相关」：片名/年份/季集 + 来源作品上下文。"""
    aliases = aliases or []
    direct = _title_match(query, title, aliases)
    source = _title_match(query, source_title, []) if source_title else None
    conflicts, missing = _year_season_evidence(query, title)

    result_title = parse_video_filename(title).title or title
    result_title_norm = _norm_release(result_title)
    query_title_norm = _norm_release(parse_video_filename(query).title or "")
    query_has_cjk = _has_cjk(query_title_norm or query)
    result_has_cjk = _has_cjk(result_title_norm or result_title)
    cross_script = (query_has_cjk and not result_has_cjk) or (
        result_has_cjk and not query_has_cjk)

    parts: list[str] = []
    confidence = "unverified"
    mismatch = False
    source_used = False

    if not result_title_norm:
        parts.append("结果标题为空或无法解析，无法判定相关性")
    elif not query_title_norm:
        parts.append("查询中没有可识别的片名，无法判定相关性")
    elif direct["state"] == "full":
        basis = "别名" if direct["basis"] == "resolved" else "片名"
        parts.append(f"标题完整匹配（{basis}「{direct['alias']}」）")
        if conflicts:
            confidence = "mismatch"
            mismatch = True
        elif missing:
            confidence = "partial"
        elif alias_low_confidence and direct["basis"] == "resolved":
            confidence = "partial"
            parts.append("TMDB 片名解析低置信，别名命中仅供参考")
        else:
            confidence = "high"
    elif direct["state"] == "partial":
        missing_words = "、".join(direct["missing"]) or "部分片名词"
        parts.append(f"部分命中，缺少「{missing_words}」")
        confidence = "mismatch" if conflicts else "partial"
        mismatch = bool(conflicts)
    elif source is not None and source["state"] in ("full", "partial"):
        # SeedHub 作品页等来源上下文：条目标题常为英文发布名，按作品判定。
        source_used = True
        parts.append(
            f"来源作品「{source['alias']}」与查询匹配；条目标题「{result_title}」"
            "为发布名、未直接命中查询，按来源作品判定")
        if conflicts:
            confidence = "mismatch"
            mismatch = True
        elif missing or source["state"] == "partial":
            confidence = "partial"
        else:
            confidence = "high"
    elif cross_script:
        parts.append("查询与结果标题语种不同，缺少片名对应关系，无法判定相关性")
    else:
        parts.append(f"标题不匹配：查询「{query}」，结果「{result_title}」")
        confidence = "mismatch"
        mismatch = True

    parts.extend(conflicts)
    parts.extend(missing)
    return {
        "confidence": confidence,
        "mismatch": mismatch,
        "parts": parts,
        "source_used": source_used,
    }


def _assess_release(query: str, title: str, *, size: Any = None,
                    seeders: int | None = None,
                    aliases: list[tuple[str, str]] | None = None,
                    alias_low_confidence: bool = False,
                    media_type: str = "",
                    source_title: str | None = None) -> dict:
    """按证据给出磁力条目的 verified/confidence/mismatch/warning。"""
    relation = _relation_evidence(
        query, title, source_title=source_title, aliases=aliases,
        alias_low_confidence=alias_low_confidence)
    issues, notes = _metadata_evidence(title, size, seeders, media_type)

    confidence = relation["confidence"]
    if confidence == "high" and issues:
        confidence = "suspicious"

    parts = relation["parts"][:] + issues + notes + ["磁力未校验实际内容"]
    return {
        "verified": False,
        "confidence": confidence,
        "mismatch": relation["mismatch"],
        "warning": "；".join(parts),
    }


def _assess_pan(query: str, title: str, probe: Any, *,
                source_title: str | None = None) -> dict:
    """网盘条目：标题相关性与 pan_probe 实时探测合并，口径与磁力分开。"""
    relation = _relation_evidence(query, title, source_title=source_title)
    parts = relation["parts"][:]

    mismatch = bool(relation["mismatch"] or probe.mismatch)
    if probe.mismatch_reason:
        parts.append(f"网盘实测：{probe.mismatch_reason}")
    elif probe.warning:
        parts.append(f"网盘实测：{probe.warning}")
    elif probe.verified:
        parts.append("网盘已实时探测，文件规格与分享一致")
    else:
        parts.append("网盘未完成内容校验")

    confidence = relation["confidence"]
    if confidence == "high":
        if probe.mismatch or probe.confidence == "mismatch":
            confidence = "suspicious"
        elif probe.confidence == "high" and probe.verified:
            confidence = "high"
        else:
            confidence = "unverified"
    elif confidence not in ("mismatch", "partial"):
        confidence = "unverified"

    return {
        "verified": bool(probe.verified),
        "confidence": confidence,
        "mismatch": mismatch,
        "warning": "；".join(parts),
    }


def _from_module_error(source: str, payload: dict) -> dict | None:
    """模块结构化错误 → site_errors 条目；no_results/disabled 不算失败。"""
    err = payload.get("error") or {}
    code = str(err.get("code") or "unknown")
    if code in ("no_results", "disabled"):
        return None
    extra = {k: v for k, v in err.items()
             if k in ("retryable", "retry_after", "status_code")}
    return _err_entry(source, code, str(err.get("message") or code), **extra)


# --------------------------------------------------------------------------- #
# 三个分支（各自独立预算，返回 (unified_items, site_error|None)）
# --------------------------------------------------------------------------- #


def _seedhub_branch(cfg: NasConfig, query: str) -> tuple[list[dict], dict | None]:
    scfg = cfg.seedhub
    if not scfg.enabled:
        return [], None
    if seedhub_mod.cf_requests is None:
        return [], _err_entry(
            "seedhub", "dependency_missing",
            "curl_cffi 未安装，SeedHub 不可用（其余工具不受影响）")
    budget = cfg.media_search.seedhub_timeout_seconds
    deadline = time.monotonic() + budget

    sr = seedhub_mod.search(scfg, query, timeout=budget)
    if not sr.get("ok"):
        err = _from_module_error("seedhub", sr)
        return [], err

    items: list[dict] = []
    for movie in (sr.get("results") or [])[:_SEEDHUB_TOP_MOVIES]:
        if time.monotonic() > deadline:
            break
        mid = movie.get("id")
        base_title = str(movie.get("title") or "")
        mr = seedhub_mod.magnets(
            scfg, mid, 3, timeout=budget, deadline=deadline)
        if not mr.get("ok"):
            err = _from_module_error("seedhub", mr)
            if err:
                return items, err
        for m in (mr.get("results") or []):
            magnet = m.get("magnet")
            if not magnet:                     # resolve_failed 不进结果
                continue
            title = str(m.get("title") or base_title)
            verdict = _assess_release(
                query, title, size=m.get("size"), source_title=base_title)
            items.append(_blank_item(
                title=title,
                magnet=magnet,
                size=m.get("size"),
                quality_tag=_quality_from_title(title),
                source="seedhub",
                updated=m.get("updated"),
                **verdict,
            ))
        if time.monotonic() > deadline:
            break
        pr = seedhub_mod.pans(
            scfg, mid, 2, timeout=budget, deadline=deadline)
        if not pr.get("ok"):
            err = _from_module_error("seedhub", pr)
            if err:
                return items, err
        for p in (pr.get("results") or []):
            url = p.get("url")
            if not url:
                continue
            title = str(p.get("title") or base_title)
            q_tag = p.get("actual_quality") or _quality_from_title(title)
            probe_view = SimpleNamespace(
                verified=bool(p.get("verified")),
                confidence=p.get("confidence") or "unverified",
                mismatch=bool(p.get("mismatch")),
                mismatch_reason=p.get("warning") if p.get("mismatch") else None,
                warning=None if p.get("mismatch") else p.get("warning"),
            )
            verdict = _assess_pan(query, title, probe_view, source_title=base_title)
            items.append(_blank_item(
                title=title,
                pan_url=url,
                quality_tag=q_tag,
                source="seedhub",
                updated=p.get("updated"),
                real_files=p.get("real_files"),
                **verdict,
            ))
    return items, None


def _dysbk_branch(cfg: NasConfig, query: str) -> tuple[list[dict], dict | None]:
    dcfg = cfg.dysbk
    if not dcfg.enabled:
        return [], None
    budget = cfg.media_search.dysbk_timeout_seconds
    deadline = time.monotonic() + budget

    sr = dysbk_mod.search(dcfg, query, limit=5, timeout=budget)
    if not sr.get("ok"):
        return [], _from_module_error("dysbk", sr)

    items: list[dict] = []
    for disc in (sr.get("results") or [])[:5]:
        if time.monotonic() > deadline:
            break
        lr = dysbk_mod.links(
            dcfg, int(disc["id"]),
            timeout=max(1.0, deadline - time.monotonic()),
        )
        if not lr.get("ok"):
            err = _from_module_error("dysbk", lr)
            if err:
                return items, err
            continue
        title = str(disc.get("title") or "")
        for link in (lr.get("links") or []):
            probe_res = pan_probe.probe_pan_url(link, claimed_title=title, timeout=2.0)
            q_tag = probe_res.actual_quality or _quality_from_title(title)
            verdict = _assess_pan(query, title, probe_res)
            items.append(_blank_item(
                title=title,
                pan_url=link,
                quality_tag=q_tag,
                source="dysbk",
                real_files=probe_res.real_files or None,
                **verdict,
            ))
    return items, None


def _jackett_branch(cfg: NasConfig, query: str) -> tuple[list[dict], dict | None]:
    if not cfg.jackett.enabled:
        return [], None
    try:
        jr = jackett_mod.search(
            cfg.jackett, cfg.tmdb, query=query, limit=0)
    except JackettError as exc:
        return [], _err_entry(
            "jackett", exc.code, str(exc),
            retryable=exc.retryable, retry_after=exc.retry_after)
    except InvalidArguments as exc:
        return [], _err_entry("jackett", "invalid_arguments", str(exc))

    items: list[dict] = []
    resolved = jr.get("resolved") or {}
    alias_specs: list[tuple[str, str]] = []
    alias_low_confidence = False
    media_type = ""
    if isinstance(resolved, dict):
        alias_low_confidence = bool(resolved.get("low_confidence"))
        media_type = str(resolved.get("media_type") or "")
        for key in ("canonical_title", "original_title"):
            value = resolved.get(key)
            if value:
                alias_specs.append((str(value), "resolved"))
    for r in (jr.get("results") or []):
        rel = r.get("release") or {}
        quality = " ".join(
            str(x) for x in (rel.get("resolution"), rel.get("source_type")) if x
        ).strip() or _quality_from_title(str(r.get("title") or ""))
        title = str(r.get("title") or "")
        verdict = _assess_release(
            query, title,
            size=r.get("size"),
            seeders=r.get("seeders"),
            aliases=alias_specs,
            alias_low_confidence=alias_low_confidence,
            media_type=media_type,
        )
        items.append(_blank_item(
            title=title,
            magnet=r.get("magnet"),
            size=r.get("size"),
            seeders=r.get("seeders"),
            quality_tag=quality or None,
            source="jackett",
            updated=r.get("publish_date"),
            **verdict,
        ))
    return items, None


# --------------------------------------------------------------------------- #
# 合并：去重 → 排序 → 每部 top3 → 总闸 20
# --------------------------------------------------------------------------- #


def _infohash(magnet: str | None) -> str:
    if not magnet:
        return ""
    m = _INFOHASH_RE.search(magnet)
    return m.group(1).lower() if m else ""


def _group_key(title: str) -> str:
    """作品分组键：剔除画质/年份噪声后归一化（大小写/分隔符不敏感）。"""
    t = _GROUP_STRIP_RE.sub(" ", title)
    t = _YEAR_RE.sub(" ", t)
    return _NON_WORD_RE.sub("", t.lower())


def _rank(item: dict) -> tuple:
    """排序：虚标置底；有网盘直链 > 有做种数 > 画质标签；再按源优先级/做种数。"""
    return (
        1 if item.get("mismatch") else 0,
        0 if item.get("pan_url") else 1,
        _CONFIDENCE_RANK.get(str(item.get("confidence") or ""), 3),
        0 if item.get("seeders") is not None else 1,
        0 if item.get("quality_tag") else 1,
        _SOURCE_PRIORITY.get(item.get("source", ""), 9),
        -(item.get("seeders") or 0),
        item.get("title") or "",
    )


def _merge(by_source: dict[str, list[dict]]) -> tuple[list[dict], dict]:
    """去重（infohash/标题、网盘链接）+ 排序 + 每部 top3 + 总闸。"""
    ordered: list[dict] = []
    for source in ("dysbk", "seedhub", "jackett"):
        ordered.extend(by_source.get(source, []))
    ordered.sort(key=_rank)

    seen_hash: set[str] = set()
    seen_pan: set[str] = set()
    seen_plain: set[str] = set()
    deduped: list[dict] = []
    for item in ordered:
        h = _infohash(item.get("magnet"))
        pan = (item.get("pan_url") or "").strip()
        if h and h in seen_hash:
            continue
        if pan and pan in seen_pan:
            continue
        if not h and not pan:
            plain = _NON_WORD_RE.sub("", (item.get("title") or "").lower())
            if plain and plain in seen_plain:
                continue
            seen_plain.add(plain)
        if h:
            seen_hash.add(h)
        if pan:
            seen_pan.add(pan)
        deduped.append(item)

    # 每部作品 top3（deduped 已按 _rank 排好序，组内靠前即最优）
    groups: dict[str, int] = {}
    capped: list[dict] = []
    for item in deduped:
        key = _group_key(item.get("title") or "") or "|"
        used = groups.get(key, 0)
        if used >= TOP_PER_WORK:
            continue
        groups[key] = used + 1
        capped.append(item)

    results = capped[:MAX_MERGED_RESULTS]
    stats = {
        "merged": len(capped),
        "truncated": len(capped) - len(results),
    }
    return results, stats


# --------------------------------------------------------------------------- #
# 公开入口
# --------------------------------------------------------------------------- #


def search(cfg: NasConfig, query: str) -> dict:
    """三路并行搜索并合并（media_search 的实现体）。"""
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": {
            "code": "invalid_arguments", "message": "query 不能为空"}}

    started = time.monotonic()
    skip_seedhub = _is_ascii_dominant(query)

    branches: list[tuple[str, Any, float]] = [
        ("dysbk", lambda: _dysbk_branch(cfg, query),
         cfg.media_search.dysbk_timeout_seconds),
        ("jackett", lambda: _jackett_branch(cfg, query),
         float(cfg.jackett.timeout_seconds)),
    ]
    if not skip_seedhub:
        branches.insert(1, (
            "seedhub",
            lambda: _seedhub_branch(cfg, query),
            cfg.media_search.seedhub_timeout_seconds,
        ))

    # 并行 fan-out：单路失败只记 site_errors，其余照常返回
    t0 = time.monotonic()
    by_source: dict[str, list[dict]] = {"seedhub": [], "dysbk": [], "jackett": []}
    site_errors: list[dict] = []
    pool = futures.ThreadPoolExecutor(max_workers=3)
    running = {name: pool.submit(fn) for name, fn, _b in branches}
    try:
        for name, _fn, budget in branches:
            fut = running[name]
            # 各路预算从分支启动时刻起算（并行等待，总耗时 ≈ 最慢路）
            wait = max(0.05, t0 + budget - time.monotonic())
            try:
                items, err = fut.result(timeout=wait)
            except futures.TimeoutError:
                items, err = [], _err_entry(
                    name, f"{name}_timeout",
                    f"{name} 单路预算 {budget:.0f}s 已用尽，本路未响应")
            except Exception as exc:  # noqa: BLE001 - 单路异常不许拖垮整批
                logger.warning("media_search %s 分支异常: %s", name, type(exc).__name__)
                items, err = [], _err_entry(
                    name, "branch_error", f"{name} 分支异常（{type(exc).__name__}）")
            by_source[name] = items or []
            if err:
                site_errors.append(err)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # site_errors 按 (source, code) 归并去重
    dedup_errors: list[dict] = []
    seen_err: set[tuple[str, str]] = set()
    for e in site_errors:
        key = (e.get("source", ""), e.get("code", ""))
        if key in seen_err:
            continue
        seen_err.add(key)
        dedup_errors.append(e)

    results, stats = _merge(by_source)
    return {
        "ok": True,
        "query": query,
        "results": results,
        "sources": {
            "seedhub": len(by_source["seedhub"]),
            "dysbk": len(by_source["dysbk"]),
            "jackett": len(by_source["jackett"]),
            **({"jackett_skipped_by_config": True} if not cfg.jackett.enabled else {}),
        },
        "merged": stats["merged"],
        "truncated": stats["truncated"],
        "site_errors": dedup_errors,
        # 语言路由可观测：True=本查询跳过了 SeedHub（英文为主，非失败）
        "seedhub_skipped_by_language": skip_seedhub,
        "jackett_skipped_by_config": not cfg.jackett.enabled,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
