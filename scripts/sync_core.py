#!/usr/bin/env python3
"""跨平台核心同步脚本（跨平台替代 sync-core.sh）。

将事实来源 nas-octop-plugin/nas_mcp 同步到：
1. nas-app/nas_mcp
2. nas-mcp/src/nas_mcp
并保持各端专属启动文件（如 __main__.py / logging_setup.py）不被破坏。
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "nas-octop-plugin" / "nas_mcp"
DST_APP = ROOT / "nas-app" / "nas_mcp"
DST_MCP = ROOT / "nas-mcp" / "src" / "nas_mcp"

def sync_all():
    if not SRC.is_dir():
        print(f"Error: 来源目录不存在: {SRC}", file=sys.stderr)
        sys.exit(1)

    py_files = sorted(SRC.glob("*.py"))
    print(f"Found {len(py_files)} core modules in {SRC.relative_to(ROOT)}")

    # 1. Sync to nas-app
    DST_APP.mkdir(parents=True, exist_ok=True)
    for f in py_files:
        shutil.copy2(f, DST_APP / f.name)
    # clean pycache in DST_APP
    for d in DST_APP.glob("**/__pycache__"):
        shutil.rmtree(d, ignore_errors=True)
    print(f"Synced {len(py_files)} files -> {DST_APP.relative_to(ROOT)}")

    # 2. Sync to nas-mcp
    DST_MCP.mkdir(parents=True, exist_ok=True)
    for f in py_files:
        shutil.copy2(f, DST_MCP / f.name)
    # clean pycache in DST_MCP
    for d in DST_MCP.glob("**/__pycache__"):
        shutil.rmtree(d, ignore_errors=True)
    print(f"Synced {len(py_files)} files -> {DST_MCP.relative_to(ROOT)}")

if __name__ == "__main__":
    sync_all()
