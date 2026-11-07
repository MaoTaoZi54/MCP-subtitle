"""配置 schema 补缺：只补缺失键，绝不改已有值、不删未知键。

设计原则（配置生命周期）：
- 运行时配置固定为 `$OCTOP_DATA_DIR/nas-media/config.yaml`；
- 首次安装文件不存在时由调用方报 `not_configured`，本模块不创建配置；
- 升级（文件已存在）时把当前 schema（dataclass 字段 + 模板键）里缺失的键
  用默认值补上；已有键与未知键原样保留；
- 只有确实补了键才写文件：先存 `config.yaml.bak`，再同目录临时文件
  `os.replace` 原子替换，权限保持 0600；没有缺失键时不触碰文件。

注意：`media.skip_filename_patterns` / `skip_pattern_categories` 不纳入
schema——它们的“缺失”语义是使用内置默认过滤集，显式补成 `[]` 反而会
关闭家庭录像/成人内容过滤，属于改变用户行为，故只在模板注释里保留。
"""

from __future__ import annotations

import copy
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import (
    DEFAULT_FAST_INDEXERS,
    DEFAULT_SUBTITLE_EXTENSIONS,
    DEFAULT_VIDEO_EXTENSIONS,
    CleanupConfig,
    DysbkConfig,
    JackettConfig,
    Limits,
    LoggingConfig,
    MediaConfig,
    MediaSearchConfig,
    OpensubtitlesConfig,
    OrganizerConfig,
    SeedhubConfig,
    ShooterConfig,
    SubhdConfig,
    SubtitlesConfig,
    TmdbConfig,
    TvmazeConfig,
)


def _list(value: Any) -> list:
    """把 dataclass 里的 tuple/frozenset 默认值转成 YAML 列表（保序）。"""
    return [item for item in value]


def _section(cls, **overrides) -> dict:
    """按 dataclass 字段默认值生成 YAML 段（字段名即 YAML 键）。"""
    import dataclasses

    out: dict[str, Any] = {}
    for field in dataclasses.fields(cls):
        if not field.init or field.default is dataclasses.MISSING:
            continue
        value = field.default
        if isinstance(value, (tuple, list, set, frozenset)):
            value = _list(value)
        out[field.name] = copy.deepcopy(value)
    out.update(overrides)
    return out


def schema_defaults() -> dict[str, Any]:
    """当前 schema 的默认值树（YAML 键名，非 dataclass 字段名）。"""
    media = _section(MediaConfig)
    # 这两个键不补：缺失 = 使用内置默认过滤集；显式 [] 会关闭过滤，属于改变行为
    media.pop("skip_filename_patterns", None)
    media.pop("skip_pattern_categories", None)
    return {
        "video_extensions": list(DEFAULT_VIDEO_EXTENSIONS),
        "subtitle_extensions": list(DEFAULT_SUBTITLE_EXTENSIONS),
        "limits": _section(Limits),
        "media": media,
        "organizer": _section(OrganizerConfig),
        "cleanup": _section(CleanupConfig),
        "logging": _section(LoggingConfig),
        # YAML 键是 subtitle（单数），dataclass 是 SubtitlesConfig
        "subtitle": _section(SubtitlesConfig),
        "opensubtitles": _section(OpensubtitlesConfig),
        "shooter": _section(ShooterConfig),
        "subhd": _section(SubhdConfig),
        "tmdb": _section(TmdbConfig),
        # 插件形态的生效默认值是开启（容器形态仍为 False）；写显式 true
        # 与「缺段即开」的行为一致，不改变现有配置的选路。
        "tvmaze": _section(TvmazeConfig, enabled=True),
        "jackett": _section(JackettConfig, fast_indexers=list(DEFAULT_FAST_INDEXERS)),
        "seedhub": _section(SeedhubConfig),
        "dysbk": _section(DysbkConfig),
        "media_search": _section(MediaSearchConfig),
        "enable_write": False,
        "enable_organize": False,
        "enable_delete": False,
    }


def _merge_missing(data: dict, defaults: dict, prefix: str = "") -> list[str]:
    """把 defaults 中缺失的键补进 data（递归），返回补上的点分键路径列表。"""
    added: list[str] = []
    for key, default in defaults.items():
        dotted = f"{prefix}{key}"
        if key not in data:
            data[key] = copy.deepcopy(default)
            added.append(dotted)
        elif isinstance(default, dict) and isinstance(data.get(key), dict):
            added.extend(_merge_missing(data[key], default, f"{dotted}."))
    return added


def _missing_tree(data: dict, defaults: dict, prefix: str = "",
                  added: list[str] | None = None) -> dict:
    """只取缺失部分（不修改 data），同时收集补上的点分键路径。"""
    if added is None:
        added = []
    out: dict[str, Any] = {}
    for key, default in defaults.items():
        dotted = f"{prefix}{key}"
        if key not in data:
            out[key] = copy.deepcopy(default)
            added.append(dotted)
        elif isinstance(default, dict) and isinstance(data.get(key), dict):
            sub = _missing_tree(data[key], default, f"{dotted}.", added)
            if sub:
                out[key] = sub
    return out


