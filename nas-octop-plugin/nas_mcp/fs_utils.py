"""文件系统遍历与条目记录构造的公共工具。

设计要点：
- 单文件异常只记录到 errors 列表，绝不让整个扫描失败（规格 §27）；
- 遍历不跟随符号链接（防止逃逸出白名单与死循环）；
- 所有条目数都有上限（规格 §28 性能要求）。
"""

from __future__ import annotations

import os
import re
import stat as stat_module
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from .security import PathGuard


def make_skip_matcher(patterns) -> Callable[[str], bool]:
    """根据文件名模式正则列表构造"应跳过"判定函数（家庭录像/成人内容等）。

    对文件名 stem（去扩展名）做大小写不敏感的 search 匹配；patterns 为空时
    返回恒 False 的函数（零开销）。匹配在调用侧（nas_find_files /
    nas_scan_media）进行，被跳过文件单独计数，不出现在结果中。
    """
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

    def is_skipped(name: str) -> bool:
        stem = Path(name).stem
        return any(c.search(stem) for c in compiled)

    return is_skipped


def human_size(num: float) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def media_category(extension: str, video_exts: frozenset[str], subtitle_exts: frozenset[str]) -> str | None:
    if not extension:
        return None
    if extension in video_exts:
        return "video"
    if extension in subtitle_exts:
        return "subtitle"
    return None


def kind_from_stat(st: os.stat_result) -> str:
    mode = st.st_mode
    if stat_module.S_ISLNK(mode):
        return "symlink"
    if stat_module.S_ISDIR(mode):
        return "dir"
    if stat_module.S_ISREG(mode):
        return "file"
    return "other"


def make_record(
    *,
    path: Path,
    st: os.stat_result,
    relative_to: Path,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
) -> dict:
    """把一个文件系统条目转成结构化记录（规格 §29：返回结构化数据）。"""
    name = path.name
    kind = kind_from_stat(st)
    ext = Path(name).suffix.lstrip(".").lower() if kind in ("file", "symlink") else ""
    return {
        "name": name,
        "type": kind,
        "extension": ext or None,
        "media_category": media_category(ext, video_exts, subtitle_exts),
        "size_bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
        "relative_path": path.relative_to(relative_to).as_posix(),
        "path": str(path),
    }


def iter_tree(
    root: Path,
    guard: PathGuard,
    *,
    max_files: int,
    max_depth: int,
    include_dirs: bool,
    on_error: Callable[[Path, str], None],
    state: dict,
) -> Iterator[tuple[Path, os.stat_result]]:
    """深度受限、排除目录剪枝、错误不中断的目录树遍历（不跟随符号链接）。

    依次 yield (路径, lstat 结果)；访问条目数达到 max_files 时提前停止并置
    state["truncated"] = True。计数器存于 state["count"]，调用方在多个根目录
    之间复用同一 state 即可实现全局限额。子目录若（解析符号链接后）落在白名单
    外或排除目录内，则整枝剪掉；文件级符号链接同样检查目标，越界链接直接隐藏
    （与平铺模式行为一致，防止经链接读取白名单外内容）。
    """
    root_depth = len(root.parts)
    state.setdefault("count", 0)

    def walk_onerror(err: OSError) -> None:
        on_error(Path(getattr(err, "filename", "") or str(root)), str(err))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=walk_onerror):
        current = Path(dirpath)
        depth = len(current.parts) - root_depth

        kept: list[str] = []
        for name in sorted(dirnames):
            if depth + 1 > max_depth:
                break
            child = current / name
            try:
                target = child.resolve()
            except OSError:
                continue
            if not guard.is_allowed_resolved(target):
                continue
            kept.append(name)
        dirnames[:] = kept

        if include_dirs:
            for name in kept:
                if state["count"] >= max_files:
                    state["truncated"] = True
                    return
                child = current / name
                try:
                    st = os.lstat(child)
                except OSError as exc:
                    on_error(child, str(exc))
                    continue
                state["count"] += 1
                yield child, st

        for name in sorted(filenames):
            if state["count"] >= max_files:
                state["truncated"] = True
                return
            fpath = current / name
            try:
                st = os.lstat(fpath)
                # 文件级符号链接：目标越出白名单/落入排除目录时直接隐藏，
                # 否则 ffprobe 等下游会跟随链接读到白名单外的内容
                if stat_module.S_ISLNK(st.st_mode) and not guard.is_allowed_resolved(
                    fpath.resolve()
                ):
                    continue
            except OSError as exc:
                on_error(fpath, str(exc))
                continue
            state["count"] += 1
            yield fpath, st
