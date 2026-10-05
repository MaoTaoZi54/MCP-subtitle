# -*- coding: utf-8 -*-
"""飞牛 NAS 媒体工具箱 —— Octop 插件入口。

与独立版 nas-mcp（stdio MCP Server）共享同一套能力包（本目录 nas_mcp/），
差异仅在装配层：
- MCP 版：config.yaml 路径由 --config / NAS_MCP_CONFIG 指定，stdio 通信；
- 插件版：config.yaml 固定在插件目录（~/.octop/plugins/nas-media/config.yaml），
  工具由 Octop 进程内加载，结构化结果统一序列化为 JSON 字符串返回。

配置方式二选一：
1. 对话式：让 Agent 调用 nas_save_config（白名单路径、各开关、TMDB/Jackett 密钥）；
2. 直接编辑插件目录下的 config.yaml（字段同 nas-mcp 的 config.example.yaml）。
"""

from __future__ import annotations

import functools
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

# 插件由 importlib 以 harness_plugin_nas_media 模块加载，插件目录未必在 sys.path，
# 显式注入后即可 import 同级 nas_mcp 能力包。
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import yaml  # noqa: E402

from nas_mcp import __version__  # noqa: E402
from nas_mcp import disk as disk_tools  # noqa: E402
from nas_mcp import fs as fs_tools  # noqa: E402
from nas_mcp import jackett as jackett_tools  # noqa: E402
from nas_mcp import library as library_tools  # noqa: E402
from nas_mcp import media as media_tools  # noqa: E402
from nas_mcp import region as region_tools  # noqa: E402
from nas_mcp import resolver as resolver_tools  # noqa: E402
from nas_mcp import subhd as subhd_tools  # noqa: E402
from nas_mcp.config import ConfigError, load_config  # noqa: E402
from nas_mcp.jackett import JackettError  # noqa: E402
from nas_mcp.resolver import ResolverError  # noqa: E402
from nas_mcp.security import (  # noqa: E402
    AccessDenied,
    InvalidArguments,
    PathGuard,
    expand_roots,
)
from nas_mcp.subhd import SubhdError  # noqa: E402

# Octop 在 Agent 调用链中注入工具表单配置；非 Agent 上下文（本地测试）会取不到，
# 延迟导入并容错使用。
try:  # pragma: no cover - 取决于宿主环境
    from octop_harness.plugins import get_tool_config
except Exception:  # noqa: BLE001
    get_tool_config = None  # type: ignore[assignment]

logger = logging.getLogger("nas_mcp.plugin")

CONFIG_PATH = _PLUGIN_DIR / "config.yaml"

# 配置缓存：按 config.yaml 的 mtime 失效，改配置无需重启 Octop。
_cfg_cache: dict[str, Any] = {"mtime": None, "cfg": None}


def _get_cfg():
    """每次工具调用取最新配置（文件不存在时给出可操作的引导错误）。"""
    if not CONFIG_PATH.is_file():
        raise ConfigError(
            "插件尚未配置：请调用 nas_save_config 保存白名单与密钥，"
            "或在插件目录创建 config.yaml（模板见 config.example.yaml）"
        )
    mtime = CONFIG_PATH.stat().st_mtime
    if _cfg_cache["cfg"] is None or mtime != _cfg_cache["mtime"]:
        cfg = load_config(str(CONFIG_PATH))
        _cfg_cache["cfg"] = cfg
        _cfg_cache["mtime"] = mtime
    return _cfg_cache["cfg"]


