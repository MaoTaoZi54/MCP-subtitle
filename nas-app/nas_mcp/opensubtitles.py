"""OpenSubtitles 字幕搜索与下载（REST API v1）。

链路（规格 §19/§20）：
  搜索 GET /api/v1/subtitles → 候选列表
  下载 POST /api/v1/auth/login → JWT → POST /api/v1/download → 直链 → 下载

安全约束：
  - 总开关 opensubtitles.enabled=false 时由上层编排拒绝，本模块不重复检查；
  - JWT 懒加载：首次下载调用时获取并缓存在客户端实例；
  - 按 min_request_interval_seconds 限速（与 SubhdClient 同模式）；
  - 5xx/网络错误重试，4xx 与解析错误不重试（与 SubhdClient 同逻辑）；
  - 复用 subhd.py 的共享数据结构与工具函数（SubtitleCandidate / VideoQuery 等）。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import OpensubtitlesConfig
from .subhd import SubtitleCandidate, VideoQuery, sniff_archive_format

logger = logging.getLogger("nas_mcp.opensubtitles")


# ---------------------------------------------------------------- 常量

# 重试前固定退避（秒）。仅瞬时故障才重试：网络层失败（无 HTTP 状态码——
# 超时/断连/不可达）或服务端 5xx；4xx（认证/不存在/超限）与解析错误不重试。
_RETRY_BACKOFF_SECONDS = 1.0

# 默认 User-Agent（OpenSubtitles REST API 要求 Api-Key 头，UA 用通用浏览器标识）
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# sid 前缀：候选 sid 统一带 opensubtitles:{file_id}，fetch 入参去掉前缀还原 file_id
_SID_PREFIX = "opensubtitles:"

# OpenSubtitles 语言代码 → 内部语言标签映射（zh-CN→zh-Hans 等）
_LANGUAGE_CODE_MAP: dict[str, str] = {
    "zh-cn": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh": "zh-Hans",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "fr": "fr",
    "de": "de",
    "es": "es",
    "ru": "ru",
    "ar": "ar",
    "th": "th",
    "vi": "vi",
    "pt-br": "pt-BR",
    "pt": "pt",
    "it": "it",
}

# Content-Disposition 文件名解析（兼容 filename= 与 filename*=UTF-8'' 两种形态）
_CONTENT_DISPOSITION_RE = re.compile(
    r"""filename\*?=(?:UTF-8'')?"?([^";]+)"?""",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- 错误类型


