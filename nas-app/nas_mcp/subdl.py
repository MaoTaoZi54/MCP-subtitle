"""SubDL 字幕搜索与下载（REST API v1）。

链路：
  GET  {base_url}/api/v1/subtitles?api_key=...&film_name=...&languages=ZH...
  GET  https://dl.subdl.com{subtitle.url}  → zip 归档或裸字幕

安全约束：
  - 总开关 subdl.enabled=false 时由本模块直接拒绝，不产生网络请求；
  - api_key 只出现在搜索 URL 查询参数或下载请求头中，绝不写日志；
  - 错误消息只含状态/类型，不包含响应原文与凭据；
  - 统一错误结构：code / retryable / site / endpoint / detail。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from .config import SubdlConfig
from .subhd import SubtitleCandidate, VideoQuery, score_candidate

logger = logging.getLogger("nas_mcp.subdl")

_SID_PREFIX = "subdl:"

# 重试前固定退避（秒）。仅瞬时故障才重试：网络层失败或 5xx；
# 4xx（认证/不存在/超限）与解析错误不重试。
_RETRY_BACKOFF_SECONDS = 1.0

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 依赖可用性开关：测试可 monkeypatch 为 False 模拟缺依赖，
# 触发 dependency_missing 降级（只影响 subdl 站）。
_HAS_URLLIB = True

# SubDL 语言代码/描述 → 内部语言标签
_LANGUAGE_MAP = {
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-hans": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hant": "zh-Hant",
    "zh-hk": "zh-Hant",
    "chinese": "zh-Hans",
    "chinese (simplified)": "zh-Hans",
    "chinese (traditional)": "zh-Hant",
    "en": "en",
    "english": "en",
    "ja": "ja",
    "japanese": "ja",
    "ko": "ko",
    "korean": "ko",
}

# Content-Disposition 文件名解析（兼容 filename= 与 filename*=UTF-8'' 两种形态）
_CONTENT_DISPOSITION_RE = re.compile(
    r"""filename\*?=(?:UTF-8'')?"?([^";]+)"?""", re.IGNORECASE
)

_TEXT_FORMATS = frozenset({"srt", "ass", "ssa", "vtt", "sub", "smi"})


