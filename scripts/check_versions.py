#!/usr/bin/env python3
"""版本单一源校验 + 三副本内核哈希断言。

事实来源：nas-octop-plugin/nas_mcp/__init__.py 的 __version__。
本脚本只检查、不修改；CI 与打包前都应先跑它。

用法：
    python scripts/check_versions.py
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "nas-octop-plugin"
APP = ROOT / "nas-app"
MCP = ROOT / "nas-mcp"

SOURCE = PLUGIN / "nas_mcp" / "__init__.py"


def source_version() -> str:
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', SOURCE.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"无法从 {SOURCE} 解析 __version__")
    return match.group(1)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def main() -> int:
    version = source_version()
    failures: list[str] = []

    def expect(path: Path, pattern: str, label: str) -> None:
        if not path.is_file():
            failures.append(f"{label}: 文件不存在 {path}")
            return
        if re.search(pattern.format(version=re.escape(version)), read(path), re.M):
            print(f"[PASS] {label}: {version}")
        else:
            failures.append(f"{label}: 未找到版本 {version}（{path}）")

    print(f"事实来源版本: {version} ({SOURCE.relative_to(ROOT)})")

    # 1. 发布/代码里的版本必须与事实来源一致
    expect(PLUGIN / "plugin.yaml", r"^version:\s*{version}$", "plugin.yaml")
    expect(PLUGIN / "nas_mcp" / "__init__.py", r'^__version__\s*=\s*"{version}"$', "插件内核 __init__")
    expect(APP / "nas_mcp" / "__init__.py", r'^__version__\s*=\s*"{version}"$', "nas-app 内核 __init__")
    expect(MCP / "src" / "nas_mcp" / "__init__.py", r'^__version__\s*=\s*"{version}"$', "nas-mcp 内核 __init__")
    expect(APP / "nas_app" / "__init__.py", r'^__version__\s*=\s*"{version}"$', "nas_app __init__")
    expect(APP / "pyproject.toml", r'^version\s*=\s*"{version}"$', "nas-app pyproject")
    expect(MCP / "pyproject.toml", r'^version\s*=\s*"{version}"$', "nas-mcp pyproject")
    expect(APP / "packaging" / "fnos" / "manifest", r"^version={version}$", "fnos manifest")
    expect(APP / "tests" / "test_fpk_package.py", r'"{version}"', "fpk 测试版本断言")

    # 2. 文档与模板必须同步（只断言当前版本出现，历史版本记录不在此列）
    expect(PLUGIN / "config.example.yaml", r"与内核 {version}", "config.example.yaml 对齐声明")
    expect(PLUGIN / "README.md", r"v{version}", "插件 README")
    expect(PLUGIN / "README.md", r"nas-media-{version}\.zip", "插件 README 安装包名")
    expect(ROOT / "README.md", r"{version}", "根 README")
    expect(APP / "README.md", r"{version}", "nas-app README")
    expect(MCP / "README.md", r"{version}", "nas-mcp README")

    # 3. 三副本内核：文件集合一致 + 每个 .py 哈希一致
    copies = {
        "nas-octop-plugin": PLUGIN / "nas_mcp",
        "nas-app": APP / "nas_mcp",
        "nas-mcp": MCP / "src" / "nas_mcp",
    }
    names = {label: {p.name for p in d.glob("*.py")} for label, d in copies.items()}
    # nas-mcp 多了 stdio 启动入口 __main__.py 与独立进程日志 logging_setup.py
    # （装配层专属文件，不参与三副本内核同步）
    ALLOWED_EXTRA = {"nas-mcp": {"__main__.py", "logging_setup.py"}}
    base = names["nas-octop-plugin"]
    ok = base == names["nas-app"] and names["nas-mcp"] - base == ALLOWED_EXTRA["nas-mcp"]
    if not ok:
        failures.append(f"三副本 .py 文件集合不一致: { {k: sorted(v)[:5] for k, v in names.items()} }")
    else:
        common = sorted(base)
        print(f"[PASS] 三副本文件集合一致：{len(common)} 个内核 .py（nas-mcp 另有 __main__.py/logging_setup.py）")
        for name in common:
            digests = {
                label: hashlib.sha256((d / name).read_bytes()).hexdigest()
                for label, d in copies.items()
            }
            if len(set(digests.values())) != 1:
                failures.append(f"内核文件哈希不一致: {name} -> {digests}")
        print(f"[PASS] 三副本 {len(common)} 个 .py 哈希一致")

    print()
    if failures:
        for item in failures:
            print(f"[FAIL] {item}", file=sys.stderr)
        return 1
    print("版本与三副本哈希校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
