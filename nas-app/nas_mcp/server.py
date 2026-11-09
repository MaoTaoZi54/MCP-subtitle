"""MCP Server 装配：把只读工具注册到 FastMCP，并统一做日志与错误映射。

设计：
- 每次工具调用记录参数摘要、耗时、成功/失败（规格 §26）；
- 预期错误（拒绝访问/路径不存在/参数错误）返回结构化 {"ok": false, "error": ...}，
  不让 Agent 拿到含糊的异常（规格 §27/§29）；
- 未预期异常记录堆栈后原样抛出，由 MCP 框架返回工具级错误，Server 不崩溃。
"""

from __future__ import annotations

import functools
import logging
import time
from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import __version__
from . import cleanup as cleanup_tools
from . import disk as disk_tools
from . import dysbk as dysbk_tools
from . import federated as federated_tools
from . import fs as fs_tools
from . import integrity as integrity_tools
from . import jackett as jackett_tools
from . import library as library_tools
from . import media as media_tools
from . import missing as missing_tools
from . import mux as mux_tools
from . import organizer as organizer_tools
from . import region as region_tools
from . import resolver as resolver_tools
from . import rollback as rollback_tools
from . import seedhub as seedhub_tools
from . import subhd as subhd_tools
from . import subtitle as subtitle_orchestrator
from .config import NasConfig
from .jackett import JackettError
from .resolver import ResolverError
from .security import AccessDenied, InvalidArguments, PathGuard, expand_roots
from .subhd import SubhdError
from .subtitle import SubtitleError

logger = logging.getLogger("nas_mcp.server")


def _summarize(result: object) -> str:
    if isinstance(result, dict):
        items = (
            result.get("entries")
            or result.get("matches")
            or result.get("videos")
            or result.get("filesystems")
            or result.get("candidates")
            or result.get("results")
            or result.get("plan")
            or []
        )
        return f"ok={result.get('ok')} items={len(items)}"
    return type(result).__name__


