#!/usr/bin/env python3
"""插件优先打包入口：只产出 Octop 插件 ZIP，并归档历史包。

- 事实来源版本：nas-octop-plugin/nas_mcp/__init__.py 的 __version__
- 产物：nas-octop-plugin/dist/nas-media-<version>.zip + 根目录 nas-octop-plugin.zip
- 历史包：dist 内只保留最近 KEEP 个，其余移入 dist/archive/
- nas-app 的 ZIP / fpk 由 nas-app/packaging/fnos/build-fpk.sh 负责，本脚本不再产出
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_DIR = ROOT / "nas-octop-plugin"
DIST_DIR = PLUGIN_DIR / "dist"
ARCHIVE_DIR = DIST_DIR / "archive"
KEEP = 5

artifact_env = os.environ.get("ARTIFACT_DIR")
ARTIFACT_DIR = Path(artifact_env) if artifact_env else ROOT / "dist"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def get_version() -> str:
    init_path = PLUGIN_DIR / "nas_mcp" / "__init__.py"
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', init_path.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"无法从 {init_path} 解析 __version__")
    return match.group(1)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def archive_old_packages(keep: int = KEEP) -> None:
    """dist 下仅保留最近 keep 个版本包，其余归档到 dist/archive。"""
    packages = sorted(
        DIST_DIR.glob("nas-media-*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if len(packages) <= keep:
        return
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    for old in packages[keep:]:
        target = ARCHIVE_DIR / old.name
        if target.exists():
            target.unlink()
        shutil.move(str(old), str(target))
        print(f"Archived {old.name} -> {target.relative_to(ROOT)}")


def zip_octop_plugin() -> Path:
    # 版本与三副本哈希先校验，避免打出带漂移的包
    check = ROOT / "scripts" / "check_versions.py"
    if check.is_file():
        result = subprocess.run([sys.executable, str(check)], cwd=str(ROOT))
        if result.returncode != 0:
            raise SystemExit("版本/三副本校验失败，终止打包")

    version = get_version()
    if version != _plugin_yaml_version():
        raise SystemExit(
            f"版本不一致：__init__={version} plugin.yaml={_plugin_yaml_version()}；"
            "先跑 python scripts/check_versions.py"
        )

    out_root_zip = ROOT / "nas-octop-plugin.zip"
    out_dist_zip = DIST_DIR / f"nas-media-{version}.zip"
    DIST_DIR.mkdir(exist_ok=True)

    exclude_dirs = {".git", "__pycache__", "tests", "dist", "logs", ".pytest_cache", "archive"}
    exclude_exts = {".pyc", ".pyo"}

    def _skip_file(name: str) -> bool:
        """真实配置与其备份一律不打包；插件目录只保留 config.example.yaml。"""
        if Path(name).suffix in exclude_exts or name == ".DS_Store":
            return True
        return name in {"config.yaml", "config.yml"} or name.startswith(
            ("config.yaml.", "config.yml.")
        )

    root_copy_ok = True
    for out_zip, required in ((out_dist_zip, True), (out_root_zip, False)):
        try:
            if out_zip.exists():
                out_zip.unlink()
        except OSError as exc:
            if required:
                raise
            # 根目录副本只是便利产物：被 IDE/资源管理器占用时不应阻断 dist 打包
            root_copy_ok = False
            print(
                f"[WARN] {out_zip.name} 被其他程序占用（{exc}），跳过根目录副本；"
                "关掉占用后重跑可恢复（dist 包不受影响）"
            )
            continue
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(PLUGIN_DIR):
                dirs[:] = [d for d in dirs if d not in exclude_dirs]
                rel_root = Path(root).relative_to(PLUGIN_DIR)
                for f in files:
                    if _skip_file(f):
                        continue
                    file_path = Path(root) / f
                    z.write(file_path, str(rel_root / f).replace("\\", "/"))

        # 兜底：包内绝不允许出现真实配置或备份
        with zipfile.ZipFile(out_zip) as z:
            leaked = [
                name
                for name in z.namelist()
                if Path(name).name in {"config.yaml", "config.yml"}
                or Path(name).name.startswith(("config.yaml.", "config.yml."))
            ]
        if leaked:
            raise SystemExit(f"打包内容含真实配置，已终止：{leaked}")

    print(f"Generated {out_dist_zip} ({out_dist_zip.stat().st_size} bytes)")
    print(f"SHA256 {sha256_of(out_dist_zip)}")

    if ARTIFACT_DIR != DIST_DIR:
        shutil.copy2(out_root_zip if root_copy_ok else out_dist_zip, ARTIFACT_DIR / "nas-octop-plugin.zip")
        artifact_plugin_dist = ARTIFACT_DIR / "nas-octop-plugin" / "dist"
        artifact_plugin_dist.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_dist_zip, artifact_plugin_dist / out_dist_zip.name)
        print("Copied plugin zips to artifact directory")

    archive_old_packages()
    return out_dist_zip


def _plugin_yaml_version() -> str:
    text = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)\s*$", text, re.M)
    if not match:
        raise SystemExit("plugin.yaml 缺少 version")
    return match.group(1)


if __name__ == "__main__":
    zip_path = zip_octop_plugin()
    if not zip_path.is_file():
        sys.exit("打包失败")
