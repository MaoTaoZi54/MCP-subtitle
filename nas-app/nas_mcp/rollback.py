"""操作回滚（media_rollback，v1.7.2 阶段四）：按 operation_id 回滚写操作（§35-§48）。

核心约束：
- 只接受 operation_id（Agent 不得传 source/target 要求回滚，§35）；
- 支持 move / rename / create 三类（§36）+ 两类目录逆操作（§49/#9）：
  RMDIR_EMPTY → 重建空目录、organize 新建的 MKDIR 目标侧目录 → 逆向删除
  （仅空壳，且必须排在 move 恢复之后执行）；永久删除一律 rollback_unsupported（§44）；
- legacy 审计记录（无 operation_id）→ rollback_unsupported，绝不按 source/target
  猜测（§33/O5）；
- 批量回滚：先检查全部项目，任一冲突 → 整体 rollback_conflict，在物理执行前
  整体中止，绝不"能回多少回多少"（§43/O8）；
- 快速完整性校验默认 size + mtime（st_mtime_ns），不默认全量 SHA256（§41/§42）；
- 八项安全检查（§40）：operation_id 存在、类型支持、source/target 存在、路径仍在
  允许范围、冲突、当前权限（§45）、size 匹配、mtime 匹配；
- dry_run=true（默认）只检查返回 rollback_ready，不改文件（§48）；
- 执行中途不可恢复错误 → partially_rolled_back，完整报告
  rolled_back_items / failed_items / errors（§47/O9）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import OrganizerConfig
from .fs_utils import ensure_dir
from .organizer import _audit, _move_file, read_audit_records
from .security import AccessDenied, InvalidArguments, PathGuard

logger = logging.getLogger(__name__)

# 审计 action → 回滚处理类别（仅 result=SUCCESS 的记录代表已执行动作）
MOVE_ACTIONS = frozenset({"MOVE", "MOVE_RELATED", "RENAME_DIR"})
CREATE_ACTIONS = frozenset({"CREATE"})          # subhd_download 等新建文件
MKDIR_ACTIONS = frozenset({"RMDIR_EMPTY"})      # 回滚 = 重建空目录（create 语义）
# #9：organize 新建的目标侧目录（Season XX 空壳）→ 回滚 = 逆向删除（仅空目录）
MKDIR_CREATED_ACTIONS = frozenset({"MKDIR"})
DELETE_ACTIONS = frozenset(
    {"DELETE_DUPLICATE", "DELETE_RELATED", "DELETE_CLEANUP"}
)
WRITE_ACTIONS = (
    MOVE_ACTIONS | CREATE_ACTIONS | MKDIR_ACTIONS
    | MKDIR_CREATED_ACTIONS | DELETE_ACTIONS
)


def _item(audit_action: str, kind: str, path: str, restore_to: str | None,
          status: str, reason: str = "") -> dict:
    return {
        "audit_action": audit_action,
        "type": kind,            # move / create / mkdir
        "path": path,             # 涉及对象的当前物理路径
        "restore_to": restore_to,  # move：回滚后的原位置；create/mkdir 为 None
        "status": status,
        "reason": reason,
    }


def _overall(items: list[dict], *, dry_run: bool, execute) -> tuple[str, dict]:
    """按 §43 批量语义判定整体状态：权限 > 冲突 > not_found > ready/执行。"""
    statuses = {i["status"] for i in items}
    if "permission_denied" in statuses:
        return "permission_denied", {}
    if "rollback_conflict" in statuses:
        return "rollback_conflict", {}  # 执行前整体中止（§43）
    if "not_found" in statuses:
        return "not_found", {}
    if dry_run:
        return "rollback_ready", {}
    return execute(items)


def rollback(
    guard: PathGuard,
    org_cfg: OrganizerConfig,
    *,
    enable_organize: bool,
    enable_delete: bool,
    operation_id: str,
    dry_run: bool = True,
) -> dict:
    """按 operation_id 回滚一次写操作；dry_run=true（默认）只做八项检查。"""
    operation_id = str(operation_id or "").strip()
    if not operation_id:
        raise InvalidArguments(
            "operation_id 必填：只能按操作 ID 回滚（不接受 source/target，§35）"
        )

    # OPEN-8：cleanup 操作审计在独立 cleanup-audit.jsonl，回滚查询需覆盖两通道，
    # 否则 cleanup 操作的 operation_id 会被误判为 not_found（应返回 rollback_unsupported）
    records = read_audit_records(org_cfg) + read_audit_records(
        org_cfg, channel="cleanup"
    )
    matching = [r for r in records if r.get("operation_id") == operation_id]

    def _early(status: str, reason: str) -> dict:
        return {
            "ok": True,
            "operation_id": operation_id,
            "dry_run": bool(dry_run),
            "status": status,
            "reason": reason,
            "items": [],
            "rolled_back_items": [],
            "failed_items": [],
            "errors": [],
            "checks": [
                "operation_id_exists", "action_supported", "source_target_exists",
                "path_allowed", "conflict", "permission", "size", "mtime",
            ],
        }

    # MUX 操作为重封装覆盖，不支持回滚
    if any(r.get("action") == "MUX" for r in matching):
        return _early("rollback_unsupported", "mux_operation_cannot_be_rolled_back")

    if not matching:
        # §33/O5：纯 legacy 日志（全部无 operation_id）→ rollback_unsupported；
        # 混合日志里查不到该 ID → not_found（不得按 source/target 猜测）
        legacy = [
            r for r in records
            if r.get("result") == "SUCCESS" and r.get("action") in WRITE_ACTIONS
            and not r.get("operation_id")
        ]
        has_modern = any(r.get("operation_id") for r in records)
        if legacy and not has_modern:
            return _early("rollback_unsupported", "legacy_audit_no_operation_id")
        return _early("not_found", "unknown_operation_id")

    done = [
        r for r in matching
        if r.get("result") == "SUCCESS" and r.get("action") in WRITE_ACTIONS
    ]
    if not done:
        return _early("not_found", "operation_has_no_successful_actions")

    # 操作类型判定：含已执行的永久删除 → 整体不支持回滚（§44，不伪造回滚）
    kinds: set[str] = set()
    for r in done:
        action = r["action"]
        if action in MOVE_ACTIONS:
            kinds.add("move")
        elif action in CREATE_ACTIONS:
            kinds.add("create")
        elif action in MKDIR_ACTIONS:
            kinds.add("mkdir")
        elif action in MKDIR_CREATED_ACTIONS:
            kinds.add("rmdir")  # #9：目标侧新建目录，逆向删除空壳
        else:
            kinds.add("delete")
    if "delete" in kinds:
        return _early(
            "rollback_unsupported",
            "operation_contains_permanent_delete",  # §44
        )

    # 八项安全检查（§40），逐项目构建状态；批量语义稍后统一裁决（§43）
    items: list[dict] = []
    for r in done:
        action = r["action"]
        rec_size, rec_mtime = r.get("size"), r.get("mtime")
        recorded = rec_size is not None and rec_mtime is not None
        if action in MOVE_ACTIONS:
            kind = "move"
            original, current = str(r.get("source") or ""), str(r.get("target") or "")
        elif action in CREATE_ACTIONS:
            kind = "create"
            original, current = "", str(r.get("target") or "")
        elif action in MKDIR_ACTIONS:  # RMDIR_EMPTY → 重建目录（原地 create）
            kind = "mkdir"
            original, current = "", str(r.get("source") or "")
        else:  # MKDIR（#9）→ 逆向删除 organize 新建的目标侧目录
            kind = "rmdir"
            original, current = "", str(r.get("target") or "")

        # 4) 路径仍在允许范围（PathGuard：白名单 + 排除目录）
        try:
            current_p = guard.check(current)
            original_p = guard.check(original) if original else None
        except AccessDenied as exc:
            items.append(_item(action, kind, current, original,
                               "permission_denied", f"path_not_allowed: {exc.reason}"))
            continue

        # 6) 当前权限（§45：move/mkdir/rmdir 需 enable_organize，create 需 enable_delete）
        needs_organize = kind in ("move", "mkdir", "rmdir")
        needs_delete = kind == "create"
        if (needs_organize and not enable_organize) or (needs_delete and not enable_delete):
            flag = "enable_organize" if needs_organize else "enable_delete"
            items.append(_item(action, kind, current, original,
                               "permission_denied", f"{flag}_disabled"))
            continue

        if kind == "move":
            # 3) target（当前位置）必须存在；source（原位置）不得被占用（O2/O3）
            if not current_p.exists():
                items.append(_item(action, kind, current, original,
                                   "rollback_conflict", "target_missing"))
                continue
            assert original_p is not None
            if original_p.exists():
                items.append(_item(action, kind, current, original,
                                   "rollback_conflict", "source_occupied"))
                continue
            # 7/8) size + mtime 快速完整性（O4；记录缺失也视为不可验证 → 冲突）
            try:
                st = current_p.stat()
            except OSError as exc:
                items.append(_item(action, kind, current, original,
                                   "rollback_conflict", f"stat_failed: {exc}"))
                continue
            if not recorded or st.st_size != rec_size or st.st_mtime_ns != rec_mtime:
                items.append(_item(action, kind, current, original,
                                   "rollback_conflict", "size_mtime_mismatch"))
                continue
            items.append(_item(action, kind, current, original, "rollback_ready"))
        elif kind == "create":
            # O7：文件存在且 size/mtime 匹配才允许删除回滚；变化 → 冲突
            if not current_p.exists():
                items.append(_item(action, kind, current, None,
                                   "not_found", "created_file_missing"))
                continue
            try:
                st = current_p.stat()
            except OSError as exc:
                items.append(_item(action, kind, current, None,
                                   "rollback_conflict", f"stat_failed: {exc}"))
                continue
            if not recorded or st.st_size != rec_size or st.st_mtime_ns != rec_mtime:
                items.append(_item(action, kind, current, None,
                                   "rollback_conflict", "size_mtime_mismatch"))
                continue
            items.append(_item(action, kind, current, None, "rollback_ready"))
        else:
            # mkdir（RMDIR_EMPTY 逆操作）：目录已存在视作已恢复（幂等），被文件占用 → 冲突
            # rmdir（#9 MKDIR 逆操作）：检查阶段文件尚未搬回、目录可能仍非空——
            # 空否留到执行期（移动恢复之后）判定；目录已不存在 → 幂等就绪
            if current_p.exists() and not current_p.is_dir():
                items.append(_item(action, kind, current, None,
                                   "rollback_conflict", "path_occupied_by_file"))
                continue
            items.append(_item(action, kind, current, None, "rollback_ready"))

    def execute(batch: list[dict]) -> tuple[str, dict]:
        rolled: list[dict] = []
        failed: list[dict] = []
        errors: list[str] = []
        # #9：rmdir（删除目标侧新建空壳）必须排在 move/create 之后——
        # 目录要等文件搬回原位置才会变空；同组内按路径深度倒序（先深后浅）
        ordered = (
            [it for it in batch if it["type"] != "rmdir"]
            + sorted(
                (it for it in batch if it["type"] == "rmdir"),
                key=lambda i: -len(Path(i["path"]).parts),
            )
        )
        for it in ordered:
            p = Path(it["path"])
            try:
                if it["type"] == "move":
                    restore = Path(it["restore_to"])
                    ensure_dir(restore.parent)
                    _move_file(p, restore)
                    _audit(org_cfg, action="ROLLBACK_MOVE", source=str(restore),
                           target=str(p), result="SUCCESS", operation_id=operation_id)
                elif it["type"] == "create":
                    p.unlink()  # 删除新建文件（create 回滚），预检已过 §38
                    _audit(org_cfg, action="ROLLBACK_DELETE", source="",
                           target=str(p), result="SUCCESS", operation_id=operation_id)
                elif it["type"] == "rmdir":
                    # #9：逆向删除 organize 新建目录，仅当仍为空壳；
                    # 非空（用户放入了新文件/搬回未完成）→ 该项失败并明示原因
                    if p.is_dir():
                        try:
                            p.rmdir()
                        except OSError as exc:
                            raise OSError(
                                f"目标侧新建目录非空，未删除（请人工确认后手动清理）: {exc}"
                            ) from exc
                        _audit(org_cfg, action="ROLLBACK_RMDIR", source="",
                               target=str(p), result="SUCCESS",
                               operation_id=operation_id)
                    # 目录已不存在 → 幂等成功（无需审计）
                else:
                    ensure_dir(p)  # 重建原空目录（fnOS 新建目录需 chmod 补写位）
                    _audit(org_cfg, action="ROLLBACK_MKDIR", source=str(p),
                           target="", result="SUCCESS", operation_id=operation_id)
                it["status"] = "rolled_back"
                rolled.append(it)
            except OSError as exc:
                it["status"] = "failed"
                it["reason"] = str(exc)
                failed.append(it)
                errors.append(f"{it['path']}: {exc}")
                _audit(org_cfg, action=f"ROLLBACK_{it['type'].upper()}", source="",
                       target=str(p), result="FAILED", error=str(exc),
                       operation_id=operation_id)
        if not failed:
            status = "rolled_back"
        elif not rolled:
            status = "failed"
        else:
            status = "partially_rolled_back"  # §47/O9
        return status, {
            "rolled_back_items": rolled,
            "failed_items": failed,
            "errors": errors,
        }

    status, executed = _overall(items, dry_run=dry_run, execute=execute)

    return {
        "ok": True,
        "operation_id": operation_id,
        "dry_run": bool(dry_run),
        "status": status,
        "reason": "" if status not in (
            "permission_denied", "rollback_conflict", "not_found",
        ) else _dominant_reason(items, status),
        "items": items,
        "rolled_back_items": [
            {"type": i["type"], "path": i["path"], "restore_to": i["restore_to"]}
            for i in executed.get("rolled_back_items", [])
        ],
        "failed_items": [
            {"type": i["type"], "path": i["path"], "restore_to": i["restore_to"],
             "error": i.get("reason", "")}
            for i in executed.get("failed_items", [])
        ],
        "errors": executed.get("errors", []),
        "checks": [
            "operation_id_exists", "action_supported", "source_target_exists",
            "path_allowed", "conflict", "permission", "size", "mtime",
        ],
        "hint": _hint(status),
    }


def _dominant_reason(items: list[dict], status: str) -> str:
    for i in items:
        if i["status"] == status:
            return i.get("reason", "")
    return ""


def _hint(status: str) -> str:
    if status == "rollback_ready":
        return "检查通过（rollback_ready）；保持参数不变、dry_run=false 才执行实际回滚"
    if status == "rolled_back":
        return "回滚已执行完成"
    if status == "rollback_conflict":
        return "存在冲突，已在物理执行前整体中止（§43）；请处理冲突后重新检查"
    if status == "rollback_unsupported":
        return "该操作不支持回滚（legacy 记录或含永久删除）"
    if status == "permission_denied":
        return "当前权限不允许回滚：移动/改名需 enable_organize，删除新建文件需 enable_delete"
    if status == "not_found":
        return "operation_id 不存在或没有已执行动作；不得猜测其他操作"
    if status == "partially_rolled_back":
        return "部分回滚已完成，部分失败；见 rolled_back_items / failed_items / errors"
    return ""