def _safe_tool(fn):
    """工具包装器：调用日志 + 预期错误转结构化结果 + 未预期异常记日志后抛出。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        name = fn.__name__
        started = time.monotonic()
        logger.info("工具调用 %s 参数=%s", name, kwargs)
        try:
            result = fn(*args, **kwargs)
        except AccessDenied as exc:
            logger.warning(
                "工具 %s 拒绝访问 路径=%s 耗时=%.3fs", name, exc.raw_path, time.monotonic() - started
            )
            return {
                "ok": False,
                "error": {"code": "access_denied", "message": exc.reason, "path": exc.raw_path},
            }
        except (FileNotFoundError, NotADirectoryError) as exc:
            logger.warning("工具 %s 路径错误 耗时=%.3fs: %s", name, time.monotonic() - started, exc)
            return {"ok": False, "error": {"code": "not_found", "message": str(exc)}}
        except FileExistsError as exc:
            logger.warning("工具 %s 目标已存在 耗时=%.3fs: %s", name, time.monotonic() - started, exc)
            return {"ok": False, "error": {"code": "target_exists", "message": str(exc)}}
        except PermissionError as exc:
            logger.warning("工具 %s 权限不足 耗时=%.3fs: %s", name, time.monotonic() - started, exc)
            return {"ok": False, "error": {"code": "permission_denied", "message": str(exc)}}
        except InvalidArguments as exc:
            logger.warning("工具 %s 参数错误 耗时=%.3fs: %s", name, time.monotonic() - started, exc)
            return {"ok": False, "error": {"code": "invalid_arguments", "message": str(exc)}}
        except SubhdError as exc:
            logger.warning(
                "工具 %s SubHD 错误 code=%s 状态=%s 耗时=%.3fs: %s",
                name, exc.code, exc.status_code, time.monotonic() - started, exc,
            )
            payload = {
                "code": exc.code,
                "message": str(exc),
                "status_code": exc.status_code,
            }
            if exc.site:
                payload["site"] = exc.site
            if exc.endpoint:
                payload["endpoint"] = exc.endpoint
            if exc.retryable is not None:
                payload["retryable"] = exc.retryable
            if exc.detail:
                payload["detail"] = exc.detail
            return {"ok": False, "error": payload}
        except SubtitleError as exc:
            logger.warning(
                "工具 %s 字幕编排错误 耗时=%.3fs: %s site_errors=%s",
                name, time.monotonic() - started, exc, exc.site_errors,
            )
            # 兼容既有契约：普通失败仍是 subtitle_error；仅把可判定的
            # Cloudflare 挑战类错误码暴露到顶层，便于调用方决策。
            code = getattr(exc, "code", None) or "subtitle_error"
            if code == "all_sites_failed":
                code = "subtitle_error"
            payload = {
                "code": code,
                "message": str(exc),
                "site_errors": exc.site_errors,
            }
            if getattr(exc, "site_error_details", None):
                payload["site_error_details"] = exc.site_error_details
            if getattr(exc, "retryable", None) is not None:
                payload["retryable"] = exc.retryable
            return {"ok": False, "error": payload}
        except JackettError as exc:
            logger.warning(
                "工具 %s Jackett 错误 code=%s 状态=%s 耗时=%.3fs: %s",
                name, exc.code, exc.status_code, time.monotonic() - started, exc,
            )
            return {"ok": False, "error": {
                "code": exc.code,
                "message": str(exc),
                "status_code": exc.status_code,
            }}
        except ResolverError as exc:
            logger.warning(
                "工具 %s Resolver 错误 状态=%s 耗时=%.3fs: %s",
                name, exc.status_code, time.monotonic() - started, exc,
            )
            return {"ok": False, "error": {
                "code": "resolver_error",
                "message": str(exc),
                "status_code": exc.status_code,
            }}
        except ValueError as exc:
            logger.warning("工具 %s 内部 ValueError 耗时=%.3fs: %s", name, time.monotonic() - started, exc)
            return {"ok": False, "error": {"code": "internal_error", "message": str(exc)}}
        except Exception:
            logger.exception("工具 %s 未预期异常 耗时=%.3fs", name, time.monotonic() - started)
            raise
        logger.info(
            "工具 %s 完成 耗时=%.3fs 结果=%s", name, time.monotonic() - started, _summarize(result)
        )
        return result

    return wrapper


def create_server(cfg: NasConfig) -> MCPServer:
    guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
    mcp: MCPServer = MCPServer(
        "nas-mcp",
        description="只读 NAS 工具集：目录浏览、文件搜索、磁盘空间、媒体分析与字幕检测"
        "（路径白名单 + 排除目录强制隔离）",
        # 服务器级使用说明：initialize 握手时随 serverInfo 发给 Agent，
        # 相当于自带系统提示——写工具链约定而非重复各工具 docstring 细节。
        instructions=(
            "nas-mcp：飞牛 NAS 媒体库安全工具层（默认只读）。使用约定：\n"
            "1. 首次接入先调 nas_get_config 了解白名单范围、各开关状态与限额；"
            "所有 path 参数必须在 allowed_paths 内且不在 exclude_paths 中，越界即被拒绝。\n"
            "2. 中文片名找资源：先 resolve_media 解析出英文标准名与年份"
            "（resolved=false 时把候选列表交给用户确认，不要猜测），"
            "再用英文名调 jackett_search；jackett_search 只返回磁力链，绝不下载。\n"
            "3. 补字幕工作流：nas_scan_media(missing_subtitle_only=true) 找出缺中文字幕的视频"
            " → nas_classify_region 判断国产/非国产（confidence 低或 unknown 时交用户决策）"
            " → subtitle_search(video_path=...) 取候选 → subtitle_download 保存到视频同目录。\n"
            "4. subtitle_download 是写操作，需至少一个站点 enabled 与 enable_write 都为 true；"
            "目标文件已存在时默认拒绝覆盖。\n"
            "5. 分页工具用 limit/offset 翻页，配合返回中的 total/truncated，不要一次拉全量。\n"
            "6. 库治理：media_library_audit 出体检报告（低清/无字幕/重复/异常），"
            "duplicate_media_detect 找同一作品多版本（组内 best_path 为建议保留版本），"
            "media_quality_analyze 看单文件质量标签；整库分析受 ffprobe 探测预算约束，"
            "看到 probe_budget_exhausted/scan_truncated 就分目录多次调用。\n"
            "7. 所有错误都是结构化 {\"ok\": false, \"error\": {\"code\", \"message\"}}，"
            "按 code 决策；API Key 永不出现在结果或日志中。\n"
            "8. 整理媒体目录：organize_media 先用默认 dry_run=true 只出计划，"
            "逐项审阅 move/identical/conflict/blocked 后再 dry_run=false 执行"
            "（执行需 enable_organize=true）；identical 去重与空目录清理属删除类，"
            "需 enable_delete=true，未开启时只报告 level3_pending；执行全部写审计日志。"
            "找片定位用 find_media(title, season, episode)。\n"
            "9. 库治理补全：media_missing_episodes 只按 TMDB 官方集数判断缺集"
            "（expected=null 表示基线不可用，不得用本地最大集数推断）；"
            "media_integrity_check 用 basic/deep 查文件完整性；"
            "media_cleanup 默认 dry_run 只出分类计划，真删需 enable_delete=true；"
            "media_rollback 只能按 operation_id 回滚，不接受 source/target；"
            "media_fill_subtitles 按{剧,季}批量补字幕，默认 dry_run 只出计划。"
        ),
    )

    @mcp.tool()
    @_safe_tool
    def nas_get_config() -> dict:
        """获取 NAS MCP 自身的配置：白名单、排除目录、搜索范围、扩展名与限额。

        在调用其他工具之前，可先调用本工具了解当前可访问的目录范围和限制。
        allowed_paths / exclude_paths / media_roots 中的条目可能为 glob 模式
        （含 * 或 [ ]，如 /vol[1-9]*/1000），新增存储卷自动纳入、网盘挂载自动排除；
        media_roots_expanded 是当前实际展开生效的目录列表。
        """
        return {
            "ok": True,
            "server": "nas-mcp",
            "version": __version__,
            "mode": "read-only",
            "allowed_paths": list(cfg.allowed_paths),
            "exclude_paths": list(cfg.exclude_paths),
            "media_roots": list(cfg.media_roots),
            "media_roots_expanded": list(expand_roots(cfg.media_roots)),
            "video_extensions": sorted(cfg.video_extensions),
            "subtitle_extensions": sorted(cfg.subtitle_extensions),
            "limits": asdict(cfg.limits),
            "media": asdict(cfg.media),
            "organizer": asdict(cfg.organizer),
            # OPEN-6：cleanup 段回显（此前只能直接读 yaml 才能看到清理配置）
            "cleanup": asdict(cfg.cleanup),
            "subhd": asdict(cfg.subhd),
            "opensubtitles": {
                "enabled": cfg.opensubtitles.enabled,
                "api_key_configured": bool(cfg.opensubtitles.api_key),
                "username_configured": bool(cfg.opensubtitles.username),
                "language": cfg.opensubtitles.language,
            },
            "assrt": {
                "enabled": cfg.assrt.enabled,
                "token_configured": bool(cfg.assrt.token),
                "proxy_configured": bool(cfg.assrt.proxy),
                # 显式暴露传输模式：insecure_no_sni_direct 为不安全降级连接
                "transport": (
                    "insecure_no_sni_direct" if cfg.assrt.insecure_no_sni_direct
                    else ("proxy" if cfg.assrt.proxy else "direct")
                ),
            },
            "subdl": {
                "enabled": cfg.subdl.enabled,
                "api_key_configured": bool(cfg.subdl.api_key),
            },
            # 旧客户端兼容：assrt 的历史字段名是 shooter
            "shooter": {
                "enabled": cfg.assrt.enabled,
                "token_configured": bool(cfg.assrt.token),
            },
            "subtitles": asdict(cfg.subtitles),
            # §42：API Key 绝不返回/入日志，只暴露是否已配置
            "jackett": {
                "enabled": cfg.jackett.enabled,
                "url": cfg.jackett.url,
                "api_key_configured": bool(cfg.jackett.api_key),
                "max_queries": cfg.jackett.max_queries,
            },
            "tmdb": {
                "enabled": cfg.tmdb.enabled,
                "api_key_configured": bool(cfg.tmdb.api_key),
                "language": cfg.tmdb.language,
            },
            # S4 新增源配置（默认值即可跑，无密钥字段）
            "seedhub": asdict(cfg.seedhub),
            "dysbk": asdict(cfg.dysbk),
            "media_search": asdict(cfg.media_search),
            "enable_write": cfg.enable_write,
            "enable_organize": cfg.enable_organize,
            "enable_delete": cfg.enable_delete,
        }

    @mcp.tool()
    @_safe_tool
    def nas_list_directory(
        path: str, recursive: bool = False, limit: int = 0, offset: int = 0
    ) -> dict:
        """列出 NAS 上某个目录的内容（只读）。

        返回每个条目的名称、类型（dir/file/symlink）、扩展名、大小、修改时间、
        相对路径和媒体类别（video/subtitle/None）。排除目录会被拒绝，
        指向排除目录或白名单外的符号链接会被隐藏。

        Args:
            path: 目录绝对路径，必须在 allowed_paths 白名单内
            recursive: 是否递归列出全部子目录（默认只列当前层）
            limit: 单页最多返回多少条，0 表示使用服务器默认上限
            offset: 分页偏移量，配合 total_scanned/truncated 实现翻页
        """
        return fs_tools.list_directory(
            guard,
            cfg.limits,
            cfg.video_extensions,
            cfg.subtitle_extensions,
            path=path,
            recursive=recursive,
            limit=limit,
            offset=offset,
        )

    @mcp.tool()
    @_safe_tool
    def nas_find_files(
        keyword: str = "",
        category: str = "",
        extensions: list[str] | None = None,
        root: str = "",
        recursive: bool = True,
        limit: int = 0,
        offset: int = 0,
    ) -> dict:
        """在 NAS 媒体目录中按扩展名/类别/关键词搜索文件（只读）。

        category="video" 使用服务器配置的视频扩展名集合（mkv/mp4/...），
        category="subtitle" 使用字幕扩展名集合（srt/ass/...）；也可用 extensions
        显式指定。keyword 对文件名做大小写不敏感的包含匹配。至少需要一个过滤条件。
        默认搜索全部 media_roots；结果中的路径只会来自白名单内、且排除目录
        永远不会被搜索。命中 media.skip_filename_patterns 的文件（家庭录像/
        成人内容等，如 IMG_/VID_/DJI_ 命名）会被静默隐藏，只在
        skipped_by_filter 中计数。

        Args:
            keyword: 文件名包含的关键词（大小写不敏感），如 "breaking bad"
            category: "video" 或 "subtitle"
            extensions: 显式扩展名列表（如 ["mkv", "mp4"]），优先于 category
            root: 限定搜索范围的目录；留空则搜索全部 media_roots
            recursive: 是否递归子目录（默认是）
            limit: 单页最多返回条数，0 表示默认上限
            offset: 分页偏移量
        """
        return fs_tools.find_files(
            guard,
            cfg.limits,
            cfg.video_extensions,
            cfg.subtitle_extensions,
            cfg.media_roots,
            keyword=keyword,
            category=category,
            extensions=extensions,
            root=root,
            recursive=recursive,
            limit=limit,
            offset=offset,
            skip_patterns=cfg.media.skip_filename_patterns,
        )

    @mcp.tool()
    @_safe_tool
    def nas_disk_usage(path: str = "") -> dict:
        """查看 NAS 存储空间（只读）。

        不带参数时返回各存储卷（按 media_roots 去重后的文件系统）的设备、挂载点、
        总容量、已用、可用与使用率；带 path 时只查看该路径所在的文件系统。
        用于判断 NAS 是否还有足够空间继续下载电影/字幕。

        Args:
            path: 可选；只查询该路径所在的文件系统，必须在白名单内
        """
        return disk_tools.disk_usage(
            guard, cfg.media_roots, path=path or None
        )

    @mcp.tool()
    @_safe_tool
    def nas_scan_media(
        path: str = "",
        recursive: bool = True,
        probe: bool = True,
        missing_subtitle_only: bool = False,
        media_type: str = "",
        limit: int = 0,
        offset: int = 0,
    ) -> dict:
        """扫描 NAS 媒体目录中的视频，分析媒体信息与字幕状况（只读）。

        对每个视频文件：用 ffprobe 获取编码/分辨率/帧率/音轨/内嵌字幕轨（规格：
        内嵌字幕必须用 ffprobe 流检测，不能只看有没有 .srt），并在同目录按
        “视频主名前缀”匹配外挂字幕、从文件名识别语言标签。输出
        has_chinese_subtitle / chinese_subtitle_sources，供“找出没有中文字幕的
        非国产视频”这类任务使用。媒体类型（movie/tv/anime/documentary/variety/
        unknown）为文件名启发式粗分类，仅供决策参考。
        命中 media.skip_filename_patterns 的文件（家庭录像/成人内容等）会被
        静默隐藏，只在 skipped_by_filter 中计数。

        Args:
            path: 扫描起点目录；留空则扫描全部 media_roots
            recursive: 是否递归子目录（默认是）
            probe: 是否运行 ffprobe；false 时跳过媒体信息，has_chinese_subtitle 只反映外挂字幕
            missing_subtitle_only: 只返回没有中文字幕的视频（probe=false 时仅按外挂字幕判断）
            media_type: 可选过滤 movie/tv/anime/documentary/variety/unknown
            limit: 单页最多返回条数，0 表示默认上限
            offset: 分页偏移量
        """
        return media_tools.scan_media(
            guard,
            cfg.limits,
            cfg.video_extensions,
            cfg.subtitle_extensions,
            cfg.media_roots,
            cfg.media,
            path=path,
            recursive=recursive,
            probe=probe,
            missing_subtitle_only=missing_subtitle_only,
            media_type=media_type,
            limit=limit,
            offset=offset,
        )

    @mcp.tool()
    @_safe_tool
    def nas_check_subtitle(path: str) -> dict:
        """查看单个视频文件的字幕详情（只读）：外挂字幕 + ffprobe 检测的内嵌字幕。

        返回外挂字幕文件列表（含语言标签）、内嵌字幕流（语言/编码/标题）、
        音轨语言、时长、分辨率等，以及 has_chinese_subtitle 结论。
        ffprobe 失败时结果仍可用（内嵌字幕标记为未检测），失败原因在 probe_error。

        Args:
            path: 视频文件绝对路径，必须在白名单内
        """
        return media_tools.check_subtitle(
            guard, cfg.video_extensions, cfg.subtitle_extensions, cfg.media, path=path
        )

    @mcp.tool()
    @_safe_tool
    def nas_classify_region(path: str = "", paths: list[str] | None = None) -> dict:
        """判断视频是国产还是非国产（只读，带 confidence 置信度，需求 §18）。

        信号优先级：音轨语言 > TMDB 元数据（original_language/origin_country，
        需 tmdb.enabled）> 文件名配音提示（国语/中配等，弱证据）。
        字幕语言不作为产地证据——任何片都可能挂中字。
        region 取值 domestic（国产）/ non_domestic（非国产）/ unknown（无法判断）；
        confidence 低或 unknown 时请交给用户决策，不要强行猜测。
        典型用途：配合 nas_scan_media 的 has_chinese_subtitle 字段，
        找出"没有中文字幕的非国产视频"再决定去哪些站点补字幕。

        Args:
            path: 单个视频文件绝对路径，必须在白名单内
            paths: 批量模式，视频路径列表（最多 media.max_classify_batch 个，默认 20）；
                   与 path 二选一
        """
        return region_tools.classify(
            guard, cfg.media, cfg.tmdb, cfg.video_extensions, path=path, paths=paths
        )

    @mcp.tool()
    @_safe_tool
    def media_quality_analyze(path: str = "", paths: list[str] | None = None) -> dict:
        """分析视频文件的画质/音质质量标签（只读，需求 2.0 §31）。

        以 ffprobe 真实信息为准（分辨率/HDR 形态 SDR/HDR10/HDR10+/Dolby Vision/
        视频编码/位深/音轨编码/声道/Atmos），文件名补充来源类型（REMUX/BluRay/
        WEB-DL/WEBRip/HDTV/CAM），输出 0-100 quality_score 与 quality_tier
        （excellent/good/fair/poor/bad），并给 low_quality 结论（≤720p 或 poor/bad）。
        用于挑出库里的低质量版本；整库统计请用 media_library_audit。

        Args:
            path: 单个视频文件绝对路径，必须在白名单内
            paths: 批量模式，视频路径列表（最多 media.max_classify_batch 个）；与 path 二选一
        """
        return library_tools.quality_analyze(
            guard, cfg.media, cfg.video_extensions, path=path, paths=paths
        )

    @mcp.tool()
    @_safe_tool
    def duplicate_media_detect(
        path: str = "",
        paths: list[str] | None = None,
        recursive: bool = True,
    ) -> dict:
        """检测同一作品的不同版本/重复文件（只读，需求 2.0 §30）。

        不只看文件名：tmdb.enabled 时优先按 TMDB ID 归并（剧集细分到季集），
        未命中/未启用时才退化为"规范化片名+年份+季集"匹配，绝不跨语言凭文件名
        合并（如"星际穿越"与"Interstellar"无 TMDB 时不会划等号）。同组用时长
        做合理性校验（差过大标 duration_mismatch）。每组给出成员列表与最佳版本
        （质量分最高，平分取文件更大者）。受 ffprobe 探测预算约束。

        Args:
            path: 待检测目录（或单个视频文件）；留空且无 paths 时报错
            paths: 显式视频文件列表；与 path 二选一
            recursive: 目录模式是否递归（默认是）
        """
        return library_tools.duplicate_detect(
            guard, cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
            cfg.media_roots, cfg.media, cfg.tmdb,
            path=path, paths=paths, recursive=recursive,
        )

    @mcp.tool()
    @_safe_tool
    def media_library_audit(
        path: str = "",
        paths: list[str] | None = None,
        recursive: bool = True,
    ) -> dict:
        """媒体库一键体检，返回结构化统计报告（只读，需求 2.0 §29/§33）。

        八项统计：电影数 / 电视剧数 / 无法识别数（需 tmdb.enabled，否则为 null）/
        无字幕数 / 无中文字幕数 / 低分辨率数（≤720p）/ 重复作品组数与文件数 /
        异常文件数（ffprobe 失败）。另附低分辨率文件样例、重复组（含建议保留的
        最佳版本路径）与探测预算/截断状态。受 ffprobe 探测预算约束，超大库会标记
        probe_budget_exhausted，可分目录多次调用。

        Args:
            path: 体检目录；留空且无 paths 时体检全部 media_roots
            paths: 显式视频文件列表；与 path 二选一
            recursive: 是否递归子目录（默认是）
        """
        return library_tools.audit(
            guard, cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
            cfg.media_roots, cfg.media, cfg.tmdb,
            path=path, paths=paths, recursive=recursive,
        )

    @mcp.tool()
    @_safe_tool
    def media_missing_episodes(
        title: str = "",
        season: int | None = None,
        path: str = "",
        paths: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """检测电视剧缺失的集数（只读，以 TMDB 官方集数为基线）。

        expected 集数列表只来自 TMDB tv season endpoint，绝不用本地最大集数推断。
        TMDB 不可用 / 查询失败 / 标题歧义 → expected=null, missing=null,
        reason="expected_episode_set_unavailable"；标题歧义时
        title_resolution="ambiguous"，不猜测。Season 0（Specials）默认不参与，
        显式 season=0 才检查。权限错误只进 errors，绝不当 missing。

        Args:
            title: 电视剧名；留空扫描全部可识别电视剧
            season: 季号（>=0）；留空检查本地出现的所有季；0 检查 Specials
            path: 待检查目录或单个文件；留空且无 paths 时扫全部 media_roots
            paths: 显式视频文件列表；与 path 二选一
            limit: 单页最多返回剧数（扫剧模式）/ 项数
            offset: 分页偏移
        """
        return missing_tools.missing_episodes(
            guard, cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
            cfg.media_roots, cfg.media, cfg.tmdb, tvcfg=cfg.tvmaze,
            title=title, season=season, path=path, paths=paths,
            limit=limit, offset=offset,
        )

    @mcp.tool()
    @_safe_tool
    def media_integrity_check(
        path: str = "",
        paths: list[str] | None = None,
        mode: str = "basic",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """检查媒体文件完整性（只读，basic/deep 两种模式）。

        basic：文件存在/可读/容器/视频流；deep：更深 probe（无音轨/时长异常等
        → warning），遵守 media.max_probe_files 预算。状态仅 healthy/warning/
        corrupted/incomplete/unknown；权限错误只进 errors，不当 corrupted。
        probe 预算耗尽 → truncated=true、对应项 reason="probe_budget_exhausted"。

        Args:
            path: 待检查目录或单个文件；留空且无 paths 时扫全部 media_roots
            paths: 显式视频文件列表；与 path 二选一
            mode: basic（默认）或 deep
            limit: 单页最多检查文件数
            offset: 分页偏移
        """
        return integrity_tools.integrity_check(
            guard, cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
            cfg.media_roots, cfg.media, path=path, paths=paths, mode=mode,
            limit=limit, offset=offset,
        )

    @mcp.tool()
    @_safe_tool
    def media_cleanup(
        path: str = "",
        files: list[str] | None = None,
        dry_run: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """扫描/指定媒体文件，给出垃圾清理分类计划；默认 dry_run，真删需五重门。

        四类分类：media / media_junk / personal_video / unknown。复用
        skip_filename_patterns 与 organizer 的 special_dir_names；空目录不属本
        工具职责。每个计划项带 path/size/mtime_age/classification/matched_pattern/
        protection_reason/cleanup_allowed/action 证据字段。
        永久删除五重门（缺一不可）：classification=media_junk、cleanup_allowed、
        enable_delete=true、dry_run=false、PathGuard 通过；删除不可回滚
        （审计记录 rollback_unsupported）。files 显式清单只改目标选取，不改删除
        资格；任一条目越界整体 path_not_allowed，不部分执行。

        Args:
            path: 扫描目录（留空扫全部 media_roots）；须在白名单与 media_roots 内
            files: 显式文件清单（与 path 二选一）
            dry_run: true（默认）只返回计划不改文件；false 才执行删除
            limit: 单页最多返回计划项数
            offset: 分页偏移
        """
        return cleanup_tools.cleanup(
            guard, cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
            cfg.media_roots, cfg.media, cfg.cleanup, cfg.organizer,
            enable_delete=cfg.enable_delete, path=path, files=files,
            dry_run=dry_run, limit=limit, offset=offset,
        )

    @mcp.tool()
    @_safe_tool
    def media_rollback(operation_id: str = "", dry_run: bool = True) -> dict:
        """按 operation_id 回滚一次写操作（move/rename/create），默认只检查。

        只接受 operation_id，不得传入 source/target 要求回滚；ID 来自
        organize_media / subtitle_download / media_cleanup 的执行返回值。
        状态：rollback_ready / rolled_back / rollback_conflict /
        rollback_unsupported / not_found / permission_denied / failed /
        partially_rolled_back。批量先检查全部项目，任一冲突 → 执行前整体中止，
        绝不部分回滚。权限：move/rename 回滚需 enable_organize=true；
        create 回滚（删除新建字幕）需 enable_delete=true。
        dry_run=true（默认）只检查返回 rollback_ready，不改文件。

        Args:
            operation_id: 操作 ID（必填，如 OP-20261009-K3M7P）
            dry_run: true（默认）只做安全检查；false 才执行实际回滚
        """
        return rollback_tools.rollback(
            guard, cfg.organizer, enable_organize=cfg.enable_organize,
            enable_delete=cfg.enable_delete, operation_id=operation_id,
            dry_run=dry_run,
        )

    @mcp.tool()
    @_safe_tool
    def subtitle_search(video_path: str = "", keyword: str = "") -> dict:
        """在多站点搜索字幕候选（只读网络搜索，不落盘）。

        支持 SubHD、OpenSubtitles、assrt、SubDL 四个站点，按 subtitle.site_order
        顺序串行搜索，站点报错/0 结果/最高分低于阈值时自动切换下一站。
        某站最高分达到 switch_min_score（默认 0.9）即提前停止。
        各站候选汇总后统一评分排序，sid 带站点前缀（如 subhd:OyVNRJ）。

        站点启停：subhd.enabled / opensubtitles.enabled / assrt.enabled / subdl.enabled
        各自独立控制；site_order 中 enabled=false 的站点自动跳过。
        提供 video_path 时从文件名解析搜索词；也可用 keyword 自由搜索。

        Args:
            video_path: 视频文件绝对路径（必须在白名单内），用于解析搜索词
            keyword: 自由搜索关键词（video_path 为空时使用）
        """
        return subtitle_orchestrator.search_subtitles(
            guard, cfg, video_path=video_path, keyword=keyword, tmdb_cfg=cfg.tmdb
        )

    @mcp.tool()
    @_safe_tool
    def subtitle_download(
        video_path: str,
        sid: str,
        lang: str = "zh-Hans",
        ext: str = "srt",
        overwrite: bool = False,
    ) -> dict:
        """从多站点下载字幕并保存到视频同目录（写操作）。

        sid 带站点前缀路由（subhd:XXX / opensubtitles:XXX / assrt:XXX / subdl:...），
        无前缀默认走 subhd。该站下载失败时自动按 site_order 换站重搜重下，
        全部启用站点都失败才报错（错误信息汇总每站失败原因）。

        双重门控：至少一个站点 enabled 且 enable_write=true。
        保存位置固定为视频所在目录，文件名按 subhd.filename_template 生成。
        RAR 包用 unar 解压，字幕落盘前统一转 UTF-8。

        Args:
            video_path: 视频文件绝对路径（必须在白名单内）
            sid: 字幕条目 ID（由 subtitle_search 返回，带站点前缀）
            lang: 写入文件名的语言标签（默认 zh-Hans）
            ext: 写入文件名的字幕扩展名（默认 srt）
            overwrite: 目标文件已存在时是否覆盖（默认否）
        """
        result = subtitle_orchestrator.download_subtitle(
            guard, cfg, cfg.subtitle_extensions, cfg.enable_write,
            video_path=video_path, sid=sid, lang=lang, ext=ext, overwrite=overwrite,
        )
        if result.get("ok"):
            # 与插件/app 同口径：字幕落盘写 CREATE 审计并返回 operation_id，
            # 供 media_rollback 按 create 回滚。
            try:
                sp = Path(str(result.get("subtitle_path") or ""))
                st = sp.stat()
                oid = organizer_tools.new_operation_id(cfg.organizer)
                organizer_tools._audit(
                    cfg.organizer, action="CREATE",
                    source=str(result.get("video_path") or ""), target=str(sp),
                    result="SUCCESS", operation_id=oid,
                    size=st.st_size, mtime=st.st_mtime_ns,
                )
                result["operation_id"] = oid
            except OSError as exc:
                result["operation_id"] = None
                logger.warning("字幕创建审计写入失败: %s", exc)
        return result

    @mcp.tool()
    @_safe_tool
    def media_fill_subtitles(
        path: str = "",
        dry_run: bool = True,
        lang: str = "zh-Hans",
        ext: str = "srt",
        check_embedded: bool = True,
        budget_seconds: float = 0.0,
    ) -> dict:
        """批量补全缺失中文字幕（按{剧,季}聚合，整季包优先、单集降级；默认 dry_run）。

        扫描（外挂口径 probe=false）按 {剧, 季} 聚合出 missing/total 后分档：
        missing ≥ max(subtitles.season_pack_threshold, 50%×季总集数) 走整季包
        路径，否则走单集路径。包不全/失败 → 剩余集降级单集；幂等：写盘前检查
        同目录已有中文字幕与目标文件。逐季返回 {title, season, strategy,
        pack_sid, filled[], fallback[], failed[], skipped[]}；每个落盘文件写入
        CREATE 审计并返回 operation_id（供 media_rollback 按 create 回滚）。
        dry_run=true（默认）只出计划与分档，不发起网络请求、不写文件；
        dry_run=false 落盘需 enable_write=true。

        Args:
            path: 扫描目录（留空扫全部 media_roots）；须在白名单与 media_roots 内
            dry_run: true（默认）只返回计划不下载；false 才实际补全
            lang: 字幕文件名语言标签（默认 zh-Hans）
            ext: 字幕扩展名（默认 srt）
            check_embedded: 是否检查内嵌中文字幕（扫描仍按外挂口径）
            budget_seconds: wall-clock 预算秒数（0=不限）；超时后剩余季标记
                reason=budget_exhausted 并返回已完成部分
        """
        result = subtitle_orchestrator.fill_missing_subtitles(
            guard, cfg, cfg.subtitle_extensions, cfg.enable_write,
            path=path, dry_run=dry_run, lang=lang, ext=ext,
            check_embedded=check_embedded, budget_seconds=budget_seconds,
        )
        if result.get("ok") and not dry_run:
            # 与插件/app 同口径：每个落盘字幕写 CREATE 审计并返回 operation_id。
            for season in result.get("seasons", []):
                for item in season.get("filled", []):
                    if not item.get("subtitle_path"):
                        continue
                    try:
                        sp = Path(str(item["subtitle_path"]))
                        st = sp.stat()
                        oid = organizer_tools.new_operation_id(cfg.organizer)
                        organizer_tools._audit(
                            cfg.organizer, action="CREATE",
                            source=str(item.get("video_path") or ""), target=str(sp),
                            result="SUCCESS", operation_id=oid,
                            size=st.st_size, mtime=st.st_mtime_ns,
                        )
                        item["operation_id"] = oid
                    except OSError as exc:
                        item["operation_id"] = None
                        logger.warning("字幕创建审计写入失败: %s", exc)
        return result

    @mcp.tool()
    @_safe_tool
    def media_mux_subtitles(
        path: str = "",
        video_path: str = "",
        dry_run: bool = True,
        delete_external: bool = False,
        replace_original: bool = False,
        force: bool = False,
        limit: int = 50,
    ) -> dict:
        """扫描并批量/单文件将外挂字幕混流封装入视频（MKV，支持双轨/单轨自动降级）。"""
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
        return mux_tools.media_mux_subtitles(
            guard,
            cfg,
            path=path,
            video_path=video_path,
            dry_run=dry_run,
            delete_external=delete_external,
            replace_original=replace_original,
            force=force,
            limit=limit,
        )

    @mcp.tool()
    @_safe_tool
    def resolve_media(query: str, year: int = 0, media_type: str = "") -> dict:
        """把中文/原始影视名解析成标准实体（TMDB，只读网络查询）。

        受 tmdb.enabled 开关控制。返回标准实体（canonical_title/original_title/
        year/media_type/tmdb_id）与候选列表；无法确认时 resolved=false 并返回
        候选，交给 Octop 决策，绝不强行猜测。解析结果可直接用于 jackett_search
        与 subtitle_search 的搜索词构造。

        Args:
            query: 影视名称，如 "星际穿越" 或 "Interstellar"
            year: 上映年份（可选，用于消歧）
            media_type: "movie" 或 "tv"（留空两者都查）
        """
        return resolver_tools.resolve(
            cfg.tmdb, query, year=year or None, media_type=media_type
        )

    @mcp.tool()
    @_safe_tool
    def jackett_search(
        query: str, year: int = 0, media_type: str = "", limit: int = 0
    ) -> dict:
        """通过 Jackett 搜索媒体资源（只读网络搜索，绝不下载）。

        受 jackett.enabled 开关控制。内部流程（规格 §26）：query 先经
        MediaResolver（tmdb.enabled 时）解析出英文标准名与年份，再生成多个
        搜索关键词（数量受 jackett.max_queries 限制）逐个查询 Jackett，
        结果标准化后按 infohash（无则 normalized_title+size）去重，按做种数
        降序返回。每条结果含 title/size/seeders/leechers/indexer/publish_date/
        magnet/torrent_url/details_url；magnet 允许为 null（部分索引器只给
        torrent 链接，不影响整体搜索）。本工具只返回磁力链，绝不执行下载。

        seeders_missing_count：结果中索引器未提供做种数的条数。>0 表示该索引器
        未返回 Seeders 字段（站点定义抓取限制，常见于 The Pirate Bay 等），
        并非本服务故障；seeders=null 的结果已排在有做种数的结果之后，可直接
        按其余质量维度（release 字段）比较，不要臆测 Jackett 版本或服务异常。

        Args:
            query: 影视名称或关键词，如 "星际穿越" 或 "Interstellar"
            year: 上映年份（可选，用于消歧与关键词生成）
            media_type: "movie" 或 "tv"（留空自动判断）
            limit: 最多返回条数（≤1000），0 表示使用 jackett.max_results
        """
        return jackett_tools.search(
            cfg.jackett, cfg.tmdb, query=query, year=year,
            media_type=media_type, limit=limit,
        )

    @mcp.tool()
    @_safe_tool
    def organize_media(
        path: str, mode: str = "all", dry_run: bool = True,
        preexisting_empty_scope: str = "library_root",
        language_split: bool = False,
    ) -> dict:
        """把杂乱的下载目录整理成标准媒体库结构（L1 计划 / L2 执行 / L3 删除）。

        能识别并整理的形态（识别不了的绝不乱动）：
        - 电影包装目录展开：Movie/Movie.1080p/Movie.mkv(+srt+nfo) → Movie/Movie.mkv
        - 单集文件夹合并：Series/S01E01/S01E01.mkv → Series/Season 01/S01E01.mkv
        - 散集归季：剧集目录内 S01E01.mkv、S02E01.mkv 混存 → 分建 Season 01/02
        - 分类目录自动下探：电视剧/电影/动漫/国产剧/TV Shows/Movies 这类分类层
          （名字命中分类词，或无分类词但含 ≥2 个剧目结构子目录；最多 2 层）即使
          根下直接散落着视频也会穿透，顶层一次出全部计划（返回 library_roots）；
          单个"多视频+子目录"的混合内容目录仍保守跳过
        - 错误剧集根纠正：单集/整季命名目录（如 "Lioness (2023) - S03E08 - …/
          Season 03"、"Supernatural (2005) Season 10 S10 (…)/Season 10"）出
          rename_dir 计划项，目录名剥离残留季信息后改名为规范剧集根（L2，仅改名
          不搬文件，对应 Sonarr 的 Rename Series Folder）；规范根已存在时逐集并入
        - 隐藏目录/文件（. 开头，如 TMM 残留 .deletedByTMM）整树不发现、不规划
        - 字幕强制跟随视频移动；nfo/jpg 封面在名称无歧义时跟随

        三级分级：dry_run=true（默认）只生成计划、恒可用、不改任何文件；
        dry_run=false 执行移动/改名，需配置 enable_organize=true（L2）；
        identical/duplicate 删源与空目录清理属删除类（L3），需 enable_delete=true，
        未开启时只报告 level3_pending 绝不删除。副本裁决按"规划后布局"进行：
        rename 后将落在规范 <剧名>/Season NN/ 位置的副本为保留方（target 指向
        执行阶段1删除时刻真实存在的路径，keeper_target 注明最终位置），同组只
        保留一份、绝不双删；本批不触及的存量重复不在此处理。
        同名目标冲突三态：大小/时长/编码一致 → identical（可去重），
        不一致 → conflict（保留双方），无法判断（ffprobe 失败）→ blocked 不动。
        BDMV/VIDEO_TS 等蓝光原盘目录、含子目录且关系不明的目录一律跳过；
        同批两个计划指向同一目标（多版本）双双 blocked。
        write_preflight 除计划真实写入点（源父/目标现存祖先）外，还常驻探活
        root 与每个 library_root：即使本轮全 skip，目录只读时也报
        ok=false/status=not_writable，绝不在已知不可写时误报 ok。
        权限提示以属主/属组（chown/chmod u+w）优先，避免盲目 o+rwx。
        每次执行逐动作写 JSONL 审计日志（organizer.audit_log_file）。

        使用约定：先 dry_run=true 审阅计划再执行，确认无误后保持参数不变仅改
        dry_run=false；大目录分次整理，返回 plan_truncated 时缩小 path 范围。

        Args:
            path: 待整理目录绝对路径（必须在白名单内）；Season 目录建在对应库根内
            mode: "all"（默认）/ "movie" 只整理电影 / "tv" 只整理剧集
            dry_run: true（默认）只返回计划；false 实际执行（需 enable_organize）
            preexisting_empty_scope: 预存空目录收集范围："library_root"（默认，
                扫描全部库根）或 "target_subtree"（仅本轮操作目标子树内，
                对应 Sonarr 只清自己管辖目录的保守语义）
            language_split: 开启后电影/剧集按国语/外语分入子目录（ffprobe 音轨判断）
        """
        plan = organizer_tools.plan_session(
            guard, cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
            cfg.media, cfg.organizer, path=path, mode=mode,
            preexisting_empty_scope=preexisting_empty_scope,
            language_split=language_split,
            tmdb_cfg=cfg.tmdb,
        )
        if dry_run:
            return plan
        return organizer_tools.execute_plan(
            guard, plan, cfg.organizer,
            enable_organize=cfg.enable_organize,
            enable_delete=cfg.enable_delete,
        )

    @mcp.tool()
    @_safe_tool
    def find_media(title: str, season: int = 0, episode: int = 0) -> dict:
        """按片名在媒体库中定位视频文件（只读）。

        title 做归一化后的包含匹配（文件名与解析出的片名都参与，大小写/
        分隔符不敏感）；season/episode > 0 时按季集精确过滤（无法识别季集的
        文件不会被排除，避免漏报）。每条命中附文件名解析结果与外挂字幕列表。
        整理前可用本工具确认某部剧/电影的文件分布。

        Args:
            title: 片名关键词，如 "星际穿越" 或 "breaking bad"
            season: 季数过滤（0 表示不过滤），如 2
            episode: 集数过滤（0 表示不过滤），如 5
        """
        return organizer_tools.find_media(
            guard, cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
            cfg.media_roots, title=title, season=season, episode=episode,
        )

    @mcp.tool()
    @_safe_tool
    def seedhub_search(keyword: str) -> dict:
        """在 SeedHub 搜索影片（只读网络搜索，磁力+网盘双料站）。

        **只支持中文片名**（英文片名恒 0 结果，属站点限制不是 bug）；
        **依赖 curl_cffi** 过 Cloudflare（未安装时报 dependency_missing，
        其余工具不受影响）。受 seedhub.enabled 开关控制。返回 [{id, title, url}]；
        0 结果返回 no_results 错误码并提示换中文片名；触发 Cloudflare 挑战
        返回 cf_blocked（稍后重试）。命中后用 seedhub_magnets / seedhub_pans
        取磁力与网盘详情；一次性合并三路资源请用 media_search。

        Args:
            keyword: 中文片名，如 "流人" 或 "星际穿越"
        """
        return seedhub_tools.search(
            cfg.seedhub, keyword,
            timeout=cfg.media_search.seedhub_timeout_seconds,
        )

    @mcp.tool()
    @_safe_tool
    def seedhub_magnets(movie_id: int, limit: int = 0, offset: int = 0) -> dict:
        """取 SeedHub 某影片的磁力列表（只读网络搜索，绝不下载）。

        **只支持中文片名**站点、**依赖 curl_cffi**。movie_id 来自
        seedhub_search；返回 [{title, magnet, size, updated}]——磁力为裸
        magnet（**无 tracker、无做种数**，属站点原貌，不要臆造 seeders 字段）。
        limit=0 最多返回 50 条，超出 truncated=true；页面条数多于 limit 时
        用 offset 翻页（返回体给 total/offset）。逐条 /link_start 解析
        限速 ≥1s，单条解码失败标 resolve_failed=true（magnet 为 null），
        不影响其余条目。movie_id 非法/不存在返回 not_found。

        Args:
            movie_id: 影片 ID（seedhub_search 返回的 id）
            limit: 返回条数上限；0=默认 50 条
            offset: 起始下标（翻页用，配合返回体 total）
        """
        return seedhub_tools.magnets(
            cfg.seedhub, movie_id, limit, offset,
            timeout=cfg.media_search.seedhub_timeout_seconds,
        )

    @mcp.tool()
    @_safe_tool
    def seedhub_pans(movie_id: int, limit: int = 0, offset: int = 0) -> dict:
        """取 SeedHub 某影片的网盘直链列表（只读网络搜索，含提取码）。

        **只支持中文片名**站点、**依赖 curl_cffi**。movie_id 来自
        seedhub_search；返回 [{title, host, url, updated}]，host 为
        夸克/百度/迅雷/UC/阿里等网盘名，url 带提取码参数。
        limit=0 最多返回 50 条，超出 truncated=true；页面条数多于 limit 时
        用 offset 翻页（返回体给 total/offset）；单条解码失败标
        resolve_failed=true。movie_id 非法/不存在返回 not_found。

        Args:
            movie_id: 影片 ID（seedhub_search 返回的 id）
            limit: 返回条数上限；0=默认 50 条
            offset: 起始下标（翻页用，配合返回体 total）
        """
        return seedhub_tools.pans(
            cfg.seedhub, movie_id, limit, offset,
            timeout=cfg.media_search.seedhub_timeout_seconds,
        )

    @mcp.tool()
    @_safe_tool
    def dysbk_search(query: str, limit: int = 20) -> dict:
        """在网盘资源分享社（bbs.dysbk.com，Flarum 论坛）全文搜索资源帖（只读）。

        中文英文均可搜；返回去标签标题与讨论 id：[{id, title}]，网盘链接用
        dysbk_links 按 id 取。单请求 10s 预算，超时错误码 dysbk_timeout。
        受 dysbk.enabled 开关控制。Flarum 全文搜索有噪声（求资源/闲聊帖），
        跨源去重与选型请用 media_search。

        Args:
            query: 关键词，如 "流人" 或 "Slow Horses"
            limit: 返回条数（1-50，默认 20）
        """
        return dysbk_tools.search(
            cfg.dysbk, query, limit,
            timeout=cfg.media_search.dysbk_timeout_seconds,
        )

    @mcp.tool()
    @_safe_tool
    def dysbk_links(discussion_id: int) -> dict:
        """取网盘资源分享社某讨论首帖中的网盘直链（只读）。

        只返回 /s/ 分享链接（夸克/百度/迅雷/UC，保留 ?pwd= 提取码参数），
        正文其余内容（剧情简介等）一律不返回。discussion_id 来自
        dysbk_search；返回 count + links 扁平列表（每项为完整 URL 字符串）。
        讨论不存在返回 not_found，超时错误码 dysbk_timeout。

        Args:
            discussion_id: 讨论 ID（dysbk_search 返回的 id）
        """
        return dysbk_tools.links(
            cfg.dysbk, discussion_id,
            timeout=cfg.media_search.dysbk_timeout_seconds,
        )

    @mcp.tool()
    @_safe_tool
    def media_search(query: str) -> dict:
        """三路并行合并搜索资源（SeedHub + 网盘资源分享社 + Jackett，只读）。

        **三路并行，单路失败不影响其他路；中文英文均可搜；60s 内返回**。
        这是找资源的首选入口：各路独立预算（dysbk 10s / SeedHub 20s /
        Jackett 沿用 jackett 段配置），单路失败/超时只记 site_errors，
        其余照常返回、绝不整批重试；英文查询自动跳过 SeedHub（它只吃中文），
        且不计入 site_errors。三路原始结果只在插件内部合并：按 infohash/
        网盘链接去重 → 同一作品按「有网盘直链 > 有做种数 > 画质标签」排序、
        每部只留 top3 版本 → 总闸 20 条。每条统一 schema：
        {title, magnet, pan_url, size, seeders, quality_tag, source, updated,
        verified, confidence, mismatch, warning, real_files}
        （缺失置 null，不臆造）。confidence：high=标题完整命中且季集/年份一致、
        partial=部分命中（缺季/集/年份等）、suspicious=标题命中但做种/体积明显异常、
        mismatch=标题/年份/季集冲突、unverified=无法判定；verified 仅网盘经
        pan_probe 实时探测为真，磁力未探测内容恒为 false；warning 写明判定依据
        （磁力条目会标注「未校验实际内容」）。返回附 sources/merged/truncated/site_errors
        统计；sources 中 seedhub=0 且 seedhub_skipped_by_language=true 表示
        被语言路由跳过（英文查询的正常表现，非故障）。

        Args:
            query: 片名关键词，中文英文均可，如 "流人" 或 "Slow Horses"
        """
        return federated_tools.search(cfg, query)

    return mcp
