"""多站点字幕搜索与下载编排（subhd / opensubtitles / assrt / subdl）。

协调三个字幕站点进行搜索和下载，支持自动 failover：
- 搜索：按 site_order 依次搜索各站点，候选合并去重（sid 带站点前缀，天然无冲突），
  达到 switch_min_score 即提前停止后续站点；
- 下载：按 sid 前缀路由到对应站点，失败则按 site_order 依次 failover
  （在新站点重新搜索取最佳候选的 sid 再下载）。

安全约束：
- 至少一个站点启用才允许操作；
- 下载是写操作，受 enable_write 与路径白名单双重门控；
- 各站点的 HTTP/解析细节由站点模块自行处理，编排层只调 search/fetch；
- 字幕落盘前统一转 UTF-8（复用 subhd 的 convert_to_utf8）。
"""

from __future__ import annotations

import errno
import logging
import os
import time
from dataclasses import replace
from pathlib import Path

from . import assrt as assrt_mod
from . import media as media_tools
from . import opensubtitles as opensubtitles_mod
from . import subdl as subdl_mod
from . import subhd as subhd_tools
from .fs_utils import write_bytes as safe_write_bytes
from .platform_hints import permission_hint_text
from .config import (
    NasConfig,
    OpensubtitlesConfig,
    SubhdConfig,
    TmdbConfig,
)
from .progress import check_canceled, mark_partial, report as report_progress
from .security import InvalidArguments, PathGuard
from .subhd import (
    SubtitleCandidate,
    SubhdClient,
    SubhdError,
    VideoQuery,
    _archive_entry_score,
    _candidate_episodes,
    _CJK_RE,
    _norm_release,
    _TEXT_SUBTITLE_FORMATS,
    collect_archive_entries,
    convert_to_utf8,
    fetch_and_process,
    infer_language_tag,
    language_compatible,
    parse_video_filename,
    report_language_tag,
    score_candidate,
    sniff_archive_format,
    sniff_subtitle_format,
    suggest_subtitle_name,
)

logger = logging.getLogger("nas_mcp.subtitle")


# ---------------------------------------------------------------- 异常


class SubtitleError(Exception):
    """多站点字幕编排失败。

    ``code`` 默认 all_sites_failed；当主站点失败原因可判定（例如 SubHD 的
    Cloudflare 挑战）时提升为对应错误码，便于 Agent 决策。
    """

    def __init__(
        self,
        message,
        *,
        site_errors=None,
        site_error_details=None,
        code="all_sites_failed",
        retryable=None,
    ):
        self.site_errors = site_errors or {}
        self.site_error_details = site_error_details or {}
        self.code = code
        self.retryable = retryable
        super().__init__(message)


# 需要从各站点错误里提升到编排层顶部的可判定错误码（顺序即优先级）。
_ESCALATE_ERROR_CODES = (
    "cloudflare_challenge",
    "browser_unavailable",
    "clearance_expired",
    "subhd_rate_limited",
    "missing_credentials",
    "dependency_missing",
    "auth_failed",
    "rate_limited",
)

# 用户可开启的全部站点（用于错误提示；顺序与默认 site_order 一致）
_ALL_SITES_HINT = "subhd/opensubtitles/assrt/subdl"


def _describe_site_error(exc: Exception) -> dict | None:
    """把站点异常整理成结构化详情（不含敏感信息）。

    仅返回自有错误码的异常；普通异常只保留 message，避免把第三方对象的
    repr 混进响应。
    """
    code = getattr(exc, "code", None)
    if not code:
        return None
    detail = {
        "code": str(code),
        "message": str(exc),
        "site": getattr(exc, "site", None),
        "endpoint": getattr(exc, "endpoint", None) or None,
        "retryable": getattr(exc, "retryable", None),
        "status_code": getattr(exc, "status_code", None),
        "detail": getattr(exc, "detail", None) or None,
    }
    return {key: value for key, value in detail.items() if value is not None}


# ---------------------------------------------------------------- 站点适配


def _site_enabled(cfg: NasConfig, site: str) -> bool:
    """检查指定站点是否启用。"""
    if site == "subhd":
        return cfg.subhd.enabled
    if site == "opensubtitles":
        return cfg.opensubtitles.enabled
    if site == "assrt":
        return cfg.assrt.enabled
    if site == "subdl":
        return cfg.subdl.enabled
    return False


def _subhd_search(
    guard: PathGuard,
    cfg_subhd: SubhdConfig,
    *,
    video_path: str = "",
    keyword: str = "",
    tmdb_cfg: TmdbConfig | None = None,
) -> list[SubtitleCandidate]:
    """适配 SubHD 搜索：复用 subhd_tools.search_subtitles 的多轮召回（含 TMDB 别名）。

    返回的 SubtitleCandidate.sid 统一带 subhd: 前缀，避免与
    opensubtitles:/assrt:/subdl: 冲突。
    score 置 0.0——由编排层统一评分，保证各站点评分基准一致。
    """
    result = subhd_tools.search_subtitles(
        guard,
        cfg_subhd,
        video_path=video_path,
        keyword=keyword,
        tmdb_cfg=tmdb_cfg,
    )
    candidates: list[SubtitleCandidate] = []
    for d in result.get("candidates", []):
        raw_sid = d["sid"]
        prefixed = raw_sid if raw_sid.startswith("subhd:") else f"subhd:{raw_sid}"
        candidates.append(
            SubtitleCandidate(
                sid=prefixed,
                title=d["title"],
                release_name=d["release_name"],
                languages=tuple(d["languages"]),
                format=d["format"],
                source=d["source"],
                score=0.0,
                detail_url=d.get("detail_url", ""),
                site="subhd",
            )
        )
    return candidates


def _subhd_fetch(cfg_subhd: SubhdConfig, sid: str) -> tuple[str, bytes]:
    """适配 SubHD 下载：调用 SubhdClient 下载链，返回 (文件名, 原始字节)。

    sid 可带 subhd: 前缀，自动剥离后传入 SubhdClient。
    文件名未知（归档内才知），返回空串，由 fetch_and_process 确定。
    """
    raw_sid = sid[len("subhd:"):] if sid.startswith("subhd:") else sid
    client = SubhdClient(cfg_subhd)
    download_path = client.prepare_download(raw_sid)
    direct_url = client.verify_download(raw_sid, download_path)
    archive_data, _ = client.download_file(
        direct_url, referer=f"{cfg_subhd.base_url}/a/{raw_sid}"
    )
    return "", archive_data