_TOP_LEVEL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def _dump_block(value: Any, indent: int) -> list[str]:
    """把缺失片段转成 YAML 行（不动原文件的其他行）。"""
    text = yaml.safe_dump(
        value, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).rstrip("\n")
    pad = " " * indent
    return [pad + line if line.strip() else "" for line in text.split("\n")]


def _inject_missing(text: str, missing: dict) -> str | None:
    """在原文里就地插入缺失键，保留已有注释与格式。

    返回 None 表示结构不适合行级插入（如 section 用了 flow 写法），
    调用方回退到整文件重写。
    """
    lines = text.splitlines()
    positions: dict[str, tuple[int, str]] = {}
    for i, line in enumerate(lines):
        match = _TOP_LEVEL_RE.match(line)
        if match:
            positions.setdefault(match.group(1), (i, match.group(2)))

    edits: list[tuple[int, list[str]]] = []
    appended: list[list[str]] = []

    for key, value in missing.items():
        if key not in positions:
            appended.append(_dump_block({key: value}, 0))
            continue
        start, tail = positions[key]
        # 已有 section 必须是块映射；flow/scalar 写法无法按行补子键
        if tail.strip() and not tail.strip().startswith("#"):
            return None
        if not isinstance(value, dict):
            return None
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if _TOP_LEVEL_RE.match(lines[j]):
                end = j
                break
        child_indent: int | None = None
        for j in range(start + 1, end):
            line = lines[j]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line.startswith((" ", "\t")):
                # 顶层块序列（- item）不属于子键；不冒险插入
                return None
            child_indent = len(line) - len(line.lstrip(" "))
            break
        if child_indent is None:
            child_indent = 2
        # 插入点：section 末尾，但跳过紧贴下一个顶层键的空白/注释块
        insert_at = end
        while insert_at > start + 1 and (
            not lines[insert_at - 1].strip() or lines[insert_at - 1].startswith("#")
        ):
            insert_at -= 1
        edits.append((insert_at, _dump_block(value, child_indent)))

    for index, block in sorted(edits, key=lambda item: item[0], reverse=True):
        lines[index:index] = block

    if appended:
        if lines and lines[-1].strip():
            lines.append("")
        for block in appended:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend(block)

    return "\n".join(lines) + ("\n" if lines else "")


def apply_schema(config_path: Path, *, backup_suffix: str = ".bak") -> dict:
    """对已存在的配置补缺失键；返回可追溯的 schema 报告。

    - 文件不存在：返回 `status="not_configured"`，不创建任何文件；
    - 无缺失键：`written=false`，不触碰文件；
    - 有缺失键：备份 `<config>.bak` → 临时文件 → `os.replace` 原子替换，
      写完 chmod 0600。
    """
    path = Path(config_path)
    if not path.is_file():
        return {
            "status": "not_configured",
            "source_path": str(path),
            "added_keys": [],
            "written": False,
            "backup_path": None,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    try:
        raw_text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {
            "status": "unreadable",
            "source_path": str(path),
            "added_keys": [],
            "written": False,
            "backup_path": None,
            "error": str(exc),
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if not isinstance(data, dict):
        return {
            "status": "invalid",
            "source_path": str(path),
            "added_keys": [],
            "written": False,
            "backup_path": None,
            "error": "配置顶层不是键值映射",
            "checked_at": checked_at,
        }

    defaults = schema_defaults()
    added: list[str] = []
    missing = _missing_tree(data, defaults, added=added)
    if not missing:
        return {
            "status": "ok",
            "source_path": str(path),
            "added_keys": [],
            "written": False,
            "backup_path": None,
            "checked_at": checked_at,
        }

    backup = path.with_name(path.name + backup_suffix)
    try:
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    except OSError as exc:
        return {
            "status": "error",
            "source_path": str(path),
            "added_keys": added,
            "written": False,
            "backup_path": str(backup),
            "error": f"备份失败: {exc}",
            "checked_at": checked_at,
        }

    header = (
        f"# 由 nas-media 插件于 {checked_at} 自动补齐缺失键："
        f"{', '.join(added)}\n"
        f"# 原文件已备份为 {backup.name}；已有值与未知键均未改动。\n"
    )
    injected = _inject_missing(raw_text, missing)
    if injected is None:
        # 行级插入不适用（flow 写法等）才整文件重写；已有值/未知键仍保持不变
        _merge_missing(data, defaults)
        body = yaml.safe_dump(
            data, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
    else:
        body = injected
    text = header + body
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return {
            "status": "error",
            "source_path": str(path),
            "added_keys": added,
            "written": False,
            "backup_path": str(backup),
            "error": f"写入失败: {exc}",
            "checked_at": checked_at,
        }

    return {
        "status": "merged",
        "source_path": str(path),
        "added_keys": added,
        "written": True,
        "backup_path": str(backup),
        "backup_exists": backup.is_file(),
        "checked_at": checked_at,
    }
