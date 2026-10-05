# -*- coding: utf-8 -*-
"""真实 octop-harness 兼容性验证：load_plugin_dir + build_plugin_tools。

需在装有 octop-harness 的环境运行：
    .sdk/venv/Scripts/python.exe tests/harness_check.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from octop_harness.plugins import PluginRegistry, build_plugin_tools, load_plugin_dir

PLUGIN_DIR = Path(__file__).resolve().parents[1]

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("[PASS] " if cond else "[FAIL] ") + msg)
    if not cond:
        failures.append(msg)


PluginRegistry.reset()
loaded = load_plugin_dir(PLUGIN_DIR, install_deps=False)
check(loaded.manifest.id == "nas-media", f"harness 加载插件成功（id={loaded.manifest.id} v{loaded.manifest.version}）")
check(len(loaded.tools) == 15, f"setup 注册 15 个工具（实际 {len(loaded.tools)}）")

tools = build_plugin_tools()
check(len(tools) == 15, f"build_plugin_tools 产出 15 个 StructuredTool（实际 {len(tools)}）")

by_name = {t.name: t for t in tools}
for name in [
    "nas_save_config", "nas_get_config", "nas_list_directory", "nas_find_files",
    "nas_disk_usage", "nas_scan_media", "nas_check_subtitle", "nas_classify_region",
    "media_quality_analyze", "duplicate_media_detect", "media_library_audit",
    "subhd_search", "subhd_download", "resolve_media", "jackett_search",
]:
    check(name in by_name, f"工具 {name} 存在")

# schema 推断：可选 list/默认值/bool 都必须被 pydantic 接受
scan = by_name["nas_scan_media"]
fields = scan.args_schema.model_fields
check(
    fields["recursive"].default is True and fields["offset"].default == 0,
    "nas_scan_media schema 默认值正确（recursive=True/offset=0）",
)
q = by_name["media_quality_analyze"]
check(
    "paths" in q.args_schema.model_fields,
    "media_quality_analyze 的 Optional[list[str]] 参数进入 schema",
)
save = by_name["nas_save_config"]
check(len(save.args_schema.model_fields) == 10, "nas_save_config schema 含 10 个配置参数")

# 真实调用链：插件目录无 config.yaml → not_configured（验证异常不穿透到 LangChain）
out = by_name["nas_get_config"].invoke({})
data = json.loads(out)
check(
    not data["ok"] and data["error"]["code"] == "not_configured",
    "StructuredTool 调用返回结构化 JSON 字符串（未配置引导）",
)

# 开关关闭类错误也应结构化而非抛异常
out = by_name["jackett_search"].invoke({"query": "x"})
data = json.loads(out)
check(not data["ok"] and data["error"]["code"] == "not_configured", "未配置时 jackett_search 同样结构化返回")

print()
if failures:
    print(f"失败 {len(failures)} 项")
    sys.exit(1)
print("harness 兼容性验证全部通过")