def _site_search(
    site: str,
    cfg: NasConfig,
    guard: PathGuard,
    *,
    video_path: str = "",
    keyword: str = "",
    query: VideoQuery,
    release_hint: str | None,
    tmdb_cfg: TmdbConfig | None,
) -> list[SubtitleCandidate]:
    """统一搜索入口：按站点名分派到对应搜索函数。"""
    if site == "subhd":
        return _subhd_search(
            guard, cfg.subhd,
            video_path=video_path, keyword=keyword, tmdb_cfg=tmdb_cfg,
        )
    if site == "opensubtitles":
        return opensubtitles_mod.search(cfg.opensubtitles, keyword, query)
    if site == "assrt":
        # 统一最小协议 search(query)；评分由编排层统一做，避免站内排序不一致
        return assrt_mod.search(cfg.assrt, query)
    if site == "subdl":
        return subdl_mod.search(cfg.subdl, query)
    raise ValueError(f"未知站点: {site}")


def _site_fetch(site: str, cfg: NasConfig, sid: str) -> tuple[str, bytes]:
    """统一下载入口：按站点名分派到对应 fetch 函数。"""
    if site == "subhd":
        return _subhd_fetch(cfg.subhd, sid)
    if site == "opensubtitles":
        return opensubtitles_mod.fetch(cfg.opensubtitles, sid)
    if site == "assrt":
        return assrt_mod.fetch(cfg.assrt, sid)
    if site == "subdl":
        return subdl_mod.fetch(cfg.subdl, sid)
    raise ValueError(f"未知站点: {site}")


# ---------------------------------------------------------------- 搜索编排


def search_subtitles(
    guard: PathGuard,
    cfg: NasConfig,
    *,
    video_path: str = "",
    keyword: str = "",
    tmdb_cfg: TmdbConfig | None = None,
) -> dict:
    """多站点字幕搜索（只读网络搜索）。

    必须提供 video_path（从文件名解析搜索词）或 keyword（自由搜索）。
    按 cfg.subtitles.site_order 依次搜索各站点，候选合并去重，
    达到 cfg.subtitles.switch_min_score 即提前停止后续站点。

    返回:
        dict: ok / query / total / candidates / tried_sites / site_errors
    """
    # 1-2. 解析视频文件名或关键词，构建搜索词
    release_hint: str | None = None
    if video_path:
        resolved = guard.check(video_path)
        if not resolved.is_file():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")
        query = parse_video_filename(resolved.name)
        search_keyword = query.title
        if query.year:
            search_keyword += f" {query.year}"
        release_hint = resolved.stem
    elif keyword:
        query = parse_video_filename(keyword)
        search_keyword = keyword
    else:
        raise InvalidArguments("必须提供 video_path 或 keyword")

    if not search_keyword.strip():
        raise InvalidArguments("搜索关键词为空")

    # 3. 校验：至少一个站点启用
    enabled_sites = [s for s in cfg.subtitles.site_order if _site_enabled(cfg, s)]
    if not enabled_sites:
        raise InvalidArguments(
            "所有字幕站点均未启用"
            f"（请在配置中开启至少一个：{_ALL_SITES_HINT}）"
        )

    # 4. 逐站点搜索 + failover
    by_sid: dict[str, SubtitleCandidate] = {}
    tried_sites: list[str] = []
    site_errors: dict[str, str] = {}
    site_error_details: dict[str, dict] = {}
    # #6：逐站状态明细，让调用方区分"该站确实无数据"与"该站失败了"
    site_attempts: list[dict] = []

    for site in cfg.subtitles.site_order:
        if not _site_enabled(cfg, site):
            continue
        tried_sites.append(site)
        try:
            candidates = _site_search(
                site, cfg, guard,
                video_path=video_path, keyword=search_keyword,
                query=query, release_hint=release_hint, tmdb_cfg=tmdb_cfg,
            )
            # 统一评分（覆盖站点内部评分，保证基准一致）；site 强制取本轮正在
            # 搜索的站点——与 site_attempts、sid 前缀三方一致，杜绝 dataclass
            # 默认值把外站候选标成 subhd（#7）
            scored = [
                replace(
                    c,
                    score=score_candidate(c, query, release_hint=release_hint),
                    site=site,
                )
                for c in candidates
            ]
            # 合并到全局 by_sid（sid 带站点前缀，天然无冲突）
            for c in scored:
                old = by_sid.get(c.sid)
                if old is None or c.score > old.score:
                    by_sid[c.sid] = c
            if not scored:
                # 0 结果是合法响应（该站确实无数据），不进 site_errors（#6）
                site_attempts.append({
                    "site": site, "status": "no_results", "count": 0,
                })
                logger.debug("站点 %s 返回 0 条候选", site)
                continue
            best = max(c.score for c in scored)
            site_attempts.append({
                "site": site, "status": "ok", "count": len(scored),
                "best_score": round(best, 3),
            })
            logger.info(
                "站点 %s 搜索完成：%d 条候选，最高分 %.3f", site, len(scored), best
            )
            # 达到切换分数线即提前停止
            if best >= cfg.subtitles.switch_min_score:
                site_attempts.append({
                    "site": "_stopped", "status": "stopped_early",
                    "count": 0, "reason": f"best_score>={cfg.subtitles.switch_min_score}",
                })
                logger.info(
                    "站点 %s 最高分 %.3f >= switch_min_score %.3f，提前停止",
                    site, best, cfg.subtitles.switch_min_score,
                )
                break
        except Exception as exc:
            site_errors[site] = str(exc)
            described = _describe_site_error(exc)
            if described:
                site_error_details[site] = described
            site_attempts.append({
                "site": site,
                "status": "error",
                "count": 0,
                "code": getattr(exc, "code", "error"),
                "retryable": getattr(exc, "retryable", None),
                "error": str(exc),
            })
            # 日志只记异常类型与错误码：不输出凭据、URL 查询串或响应原文
            logger.warning(
                "站点 %s 搜索失败（%s: %s）",
                site, getattr(exc, "code", "error"), type(exc).__name__,
            )
            continue

    # 5. 评分排序
    scored_list = sorted(by_sid.values(), key=lambda x: x.score, reverse=True)
    # 简体优先于中英双语，纯繁体沉底（评分相同时保持上述顺序）
    scored_list.sort(key=_lang_rank)

    # search_status：区分"确认无数据"与"全部失败"与"部分失败"（#6）
    # 有结果：无错误 → results_found；有错误 → partial_failure（错误站可能漏好候选）
    # 无结果：至少一站正常返回（哪怕 0 条）→ no_results_confirmed；全部报错 → all_sites_failed
    n_ok = sum(1 for a in site_attempts if a["status"] == "ok")
    n_nores = sum(1 for a in site_attempts if a["status"] == "no_results")
    n_err = sum(1 for a in site_attempts if a["status"] == "error")
    if scored_list:
        search_status = "partial_failure" if n_err else "results_found"
    elif n_ok or n_nores:
        search_status = "no_results_confirmed"
    else:
        search_status = "all_sites_failed"

    return {
        "ok": True,
        "query": {
            "title": query.title,
            "year": query.year,
            "season": query.season,
            "episode": query.episode,
            "resolution": query.resolution,
            "source": query.source,
            "release_group": query.release_group,
            "keyword": search_keyword,
        },
        "total": len(scored_list),
        "search_status": search_status,
        "site_attempts": site_attempts,
        "candidates": [
            {
                "sid": c.sid,
                "title": c.title,
                "release_name": c.release_name,
                "languages": list(c.languages),
                "format": c.format,
                "source": c.source,
                "score": c.score,
                "detail_url": c.detail_url,
                "site": c.site,
                # #8：全季包标记（有季无集号/含整季关键词）——单集补全场景需
                # 按集拆分后再落盘，交调用方决策是否选用
                "pack_hint": subhd_tools.is_season_pack(c.release_name, c.title),
            }
            for c in scored_list
        ],
        "tried_sites": tried_sites,
        "site_errors": site_errors,
        "site_error_details": site_error_details,
        # assrt 的传输模式（proxy/direct/insecure_no_sni_direct）显式暴露，
        # 让调用方一眼看出是否启用了降级（不安全）直连
        "site_transports": _site_transports(cfg),
    }


