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
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace

from .config import JackettConfig, TmdbConfig
from .release import parse_release, release_dict
from .resolver import ResolverError
from .resolver import resolve as tmdb_resolve
from .security import InvalidArguments

logger = logging.getLogger("nas_mcp.jackett")

# §46/§47：单次搜索返回上限
MAX_RESULTS_CAP = 1000

# 关键词扩展质量词（§27）：基础查询之外的扩展维度
_QUALITY_HINTS = ("1080p", "2160p", "BluRay", "WEB-DL")

# 重试前固定退避（秒）。仅瞬时故障（超时/连接错误/5xx）才重试，
# 401/403 等 4xx 与解析错误重试无意义，直接抛出。
_RETRY_BACKOFF_SECONDS = 1.0

# T1：剩余总预算低于该秒数时不再重试，直接抛最后一次错误——否则重试链
# 会把 deadline 吃穿（实测 48s 超时 × 2 次 + backoff ≈ 99s > 50s 预算）。
_MIN_RETRY_BUDGET_SECONDS = 3.0

# OPEN-2：聚合端点单请求的独立预算上限。此前首跳 eff_timeout=min(90,
# 剩余≈50s) 会把总 deadline 一次吃穿，超时后 deadline-now>=1.0 恒不满足，
# T4 并行降级永远不触发——任何慢索引器 = 整腿死透。
# 真机实测：单关键词聚合查询耗时 19.3s（15s 预算会被判超时），故放宽到 45s，
# 给 2 倍余量；总预算仍受 config.jackett.deadline_seconds（默认 50s）约束，
# 超出部分由 _FALLBACK_RESERVE_SECONDS 留给并行降级。
_AGGREGATE_HOP_SECONDS = 45.0
# 并行降级的保底预算：聚合请求吃剩不足该值时收缩单请求预算（但不低于下限，
# 避免把正常慢源挤死）。
_FALLBACK_RESERVE_SECONDS = 8.0
_MIN_AGGREGATE_HOP_SECONDS = 5.0

