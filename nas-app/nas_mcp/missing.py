# -*- coding: utf-8 -*-
"""媒体缺集检测（v1.7.2 阶段一，只读）。

media_missing_episodes：以 TMDB 官方集数列表为 expected 基线，比对本地实际
持有的集数，报告缺失集号。expected 必须来自 TMDB（tv season endpoint），
不得用本地最大集数推断。

核心规则：
  - TMDB 不可用 / 查询失败 / 标题歧义 → expected=null, missing=null,
    reason="expected_episode_set_unavailable"（绝不臆测集数）。
  - title 歧义 → title_resolution="ambiguous"，不猜测（§A4）。
  - Season 0 默认不参与（Specials）；显式 season=0 才检查 Specials。
  - 权限错误只进 errors，绝不当 missing。
  - 分页 limit/offset/truncated/next_offset。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from . import resolver as resolver_tools
from .config import Limits, MediaConfig, TmdbConfig, TvmazeConfig
from .fs import _flat_records, _walk_records
from .fs_utils import ErrorSink, make_skip_matcher, mark_incomplete_from_sink, normalize_list_arg
from .progress import check_canceled, mark_partial, report
from .resolver import ResolverError
from .security import InvalidArguments, PathGuard, expand_roots
from .subhd import parse_video_filename

logger = logging.getLogger("nas_mcp.missing")


def _norm_title(s: str) -> str:
    """规范化片名用于本地文件↔TMDB 比对（去空白、小写、去非字母数字/CJK）。"""
    return re.sub(r"[^a-z0-9一-鿿]+", "", (s or "").lower())


def _scan_local_episodes(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    mcfg: MediaConfig,
    *,
    path: str,
    paths: list[str] | None,
    title_norm: str | None,
) -> tuple[list[dict], "ErrorSink", bool]:
    """扫描本地视频，返回 (records, error_sink, scan_truncated)。

    每条 record：{path, name, title(parsed), season, episode, title_norm}。
    title_norm 非空时只保留片名匹配的文件（单剧模式）；为空时返回全部（扫剧模式）。
    """
    is_skipped = make_skip_matcher(mcfg.skip_filename_patterns)
    sink = ErrorSink(limits.max_errors_reported)
    state = {"truncated": False}

    def _on_error(p, msg: str) -> None:
        sink.add(p, msg)

    # 确定遍历基目录：优先 path/paths，否则 media_roots
    if paths:
        bases: list[Path] = []
        for raw in paths:
            try:
                p = guard.check(raw)
            except Exception as exc:  # AccessDenied 等
                sink.add(raw, str(exc))
                continue
            if p.is_file() and p.suffix.lstrip(".").lower() in video_exts:
                bases.append(p)
            elif p.is_dir():
                bases.append(p)
        # 文件直接处理，目录遍历
        file_targets = [b for b in bases if b.is_file()]
        dir_targets = [b for b in bases if b.is_dir()]
    elif path:
        base = guard.check(path)
        if base.is_file():
            file_targets = [base]
            dir_targets = []
        elif base.is_dir():
            file_targets = []
            dir_targets = [base]
        else:
            raise FileNotFoundError(f"路径不存在或不是目录: {path}")
    else:
        file_targets = []
        dir_targets = []
        for r in expand_roots(media_roots):
            try:
                b = guard.check(r)
            except Exception as exc:
                sink.add(r, str(exc))
                continue
            if b.is_dir() and b not in dir_targets:
                dir_targets.append(b)

    records: list[dict] = []

    def _emit(file_path: Path) -> None:
        if file_path.suffix.lstrip(".").lower() not in video_exts:
            return
        if is_skipped(file_path.name):
            return
        q = parse_video_filename(file_path.name)
        if not q.title:
            return
        tn = _norm_title(q.title)
        if title_norm is not None and tn != title_norm:
            # 单剧模式：片名不匹配则跳过（不在此处报错）
            return
        records.append({
            "path": str(file_path),
            "name": file_path.name,
            "title": q.title,
            "title_norm": tn,
            "season": q.season,
            "episode": q.episode,
        })

    target_count = len(file_targets) + len(dir_targets)
    processed_targets = 0
    for f in file_targets:
        if check_canceled():
            break
        report(
            "scan", "扫描本地剧集文件",
            done=processed_targets, total=target_count, current=str(f),
        )
        _emit(f)
        processed_targets += 1
    for base in dir_targets:
        if check_canceled():
            break
        report(
            "scan", "扫描本地剧集文件",
            done=processed_targets, total=target_count, current=str(base),
        )
        for rec in _walk_records(
            base, guard, limits.max_scan_files, limits.max_list_depth,
            include_dirs=False, video_exts=video_exts, subtitle_exts=subtitle_exts,
            on_error=_on_error, state=state,
        ):
            if check_canceled():
                break
            if rec["media_category"] != "video":
                continue
            _emit(Path(rec["path"]))
        processed_targets += 1

    return records, sink, state["truncated"]


def _expected_episodes(client, tv_id: int, season: int) -> list[int] | None:
    """从剧集源（TMDB season endpoint / TVmaze episodes）取 expected 集号；失败返回 None。"""
    if client is None:
        return None
    try:
        data = client.tv_season(tv_id, season)
    except ResolverError as exc:
        logger.info("tv_season(%d, %d) 失败: %s", tv_id, season, exc)
        return None
    eps = data.get("episodes") if isinstance(data, dict) else None
    if not isinstance(eps, list) or not eps:
        return None
    out: list[int] = []
    for e in eps:
        if isinstance(e, dict) and e.get("episode_number") is not None:
            try:
                out.append(int(e["episode_number"]))
            except (TypeError, ValueError):
                continue
    return sorted(set(out)) if out else None


def _resolve_tv(
    tcfg: TmdbConfig,
    tvcfg: TvmazeConfig | None,
    title: str,
    year: int | None,
) -> dict:
    """解析电视剧身份。

    返回 {resolved, ambiguous, entity_id, source, canonical, original, year}。
    TMDB 仍为首选；tvmaze.enabled=true 时由 resolver 在 TMDB 不可达/查不到时
    回落 TVmaze（source="tvmaze"，entity_id 为 TVmaze show id）。tmdb 关闭但
    tvmaze 开启时直接走 TVmaze。
    """
    base = {"resolved": False, "ambiguous": False, "entity_id": None,
            "source": None, "canonical": None, "original": None, "year": year}
    tvmaze_on = tvcfg is not None and tvcfg.enabled
    if not tcfg.enabled and not tvmaze_on:
        return base
    try:
        # #1：固定 media_type="tv"（缺集检测本就只针对剧集），并共享进程级
        # 解析缓存，保证与 audit/region 解析到同一实体。
        if tcfg.enabled:
            r = resolver_tools.resolve_cached(
                tcfg, title, year=year, media_type="tv", tvmaze=tvcfg)
        else:
            r = resolver_tools.resolve_tvmaze(tvcfg, title, year=year)
    except (ResolverError, InvalidArguments) as exc:
        logger.info("resolve(%r) 失败: %s", title, exc)
        return base
    cands = r.get("candidates") or []
    if r.get("resolved") and r.get("identity"):
        ident = r["identity"]
        base.update({
            "resolved": True,
            "entity_id": ident["tmdb_id"],
            "source": ident.get("metadata_source") or "tmdb",
            "canonical": ident["canonical_title"],
            "original": ident["original_title"],
            "year": ident.get("year") or year,
        })
        return base
    # 未解析但有多个候选 → 歧义
    if len(cands) >= 2:
        base["ambiguous"] = True
    return base


def _build_show_entry(
    tcfg: TmdbConfig,
    tvcfg: TvmazeConfig | None,
    *,
    title: str,
    year: int | None,
    season: int | None,
    local_records: list[dict],
) -> dict:
    """构造单剧的缺集报告条目。"""
    entry: dict = {
        "title": title,
        "year": year,
        "season": season,
        "title_resolution": "not_checked",
        "metadata_source": None,
        "tmdb_id": None,
        "expected": None,
        "actual": None,
        "missing": None,
        "reason": None,
        "files": [],
    }
    # 本地文件归属（用于 actual 计算）
    def _match_rec(rec: dict, s: int) -> bool:
        return rec.get("season") == s and rec.get("episode") is not None

    files = [{"path": r["path"], "name": r["name"], "season": r["season"],
              "episode": r["episode"]} for r in local_records]
    entry["files"] = files

    if check_canceled():
        entry["title_resolution"] = "canceled"
        entry["reason"] = "canceled"
        return entry

    if not tcfg.enabled and not (tvcfg is not None and tvcfg.enabled):
        entry["reason"] = "expected_episode_set_unavailable"
        entry["title_resolution"] = "tmdb_disabled"
        return entry

    rinfo = _resolve_tv(tcfg, tvcfg, title, year)
    if rinfo["ambiguous"]:
        entry["title_resolution"] = "ambiguous"
        entry["reason"] = "expected_episode_set_unavailable"
        return entry
    if not rinfo["resolved"]:
        entry["title_resolution"] = "not_found"
        entry["reason"] = "expected_episode_set_unavailable"
        return entry

    source = rinfo["source"] or "tmdb"
    entry["title_resolution"] = "resolved"
    entry["metadata_source"] = source
    entry["tmdb_id"] = rinfo["entity_id"]
    entity_id = rinfo["entity_id"]
    # 按实体来源选客户端：两个源的 tv_season 都返回 {"episodes": [...]}。
    if source == "tvmaze":
        client = (resolver_tools.TvmazeClient(tvcfg)
                  if tvcfg is not None and tvcfg.enabled else None)
    else:
        client = resolver_tools.TmdbClient(tcfg) if tcfg.enabled else None

    # 确定要检查的季
    if season is not None:
        seasons_to_check = [season]
    else:
        # season null：扫本地出现的季（>0）；本地无任何季则默认查第 1 季
        local_seasons = sorted({r["season"] for r in local_records
                               if r.get("season") and r["season"] > 0})
        seasons_to_check = local_seasons or [1]

    # 若该季无本地文件：actual 为空，missing=expected（全部缺失）
    per_season: list[dict] = []
    season_canceled = False
    for s in seasons_to_check:
        if check_canceled():
            season_canceled = True
            break
        report(
            "season", "获取官方集数并比对",
            done=len(per_season), total=len(seasons_to_check), current=f"Season {s}",
        )
        expected = _expected_episodes(client, entity_id, s)
        if expected is None:
            per_season.append({
                "season": s, "expected": None, "actual": None,
                "missing": None, "reason": "expected_episode_set_unavailable",
            })
            continue
        actual = sorted({r["episode"] for r in local_records
                         if _match_rec(r, s) and r["episode"] is not None})
        missing = sorted(set(expected) - set(actual))
        per_season.append({
            "season": s, "expected": expected, "actual": actual,
            "missing": missing, "reason": None,
        })

    # 单季：扁平到顶层；多季：放进 seasons[]
    if len(per_season) == 1 and (season is not None or len(seasons_to_check) == 1):
        s0 = per_season[0]
        entry["season"] = s0["season"]
        entry["expected"] = s0["expected"]
        entry["actual"] = s0["actual"]
        entry["missing"] = s0["missing"]
        if s0["reason"]:
            entry["reason"] = s0["reason"]
    else:
        entry["seasons"] = per_season
        entry["season"] = None
        # 顶层回填：聚合自 seasons 明细（#2——LLM 优先读顶层，数据在就绝不留
        # null；集号为各季并集，季维度明细以 seasons[] 为准）。
        available = [ps for ps in per_season if ps["reason"] is None]
        if available:
            entry["expected"] = sorted(
                {e for ps in available for e in (ps["expected"] or [])})
            entry["actual"] = sorted(
                {e for ps in available for e in (ps["actual"] or [])})
            # missing 必须自洽 = expected - actual（跨季并集口径）：直接并
            # 各季 missing 会得到"某季缺 E1、另一季有 E1 也算缺"的矛盾结果。
            entry["missing"] = sorted(
                set(entry["expected"]) - set(entry["actual"]))
        # reason 与数据互斥：全部季不可用 → null + reason；
        # 部分季不可用 → 数据照常回填 + partial_seasons 点名；全部可用 → reason 保持 None
        if not available:
            entry["reason"] = "expected_episode_set_unavailable"
        elif len(available) < len(per_season):
            entry["partial_seasons"] = [
                ps["season"] for ps in per_season if ps["reason"] is not None
            ]

    if season_canceled:
        checked = {ps["season"] for ps in per_season}
        unchecked = [s for s in seasons_to_check if s not in checked]
        if unchecked:
            entry["partial_seasons"] = unchecked
        if not per_season:
            entry["title_resolution"] = "canceled"
            entry["reason"] = "canceled"

    return entry


def missing_episodes(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    mcfg: MediaConfig,
    tcfg: TmdbConfig,
    *,
    tvcfg: TvmazeConfig | None = None,
    title: str = "",
    season: int | None = None,
    path: str = "",
    paths: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """media_missing_episodes 入口（只读）。"""
    paths = normalize_list_arg(paths, "paths")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise InvalidArguments(f"limit 必须是正整数，得到: {limit!r}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidArguments(f"offset 必须是非负整数，得到: {offset!r}")
    if season is not None:
        if not isinstance(season, int) or isinstance(season, bool) or season < 0:
            raise InvalidArguments(f"season 必须是非负整数或 null，得到: {season!r}")
    title = (title or "").strip()

    if title:
        # ---- 单剧模式 ----
        # 先扫描本地全部匹配文件（按规范化片名）。片名规范化需要解析结果，
        # 但解析前用原始 title 做一次粗扫（按解析后的规范化名再过滤）。
        records, sink, scan_truncated = _scan_local_episodes(
            guard, limits, video_exts, subtitle_exts, media_roots, mcfg,
            path=path, paths=paths, title_norm=None,
        )
        # 解析后用 canonical/original 规范化名过滤本地文件
        norm_titles: set[str] = {_norm_title(title)}
        if tcfg.enabled or (tvcfg is not None and tvcfg.enabled):
            rinfo = _resolve_tv(tcfg, tvcfg, title, None)
            if rinfo["resolved"]:
                if rinfo["canonical"]:
                    norm_titles.add(_norm_title(rinfo["canonical"]))
                if rinfo["original"]:
                    norm_titles.add(_norm_title(rinfo["original"]))
        matched = [r for r in records if r["title_norm"] in norm_titles]

        entry = _build_show_entry(
            tcfg, tvcfg, title=title, year=None, season=season,
            local_records=matched,
        )
        # 单剧模式不分页（只有一个条目），但保留 pagination 结构
        result = {
            "ok": True,
            "title": title,
            "season": season,
            "title_resolution": entry["title_resolution"],
            "metadata_source": entry.get("metadata_source"),
            "expected": entry.get("expected"),
            "actual": entry.get("actual"),
            "missing": entry.get("missing"),
            "reason": entry.get("reason"),
            "shows": [entry],
            "pagination": {
                "limit": limit, "offset": offset,
                "truncated": False, "next_offset": None,
            },
            "scan_truncated": scan_truncated,
            "errors": sink.errors,
            "errors_summary": sink.summary(),
        }
        # T3：本地一个文件都没扫到 + 权限错误 → 结果不可信
        result = mark_incomplete_from_sink(result, sink, empty=not matched)
        if check_canceled():
            report("canceled", "已取消，保留已完成部分",
                   done=len(matched), total=len(records))
            mark_partial(result)
        else:
            report("done", "缺集检测完成", done=len(matched), total=len(matched))
        return result

    # ---- 扫剧模式（title=null）----
    records, sink, scan_truncated = _scan_local_episodes(
        guard, limits, video_exts, subtitle_exts, media_roots, mcfg,
        path=path, paths=paths, title_norm=None,
    )
    # 按规范化片名分组
    groups: dict[str, list[dict]] = {}
    group_year: dict[str, int | None] = {}
    for r in records:
        groups.setdefault(r["title_norm"], []).append(r)
        group_year.setdefault(r["title_norm"], None)

    # 对每个组解析并构造条目
    all_entries: list[dict] = []
    group_items = list(groups.items())
    for group_index, (tn, recs) in enumerate(group_items):
        if check_canceled():
            break
        # 取一个代表性原始片名（首条记录）
        rep_title = recs[0]["title"]
        report(
            "show", "逐剧比对缺集",
            done=group_index, total=len(group_items), current=rep_title,
        )
        entry = _build_show_entry(
            tcfg, tvcfg, title=rep_title, year=group_year.get(tn),
            season=season, local_records=recs,
        )
        all_entries.append(entry)

    # 分页
    page = all_entries[offset:offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(all_entries) else None
    truncated = next_offset is not None

    result = {
        "ok": True,
        "title": None,
        "season": season,
        "title_resolution": "scan_all",
        "shows": page,
        "pagination": {
            "limit": limit, "offset": offset,
            "truncated": truncated, "next_offset": next_offset,
        },
        "scan_truncated": scan_truncated,
        "total_shows": len(all_entries),
        "errors": sink.errors,
        "errors_summary": sink.summary(),
    }
    if check_canceled():
        report("canceled", "已取消，保留已完成部分",
               done=len(all_entries), total=len(group_items))
        mark_partial(result)
    else:
        report("done", "缺集检测完成",
               done=len(all_entries), total=len(all_entries))
    return result
