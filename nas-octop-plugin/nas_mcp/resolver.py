"""MediaResolver（P3：resolve_media）：把中文影视名解析成标准实体。

设计（规格 §19-§23）：
- 中文片名不能直接机器翻译成英文搜索名，必须通过影视数据库（TMDB）确认；
- 统一输出 MediaIdentity：canonical_title / original_title / year / media_type / tmdb_id；
- 作为公共能力同时服务 Jackett 资源搜索与 SubHD 字幕搜索；
- 解析不出时不强行猜测：resolved=false 并返回候选列表，交给 Octop 决策；
- 解析优先级（§22）：TMDB 精确匹配 > TMDB 模糊匹配 > 年份联合匹配 > 无法确认；
- API Key 绝不进入日志与错误消息。

TMDB API（v3）：
  GET {base_url}/search/movie?api_key=...&query=...&year=...
  GET {base_url}/search/tv?api_key=...&query=...&first_air_date_year=...
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import TmdbConfig
from .security import InvalidArguments

logger = logging.getLogger("nas_mcp.resolver")

_VALID_MEDIA_TYPES = ("movie", "tv")


# ---------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class MediaIdentity:
    """标准影视实体（规格 §20）。"""

    input_title: str          # 用户输入的原始名称
    canonical_title: str      # 标准显示名（中文环境下通常是中文译名）
    original_title: str       # 原始语言名（用于 Jackett/SubHD 搜索）
    year: int | None
    media_type: str           # movie / tv
    tmdb_id: int
    original_language: str | None = None   # TMDB 原语言代码（zh/en/ja...，供国产识别用）
    origin_country: tuple[str, ...] = ()   # 产出国家/地区代码（TMDB 主要剧集有，电影搜索无）
    resolved: bool = True


class ResolverError(Exception):
    """TMDB 解析失败（网络/认证/解析等）。status_code 为 HTTP 状态码（如有）。"""

    def __init__(self, message: str, *, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


# ---------------------------------------------------------------- TMDB Client


class TmdbClient:
    """TMDB API v3 只读客户端（stdlib urllib，无额外依赖）。"""

    def __init__(self, cfg: TmdbConfig):
        self.cfg = cfg

    def _get_json(self, path: str, params: dict[str, str]):
        query = dict(params)
        query["api_key"] = self.cfg.api_key
        query["language"] = self.cfg.language
        url = f"{self.cfg.base_url}{path}?{urllib.parse.urlencode(query)}"
        # 错误消息只含状态码，绝不包含 URL（api_key 在 query 里，§42）
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                msg = f"TMDB 认证失败（HTTP {exc.code}）：API Key 无效"
            else:
                msg = f"TMDB HTTP {exc.code}"
            raise ResolverError(msg, status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise ResolverError(f"TMDB 网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ResolverError(f"TMDB 请求超时（>{self.cfg.timeout_seconds}s）") from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ResolverError(f"TMDB JSON 解析失败: {exc}") from exc

    def search_movie(self, query: str, year: int | None) -> list[dict]:
        params: dict[str, str] = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = str(year)
        data = self._get_json("/search/movie", params)
        results = data.get("results") if isinstance(data, dict) else None
        return results if isinstance(results, list) else []

    def search_tv(self, query: str, year: int | None) -> list[dict]:
        params: dict[str, str] = {"query": query, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = str(year)
        data = self._get_json("/search/tv", params)
        results = data.get("results") if isinstance(data, dict) else None
        return results if isinstance(results, list) else []


# ---------------------------------------------------------------- 解析逻辑


def _year_of(item: dict, media_type: str) -> int | None:
    date = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
    if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _to_identity(item: dict, media_type: str, input_title: str) -> MediaIdentity:
    return MediaIdentity(
        input_title=input_title,
        canonical_title=str(item.get("title") or item.get("name") or ""),
        original_title=str(item.get("original_title") or item.get("original_name") or ""),
        year=_year_of(item, media_type),
        media_type=media_type,
        tmdb_id=int(item.get("id", 0)),
        original_language=item.get("original_language"),
        origin_country=tuple(item.get("origin_country") or []),
    )


def _norm(s: str) -> str:
    return "".join(s.lower().split())


def _pick_best(
    candidates: list[tuple[dict, str]],  # (item, media_type)
    query: str,
    year: int | None,
) -> tuple[dict, str] | None:
    """按 §22 优先级挑选：精确匹配（标题/原名）> 年份一致 > 热度最高。

    candidates 已按 TMDB 返回顺序（大致热度）排列。
    """
    if not candidates:
        return None
    nq = _norm(query)

    # 1. 精确匹配：标题或原名与输入完全一致（忽略大小写/空白）
    exact = [
        (item, mt)
        for item, mt in candidates
        if _norm(str(item.get("title") or item.get("name") or "")) == nq
        or _norm(str(item.get("original_title") or item.get("original_name") or "")) == nq
    ]
    pool = exact or candidates

    # 2. 年份联合匹配：调用方给了年份时优先年份一致的
    if year:
        by_year = [(item, mt) for item, mt in pool if _year_of(item, mt) == year]
        if by_year:
            return by_year[0]
        if exact:
            # 有精确匹配但年份不符：仍返回精确匹配，但交由候选列表体现歧义
            return exact[0]
        # 模糊匹配且年份不符：无法确认
        return None

    return pool[0]


def resolve(
    cfg: TmdbConfig,
    query: str,
    *,
    year: int | None = None,
    media_type: str = "",
) -> dict:
    """把用户输入的影视名解析成标准实体。

    media_type 为 "movie"/"tv" 时只查对应类型，否则两者都查后合并。
    无法确认时返回 resolved=false + candidates（交给 Octop 决策，不强行猜测）。
    """
    query = query.strip()
    if not query:
        raise InvalidArguments("影视名称 query 不能为空")
    if not cfg.enabled:
        raise InvalidArguments("tmdb.enabled=false，未启用影视识别（请配置 tmdb 段）")
    if media_type and media_type not in _VALID_MEDIA_TYPES:
        raise InvalidArguments(
            f"media_type 只能是 movie/tv（留空自动判断），得到: {media_type!r}"
        )
    if year is not None and not (1878 <= year <= 2100):
        raise InvalidArguments(f"year 非法: {year!r}")

    client = TmdbClient(cfg)
    candidates: list[tuple[dict, str]] = []
    types_to_query = [media_type] if media_type else list(_VALID_MEDIA_TYPES)
    for mt in types_to_query:
        try:
            items = (
                client.search_movie(query, year) if mt == "movie"
                else client.search_tv(query, year)
            )
        except ResolverError:
            raise
        for item in items[:10]:
            if isinstance(item, dict) and item.get("id"):
                candidates.append((item, mt))

    best = _pick_best(candidates, query, year)
    candidate_list = [
        {
            "canonical_title": str(item.get("title") or item.get("name") or ""),
            "original_title": str(item.get("original_title") or item.get("original_name") or ""),
            "year": _year_of(item, mt),
            "media_type": mt,
            "tmdb_id": int(item.get("id", 0)),
            "original_language": item.get("original_language"),
            "origin_country": list(item.get("origin_country") or []),
        }
        for item, mt in candidates[:5]
    ]

    if best is None:
        logger.info("resolve %r 无法确认（候选 %d 个）", query, len(candidates))
        return {
            "ok": True,
            "resolved": False,
            "input_title": query,
            "identity": None,
            "candidates": candidate_list,
        }

    identity = _to_identity(best[0], best[1], query)
    logger.info(
        "resolve %r → %s (%s, %s, tmdb=%d)",
        query, identity.canonical_title, identity.year, identity.media_type, identity.tmdb_id,
    )
    return {
        "ok": True,
        "resolved": True,
        "input_title": query,
        "identity": {
            "canonical_title": identity.canonical_title,
            "original_title": identity.original_title,
            "year": identity.year,
            "media_type": identity.media_type,
            "tmdb_id": identity.tmdb_id,
            "original_language": identity.original_language,
            "origin_country": list(identity.origin_country),
        },
        "candidates": candidate_list,
    }