def _json(result: dict) -> str:
    """LangChain StructuredTool 统一返回 JSON 字符串（保证中文可读、字段稳定）。"""
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def _safe(fn):
    """预期错误转结构化 JSON（与 MCP 版 _safe_tool 同一套错误码），不把异常抛给 Agent。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> str:
        name = fn.__name__
        started = time.monotonic()

        def err(code: str, message: str, **extra: Any) -> str:
            logger.warning("工具 %s %s 耗时=%.3fs: %s", name, code, time.monotonic() - started, message)
            payload: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
            payload["error"].update(extra)
            return _json(payload)

        try:
            logger.info("工具调用 %s 参数=%s", name, kwargs)
            result = fn(*args, **kwargs)
            logger.info("工具 %s 完成 耗时=%.3fs", name, time.monotonic() - started)
            return _json(result)
        except ConfigError as exc:
            return err("not_configured", str(exc))
        except AccessDenied as exc:
            return err("access_denied", exc.reason, path=exc.raw_path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            return err("not_found", str(exc))
        except FileExistsError as exc:
            return err("already_exists", str(exc))
        except PermissionError as exc:
            return err("permission_denied", str(exc))
        except InvalidArguments as exc:
            return err("invalid_arguments", str(exc))
        except SubhdError as exc:
            return err("subhd_error", str(exc), status_code=exc.status_code)
        except JackettError as exc:
            return err(exc.code, str(exc), status_code=exc.status_code)
        except ResolverError as exc:
            return err("resolver_error", str(exc), status_code=exc.status_code)
        except ValueError as exc:
            return err("internal_error", str(exc))
        except Exception:  # noqa: BLE001
            logger.exception("工具 %s 未预期异常 耗时=%.3fs", name, time.monotonic() - started)
            raise

    return wrapper


# --------------------------------------------------------------------------- #
# nas_save_config 的表单/参数定义
# --------------------------------------------------------------------------- #

# Octop「工具管理」页据此渲染配置表单（值经 get_tool_config 注入，
# 与 Agent 直接传参二选一，函数参数显式传入时优先）。
CONFIG_FIELDS: list[dict[str, Any]] = [
    {
        "name": "allowed_paths",
        "type": "text",
        "required": True,
        "label": "路径白名单（每行一条，支持 glob）",
        "placeholder": "/vol[1-9]*/1000",
        "description": "最高优先级安全边界；fnOS 推荐 /vol[1-9]*/1000（本地存储，新盘自动纳入）",
    },
    {
        "name": "media_roots",
        "type": "text",
        "required": False,
        "label": "媒体搜索根目录（每行一条；留空=同白名单）",
        "placeholder": "/vol[1-9]*/1000",
    },
    {
        "name": "exclude_paths",
        "type": "text",
        "required": False,
        "label": "排除目录（每行一条）",
        "placeholder": "/vol0*\n/vol[1-9]*/1000-*",
        "description": "USB 外接与网盘远程挂载；/vol0* 与 /vol[1-9]*/1000-* 为推荐默认",
    },
    {
        "name": "enable_write",
        "type": "text",
        "required": False,
        "label": "允许字幕下载落盘（true/false，默认 false）",
        "placeholder": "false",
    },
    {
        "name": "tmdb_enabled",
        "type": "text",
        "required": False,
        "label": "启用 TMDB 影视识别（true/false）",
        "placeholder": "false",
    },
    {"name": "tmdb_api_key", "type": "text", "required": False, "label": "TMDB v3 API Key（32 位十六进制）"},
    {
        "name": "jackett_enabled",
        "type": "text",
        "required": False,
        "label": "启用 Jackett 资源搜索（true/false）",
        "placeholder": "false",
    },
    {
        "name": "jackett_url",
        "type": "text",
        "required": False,
        "label": "Jackett 服务地址",
        "placeholder": "http://192.168.x.x:9117",
    },
    {"name": "jackett_api_key", "type": "text", "required": False, "label": "Jackett API Key"},
    {
        "name": "subhd_enabled",
        "type": "text",
        "required": False,
        "label": "启用 SubHD 字幕搜索（true/false，默认 true）",
        "placeholder": "true",
    },
]

_BOOL_TEXTS = {"true", "1", "yes", "on", "是", "开"}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _BOOL_TEXTS


def _as_lines(value: Any) -> list[str]:
    """多行文本/列表 → 路径条目列表（换行与逗号都作分隔，兼容表单与对话传参）。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for part in value:
            items.extend(_as_lines(part))
        return items
    out: list[str] = []
    for chunk in str(value).replace(",", "\n").splitlines():
        item = chunk.strip()
        if item:
            out.append(item)
    return out


