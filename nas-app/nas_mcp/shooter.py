"""射手网（伪，assrt.net）字幕搜索与下载。

链路（比 SubHD 简单——纯 REST API，无会话/验证中转）：
  GET /sub/search?q=...&token=...  → JSON 搜索结果列表
  GET /sub/{id}?token=...          → JSON 详情（含下载直链 url）
  GET 直链                         → 字幕文件正文（部分条目返回压缩包，部分直接返回文本）

安全约束：
  - 总开关 shooter.enabled=false 时所有工具直接拒绝；
  - 仅需 token 认证，无 Cookie/会话需求；
  - 按 min_request_interval_seconds 限速；
  - 5xx/网络层错误自动重试，4xx/解析错误不重试；
  - 使用 urllib.request，无第三方依赖。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from .config import ShooterConfig
from .subhd import SubtitleCandidate, VideoQuery, score_candidate

logger = logging.getLogger("nas_mcp.shooter")


# 重试前固定退避（秒）。仅瞬时故障才重试：网络层失败（无 HTTP 状态码——
# 超时/断连/不可达）或服务端 5xx；4xx（认证/不存在/超限）与解析错误不重试。
_RETRY_BACKOFF_SECONDS = 1.0

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


class ShooterError(Exception):
    """射手网操作失败（网络/解析/认证等）。

    code 用于 server 层错误映射：默认 "shooter_error"；
    可区分场景传专用码（如详情无直链的 "detail_no_url"），
    Agent 按错误码决策而不是解析笼统 message。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: bytes = b"",
        code: str = "shooter_error",
    ):
        self.status_code = status_code
        self.body = body
        self.code = code
        super().__init__(message)


def _shooter_retryable(exc: ShooterError) -> bool:
    """该错误是否属于值得重试的瞬时故障。"""
    return exc.status_code is None or exc.status_code >= 500


def _map_langlist(langlist: Any) -> tuple[str, ...]:
    """把 assrt.net 的 langlist（{"zh-CN": 1, "en": 1}）映射为内部语言代码元组。

    langlist 的键可能是 BCP-47 风格（zh-CN/en）、也可能是中文描述（简体/双语），
    统一映射到内部代码；无法识别的键若是 zh/en/ja/ko 子串则原样保留，
    其余忽略（避免噪声进入候选语言列表）。
    """
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
        # 未在映射表：若是标准语言子串（含 zh/en/ja/ko），保留原样
        lowered = k.lower()
        if any(tag in lowered for tag in ("zh", "en", "ja", "ko")):
            found.add(k)
    return tuple(sorted(found))


def _extract_subs(resp: dict) -> list[dict]:
    """从响应 JSON 中提取 subs 列表，兼容 {sub:{subs:[...]}} 与 {subs:[...]} 两种形态。"""
    sub = resp.get("sub")
    if isinstance(sub, dict):
        subs = sub.get("subs")
    elif isinstance(sub, list):
        subs = sub
    else:
        subs = resp.get("subs")
    return [s for s in (subs or []) if isinstance(s, dict)]


def _filename_from_url(url: str) -> str:
    """从 URL 路径推导文件名（详情接口单文件形态可能不带 f 字段）。"""
    path = urllib.parse.urlparse(url).path
    name = PurePosixPath(path).name
    return name if name else "subtitle"


def _strip_shooter_prefix(sid: str) -> int:
    """从 sid（如 "shooter:12345"）剥离前缀取数字 id；非法则抛 ShooterError。"""
    raw = sid.strip()
    if raw.lower().startswith("shooter:"):
        raw = raw[len("shooter:"):].strip()
    try:
        return int(raw)
    except ValueError:
        raise ShooterError(
            f"非法的 Shooter sid: {sid!r}（应为 shooter:数字ID 形态）",
            code="invalid_sid",
        )