# ---------------------------------------------------------------- 下载编排


def _site_transports(cfg: NasConfig) -> dict[str, dict]:
    """返回需要标识传输模式的站点信息（目前仅 assrt 支持降级直连）。"""
    transports: dict[str, dict] = {}
    if cfg.assrt.enabled:
        transports["assrt"] = assrt_mod.transport_info(cfg.assrt)
    return transports


def _parse_sid_site(sid: str) -> str:
    """从 sid 前缀解析站点名（无前缀 → subhd）。

    assrt:/subdl: 为正式前缀；shooter: 作为 assrt 的历史前缀继续兼容。
    """
    lowered = sid.lower()
    if lowered.startswith("opensubtitles:"):
        return "opensubtitles"
    if lowered.startswith(("assrt:", "shooter:")):
        return "assrt"
    if lowered.startswith("subdl:"):
        return "subdl"
    # subhd:XXX 或无前缀 → subhd
    return "subhd"


def _search_best_sid_for_failover(
    site: str,
    cfg: NasConfig,
    guard: PathGuard,
    *,
    video_path: str,
    query: VideoQuery,
    release_hint: str | None,
    tmdb_cfg: TmdbConfig | None,
) -> str | None:
    """下载 failover 时在新站点搜索并返回最佳候选的 sid。

    搜索失败或 0 结果时返回 None，由调用方跳过该站点。
    """
    search_keyword = query.title
    if query.year:
        search_keyword += f" {query.year}"
    if not search_keyword.strip():
        return None
    try:
        candidates = _site_search(
            site, cfg, guard,
            video_path=video_path, keyword=search_keyword,
            query=query, release_hint=release_hint, tmdb_cfg=tmdb_cfg,
        )
    except Exception as exc:
        logger.warning(
            "failover 搜索站点 %s 失败（%s: %s）",
            site, getattr(exc, "code", "error"), type(exc).__name__,
        )
        return None
    if not candidates:
        return None
    scored = [
        replace(c, score=score_candidate(c, query, release_hint=release_hint))
        for c in candidates
    ]
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored[0].sid


