"""MCP 端点诊断：列出工具、可选调用一次，验证 Octop 能否直连。

用法：

    py -3 scripts/probe_mcp.py --url http://127.0.0.1:8670/mcp --token <token>
    py -3 scripts/probe_mcp.py --url http://127.0.0.1:8670/mcp --token <token> \
        --call nas_list_directory --args '{"path": "/media"}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def _run(url: str, token: str, tool: str | None, args: dict) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(30, read=300)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(item.name for item in tools.tools)
                print(f"tools={len(names)}")
                for name in names[:10]:
                    print(f"  - {name}")
                if len(names) > 10:
                    print(f"  ... 另有 {len(names) - 10} 个")
                if not tool:
                    return 0
                result = await session.call_tool(tool, args)
                for block in result.content:
                    text = getattr(block, "text", None)
                    if text is not None:
                        print(text)
                return 0 if not result.isError else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 nas-app 的 MCP Streamable HTTP 端点")
    parser.add_argument("--url", default="http://127.0.0.1:8670/mcp")
    parser.add_argument("--token", required=True, help="NAS_APP_TOKEN")
    parser.add_argument("--call", default=None, help="额外调用一个工具")
    parser.add_argument("--args", default="{}", help="工具参数 JSON（默认 {}）")
    opts = parser.parse_args()
    try:
        args = json.loads(opts.args)
    except ValueError as exc:
        print(f"--args 不是合法 JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(args, dict):
        print("--args 必须是 JSON 对象", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(opts.url, opts.token, opts.call, args))
    except Exception as exc:  # noqa: BLE001 - 诊断脚本直接给出原因
        print(f"探测失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
