"""运行平台识别与权限修复指引（只生成文案，绝不执行任何权限变更）。

为什么需要平台识别：
- fnOS（飞牛）的目录授权不是 POSIX ACL / mode 位，而是应用级授权：
  「系统设置 → 应用设置 → OCTOP → 访问权限 → 添加文件夹（只读/读写）」，
  底层落在 trim_acl（btrfs 的 trimacl 挂载）。因此判断一律以应用访问权限为准，
  修复入口就是上面这个设置页；本模块只生成指引，绝不执行权限变更。
- fnOS 还会裁掉新建文件/目录的写位（实测 uid 985 自建文件与目录均为 0555），
  写入方必须在创建后显式 chmod（见 fs_utils.write_bytes / fs_utils.ensure_dir）。

本模块只回答两件事：当前是什么平台、失败时该让管理员去哪里改。
"""

from __future__ import annotations

import functools
import os
from collections.abc import Iterable
from pathlib import Path

# fnOS 的系统组件目录（Debian 基础上叠加的 trim 运行时）
_FNOS_MARKERS = ("/usr/trim", "/etc/trim")


@functools.lru_cache(maxsize=1)
def is_fnos() -> bool:
    """判定当前宿主是否为 fnOS（飞牛）。结果进程内缓存，平台不会运行时变化。"""
    for marker in _FNOS_MARKERS:
        try:
            if Path(marker).is_dir():
                return True
        except OSError:
            pass
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:  # noqa: PTH123
            return any("trimacl" in line for line in fh)
    except OSError:
        return False


def platform_name() -> str:
    return "fnos" if is_fnos() else "posix"


def _as_dir(path: str | os.PathLike[str] | None) -> str:
    """把「被拒绝的路径」归到目录：存在且是目录原样返回，否则取父目录。"""
    if not path:
        return ""
    p = Path(path)
    try:
        if p.is_dir():
            return str(p)
    except OSError:
        pass
    return str(p.parent)


def permission_hint(path: str | os.PathLike[str] | None = None) -> dict:
    """生成权限失败的修复指引（仅文本，不执行）。

    返回字段：
    - platform: fnos / posix
    - denied_dir: 归到目录后的路径（可能为空）
    - message: 一句话说明失败原因（fnOS 明确指向应用访问权限）
    - steps: 管理员按顺序执行的页面路径 / 命令
    - alternative: 仅非 fnOS 平台给出 setfacl/chmod 备选
    """
    target = _as_dir(path)
    if is_fnos():
        where = f"「{target}」" if target else "该文件夹"
        return {
            "platform": "fnos",
            "denied_dir": target,
            "message": f"该目录未在 OCTOP 应用访问权限中授权：{target or '（未知路径）'}",
            "steps": [
                "系统设置 → 应用设置 → OCTOP → 访问权限",
                f"添加文件夹{where}并设为「读写」",
                "保存后无需重启 Octop；再调用 nas_get_config 确认 write_access 中该根为 writable=true",
            ],
            "note": (
                "fnOS 的目录授权由应用访问权限（trim_acl）决定；按上面三步在"
                "OCTOP 应用页配置即可，保存后即时生效"
            ),
        }
    where = f"「{target}」" if target else "目标目录"
    return {
        "platform": "posix",
        "denied_dir": target,
        "message": f"目标目录不可写：{target or '（未知路径）'}（POSIX 权限不足）",
        "steps": [
            f"确认运行用户对{where}有 w+x 权限（属主/属组优先，避免 o+rwx）",
            f"必要时：sudo setfacl -m u:<运行用户>:rwx {target or '<目录>'}",
        ],
        "alternative": f"sudo setfacl -m u:<运行用户>:rwx {target or '<目录>'}",
        "note": "插件不会自动执行 ACL/权限变更；确认后由管理员手工执行",
    }


def permission_hint_text(paths: str | os.PathLike[str] | Iterable[str] | None = None) -> str:
    """把 permission_hint 压成一行可拼进 reason/错误信息的文案。"""
    if paths is None:
        items: list[str] = []
    elif isinstance(paths, (str, os.PathLike)):
        items = [str(paths)]
    else:
        items = [str(p) for p in paths]
    target = "、".join(dict.fromkeys(items))
    if is_fnos():
        return (
            "该目录未在 OCTOP 应用访问权限中授权"
            + (f"：{target}" if target else "")
            + "；请到 系统设置 → 应用设置 → OCTOP → 访问权限 添加该文件夹并设为「读写」"
        )
    return (
        "目标目录不可写"
        + (f"：{target}" if target else "")
        + "；请让运行用户获得属主/属组写权限（或对必要目录单独 setfacl）"
    )
