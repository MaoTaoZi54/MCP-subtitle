"""媒体清理（media_cleanup，v1.7.2 阶段二）：分类、保护判断、五重门与 dry_run 计划。

职责边界（§14-§30）：
- 只处理明确的媒体管理垃圾（junk_extensions 来自 config.cleanup，不硬编码第二套，§22）；
- 只做四类分类 media / media_junk / personal_video / unknown；无法可靠判断的一律
  unknown + action=review，绝不猜测执行（§17/§19/附录 B 原则）；
- 复用现有 skip_filename_patterns（§18）：camera_personal_video 命中 → personal_video；
  成人保护等其余命中（含旧配置无分类条目）→ 保留 unknown +
  protection_reason=protected_by_skip_pattern，绝不自动升为 personal_video（B4）；
- 复用 organizer 的 special_dir_names 权威规则保护 Samples/Trailers（§16），
  本模块不重建第二套目录规则（B6）；
- 空目录不归本模块（§15，仍由 organize_media 负责，B7：扫描只 yield 文件）；
- 默认 dry_run=true 只返回计划；每项必带完整证据字段（§27），缺失 → plan_incomplete
  拒绝执行；
- 永久删除五重门（§28）：classification=media_junk、cleanup_allowed=true、
  enable_delete=true、dry_run=false、PathGuard 通过，缺一不可；
- files 显式清单只改变目标选取、不改变删除资格（§20）：任意一项越界 → 整体
  path_not_allowed 不部分执行（不抛 AccessDenied，避免被映射成 access_denied）；
  不存在条目按 not_found 报告，不静默跳过；
- 正在下载保护（§24/§25）：mtime 在 active_download_grace_minutes 窗口内、被进程
  占用、或无法确定 → cleanup_allowed=false；
- 永久删除不可回滚（§29/§30）：不建 .operation_trash，成功删除的审计记录 error
  字段写入 rollback_unsupported。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

from .config import CleanupConfig, Limits, MediaConfig, OrganizerConfig
from .fs import _walk_records
from .fs_utils import normalize_list_arg
from .organizer import _audit, new_operation_id
from .security import AccessDenied, InvalidArguments, PathGuard, expand_roots, is_within

logger = logging.getLogger(__name__)

# 证据字段（§27）：dry_run 计划每项必须携带，缺失视为计划不完整，不得执行。
EVIDENCE_FIELDS = (
    "path",
    "size",
    "mtime_age",
    "classification",
    "matched_pattern",
    "protection_reason",
    "cleanup_allowed",
    "action",
)

_VALID_CLASSIFICATIONS = ("media", "media_junk", "personal_video", "unknown")


class _PathNotAllowed(Exception):
    """内部信号：路径未通过 cleanup 的范围校验（白名单 / media_roots / 排除目录）。"""

    def __init__(self, raw: str, reason: str) -> None:
        super().__init__(reason)
        self.raw = raw
        self.reason = reason


def _path_not_allowed(raw: str, reason: str) -> dict:
    """B5：以 path_not_allowed 错误码整体拒绝（区别于 _safe 映射的 access_denied）。"""
    return {
        "ok": False,
        "error": {"code": "path_not_allowed", "message": reason, "path": raw},
    }


def _scoped_resolve(guard: PathGuard, roots: list[Path], raw: str) -> Path:
    """路径必须过 PathGuard（白名单 + 排除目录），且位于 media_roots 范围内（§20）。"""
    try:
        resolved = guard.check(raw)
    except AccessDenied as exc:
        raise _PathNotAllowed(raw, exc.reason) from exc
    if roots and not any(is_within(resolved, r) for r in roots):
        raise _PathNotAllowed(raw, "路径不在 media_roots 媒体库范围内")
    return resolved


def _file_in_use(path: Path) -> bool | None:
    """尽力检测文件是否被进程占用：True=占用 / False=未占用 / None=无法确定（§25）。"""
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return None  # 打不开（含 PermissionError）→ 无法确定 → 默认保护
    try:
        os.close(fd)
    except OSError:
        return None
    return False


def _special_dir_hit(fpath: Path, roots: list[Path], org_cfg: OrganizerConfig) -> bool:
    """复用 organizer.special_dir_names 权威规则：祖先目录命中（Samples/Trailers 等）→ 保护。

    与 organizer._is_special 同判法（strip().lower() 精确匹配），向上检查到包含它的
    media_root（含根目录本身）为止，不重建第二套规则（§16）。
    """
    special = {name.strip().lower() for name in org_cfg.special_dir_names}
    if not special:
        return False
    current = fpath.parent
    while True:
        if current.name.strip().lower() in special:
            return True
        if any(is_within(current, r) for r in roots):
            break  # 已到达媒体库根（含根目录名本身），不再向上
        if current.parent == current:
            break
        current = current.parent
    return False


def _match_skip(
    name: str, compiled_skip: list[tuple[str, str, re.Pattern[str]]]
) -> tuple[str, str] | None:
    """对文件名 stem 大小写不敏感匹配 skip 模式，返回 (pattern, category) 或 None。

    与 fs_utils.make_skip_matcher 同匹配口径（stem + re.search + IGNORECASE），
    但额外带回命中的模式与分类，供证据字段 matched_pattern 使用（§18/§27）。
    """
    stem = Path(name).stem
    for pattern, category, regex in compiled_skip:
        if regex.search(stem):
            return pattern, category
    return None


def _evaluate(
    fpath: Path,
    *,
    size: int,
    mtime_epoch: float,
    now: float,
    compiled_skip: list[tuple[str, str, re.Pattern[str]]],
    junk_exts: frozenset[str],
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    grace_seconds: int,
    roots: list[Path],
) -> dict:
    """对单个文件做分类 + 保护判断，产出带完整证据字段的计划项（§17-§27）。"""
    # 1) skip 模式优先（命中 ≠ personal_video，§18）
    matched: str | None = None
    protection: str | None = None
    hit = _match_skip(fpath.name, compiled_skip)
    if hit is not None:
        matched, category = hit
        classification = "personal_video" if category == "camera_personal_video" else "unknown"
        protection = "protected_by_skip_pattern"
    else:
        # 2) 扩展名分类：垃圾扩展名 → media_junk；视频/字幕 → media；其余 → unknown
        ext = fpath.suffix.lower()
        plain_ext = ext.lstrip(".")
        if ext in junk_exts:
            classification = "media_junk"
        elif plain_ext in video_exts or plain_ext in subtitle_exts:
            classification = "media"
        else:
            classification = "unknown"

    # 3) 保护判断（skip 已置保护时不再覆盖）
    if protection is None:
        age = max(0.0, now - mtime_epoch)
        if _special_dir_hit(fpath, roots, org_cfg):
            protection = "protected_by_special_dir_names"
        elif classification == "media":
            protection = "not_media_junk"
        elif classification == "media_junk":
            if grace_seconds > 0 and age < grace_seconds:
                protection = "active_download_grace"  # 正在下载（§24/§25）
            else:
                in_use = _file_in_use(fpath)
                if in_use is True:
                    protection = "file_in_use"
                elif in_use is None:
                    protection = "file_status_unknown"  # 无法确定 → 默认保护（§25）
        # unknown 且无命中 → protection 保持 None（review，§19）

    cleanup_allowed = classification == "media_junk" and protection is None
    if cleanup_allowed:
        action = "delete"
    elif classification == "unknown":
        action = "review"
    else:
        action = "keep"

    return {
        "path": str(fpath),
        "size": int(size),
        "mtime_age": int(now - mtime_epoch),  # 距上次修改的秒数
        "classification": classification,
        "matched_pattern": matched,
        "protection_reason": protection,
        "cleanup_allowed": cleanup_allowed,
        "action": action,
    }


def cleanup(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    mcfg: MediaConfig,
    ccfg: CleanupConfig,
    org_cfg: OrganizerConfig,
    *,
    enable_delete: bool,
    path: str = "",
    files: list[str] | None = None,
    dry_run: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """media_cleanup 入口。

    目标选取两种模式：files 显式清单（供调用方审阅 dry_run 计划后提交认可子集），
    或按 path / media_roots 扫描。范围校验失败一律整体 path_not_allowed 不部分执行。
    dry_run=true（默认）只返回计划；dry_run=false 且 enable_delete=true 时对本页
    action=delete 项执行永久删除（执行前逐项复核 PathGuard 与证据完整性）。
    """
    files = normalize_list_arg(files, "files")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise InvalidArguments(f"limit 必须是正整数，得到: {limit!r}")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise InvalidArguments(f"offset 必须是非负整数，得到: {offset!r}")

    # media_roots 范围（glob 展开；为空时只以白名单为硬边界）
    roots = [Path(p).resolve() for p in expand_roots(media_roots)]

    compiled_skip = [
        (pattern, category, re.compile(pattern, re.IGNORECASE))
        for pattern, category in mcfg.skip_pattern_categories
    ]
    junk_exts = frozenset(e.lower() if e.startswith(".") else f".{e.lower()}"
                          for e in ccfg.junk_extensions)
    grace_seconds = int(ccfg.active_download_grace_minutes) * 60

    errors: list[dict] = []
    not_found: list[dict] = []
    scan_roots: list[Path] = []
    candidates: list[tuple[Path, int, float]] = []  # (路径, size, mtime epoch)
    scan_truncated = False

    if files is not None:
        # ---- files 显式清单（§20）：先整体做范围校验，任一越界整体拒绝 ----
        if not files:
            raise InvalidArguments("files 为空列表；不提供 files 时应按 path 扫描")
        resolved_list: list[Path] = []
        for raw in files:
            raw_s = str(raw).strip()
            if not raw_s:
                raise InvalidArguments(f"files 含空条目: {files!r}")
            try:
                resolved_list.append(_scoped_resolve(guard, roots, raw_s))
            except _PathNotAllowed as exc:
                return _path_not_allowed(exc.raw, exc.reason)
        seen: set[str] = set()
        for raw, resolved in zip(files, resolved_list):
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            if not resolved.exists():
                not_found.append({"path": str(raw), "reason": "not_found"})
                continue
            if not resolved.is_file():
                not_found.append({"path": str(raw), "reason": "not_a_file"})
                continue
            try:
                st = resolved.stat()
            except OSError as exc:
                errors.append({"path": str(resolved), "error": f"stat_failed: {exc}"})
                continue
            candidates.append((resolved, st.st_size, st.st_mtime))
        scan_roots = roots
    else:
        # ---- 按 path / media_roots 扫描 ----
        if path:
            try:
                target = _scoped_resolve(guard, roots, path)
            except _PathNotAllowed as exc:
                return _path_not_allowed(exc.raw, exc.reason)
            if not target.exists():
                raise FileNotFoundError(f"路径不存在: {path}")
            scan_roots = [target]
        else:
            for root_str in expand_roots(media_roots):
                try:
                    scan_roots.append(_scoped_resolve(guard, roots, root_str))
                except _PathNotAllowed as exc:
                    return _path_not_allowed(exc.raw, exc.reason)

        walk_state: dict = {"truncated": False}

        def on_error(p: Path, message: str) -> None:
            if len(errors) < limits.max_errors_reported:
                errors.append({"path": str(p), "error": message})

        for root in scan_roots:
            if root.is_file():
                try:
                    st = root.stat()
                except OSError as exc:
                    errors.append({"path": str(root), "error": f"stat_failed: {exc}"})
                    continue
                candidates.append((root, st.st_size, st.st_mtime))
                continue
            if not root.is_dir():
                errors.append({"path": str(root), "error": "不是目录或不可访问，已跳过"})
                continue
            # 对所有文件 yield（含垃圾扩展名），include_dirs=False → 空目录不产生条目（§15）
            for record in _walk_records(
                root, guard, limits.max_scan_files, limits.max_list_depth,
                include_dirs=False, video_exts=video_exts, subtitle_exts=subtitle_exts,
                on_error=on_error, state=walk_state,
            ):
                try:
                    mtime_epoch = datetime.fromisoformat(record["mtime"]).timestamp()
                except ValueError:
                    mtime_epoch = os.path.getmtime(record["path"])
                candidates.append(
                    (Path(record["path"]), int(record["size_bytes"] or 0), mtime_epoch)
                )
        scan_truncated = bool(walk_state.get("truncated"))

    # ---- 分类 + 保护判断（全量评估，计数对整个范围有意义） ----
    now = datetime.now().timestamp()
    items: list[dict] = []
    counts = {"media": 0, "media_junk": 0, "personal_video": 0, "unknown": 0, "deletable": 0}
    for fpath, size, mtime_epoch in candidates:
        item = _evaluate(
            fpath, size=size, mtime_epoch=mtime_epoch, now=now,
            compiled_skip=compiled_skip, junk_exts=junk_exts,
            video_exts=video_exts, subtitle_exts=subtitle_exts,
            org_cfg=org_cfg, grace_seconds=grace_seconds, roots=roots,
        )
        counts[item["classification"]] += 1
        if item.get("cleanup_allowed"):  # 证据字段缺失在此只影响计数；执行前有 plan_incomplete 终检
            counts["deletable"] += 1
        items.append(item)

    # ---- 分页（§12） ----
    limit_capped = min(limit, limits.max_list_entries)
    total = len(items)
    page = items[offset:offset + limit_capped]
    next_offset = offset + len(page) if offset + len(page) < total else None

    result: dict = {
        "ok": True,
        "dry_run": bool(dry_run),
        "mode": "files" if files is not None else "scan",
        "scope": {
            "path": path or None,
            "roots": [str(r) for r in (scan_roots or roots)],
            "total_candidates": total,
            "scan_truncated": scan_truncated,
            "counts": counts,
            "enable_delete": bool(enable_delete),
            "active_download_grace_minutes": ccfg.active_download_grace_minutes,
            "junk_extensions": sorted(junk_exts),
            "evidence_fields": list(EVIDENCE_FIELDS),
        },
        "pagination": {
            "limit": limit_capped,
            "offset": offset,
            "truncated": next_offset is not None,
            "next_offset": next_offset,
        },
        "items": page,
        "not_found": not_found,
        "errors": errors,
        "operation_id": None,  # §32：执行写操作时才产生（返回值同步回填）
        "execution": {
            "performed": False,
            "deleted": [],
            "failed": [],
            "blocked_reason": None,
        },
    }

    if dry_run:
        return result  # §27：默认只出计划，不改任何文件

    # ---- 执行（§27/§28）：证据完整性 → enable_delete → 五重门复核 → 永久删除 ----
    incomplete: list[dict] = []
    for item in page:
        missing = [f for f in EVIDENCE_FIELDS if f not in item]
        if missing:
            incomplete.append({"path": item.get("path"), "missing_fields": missing})
    if incomplete:
        result["ok"] = False
        result["error"] = {
            "code": "plan_incomplete",
            "message": "计划项证据字段缺失，按 §27 规定不得执行",
            "incomplete": incomplete,
        }
        return result

    if not enable_delete:
        result["execution"]["blocked_reason"] = "enable_delete_disabled"  # B8
        return result

    result["execution"]["performed"] = True
    operation_id = new_operation_id(org_cfg)  # §32/§46：本次删除的唯一操作 ID
    result["operation_id"] = operation_id
    deleted: list[str] = result["execution"]["deleted"]
    failed: list[dict] = result["execution"]["failed"]
    for item in page:
        if item["action"] != "delete":
            continue
        # 执行前复核：五重门缺一不可（§28），PathGuard 重过一遍（防路径篡改）
        if item["classification"] != "media_junk" or not item["cleanup_allowed"]:
            continue
        raw_path = item["path"]
        try:
            resolved = _scoped_resolve(guard, roots, raw_path)
        except _PathNotAllowed as exc:
            failed.append({"path": raw_path, "error": f"path_not_allowed: {exc.reason}"})
            _audit(org_cfg, action="DELETE_CLEANUP", source=raw_path, target="",
                   result="ERROR", error=f"path_not_allowed: {exc.reason}",
                   operation_id=operation_id, channel="cleanup")
            continue
        try:
            st = resolved.stat()  # §34：删除前记录 size/mtime 供审计与回滚判定
            resolved.unlink()  # 永久删除，不可回滚（§29）
        except OSError as exc:
            failed.append({"path": raw_path, "error": str(exc)})
            _audit(org_cfg, action="DELETE_CLEANUP", source=raw_path, target="",
                   result="ERROR", error=str(exc), operation_id=operation_id,
                   channel="cleanup")
            continue
        deleted.append(raw_path)
        # §30：永久删除必须明确记录 rollback_unsupported
        _audit(org_cfg, action="DELETE_CLEANUP", source=raw_path, target="",
               result="SUCCESS", error="rollback_unsupported",
               operation_id=operation_id, size=st.st_size, mtime=st.st_mtime_ns,
               channel="cleanup")

    return result