def download_subtitle(
    guard: PathGuard,
    cfg: NasConfig,
    subtitle_exts: frozenset[str],
    enable_write: bool,
    *,
    video_path: str,
    sid: str,
    lang: str = "zh-Hans",
    ext: str = "srt",
    overwrite: bool = False,
) -> dict:
    """多站点字幕下载（写操作，受 enable_write 门控）。

    按 sid 前缀路由到对应站点，失败则按 site_order 依次 failover
    （在新站点重新搜索取最佳候选的 sid 再下载）。
    所有站点均失败时抛 SubtitleError（含各站点错误详情）。
    """
    # 1. 校验
    enabled_sites = [s for s in cfg.subtitles.site_order if _site_enabled(cfg, s)]
    if not enabled_sites:
        raise InvalidArguments(
            "所有字幕站点均未启用"
            f"（请在配置中开启至少一个：{_ALL_SITES_HINT}）"
        )
    if not enable_write:
        raise InvalidArguments("写操作未启用（enable_write=false）")

    # 2. 解析视频路径
    resolved_video = guard.check(video_path)
    if not resolved_video.is_file():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    # 3. 生成目标文件名（仅依赖模板，提前生成以便网络请求前就拒绝冲突）
    target_name = suggest_subtitle_name(
        cfg.subhd.filename_template, resolved_video, lang, ext
    )
    target_path = resolved_video.parent / target_name
    guard.check(str(target_path))

    # 4. 检查目标存在/覆盖/可写
    if target_path.exists() and not overwrite:
        raise FileExistsError(
            f"目标文件已存在: {target_name}（如需覆盖请设 overwrite=true）"
        )
    if not os.access(resolved_video.parent, os.W_OK):
        raise PermissionError(
            errno.EACCES,
            permission_hint_text(resolved_video.parent),
            str(resolved_video.parent),
        )

    # 5. 解析 sid 前缀确定主站点
    primary_site = _parse_sid_site(sid)

    # 构建下载顺序：主站点优先，其余按 site_order
    site_order = list(cfg.subtitles.site_order)
    if primary_site in site_order:
        site_order.remove(primary_site)
        site_order.insert(0, primary_site)

    # 解析视频元数据（failover 搜索需要）
    query = parse_video_filename(resolved_video.name)
    release_hint = resolved_video.stem
    tmdb_cfg = cfg.tmdb

    site_errors: dict[str, str] = {}
    site_error_details: dict[str, dict] = {}

    # 6. 逐站点下载 + failover
    for site in site_order:
        if not _site_enabled(cfg, site):
            continue
        try:
            # 确定该站点的 sid
            if site == primary_site:
                fetch_sid = sid
            else:
                # failover：在新站点重新搜索取最佳 sid
                fetch_sid = _search_best_sid_for_failover(
                    site, cfg, guard,
                    video_path=video_path,
                    query=query, release_hint=release_hint,
                    tmdb_cfg=tmdb_cfg,
                )
                if not fetch_sid:
                    site_errors[site] = "failover 搜索返回 0 条候选"
                    continue
                logger.info("failover 到站点 %s，使用 sid=%s", site, fetch_sid)

            # 调用站点 fetch 获取原始内容
            _, raw_content = _site_fetch(site, cfg, fetch_sid)

            # 处理内容（归档解压/裸字幕/非法内容识别）
            # #10：lang 透传给归档选择——多语言包按请求语言选，整季包按集号选
            archive_filename, content, actual_format = fetch_and_process(
                raw_content, subtitle_exts, resolved_video.stem, lang=lang
            )

            # 调用方默认扩展名兜底
            actual_format = actual_format or ext

            # 格式不一致时重新生成目标路径
            if actual_format != ext:
                target_name = suggest_subtitle_name(
                    cfg.subhd.filename_template,
                    resolved_video,
                    lang,
                    actual_format,
                )
                target_path = resolved_video.parent / target_name
                guard.check(str(target_path))
                if target_path.exists() and not overwrite:
                    raise FileExistsError(
                        f"目标文件已存在: {target_name}"
                        f"（如需覆盖请设 overwrite=true）"
                    )

            # 文本类字幕统一 UTF-8；位图等二进制格式原样落盘
            if actual_format in _TEXT_SUBTITLE_FORMATS:
                content = convert_to_utf8(content)

            # 落盘（fnOS 会把新文件裁成 0555，写完显式恢复 0644 便于后续覆盖）
            safe_write_bytes(target_path, content)

            logger.info(
                "字幕下载成功：站点=%s sid=%s 路径=%s 格式=%s 大小=%d",
                site, fetch_sid, target_path, actual_format, len(content),
            )

            # #10：落盘名按请求 lang 生成，标注包内实际文件名/语言，
            # 与请求不一致时显式告警（避免下游 nas_check_subtitle 假阳性）
            # OPEN-4：判定不了语言时回退请求 lang，不返回空串
            actual_language = report_language_tag(
                infer_language_tag(archive_filename), lang
            )
            result = {
                "ok": True,
                "video_path": str(resolved_video),
                "subtitle_path": str(target_path),
                "used_site": site,
                "source_sid": fetch_sid,
                "archive_filename": archive_filename,
                "actual_filename": archive_filename,
                "actual_language": actual_language,
                "size_bytes": len(content),
                "language": lang,
                "format": actual_format,
                "site_errors": site_errors,
                **({
                    "requested_format": ext,
                    "format_note": "已保留源字幕原生格式（特效与样式以源格式为准）",
                } if actual_format != ext else {}),
            }
            if not language_compatible(actual_language, lang):
                result["language_warning"] = (
                    f"包内实际文件 {archive_filename!r} 推断语言为 "
                    f"{actual_language}，与请求 {lang} 不一致"
                    f"（落盘文件名仍按请求语言生成，请核对内容）"
                )
            # 降级连接可见性：assrt 走 no-SNI 直连时，下载结果里明确标注
            if site == "assrt":
                result["site_transport"] = assrt_mod.transport_info(cfg.assrt)
            return result
        except FileExistsError:
            # NEW-B：本地目标已存在属于存在性冲突，直接抛出由上层返回 target_exists，
            # 不进入 failover 降级并被误判为 all_sites_failed
            raise
        except Exception as exc:
            site_errors[site] = str(exc)
            described = _describe_site_error(exc)
            if described:
                site_error_details[site] = described
            # 日志只记异常类型与错误码：不输出凭据、URL 查询串或响应原文
            logger.warning(
                "站点 %s 下载失败（%s: %s）",
                site, getattr(exc, "code", "error"), type(exc).__name__,
            )
            continue

    # 7. 所有站点均失败：主站点的可判定错误（如 CF 挑战）提升到顶层 code，
    # 便于 Agent 直接判断是装依赖、换出口还是等 cookie 续签，而不是只看
    # 笼统的 all_sites_failed。
    code = "all_sites_failed"
    retryable = None
    for site in site_order:
        detail = site_error_details.get(site)
        if detail and detail.get("code") in _ESCALATE_ERROR_CODES:
            code = detail["code"]
            retryable = detail.get("retryable")
            break
    raise SubtitleError(
        f"所有站点字幕下载均失败（已尝试: {', '.join(site_order)}）",
        site_errors=site_errors,
        site_error_details=site_error_details,
        code=code,
        retryable=retryable,
    )


# ---------------------------------------------------------------- 批量补全
# 字幕补全批处理策略（设计附录）：按 {剧, 季} 聚合 → 阈值分档 → 整季包优先、
# 单集降级 → 逐季报告。写盘受 enable_write 与白名单双重门控，dry_run 恒只读。


_MAX_SCAN_PAGES = 20  # 分页扫描页数上限（≈20×limits.max_search_results 个视频）


def _content_looks_chinese(content: bytes) -> bool:
    """中文内容抽样验证（③）：转 UTF-8 后前 20000 字符含 CJK 才算过关。"""
    text = convert_to_utf8(content).decode("utf-8", errors="ignore")
    return bool(_CJK_RE.search(text[:20000]))


def _pack_entry_reject_reason(
    name: str,
    content: bytes,
    lang: str,
    subtitle_exts: frozenset[str],
) -> str | None:
    """整季包条目落盘前验证（不过关不落盘）：通过返回 None，否则返回拒绝原因。"""
    tags = media_tools.extract_language_tags(Path(name).stem)
    if any(media_tools.is_traditional(t) for t in tags) and not any(
        media_tools.is_chinese(t) for t in tags
    ):
        return "traditional_not_accepted"
    low_ext = Path(name).suffix.lstrip(".").lower()
    fmt = sniff_subtitle_format(content) or (low_ext if low_ext in subtitle_exts else "")
    if fmt in _TEXT_SUBTITLE_FORMATS:
        if lang.startswith("zh") and not _content_looks_chinese(content):
            return "content_not_chinese"
        return None
    # 位图等二进制字幕无法抽样验证内容 → 按包内文件名语言判定；判定不了不拦
    actual = infer_language_tag(name)
    if actual and not language_compatible(actual, lang):
        return "language_mismatch"
    return None


def _scan_all_videos(
    guard: PathGuard,
    cfg: NasConfig,
    subtitle_exts: frozenset[str],
    path: str,
    probe: bool = False,
) -> tuple[list[dict], dict]:
    """分页扫描全部视频（默认外挂口径 probe=false），返回 (videos, scan_info)。"""
    videos: list[dict] = []
    offset = 0
    total = 0
    errors_summary: list = []
    truncated = False
    for _ in range(_MAX_SCAN_PAGES):
        page = media_tools.scan_media(
            guard,
            cfg.limits,
            cfg.video_extensions,
            subtitle_exts,
            cfg.media_roots,
            cfg.media,
            path=path,
            recursive=True,
            probe=probe,
            limit=0,
            offset=offset,
        )
        total = page["total_matched"]
        errors_summary = page.get("errors_summary", [])
        got = page.get("videos", [])
        videos.extend(got)
        offset += len(got)
        if not got or offset >= total:
            truncated = bool(page.get("scan_truncated"))
            break
    else:
        truncated = True  # 页数上限：结果可能不全
    return videos, {
        "total_videos": total,
        "collected": len(videos),
        "truncated": truncated,
        "probe": probe,
        "errors_summary": errors_summary,
    }


