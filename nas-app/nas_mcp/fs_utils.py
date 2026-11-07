"""文件系统遍历与条目记录构造的公共工具。

设计要点：
- 单文件异常只记录到 errors 列表，绝不让整个扫描失败（规格 §27）；
- 遍历不跟随符号链接（防止逃逸出白名单与死循环）；
- 所有条目数都有上限（规格 §28 性能要求）。
"""

from __future__ import annotations

import json
import os
import re
import stat as stat_module
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from .security import InvalidArguments, PathGuard

# fnOS 的 trim_acl 会把新建文件/目录的写位裁掉（实测 uid 985 自建文件与目录
# 均为 0555），导致「创建成功、后续再写入却 EACCES」。所有落盘/建目录走下面
# 三个助手：创建后显式 chmod，保证后续 ffmpeg 追加、覆盖下载、向新建目录移动
# 都能继续工作。chmod 失败不阻断主流程（非属主时本来也改不了）。


def ensure_mode(path: Path, mode: int) -> bool:
    """尽力把 path 的权限位设为 mode；失败返回 False（不抛异常）。"""
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """写文件并保证写位：覆盖前先放开属主写位，写完再统一设 mode。

    覆盖已有文件时必须先 chmod——fnOS 上旧文件可能是 0555，直接 "wb" 打开
    会 EACCES；属主是当前进程时 chmod 一定成功，非属主时保持原错误上抛。
    """
    if path.exists():
        ensure_mode(path, mode)
    path.write_bytes(data)
    ensure_mode(path, mode)


def ensure_dir(path: Path, *, mode: int = 0o755) -> None:
    """创建目录（含父级）并保证目录自身可写可进。

    organize 会先建 Season 目录再往里 rename；fnOS 新建目录是 0505，
    不 chmod 会在下一步移动时 EACCES。
    """
    path.mkdir(parents=True, exist_ok=True)
    ensure_mode(path, mode)


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


def normalize_list_arg(value, field: str) -> list | None:
    """列表参数兼容：部分客户端会把列表以 JSON 编码字符串形式传入
    （如 paths='["/a.mkv"]'、extensions='["mkv"]'）。

    MCP SDK 服务端本有 pre_parse_json 预解析，但插件直调等路径绕过了它，
    故能力包入口统一做一次：None 透传；list 透传；str 尝试 json.loads，
    解析结果必须是 list，否则按 invalid_arguments 拒绝。
    """
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise InvalidArguments(
                f"{field} 收到字符串但不是有效的 JSON 列表：{exc}"
            ) from exc
        if not isinstance(parsed, list):
            raise InvalidArguments(
                f"{field} 的 JSON 字符串解析后必须是列表，"
                f"得到 {type(parsed).__name__}"
            )
        return parsed
    raise InvalidArguments(
        f"{field} 必须是列表（或 JSON 列表字符串），得到 {type(value).__name__}"
    )


# ---------------------------------------------------------------- 扫描错误归并（#5）
# 进程级"固有不可读目录"基线：白名单内但运行用户无读权限的目录（fnOS 各应用
# 私有目录）每次全库扫描都会撞上，10 条 Permission denied 逐字重复返回纯属
# token 噪音。首次见到时完整上报并记入基线，之后各工具只报增量 + 计数汇总。
_KNOWN_DENIED: dict[str, str] = {}


def known_denied_paths() -> list[str]:
    """本进程已确认"固有不可读"的路径（供 nas_get_config 一次性披露）。"""
    return sorted(_KNOWN_DENIED)


def reset_known_denied() -> None:
    """清空固有不可读基线（测试/诊断用）。"""
    _KNOWN_DENIED.clear()


def _is_permission_denied(message: str) -> bool:
    low = message.lower()
    return "permission denied" in low or "errno 13" in low


class ErrorSink:
    """扫描错误收集器：结构化计数 + 权限错误增量上报。

    - 权限错误（Errno 13）：首次见到 → 进 errors 并记入进程级基线；
      再次遇到（各工具/各轮扫描重复出现）→ 只计数，不再重复返回；
    - 其他错误：始终上报（受 max_reported 限额，超出计入 over_limit_suppressed）；
    - summary() 返回 errors_summary，让调用方区分"新错误"与"已知固有错误"。
    """

    def __init__(self, max_reported: int):
        self.max_reported = max(0, int(max_reported))
        self.errors: list[dict] = []
        self._total = 0
        self._permission_denied = 0
        self._new_permission_denied = 0
        self._known_suppressed = 0
        self._over_limit = 0
        # T3：权限错误路径样本（≤5 条，含被基线抑制的）——调用方据此区分
        # "库里没有"与"没权限看"，不能因增量抑制而丢证据
        self._denied_sample: list[str] = []

    def add(self, path, message: str) -> None:
        self._total += 1
        denied = _is_permission_denied(message)
        if denied:
            self._permission_denied += 1
            key = str(path)
            if key not in self._denied_sample and len(self._denied_sample) < 5:
                self._denied_sample.append(key)
            if key in _KNOWN_DENIED:
                self._known_suppressed += 1
                return
            _KNOWN_DENIED[key] = message
            self._new_permission_denied += 1
        if len(self.errors) < self.max_reported:
            self.errors.append({"path": str(path), "error": message})
        else:
            self._over_limit += 1

    @property
    def messages(self) -> list[str]:
        """字符串形态的已上报错误（find_media 等旧结构用）。"""
        return [f"{e['path']}: {e['error']}" for e in self.errors]

    def denied_sample(self) -> list[str]:
        """T3：权限错误路径样本（最多 5 条，去重，含被基线抑制的）。"""
        return list(self._denied_sample)

    def summary(self) -> dict:
        return {
            "total": self._total,
            "reported": len(self.errors),
            "permission_denied": self._permission_denied,
            "new_permission_denied": self._new_permission_denied,
            "known_permission_denied_suppressed": self._known_suppressed,
            "over_limit_suppressed": self._over_limit,
        }


def mark_incomplete(result: dict, *, permission_denied: int,
                    denied_sample: list[str], empty: bool) -> dict:
    """T3：按目录扫描工具的权限错误标注（只新增字段，不改已有字段）。

    - permission_denied > 0 且结果为空 → `result_incomplete: true`
      + `denied_paths_sample`（≤5 条）：调用方不能把空结果读成"没有数据"；
    - permission_denied > 0 且结果非空 → 只加 `denied_paths_sample`。
    """
    if permission_denied <= 0:
        return result
    if empty:
        result["result_incomplete"] = True
    if denied_sample:
        result["denied_paths_sample"] = list(denied_sample)[:5]
    return result


def mark_incomplete_from_sink(result: dict, sink: ErrorSink, *, empty: bool) -> dict:
    """mark_incomplete 的 ErrorSink 便捷入口。"""
    return mark_incomplete(
        result,
        permission_denied=sink.summary()["permission_denied"],
        denied_sample=sink.denied_sample(),
        empty=empty,
    )
