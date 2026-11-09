"""伪射手（assrt.net）字幕搜索与下载。

链路（纯 REST API，无会话/验证中转）：
  GET /v1/sub/search?q=...        → JSON 搜索结果列表
  GET /v1/sub/detail?id=...       → JSON 详情（含下载直链 url）
  GET 直链                        → 字幕文件正文（部分条目返回压缩包，部分直接返回文本）

连接策略（安全底线）：
  - 默认使用 ``assrt.proxy`` 配置的代理；未配置代理时普通直连；
  - 只有显式 ``insecure_no_sni_direct: true`` 才启用「不发 SNI + 不校验证书」
    的直连降级；启用时日志 WARNING，并在候选/下载结果中带
    ``transport=insecure_no_sni_direct`` 标记，调用方可见；
  - 绝不默认降低 TLS 安全性，也不把 token 拼进 URL 或写日志。

错误结构：``code`` / ``retryable`` / ``site`` / ``endpoint`` / ``detail``，
由编排层汇总进 site_errors / site_error_details，不暴露响应原文。
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from .config import AssrtConfig
from .subhd import SubtitleCandidate, VideoQuery, score_candidate

logger = logging.getLogger("nas_mcp.assrt")

_SID_PREFIX = "assrt:"
_LEGACY_SID_PREFIXES = ("shooter:",)

# 重试前固定退避（秒）。仅瞬时故障才重试：网络层失败（无 HTTP 状态码——
# 超时/断连/不可达）或服务端 5xx；4xx（认证/不存在/超限）与解析错误不重试。
_RETRY_BACKOFF_SECONDS = 1.0

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 依赖可用性开关：测试可 monkeypatch 为 False 模拟运行环境缺 ssl/http.client，
# 触发 dependency_missing 降级（只影响 assrt 站，不影响其他站点）。
_HAS_SSL = True
_HAS_HTTP_CLIENT = True

TRANSPORT_DIRECT = "direct"
TRANSPORT_PROXY = "proxy"
TRANSPORT_INSECURE_NO_SNI = "insecure_no_sni_direct"

# assrt.net langlist 键到内部语言代码的映射（与 SubHD 内部语言标签对齐）
_LANGLIST_MAPPING = {
    "zh-CN": "zh-Hans",
    "zh-TW": "zh-Hant",
    "zh-Hans": "zh-Hans",
    "zh-Hant": "zh-Hant",
    "简体": "zh-Hans",
    "繁体": "zh-Hant",
    "双语": "zh-Hans+en",
    "en": "en",
    "英语": "en",
    "ja": "ja",
    "日文": "ja",
    "ko": "ko",
    "韩语": "ko",
}


class AssrtError(Exception):
    """assrt.net 操作失败（网络/解析/认证/依赖等）。

    统一错误结构：code 供 Agent 决策，retryable 表示是否值得重试，
    site/endpoint 定位来源，detail 只放非敏感补充信息。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: bytes = b"",
        code: str = "assrt_error",
        site: str = "assrt",
        endpoint: str = "",
        retryable: bool | None = None,
        detail: str = "",
    ):
        self.status_code = status_code
        self.body = body
        self.code = code
        self.site = site
        self.endpoint = endpoint
        self.retryable = retryable
        self.detail = detail
        super().__init__(message)


def _retryable(exc: AssrtError) -> bool:
    """该错误是否属于值得重试的瞬时故障。"""
    if exc.retryable is not None:
        return exc.retryable
    return exc.status_code is None or exc.status_code >= 500


def _ensure_dependencies() -> None:
    """assrt 传输所需的 stdlib 依赖检查（缺失只降级本站点）。"""
    if not _HAS_SSL or not _HAS_HTTP_CLIENT:
        raise AssrtError(
            "assrt 传输依赖不可用（ssl/http.client 缺失）",
            code="dependency_missing",
            retryable=False,
        )


def transport_info(cfg: AssrtConfig) -> dict:
    """当前 assrt 连接模式（供编排层写进返回体，便于识别降级连接）。"""
    if cfg.insecure_no_sni_direct:
        return {
            "mode": TRANSPORT_INSECURE_NO_SNI,
            "degraded": True,
            "note": "未发送 SNI 且未校验证书，属不安全降级连接",
        }
    if cfg.proxy:
        return {"mode": TRANSPORT_PROXY, "degraded": False, "note": ""}
    return {"mode": TRANSPORT_DIRECT, "degraded": False, "note": ""}


