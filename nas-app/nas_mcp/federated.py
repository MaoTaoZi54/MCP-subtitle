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
from typing import Any

from . import dysbk as dysbk_mod
from . import jackett as jackett_mod
from . import seedhub as seedhub_mod
from . import pan_probe
from .config import NasConfig
from .jackett import JackettError
from .security import InvalidArguments

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
            items.append(_blank_item(
                title=title,
                magnet=magnet,
                size=m.get("size"),
                quality_tag=_quality_from_title(title),
                source="seedhub",
                updated=m.get("updated"),
                verified=True,
                confidence="high",
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
            items.append(_blank_item(
                title=title,
                pan_url=url,
                quality_tag=q_tag,
                source="seedhub",
                updated=p.get("updated"),
                verified=bool(p.get("verified")),
                confidence=p.get("confidence") or "unverified",
                mismatch=bool(p.get("mismatch")),
                warning=p.get("warning"),
                real_files=p.get("real_files"),
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
            items.append(_blank_item(
                title=title,
                pan_url=link,
                quality_tag=q_tag,
                source="dysbk",
                verified=probe_res.verified,
                confidence=probe_res.confidence,
                mismatch=probe_res.mismatch,
                warning=probe_res.mismatch_reason or probe_res.warning,
                real_files=probe_res.real_files or None,
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
    for r in (jr.get("results") or []):
        rel = r.get("release") or {}
        quality = " ".join(
            str(x) for x in (rel.get("resolution"), rel.get("source_type")) if x
        ).strip() or _quality_from_title(str(r.get("title") or ""))
        items.append(_blank_item(
            title=str(r.get("title") or ""),
            magnet=r.get("magnet"),
            size=r.get("size"),
            seeders=r.get("seeders"),
            quality_tag=quality or None,
            source="jackett",
            updated=r.get("publish_date"),
            verified=True,
            confidence="high",
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