def _cand_languages(cand) -> list[str]:
    """候选语言标签：结构化 languages + 标题/发布名，统一规范化后再判定。

    各站点返回值不统一（SubHD 徽章经映射、其他站点可能给 raw 文本），
    这里统一过一遍 extract_language_tags，避免“繁体”这类中文写法漏判。
    候选既可能是 dict（返回体），也可能是 SubtitleCandidate 数据类。
    """
    if isinstance(cand, dict):
        raw = list(cand.get("languages") or [])
        title = cand.get("title") or ""
        release = cand.get("release_name") or ""
    else:
        raw = list(getattr(cand, "languages", None) or [])
        title = getattr(cand, "title", "") or ""
        release = getattr(cand, "release_name", "") or ""
    parts = [str(x) for x in raw]
    parts.append(str(title))
    parts.append(str(release))
    return media_tools.extract_language_tags(" ".join(parts))


def _lang_rank(cand) -> int:
    """候选语言优先级（数值越小越优先）。

    0 纯简中 < 1 简中/中英双语（不含繁体）< 2 简繁混合 < 3 无中文 < 4 纯繁体。
    “繁体一概不接受”：纯繁体排最后，调用方还会二次拦截；简繁混合因确实
    带简中而可接受，但排在纯简中之后（简中优先于中英、优先于夹带繁体）。
    """
    langs = _cand_languages(cand)
    trad = any(media_tools.is_traditional(l) for l in langs)
    simp = any(media_tools.is_chinese(l) for l in langs)
    if trad and not simp:
        return 4
    if not simp:
        return 3
    if trad:
        return 2
    if "zh-Hans" in langs and "en" not in langs:
        return 0
    return 1


def _only_traditional(cand) -> bool:
    """候选是否只带繁体标记（没有任何可接受的简中）。语言未知时返回 False。"""
    langs = _cand_languages(cand)
    return (
        any(media_tools.is_traditional(l) for l in langs)
        and not any(media_tools.is_chinese(l) for l in langs)
    )


def _existing_chinese_subtitle(
    video_path: Path,
    subtitle_exts: frozenset[str],
) -> dict | None:
    """幂等预检：同目录已有中文字幕（外挂）→ 返回该字幕，否则 None。"""
    for sub in media_tools.find_external_subtitles(video_path, subtitle_exts):
        if any(media_tools.is_chinese(l) for l in sub.get("languages", [])):
            return sub
    return None


def _aggregate_missing_groups(
    videos: list[dict],
    cfg: NasConfig | None = None,
    check_embedded: bool = True,
) -> list[dict]:
    """按 {剧, 季} 聚合（①）：total=季内全部视频，missing=缺中文字幕的。"""
    groups: dict[tuple[str, int | None], dict] = {}
    for v in videos:
        # Q1: 当 check_embedded 为 True 且未标已有中文字幕时，优先复用已探测结果，避免重复 ffprobe
        if not v.get("has_chinese_subtitle") and check_embedded and cfg is not None:
            if v.get("subtitle", {}).get("embedded_checked") or v.get("probed"):
                # 前序 scan_media 已执行过 probe，无需重复调用
                pass
            else:
                try:
                    pv = media_tools.probe_video(
                        Path(v["path"]),
                        cfg.media.ffprobe_path,
                        cfg.media.ffprobe_timeout_seconds,
                    )
                    if any(media_tools.stream_has_chinese(s) for s in pv.get("embedded_subtitles", [])):
                        v["has_chinese_subtitle"] = True
                        v["chinese_subtitle_sources"] = list(v.get("chinese_subtitle_sources") or []) + ["embedded"]
                    v.setdefault("subtitle", {})["embedded_checked"] = True
                except Exception as exc:
                    # 探测失败必须可见：否则会被当成“无字幕”去下载（重复补全的根因之一）
                    logger.warning("probe_video %s failed: %s", v.get("path"), exc)
                    v["probe_error"] = str(exc)

        q = parse_video_filename(v["name"])
        title_key = _norm_release(q.title) or _norm_release(v["name"])
        key = (title_key, q.season)
        g = groups.get(key)
        if g is None:
            g = {
                "title": q.title or v["name"],
                "season": q.season,
                "year": q.year,
                "all": [],
                "missing": [],
            }
            groups[key] = g
        g["all"].append(v)
        if not v.get("has_chinese_subtitle"):
            g["missing"].append((v, q))
    # 只报告有缺失的组；season None 排在数字季前，保证排序键可比较
    return [
        g
        for _, g in sorted(
            groups.items(),
            key=lambda kv: (kv[0][0], -1 if kv[0][1] is None else kv[0][1]),
        )
        if g["missing"]
    ]


def _decide_pack_strategy(group: dict, threshold: int) -> bool:
    """阈值分档（②）：missing ≥ max(阈值, 50%×季总集数) 走整季包；缺 1 集恒单集。"""
    missing_n = len(group["missing"])
    if group["season"] is None or missing_n < 2:
        return False
    return missing_n >= max(threshold, len(group["all"]) * 0.5)


def _season_pack_plan(
    guard: PathGuard,
    cfg: NasConfig,
    subtitle_exts: frozenset[str],
    group: dict,
) -> dict:
    """整季包路径（③）：季级搜索 → 识别包候选 → 下载 1 次 → 解包建集映射。

    失败返回 ok=False + reason（调用方按⑥整季降级单集）；
    成功返回 ep_map: {episode: [(entry_name, content), ...]}。
    """
    keyword = group["title"]
    if group["year"]:
        keyword = f"{keyword} {group['year']}"
    if group["season"] is not None:
        # 季标记进 query：评分给同季加分、异季冲突降权，避免下错季的包
        keyword = f"{keyword} S{group['season']:02d}"
    keyword = keyword.strip()
    if not keyword:
        return {"ok": False, "reason": "empty_search_keyword"}
    try:
        sr = search_subtitles(guard, cfg, keyword=keyword, tmdb_cfg=cfg.tmdb)
    except Exception as exc:
        return {"ok": False, "reason": f"pack_search_failed: {exc}"}

    pack_cands = [
        c for c in sr.get("candidates", [])
        if c.get("pack_hint") and not _only_traditional(c)
    ]
    if not pack_cands:
        return {
            "ok": False,
            "reason": f"no_pack_candidate(search_status={sr.get('search_status')})",
        }

    attempts: list[dict] = []
    entries: list[tuple[str, bytes]] | None = None
    used: dict | None = None
    for c in pack_cands:
        site = c["site"]
        try:
            _, raw = _site_fetch(site, cfg, c["sid"])
        except Exception as exc:
            attempts.append({"sid": c["sid"], "site": site, "error": str(exc)})
            continue
        if sniff_archive_format(raw) is None:
            attempts.append({"sid": c["sid"], "site": site, "error": "下载内容不是归档"})
            continue
        try:
            entries = collect_archive_entries(raw, subtitle_exts)
        except SubhdError as exc:
            attempts.append({"sid": c["sid"], "site": site, "error": str(exc)})
            continue
        used = c
        break
    if entries is None or used is None:
        return {"ok": False, "reason": "pack_download_failed", "attempts": attempts}

    # 按包内文件名解析 SxxExx 建集映射（③）：错季条目与无集号条目不入映射
    ep_map: dict[int, list[tuple[str, bytes]]] = {}
    for name, content in entries:
        # 包内条目同样过滤：只标繁体/正体的条目不入映射
        tags = media_tools.extract_language_tags(Path(name).stem)
        if any(media_tools.is_traditional(t) for t in tags) and not any(
            media_tools.is_chinese(t) for t in tags
        ):
            continue
        seasons, episodes = _candidate_episodes(Path(name).name)
        if not episodes:
            continue
        if seasons and group["season"] not in seasons:
            continue
        for ep in episodes:
            ep_map.setdefault(ep, []).append((name, content))
    return {
        "ok": True,
        "pack_sid": used["sid"],
        "pack_site": used.get("site") or _parse_sid_site(used["sid"]),
        "ep_map": ep_map,
        "attempts": attempts,
        "search_status": sr.get("search_status"),
    }