def _map_langlist(langlist: Any) -> tuple[str, ...]:
    """把 assrt.net 的 langlist 映射为内部语言代码元组。"""
    if not isinstance(langlist, dict):
        return ()
    found: set[str] = set()
    for key in langlist:
        k = str(key).strip()
        if not k:
            continue
        mapped = _LANGLIST_MAPPING.get(k)
        if mapped is not None:
            if "+" in mapped:
                found.update(mapped.split("+"))
            else:
                found.add(mapped)
            continue
        lowered = k.lower()
        if any(tag in lowered for tag in ("zh", "en", "ja", "ko")):
            found.add(k)
    return tuple(sorted(found))


def _extract_subs(resp: dict) -> list[dict]:
    """兼容 {sub:{subs:[...]}} 与 {subs:[...]} 两种响应形态。"""
    sub = resp.get("sub")
    if isinstance(sub, dict):
        subs = sub.get("subs")
    elif isinstance(sub, list):
        subs = sub
    else:
        subs = resp.get("subs")
    return [s for s in (subs or []) if isinstance(s, dict)]


def _filename_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = PurePosixPath(path).name
    return name if name else "subtitle"


def _strip_sid_prefix(sid: str) -> int:
    """从 sid（assrt:12345 / 兼容 shooter:12345）剥离前缀取数字 id。"""
    raw = str(sid).strip()
    lowered = raw.lower()
    if lowered.startswith(_SID_PREFIX):
        raw = raw[len(_SID_PREFIX):].strip()
    else:
        for prefix in _LEGACY_SID_PREFIXES:
            if lowered.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
    try:
        return int(raw)
    except ValueError:
        raise AssrtError(
            f"非法的 assrt sid: {sid!r}（应为 assrt:数字ID 形态）",
            code="invalid_sid",
            retryable=False,
        )


def _keyword(query: VideoQuery | str) -> str:
    """把 VideoQuery / 自由关键词统一成 assrt 的 q 参数。"""
    if isinstance(query, str):
        return query.strip()
    parts = [query.title]
    if query.year:
        parts.append(str(query.year))
    return " ".join(p for p in parts if p).strip()


