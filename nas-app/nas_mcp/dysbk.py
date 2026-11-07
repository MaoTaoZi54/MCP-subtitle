# -*- coding: utf-8 -*-
"""网盘资源分享社（bbs.dysbk.com，Flarum 论坛）适配（S6）。

站点事实（2026-10-11 实测）：
- Flarum 论坛，框架原生 REST API 免登录可读，普通 urllib 即可
  （不吃 Cloudflare，无需 curl_cffi）；
- 搜索：GET /api/discussions?filter[q]=<关键词>&page[limit]=N
  → data[].{id, attributes.title}（title 为 HTML，需去标签）；
- 正文（网盘链接在这）：GET /api/posts?filter[discussion]=<id>
  → data[0].attributes.contentHtml；
- 全文搜索有噪声（混入求资源/闲聊帖），只返回标题+discussion id，
  由合并层（federated）做标题归一化去重；
- 无正式 API 承诺：解析容错，字段缺失降级 parse_failed，绝不抛异常。
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import DysbkConfig

logger = logging.getLogger("nas_mcp.dysbk")

DEFAULT_TIMEOUT_SECONDS = 10.0   # 单请求超时 = media_search 方案 A 预算
MAX_SEARCH_LIMIT = 50

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# 网盘 /s/ 分享链接（quark/baidu/xunlei/uc），保留 ?pwd= 提取码参数；
# 其余正文（剧情简介等）一律不返回——避免大段文本灌进上下文。
_PAN_LINK_RE = re.compile(
    r"https?://(?:pan\.quark\.cn|pan\.baidu\.com|pan\.xunlei\.com|drive\.uc\.cn)"
    r"/s/[A-Za-z0-9_-]+(?:\?pwd=[A-Za-z0-9]+)?",
    re.I,
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _err(code: str, message: str, **extra: Any) -> dict:
    """结构化错误（全局约束 1）。"""
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def _get_json(url: str, timeout: float) -> tuple[int | None, Any]:
    """GET 并解析 JSON。返回 (status, payload)；传输错误向上抛。"""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = int(resp.status)
        raw = resp.read()
    if status != 200:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _ParseFailed(f"响应不是合法 JSON（{type(exc).__name__}）") from exc


class _ParseFailed(Exception):
    """响应结构与预期不符（Flarum 升级可能改结构）。"""


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError) and exc.reason is not None:
        return isinstance(exc.reason, (TimeoutError, socket.timeout))
    return False


def _get(url: str, timeout: float, what: str) -> tuple[int | None, Any]:
    """带错误映射的 GET：超时 → dysbk_timeout，解析失败 → parse_failed。"""
    try:
        return _get_json(url, timeout)
    except _ParseFailed as exc:
        return None, _err("parse_failed", f"{what}：{exc}")
    except Exception as exc:  # noqa: BLE001 - 网络边界统一映射
        if _is_timeout(exc):
            return None, _err(
                "dysbk_timeout",
                f"网盘资源分享社请求超时（{timeout:.0f}s 预算），稍后重试",
                what=what,
            )
        logger.warning("dysbk 请求失败（%s）: %s", what, type(exc).__name__)
        return None, _err(
            "unreachable",
            f"网盘资源分享社不可达（{type(exc).__name__}），请稍后重试",
            what=what,
        )


def _strip_title(raw: Any) -> str:
    """Flarum title 是 HTML 片段：去标签 + 实体反转义 + 压缩空白。"""
    text = _TAG_RE.sub(" ", str(raw or ""))
    return _WS_RE.sub(" ", html_lib.unescape(text)).strip()


def search(
    cfg: DysbkConfig,
    query: str,
    limit: int = 20,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """全文搜索讨论帖，返回 [{id, title}]（去标签）+ discussion id。"""
    if not cfg.enabled:
        return _err("disabled", "dysbk.enabled=false，网盘资源分享社已停用")
    query = (query or "").strip()
    if not query:
        return _err("invalid_arguments", "query 不能为空")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return _err("invalid_arguments", f"limit 非法: {limit!r}")
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))

    url = (
        f"{cfg.base_url}/api/discussions?"
        f"filter[q]={urllib.parse.quote(query)}&page[limit]={limit}"
    )
    status, payload = _get(url, timeout, f"搜索 {query!r}")
    if status is None:
        return payload            # 结构化错误 dict
    if status != 200:
        return _err(
            "request_failed",
            f"网盘资源分享社返回 HTTP {status}，稍后重试",
            status_code=status,
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return _err("parse_failed", "搜索响应缺少 data 列表（Flarum 结构可能已变更）")

    results: list[dict] = []
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        attrs = entry.get("attributes") or {}
        try:
            did = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        title = _strip_title(attrs.get("title"))
        if not title:
            continue
        results.append({"id": did, "title": title})
    return {"ok": True, "query": query, "count": len(results), "results": results}


def links(
    cfg: DysbkConfig,
    discussion_id: int,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """取讨论首帖中的网盘直链（只返回链接，正文其余内容不返回）。"""
    if not cfg.enabled:
        return _err("disabled", "dysbk.enabled=false，网盘资源分享社已停用")
    try:
        did = int(discussion_id)
    except (TypeError, ValueError):
        return _err("not_found", f"discussion_id 非法: {discussion_id!r}")
    if did <= 0:
        return _err("not_found", f"discussion_id 非法: {discussion_id!r}")

    url = f"{cfg.base_url}/api/posts?filter[discussion]={did}"
    status, payload = _get(url, timeout, f"discussion_id={did}")
    if status is None:
        return payload
    if status != 200:
        return _err(
            "request_failed",
            f"网盘资源分享社返回 HTTP {status}，稍后重试",
            status_code=status,
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return _err("parse_failed", "正文响应缺少 data 列表（Flarum 结构可能已变更）")
    if not payload["data"]:
        return _err("not_found", f"discussion_id={did} 不存在", discussion_id=did)

    first = payload["data"][0]
    content = ((first or {}).get("attributes") or {}).get("contentHtml")
    if not isinstance(content, str) or not content.strip():
        return _err("parse_failed", "首帖缺少 contentHtml 字段（Flarum 结构可能已变更）")

    # 只保留网盘 /s/ 链接（带 ?pwd= 提取码），去重保序
    seen: set[str] = set()
    found: list[str] = []
    for m in _PAN_LINK_RE.finditer(content):
        link = m.group(0)
        if link not in seen:
            seen.add(link)
            found.append(link)
    return {
        "ok": True,
        "discussion_id": did,
        "count": len(found),
        "links": found,
    }