def _write_pack_entry(
    guard: PathGuard,
    cfg: NasConfig,
    subtitle_exts: frozenset[str],
    video_path: str,
    entry_name: str,
    content: bytes,
    lang: str,
    ext: str,
) -> dict:
    """整季包条目写盘（白名单/幂等/编码与 download_subtitle 同规格），返回落盘信息。"""
    resolved = guard.check(video_path)
    if not resolved.is_file():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")
    low_ext = Path(entry_name).suffix.lstrip(".").lower()
    fmt = (
        sniff_subtitle_format(content)
        or (low_ext if low_ext in subtitle_exts else "")
        or ext
    )
    target_name = suggest_subtitle_name(
        cfg.subhd.filename_template, resolved, lang, fmt
    )
    target = resolved.parent / target_name
    guard.check(str(target))
    if target.exists():
        raise FileExistsError(f"目标文件已存在: {target_name}")
    if not os.access(resolved.parent, os.W_OK):
        raise PermissionError(
            errno.EACCES,
            permission_hint_text(resolved.parent),
            str(resolved.parent),
        )
    data = convert_to_utf8(content) if fmt in _TEXT_SUBTITLE_FORMATS else content
    safe_write_bytes(target, data)
    return {
        "video_path": str(resolved),
        "subtitle_path": str(target),
        "actual_filename": entry_name,
        # OPEN-4：判定不了语言时回退请求 lang，不返回空串
        "actual_language": report_language_tag(infer_language_tag(entry_name), lang),
        "format": fmt,
        "size_bytes": len(data),
    }


def _fill_individual(
    guard: PathGuard,
    cfg: NasConfig,
    subtitle_exts: frozenset[str],
    items: list[tuple[dict, VideoQuery]],
    lang: str,
    ext: str,
    check_embedded: bool = False,
) -> tuple[list[dict], list[dict], list[dict], int, list[dict]]:
    """单集路径（⑤）：逐集搜索 → 取最高分候选 → download_subtitle（overwrite=false）。

    返回 (filled, skipped, failed, download_attempts, probe_errors)。
    """
    filled: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    probe_errors: list[dict] = []
    attempts = 0
    for v, q in items:
        episode = q.episode
        try:
            resolved = guard.check(v["path"])
        except Exception as exc:
            failed.append({"video_path": v["path"], "episode": episode,
                           "reason": f"path_denied: {exc}"})
            continue
        video_path = str(resolved)
        if not resolved.is_file():
            failed.append({"video_path": video_path, "episode": episode,
                           "reason": "video_not_found"})
            continue
        existing = _existing_chinese_subtitle(resolved, subtitle_exts)
        if existing is not None:
            skipped.append({"video_path": video_path, "episode": episode,
                            "reason": "existing_chinese_subtitle",
                            "existing": existing["name"]})
            continue
        if check_embedded:
            try:
                pv = media_tools.probe_video(resolved, cfg.media.ffprobe_path, cfg.media.ffprobe_timeout_seconds)
                if any(media_tools.stream_has_chinese(s) for s in pv.get("embedded_subtitles", [])):
                    skipped.append({"video_path": video_path, "episode": episode,
                                    "reason": "existing_embedded_chinese_subtitle"})
                    continue
            except Exception as exc:
                logger.warning("probe_video %s failed: %s", resolved, exc)
                probe_errors.append({"video_path": video_path, "episode": episode,
                                     "error": str(exc)})
        try:
            sr = search_subtitles(
                guard, cfg, video_path=video_path, tmdb_cfg=cfg.tmdb
            )
        except Exception as exc:
            failed.append({"video_path": video_path, "episode": episode,
                           "reason": f"search_failed: {exc}"})
            continue
        cands = sr.get("candidates") or []
        if not cands:
            failed.append({
                "video_path": video_path, "episode": episode,
                "reason": f"no_results(search_status={sr.get('search_status')})",
            })
            continue
        # 繁体一概不接受：纯繁体候选直接丢弃（宁可这集失败，也不下繁体）
        cands = [c for c in cands if not _only_traditional(c)]
        if not cands:
            failed.append({
                "video_path": video_path, "episode": episode,
                "reason": "only_traditional_candidates",
            })
            continue
        attempts += 1
        try:
            r = download_subtitle(
                guard, cfg, subtitle_exts, True,
                video_path=video_path, sid=cands[0]["sid"],
                lang=lang, ext=ext, overwrite=False,
            )
        except FileExistsError as exc:
            skipped.append({"video_path": video_path, "episode": episode,
                            "reason": "target_exists", "detail": str(exc)})
            continue
        except Exception as exc:
            item = {"video_path": video_path, "episode": episode,
                    "reason": f"download_failed: {exc}"}
            if isinstance(exc, SubtitleError):
                item["site_errors"] = exc.site_errors
                if getattr(exc, "site_error_details", None):
                    item["site_error_details"] = exc.site_error_details
                if getattr(exc, "code", ""):
                    item["error_code"] = exc.code
            failed.append(item)
            continue
        item = {
            "video_path": r["video_path"],
            "subtitle_path": r["subtitle_path"],
            "episode": episode,
            "strategy": "individual",
            "used_site": r.get("used_site"),
            "source_sid": r.get("source_sid"),
            "actual_filename": r.get("actual_filename"),
            "actual_language": r.get("actual_language"),
            "size_bytes": r.get("size_bytes"),
            "format": r.get("format"),
        }
        if r.get("language_warning"):
            item["language_warning"] = r["language_warning"]
        if r.get("site_transport"):
            item["site_transport"] = r["site_transport"]
        filled.append(item)
    return filled, skipped, failed, attempts, probe_errors