def _injected_config() -> dict[str, Any]:
    """读取 Octop 工具管理页为 nas_save_config 注入的表单值（非 Agent 上下文返回 {}）。"""
    if get_tool_config is None:
        return {}
    try:
        data = get_tool_config("nas_save_config")
        return dict(data) if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def setup(ctx) -> None:  # noqa: C901 - 工具注册集中处，可读性优先
    """注册全部 NAS 媒体工具。每个工具调用时读取最新 config.yaml。"""

    # -- 配置工具（插件专属） ------------------------------------------------ #

    @_safe
    def nas_save_config(
        allowed_paths: str = "",
        media_roots: str = "",
        exclude_paths: str = "",
        enable_write: Any = "",
        tmdb_enabled: Any = "",
        tmdb_api_key: str = "",
        jackett_enabled: Any = "",
        jackett_url: str = "",
        jackett_api_key: str = "",
        subhd_enabled: Any = "",
    ) -> dict:
        """保存/更新插件配置并写入插件目录 config.yaml（配置变更即时生效，无需重启）。

        两种用法：① Agent 直接带参数调用（路径多值可用换行或逗号分隔）；
        ② 管理员在 Octop「工具管理」表单填好后，Agent 无参调用本工具，自动读取表单值。
        未传入/留空的字段保持现有 config.yaml 中的值不变。
        白名单 allowed_paths 必须至少一条；fnOS 推荐 /vol[1-9]*/1000。

        Args:
            allowed_paths: 路径白名单，每行/每条一个路径或 glob 模式
            media_roots: 媒体搜索根目录；留空表示与白名单相同
            exclude_paths: 排除目录（网盘/USB）；留空保留现有值
            enable_write: 是否允许字幕下载落盘（true/false）
            tmdb_enabled: 是否启用 TMDB（true/false）
            tmdb_api_key: TMDB v3 API Key；留空表示不修改
            jackett_enabled: 是否启用 Jackett（true/false）
            jackett_url: Jackett 服务地址，如 http://192.168.x.x:9117
            jackett_api_key: Jackett API Key；留空表示不修改
            subhd_enabled: 是否启用 SubHD 字幕搜索（true/false）
        """
        form = _injected_config()

        def pick(arg: Any, key: str) -> Any:
            # 显式传参优先于表单注入
            return arg if arg not in (None, "") else form.get(key, "")

        allowed = _as_lines(pick(allowed_paths, "allowed_paths"))
        roots = _as_lines(pick(media_roots, "media_roots"))
        excludes = _as_lines(pick(exclude_paths, "exclude_paths"))
        write = _as_bool(pick(enable_write, "enable_write"), default=False)
        tmdb_on = _as_bool(pick(tmdb_enabled, "tmdb_enabled"), default=False)
        tmdb_key = str(pick(tmdb_api_key, "tmdb_api_key") or "").strip()
        jackett_on = _as_bool(pick(jackett_enabled, "jackett_enabled"), default=False)
        j_url = str(pick(jackett_url, "jackett_url") or "").strip()
        j_key = str(pick(jackett_api_key, "jackett_api_key") or "").strip()
        subhd_on = _as_bool(pick(subhd_enabled, "subhd_enabled"), default=True)

        # 读出现有配置做增量合并（保留限额等高级字段）。
        data: dict[str, Any] = {}
        if CONFIG_PATH.is_file():
            try:
                loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except yaml.YAMLError as exc:
                raise InvalidArguments(f"现有 config.yaml 解析失败，已停止以免覆盖：{exc}") from exc

        if allowed:
            data["allowed_paths"] = allowed
        elif not data.get("allowed_paths"):
            raise InvalidArguments(
                "allowed_paths 白名单至少配置一条（fnOS 推荐 /vol[1-9]*/1000）"
            )
        if roots:
            data["media_roots"] = roots
        data.setdefault("media_roots", list(data["allowed_paths"]))
        if excludes:
            data["exclude_paths"] = excludes
        data["enable_write"] = write

        tmdb = dict(data.get("tmdb") or {})
        tmdb["enabled"] = tmdb_on
        if tmdb_key:
            tmdb["api_key"] = tmdb_key
            tmdb.pop("api_key_env", None)
        data["tmdb"] = tmdb

        jackett = dict(data.get("jackett") or {})
        jackett["enabled"] = jackett_on
        if j_url:
            jackett["url"] = j_url
        if j_key:
            jackett["api_key"] = j_key
            jackett.pop("api_key_env", None)
        data["jackett"] = jackett

        subhd = dict(data.get("subhd") or {})
        subhd["enabled"] = subhd_on
        data["subhd"] = subhd

        header = (
            "# 由 nas_save_config 生成/更新；高级字段（limits/media/subhd 细节）可手工补充。\n"
            "# 路径支持 glob 模式；改动即时生效。\n"
        )
        text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        CONFIG_PATH.write_text(header + text, encoding="utf-8")
        try:
            CONFIG_PATH.chmod(0o600)
        except OSError:
            pass
        _cfg_cache["mtime"] = None
        _cfg_cache["cfg"] = None

        # 立即回读校验，确认 load_config 可接受这份配置。
        cfg = _get_cfg()
        return {
            "ok": True,
            "config_path": str(CONFIG_PATH),
            "allowed_paths": list(cfg.allowed_paths),
            "media_roots": list(cfg.media_roots),
            "exclude_paths": list(cfg.exclude_paths),
            "enable_write": cfg.enable_write,
            "tmdb": {"enabled": cfg.tmdb.enabled, "api_key_configured": bool(cfg.tmdb.api_key)},
            "jackett": {
                "enabled": cfg.jackett.enabled,
                "url": cfg.jackett.url,
                "api_key_configured": bool(cfg.jackett.api_key),
            },
            "subhd": {"enabled": cfg.subhd.enabled},
            "hint": "密钥已写入 config.yaml（权限 600）；仍可手工编辑该文件调整限额等高级选项",
        }

    ctx.tool(
        "nas_save_config",
        nas_save_config,
        description=nas_save_config.__doc__,
        config_fields=CONFIG_FIELDS,
    )

    # -- 只读能力工具（与独立 MCP 版一一对应） -------------------------------- #

    @_safe
    def nas_get_config() -> dict:
        """获取 NAS 媒体工具箱当前配置：白名单、排除目录、搜索范围（含 glob 展开结果）、扩展名与限额。

        使用约定（首次使用先读本工具）：
        1. 所有 path 参数必须在 allowed_paths 内且不在 exclude_paths 中，越界即被拒绝；
        2. 中文片名找资源：先 resolve_media 拿英文标准名（resolved=false 不要猜），再 jackett_search；
        3. 补字幕：nas_scan_media(missing_subtitle_only=true) → nas_classify_region →
           subhd_search → subhd_download（唯一写操作，需 enable_write=true）；
        4. 分页用 limit/offset，配合 total/truncated，不要一次拉全量；
        5. 库治理：media_library_audit 体检 → duplicate_media_detect 查重 →
           media_quality_analyze 看质量；见 probe_budget_exhausted 就分目录多次调用；
        6. 错误均为结构化 {"ok": false, "error": {"code", "message"}}，按 code 决策。
        未配置（code=not_configured）时引导用户调用 nas_save_config。
        """
        cfg = _get_cfg()
        return {
            "ok": True,
            "server": "nas-mcp",
            "plugin": "nas-media",
            "version": __version__,
            "mode": "read-only" if not cfg.enable_write else "write-subtitle-only",
            "config_path": str(CONFIG_PATH),
            "allowed_paths": list(cfg.allowed_paths),
            "exclude_paths": list(cfg.exclude_paths),
            "media_roots": list(cfg.media_roots),
            "media_roots_expanded": list(expand_roots(cfg.media_roots)),
            "video_extensions": sorted(cfg.video_extensions),
            "subtitle_extensions": sorted(cfg.subtitle_extensions),
            "limits": asdict(cfg.limits),
            "media": asdict(cfg.media),
            "subhd": asdict(cfg.subhd),
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
            "enable_write": cfg.enable_write,
            "enable_delete": cfg.enable_delete,
        }

    @_safe
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
            limit: 单页最多返回多少条，0 表示使用默认上限
            offset: 分页偏移量，配合 total_scanned/truncated 实现翻页
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
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

    @_safe
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

        category="video" 使用配置的视频扩展名集合（mkv/mp4/...），
        category="subtitle" 使用字幕扩展名集合（srt/ass/...）；也可用 extensions
        显式指定。keyword 对文件名做大小写不敏感的包含匹配。至少需要一个过滤条件。
        默认搜索全部 media_roots；排除目录永远不会被搜索。
        命中 skip_filename_patterns 的文件（家庭录像/成人内容，如 IMG_/VID_/DJI_）
        会被静默隐藏，只在 skipped_by_filter 中计数。

        Args:
            keyword: 文件名包含的关键词（大小写不敏感），如 "breaking bad"
            category: "video" 或 "subtitle"
            extensions: 显式扩展名列表（如 ["mkv", "mp4"]），优先于 category
            root: 限定搜索范围的目录；留空则搜索全部 media_roots
            recursive: 是否递归子目录（默认是）
            limit: 单页最多返回条数，0 表示默认上限
            offset: 分页偏移量
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
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

    @_safe
    def nas_disk_usage(path: str = "") -> dict:
        """查看 NAS 存储空间（只读）。

        不带参数时返回各存储卷（按 media_roots 去重后的文件系统）的设备、挂载点、
        总容量、已用、可用与使用率；带 path 时只查看该路径所在的文件系统。

        Args:
            path: 可选；只查询该路径所在的文件系统，必须在白名单内
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
        return disk_tools.disk_usage(guard, cfg.media_roots, path=path or None)

    @_safe
    def nas_scan_media(
        path: str = "",
        recursive: bool = True,
        probe: bool = True,
        missing_subtitle_only: bool = False,
        media_type: str = "",
        limit: int = 0,
        offset: int = 0,
    ) -> dict:
        """扫描媒体目录中的视频，分析媒体信息与字幕状况（只读，需系统安装 ffprobe）。

        对每个视频文件用 ffprobe 获取编码/分辨率/帧率/音轨/内嵌字幕轨/HDR 形态，
        并在同目录匹配外挂字幕、识别语言标签，输出 has_chinese_subtitle /
        chinese_subtitle_sources。media_type（movie/tv/anime/documentary/variety/
        unknown）为文件名启发式粗分类。命中文件名跳过模式的家庭录像/成人内容
        会被隐藏并在 skipped_by_filter 计数。

        Args:
            path: 扫描起点目录；留空则扫描全部 media_roots
            recursive: 是否递归子目录（默认是）
            probe: 是否运行 ffprobe；false 时只看外挂字幕
            missing_subtitle_only: 只返回没有中文字幕的视频
            media_type: 可选过滤 movie/tv/anime/documentary/variety/unknown
            limit: 单页最多返回条数，0 表示默认上限
            offset: 分页偏移量
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
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

    @_safe
    def nas_check_subtitle(path: str) -> dict:
        """查看单个视频文件的字幕详情（只读）：外挂字幕 + ffprobe 检测的内嵌字幕。

        返回外挂字幕文件列表（含语言标签）、内嵌字幕流（语言/编码/标题）、
        音轨语言、时长、分辨率等及 has_chinese_subtitle 结论。
        ffprobe 失败时结果仍可用，失败原因在 probe_error。

        Args:
            path: 视频文件绝对路径，必须在白名单内
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
        return media_tools.check_subtitle(
            guard, cfg.video_extensions, cfg.subtitle_extensions, cfg.media, path=path
        )

    @_safe
    def nas_classify_region(path: str = "", paths: list[str] | None = None) -> dict:
        """判断视频是国产还是非国产（只读，带 confidence）。

        信号优先级：音轨语言 > TMDB 元数据（original_language/origin_country）>
        文件名配音提示（弱证据）。字幕语言不作为产地证据。
        region 取值 domestic/non_domestic/unknown；confidence 低或 unknown 时交用户决策。

        Args:
            path: 单个视频文件绝对路径，必须在白名单内
            paths: 批量模式（最多 media.max_classify_batch 个，默认 20）；与 path 二选一
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
        return region_tools.classify(
            guard, cfg.media, cfg.tmdb, cfg.video_extensions, path=path, paths=paths
        )

    @_safe
    def media_quality_analyze(path: str = "", paths: list[str] | None = None) -> dict:
        """分析视频文件的画质/音质质量标签（只读）。

        以 ffprobe 真实信息为准（分辨率/HDR 形态 SDR/HDR10/HDR10+/Dolby Vision/
        视频编码/位深/音轨编码/声道/Atmos），文件名补充来源类型（REMUX/BluRay/
        WEB-DL/WEBRip/HDTV/CAM），输出 0-100 quality_score 与 quality_tier
        （excellent/good/fair/poor/bad），并给 low_quality 结论。

        Args:
            path: 单个视频文件绝对路径，必须在白名单内
            paths: 批量模式（最多 media.max_classify_batch 个）；与 path 二选一
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
        return library_tools.quality_analyze(
            guard, cfg.media, cfg.video_extensions, path=path, paths=paths
        )

    @_safe
    def duplicate_media_detect(
        path: str = "",
        paths: list[str] | None = None,
        recursive: bool = True,
    ) -> dict:
        """检测同一作品的不同版本/重复文件（只读）。

        tmdb.enabled 时优先按 TMDB ID 归并（剧集细分到季集），未命中才退化为
        "规范化片名+年份+季集"，绝不跨语言凭文件名合并。同组用时长做合理性校验
        （差过大标 duration_mismatch）。每组给出成员列表与最佳版本 best_path。

        Args:
            path: 待检测目录（或单个视频文件）；留空且无 paths 时报错
            paths: 显式视频文件列表；与 path 二选一
            recursive: 目录模式是否递归（默认是）
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
        return library_tools.duplicate_detect(
            guard,
            cfg.limits,
            cfg.video_extensions,
            cfg.subtitle_extensions,
            cfg.media_roots,
            cfg.media,
            cfg.tmdb,
            path=path,
            paths=paths,
            recursive=recursive,
        )

    @_safe
    def media_library_audit(
        path: str = "",
        paths: list[str] | None = None,
        recursive: bool = True,
    ) -> dict:
        """媒体库一键体检，返回结构化统计报告（只读）。

        八项统计：电影数 / 电视剧数 / 无法识别数（需启用 TMDB，否则 null）/
        无字幕数 / 无中文字幕数 / 低分辨率数（≤720p）/ 重复作品组数与文件数 /
        异常文件数（ffprobe 失败）。附低清样例、重复组（含建议保留版本）与
        探测预算/截断状态；见 probe_budget_exhausted 时请分目录多次调用。

        Args:
            path: 体检目录；留空且无 paths 时体检全部 media_roots
            paths: 显式视频文件列表；与 path 二选一
            recursive: 目录模式是否递归（默认是）
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
        return library_tools.audit(
            guard,
            cfg.limits,
            cfg.video_extensions,
            cfg.subtitle_extensions,
            cfg.media_roots,
            cfg.media,
            cfg.tmdb,
            path=path,
            paths=paths,
            recursive=recursive,
        )

    @_safe
    def subhd_search(video_path: str = "", keyword: str = "") -> dict:
        """在 SubHD 上搜索字幕候选（只读网络搜索，不落盘）。

        受 subhd.enabled 开关控制。提供 video_path 时从文件名解析片名/年份/季集/
        分辨率/来源/发布组作为搜索与评分依据；也可用 keyword 自由搜索。
        返回按匹配度评分排序的候选列表（含 sid、语言、格式、来源）。

        Args:
            video_path: 视频文件绝对路径（必须在白名单内），用于解析搜索词
            keyword: 自由搜索关键词，如 "Breaking Bad 2008"
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
        return subhd_tools.search_subtitles(guard, cfg.subhd, video_path=video_path, keyword=keyword)

    @_safe
    def subhd_download(
        video_path: str,
        sid: str,
        lang: str = "zh-Hans",
        ext: str = "srt",
        overwrite: bool = False,
    ) -> dict:
        """从 SubHD 下载字幕并保存到视频同目录（唯一写操作）。

        双重门控：subhd.enabled 与 enable_write 都必须为 true。保存到视频所在目录，
        文件名按 subhd.filename_template 生成，目标路径仍受白名单校验。
        RAR 包用 unar 解压，字幕落盘前统一转 UTF-8；目标已存在默认拒绝覆盖。

        Args:
            video_path: 视频文件绝对路径（必须在白名单内）
            sid: SubHD 字幕条目 ID（由 subhd_search 返回）
            lang: 文件名语言标签（默认 zh-Hans）
            ext: 字幕扩展名（默认 srt）
            overwrite: 目标已存在时是否覆盖（默认否）
        """
        cfg = _get_cfg()
        guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
        return subhd_tools.download_subtitle(
            guard,
            cfg.subhd,
            cfg.subtitle_extensions,
            cfg.enable_write,
            video_path=video_path,
            sid=sid,
            lang=lang,
            ext=ext,
            overwrite=overwrite,
        )

    @_safe
    def resolve_media(query: str, year: int = 0, media_type: str = "") -> dict:
        """把中文/原始影视名解析成标准实体（TMDB，只读网络查询）。

        受 tmdb.enabled 开关控制。返回标准实体（canonical_title/original_title/
        year/media_type/tmdb_id）与候选列表；无法确认时 resolved=false，绝不猜测。

        Args:
            query: 影视名称，如 "星际穿越" 或 "Interstellar"
            year: 上映年份（可选，用于消歧）
            media_type: "movie" 或 "tv"（留空两者都查）
        """
        cfg = _get_cfg()
        return resolver_tools.resolve(cfg.tmdb, query, year=year or None, media_type=media_type)

    @_safe
    def jackett_search(
        query: str, year: int = 0, media_type: str = "", limit: int = 0
    ) -> dict:
        """通过 Jackett 搜索媒体资源（只读网络搜索，只返回磁力链，绝不下载）。

        受 jackett.enabled 开关控制。query 先经 TMDB 解析英文标准名与年份，
        再生成多个关键词（受 jackett.max_queries 限制）查询 Jackett，结果按
        infohash 去重、做种数降序。每条结果附 release 结构化质量维度与 0-100 评分。
        seeders_missing_count>0 表示对应索引器未提供做种数字段（抓取限制，
        非服务故障），不要臆测 Jackett 版本。

        Args:
            query: 影视名称或关键词，如 "星际穿越" 或 "Interstellar"
            year: 上映年份（可选，用于消歧与关键词生成）
            media_type: "movie" 或 "tv"（留空自动判断）
            limit: 最多返回条数（≤1000），0 表示使用 jackett.max_results
        """
        cfg = _get_cfg()
        return jackett_tools.search(
            cfg.jackett,
            cfg.tmdb,
            query=query,
            year=year,
            media_type=media_type,
            limit=limit,
        )

    for _name, _fn in (
        ("nas_get_config", nas_get_config),
        ("nas_list_directory", nas_list_directory),
        ("nas_find_files", nas_find_files),
        ("nas_disk_usage", nas_disk_usage),
        ("nas_scan_media", nas_scan_media),
        ("nas_check_subtitle", nas_check_subtitle),
        ("nas_classify_region", nas_classify_region),
        ("media_quality_analyze", media_quality_analyze),
        ("duplicate_media_detect", duplicate_media_detect),
        ("media_library_audit", media_library_audit),
        ("subhd_search", subhd_search),
        ("subhd_download", subhd_download),
        ("resolve_media", resolve_media),
        ("jackett_search", jackett_search),
    ):
        ctx.tool(_name, _fn, description=_fn.__doc__ or _name)