class ShooterClient:
    """射手网（assrt.net）HTTP 客户端：token 认证 + 限速 + 自动重试。

    与 SubHD 不同：assrt.net 是纯 REST API，无需 CookieJar/会话，
    每次 GET 带 token 查询参数即可。
    """

    def __init__(self, cfg: ShooterConfig):
        self.cfg = cfg
        self._opener = urllib.request.build_opener()
        self._last_request_at = 0.0

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.cfg.min_request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _open_once(self, req: urllib.request.Request) -> tuple[bytes, str]:
        """单次 HTTP 打开；失败统一转 ShooterError（带 status_code 供重试判定）。"""
        try:
            with self._opener.open(req, timeout=self.cfg.timeout_seconds) as resp:
                body = resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            raise ShooterError(
                f"HTTP {exc.code}: {exc.reason}",
                status_code=exc.code,
                body=body[:500],
            ) from exc
        except TimeoutError as exc:
            raise ShooterError(f"请求超时（>{self.cfg.timeout_seconds}s）") from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            # 3.12 起 RemoteDisconnected 等 ConnectionError 可能不经 URLError 包装直接冒泡
            reason = getattr(exc, "reason", exc)
            raise ShooterError(f"网络请求失败: {reason}") from exc
        return body, content_type

    def _request_json(self, url: str) -> Any:
        """带限速+重试的 GET 请求，返回解析后的 JSON。"""
        self._rate_limit()
        req = urllib.request.Request(url, method="GET")
        last_exc: ShooterError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                body, _ = self._open_once(req)
                break
            except ShooterError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries and _shooter_retryable(exc):
                    logger.warning(
                        "Shooter 请求失败（第 %d 次尝试，%.0fs 后重试）: %s",
                        attempt + 1, _RETRY_BACKOFF_SECONDS, exc,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        else:
            raise last_exc

        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ShooterError(f"JSON 解析失败: {exc}") from exc

    def _download(self, url: str) -> bytes:
        """带限速+重试的 GET 下载，返回原始字节。"""
        self._rate_limit()
        req = urllib.request.Request(url, method="GET")
        last_exc: ShooterError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                body, _ = self._open_once(req)
                return body
            except ShooterError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries and _shooter_retryable(exc):
                    logger.warning(
                        "Shooter 下载失败（第 %d 次尝试，%.0fs 后重试）: %s",
                        attempt + 1, _RETRY_BACKOFF_SECONDS, exc,
                    )
                    time.sleep(_RETRY_BACKOFF_SECONDS)
                    continue
                raise
        raise last_exc  # 不应到达

    # ---- 高层 API ----

    def search(self, keyword: str) -> list[SubtitleCandidate]:
        """搜索射手网，返回候选列表。

        GET /sub/search?token=...&q=...
        响应：{"sub": {"subs": [{"id", "native_name", "origin_name", "langlist", ...}]}}
        """
        params = urllib.parse.urlencode({"token": self.cfg.token, "q": keyword})
        url = f"{self.cfg.base_url}/sub/search?{params}"
        resp = self._request_json(url)
        if not isinstance(resp, dict):
            raise ShooterError("搜索接口返回非 JSON 对象")

        subs = _extract_subs(resp)
        candidates: list[SubtitleCandidate] = []
        for sub in subs:
            sid_num = sub.get("id")
            if sid_num is None:
                continue
            native_name = str(sub.get("native_name") or "").strip()
            origin_name = str(sub.get("origin_name") or "").strip()
            title = native_name or origin_name or f"shooter:{sid_num}"
            release_name = origin_name or native_name
            languages = _map_langlist(sub.get("langlist"))
            candidates.append(
                SubtitleCandidate(
                    sid=f"shooter:{sid_num}",
                    title=title,
                    release_name=release_name,
                    languages=languages,
                    format="",  # 搜索结果不含格式信息，下载后才知
                    source="Shooter",
                    site="shooter",  # 显式站点，避免落入 dataclass 默认值 subhd（#7）
                )
            )
        logger.info("Shooter 搜索 %r：%d 条", keyword, len(candidates))
        return candidates

    def get_detail(self, sub_id: int) -> tuple[str, str]:
        """获取字幕详情，返回 (文件名, 下载直链 URL)。

        GET /sub/{id}?token=...
        响应有两种形态：
          filelist（多文件）：subs[0].filelist = [{"f": "name.srt", "url": "..."}]
          单文件：subs[0].url = "..."
        多文件时取 filelist[0] 的 f/url；文件名缺失则从 URL 路径推导。
        """
        params = urllib.parse.urlencode({"token": self.cfg.token})
        url = f"{self.cfg.base_url}/sub/{sub_id}?{params}"
        resp = self._request_json(url)
        if not isinstance(resp, dict):
            raise ShooterError("详情接口返回非 JSON 对象")

        subs = _extract_subs(resp)
        if not subs:
            raise ShooterError(
                f"详情接口未返回字幕条目 sub_id={sub_id}", code="detail_empty"
            )
        sub = subs[0]

        # 优先 filelist（多文件），其次单一 url
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
            raise ShooterError(
                f"详情接口未返回下载直链 sub_id={sub_id}", code="detail_no_url"
            )
        return _filename_from_url(dl_url), dl_url

    def download_file(self, direct_url: str) -> bytes:
        """下载直链返回原始字节（字幕正文或压缩包，由调用方后续处理）。"""
        return self._download(direct_url)


# ---------------------------------------------------------------- 模块入口


def search(
    cfg: ShooterConfig,
    keyword: str,
    query: VideoQuery | None = None,
) -> list[SubtitleCandidate]:
    """搜索射手网字幕。

    keyword 为自由搜索词；query 用于评分排序（可选——传入则按 score_candidate
    打分并降序排列）。
    返回的 SubtitleCandidate.sid 形如 "shooter:12345"，供后续 fetch 使用。
    """
    if not cfg.enabled:
        raise ShooterError(
            "Shooter 功能未启用（shooter.enabled=false）", code="site_disabled"
        )
    if not keyword.strip():
        raise ShooterError("搜索关键词为空", code="invalid_argument")

    client = ShooterClient(cfg)
    candidates = client.search(keyword)
    if query is not None:
        candidates = [
            replace(c, score=score_candidate(c, query)) for c in candidates
        ]
        candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates


def fetch(cfg: ShooterConfig, sid: str) -> tuple[str, bytes]:
    """下载射手网字幕，返回 (文件名, 原始字节)。

    sid 形如 "shooter:12345"，自动剥离 shooter: 前缀取数字 id。
    原始字节可能是压缩包（rar/zip）或裸字幕正文，由调用方按
    sniff_archive_format / sniff_subtitle_format 分流处理。
    """
    if not cfg.enabled:
        raise ShooterError(
            "Shooter 功能未启用（shooter.enabled=false）", code="site_disabled"
        )

    sub_id = _strip_shooter_prefix(sid)
    client = ShooterClient(cfg)
    filename, dl_url = client.get_detail(sub_id)
    content = client.download_file(dl_url)
    logger.info(
        "Shooter 下载完成 sid=%s 文件名=%r 字节=%d", sid, filename, len(content)
    )
    return filename, content