class OpensubtitlesError(Exception):
    """OpenSubtitles 操作失败（网络/解析/认证等）。

    镜像 SubhdError：status_code 用于重试判定（None=网络层、>=500=服务端错误），
    code 用于 server 层错误映射。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: bytes = b"",
        code: str = "opensubtitles_error",
    ):
        self.status_code = status_code
        self.body = body
        self.code = code
        super().__init__(message)


def _opensubtitles_retryable(exc: OpensubtitlesError) -> bool:
    """该错误是否属于值得重试的瞬时故障。"""
    return exc.status_code is None or exc.status_code >= 500


# ---------------------------------------------------------------- HTTP 客户端


class OpensubtitlesClient:
    """OpenSubtitles REST API 客户端：限速 + 重试 + JWT 懒加载缓存。"""

    def __init__(self, cfg: OpensubtitlesConfig):
        self.cfg = cfg
        self._opener = urllib.request.build_opener()
        self._last_request_at = 0.0
        self._jwt: str | None = None  # JWT 懒加载缓存

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.cfg.min_request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
        parse_json: bool = False,
    ) -> str | dict | bytes:
        """发送 HTTP 请求并返回响应（JSON 解析/原始字节/文本）。

        带 5xx/网络错误重试（max_retries 次），4xx 与解析错误不重试。
        """
        self._rate_limit()
        headers: dict[str, str] = {
            "User-Agent": _DEFAULT_USER_AGENT,
            "Api-Key": self.cfg.api_key,
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        last_exc: OpensubtitlesError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                body, content_type = self._open_once(req)
                break
            except OpensubtitlesError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries and _opensubtitles_retryable(exc):
                    logger.warning(
                        "OpenSubtitles 请求失败（第 %d 次尝试，%.0fs 后重试）: %s",
                        attempt + 1, _RETRY_BACKOFF_SECONDS, exc,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        else:
            raise last_exc  # type: ignore[misc]

        if parse_json:
            try:
                return json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OpensubtitlesError(f"JSON 解析失败: {exc}") from exc

        if "json" in content_type or "text" in content_type:
            return body.decode("utf-8", errors="replace")
        return body

    def _open_once(self, req: urllib.request.Request) -> tuple[bytes, str]:
        """单次 HTTP 打开；失败统一转 OpensubtitlesError（带 status_code 供重试判定）。"""
        try:
            with self._opener.open(req, timeout=self.cfg.timeout_seconds) as resp:
                body = resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            raise OpensubtitlesError(
                f"HTTP {exc.code}: {exc.reason}",
                status_code=exc.code,
                body=body[:500],
            ) from exc
        except TimeoutError as exc:
            raise OpensubtitlesError(
                f"请求超时（>{self.cfg.timeout_seconds}s）"
            ) from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            # 3.12 起 RemoteDisconnected 等 ConnectionError 可能不经 URLError 包装直接冒泡
            reason = getattr(exc, "reason", exc)
            raise OpensubtitlesError(f"网络请求失败: {reason}") from exc
        return body, content_type

    # ---- JWT 懒加载 ----

    def _ensure_jwt(self) -> str:
        """首次下载时获取 JWT 并缓存；后续调用复用缓存。

        POST /api/v1/auth/login → {"token": "JWT"}
        """
        if self._jwt:
            return self._jwt
        url = f"{self.cfg.base_url}/api/v1/auth/login"
        data = json.dumps(
            {"username": self.cfg.username, "password": self.cfg.password}
        ).encode("utf-8")
        resp = self._request(url, method="POST", data=data, parse_json=True)
        if not isinstance(resp, dict):
            raise OpensubtitlesError("登录响应非 JSON 对象")
        token = resp.get("token", "")
        if not token:
            raise OpensubtitlesError(
                "登录失败：响应未包含 token",
                code="auth_failed",
            )
        self._jwt = token
        logger.debug("OpenSubtitles JWT 获取成功")
        return self._jwt

    # ---- 直链下载 ----

    def download_link(self, link: str) -> tuple[bytes, str]:
        """下载直链内容，返回 (body, filename)。

        filename 优先取 Content-Disposition 头，其次 URL 末段。
        带 5xx/网络错误重试（与 SubhdClient.download_file 同逻辑）。
        直链是已签名的 CDN 链接，只需 User-Agent，不需要 Api-Key/Authorization。
        """
        self._rate_limit()
        req = urllib.request.Request(
            link,
            headers={"User-Agent": _DEFAULT_USER_AGENT, "Accept": "*/*"},
        )

        last_exc: OpensubtitlesError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                with self._opener.open(req, timeout=self.cfg.timeout_seconds) as resp:
                    body = resp.read()
                    cd = resp.headers.get("Content-Disposition") or ""
                    filename = (
                        _parse_content_disposition(cd) or _filename_from_url(link)
                    )
                    return body, filename
            except urllib.error.HTTPError as exc:
                last_exc = OpensubtitlesError(
                    f"下载失败 HTTP {exc.code}: {exc.reason}",
                    status_code=exc.code,
                    body=(exc.read() if exc.fp else b"")[:500],
                )
            except TimeoutError as exc:
                last_exc = OpensubtitlesError(
                    f"下载超时（>{self.cfg.timeout_seconds}s）"
                )
            except (urllib.error.URLError, ConnectionError) as exc:
                reason = getattr(exc, "reason", exc)
                last_exc = OpensubtitlesError(f"下载网络失败: {reason}")

            # 到这里 last_exc 必非 None（仅在 except 块中赋值后到达）
            assert last_exc is not None
            if attempt < self.cfg.max_retries and _opensubtitles_retryable(last_exc):
                logger.warning(
                    "OpenSubtitles 下载失败（第 %d 次尝试，%.0fs 后重试）: %s",
                    attempt + 1, _RETRY_BACKOFF_SECONDS, last_exc,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS)
                continue
            raise last_exc

        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------- 工具函数


def _filename_from_url(url: str) -> str:
    """从 URL 路径末段提取文件名。"""
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    return path.rstrip("/").rsplit("/", 1)[-1]


def _parse_content_disposition(header: str) -> str:
    """从 Content-Disposition 头提取文件名（兼容 filename= 与 filename*=UTF-8''）。"""
    if not header:
        return ""
    m = _CONTENT_DISPOSITION_RE.search(header)
    return m.group(1).strip() if m else ""


def _map_language(lang_code: str) -> tuple[str, ...]:
    """把 OpenSubtitles 语言代码映射为内部语言标签。"""
    key = lang_code.lower()
    mapped = _LANGUAGE_CODE_MAP.get(key)
    if mapped:
        return (mapped,)
    if lang_code:
        return (lang_code,)
    return ()


def _strip_sid_prefix(sid: str) -> str:
    """去掉 opensubtitles: 前缀，返回纯 file_id 字符串。"""
    if sid.startswith(_SID_PREFIX):
        return sid[len(_SID_PREFIX):]
    return sid


# ---------------------------------------------------------------- 搜索 API


def search(
    cfg: OpensubtitlesConfig,
    keyword: str,
    query: VideoQuery,
) -> list[SubtitleCandidate]:
    """搜索 OpenSubtitles，返回候选列表。

    根据 query.season 判断 type=tvshow|movie；带上年份/季/集参数提高精度。
    sid 统一带 opensubtitles: 前缀（opensubtitles:{file_id}）。
    若 keyword 形如 opensubtitles:xxx 则去掉前缀再搜索（防御性处理）。
    """
    if keyword.startswith(_SID_PREFIX):
        keyword = _strip_sid_prefix(keyword)

    client = OpensubtitlesClient(cfg)
    params: list[tuple[str, str]] = [("query", keyword)]
    if cfg.language:
        params.append(("languages", cfg.language))
    # 有季号 → 剧集；否则 → 电影
    if query.season is not None:
        params.append(("type", "tvshow"))
        params.append(("season", str(query.season)))
        if query.episode is not None:
            params.append(("episode", str(query.episode)))
    else:
        params.append(("type", "movie"))
    if query.year:
        params.append(("year", str(query.year)))

    url = f"{cfg.base_url}/api/v1/subtitles?" + urllib.parse.urlencode(params)
    resp = client._request(url, parse_json=True)
    if not isinstance(resp, dict):
        raise OpensubtitlesError("搜索响应非 JSON 对象")

    candidates: list[SubtitleCandidate] = []
    for item in resp.get("data", []):
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        file_name = str(attrs.get("file_name") or "").strip()
        release = str(attrs.get("release") or "").strip()
        lang_code = str(attrs.get("language") or "").strip()

        # file_id：优先取 files[0].file_id，回退到顶层 file_id
        file_id = attrs.get("file_id")
        files = attrs.get("files") or []
        if isinstance(files, list) and files:
            first = files[0]
            if isinstance(first, dict) and first.get("file_id") is not None:
                file_id = first["file_id"]
        if file_id is None:
            continue

        # 格式：从 file_name 扩展名提取
        ext = Path(file_name).suffix.lstrip(".").lower() if file_name else ""
        fmt = ext.upper() if ext else ""

        candidates.append(
            SubtitleCandidate(
                sid=f"{_SID_PREFIX}{file_id}",
                title=release or file_name,
                release_name=file_name,
                languages=_map_language(lang_code),
                format=fmt,
                source="OpenSubtitles",
                site="opensubtitles",  # 显式站点，避免落入 dataclass 默认值 subhd（#7）
            )
        )

    logger.info("OpenSubtitles 搜索 %r：%d 条", keyword, len(candidates))
    return candidates


# ---------------------------------------------------------------- 下载 API


def fetch(cfg: OpensubtitlesConfig, sid: str) -> tuple[str, bytes]:
    """下载 OpenSubtitles 字幕，返回 (文件名, 原始内容)。

    sid 形如 opensubtitles:12345，去掉前缀得到 file_id。
    链路：登录换 JWT → POST /api/v1/download 取直链 → 下载直链内容。
    返回的原始内容可能是 ZIP 归档或裸字幕，由调用方决定后续处理。
    """
    file_id_str = _strip_sid_prefix(sid)
    try:
        file_id_int = int(file_id_str)
    except ValueError as exc:
        raise OpensubtitlesError(
            f"非法的 file_id: {file_id_str!r}", code="invalid_file_id"
        ) from exc

    client = OpensubtitlesClient(cfg)
    jwt = client._ensure_jwt()

    # POST /api/v1/download 取直链
    url = f"{cfg.base_url}/api/v1/download"
    data = json.dumps({"file_id": file_id_int}).encode("utf-8")
    resp = client._request(
        url,
        method="POST",
        data=data,
        extra_headers={"Authorization": f"Bearer {jwt}"},
        parse_json=True,
    )
    if not isinstance(resp, dict):
        raise OpensubtitlesError("下载响应非 JSON 对象")
    link = str(resp.get("link") or "").strip()
    if not link.startswith(("http://", "https://")):
        raise OpensubtitlesError(f"下载响应返回非法直链: {link!r}")

    # 下载直链内容
    raw, filename = client.download_link(link)

    # 调试日志：识别内容类型（归档/裸字幕），帮助调用方决策后续处理
    archive_fmt = sniff_archive_format(raw)
    if archive_fmt:
        logger.info(
            "OpenSubtitles 下载 file_id=%s：%d bytes，归档=%s，文件名=%s",
            file_id_str, len(raw), archive_fmt, filename,
        )
    else:
        logger.info(
            "OpenSubtitles 下载 file_id=%s：%d bytes，非归档，文件名=%s",
            file_id_str, len(raw), filename,
        )
    return filename, raw
