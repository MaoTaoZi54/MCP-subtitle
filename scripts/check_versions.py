#!/usr/bin/env python3
"""版本单一源校验 + 三副本内核哈希 + 三形态工具清单/默认值断言。

事实来源：nas-octop-plugin/nas_mcp/__init__.py 的 __version__。
本脚本只检查、不修改；CI 与打包前都应先跑它。

三形态的边界（允许长期存在、不应硬拉齐）：
- 插件比 nas-app 多 ``nas_save_config``（插件在 Octop 里没有 UI，只能靠工具写配置）；
- nas-app 有 Web UI、任务队列/进度/取消，插件是同步调用；
- nas-mcp 是 stdio 形态，业务工具与 app 完全一致，仅字幕两工具用 subtitle_* 命名。
除此之外的“同名工具缺失”或“关键默认值不同”都会让本脚本失败。

用法：
    python scripts/check_versions.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "nas-octop-plugin"
APP = ROOT / "nas-app"
MCP = ROOT / "nas-mcp"

SOURCE = PLUGIN / "nas_mcp" / "__init__.py"
PLUGIN_MAIN = PLUGIN / "main.py"
APP_TOOLKIT = APP / "nas_app" / "toolkit.py"
MCP_SERVER = MCP / "src" / "nas_mcp" / "server.py"
# 打包时由 scripts/build_core_manifest.py 生成，随安装包分发
CORE_MANIFEST = PLUGIN / "core_manifest.json"

# 形态差异白名单：这些不是“没同步”，而是装配形态决定的能力边界。
# 插件侧配置写入、队列管理与日志读取只存在于 Octop 插件装配层。
PLUGIN_ONLY_TOOLS = {
    "nas_save_config",
    "nas_job_list",
    "nas_job_status",
    "nas_job_cancel",
    "nas_doctor",
    "nas_get_logs",
}
# nas-mcp 已补齐全部业务工具；保留空集合便于未来按形态收窄时继续复用比对逻辑。
MCP_MISSING_TOOLS: set[str] = set()
MCP_RENAMED_TOOLS = {
    "subhd_search": "subtitle_search",
    "subhd_download": "subtitle_download",
}

# 同名工具的关键默认值：三个形态必须一致，防止“插件默认不动文件、某个形态默认动文件”。
KEY_DEFAULTS: dict[str, dict[str, object]] = {
    "media_mux_subtitles": {
        "dry_run": True,
        "delete_external": False,
        "replace_original": False,
        "force": False,
        "limit": 50,
    },
    "media_cleanup": {"dry_run": True},
    "organize_media": {
        "dry_run": True,
        "preexisting_empty_scope": "library_root",
        "language_split": False,
    },
    "media_fill_subtitles": {
        "dry_run": True,
        "lang": "zh-Hans",
        "ext": "srt",
        "check_embedded": True,
    },
    "subhd_download": {"lang": "zh-Hans", "ext": "srt", "overwrite": False},
    "subtitle_download": {"lang": "zh-Hans", "ext": "srt", "overwrite": False},
}


def source_version() -> str:
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', SOURCE.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"无法从 {SOURCE} 解析 __version__")
    return match.group(1)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse(path: Path) -> ast.AST:
    return ast.parse(read(path), filename=str(path))


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _plugin_tools(tree: ast.AST) -> set[str]:
    """插件在 setup 里用 ctx.tool(...) + (name, fn) 元组循环注册工具。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "tool" and node.args:
                value = _const_str(node.args[0])
                if value:
                    names.add(value)
        if isinstance(node, ast.For) and isinstance(node.target, ast.Tuple):
            targets = [n.id for n in node.target.elts if isinstance(n, ast.Name)]
            if targets[:2] == ["_name", "_fn"] and isinstance(node.iter, ast.Tuple):
                for item in node.iter.elts:
                    if isinstance(item, ast.Tuple) and item.elts:
                        value = _const_str(item.elts[0])
                        if value:
                            names.add(value)
    return names


