#!/usr/bin/env python3
"""生成插件安装包的 core_manifest.json（打包/发版前必跑）。

记录内容：
- 内核版本（事实来源 nas-octop-plugin/nas_mcp/__init__.py）；
- ``nas_mcp/*.py`` 的逐个 sha256 与汇总 digest；
- 三形态（插件 / nas-app / nas-mcp）工具清单与插件专属工具。

``scripts/check_versions.py`` 与插件运行时 ``nas_doctor`` 都会对照该清单校验；
改动内核或工具清单后必须重新运行本脚本，否则 CI 会失败。

用法：
    python scripts/build_core_manifest.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_versions as cv  # noqa: E402


def build() -> dict:
    version = cv.source_version()
    files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((cv.PLUGIN / "nas_mcp").glob("*.py"))
    }
    digest = hashlib.sha256(
        "".join(f"{name}:{sha}\n" for name, sha in sorted(files.items())).encode("utf-8")
    ).hexdigest()
    plugin_tools = cv._plugin_tools(cv._parse(cv.PLUGIN_MAIN))
    app_tools = cv._decorated_tools(cv._parse(cv.APP_TOOLKIT), "tool")
    mcp_tools = cv._decorated_tools(cv._parse(cv.MCP_SERVER), "tool", use_func_name=True)
    return {
        "schema": 1,
        "plugin": "nas-media",
        "version": version,
        "generated_by": "scripts/build_core_manifest.py",
        "nas_mcp": {
            "file_count": len(files),
            "digest": digest,
            "files": files,
        },
        "tools": {
            "plugin": sorted(plugin_tools),
            "plugin_only": sorted(plugin_tools - app_tools),
            "nas_app": sorted(app_tools),
            "nas_mcp": sorted(mcp_tools),
        },
    }


def main() -> int:
    manifest = build()
    cv.CORE_MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tools = manifest["tools"]
    print(
        f"已生成 {cv.CORE_MANIFEST.relative_to(cv.ROOT)}：内核 {manifest['version']}，"
        f"{manifest['nas_mcp']['file_count']} 个文件，"
        f"工具 plugin={len(tools['plugin'])} / nas_app={len(tools['nas_app'])} / "
        f"nas_mcp={len(tools['nas_mcp'])}，插件专属={len(tools['plugin_only'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