def fill_missing_subtitles(
    guard: PathGuard,
    cfg: NasConfig,
    subtitle_exts: frozenset[str],
    enable_write: bool,
    *,
    path: str = "",
    dry_run: bool = True,
    lang: str = "zh-Hans",
    ext: str = "srt",
    check_embedded: bool = True,
    budget_seconds: float = 0.0,
) -> dict:
    """字幕补全批处理（按 {剧,季} 聚合，整季包优先、单集降级；默认 dry_run）。

    策略（设计附录"字幕补全批处理策略"）：
    - ① 统计：扫描（外挂口径 probe=false）按 {剧, 季} 聚合 missing/total；
    - ② 阈值：missing ≥ max(subtitles.season_pack_threshold, 50%×季总集数)
      走整季包路径；缺 1 集恒单集（阈值主要价值）；
    - ③ 整季包：季级搜索选 pack_hint 候选 → 下载 1 次 → 解包 → 按包内
      SxxExx 建集映射 → 逐集按 _archive_entry_score 选条目，中文内容抽样
      验证不过关不落盘；
    - ④ 包不全/条目不合格 → 已覆盖收下，剩余集降级单集路径（fallback）；
    - ⑤ 单集路径：video_path 搜索取最高分候选 → download_subtitle（不覆盖）；
    - ⑥ 包搜索 0 结果/下载失败/解包失败 → 整季降级单集循环（reason 记录原因）；
    - ⑦ 逐季报告 {title, season, strategy, pack_sid, filled[], fallback[],
      failed[], skipped[]}。

    幂等：任何写盘前检查同目录已有中文字幕与目标文件存在，重复执行不重复下载。
    dry_run=true（默认）只出计划与分档，不发起任何网络请求、不写文件；
    dry_run=false 落盘需 enable_write=true（与 subhd_download 同一门控）。
    budget_seconds>0 时按 wall-clock 预算逐季检查，超时后剩余季标记
    reason=budget_exhausted 并返回已完成部分（不中断已发起的单次下载）。
    """
    # 0. 门控与站点校验（dry_run 恒可用，与 organize_media/media_cleanup 惯例一致）
    enabled_sites = [s for s in cfg.subtitles.site_order if _site_enabled(cfg, s)]
    if not enabled_sites:
        raise InvalidArguments(
            "所有字幕站点均未启用"
            f"（请在配置中开启至少一个：{_ALL_SITES_HINT}）"
        )
    if not dry_run and not enable_write:
        raise InvalidArguments(
            "写操作未启用（enable_write=false）——可先 dry_run=true 查看计划"
        )

    # 1-2. 扫描 + 按 {剧, 季} 聚合
    videos, scan_info = _scan_all_videos(guard, cfg, subtitle_exts, path, probe=check_embedded)
    groups = _aggregate_missing_groups(videos, cfg=cfg, check_embedded=check_embedded)
    missing_total = sum(len(g["missing"]) for g in groups)
    threshold = cfg.subtitles.season_pack_threshold

    season_reports: list[dict] = []
    n_fill = n_skip = n_fallback = n_fail = n_plan = 0
    n_pack_groups = n_ind_groups = download_attempts = 0
    n_probe_err = 0
    n_budget_skipped = 0
    budget_exhausted = False
    canceled = False
    processed = 0
    total_groups = len(groups)
    deadline = (
        time.monotonic() + float(budget_seconds) if budget_seconds and budget_seconds > 0 else None
    )
    report_progress("scan", "扫描视频并统计缺失字幕",
                    done=len(videos), total=len(videos))

    # 3. 逐季决策与执行
    for idx, g in enumerate(groups, 1):
        processed = idx - 1
        # 合作式取消：季边界停下，已下载并落盘的字幕保留
        if check_canceled():
            canceled = True
            break
        report_progress("fill", "逐季补字幕", done=processed, total=total_groups,
                        current=f"{g['title']} 第 {g['season']} 季")
        # P0-4：wall-clock 预算——超时后不再发起新的季/集搜索下载，剩余季显式标记
        if deadline is not None and time.monotonic() >= deadline:
            budget_exhausted = True
            n_budget_skipped += len(g["missing"])
            season_reports.append({
                "title": g["title"],
                "season": g["season"],
                "total": len(g["all"]),
                "missing_count": len(g["missing"]),
                "strategy": "skipped",
                "threshold": max(threshold, len(g["all"]) * 0.5),
                "missing": [
                    {"video_path": v["path"], "episode": q.episode}
                    for v, q in g["missing"]
                ],
                "pack_sid": None,
                "pack_site": None,
                "reason": "budget_exhausted",
                "planned": [],
                "filled": [],
                "fallback": [],
                "failed": [],
                "skipped": [],
                "probe_errors": [],
            })
            continue
        use_pack = _decide_pack_strategy(g, threshold)
        report = {
            "title": g["title"],
            "season": g["season"],
            "total": len(g["all"]),
            "missing_count": len(g["missing"]),
            "strategy": "pack" if use_pack else "individual",
            "threshold": max(threshold, len(g["all"]) * 0.5),
            "missing": [
                {"video_path": v["path"], "episode": q.episode}
                for v, q in g["missing"]
            ],
            "pack_sid": None,
            "pack_site": None,
            "reason": None,
            "planned": [],
            "filled": [],
            "fallback": [],
            "failed": [],
            "skipped": [],
            "probe_errors": [],
        }
        season_reports.append(report)

        if dry_run:
            # OPEN-3：dry_run 也按分档策略计数——此前计数器只在真执行分支
            # 自增，导致 summary 恒 0 与 seasons[].strategy 自相矛盾
            if use_pack:
                n_pack_groups += 1
            else:
                n_ind_groups += 1
            # 只出计划：不发起任何网络请求、不写文件（包覆盖率以实际下载为准）
            for v, q in g["missing"]:
                try:
                    resolved = guard.check(v["path"])
                except Exception as exc:
                    report["failed"].append(
                        {"video_path": v["path"], "episode": q.episode,
                         "reason": f"path_denied: {exc}"}
                    )
                    n_fail += 1
                    continue
                if v.get("probe_error"):
                    report["probe_errors"].append(
                        {"video_path": v["path"], "episode": q.episode,
                         "error": v["probe_error"]}
                    )
                    n_probe_err += 1
                existing = _existing_chinese_subtitle(resolved, subtitle_exts)
                if existing is not None:
                    report["skipped"].append(
                        {"video_path": str(resolved), "episode": q.episode,
                         "reason": "existing_chinese_subtitle",
                         "existing": existing["name"]}
                    )
                    n_skip += 1
                    continue
                report["planned"].append({
                    "video_path": str(resolved),
                    "episode": q.episode,
                    "target_name": suggest_subtitle_name(
                        cfg.subhd.filename_template, resolved, lang, ext
                    ),
                    "via": "season_pack" if use_pack else "individual",
                })
                n_plan += 1
            continue

        try:
            pack_ok = False
            if use_pack:
                pack = _season_pack_plan(guard, cfg, subtitle_exts, g)
                attempts = pack.get("attempts") or []
                # 每次站点 fetch 都是一次真实下载配额
                download_attempts += len(attempts) + (1 if pack.get("ok") else 0)
                if pack.get("pack_sid"):
                    report["pack_sid"] = pack["pack_sid"]
                    report["pack_site"] = pack.get("pack_site")
                if attempts:
                    report["pack_attempts"] = attempts
                if pack.get("ok"):
                    pack_ok = True
                    n_pack_groups += 1
                    ep_map = pack["ep_map"]
                    remaining: list[tuple[dict, VideoQuery, str]] = []
                    for v, q in g["missing"]:
                        try:
                            resolved = guard.check(v["path"])
                        except Exception as exc:
                            report["failed"].append(
                                {"video_path": v["path"], "episode": q.episode,
                                 "reason": f"path_denied: {exc}"}
                            )
                            n_fail += 1
                            continue
                        video_path = str(resolved)
                        existing = _existing_chinese_subtitle(resolved, subtitle_exts)
                        if existing is not None:
                            report["skipped"].append(
                                {"video_path": video_path, "episode": q.episode,
                                 "reason": "existing_chinese_subtitle",
                                 "existing": existing["name"]}
                            )
                            n_skip += 1
                            continue
                        if q.episode is None:
                            remaining.append((v, q, "no_episode_marker"))
                            continue
                        cands = ep_map.get(q.episode)
                        if not cands:
                            remaining.append((v, q, "pack_missing_episode"))
                            continue
                        entry_name, content = max(
                            cands,
                            key=lambda e: _archive_entry_score(
                                e[0], resolved.stem, lang
                            ),
                        )
                        reject = _pack_entry_reject_reason(
                            entry_name, content, lang, subtitle_exts
                        )
                        if reject:
                            remaining.append((v, q, reject))
                            continue
                        if check_embedded:
                            try:
                                pv = media_tools.probe_video(resolved, cfg.media.ffprobe_path, cfg.media.ffprobe_timeout_seconds)
                                if any(media_tools.stream_has_chinese(s) for s in pv.get("embedded_subtitles", [])):
                                    report["skipped"].append({
                                        "video_path": video_path, "episode": q.episode,
                                        "reason": "existing_embedded_chinese_subtitle",
                                    })
                                    n_skip += 1
                                    continue
                            except Exception as exc:
                                # 探测失败必须可见（否则失败会被当成“无内嵌中字”而落盘）
                                logger.warning("probe_video %s failed: %s", resolved, exc)
                                report["probe_errors"].append({
                                    "video_path": video_path, "episode": q.episode,
                                    "error": str(exc),
                                })
                                n_probe_err += 1
                        try:
                            w = _write_pack_entry(
                                guard, cfg, subtitle_exts, video_path,
                                entry_name, content, lang, ext,
                            )
                        except Exception as exc:
                            report["failed"].append(
                                {"video_path": video_path, "episode": q.episode,
                                 "reason": f"write_failed: {exc}"}
                            )
                            n_fail += 1
                            continue
                        report["filled"].append({
                            **w,
                            "episode": q.episode,
                            "strategy": "pack",
                            "pack_sid": pack["pack_sid"],
                            "used_site": pack.get("pack_site"),
                        })
                        n_fill += 1
                    # ④ 包不全/条目不合格 → 剩余集降级单集
                    if remaining:
                        for v, q, why in remaining:
                            report["fallback"].append(
                                {"video_path": v["path"],
                                 "episode": q.episode, "reason": why}
                            )
                            n_fallback += 1
                        r_fill, r_skip, r_fail, att, r_perr = _fill_individual(
                            guard, cfg, subtitle_exts,
                            [(v, q) for v, q, _ in remaining], lang, ext,
                            check_embedded=check_embedded,
                        )
                        report["filled"].extend(r_fill)
                        report["skipped"].extend(r_skip)
                        report["failed"].extend(r_fail)
                        report["probe_errors"].extend(r_perr)
                        n_probe_err += len(r_perr)
                        n_fill += len(r_fill)
                        n_skip += len(r_skip)
                        n_fail += len(r_fail)
                        download_attempts += att
                        # 回填降级项的最终结果，便于审计与断点续传
                        filled_paths = {x["video_path"] for x in report["filled"]}
                        skipped_paths = {x["video_path"] for x in report["skipped"]}
                        for fb in report["fallback"]:
                            if fb["video_path"] in filled_paths:
                                fb["outcome"] = "filled"
                            elif fb["video_path"] in skipped_paths:
                                fb["outcome"] = "skipped"
                            else:
                                fb["outcome"] = "failed"
                else:
                    # ⑥ 包路径失败 → 整季降级单集
                    report["strategy"] = "individual"
                    report["reason"] = pack.get("reason") or "pack_failed"

            if not pack_ok:
                n_ind_groups += 1
                i_fill, i_skip, i_fail, att, i_perr = _fill_individual(
                    guard, cfg, subtitle_exts, g["missing"], lang, ext,
                    check_embedded=check_embedded,
                )
                report["filled"].extend(i_fill)
                report["skipped"].extend(i_skip)
                report["failed"].extend(i_fail)
                report["probe_errors"].extend(i_perr)
                n_probe_err += len(i_perr)
                n_fill += len(i_fill)
                n_skip += len(i_skip)
                n_fail += len(i_fail)
                download_attempts += att
        except Exception as exc:
            # 单季异常不打断整批（结构化记录后继续下一季）
            report["reason"] = f"group_error: {exc}"
            logger.warning("季 %s S%s 批处理异常: %s", g["title"], g["season"], exc)

    else:
        processed = total_groups

    if canceled:
        report_progress("fill", "已取消，保留已完成部分", done=processed, total=total_groups)
    else:
        report_progress("fill", "逐季补字幕", done=processed, total=total_groups)

    result = {
        "ok": True,
        "dry_run": dry_run,
        "lang": lang,
        "ext": ext,
        "enable_write": enable_write,
        "season_pack_threshold": threshold,
        "summary": {
            "groups": len(season_reports),
            "pack_groups": n_pack_groups,
            "individual_groups": n_ind_groups,
            "missing_total": missing_total,
            "filled": n_fill,
            "skipped": n_skip,
            "fallback": n_fallback,
            "failed": n_fail,
            "planned": n_plan,
            "download_attempts": download_attempts,
            "probe_errors": n_probe_err,
            "budget_seconds": float(budget_seconds or 0),
            "budget_exhausted": budget_exhausted,
            "budget_skipped_missing": n_budget_skipped,
        },
        "seasons": season_reports,
        "scan": scan_info,
    }
    return mark_partial(result) if canceled else result