class _NoSNIHTTPSConnection(http.client.HTTPSConnection):
    """不发 SNI、不校验证书的 HTTPS 连接（仅显式降级开关时使用）。"""

    def connect(self) -> None:  # pragma: no cover - 真实握手依赖网络环境
        sock = socket.create_connection(
            (self.host, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        # server_hostname=None：TLS ClientHello 不携带 SNI
        self.sock = context.wrap_socket(sock, server_hostname=None)


class _NoSNIHTTPSHandler(urllib.request.HTTPSHandler):
    """把 urllib 的 HTTPS 打开方式替换为 no-SNI 降级连接。"""

    def https_open(self, req):  # pragma: no cover - 由真实请求触发
        return self.do_open(_NoSNIHTTPSConnection, req, context=None)


class AssrtClient:
    """assrt.net HTTP 客户端：Bearer token + 限速 + 重试 + 可选降级传输。"""

    def __init__(self, cfg: AssrtConfig):
        self.cfg = cfg
        self._last_request_at = 0.0
        _ensure_dependencies()
        if cfg.insecure_no_sni_direct:
            logger.warning(
                "assrt 使用降级传输：不发 SNI 且不校验证书（不安全），"
                "仅因显式开关 insecure_no_sni_direct=true 启用"
            )
            self._opener = urllib.request.build_opener(_NoSNIHTTPSHandler())
        else:
            handlers = []
            if cfg.proxy:
                handlers.append(
                    urllib.request.ProxyHandler(
                        {"http": cfg.proxy, "https": cfg.proxy}
                    )
                )
            self._opener = urllib.request.build_opener(*handlers)

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.cfg.min_request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _open_once(self, req: urllib.request.Request, endpoint: str) -> tuple[bytes, str]:
        try:
            with self._opener.open(req, timeout=self.cfg.timeout_seconds) as resp:
                body = resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            code = "http_error"
            retryable = exc.code >= 500
            if exc.code in (401, 403):
                code, retryable = "auth_failed", False
            elif exc.code == 429:
                code, retryable = "rate_limited", True
            raise AssrtError(
                f"HTTP {exc.code}",
                status_code=exc.code,
                body=body[:500],
                code=code,
                endpoint=endpoint,
                retryable=retryable,
                detail=_transport_detail(self.cfg),
            ) from exc
        except TimeoutError as exc:
            raise AssrtError(
                f"请求超时（>{self.cfg.timeout_seconds}s）",
                code="timeout",
                endpoint=endpoint,
                retryable=True,
                detail=_transport_detail(self.cfg),
            ) from exc
        except (urllib.error.URLError, ConnectionError, ssl.SSLError) as exc:
            reason = getattr(exc, "reason", exc)
            raise AssrtError(
                f"网络请求失败: {type(exc).__name__}",
                code="network_error",
                endpoint=endpoint,
                retryable=True,
                detail=_transport_detail(self.cfg) or f"reason={type(reason).__name__}",
            ) from exc
        return body, content_type

    def _request_json(self, url: str, endpoint: str) -> Any:
        self._rate_limit()
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self.cfg.token}",
                "Accept": "application/json",
                "User-Agent": _DEFAULT_USER_AGENT,
            },
        )
        last_exc: AssrtError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                body, _ = self._open_once(req, endpoint)
                break
            except AssrtError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries and _retryable(exc):
                    logger.warning(
                        "assrt 请求失败（第 %d 次尝试，%s）",
                        attempt + 1, type(exc).__name__,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        else:  # pragma: no cover - 循环必然 break/raise
            raise last_exc  # type: ignore[misc]

        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AssrtError(
                "JSON 解析失败",
                code="parse_error",
                endpoint=endpoint,
                retryable=False,
                detail=type(exc).__name__,
            ) from exc

    def _download(self, url: str, endpoint: str) -> bytes:
        self._rate_limit()
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "*/*", "User-Agent": _DEFAULT_USER_AGENT},
        )
        last_exc: AssrtError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                body, _ = self._open_once(req, endpoint)
                return body
            except AssrtError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries and _retryable(exc):
                    logger.warning(
                        "assrt 下载失败（第 %d 次尝试，%s）",
                        attempt + 1, type(exc).__name__,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    # ---- 高层 API ----

    def search(self, query: VideoQuery | str) -> list[SubtitleCandidate]:
        keyword = _keyword(query)
        if not keyword:
            raise AssrtError(
                "搜索关键词为空", code="invalid_argument", retryable=False
            )
        params = urllib.parse.urlencode({"q": keyword})
        url = f"{self.cfg.base_url}/v1/sub/search?{params}"
        resp = self._request_json(url, "/v1/sub/search")
        if not isinstance(resp, dict):
            raise AssrtError(
                "搜索接口返回非 JSON 对象",
                code="parse_error",
                endpoint="/v1/sub/search",
                retryable=False,
            )
        # assrt 成功 status=0；其余携带 errmsg（不含 token）
        status = resp.get("status")
        if status not in (0, "0", None) and not _extract_subs(resp):
            raise AssrtError(
                f"搜索接口返回错误状态 {status}",
                code="api_error",
                endpoint="/v1/sub/search",
                retryable=False,
            )

        candidates: list[SubtitleCandidate] = []
        for sub in _extract_subs(resp):
            sid_num = sub.get("id")
            if sid_num is None:
                continue
            native_name = str(sub.get("native_name") or "").strip()
            origin_name = str(sub.get("origin_name") or "").strip()
            video_name = str(sub.get("videoname") or "").strip()
            title = native_name or origin_name or videoname or f"assrt:{sid_num}"
            release_name = origin_name or video_name or native_name
            candidates.append(
                SubtitleCandidate(
                    sid=f"{_SID_PREFIX}{sid_num}",
                    title=title,
                    release_name=release_name,
                    languages=_map_langlist(sub.get("langlist")),
                    format="",  # 搜索结果不含格式信息，下载后才知
                    source="assrt",
                    site="assrt",
                )
            )
        logger.info("assrt 搜索完成：%d 条候选", len(candidates))
        return candidates

    def get_detail(self, sub_id: int) -> tuple[str, str]:
        params = urllib.parse.urlencode({"id": sub_id})
        url = f"{self.cfg.base_url}/v1/sub/detail?{params}"
        resp = self._request_json(url, "/v1/sub/detail")
        if not isinstance(resp, dict):
            raise AssrtError(
                "详情接口返回非 JSON 对象",
                code="parse_error",
                endpoint="/v1/sub/detail",
                retryable=False,
            )
        subs = _extract_subs(resp)
        if not subs:
            raise AssrtError(
                f"详情接口未返回字幕条目 sub_id={sub_id}",
                code="detail_empty",
                endpoint="/v1/sub/detail",
                retryable=False,
            )
        sub = subs[0]

        filelist = sub.get("filelist")
        if isinstance(filelist, list) and filelist:
            first = filelist[0]
            if isinstance(first, dict):
                filename = str(first.get("f") or "").strip()
                dl_url = str(first.get("url") or "").strip()
                if dl_url:
                    return (filename or _filename_from_url(dl_url)), dl_url

        dl_url = str(sub.get("url") or "").strip()
        if not dl_url:
            raise AssrtError(
                f"详情接口未返回下载直链 sub_id={sub_id}",
                code="detail_no_url",
                endpoint="/v1/sub/detail",
                retryable=False,
            )
        return _filename_from_url(dl_url), dl_url

    def download_file(self, direct_url: str) -> bytes:
        endpoint = urllib.parse.urlparse(direct_url).path or "/download"
        return self._download(direct_url, endpoint)


def _transport_detail(cfg: AssrtConfig) -> str:
    """错误详情里的传输标记（非敏感），让降级连接在错误结构里可见。"""
    if cfg.insecure_no_sni_direct:
        return f"transport={TRANSPORT_INSECURE_NO_SNI}"
    return ""


# ---------------------------------------------------------------- 模块入口


def search(cfg: AssrtConfig, query: VideoQuery | str) -> list[SubtitleCandidate]:
    """搜索 assrt 字幕。

    返回的 SubtitleCandidate.sid 形如 ``assrt:12345``。
    缺凭据/缺依赖/网络错误均抛 AssrtError，由编排层记入 site_errors。
    """
    if not cfg.enabled:
        raise AssrtError(
            "assrt 功能未启用（assrt.enabled=false）",
            code="site_disabled",
            retryable=False,
        )
    if not cfg.token.strip():
        raise AssrtError(
            "assrt 缺少 token（assrt.token / ASSRT_TOKEN 均为空）",
            code="missing_credentials",
            retryable=False,
        )
    client = AssrtClient(cfg)
    return client.search(query)


def fetch(cfg: AssrtConfig, sid: str) -> tuple[str, bytes]:
    """下载 assrt 字幕，返回 (文件名, 原始字节)。

    sid 形如 ``assrt:12345``（兼容旧 ``shooter:12345``）。
    原始字节可能是压缩包或裸字幕，由调用方按归档/格式嗅探分流。
    """
    if not cfg.enabled:
        raise AssrtError(
            "assrt 功能未启用（assrt.enabled=false）",
            code="site_disabled",
            retryable=False,
        )
    if not cfg.token.strip():
        raise AssrtError(
            "assrt 缺少 token（assrt.token / ASSRT_TOKEN 均为空）",
            code="missing_credentials",
            retryable=False,
        )
    sub_id = _strip_sid_prefix(sid)
    client = AssrtClient(cfg)
    filename, dl_url = client.get_detail(sub_id)
    if not dl_url.startswith(("http://", "https://")):
        raise AssrtError(
            "详情接口返回非法下载直链",
            code="detail_no_url",
            endpoint="/v1/sub/detail",
            retryable=False,
        )
    content = client.download_file(dl_url)
    logger.info("assrt 下载完成 sid=%s 文件名=%r 字节=%d", sid, filename, len(content))
    return filename, content


def score_candidates(
    candidates: list[SubtitleCandidate], query: VideoQuery
) -> list[SubtitleCandidate]:
    """按统一评分函数排序（与编排层基准一致，供独立调用）。"""
    scored = [replace(c, score=score_candidate(c, query)) for c in candidates]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored
