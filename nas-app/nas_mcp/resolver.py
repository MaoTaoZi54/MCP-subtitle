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

import copy
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import TmdbConfig, TvmazeConfig
from .security import InvalidArguments

logger = logging.getLogger("nas_mcp.resolver")

_VALID_MEDIA_TYPES = ("movie", "tv")

# ---------------------------------------------------------------- 类型强提示（#1）
# 行业惯例（Sonarr/Jellyfin/TinyMediaManager）：文件名或路径出现 SxxExx / Season N /
# 第N季 等剧集标记时强制走 /search/tv，绝不给同名电影抢实体的机会——否则
# "文件名无年份 + 目录名有年份"的剧集会落到同名电影上（如 Cold Case 2003 剧集
# 被解析成 2026 同名电影 tmdb_id=1563291）。
_SXXEXX_RE = re.compile(r"(?:^|[^a-z0-9])s\d{1,2}\s*e\d{1,3}(?:[^a-z0-9]|$)", re.IGNORECASE)
_SEASON_DIR_RE = re.compile(r"(?:^|[^a-z0-9])season\s*\d+|第\s*\d+\s*季", re.IGNORECASE)

# OPEN-1：低置信阈值——TMDB 全等命中但票数低于该值时标记 low_confidence。
# 典型场景：中文查询撞上冷门同名条目（如「流人」→ 2004 日本电影《るにん》，
# 4 票全等占坑），调用方据此回退原始关键词，避免拿垃圾结果当确认答案。
_LOW_CONFIDENCE_VOTES = 10


def infer_media_type(
    *,
    title: str = "",
    path: str = "",
    season: int | None = None,
    episode: int | None = None,
) -> str:
    """从文件名/路径信号推断 media_type 搜索域提示。

    命中剧集标记（SxxExx / Season N / 第N季 / 显式季集号）→ "tv"；
    识别不出 → ""（保持两域混合搜索的旧行为）。
    """
    if season is not None or episode is not None:
        return "tv"
    haystack = f"{title} {path}"
    if _SXXEXX_RE.search(haystack) or _SEASON_DIR_RE.search(haystack):
        return "tv"
    return ""


# ---------------------------------------------------------------- 解析缓存（#1）
# 同一实体在一次进程生命周期内只查一次 TMDB：audit/region/missing 等多个工具
# 对同库文件重复解析同一片名时共享结果，保证三处拿到的是同一个实体。
_RESOLVE_CACHE: dict[tuple, dict] = {}
_RESOLVE_CACHE_MAX = 512


def clear_resolve_cache() -> None:
    """清空进程级解析缓存（配置变更/测试用）。"""
    _RESOLVE_CACHE.clear()


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
    # 实体来源：tmdb（首选）/ tvmaze（TMDB 不可达或查不到时的回落源）。
    # tmdb_id 字段承载对应源的实体 ID（TVmaze 时为 show id）。
    metadata_source: str = "tmdb"


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

    def tv_season(self, tv_id: int, season_number: int) -> dict:
        """GET /tv/{tv_id}/season/{season_number}：返回整季详情，含 episodes[]。

        供 media_missing_episodes 取 TMDB 官方集数列表（expected_episode_set），
        不得用本地最大集数推断。404（季不存在）等失败抛 ResolverError。
        """
        return self._get_json(f"/tv/{tv_id}/season/{int(season_number)}", {})


# ---------------------------------------------------------------- TVmaze Client（回落源）


class TvmazeClient:
    """TVmaze 只读客户端：免 API key，提供剧集级（季+集号）数据。

    接口：
      GET {base_url}/search/shows?q=...        模糊搜索（含中文片名）
      GET {base_url}/shows/{id}/episodes       整部剧的集列表（season/number）
    """

    def __init__(self, cfg: TvmazeConfig):
        self.cfg = cfg

    def _get_json(self, path: str, params: dict[str, str] | None = None):
        query = urllib.parse.urlencode(params or {})
        url = f"{self.cfg.base_url}{path}" + (f"?{query}" if query else "")
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "nas-mcp/0.19"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_seconds) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise ResolverError(f"TVmaze HTTP {exc.code}", status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise ResolverError(f"TVmaze 网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ResolverError(
                f"TVmaze 请求超时（>{self.cfg.timeout_seconds}s）"
            ) from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ResolverError(f"TVmaze JSON 解析失败: {exc}") from exc

    @staticmethod
    def _normalize(show: dict, score: float) -> dict:
        """把 TVmaze show 映射成与 TMDB 条目同构的字段（供 _pick_best/_to_identity）。"""
        premiered = str(show.get("premiered") or "")
        network = show.get("network") if isinstance(show.get("network"), dict) else {}
        country = network.get("country") if isinstance(network.get("country"), dict) else {}
        country_code = country.get("code") if isinstance(country, dict) else None
        name = str(show.get("name") or "")
        return {
            "id": int(show.get("id") or 0),
            "title": name,
            "name": name,
            "original_title": name,
            "original_name": name,
            "release_date": premiered,
            "first_air_date": premiered,
            "popularity": score,       # TVmaze 无票数，用搜索相关度评分代替热度
            "vote_count": 0,
            "original_language": None,
            "origin_country": [country_code] if country_code else [],
            "tvmaze_score": score,
        }

    def search_tv(self, query: str, year: int | None = None) -> list[dict]:
        data = self._get_json("/search/shows", {"q": query})
        items: list[dict] = []
        for entry in data if isinstance(data, list) else []:
            show = entry.get("show") if isinstance(entry, dict) else None
            if not isinstance(show, dict) or not show.get("id"):
                continue
            items.append(self._normalize(show, float(entry.get("score") or 0.0)))
        if year:
            # search 接口不支持年份参数：本地把年份一致的候选排到前面
            items.sort(key=lambda it: str(it.get("first_air_date") or "")[:4] != str(year))
        return items

    def tv_season(self, tv_id: int, season_number: int) -> dict:
        """返回 {"episodes": [{"episode_number": n}, ...]}，供缺集检测取期望集号集合。"""
        data = self._get_json(f"/shows/{int(tv_id)}/episodes")
        episodes = [
            {"episode_number": int(ep.get("number") or 0)}
            for ep in (data if isinstance(data, list) else [])
            if isinstance(ep, dict)
            and ep.get("season") == int(season_number)
            and ep.get("number")
        ]
        if not episodes:
            raise ResolverError(f"TVmaze 未找到第 {season_number} 季集数（show={tv_id}）")
        return {"episodes": episodes, "season_number": int(season_number)}


# ---------------------------------------------------------------- 解析逻辑


def _year_of(item: dict, media_type: str) -> int | None:
    date = item.get("release_date") if media_type == "movie" else item.get("first_air_date")
    if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def _to_identity(
    item: dict, media_type: str, input_title: str, *, source: str = "tmdb"
) -> MediaIdentity:
    return MediaIdentity(
        input_title=input_title,
        canonical_title=str(item.get("title") or item.get("name") or ""),
        original_title=str(item.get("original_title") or item.get("original_name") or ""),
        year=_year_of(item, media_type),
        media_type=media_type,
        tmdb_id=int(item.get("id", 0)),
        original_language=item.get("original_language"),
        origin_country=tuple(item.get("origin_country") or []),
        metadata_source=source,
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
    # NEW-F: 当存在多个精确匹配时，按 (popularity, vote_count) 降序优先挑选最热门条目
    if len(exact) > 1:
        exact.sort(
            key=lambda c: (
                float(c[0].get("popularity") or 0.0),
                int(c[0].get("vote_count") or 0),
            ),
            reverse=True,
        )
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


def _match_type(query: str, item: dict) -> str:
    """OPEN-1：标注命中方式，供调用方判断可信度。

    exact = 标题/原名与输入全等；fuzzy = 未全等（含年份/热度兜底选出）。
    """
    nq = _norm(query)
    if (
        _norm(str(item.get("title") or item.get("name") or "")) == nq
        or _norm(str(item.get("original_title") or item.get("original_name") or "")) == nq
    ):
        return "exact"
    return "fuzzy"


def _confidence_fields(query: str, item: dict, *, source: str = "tmdb") -> dict:
    """OPEN-1：从原始条目提取置信度三件套 + low_confidence 标记。

    TVmaze 不提供票数（vote_count 恒为 0），改用匹配方式判定置信度：
    精确命中不算低置信，模糊命中才算——避免每条回落结果都被打成低置信。
    """
    try:
        votes = int(item.get("vote_count") or 0)
    except (TypeError, ValueError):
        votes = 0
    try:
        popularity = float(item.get("popularity") or 0.0)
    except (TypeError, ValueError):
        popularity = 0.0
    match_type = _match_type(query, item)
    if source == "tvmaze":
        low_confidence = match_type != "exact"
    else:
        low_confidence = votes < _LOW_CONFIDENCE_VOTES
    return {
        "popularity": popularity,
        "vote_count": votes,
        "match_type": match_type,
        "low_confidence": low_confidence,
    }


def _validate_params(
    cfg: TmdbConfig,
    query: str,
    year: int | None,
    media_type: str,
    tvmaze: TvmazeConfig | None = None,
) -> tuple[str, int | None]:
    """resolve 共用的参数校验（缓存命中前也必须执行，保证行为一致）。"""
    query = (query or "").strip()
    if not query:
        raise InvalidArguments("影视名称 query 不能为空")
    tvmaze_can_serve = (
        tvmaze is not None and tvmaze.enabled and media_type in ("", "tv")
    )
    if not cfg.enabled and not tvmaze_can_serve:
        raise InvalidArguments(
            "tmdb.enabled=false，未启用影视识别（请配置 tmdb 段或 tvmaze 段）"
        )
    if media_type and media_type not in _VALID_MEDIA_TYPES:
        raise InvalidArguments(
            f"media_type 只能是 movie/tv（留空自动判断），得到: {media_type!r}"
        )
    if year is not None and not (1878 <= year <= 2100):
        raise InvalidArguments(f"year 非法: {year!r}")
    return query, year


def resolve(
    cfg: TmdbConfig,
    query: str,
    *,
    year: int | None = None,
    media_type: str = "",
    tvmaze: TvmazeConfig | None = None,
) -> dict:
    """把用户输入的影视名解析成标准实体。

    media_type 为 "movie"/"tv" 时只查对应类型，否则两者都查后合并。
    无法确认时返回 resolved=false + candidates（交给 Octop 决策，不强行猜测）。

    TVmaze 回落（tvmaze.enabled=true 且搜索域为 tv/自动时生效）：
      - TMDB 不可达或候选无法确认 → 用 TVmaze 再解析一次；
      - tmdb.enabled=false 但 tvmaze.enabled=true → 直接走 TVmaze；
      - TVmaze 也失败时，若 TMDB 是网络错误则抛出原 TMDB 错误（保持旧失败语义），
        若 TMDB 只是“查不到”则返回原 resolved=false 结果。
    结果里的 identity.metadata_source 标明实体来自哪个源。
    """
    query, year = _validate_params(cfg, query, year, media_type, tvmaze)
    if not cfg.enabled:
        return _resolve_with_tvmaze(tvmaze, query, year)

    tmdb_error: ResolverError | None = None
    tmdb_result: dict | None = None
    try:
        tmdb_result = _resolve_tmdb(cfg, query, year, media_type)
    except ResolverError as exc:
        tmdb_error = exc
        logger.warning("TMDB 解析失败，尝试 TVmaze 回落: %s", exc)

    fallback_allowed = (
        tvmaze is not None
        and tvmaze.enabled
        and media_type in ("", "tv")
        and (tmdb_result is None or not tmdb_result.get("resolved"))
    )
    if fallback_allowed:
        try:
            tv_result = _resolve_with_tvmaze(tvmaze, query, year)
        except ResolverError as exc:
            logger.warning("TVmaze 回落也失败: %s", exc)
            tv_result = None
        if tv_result is not None and tv_result.get("resolved"):
            tv_result["fallback_from"] = "tmdb"
            if tmdb_result is not None:
                tv_result["tmdb_candidates"] = tmdb_result.get("candidates", [])
            return tv_result
        if tmdb_result is None:
            # 两个源都失败：保留原 TMDB 失败语义（抛 ResolverError）
            raise tmdb_error

    if tmdb_result is None:
        raise tmdb_error
    return tmdb_result


def _resolve_tmdb(
    cfg: TmdbConfig, query: str, year: int | None, media_type: str
) -> dict:
    """TMDB 解析（原 resolve 主体）。"""
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

    # NEW-F: 当跨媒体类型（未显式限定 media_type）搜索时，按 (popularity, vote_count)
    # 降序合并排序候选列表，避免首个类型（movie）排满前 5 个候选并挤掉更热门的剧集
    if not media_type and candidates:
        candidates.sort(
            key=lambda c: (
                float(c[0].get("popularity") or 0.0),
                int(c[0].get("vote_count") or 0),
            ),
            reverse=True,
        )

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
            "metadata_source": "tmdb",
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
    # OPEN-1：置信度三件套随 identity 下发，调用方可据此决定是否采信
    confidence = _confidence_fields(query, best[0])
    logger.info(
        "resolve %r → %s (%s, %s, tmdb=%d) match=%s votes=%d low_confidence=%s",
        query, identity.canonical_title, identity.year, identity.media_type,
        identity.tmdb_id, confidence["match_type"], confidence["vote_count"],
        confidence["low_confidence"],
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
            "metadata_source": identity.metadata_source,
            # OPEN-1：popularity / vote_count / match_type / low_confidence
            **confidence,
        },
        "candidates": candidate_list,
        "metadata_source": identity.metadata_source,
    }


def _resolve_with_tvmaze(tvmaze: TvmazeConfig, query: str, year: int | None) -> dict:
    """TVmaze 解析：结构与 TMDB 路径一致（identity/candidates/confidence）。"""
    client = TvmazeClient(tvmaze)
    items = client.search_tv(query, year)
    candidates: list[tuple[dict, str]] = [
        (item, "tv") for item in items[:10] if isinstance(item, dict) and item.get("id")
    ]
    best = _pick_best(candidates, query, year)
    candidate_list = [
        {
            "canonical_title": str(item.get("title") or item.get("name") or ""),
            "original_title": str(item.get("original_title") or item.get("original_name") or ""),
            "year": _year_of(item, "tv"),
            "media_type": "tv",
            "tmdb_id": int(item.get("id", 0)),
            "original_language": item.get("original_language"),
            "origin_country": list(item.get("origin_country") or []),
            "metadata_source": "tvmaze",
        }
        for item, _ in candidates[:5]
    ]
    if best is None:
        logger.info("TVmaze 回落 %r 无法确认（候选 %d 个）", query, len(candidates))
        return {
            "ok": True,
            "resolved": False,
            "input_title": query,
            "identity": None,
            "candidates": candidate_list,
            "metadata_source": "tvmaze",
        }

    identity = _to_identity(best[0], "tv", query, source="tvmaze")
    confidence = _confidence_fields(query, best[0], source="tvmaze")
    logger.info(
        "TVmaze 回落 %r → %s (%s) tvmaze_id=%d match=%s low_confidence=%s",
        query, identity.canonical_title, identity.year, identity.tmdb_id,
        confidence["match_type"], confidence["low_confidence"],
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
            "metadata_source": "tvmaze",
            **confidence,
        },
        "candidates": candidate_list,
        "metadata_source": "tvmaze",
    }


