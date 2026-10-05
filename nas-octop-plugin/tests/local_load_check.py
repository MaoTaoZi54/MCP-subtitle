# -*- coding: utf-8 -*-
"""插件本地加载验证（无需 Octop / langchain）。

模拟 octop-harness 的加载方式（spec_from_file_location + FakeCtx），验证：
1. main:setup 可加载、注册 15 个工具、docstring/config_fields 完整；
2. 工具函数参数全部带类型注解（LangChain StructuredTool 生成 schema 的前提）；
3. 未配置 → not_configured；nas_save_config 写入 → 即时生效；
4. 真实文件工具（list/find/disk）在临时白名单内可用、白名单外被拒。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = [
    "nas_save_config",
    "nas_get_config",
    "nas_list_directory",
    "nas_find_files",
    "nas_disk_usage",
    "nas_scan_media",
    "nas_check_subtitle",
    "nas_classify_region",
    "media_quality_analyze",
    "duplicate_media_detect",
    "media_library_audit",
    "subhd_search",
    "subhd_download",
    "resolve_media",
    "jackett_search",
]


class FakeCtx:
    def __init__(self) -> None:
        self.manifest_id = "nas-media"
        self.tools: list[dict] = []

    def tool(self, name, fn, *, description="", config_fields=None):
        self.tools.append(
            {"name": name, "fn": fn, "description": description, "config_fields": config_fields or []}
        )


def load_plugin():
    module_name = "harness_plugin_nas_media"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_DIR / "main.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    module.__package__ = module_name
    module.__path__ = [str(PLUGIN_DIR)]
    spec.loader.exec_module(module)
    ctx = FakeCtx()
    module.setup(ctx)
    return module, ctx


def call(tools, name, **kwargs):
    fn = next(t["fn"] for t in tools if t["name"] == name)
    return json.loads(fn(**kwargs))


def main() -> None:
    failures: list[str] = []

    def check(cond, msg):
        print(("[PASS] " if cond else "[FAIL] ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory(prefix="nasplugin_") as tmp:
        tmp = Path(tmp)
        media = tmp / "vol1" / "1000"
        (media / "Movies").mkdir(parents=True)
        (media / "Movies" / "Movie.A.2020.1080p.mkv").write_bytes(b"fake")
        (media / "IMG_1234.mp4").write_bytes(b"fake")
        outside = tmp / "outside"
        outside.mkdir()
        (outside / "secret.mkv").write_bytes(b"fake")

        module, ctx = load_plugin()

        # 1. 工具注册
        names = [t["name"] for t in ctx.tools]
        check(names == EXPECTED_TOOLS, f"注册 15 个工具且顺序正确（实际 {len(names)} 个）")

        # 2. schema 前提：全部参数有注解 + docstring 非空
        bad: list[str] = []
        for t in ctx.tools:
            if not t["description"]:
                bad.append(f"{t['name']}: 缺 docstring")
            sig = inspect.signature(t["fn"])
            for pname, p in sig.parameters.items():
                if p.annotation is inspect.Parameter.empty:
                    bad.append(f"{t['name']}.{pname}: 缺类型注解")
        check(not bad, "全部工具参数带类型注解且 docstring 非空" + (f"；问题: {bad}" if bad else ""))

        # 3. nas_save_config 的 config_fields
        save = next(t for t in ctx.tools if t["name"] == "nas_save_config")
        field_names = [f["name"] for f in save["config_fields"]]
        check(
            field_names
            == [
                "allowed_paths", "media_roots", "exclude_paths", "enable_write",
                "tmdb_enabled", "tmdb_api_key", "jackett_enabled", "jackett_url",
                "jackett_api_key", "subhd_enabled",
            ],
            "nas_save_config 声明 10 个配置表单字段",
        )

        # 4. 未配置 → not_configured
        module.CONFIG_PATH = tmp / "config.yaml"
        module._cfg_cache["cfg"] = None
        r = call(ctx.tools, "nas_get_config")
        check(not r["ok"] and r["error"]["code"] == "not_configured", "未配置时返回 not_configured 引导错误")

        # 5. 保存配置（对话式传参，多路径逗号+换行混合）
        r = call(
            ctx.tools,
            "nas_save_config",
            allowed_paths=f"{media}\n{media}",
            exclude_paths=f"{outside}, ",
            tmdb_enabled="false",
            jackett_enabled="false",
            subhd_enabled="false",
            enable_write="是",
        )
        check(r["ok"], f"nas_save_config 写入成功（{r.get('error') if not r['ok'] else ''}）")
        if r["ok"]:
            check(r["enable_write"] is True, "布尔解析支持中文'是'")
            check(r["allowed_paths"] == [str(media)], "白名单多值换行解析并去空条")
            check((tmp / "config.yaml").is_file(), "config.yaml 落盘到插件配置路径")
            check(r["tmdb"]["api_key_configured"] is False, "密钥状态只回显布尔不落日志")

        # 6. 配置即时生效
        r = call(ctx.tools, "nas_get_config")
        check(r["ok"] and r["plugin"] == "nas-media" and r["media_roots_expanded"], "配置即时生效且 glob 展开可用")

        # 7. 真实文件工具
        r = call(ctx.tools, "nas_list_directory", path=str(media))
        check(r["ok"] and any(e["name"] == "Movies" for e in r["entries"]), "nas_list_directory 列目录成功")

        r = call(ctx.tools, "nas_find_files", category="video")
        names_found = [Path(x["path"]).name for x in r.get("matches", [])]
        check(
            r["ok"] and "Movie.A.2020.1080p.mkv" in names_found,
            f"nas_find_files 找到白名单内视频（{names_found}）",
        )
        check(all("IMG_" not in n for n in names_found), "文件名跳过模式生效（IMG_ 被隐藏）")

        r = call(ctx.tools, "nas_disk_usage")
        check(r["ok"] and len(r["filesystems"]) >= 1, "nas_disk_usage 返回文件系统信息")

        # 8. 白名单外拒绝
        r = call(ctx.tools, "nas_list_directory", path=str(outside))
        check(not r["ok"] and r["error"]["code"] == "access_denied", "白名单/排除目录外路径被拒绝")

        # 9. 参数校验（未预期模块不崩溃）
        r = call(ctx.tools, "nas_find_files")
        check(not r["ok"] and r["error"]["code"] == "invalid_arguments", "无过滤条件 → invalid_arguments")

        # 10. 空密钥保存不覆盖已有密钥
        cfg_file = tmp / "config.yaml"
        cfg_file.write_text(
            cfg_file.read_text(encoding="utf-8").replace(
                "tmdb:\n  enabled: false",
                "tmdb:\n  enabled: false\n  api_key: deadbeef",
                1,
            ),
            encoding="utf-8",
        )
        module._cfg_cache["cfg"] = None
        r = call(ctx.tools, "nas_save_config", allowed_paths=str(media), subhd_enabled="false")
        check(r["ok"] and r["tmdb"]["api_key_configured"] is True, "留空密钥不覆盖既有值（增量合并）")

    print()
    if failures:
        print(f"失败 {len(failures)} 项")
        sys.exit(1)
    print("全部插件加载验证通过")


if __name__ == "__main__":
    main()
