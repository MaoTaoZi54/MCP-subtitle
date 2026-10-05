"""文件系统工具实现：nas_list_directory / nas_find_files。

只读操作；分页 + 扫描上限；单文件异常不影响整体结果。
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import Limits
from .fs_utils import iter_tree, make_record, make_skip_matcher
from .security import InvalidArguments, PathGuard, expand_roots


def list_directory(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    *,
    path: str,
    recursive: bool = False,
    limit: int = 0,
    offset: int = 0,
) -> dict:
    root = guard.check(path)
    if not root.exists():
        raise FileNotFoundError(f"目录不存在: {path}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录: {path}")

    limit = min(limit if limit > 0 else limits.max_list_entries, limits.max_list_entries)
    offset = max(offset, 0)
    window_end = offset + limit

    errors: list[dict] = []

    def on_error(p: Path, message: str) -> None:
        if len(errors) < limits.max_errors_reported:
            errors.append({"path": str(p), "error": message})

    state = {"truncated": False}
    if recursive:
        records = _walk_records(
            root, guard, limits.max_scan_files, limits.max_list_depth,
            include_dirs=True, video_exts=video_exts, subtitle_exts=subtitle_exts,
            on_error=on_error, state=state,
        )
    else:
        records = _flat_records(root, guard, video_exts, subtitle_exts, on_error)

    entries: list[dict] = []
    scanned = 0
    for record in records:
        scanned += 1
        if offset <= scanned - 1 < window_end:
            entries.append(record)

    return {
        "ok": True,
        "path": str(path),
        "resolved": str(root),
        "recursive": recursive,
        "offset": offset,
        "limit": limit,
        "total_scanned": scanned,
        "returned": len(entries),
        "truncated": scanned > window_end,
        "scan_truncated": state["truncated"],
        "errors": errors,
        "entries": entries,
    }


def find_files(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    *,
    keyword: str = "",
    category: str = "",
    extensions: list[str] | None = None,
    root: str = "",
    recursive: bool = True,
    limit: int = 0,
    offset: int = 0,
    skip_patterns: tuple[str, ...] = (),
) -> dict:
    # ---- 过滤条件解析：至少需要一个，避免无谓的全盘枚举 ----
    category_used: str | None = None
    if extensions:
        ext_filter = {str(e).lower().lstrip(".").strip() for e in extensions}
        ext_filter.discard("")
        if not ext_filter:
            raise InvalidArguments("extensions 不能为空列表")
    elif category:
        cat = category.strip().lower()
        if cat == "video":
            ext_filter, category_used = set(video_exts), "video"
        elif cat == "subtitle":
            ext_filter, category_used = set(subtitle_exts), "subtitle"
        else:
            raise InvalidArguments("category 只支持 video / subtitle")
    else:
        ext_filter = None

    kw = (keyword or "").strip().lower() or None
    if ext_filter is None and kw is None:
        raise InvalidArguments("至少提供 keyword / category / extensions 之一作为过滤条件，避免全盘枚举")

    # ---- 搜索范围 ----
    if root:
        bases = [guard.check(root)]
    else:
        bases = [guard.check(r) for r in expand_roots(media_roots)]
    unique: list[Path] = []
    for b in bases:
        if b not in unique:
            unique.append(b)

    limit = min(limit if limit > 0 else limits.max_search_results, limits.max_search_results)
    offset = max(offset, 0)
    window_end = offset + limit

    errors: list[dict] = []

    def on_error(p: Path, message: str) -> None:
        if len(errors) < limits.max_errors_reported:
            errors.append({"path": str(p), "error": message})

    state = {"truncated": False}
    matches: list[dict] = []
    total_matched = 0
    scanned = 0
    skipped_by_filter = 0
    is_skipped = make_skip_matcher(skip_patterns)

    for base in unique:
        if not base.is_dir():
            errors.append({"path": str(base), "error": "目录不存在或不可访问，已跳过"})
            continue
        if recursive:
            records = _walk_records(
                base, guard, limits.max_scan_files, limits.max_list_depth,
                include_dirs=False, video_exts=video_exts, subtitle_exts=subtitle_exts,
                on_error=on_error, state=state,
            )
        else:
            records = _flat_records(base, guard, video_exts, subtitle_exts, on_error)

        for record in records:
            # 文件名跳过模式（家庭录像/成人内容）：命中即不计入扫描结果，
            # 单独计数让调用方知道有文件被隐藏
            if is_skipped(record["name"]):
                skipped_by_filter += 1
                continue
            scanned += 1
            ext = record["extension"] or ""
            if ext_filter is not None and ext not in ext_filter:
                continue
            if kw is not None and kw not in record["name"].lower():
                continue
            total_matched += 1
            if offset <= total_matched - 1 < window_end:
                matches.append(record)

    return {
        "ok": True,
        "roots": [str(b) for b in unique],
        "keyword": kw,
        "category": category_used,
        "extensions": sorted(ext_filter) if ext_filter is not None else None,
        "recursive": recursive,
        "offset": offset,
        "limit": limit,
        "total_matched": total_matched,
        "returned": len(matches),
        "truncated": total_matched > window_end,
        "scanned_files": scanned,
        "skipped_by_filter": skipped_by_filter,
        "scan_truncated": state["truncated"],
        "errors": errors,
        "matches": matches,
    }


def _flat_records(
    root: Path,
    guard: PathGuard,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    on_error,
):
    """列出不递归的直接子条目；排除目录/白名单外链接直接隐藏。"""
    try:
        with os.scandir(root) as iterator:
            raw = list(iterator)
    except OSError:
        raise
    raw.sort(key=lambda e: e.name)
    for entry in raw:
        try:
            target = Path(entry.path).resolve()
            if not guard.is_allowed_resolved(target):
                continue
            st = entry.stat(follow_symlinks=False)
        except OSError as exc:
            on_error(Path(entry.path), str(exc))
            continue
        yield make_record(
            path=Path(entry.path), st=st, relative_to=root,
            video_exts=video_exts, subtitle_exts=subtitle_exts,
        )


def _walk_records(
    root: Path,
    guard: PathGuard,
    max_files: int,
    max_depth: int,
    *,
    include_dirs: bool,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    on_error,
    state: dict,
):
    for fpath, st in iter_tree(
        root, guard, max_files=max_files, max_depth=max_depth,
        include_dirs=include_dirs, on_error=on_error, state=state,
    ):
        yield make_record(
            path=fpath, st=st, relative_to=root,
            video_exts=video_exts, subtitle_exts=subtitle_exts,
        )

