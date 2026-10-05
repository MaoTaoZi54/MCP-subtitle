"""Jackett 资源搜索（P4：jackett_search）。

设计（规格 §24-§35）：
- Jackett 只作为"资源搜索服务"：搜索并返回磁力链，绝不执行下载（§30/§47）；
- 搜索前可经 MediaResolver 把中文名解析成英文标准实体（§26 内部流程）；
- 不只搜一个关键词：按 标题+年份(+质量词) 生成多个查询，次数受 max_queries 限制（§27）；
- 结果标准化为统一字段（§28）；magnet 允许为 null（§33，附 torrent_url/details_url）；
- 去重：优先 infohash，无 infohash 按 normalized_title+size（§34）；
- 错误细分（§35）：JACKETT_UNAVAILABLE / JACKETT_TIMEOUT / JACKETT_AUTH /
  JACKETT_PARSE / JACKETT_HTTP；
- API Key 走 apikey query 参数（Jackett 官方约定），绝不进入日志与错误消息（§42）。

Jackett API：
  GET {url}/api/v2.0/indexers/all/results?apikey=...&Query=...
  返回 {"Results": [{Guid,Title,Tracker,Size,Seeders,Peers,MagnetUri,Link,Details,PublishDate,...}]}
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import JackettConfig, TmdbConfig
from .release import release_dict
from .resolver import ResolverError
from .resolver import resolve as tmdb_resolve
from .security import InvalidArguments

logger = logging.getLogger("nas_mcp.jackett")

# §46/§47：单次搜索返回上限
MAX_RESULTS_CAP = 1000

# 关键词扩展质量词（§27）：基础查询之外的扩展维度
_QUALITY_HINTS = ("1080p", "2160p", "BluRay", "WEB-DL")

_BTIH_RE = re.compile(r"btih:([0-9a-zA-Z]+)", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------- 错误模型


class JackettError(Exception):
    """Jackett 调用失败。code 为细分错误码（§35），status_code 为 HTTP 状态码（如有）。"""

    def __init__(self, code: str, message: str, *, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(message)


# ---------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class JackettResult:
    """标准化搜索结果（§28）。magnet 允许为 None（§33）。"""

    title: str
    size: int | None
    seeders: int | None
    leechers: int | None
    indexer: str
    publish_date: str | None
    magnet: str | None
    torrent_url: str | None
    details_url: str | None
    infohash: str | None      # 从 magnet 提取，用于去重（§34）


# ---------------------------------------------------------------- Client


class JackettClient:
    """Jackett /api/v2.0/indexers/all/results 只读客户端（stdlib urllib）。

    注意：API Key 出现在请求 URL（官方约定），因此严禁把 URL 写入日志；
    所有错误消息只含状态码/原因，不含 URL（§42）。
    """

    def __init__(self, cfg: JackettConfig):
        self.cfg = cfg
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        interval = self.cfg.min_request_interval_seconds
        if interval > 0:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < interval:
                time.sleep(interval - elapsed)
        self._last_request_at = time.monotonic()

    def search(self, query: str, limit: int) -> list[dict]:
        self._throttle()
        params = urllib.parse.urlencode({"apikey": self.cfg.api_key, "Query": query})
        url = f"{self.cfg.url}/api/v2.0/indexers/all/results?{params}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise JackettError(
                    "JACKETT_AUTH", f"Jackett 认证失败（HTTP {exc.code}）：API Key 无效",
                    status_code=exc.code,
                ) from exc
            raise JackettError(
                "JACKETT_HTTP", f"Jackett HTTP {exc.code}", status_code=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
            if "timed out" in reason.lower():
                raise JackettError(
                    "JACKETT_TIMEOUT", f"Jackett 请求超时（>{self.cfg.timeout_seconds}s）"
                ) from exc
            raise JackettError(
                "JACKETT_UNAVAILABLE", f"Jackett 不可用（未启动或网络不可达）: {reason}"
            ) from exc
        except TimeoutError as exc:
            raise JackettError(
                "JACKETT_TIMEOUT", f"Jackett 请求超时（>{self.cfg.timeout_seconds}s）"
            ) from exc

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise JackettError("JACKETT_PARSE", f"Jackett 返回 JSON 解析失败: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("Results"), list):
            raise JackettError("JACKETT_PARSE", "Jackett 返回格式异常：缺少 Results 数组")
        return [r for r in data["Results"][:limit] if isinstance(r, dict)]


# ---------------------------------------------------------------- 解析/去重


def _to_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _extract_infohash(magnet: str | None) -> str | None:
    if not magnet:
        return None
    m = _BTIH_RE.search(magnet)
    return m.group(1).lower() if m else None


def _norm_title(title: str) -> str:
    return _NON_ALNUM_RE.sub("", title.lower())


def _parse_result(raw: dict) -> JackettResult:
    magnet = str(raw.get("MagnetUri") or "") or None
    link = str(raw.get("Link") or "") or None
    # Link 有时直接是 magnet 开头
    if magnet is None and link and link.lower().startswith("magnet:"):
        magnet, link = link, None
    return JackettResult(
        title=str(raw.get("Title", "")),
        size=_to_int(raw.get("Size")),
        seeders=_to_int(raw.get("Seeders")),
        leechers=_to_int(raw.get("Peers")),
        indexer=str(raw.get("Tracker", "")),
        publish_date=str(raw.get("PublishDate") or "") or None,
        magnet=magnet,
        torrent_url=link,
        details_url=str(raw.get("Details") or "") or None,
        infohash=_extract_infohash(magnet),
    )


def _dedup(results: list[JackettResult]) -> list[JackettResult]:
    """§34：优先 infohash 去重；无 infohash 按 normalized_title+size 辅助去重。"""
    seen: set[tuple] = set()
    out: list[JackettResult] = []
    for r in results:
        key = ("hash", r.infohash) if r.infohash else ("ts", _norm_title(r.title), r.size)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def generate_queries(title: str, year: int | None, max_queries: int) -> list[str]:
    """§27：生成多个搜索关键词，数量受 max_queries 限制。

    例如 Interstellar 2014 → ["Interstellar 2014", "Interstellar 2014 1080p",
    "Interstellar 2014 BluRay", "Interstellar 2014 WEB-DL"]
    """
    base = f"{title} {year}".strip() if year else title
    queries = [base]
    for hint in _QUALITY_HINTS:
        if len(queries) >= max_queries:
            break
        queries.append(f"{base} {hint}")
    return queries[:max_queries]


# ---------------------------------------------------------------- 工具入口


def search(
    jcfg: JackettConfig,
    tcfg: TmdbConfig,
    *,
    query: str,
    year: int = 0,
    media_type: str = "",
    limit: int = 0,
) -> dict:
    """搜索媒体资源（只读网络搜索，绝不下载）。

    内部流程（§26）：query → MediaResolver（tmdb.enabled 时）→ 生成多个关键词
    → Jackett API → 标准化 → 去重 → 返回（§31 格式）。
    """
    if not jcfg.enabled:
        raise InvalidArguments("jackett.enabled=false，未启用资源搜索（请配置 jackett 段）")
    query = query.strip()
    if not query:
        raise InvalidArguments("搜索关键词 query 不能为空")
    if media_type and media_type not in ("movie", "tv"):
        raise InvalidArguments(f"media_type 只能是 movie/tv（留空自动判断），得到: {media_type!r}")
    if year and not (1878 <= year <= 2100):
        raise InvalidArguments(f"year 非法: {year!r}")
    final_limit = limit if limit > 0 else jcfg.max_results
    if final_limit > MAX_RESULTS_CAP:
        raise InvalidArguments(f"limit 不能超过 {MAX_RESULTS_CAP}（§46/§47），得到: {limit}")

    # 1. MediaResolver：中文名 → 标准实体（失败不阻断搜索，回退原始 query）
    resolved_info: dict | None = None
    search_title = query
    search_year = year or None
    if tcfg.enabled:
        try:
            r = tmdb_resolve(tcfg, query, year=year or None, media_type=media_type)
        except ResolverError as exc:
            logger.warning("TMDB 解析失败（回退原始关键词搜索）: %s", exc)
        else:
            if r["resolved"] and r["identity"]:
                ident = r["identity"]
                resolved_info = ident
                # Jackett 资源多用原名/英文名（§19）
                search_title = ident["original_title"] or ident["canonical_title"]
                search_year = search_year or ident["year"]
            else:
                logger.info("TMDB 无法确认 %r，使用原始关键词搜索", query)

    # 2. 关键词生成（§27）+ 逐关键词搜索合并
    # 注意：每个关键词按配置上限 max_results 拉取原始结果，合并/去重/排序后
    # 才用 limit 截断——若按 limit 逐关键词截断，去重池会被缩小导致漏重。
    queries = generate_queries(search_title, search_year, jcfg.max_queries)
    client = JackettClient(jcfg)
    merged: list[JackettResult] = []
    for q in queries:
        raw_items = client.search(q, jcfg.max_results)
        logger.info("Jackett 搜索 %r：%d 条", q, len(raw_items))
        merged.extend(_parse_result(item) for item in raw_items)

    # 3. 去重（§34）+ 做种数降序 + 限额
    deduped = _dedup(merged)
    deduped.sort(key=lambda r: r.seeders if r.seeders is not None else -1, reverse=True)
    deduped = deduped[:final_limit]

    # §31 返回格式。seeders_missing_count：返回结果中索引器未提供做种数的条数
    # （站点定义抓取限制/索引器不支持，并非本服务故障），供 Agent 区分
    # "拿不到数据"与"服务异常"，避免臆测 Jackett 版本等问题。
    return {
        "ok": True,
        "query": query,
        "resolved": resolved_info,
        "queries": queries,
        "total": len(deduped),
        "seeders_missing_count": sum(1 for r in deduped if r.seeders is None),
        "results": [
            {
                "title": r.title,
                "size": r.size,
                "seeders": r.seeders,
                "leechers": r.leechers,
                "indexer": r.indexer,
                "publish_date": r.publish_date,
                "magnet": r.magnet,
                "torrent_url": r.torrent_url,
                "details_url": r.details_url,
                # 需求 2.0 §25：release name 结构化质量维度 + 评分，供 Octop 选版本
                "release": release_dict(r.title),
            }
            for r in deduped
        ],
    }
