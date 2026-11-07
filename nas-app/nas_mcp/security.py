"""路径安全守卫：所有工具的每一个路径参数都必须先经过 PathGuard 检查。

规则：
1. 请求路径 resolve()（跟随已存在符号链接）后必须落在 allowed_paths 白名单内；
2. 命中 exclude_paths（排除目录，如网盘挂载）时无条件拒绝，优先级高于白名单；
3. 目录遍历过程中，指向白名单外或排除目录的符号链接会被剪枝/隐藏；
4. 白名单/排除条目支持 glob 模式（含 * ? [ 的条目）：模式匹配"路径自身或任一
   祖先"，用于适配 fnOS 加盘（/vol[1-9]*/1000）与网盘挂载 hash 变化（/vol0*）；
   无特殊字符的条目按字面量前缀匹配，两类可混用。
"""

from __future__ import annotations

import fnmatch
import glob as glob_module
import logging
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger("nas_mcp.security")

_GLOB_CHARS = frozenset("*?[")


def is_pattern(s: str) -> bool:
    """条目是否为 glob 模式（含 fnmatch 特殊字符）。"""
    return any(c in _GLOB_CHARS for c in s)


def matches_pattern(path: Path, pattern: str) -> bool:
    """路径自身或任一祖先命中模式（posix 风格、大小写敏感）。

    例：/vol3/1000/movies 的祖先 /vol3/1000 命中 /vol[1-9]*/1000 → True；
        /vol02/1000-1-x 的祖先 /vol02 命中 /vol0* → True。
    """
    p = path
    while True:
        if fnmatch.fnmatchcase(p.as_posix(), pattern):
            return True
        parent = p.parent
        if parent == p:
            return False
        p = parent


def expand_roots(roots: Iterable[str]) -> tuple[str, ...]:
    """把含 glob 模式的搜索范围条目展开为当前实际存在的目录（去重、排序）。

    模式条目每次调用时重新展开——fnOS 加盘（/vol3/1000）后无需重启即可被扫描；
    字面量条目原样保留（不存在时的报错交给后续 is_dir 逻辑）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for r in roots:
        if not is_pattern(r):
            if r not in seen:
                seen.add(r)
                out.append(r)
            continue
        for hit in sorted(glob_module.glob(r)):
            hp = Path(hit).resolve()
            if hp.is_dir():
                key = hp.as_posix()
                if key not in seen:
                    seen.add(key)
                    out.append(key)
    return tuple(out)


class AccessDenied(Exception):
    """请求路径被安全策略拒绝。"""

    def __init__(self, raw_path: str, resolved: Path, reason: str):
        self.raw_path = raw_path
        self.resolved = resolved
        self.reason = reason
        super().__init__(f"{raw_path!r} -> {reason}")


class InvalidArguments(ValueError):
    """工具参数非法（调用方的问题）。

    与内部实现意外抛出的裸 ValueError 区分开：只有参数校验失败才用这个异常，
    server 层据此映射为 invalid_arguments；裸 ValueError 视为未预期异常。
    """


def is_within(child: Path, parent: Path) -> bool:
    """child 是否等于 parent 或位于 parent 之下（双方应为 resolve() 后的路径）。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_path(raw: str) -> Path:
    """把任意输入解析为绝对路径；会跟随已存在的符号链接。"""
    return Path(raw).resolve()


class PathGuard:
    """白名单 + 排除目录的统一检查器（条目支持字面量与 glob 模式混用）。"""

    def __init__(self, allowed_paths: Iterable[str], exclude_paths: Iterable[str]):
        self.allowed_literal = [Path(p) for p in allowed_paths if not is_pattern(p)]
        self.allowed_patterns = [p for p in allowed_paths if is_pattern(p)]
        self.excluded_literal = [Path(p) for p in exclude_paths if not is_pattern(p)]
        self.excluded_patterns = [p for p in exclude_paths if is_pattern(p)]

    def _is_excluded(self, resolved: Path) -> bool:
        if any(is_within(resolved, ex) for ex in self.excluded_literal):
            return True
        return any(matches_pattern(resolved, pat) for pat in self.excluded_patterns)

    def _is_allowed(self, resolved: Path) -> bool:
        if any(is_within(resolved, root) for root in self.allowed_literal):
            return True
        return any(matches_pattern(resolved, pat) for pat in self.allowed_patterns)

    def check(self, raw: str) -> Path:
        """检查用户/Agent 传入的路径，通过则返回 resolve 后的 Path，否则抛 AccessDenied。"""
        resolved = resolve_path(raw)
        if self._is_excluded(resolved):
            logger.warning("拒绝访问（排除目录）: %s -> %s", raw, resolved)
            raise AccessDenied(raw, resolved, "路径位于排除目录（exclude_paths）内")
        if self._is_allowed(resolved):
            return resolved
        logger.warning("拒绝访问（白名单外）: %s -> %s", raw, resolved)
        raise AccessDenied(raw, resolved, "路径不在 allowed_paths 白名单内")

    def is_allowed_resolved(self, resolved: Path) -> bool:
        """判断一个（已解析的）路径是否可列出/可进入；用于遍历时的剪枝与隐藏。"""
        return not self._is_excluded(resolved) and self._is_allowed(resolved)