class SubdlError(Exception):
    """SubDL 操作失败（网络/解析/认证/依赖等）。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: bytes = b"",
        code: str = "subdl_error",
        site: str = "subdl",
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


def _retryable(exc: SubdlError) -> bool:
    if exc.retryable is not None:
        return exc.retryable
    return exc.status_code is None or exc.status_code >= 500


def _ensure_dependencies() -> None:
    if not _HAS_URLLIB:
        raise SubdlError(
            "subdl 传输依赖不可用（urllib 缺失）",
            code="dependency_missing",
            retryable=False,
        )


def _map_language(lang_code: str, fallback: str) -> tuple[str, ...]:
    key = str(lang_code or "").strip().lower()
    mapped = _LANGUAGE_MAP.get(key)
    if mapped:
        return (mapped,)
    if fallback:
        return (_LANGUAGE_MAP.get(fallback.strip().lower(), "zh-Hans"),)
    return ()


def _format_from_name(name: str) -> str:
    suffix = Path(name).suffix.lstrip(".").lower()
    if suffix in _TEXT_FORMATS:
        return suffix.upper()
    return ""


def _filename_from_url(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    name = PurePosixPath(path).name
    return name or "subtitle"


def _parse_content_disposition(header: str) -> str:
    if not header:
        return ""
    m = _CONTENT_DISPOSITION_RE.search(header)
    return m.group(1).strip() if m else ""


def _normalize_sid_path(sid: str) -> str:
    """从 subdl: 前缀还原下载路径（/subtitle/...），并做 SSRF 收敛。"""
    raw = str(sid).strip()
    if raw.startswith(_SID_PREFIX):
        raw = raw[len(_SID_PREFIX):].strip()
    # 允许 sid 直接带完整 URL（便于人工排查），统一取 path
    parsed = urllib.parse.urlparse(raw)
    path = parsed.path if parsed.scheme in ("http", "https") else raw
    if not path.startswith("/subtitle/") or ".." in path or "\\" in path:
        raise SubdlError(
            f"非法的 subdl sid: {sid!r}（应为 subdl:/subtitle/... 形态）",
            code="invalid_sid",
            retryable=False,
        )
    return path


def _build_keyword(query: VideoQuery | str) -> str:
    if isinstance(query, str):
        return query.strip()
    return str(query.title or "").strip()


class SubdlClient:
    """SubDL HTTP 客户端：限速 + 重试 + 结构化错误。"""

    def __init__(self, cfg: SubdlConfig):
        self.cfg = cfg
        self._opener = urllib.request.build_opener()
        self._last_request_at = 0.0
        _ensure_dependencies()

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.cfg.min_request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _open_once(
        self,
        req: urllib.request.Request,
        endpoint: str,
        *,
        parse_disposition: bool = False,
    ) -> tuple[bytes, str, str]:
        try:
            with self._opener.open(req, timeout=self.cfg.timeout_seconds) as resp:
                body = resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()
                disposition = resp.headers.get("Content-Disposition") or ""
        except urllib.error.HTTPError as exc:
            err_body = exc.read() if exc.fp else b""
            code = "http_error"
            retryable = exc.code >= 500
            if exc.code in (401, 403):
                code, retryable = "auth_failed", False
            elif exc.code == 429:
                code, retryable = "rate_limited", True
            raise SubdlError(
                f"HTTP {exc.code}",
                status_code=exc.code,
                body=err_body[:500],
                code=code,
                endpoint=endpoint,
                retryable=retryable,
            ) from exc
        except TimeoutError as exc:
            raise SubdlError(
                f"请求超时（>{self.cfg.timeout_seconds}s）",
                code="timeout",
                endpoint=endpoint,
                retryable=True,
            ) from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            raise SubdlError(
                f"网络请求失败: {type(exc).__name__}",
                code="network_error",
                endpoint=endpoint,
                retryable=True,
            ) from exc
        return body, content_type, disposition if parse_disposition else ""

    def _request_json(self, url: str, endpoint: str) -> Any:
        self._rate_limit()
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": _DEFAULT_USER_AGENT},
        )
        last_exc: SubdlError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                body, _, _ = self._open_once(req, endpoint)
                break
            except SubdlError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries and _retryable(exc):
                    logger.warning(
                        "subdl 请求失败（第 %d 次尝试，%s）",
                        attempt + 1, type(exc).__name__,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        else:  # pragma: no cover
            raise last_exc  # type: ignore[misc]

        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SubdlError(
                "JSON 解析失败",
                code="parse_error",
                endpoint=endpoint,
                retryable=False,
                detail=type(exc).__name__,
            ) from exc

    def search(self, query: VideoQuery | str) -> list[SubtitleCandidate]:
        keyword = _build_keyword(query)
        if not keyword:
            raise SubdlError(
                "搜索关键词为空", code="invalid_argument", retryable=False
            )

        params: list[tuple[str, str]] = [
            ("api_key", self.cfg.api_key),
            ("film_name", keyword),
            ("languages", self.cfg.language),
            ("subs_per_page", "30"),
        ]
        if isinstance(query, VideoQuery):
            if query.year:
                params.append(("year", str(query.year)))
            if query.season is not None:
                params.append(("type", "tv"))
                params.append(("season_number", str(query.season)))
                if query.episode is not None:
                    params.append(("episode_number", str(query.episode)))
            else:
                params.append(("type", "movie"))

        url = f"{self.cfg.base_url}/api/v1/subtitles?" + urllib.parse.urlencode(params)
        resp = self._request_json(url, "/api/v1/subtitles")
        if not isinstance(resp, dict):
            raise SubdlError(
                "搜索接口返回非 JSON 对象",
                code="parse_error",
                endpoint="/api/v1/subtitles",
                retryable=False,
            )

        if resp.get("status") is False or resp.get("error"):
            status_code = resp.get("statusCode")
            error = str(resp.get("error") or "").lower()
            code = "api_error"
            retryable = False
            if status_code == 429 or error in ("rate_limited", "too_many_requests"):
                code, retryable = "rate_limited", True
            elif status_code in (401, 403) or error in ("not_authorized", "invalid_api_key"):
                code, retryable = "auth_failed", False
            raise SubdlError(
                "搜索接口返回错误状态 "
                f"{status_code if status_code is not None else 'unknown'}",
                status_code=status_code if isinstance(status_code, int) else None,
                code=code,
                endpoint="/api/v1/subtitles",
                retryable=retryable,
            )

        candidates: list[SubtitleCandidate] = []
        for item in resp.get("subtitles") or []:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("url") or "").strip()
            if not raw_url:
                continue
            path = urllib.parse.urlparse(raw_url).path
            if not path.startswith("/subtitle/"):
                continue
            release_name = str(item.get("release_name") or "").strip()
            name = str(item.get("name") or "").strip()
            title = release_name or name or path
            lang_raw = str(item.get("lang") or item.get("language") or "").strip()
            candidates.append(
                SubtitleCandidate(
                    sid=f"{_SID_PREFIX}{path}",
                    title=title,
                    release_name=release_name or name,
                    languages=_map_language(lang_raw, self.cfg.language),
                    format=_format_from_name(name),
                    source="SubDL",
                    detail_url=f"{self.cfg.download_base_url}{path}",
                    site="subdl",
                )
            )
        logger.info("subdl 搜索完成：%d 条候选", len(candidates))
        return candidates

    def download(self, sid: str) -> tuple[str, bytes]:
        path = _normalize_sid_path(sid)
        url = f"{self.cfg.download_base_url}{path}"
        endpoint = "/dl" + path
        self._rate_limit()
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                # dl.subdl.com 位于 Cloudflare 之后：仅 UA + Accept 会被挑战（403
                # "Just a moment..."），带上浏览器常见的 Accept-Language / Referer
                # 才会正常返回 zip。
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://subdl.com/",
                "User-Agent": _DEFAULT_USER_AGENT,
                # 用请求头传 key：不进入 URL/日志；付费 key 走账号下载额度
                "X-API-Key": self.cfg.api_key,
            },
        )
        last_exc: SubdlError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                body, _, disposition = self._open_once(
                    req, endpoint, parse_disposition=True
                )
                filename = _parse_content_disposition(disposition) or _filename_from_url(url)
                return filename, body
            except SubdlError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries and _retryable(exc):
                    logger.warning(
                        "subdl 下载失败（第 %d 次尝试，%s）",
                        attempt + 1, type(exc).__name__,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------- 模块入口


def search(cfg: SubdlConfig, query: VideoQuery | str) -> list[SubtitleCandidate]:
    """搜索 SubDL 字幕，返回 sid 形如 ``subdl:/subtitle/3197651-3213944.zip``。"""
    if not cfg.enabled:
        raise SubdlError(
            "SubDL 功能未启用（subdl.enabled=false）",
            code="site_disabled",
            retryable=False,
        )
    if not cfg.api_key.strip():
        raise SubdlError(
            "SubDL 缺少 api_key（subdl.api_key / SUBDL_API_KEY 均为空）",
            code="missing_credentials",
            retryable=False,
        )
    client = SubdlClient(cfg)
    return client.search(query)


def fetch(cfg: SubdlConfig, sid: str) -> tuple[str, bytes]:
    """下载 SubDL 字幕，返回 (文件名, 原始字节)。

    原始字节通常是 zip 归档，也可能是裸字幕，由调用方嗅探分流。
    """
    if not cfg.enabled:
        raise SubdlError(
            "SubDL 功能未启用（subdl.enabled=false）",
            code="site_disabled",
            retryable=False,
        )
    if not cfg.api_key.strip():
        raise SubdlError(
            "SubDL 缺少 api_key（subdl.api_key / SUBDL_API_KEY 均为空）",
            code="missing_credentials",
            retryable=False,
        )
    filename, content = SubdlClient(cfg).download(sid)
    logger.info("subdl 下载完成 sid=%s 文件名=%r 字节=%d", sid, filename, len(content))
    return filename, content


def score_candidates(
    candidates: list[SubtitleCandidate], query: VideoQuery
) -> list[SubtitleCandidate]:
    scored = [replace(c, score=score_candidate(c, query)) for c in candidates]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored
