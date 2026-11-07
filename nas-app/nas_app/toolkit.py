"""28 个能力的 HTTP 装配层。

与 Octop 插件 ``main.py`` / stdio 版 ``server.py`` 调用同一套 ``nas_mcp``
能力包，差异只在装配：这里把工具注册成带元数据的 ``ToolSpec``，供 Web UI
生成表单、任务系统异步执行，并统一把异常映射成结构化错误。

安全约定与另两种形态一致：
- 所有 path 参数经 PathGuard 校验（白名单 + 排除目录）；
- 写/删操作受 enable_write / enable_organize / enable_delete 门控；
- 密钥只在“是否已配置”层面回显，绝不返回明文。
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import inspect
import json
import logging
import os
import threading
import time
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from nas_mcp import __version__ as CORE_VERSION
from nas_mcp import cleanup as cleanup_tools
from nas_mcp import disk as disk_tools
from nas_mcp import dysbk as dysbk_tools
from nas_mcp import federated as federated_tools
from nas_mcp import fs as fs_tools
from nas_mcp import integrity as integrity_tools
from nas_mcp import jackett as jackett_tools
from nas_mcp import library as library_tools
from nas_mcp import media as media_tools
from nas_mcp import missing as missing_tools
from nas_mcp import mux as mux_tools
from nas_mcp import organizer as organizer_tools
from nas_mcp import region as region_tools
from nas_mcp import resolver as resolver_tools
from nas_mcp import rollback as rollback_tools
from nas_mcp import seedhub as seedhub_tools
from nas_mcp import subtitle as subtitle_tools
from nas_mcp.config import ConfigError, load_config
from nas_mcp.fs_utils import known_denied_paths, normalize_list_arg
from nas_mcp.jackett import JackettError
from nas_mcp.resolver import ResolverError
from nas_mcp.security import (
    AccessDenied,
    InvalidArguments,
    PathGuard,
    expand_roots,
)
from nas_mcp.subhd import SubhdError

from . import APP_DISPLAY_NAME, __version__

logger = logging.getLogger("nas_app.toolkit")

RISK_LABELS = {"read": "只读", "write": "写入", "delete": "删除"}


@dataclass
class ToolSpec:
    name: str
    title: str
    category: str
    risk: str
    long_running: bool
    fn: Callable[..., dict]
    description: str
    arg_docs: dict[str, str]
    params: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "category": self.category,
            "risk": self.risk,
            "risk_label": RISK_LABELS.get(self.risk, self.risk),
            "long_running": self.long_running,
            "description": self.description,
            "params": self.params,
        }


def _annotation_kind(annotation: Any) -> str:
    text = str(annotation)
    if "bool" in text:
        return "boolean"
    if "int" in text:
        return "integer"
    if "float" in text:
        return "number"
    if "list" in text:
        return "array"
    return "string"


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """拆出首段说明与 Args 段里的逐参数说明。"""
    lines = (doc or "").strip().splitlines()
    summary: list[str] = []
    args: dict[str, str] = {}
    in_args = False
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped in {"Args:", "参数:"}:
            in_args = True
            continue
        if not in_args:
            summary.append(stripped)
            continue
        if not stripped:
            current = None
            continue
        if ":" in stripped and not line.startswith((" ", "\t")):
            current = None
        if current is None:
            name, _, rest = stripped.partition(":")
            if name and rest:
                args[name.strip()] = rest.strip()
                current = name.strip()
            continue
        if current:
            args[current] = (args[current] + " " + stripped).strip()
    text = "\n".join(summary).strip()
    return text, args


def _build_params(fn: Callable) -> list[dict[str, Any]]:
    _, arg_docs = _parse_docstring(fn.__doc__ or "")
    out: list[dict[str, Any]] = []
    for name, param in inspect.signature(fn).parameters.items():
        if name in {"self", "cls"}:
            continue
        default = param.default
        required = default is inspect.Parameter.empty
        kind = _annotation_kind(param.annotation)
        item: dict[str, Any] = {
            "name": name,
            "type": kind,
            "required": required,
            "description": arg_docs.get(name, ""),
        }
        if not required:
            if isinstance(default, (str, int, float, bool)) or default is None:
                item["default"] = default
        out.append(item)
    return out


_TRUE_TEXTS = {"true", "1", "yes", "on", "是", "开", "打开", "开启"}
_FALSE_TEXTS = {"false", "0", "no", "off", "否", "关", "关闭"}


def _as_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE_TEXTS:
        return True
    if text in _FALSE_TEXTS:
        return False
    raise InvalidArguments(f"{field_name} 需要布尔值，收到 {value!r}")


def _as_lines(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for part in value:
            items.extend(_as_lines(part))
        return items
    return [chunk.strip() for chunk in str(value).replace(",", "\n").splitlines() if chunk.strip()]


def _as_list(value: Any, field_name: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("["):
            return normalize_list_arg(text, field_name)
        return _as_lines(text)
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise InvalidArguments(f"{field_name} 必须是列表或逗号分隔字符串")


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, set):
        return sorted(str(v) for v in value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _normalize_error_envelope(result: dict[str, Any]) -> dict[str, Any]:
    """把工具返回的字符串 error 收口为标准 {code, message}。

    个别底层工具（如 mux 的写门检查）仍返回 ``{"ok": False, "error": "write_disabled"}``，
    与统一错误结构不一致；任务持久化与前端渲染都假定 error 是对象，这里统一。
    """
    if result.get("ok") is not False:
        return result
    error = result.get("error")
    if error is None or isinstance(error, dict):
        return result
    normalized = dict(result)
    normalized["error"] = {
        "code": str(error),
        "message": str(result.get("message") or error),
    }
    return normalized


class ToolRegistry:
    """工具注册表 + 配置缓存 + 统一错误映射。"""

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        self._specs: dict[str, ToolSpec] = {}
        self._config_cache: dict[str, Any] = {"mtime": None, "cfg": None}
        self._lock = threading.RLock()
        self._register_all()

    # ------------------------------------------------------------------ 注册
    def tool(
        self,
        name: str,
        title: str,
        category: str,
        *,
        risk: str = "read",
        long_running: bool = False,
    ):
        def decorator(fn: Callable[..., dict]) -> Callable[..., dict]:
            description, arg_docs = _parse_docstring(fn.__doc__ or "")
            self._specs[name] = ToolSpec(
                name=name,
                title=title,
                category=category,
                risk=risk,
                long_running=long_running,
                fn=fn,
                description=description,
                arg_docs=arg_docs,
                params=_build_params(fn),
            )
            return fn

        return decorator

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def get_spec(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise InvalidArguments(f"未知工具: {name}") from exc

    # ------------------------------------------------------------------ 配置
    def reload(self) -> None:
        with self._lock:
            self._config_cache["mtime"] = None
            self._config_cache["cfg"] = None
            self.get_config()

    def get_config(self):
        if not self.config_path.is_file():
            raise ConfigError(
                f"配置文件不存在: {self.config_path}（首次启动可设置 NAS_APP_ALLOWED_PATHS 自动生成）"
            )
        mtime = self.config_path.stat().st_mtime
        with self._lock:
            if self._config_cache["cfg"] is None or mtime != self._config_cache["mtime"]:
                self._config_cache["cfg"] = load_config(str(self.config_path))
                self._config_cache["mtime"] = mtime
            return self._config_cache["cfg"]

    def _guard(self) -> PathGuard:
        cfg = self.get_config()
        return PathGuard(cfg.allowed_paths, cfg.exclude_paths)

    # ------------------------------------------------------------------ 调用
    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = self.get_spec(name)
        started = time.monotonic()
        try:
            kwargs = self._prepare_args(spec, args or {})
            logger.info("工具调用 %s 参数=%s", name, _mask(kwargs))
            result = spec.fn(**kwargs)
            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
            result = _plain(result)
            result = _normalize_error_envelope(result)
            logger.info("工具 %s 完成 耗时=%.3fs", name, time.monotonic() - started)
            return result
        except ConfigError as exc:
            return self._error(name, "not_configured", str(exc), started)
        except AccessDenied as exc:
            return self._error(name, "access_denied", exc.reason, started, path=exc.raw_path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            return self._error(name, "not_found", str(exc), started)
        except FileExistsError as exc:
            return self._error(name, "target_exists", str(exc), started)
        except PermissionError as exc:
            return self._error(name, "permission_denied", str(exc), started)
        except InvalidArguments as exc:
            return self._error(name, "invalid_arguments", str(exc), started)
        except SubhdError as exc:
            return self._error(
                name, exc.code, str(exc), started, status_code=exc.status_code
            )
        except subtitle_tools.SubtitleError as exc:
            return self._error(
                name, "all_sites_failed", str(exc), started, site_errors=exc.site_errors
            )
        except JackettError as exc:
            return self._error(
                name,
                exc.code,
                str(exc),
                started,
                status_code=exc.status_code,
                retryable=exc.retryable,
                retry_after=exc.retry_after,
            )
        except ResolverError as exc:
            return self._error(name, "resolver_error", str(exc), started, status_code=exc.status_code)
        except ValueError as exc:
            return self._error(name, "internal_error", str(exc), started)
        except Exception as exc:  # noqa: BLE001 - 边界处统一收口，避免服务崩溃
            logger.exception("工具 %s 未预期异常", name)
            return self._error(name, "internal_error", f"{type(exc).__name__}: {exc}", started)

    def _error(self, name: str, code: str, message: str, started: float, **extra: Any) -> dict:
        logger.warning("工具 %s %s 耗时=%.3fs: %s", name, code, time.monotonic() - started, message)
        payload: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
        payload["error"].update(_plain(extra))
        return payload

    def _prepare_args(self, spec: ToolSpec, raw: dict[str, Any]) -> dict[str, Any]:
        sig = inspect.signature(spec.fn)
        kwargs: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in sig.parameters:
                raise InvalidArguments(f"工具 {spec.name} 不接受参数 {key}")
            param = sig.parameters[key]
            if value is None:
                if param.default is inspect.Parameter.empty:
                    raise InvalidArguments(f"参数 {key} 不能为空")
                continue
            kind = _annotation_kind(param.annotation)
            if kind == "boolean":
                kwargs[key] = _as_bool(value, key)
            elif kind == "integer":
                if isinstance(value, str) and not value.strip():
                    continue
                try:
                    kwargs[key] = int(value)
                except (TypeError, ValueError) as exc:
                    raise InvalidArguments(f"参数 {key} 需要整数，收到 {value!r}") from exc
            elif kind == "number":
                try:
                    kwargs[key] = float(value)
                except (TypeError, ValueError) as exc:
                    raise InvalidArguments(f"参数 {key} 需要数字，收到 {value!r}") from exc
            elif kind == "array":
                parsed = _as_list(value, key)
                if parsed is None:
                    continue
                kwargs[key] = parsed
            else:
                if isinstance(value, (dict, list)):
                    raise InvalidArguments(f"参数 {key} 需要字符串")
                kwargs[key] = str(value)
        for key, param in sig.parameters.items():
            if param.default is inspect.Parameter.empty and key not in kwargs:
                raise InvalidArguments(f"缺少必填参数 {key}")
        return kwargs

    # ------------------------------------------------------------------ 配置保存
    def config_view(self) -> dict[str, Any]:
        cfg = self.get_config()
        return {
            "ok": True,
            "config_path": str(self.config_path),
            "core_version": CORE_VERSION,
            "allowed_paths": list(cfg.allowed_paths),
            "media_roots": list(cfg.media_roots),
            "exclude_paths": list(cfg.exclude_paths),
            "allowed_paths_expanded": list(expand_roots(cfg.allowed_paths)),
            "enable_write": cfg.enable_write,
            "enable_organize": cfg.enable_organize,
            "enable_delete": cfg.enable_delete,
            "tmdb": {"enabled": cfg.tmdb.enabled, "api_key_configured": bool(cfg.tmdb.api_key)},
            "jackett": {
                "enabled": cfg.jackett.enabled,
                "url": cfg.jackett.url,
                "api_key_configured": bool(cfg.jackett.api_key),
            },
            "subhd": {"enabled": cfg.subhd.enabled, "base_url": cfg.subhd.base_url},
            "opensubtitles": {
                "enabled": cfg.opensubtitles.enabled,
                "api_key_configured": bool(cfg.opensubtitles.api_key),
                "username_configured": bool(cfg.opensubtitles.username),
                "password_configured": bool(cfg.opensubtitles.password),
            },
            "shooter": {
                "enabled": cfg.shooter.enabled,
                "token_configured": bool(cfg.shooter.token),
            },
            "seedhub": {"enabled": cfg.seedhub.enabled},
            "dysbk": {"enabled": cfg.dysbk.enabled},
            "cleanup": {
                "junk_extensions": list(cfg.cleanup.junk_extensions),
                "active_download_grace_minutes": cfg.cleanup.active_download_grace_minutes,
            },
            "limits": _plain(cfg.limits),
        }

    def save_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """结构化更新 config.yaml：只改传入的字段，密钥留空表示保持原值。"""
        if not isinstance(updates, dict):
            raise InvalidArguments("配置更新必须是 JSON 对象")
        allowed_keys = {
            "allowed_paths",
            "media_roots",
            "exclude_paths",
            "enable_write",
            "enable_organize",
            "enable_delete",
            "tmdb",
            "jackett",
            "subhd",
            "opensubtitles",
            "shooter",
            "seedhub",
            "dysbk",
            "cleanup",
        }
        unknown = set(updates) - allowed_keys
        if unknown:
            raise InvalidArguments(f"不支持的配置字段: {', '.join(sorted(unknown))}")

        with self._lock:
            data: dict[str, Any] = {}
            if self.config_path.is_file():
                try:
                    loaded = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
                except yaml.YAMLError as exc:
                    raise InvalidArguments(f"现有 config.yaml 解析失败，已停止写入: {exc}") from exc
                if isinstance(loaded, dict):
                    data = loaded

            if "allowed_paths" in updates:
                allowed = _as_lines(updates["allowed_paths"])
                if not allowed:
                    raise InvalidArguments("allowed_paths 至少需要一条白名单路径")
                data["allowed_paths"] = allowed
            elif not data.get("allowed_paths"):
                raise InvalidArguments("allowed_paths 至少需要一条白名单路径")

            if "media_roots" in updates:
                roots = _as_lines(updates["media_roots"])
                if roots:
                    data["media_roots"] = roots
                else:
                    data.pop("media_roots", None)

            if "exclude_paths" in updates:
                data["exclude_paths"] = _as_lines(updates["exclude_paths"])

            for key in ("enable_write", "enable_organize", "enable_delete"):
                if key in updates:
                    data[key] = _as_bool(updates[key], key)

            self._merge_section(data, updates, "tmdb", ("enabled",), ("api_key",))
            self._merge_section(
                data, updates, "jackett", ("enabled", "url"), ("api_key",)
            )
            self._merge_section(data, updates, "subhd", ("enabled",), ())
            self._merge_section(
                data, updates, "opensubtitles",
                ("enabled", "username"), ("api_key", "password"),
            )
            self._merge_section(data, updates, "shooter", ("enabled",), ("token",))
            self._merge_section(data, updates, "seedhub", ("enabled",), ())
            self._merge_section(data, updates, "dysbk", ("enabled",), ())

            if "cleanup" in updates:
                cleanup_in = updates["cleanup"]
                if not isinstance(cleanup_in, dict):
                    raise InvalidArguments("cleanup 必须是对象")
                cleanup = dict(data.get("cleanup") or {})
                if "junk_extensions" in cleanup_in:
                    exts = []
                    for chunk in _as_lines(cleanup_in["junk_extensions"]):
                        ext = chunk.lower()
                        if ext and ext not in exts:
                            exts.append(ext if ext.startswith(".") else f".{ext}")
                    cleanup["junk_extensions"] = exts
                if "active_download_grace_minutes" in cleanup_in:
                    try:
                        grace = int(cleanup_in["active_download_grace_minutes"])
                    except (TypeError, ValueError) as exc:
                        raise InvalidArguments("宽限分钟数必须是整数") from exc
                    if grace < 0:
                        raise InvalidArguments("宽限分钟数不能为负")
                    cleanup["active_download_grace_minutes"] = grace
                data["cleanup"] = cleanup

            text = yaml.safe_dump(
                data, allow_unicode=True, sort_keys=False, default_flow_style=False
            )
            try:
                self._write_validated(text)
            except ConfigError as exc:
                raise InvalidArguments(str(exc)) from exc
            self._config_cache["mtime"] = None
            self._config_cache["cfg"] = None
            cfg = self.get_config()
            view = self.config_view()
            view["media_roots"] = list(cfg.media_roots)
            return view

    @staticmethod
    def _merge_section(
        data: dict[str, Any],
        updates: dict[str, Any],
        section: str,
        plain_fields: tuple[str, ...],
        secret_fields: tuple[str, ...],
    ) -> None:
        if section not in updates:
            return
        incoming = updates[section]
        if not isinstance(incoming, dict):
            raise InvalidArguments(f"{section} 必须是对象")
        target = dict(data.get(section) or {})
        for key in plain_fields:
            if key not in incoming or incoming[key] is None:
                continue
            if key == "enabled":
                target[key] = _as_bool(incoming[key], f"{section}.enabled")
            else:
                # 空字符串视为“保持原值”：界面无法回显全部明文，
                # 若空串覆盖会把已配置的用户名/地址抹掉（与密钥字段语义一致）。
                text = str(incoming[key]).strip()
                if text:
                    target[key] = text
        for key in secret_fields:
            value = str(incoming.get(key) or "").strip()
            if value:
                target[key] = value
                target.pop(f"{key}_env", None)
        if incoming.get("clear_secret"):
            for key in secret_fields:
                target.pop(key, None)
                target.pop(f"{key}_env", None)
        data[section] = target

    def _write_validated(self, text: str) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            load_config(str(tmp))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.config_path)
        try:
            self.config_path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ 概览
    def overview(self) -> dict[str, Any]:
        cfg_view = self.config_view()
        cfg = self.get_config()
        # 概览磁盘统计覆盖全部存储位置：媒体根目录 + 白名单内其余位置；
        # 内核 disk_usage 会按设备号去重（同一块磁盘只报一行）。
        roots = list(cfg.media_roots) + [
            p for p in cfg.allowed_paths if p not in cfg.media_roots
        ]
        if roots:
            disk = disk_tools.disk_usage(self._guard(), tuple(roots))
        else:
            disk = {"ok": True, "filesystems": [], "errors": []}
        return {
            "ok": True,
            "app": {
                "name": APP_DISPLAY_NAME,
                "version": __version__,
                "core_version": CORE_VERSION,
                "tool_count": len(self._specs),
            },
            "config": cfg_view,
            "disk": disk,
        }

    # ------------------------------------------------------------------ 注册全部工具
    def _register_all(self) -> None:  # noqa: C901 - 集中注册，可读性优先
        @self.tool("nas_get_config", "读取配置", "配置", risk="read")
        def nas_get_config() -> dict:
            """读取当前白名单、搜索范围、开关状态与限额。

            所有 path 参数都必须在 allowed_paths 内且不在 exclude_paths 中。
            API Key 只回显是否已配置，绝不返回明文。
            """
            cfg = self.get_config()
            return {
                "ok": True,
                "server": "nas-app",
                "version": CORE_VERSION,
                "config_path": str(self.config_path),
                "allowed_paths": list(cfg.allowed_paths),
                "exclude_paths": list(cfg.exclude_paths),
                "media_roots": list(cfg.media_roots),
                "media_roots_expanded": list(expand_roots(cfg.media_roots)),
                "video_extensions": sorted(cfg.video_extensions),
                "subtitle_extensions": sorted(cfg.subtitle_extensions),
                "allowed_paths_expanded": list(expand_roots(cfg.allowed_paths)),
                "limits": _plain(cfg.limits),
                "media": _plain(cfg.media),
                "organizer": _plain(cfg.organizer),
                "cleanup": _plain(cfg.cleanup),
                "subhd": _plain(cfg.subhd),
                "opensubtitles": {
                    "enabled": cfg.opensubtitles.enabled,
                    "api_key_configured": bool(cfg.opensubtitles.api_key),
                    "username_configured": bool(cfg.opensubtitles.username),
                    "language": cfg.opensubtitles.language,
                },
                "shooter": {
                    "enabled": cfg.shooter.enabled,
                    "token_configured": bool(cfg.shooter.token),
                },
                "subtitles": _plain(cfg.subtitles),
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
                "seedhub": {"enabled": cfg.seedhub.enabled},
                "dysbk": {"enabled": cfg.dysbk.enabled},
                "enable_write": cfg.enable_write,
                "enable_organize": cfg.enable_organize,
                "enable_delete": cfg.enable_delete,
                "known_unreadable": known_denied_paths(),
            }

        @self.tool("nas_list_directory", "浏览目录", "文件", risk="read")
        def nas_list_directory(path: str, recursive: bool = False, limit: int = 0, offset: int = 0) -> dict:
            """列出 NAS 上某个目录的内容。

            Args:
                path: 目录绝对路径，必须在白名单内
                recursive: 是否递归列出子目录
                limit: 单页条数，0 使用默认上限
                offset: 分页偏移
            """
            cfg = self.get_config()
            return fs_tools.list_directory(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                path=path, recursive=recursive, limit=limit, offset=offset,
            )

        @self.tool("nas_find_files", "搜索文件", "文件", risk="read")
        def nas_find_files(
            keyword: str = "",
            category: str = "",
            extensions: list[str] | str | None = None,
            root: str = "",
            recursive: bool = True,
            limit: int = 0,
            offset: int = 0,
        ) -> dict:
            """按关键词 / 类别 / 扩展名搜索媒体文件。

            Args:
                keyword: 文件名关键词（大小写不敏感）
                category: video 或 subtitle
                extensions: 显式扩展名列表，优先于 category
                root: 限定搜索目录，留空搜索全部 media_roots
                recursive: 是否递归
                limit: 单页条数
                offset: 分页偏移
            """
            extensions = normalize_list_arg(extensions, "extensions")
            cfg = self.get_config()
            return fs_tools.find_files(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                cfg.media_roots, keyword=keyword, category=category,
                extensions=extensions, root=root, recursive=recursive,
                limit=limit, offset=offset, skip_patterns=cfg.media.skip_filename_patterns,
            )

        @self.tool("nas_disk_usage", "存储空间", "文件", risk="read")
        def nas_disk_usage(path: str = "") -> dict:
            """查看各存储卷或指定路径所在文件系统的容量。

            Args:
                path: 可选，只查看该路径所在文件系统
            """
            cfg = self.get_config()
            return disk_tools.disk_usage(self._guard(), cfg.media_roots, path=path or None)

        @self.tool("nas_scan_media", "扫描媒体库", "媒体", risk="read", long_running=True)
        def nas_scan_media(
            path: str = "",
            recursive: bool = True,
            probe: bool = True,
            missing_subtitle_only: bool = False,
            media_type: str = "",
            limit: int = 0,
            offset: int = 0,
        ) -> dict:
            """扫描视频并分析编码、HDR、音轨与中文字幕状况（需要 ffprobe）。

            Args:
                path: 扫描起点目录，留空扫描全部 media_roots
                recursive: 是否递归
                probe: 是否运行 ffprobe；false 只看外挂字幕
                missing_subtitle_only: 只返回没有中文字幕的视频
                media_type: movie/tv/anime/documentary/variety/unknown
                limit: 单页条数
                offset: 分页偏移
            """
            cfg = self.get_config()
            return media_tools.scan_media(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                cfg.media_roots, cfg.media, path=path, recursive=recursive, probe=probe,
                missing_subtitle_only=missing_subtitle_only, media_type=media_type,
                limit=limit, offset=offset,
            )

        @self.tool("nas_check_subtitle", "字幕详情", "媒体", risk="read")
        def nas_check_subtitle(path: str) -> dict:
            """查看单个视频的外挂字幕与内嵌字幕轨。

            Args:
                path: 视频文件绝对路径
            """
            cfg = self.get_config()
            return media_tools.check_subtitle(
                self._guard(), cfg.video_extensions, cfg.subtitle_extensions, cfg.media, path=path
            )

        @self.tool("nas_classify_region", "国产判定", "媒体", risk="read")
        def nas_classify_region(path: str = "", paths: list[str] | str | None = None) -> dict:
            """判断视频是国产还是非国产（音轨 > TMDB > 文件名，带置信度）。

            Args:
                path: 单个视频路径
                paths: 批量视频路径列表
            """
            paths = normalize_list_arg(paths, "paths")
            cfg = self.get_config()
            return region_tools.classify(
                self._guard(), cfg.media, cfg.tmdb, cfg.video_extensions, path=path, paths=paths
            )

        @self.tool("media_quality_analyze", "画质分析", "媒体", risk="read", long_running=True)
        def media_quality_analyze(path: str = "", paths: list[str] | str | None = None) -> dict:
            """分析画质/音质并给出 0-100 质量分与 low_quality 结论。

            Args:
                path: 单个视频路径
                paths: 批量视频路径列表
            """
            paths = normalize_list_arg(paths, "paths")
            cfg = self.get_config()
            return library_tools.quality_analyze(
                self._guard(), cfg.media, cfg.video_extensions, path=path, paths=paths
            )

        @self.tool("duplicate_media_detect", "重复检测", "媒体", risk="read", long_running=True)
        def duplicate_media_detect(
            path: str = "", paths: list[str] | str | None = None, recursive: bool = True
        ) -> dict:
            """检测同一作品的多版本/重复文件，给出建议保留版本。

            Args:
                path: 待检测目录或单文件
                paths: 显式视频文件列表
                recursive: 目录模式是否递归
            """
            paths = normalize_list_arg(paths, "paths")
            cfg = self.get_config()
            return library_tools.duplicate_detect(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                cfg.media_roots, cfg.media, cfg.tmdb, path=path, paths=paths, recursive=recursive,
            )

        @self.tool("media_library_audit", "媒体库体检", "媒体", risk="read", long_running=True)
        def media_library_audit(
            path: str = "", paths: list[str] | str | None = None, recursive: bool = True
        ) -> dict:
            """媒体库八项体检：电影/剧集/无中文字幕/低清/重复/异常等。

            Args:
                path: 体检目录，留空体检全部 media_roots
                paths: 显式视频文件列表
                recursive: 是否递归
            """
            paths = normalize_list_arg(paths, "paths")
            cfg = self.get_config()
            return library_tools.audit(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                cfg.media_roots, cfg.media, cfg.tmdb, path=path, paths=paths, recursive=recursive,
            )

        @self.tool("media_missing_episodes", "缺集检测", "媒体", risk="read", long_running=True)
        def media_missing_episodes(
            title: str = "",
            season: int | None = None,
            path: str = "",
            paths: list[str] | str | None = None,
            limit: int = 100,
            offset: int = 0,
        ) -> dict:
            """按 TMDB 官方集数检测剧集缺集。

            Args:
                title: 剧名，留空扫描全部可识别剧集
                season: 季号；0 检查 Specials；留空检查本地出现的季
                path: 待检查目录或单文件
                paths: 显式文件列表
                limit: 单页条数
                offset: 分页偏移
            """
            paths = normalize_list_arg(paths, "paths")
            cfg = self.get_config()
            return missing_tools.missing_episodes(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                cfg.media_roots, cfg.media, cfg.tmdb, tvcfg=cfg.tvmaze,
                title=title, season=season,
                path=path, paths=paths, limit=limit, offset=offset,
            )

        @self.tool("media_integrity_check", "完整性检查", "媒体", risk="read", long_running=True)
        def media_integrity_check(
            path: str = "",
            paths: list[str] | str | None = None,
            mode: str = "basic",
            limit: int = 100,
            offset: int = 0,
        ) -> dict:
            """检查媒体文件完整性（basic / deep）。

            Args:
                path: 目录或单文件
                paths: 显式文件列表
                mode: basic 或 deep
                limit: 单页条数
                offset: 分页偏移
            """
            paths = normalize_list_arg(paths, "paths")
            cfg = self.get_config()
            return integrity_tools.integrity_check(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                cfg.media_roots, cfg.media, path=path, paths=paths, mode=mode,
                limit=limit, offset=offset,
            )

        @self.tool("media_cleanup", "垃圾清理", "运维", risk="delete", long_running=True)
        def media_cleanup(
            path: str = "",
            files: list[str] | str | None = None,
            dry_run: bool = True,
            limit: int = 100,
            offset: int = 0,
        ) -> dict:
            """分类媒体垃圾并生成清理计划；默认 dry_run，真删需五重门。

            Args:
                path: 扫描目录，留空扫描 media_roots
                files: 显式文件清单
                dry_run: 默认 true 只出计划
                limit: 单页条数
                offset: 分页偏移
            """
            files = normalize_list_arg(files, "files")
            cfg = self.get_config()
            return cleanup_tools.cleanup(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                cfg.media_roots, cfg.media, cfg.cleanup, cfg.organizer,
                enable_delete=cfg.enable_delete, path=path, files=files,
                dry_run=dry_run, limit=limit, offset=offset,
            )

        @self.tool("media_rollback", "操作回滚", "运维", risk="write", long_running=True)
        def media_rollback(operation_id: str = "", dry_run: bool = True) -> dict:
            """按 operation_id 回滚一次写操作；默认只做安全检查。

            Args:
                operation_id: 操作 ID（如 OP-20261009-K3M7P）
                dry_run: 默认 true 只检查
            """
            cfg = self.get_config()
            return rollback_tools.rollback(
                self._guard(), cfg.organizer, enable_organize=cfg.enable_organize,
                enable_delete=cfg.enable_delete, operation_id=operation_id, dry_run=dry_run,
            )

        @self.tool("subhd_search", "搜索字幕", "字幕", risk="read", long_running=True)
        def subhd_search(video_path: str = "", keyword: str = "") -> dict:
            """多站点搜索字幕候选（SubHD / OpenSubtitles / 射手），不落盘。

            Args:
                video_path: 视频文件路径，用于解析搜索词
                keyword: 自由关键词
            """
            cfg = self.get_config()
            return subtitle_tools.search_subtitles(
                self._guard(), cfg, video_path=video_path, keyword=keyword, tmdb_cfg=cfg.tmdb
            )

        @self.tool("subhd_download", "下载字幕", "字幕", risk="write")
        def subhd_download(
            video_path: str,
            sid: str,
            lang: str = "zh-Hans",
            ext: str = "srt",
            overwrite: bool = False,
        ) -> dict:
            """下载字幕到视频同目录（需站点 enabled 且 enable_write=true）。

            Args:
                video_path: 视频文件绝对路径
                sid: 字幕条目 ID（来自搜索结果的 sid）
                lang: 文件名语言标签
                ext: 字幕扩展名
                overwrite: 目标已存在时是否覆盖
            """
            cfg = self.get_config()
            result = subtitle_tools.download_subtitle(
                self._guard(), cfg, cfg.subtitle_extensions, cfg.enable_write,
                video_path=video_path, sid=sid, lang=lang, ext=ext, overwrite=overwrite,
            )
            if result.get("ok"):
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

        @self.tool("media_fill_subtitles", "批量补字幕", "字幕", risk="write", long_running=True)
        def media_fill_subtitles(
            path: str = "", dry_run: bool = True, lang: str = "zh-Hans", ext: str = "srt",
            check_embedded: bool = True,
        ) -> dict:
            """批量补全缺失中文字幕，按剧/季聚合，整季包优先。

            Args:
                path: 扫描目录，留空扫描 media_roots
                dry_run: 默认 true 只出计划
                lang: 文件名语言标签
                ext: 字幕扩展名
            """
            cfg = self.get_config()
            result = subtitle_tools.fill_missing_subtitles(
                self._guard(), cfg, cfg.subtitle_extensions, cfg.enable_write,
                path=path, dry_run=dry_run, lang=lang, ext=ext,
                check_embedded=check_embedded,
            )
            if result.get("ok") and not dry_run:
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

        @self.tool("media_mux_subtitles", "字幕混流", "字幕", risk="write", long_running=True)
        def media_mux_subtitles(
            path: str = "",
            video_path: str = "",
            dry_run: bool = True,
            delete_external: bool = False,
            replace_original: bool = False,
            force: bool = False,
            limit: int = 50,
        ) -> dict:
            """把外挂字幕无损混流进 MKV（ASS 优先 + SRT 保底双轨）。

            Args:
                path: 扫描目录
                video_path: 单文件模式
                dry_run: 默认 true 只出计划
                delete_external: 混流成功后删除原外挂字幕（默认 false，需 enable_delete）
                replace_original: mp4 等格式成功后删除原文件（默认 false，需 enable_delete）
                force: 已有内嵌中文字幕时仍强制混流
                limit: 单次处理上限
            """
            cfg = self.get_config()
            return mux_tools.media_mux_subtitles(
                self._guard(), cfg, path=path, video_path=video_path, dry_run=dry_run,
                delete_external=delete_external, replace_original=replace_original,
                force=force, limit=limit,
            )

        @self.tool("resolve_media", "片名解析", "资源", risk="read")
        def resolve_media(query: str, year: int = 0, media_type: str = "") -> dict:
            """把中文/原始片名解析成 TMDB 标准实体（需启用 TMDB）。

            Args:
                query: 片名，如 星际穿越
                year: 上映年份，0 表示不限定
                media_type: movie 或 tv
            """
            cfg = self.get_config()
            return resolver_tools.resolve(
                cfg.tmdb, query, year=year or None, media_type=media_type,
                tvmaze=cfg.tvmaze,
            )

        @self.tool("jackett_search", "Jackett 搜索", "资源", risk="read", long_running=True)
        def jackett_search(
            query: str, year: int = 0, media_type: str = "", limit: int = 0
        ) -> dict:
            """通过 Jackett 搜索磁力资源，只返回磁力链，绝不下载。

            Args:
                query: 片名或关键词
                year: 年份
                media_type: movie 或 tv
                limit: 最多返回条数，0 使用配置值
            """
            cfg = self.get_config()
            return jackett_tools.search(
                cfg.jackett, cfg.tmdb, query=query, year=year,
                media_type=media_type, limit=limit,
            )

        @self.tool("organize_media", "媒体整理", "整理", risk="write", long_running=True)
        def organize_media(
            path: str,
            mode: str = "all",
            dry_run: bool = True,
            preexisting_empty_scope: str = "library_root",
            language_split: bool = False,
        ) -> dict:
            """把下载目录整理成标准媒体库结构；默认只出计划。

            Args:
                path: 待整理目录绝对路径
                mode: all / movie / tv
                dry_run: 默认 true 只出计划；false 需 enable_organize
                preexisting_empty_scope: library_root 或 target_subtree
                language_split: 开启后电影/剧集按国语/外语分入子目录（ffprobe 音轨判断）
            """
            cfg = self.get_config()
            plan = organizer_tools.plan_session(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                cfg.media, cfg.organizer, path=path, mode=mode,
                preexisting_empty_scope=preexisting_empty_scope, tmdb_cfg=cfg.tmdb,
                language_split=language_split,
            )
            if dry_run:
                return plan
            return organizer_tools.execute_plan(
                self._guard(), plan, cfg.organizer,
                enable_organize=cfg.enable_organize, enable_delete=cfg.enable_delete,
            )

        @self.tool("find_media", "定位影片", "整理", risk="read", long_running=True)
        def find_media(title: str, season: int = 0, episode: int = 0) -> dict:
            """按片名在媒体库中定位视频文件。

            Args:
                title: 片名关键词
                season: 季数过滤，0 不过滤
                episode: 集数过滤，0 不过滤
            """
            cfg = self.get_config()
            return organizer_tools.find_media(
                self._guard(), cfg.limits, cfg.video_extensions, cfg.subtitle_extensions,
                cfg.media_roots, title=title, season=season, episode=episode,
            )

        @self.tool("seedhub_search", "SeedHub 搜索", "资源", risk="read")
        def seedhub_search(keyword: str) -> dict:
            """在 SeedHub 搜索影片（只支持中文片名，需 curl_cffi）。

            Args:
                keyword: 中文片名
            """
            cfg = self.get_config()
            return seedhub_tools.search(
                cfg.seedhub, keyword, timeout=cfg.media_search.seedhub_timeout_seconds
            )

        @self.tool("seedhub_magnets", "SeedHub 磁力", "资源", risk="read")
        def seedhub_magnets(movie_id: int, limit: int = 0, offset: int = 0) -> dict:
            """列出 SeedHub 影片的磁力直链。

            Args:
                movie_id: 影片 ID
                limit: 条数上限，0 为默认 50
                offset: 分页偏移
            """
            cfg = self.get_config()
            return seedhub_tools.magnets(
                cfg.seedhub, movie_id, limit, offset,
                timeout=cfg.media_search.seedhub_timeout_seconds,
            )

        @self.tool("seedhub_pans", "SeedHub 网盘", "资源", risk="read")
        def seedhub_pans(movie_id: int, limit: int = 0, offset: int = 0) -> dict:
            """列出 SeedHub 影片的网盘直链（含提取码）。

            Args:
                movie_id: 影片 ID
                limit: 条数上限，0 为默认 50
                offset: 分页偏移
            """
            cfg = self.get_config()
            return seedhub_tools.pans(
                cfg.seedhub, movie_id, limit, offset,
                timeout=cfg.media_search.seedhub_timeout_seconds,
            )

        @self.tool("dysbk_search", "网盘社搜索", "资源", risk="read")
        def dysbk_search(query: str, limit: int = 20) -> dict:
            """在网盘资源分享社搜索资源帖。

            Args:
                query: 关键词，中英文均可
                limit: 返回条数（1-50）
            """
            cfg = self.get_config()
            return dysbk_tools.search(
                cfg.dysbk, query, limit, timeout=cfg.media_search.dysbk_timeout_seconds
            )

        @self.tool("dysbk_links", "网盘社直链", "资源", risk="read")
        def dysbk_links(discussion_id: int) -> dict:
            """提取网盘社帖子中的网盘分享链接。

            Args:
                discussion_id: 讨论 ID（来自搜索）
            """
            cfg = self.get_config()
            return dysbk_tools.links(
                cfg.dysbk, discussion_id, timeout=cfg.media_search.dysbk_timeout_seconds
            )

        @self.tool("media_search", "三路聚合搜索", "资源", risk="read", long_running=True)
        def media_search(query: str) -> dict:
            """并行搜索 SeedHub + 网盘社 + Jackett，合并去重后每部留 top3。

            Args:
                query: 片名关键词，中英文均可
            """
            return federated_tools.search(self.get_config(), query)


def _mask(args: dict[str, Any]) -> dict[str, Any]:
    masked = {}
    for key, value in args.items():
        if any(token in key.lower() for token in ("key", "token", "password", "secret")):
            masked[key] = "***"
        else:
            masked[key] = value
    return masked