_BTIH_RE = re.compile(r"btih:([0-9a-zA-Z]+)", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _jackett_retryable(exc: "JackettError") -> bool:
    """该错误是否属于值得重试的瞬时故障。"""
    if exc.code in ("JACKETT_TIMEOUT", "JACKETT_UNAVAILABLE"):
        return True
    if exc.code == "JACKETT_HTTP" and exc.status_code is not None:
        return exc.status_code >= 500
    return False


# ---------------------------------------------------------------- 熔断（#3）
# 连续 TIMEOUT/UNAVAILABLE 达到阈值 → 冷却期内直接快速失败（Prowlarr 同款思路）：
# 上游已证明不可达时继续逐关键词重试只会把 harness 的总超时耗尽，Agent 拿不到
# 任何结构化结果。冷却期过后放行一次半开探测（下次调用正常走）。
_CIRCUIT_FAILURE_THRESHOLD = 3
_CIRCUIT_COOLDOWN_SECONDS = 60.0
# T8：失败计数的滚动时间窗——只有 10 分钟内的连续失败才累计；超窗从 1 重计，
# 避免偶发故障在数小时跨度上凑满阈值后误熔断。
_CIRCUIT_FAILURE_WINDOW_SECONDS = 600.0
_circuit_failures = 0
_circuit_open_until = 0.0
_last_failure_at = 0.0


def reset_circuit() -> None:
    """清空熔断状态（测试/诊断用）。"""
    global _circuit_failures, _circuit_open_until, _last_failure_at
    _circuit_failures = 0
    _circuit_open_until = 0.0
    _last_failure_at = 0.0


def _circuit_state() -> dict:
    remaining = max(0.0, _circuit_open_until - time.monotonic())
    return {
        "state": "open" if remaining > 0 else ("half_open" if _circuit_open_until else "closed"),
        "cooldown_remaining_seconds": round(remaining, 1),
        "consecutive_failures": _circuit_failures,
    }


def _record_circuit(success: bool) -> None:
    global _circuit_failures, _circuit_open_until, _last_failure_at
    if success:
        _circuit_failures = 0
        _circuit_open_until = 0.0
        _last_failure_at = 0.0
        return
    now = time.monotonic()
    # T8：距上次失败超过时间窗 → 视为新一轮，从 1 重新计数
    if _last_failure_at and now - _last_failure_at > _CIRCUIT_FAILURE_WINDOW_SECONDS:
        _circuit_failures = 0
    _last_failure_at = now
    _circuit_failures += 1
    if _circuit_failures >= _CIRCUIT_FAILURE_THRESHOLD:
        _circuit_open_until = now + _CIRCUIT_COOLDOWN_SECONDS


# ---------------------------------------------------------------- 错误模型


class JackettError(Exception):
    """Jackett 调用失败。code 为细分错误码（§35），status_code 为 HTTP 状态码（如有）。

    retryable：该错误是否值得稍后重试（瞬时故障/熔断冷却后可重试），
    供 Agent 决策"等一会重试"还是"换路线"，避免无限重试打爆上游。
    retry_after：秒数建议（熔断打开时为剩余冷却时间），否则 None。
    """

    def __init__(self, code: str, message: str, *, status_code: int | None = None,
                 retryable: bool | None = None, retry_after: float | None = None):
        self.code = code
        self.status_code = status_code
        self.retryable = _jackett_retryable(self) if retryable is None else retryable
        self.retry_after = retry_after
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

    def search(self, query: str, limit: int, *, timeout: float | None = None,
               deadline: float | None = None) -> list[dict]:
        self._throttle()
        params = urllib.parse.urlencode({"apikey": self.cfg.api_key, "Query": query})
        url = f"{self.cfg.url}/api/v2.0/indexers/all/results?{params}"
        body = self._fetch_with_retry(url, timeout=timeout, deadline=deadline)

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise JackettError("JACKETT_PARSE", f"Jackett 返回 JSON 解析失败: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("Results"), list):
            raise JackettError("JACKETT_PARSE", "Jackett 返回格式异常：缺少 Results 数组")
        return [r for r in data["Results"][:limit] if isinstance(r, dict)]

    def _fetch_with_retry(self, url: str, *, timeout: float | None = None,
                          deadline: float | None = None) -> bytes:
        """单次 GET；瞬时故障（超时/不可达/5xx）按 max_retries 自动重试。

        timeout：单请求超时上限（秒）。
        deadline：总预算的绝对时刻（time.monotonic() 基准，T1）。每次 attempt
        前重算 eff_timeout = min(timeout, deadline - now)，并保证重试链总耗时
        不吃穿 deadline；剩余预算 < 3s 时不再重试，直接抛最后一次 JackettError。
        """
        base_timeout = timeout if timeout is not None else self.cfg.timeout_seconds
        last_exc: JackettError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            # 每次 attempt 前按绝对时刻重算本次有效超时（T1 核心）
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise last_exc if last_exc is not None else JackettError(
                        "JACKETT_TIMEOUT",
                        f"搜索 deadline 预算已耗尽（>{base_timeout:.1f}s 单请求上限）",
                    )
                eff_timeout = min(base_timeout, remaining)
            else:
                eff_timeout = base_timeout
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=eff_timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    last_exc = JackettError(
                        "JACKETT_AUTH", f"Jackett 认证失败（HTTP {exc.code}）：API Key 无效",
                        status_code=exc.code,
                    )
                else:
                    last_exc = JackettError(
                        "JACKETT_HTTP", f"Jackett HTTP {exc.code}", status_code=exc.code
                    )
            except (urllib.error.URLError, ConnectionError) as exc:
                # 3.12 起 RemoteDisconnected 等 ConnectionError 可能不经 URLError 包装直接冒泡
                reason = getattr(exc, "reason", exc)
                reason_s = str(reason)
                if "timed out" in reason_s.lower():
                    last_exc = JackettError(
                        "JACKETT_TIMEOUT", f"Jackett 请求超时（>{eff_timeout:.1f}s）"
                    )
                else:
                    last_exc = JackettError(
                        "JACKETT_UNAVAILABLE",
                        f"Jackett 不可用（未启动或网络不可达）: {reason_s}",
                    )
            except TimeoutError as exc:
                last_exc = JackettError(
                    "JACKETT_TIMEOUT", f"Jackett 请求超时（>{eff_timeout:.1f}s）"
                )

            if (attempt < self.cfg.max_retries
                    and last_exc is not None
                    and _jackett_retryable(last_exc)):
                # T1：剩余总预算不足以再撑一次重试 → 不重试，直接抛最后错误
                if (deadline is not None
                        and deadline - time.monotonic()
                        < _MIN_RETRY_BUDGET_SECONDS):
                    break
                logger.warning(
                    "Jackett 请求失败（第 %d 次尝试，%.0fs 后重试）: %s",
                    attempt + 1, _RETRY_BACKOFF_SECONDS, last_exc,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS)
                continue
            break

        assert last_exc is not None  # 循环走完未返回必有错误
        raise last_exc


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


def _strip_jackett_apikey(url: str | None) -> str | None:
    """T2：剥离 URL 中的 jackett_apikey 参数（§42 硬约定）。

    Jackett 返回的 Link 形如 /dl/<indexer>/?jackett_apikey=<key>&path=...，
    原样进结果体等于把 API key 送给 Agent 上下文。
    - 无该参数 → 原样返回；
    - 可剥离 → 返回剥离后的 URL；
    - 无法剥离（解析失败/剥离后仍残留）→ 返回 None（置 null）。
    """
    if not url:
        return url
    if "jackett_apikey" not in url.lower():
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        kept = [(k, v) for k, v in urllib.parse.parse_qsl(
            parts.query, keep_blank_values=True)
            if "jackett_apikey" not in k.lower()]
        rebuilt = urllib.parse.urlunsplit((
            parts.scheme, parts.netloc, parts.path,
            urllib.parse.urlencode(kept), parts.fragment,
        ))
    except Exception:  # noqa: BLE001 - 剥离失败一律置 null
        return None
    return None if "jackett_apikey" in rebuilt.lower() else rebuilt


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


# ---------------------------------------------------------------- T4 并行降级

# torznab 命名空间（Jackett 的单 indexer 端点返回该 XML 格式）
_TORZNAB_NS = {"t": "http://torznab.com/schemas/2015/feed"}

# 降级并发度（任务指定 max_workers=5）
_FALLBACK_MAX_WORKERS = 5


def _parse_torznab_items(data: bytes, indexer_id: str) -> list[dict]:
    """把 torznab 单源响应（XML，兼容 Jackett JSON）转成 _parse_result 的 raw 结构。

    复用统一的标准化/去重逻辑（T4 要求"复用现有解析/去重"），这里只做格式适配。
    """
    text = data.decode("utf-8", errors="replace").lstrip()
    if text.startswith("{"):
        # 兜底：个别部署对单 indexer 也返回 Jackett JSON
        parsed = json.loads(text)
        items = parsed.get("Results") if isinstance(parsed, dict) else None
        return [r for r in (items or []) if isinstance(r, dict)]
    root = ET.fromstring(text)
    out: list[dict] = []
    for item in root.iter("item"):
        attrs: dict[str, str] = {}
        for attr in item.findall("t:attr", _TORZNAB_NS):
            name = attr.get("name") or ""
            if name:
                attrs[name] = attr.get("value") or ""
        enc = item.find("enclosure")
        out.append({
            "Title": item.findtext("title") or "",
            "Link": item.findtext("link") or "",
            "Details": item.findtext("guid") or "",
            "Size": (enc.get("length") if enc is not None else None) or attrs.get("size"),
            "Seeders": attrs.get("seeders"),
            "Peers": attrs.get("peers"),
            "MagnetUri": attrs.get("magneturl") or "",
            "PublishDate": item.findtext("pubDate") or "",
            "Tracker": indexer_id,
        })
    return out


def _fetch_one_indexer(client: "JackettClient", indexer_id: str,
                       query: str, deadline: float, limit: int) -> list[dict]:
    """T4：请求单 indexer 的 torznab 端点（受总 deadline 约束，与 T1 同一口径）。"""
    params = urllib.parse.urlencode(
        {"apikey": client.cfg.api_key, "t": "search", "q": query})
    url = (f"{client.cfg.url}/api/v2.0/indexers/"
           f"{urllib.parse.quote(indexer_id, safe='')}/results/torznab/api?{params}")
    body = client._fetch_with_retry(url, deadline=deadline)
    return _parse_torznab_items(body, indexer_id)[:limit]


def _parallel_fallback_search(client: "JackettClient", query: str,
                              deadline: float, limit: int
                              ) -> tuple[list[JackettResult], list[str]]:
    """T4：ThreadPoolExecutor 并行打各单源；返回 (结果, 失败的 indexer id)。

    - 单源失败（超时/HTTP/解析）忽略并计入 failed，绝不让一个源拖垮降级；
    - 每个请求的超时都由 T1 的 deadline 重算收口，整体不超出总预算。
    """
    ids = list(client.cfg.fast_indexers)
    results: list[JackettResult] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=_FALLBACK_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_one_indexer, client, idx, query, deadline, limit): idx
            for idx in ids
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                raws = fut.result()
            except Exception as exc:  # noqa: BLE001 - 单源失败忽略并计数
                logger.info("T4 降级：单源 %s 失败（%s）", idx, type(exc).__name__)
                failed.append(idx)
                continue
            results.extend(_parse_result(r) for r in raws)
    return results, sorted(failed)


# ---------------------------------------------------------------- T6 302 补磁力

# 302 跟进并行上限（任务指定 ≤20 条并行）
_FOLLOW_MAX_WORKERS = 20
# 40 位 hex btih（严格：过短/过长都不要，避免把随机串当 infohash）
_BTIH40_RE = re.compile(r"btih:([0-9a-fA-F]{40})(?:[^0-9a-zA-Z]|$)", re.IGNORECASE)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁跳转：只读 302 Location，不跟随（任务要求）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _is_own_dl_link(jcfg: JackettConfig, url: str | None) -> bool:
    """SSRF 收敛：只对 {jackett_url}/dl/ 前缀的链接发跟进请求。"""
    if not url:
        return False
    try:
        base = urllib.parse.urlsplit(jcfg.url.rstrip("/"))
        target = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (
        target.scheme == base.scheme
        and target.netloc == base.netloc
        and target.path.startswith("/dl/")
    )


def _follow_one_magnet(jcfg: JackettConfig,
                       r: "JackettResult") -> "JackettResult | None":
    """单条：禁跳 GET torrent_url，从 302 Location 提取 40 位 btih 补裸磁力。

    返回补全后的新结果（frozen dataclass 不可原地改）；任何失败（超时/非 302/
    无 btih/非本服务链接）静默降级返回 None（保持原 magnet=None）。
    """
    if r.magnet is not None or not r.torrent_url:
        return None
    if not _is_own_dl_link(jcfg, r.torrent_url):
        return None
    location = ""
    try:
        req = urllib.request.Request(r.torrent_url, method="GET",
                                     headers={"Accept": "*/*"})
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(req, timeout=jcfg.follow_timeout):
            # 2xx 说明不是跳转链接 → 无法取得 btih，静默放弃
            return None
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            return None
        location = exc.headers.get("Location") or ""
    except Exception:  # noqa: BLE001 - 跟进失败一律静默降级
        return None
    m = _BTIH40_RE.search(location)
    if not m:
        return None
    btih = m.group(1).upper()
    return replace(r, magnet=f"magnet:?xt=urn:btih:{btih}", infohash=btih.lower())


def _fill_missing_magnets(jcfg: JackettConfig,
                          results: list["JackettResult"]) -> None:
    """T6：magnet 缺失且有 torrent_url 的结果，并行跟进 302 补磁力（≤20 并行）。

    原地用补全后的新结果替换列表元素（frozen dataclass）。
    """
    if not jcfg.follow_magnet_redirects:
        return
    slots = [(i, r) for i, r in enumerate(results)
             if r.magnet is None and r.torrent_url]
    if not slots:
        return
    with ThreadPoolExecutor(max_workers=_FALLBACK_MAX_WORKERS) as pool:
        futures = {pool.submit(_follow_one_magnet, jcfg, r): i
                   for i, r in slots}
        for fut in as_completed(futures):
            try:
                new = fut.result()
            except Exception:  # noqa: BLE001 - 已在单条内收口，兜底静默
                continue
            if new is not None:
                results[futures[fut]] = new


# ---------------------------------------------------------------- 工具入口


def _aggregate_hop_deadline(deadline: float, *, reserve_fallback: bool) -> float:
    """OPEN-2：计算本次聚合端点请求的预算截止时刻。

    - 单请求最多 _AGGREGATE_HOP_SECONDS，保证慢源不会把总 deadline 吃穿；
    - 还没拿到结果且启用并行降级时，为降级预留 _FALLBACK_RESERVE_SECONDS
      保底预算（收缩后不低于 _MIN_AGGREGATE_HOP_SECONDS，也不越过总 deadline）。
    """
    now = time.monotonic()
    remaining = deadline - now
    budget = min(_AGGREGATE_HOP_SECONDS, remaining)
    if reserve_fallback:
        if remaining - budget < _FALLBACK_RESERVE_SECONDS:
            budget = min(
                budget,
                max(_MIN_AGGREGATE_HOP_SECONDS, remaining - _FALLBACK_RESERVE_SECONDS),
                remaining,
            )
    return now + budget


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

    # 熔断打开期：直接快速失败，返回结构化错误而不是让 harness 超时切断（#3）
    if time.monotonic() < _circuit_open_until:
        remaining = _circuit_open_until - time.monotonic()
        raise JackettError(
            "JACKETT_CIRCUIT_OPEN",
            f"Jackett 连续 {_circuit_failures} 次不可达/超时已熔断，"
            f"{remaining:.0f}s 后重试（retry_after 给出冷却剩余秒数）",
            retryable=True,
            retry_after=round(remaining, 1),
        )

    started = time.monotonic()
    deadline = started + jcfg.deadline_seconds

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
                if ident.get("low_confidence"):
                    # OPEN-1：低置信命中（如「流人」→ 4 票同名电影）不再替换
                    # 关键词——继续用原始 query 搜索，resolved 仅作参考返回，
                    # 由 Agent 结合 match_type/vote_count 判断是否可信
                    logger.info(
                        "TMDB 解析低置信（match=%s votes=%s），回退原始关键词搜索",
                        ident.get("match_type"), ident.get("vote_count"),
                    )
                else:
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
    queries_executed = 0
    deadline_exceeded = False
    last_error: JackettError | None = None
    for q in queries:
        # 总 deadline：剩余预算不足单请求时提前收口（#3），保证超时发生在
        # 插件内并以结构化错误/部分结果返回，而不是被 harness 掐断拿不到任何输出
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            deadline_exceeded = True
            break
        try:
            # T1：传绝对 deadline，由 _fetch_with_retry 每次 attempt 前重算
            # eff_timeout，重试链也不吃穿总预算。
            # OPEN-2：聚合端点走独立小上限（并行降级还可拿到保底预算），
            # 慢源超时后仍有剩余预算触发 T4 降级，而不是把 deadline 吃穿
            hop_deadline = _aggregate_hop_deadline(
                deadline,
                reserve_fallback=bool(
                    jcfg.parallel_fallback and jcfg.fast_indexers and not merged
                ),
            )
            raw_items = client.search(q, jcfg.max_results, deadline=hop_deadline)
        except JackettError as exc:
            last_error = exc
            if exc.code in ("JACKETT_TIMEOUT", "JACKETT_UNAVAILABLE"):
                _record_circuit(success=False)
            if queries_executed == 0 and not _jackett_retryable(exc):
                raise  # 首个关键词就不可重试失败：直接抛出结构化错误
            break
        _record_circuit(success=True)
        queries_executed += 1
        logger.info("Jackett 搜索 %r：%d 条", q, len(raw_items))
        merged.extend(_parse_result(item) for item in raw_items)

    # T4：聚合端点（indexers/all）超时/不可达或 0 结果 → 降级为 fast_indexers
    # 并行单源查询（torznab 单 indexer 端点，通常响应快），受同一 deadline 约束；
    # 降级成功视为一次可用性成功（重置熔断），返回体标注 mode 区分两种来源。
    mode = "aggregate"
    fallback_failed: list[str] = []
    if (
        jcfg.parallel_fallback
        and jcfg.fast_indexers
        and not merged
        and (
            (last_error is not None and last_error.code in ("JACKETT_TIMEOUT", "JACKETT_UNAVAILABLE"))
            or deadline_exceeded
            or queries_executed > 0   # 聚合端点成功但 0 结果
        )
        and deadline - time.monotonic() >= 1.0   # 至少留 1s 降级预算才值得尝试
    ):
        fb_results, fallback_failed = _parallel_fallback_search(
            client, queries[0], deadline, jcfg.max_results
        )
        if fb_results:
            merged.extend(fb_results)
            mode = "fallback_parallel"
            _record_circuit(success=True)

    if queries_executed == 0 and not merged and last_error is not None:
        # 一个关键词都没跑成功（且降级也没救回来）：结构化错误 + 诊断字段（#3）
        raise JackettError(
            last_error.code,
            f"{last_error}（deadline={jcfg.deadline_seconds}s, "
            f"elapsed={time.monotonic() - started:.1f}s）",
            status_code=last_error.status_code,
            retryable=last_error.retryable,
            retry_after=last_error.retry_after,
        )
    if queries_executed == 0 and not merged and deadline_exceeded:
        raise JackettError(
            "JACKETT_TIMEOUT",
            f"搜索总预算 {jcfg.deadline_seconds}s 已耗尽，未发出任何关键词请求",
            retryable=True,
            retry_after=1.0,
        )

    # 3. 去重（§34）+ 排序 + 限额
    deduped = _dedup(merged)
    # T5：quality_score 排序前一次性预计算（禁止 sort key 内重复调 parse_release）
    quality_scores = {id(r): parse_release(r.title).quality_score for r in deduped}
    if jcfg.sort == "quality":
        # quality_score 降序 → seeders 降序 → title 升序
        deduped.sort(key=lambda r: (
            -quality_scores[id(r)],
            -(r.seeders if r.seeders is not None else -1),
            r.title,
        ))
    elif jcfg.sort == "hybrid":
        # 0.8*quality_score + 20*min(log2(1+max(seeders,0)), 10)，降序
        def _hybrid(r: JackettResult) -> float:
            s = r.seeders if r.seeders is not None else -1
            return 0.8 * quality_scores[id(r)] + 20 * math.log2(1 + max(s, 0))
        deduped.sort(key=_hybrid, reverse=True)
    else:
        # seeders（默认，行为与历史版本一致）
        deduped.sort(key=lambda r: r.seeders if r.seeders is not None else -1, reverse=True)
    deduped = deduped[:final_limit]

    # T6：magnet 缺失的结果并行跟进 {url}/dl/ 的 302 Location 补 infohash 磁力
    # （在截断后仅补最终返回的条目；跟进用原始 torrent_url，T2 清洗只发生在序列化处）
    _fill_missing_magnets(jcfg, deduped)

    # §31 返回格式。seeders_missing_count：返回结果中索引器未提供做种数的条数
    # （站点定义抓取限制/索引器不支持，并非本服务故障），供 Agent 区分
    # "拿不到数据"与"服务异常"，避免臆测 Jackett 版本等问题。
    return {
        "ok": True,
        "query": query,
        "resolved": resolved_info,
        "queries": queries,
        # #3 诊断字段：覆盖度与耗时，供 Agent 判断结果是否被 deadline 截断
        "queries_planned": len(queries),
        "queries_executed": queries_executed,
        "deadline_seconds": jcfg.deadline_seconds,
        "deadline_exceeded": deadline_exceeded,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        # T4：aggregate=聚合端点常规路径；fallback_parallel=聚合超时/0 结果后
        # 的并行单源降级（附失败的 indexer 列表，供诊断哪些单源不可用）
        "mode": mode,
        **({"fallback_failed_indexers": fallback_failed} if mode == "fallback_parallel" else {}),
        "circuit": _circuit_state(),
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
                # T2：序列化前剥离 jackett_apikey，绝不把 API key 送进上下文（§42）
                "torrent_url": _strip_jackett_apikey(r.torrent_url),
                "details_url": _strip_jackett_apikey(r.details_url),
                # 需求 2.0 §25：release name 结构化质量维度 + 评分，供 Octop 选版本
                "release": release_dict(r.title),
            }
            for r in deduped
        ],
    }
