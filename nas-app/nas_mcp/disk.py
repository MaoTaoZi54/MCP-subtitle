"""存储工具实现：nas_disk_usage。

不带参数时按 media_roots 去重（同一文件系统只报一次）返回各卷空间；
带 path 时返回该路径所在文件系统的空间。用于判断是否还有空间下载。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .fs_utils import human_size
from .security import PathGuard, expand_roots, is_within


def disk_usage(
    guard: PathGuard,
    media_roots: tuple[str, ...],
    *,
    path: str | None = None,
) -> dict:
    errors: list[dict] = []

    if path is not None:
        targets = [guard.check(path)]
    else:
        targets = [guard.check(r) for r in expand_roots(media_roots)]

    # 按设备号去重：多个媒体目录可能位于同一文件系统
    unique: list[Path] = []
    seen_devs: set[int] = set()
    for t in targets:
        try:
            dev = t.stat().st_dev
        except OSError:
            if path is not None:
                raise FileNotFoundError(f"路径不存在: {path}") from None
            errors.append({"path": str(t), "error": "目录不存在或不可访问，已跳过"})
            continue
        if dev not in seen_devs:
            seen_devs.add(dev)
            unique.append(t)

    filesystems = []
    for target in unique:
        try:
            usage = shutil.disk_usage(str(target))
        except OSError as exc:
            errors.append({"path": str(target), "error": str(exc)})
            continue
        percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
        mount = _mount_info(target)
        filesystems.append(
            {
                "mount_point": mount["mount_point"],
                "device": mount["device"],
                "fstype": mount["fstype"],
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "total_human": human_size(usage.total),
                "used_human": human_size(usage.used),
                "free_human": human_size(usage.free),
                "percent_used": percent,
                "sampled_from": str(target),
            }
        )

    return {"ok": True, "filesystems": filesystems, "errors": errors}


def _mount_info(target: Path) -> dict:
    """尽力找出 target 所在的挂载点/设备/文件系统类型；失败时给出回退值。"""
    info: dict = {"device": None, "fstype": None, "mount_point": str(target)}
    best_parts = 0
    try:
        with open("/proc/self/mounts", encoding="utf-8") as fh:  # noqa: PTH123
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mount_dir, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
                mp = Path(mount_dir)
                if is_within(target, mp) and len(mp.parts) > best_parts:
                    best_parts = len(mp.parts)
                    info.update(device=device, fstype=fstype, mount_point=mount_dir)
    except OSError:
        pass
    if info["device"] is None:
        # 非 Linux（开发机）回退：用盘符
        drive, _ = os.path.splitdrive(str(target))
        if drive:
            info.update(device=drive, fstype="unknown", mount_point=drive + "\\")
    return info