def _decorated_tools(tree: ast.AST, attr: str, use_func_name: bool = False) -> set[str]:
    """nas-app 用 @self.tool("name", ...)，nas-mcp 用 @mcp.tool() + 函数名。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (isinstance(func, ast.Attribute) and func.attr == attr):
                continue
            if use_func_name:
                names.add(node.name)
            elif decorator.args:
                value = _const_str(decorator.args[0])
                if value:
                    names.add(value)
    return names


def _function_defaults(tree: ast.AST) -> dict[str, dict[str, object]]:
    """提取每个函数的参数默认值，用于跨形态比对。"""
    out: dict[str, dict[str, object]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        spec = node.args
        positional = [*spec.posonlyargs, *spec.args]
        defaults: list[object] = [None] * (len(positional) - len(spec.defaults)) + list(spec.defaults)
        values: dict[str, object] = {}
        for arg, default in zip(positional, defaults):
            if default is not None:
                try:
                    values[arg.arg] = ast.literal_eval(default)
                except (ValueError, SyntaxError):
                    pass
        for arg, default in zip(spec.kwonlyargs, spec.kw_defaults):
            if default is not None:
                try:
                    values[arg.arg] = ast.literal_eval(default)
                except (ValueError, SyntaxError):
                    pass
        out[node.name] = values
    return out


def check_tool_parity() -> list[str]:
    """工具清单 + 关键默认值一致性；返回失败项列表。"""
    failures: list[str] = []
    plugin_tools = _plugin_tools(_parse(PLUGIN_MAIN))
    app_tools = _decorated_tools(_parse(APP_TOOLKIT), "tool")
    mcp_tools = _decorated_tools(_parse(MCP_SERVER), "tool", use_func_name=True)

    if plugin_tools - PLUGIN_ONLY_TOOLS != app_tools:
        failures.append(
            "插件与 nas-app 工具集不一致："
            f"仅插件={sorted(plugin_tools - PLUGIN_ONLY_TOOLS - app_tools)}，"
            f"仅 app={sorted(app_tools - plugin_tools)}"
        )
    else:
        print(f"[PASS] 插件/ nas-app 工具集对齐：{len(app_tools)} 个业务工具（插件多 nas_save_config）")

    expected_mcp = (
        plugin_tools - PLUGIN_ONLY_TOOLS - MCP_MISSING_TOOLS - set(MCP_RENAMED_TOOLS)
    ) | set(MCP_RENAMED_TOOLS.values())
    if mcp_tools != expected_mcp:
        failures.append(
            "nas-mcp 工具集偏离约定："
            f"多={sorted(mcp_tools - expected_mcp)}，"
            f"缺={sorted(expected_mcp - mcp_tools)}"
        )
    else:
        print(f"[PASS] nas-mcp 工具集符合约定：{len(mcp_tools)} 个（业务工具齐全 + 2 个 subtitle_* 改名）")

    default_maps = {
        "插件": (plugin_tools, _function_defaults(_parse(PLUGIN_MAIN))),
        "nas-app": (app_tools, _function_defaults(_parse(APP_TOOLKIT))),
        "nas-mcp": (mcp_tools, _function_defaults(_parse(MCP_SERVER))),
    }
    mismatches: list[str] = []
    for tool, expected in KEY_DEFAULTS.items():
        for label, (registered, defaults) in default_maps.items():
            if tool not in registered:
                continue
            actual = defaults.get(tool, {})
            for param, value in expected.items():
                if param not in actual:
                    mismatches.append(f"{label}/{tool}: 缺少参数 {param}")
                elif actual[param] != value:
                    mismatches.append(f"{label}/{tool}.{param}={actual[param]!r}，应为 {value!r}")
    if mismatches:
        failures.extend(mismatches)
    else:
        print("[PASS] 三形态关键默认值一致（dry_run/覆盖/替换/分语言/limit）")
    return failures


def check_core_manifest(version: str) -> list[str]:
    """core_manifest.json 必须与源码一致：版本 + nas_mcp 哈希 + 三形态工具清单。"""
    failures: list[str] = []
    if not CORE_MANIFEST.is_file():
        return [
            f"core_manifest.json 不存在：{CORE_MANIFEST}"
            "（请运行 scripts/build_core_manifest.py 生成）"
        ]
    try:
        manifest = json.loads(read(CORE_MANIFEST))
    except ValueError as exc:
        return [f"core_manifest.json 解析失败：{exc}"]
    if not isinstance(manifest, dict):
        return ["core_manifest.json 顶层不是对象"]

    if manifest.get("version") != version:
        failures.append(
            f"core_manifest 版本 {manifest.get('version')!r} 与源码 {version} 不一致"
            "（改动内核后请重新生成）"
        )

    core = manifest.get("nas_mcp")
    recorded = core.get("files") if isinstance(core, dict) else None
    if not isinstance(recorded, dict):
        failures.append("core_manifest 缺少 nas_mcp.files")
        recorded = {}
    actual = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((PLUGIN / "nas_mcp").glob("*.py"))
    }
    if set(recorded) != set(actual):
        failures.append(
            "core_manifest 文件清单不一致："
            f"多={sorted(set(recorded) - set(actual))}，"
            f"缺={sorted(set(actual) - set(recorded))}"
        )
    for name in sorted(set(recorded) & set(actual)):
        if recorded[name] != actual[name]:
            failures.append(f"core_manifest 哈希不一致（源文件被篡改？）: {name}")
    digest = hashlib.sha256(
        "".join(f"{name}:{digest}\n" for name, digest in sorted(actual.items())).encode("utf-8")
    ).hexdigest()
    if core.get("digest") != digest:
        failures.append(
            f"core_manifest 汇总 digest 不一致：{core.get('digest')} != {digest}"
        )

    plugin_tools = _plugin_tools(_parse(PLUGIN_MAIN))
    app_tools = _decorated_tools(_parse(APP_TOOLKIT), "tool")
    mcp_tools = _decorated_tools(_parse(MCP_SERVER), "tool", use_func_name=True)
    expected = {
        "plugin": sorted(plugin_tools),
        "plugin_only": sorted(plugin_tools - app_tools),
        "nas_app": sorted(app_tools),
        "nas_mcp": sorted(mcp_tools),
    }
    tools = manifest.get("tools")
    if not isinstance(tools, dict):
        failures.append("core_manifest 缺少 tools")
        tools = {}
    for key, names in expected.items():
        recorded_names = tools.get(key)
        if not isinstance(recorded_names, list):
            failures.append(f"core_manifest 缺少 tools.{key}")
        elif sorted(recorded_names) != names:
            failures.append(f"core_manifest tools.{key} 与源码不一致")
    return failures


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

    # 4. 三形态工具清单 + 关键默认值（防止新增能力只落一个形态）
    try:
        failures.extend(check_tool_parity())
    except Exception as exc:  # noqa: BLE001 - 解析失败本身就是不同步
        failures.append(f"三形态工具清单校验异常: {exc}")

    # 5. 打包清单 core_manifest.json 与源码一致（篡改/漏更新必须失败）
    try:
        manifest_failures = check_core_manifest(version)
    except Exception as exc:  # noqa: BLE001
        manifest_failures = [f"core_manifest 校验异常: {exc}"]
    if manifest_failures:
        failures.extend(manifest_failures)
    else:
        print("[PASS] core_manifest.json 与源码一致（版本/sha256/三形态工具清单）")

    print()
    if failures:
        for item in failures:
            print(f"[FAIL] {item}", file=sys.stderr)
        return 1
    print("版本与三副本哈希校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