def resolve_tvmaze(
    tvmaze: TvmazeConfig, query: str, *, year: int | None = None
) -> dict:
    """只用 TVmaze 解析（tmdb.enabled=false 但 tvmaze.enabled=true 时使用）。"""
    query = query.strip()
    if not query:
        raise InvalidArguments("影视名称 query 不能为空")
    if not tvmaze.enabled:
        raise InvalidArguments("tvmaze.enabled=false，未启用 TVmaze 回落源")
    return _resolve_with_tvmaze(tvmaze, query, year)


def resolve_cached(
    cfg: TmdbConfig,
    query: str,
    *,
    year: int | None = None,
    media_type: str = "",
    tvmaze: TvmazeConfig | None = None,
) -> dict:
    """resolve() 的进程级缓存封装：同一 (片名, 年份, 搜索域, 数据源) 只查一次。

    返回深拷贝，调用方修改不影响缓存。参数校验与 resolve 完全一致（缓存命中
    也照样抛 InvalidArguments）。网络/解析错误不缓存，下次调用照常重试。
    缓存键带上 TVmaze 源标识，避免 TMDB 不可达时的回落结果与直连结果串味。
    """
    query, year = _validate_params(cfg, query, year, media_type, tvmaze)
    source_key = tvmaze.base_url if (tvmaze is not None and tvmaze.enabled) else "-"
    key = (cfg.base_url, cfg.language, query.casefold(), year, media_type, source_key)
    hit = _RESOLVE_CACHE.get(key)
    if hit is not None:
        return copy.deepcopy(hit)
    result = resolve(cfg, query, year=year, media_type=media_type, tvmaze=tvmaze)
    if len(_RESOLVE_CACHE) >= _RESOLVE_CACHE_MAX:
        _RESOLVE_CACHE.clear()
    _RESOLVE_CACHE[key] = result
    return copy.deepcopy(result)
