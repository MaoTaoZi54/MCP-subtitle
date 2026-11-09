"""SubHD 字幕搜索与下载（M4）。

链路（规格 §19/§20）：
  搜索页 → 详情页 → prepare-download → /down 中转页 → down 验证 → 直链下载

安全约束：
  - 总开关 subhd.enabled=false 时所有工具直接拒绝；
  - 下载是写操作，受 enable_write 与路径白名单双重门控；
  - 全程共享 curl_cffi 持久 Session（Cookie + 连接池复用，safari TLS 指纹——
    chrome 指纹会被 prepare-download 403）+ Chrome UA，按
    min_request_interval_seconds 限速；跨境链路偶发握手停顿时，连接超时控制在
    connect_timeout_seconds 内，重试会重建连接（重新解析 DNS / 换边缘 IP）；
  - RAR 用 unar 解（7z/unrar-free 对 SubHD 的 RAR 有兼容问题），ZIP 用 zipfile；
  - 字幕落盘前统一转 UTF-8（SubHD 常见 UTF-16LE BOM，很多播放器不认）。
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from . import subhd_clearance as clearance_mod
from .config import SubhdConfig, TmdbConfig
from .fs_utils import write_bytes as safe_write_bytes
from .platform_hints import permission_hint_text
from .media import extract_language_tags, is_chinese
from .security import InvalidArguments, PathGuard

logger = logging.getLogger("nas_mcp.subhd")

# 可选依赖：curl_cffi 提供 Chrome 指纹 + 连接池复用。缺失时回退 urllib，
# 功能不受影响，但每个请求都要新建 TCP+TLS（老环境兼容路径）。
try:  # pragma: no cover - 取决于运行环境是否安装可选依赖
    from curl_cffi import requests as _cf_requests
except Exception:  # pragma: no cover
    _cf_requests = None


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class SubtitleCandidate:
    """搜索结果中的一条字幕候选。"""

    sid: str                    # SubHD 详情页 ID（如 OyVNRJ）
    title: str                  # 详情页标题（如 绝命毒师 第二季.Breaking.Bad.S02...）
    release_name: str           # 发布文件名（用于匹配）
    languages: tuple[str, ...]  # 语言标签（如 简体/英语/双语）
    format: str                 # 字幕格式（SRT/ASS/SSA/SUP 等）
    source: str                 # 来源（其他来源/YYeTs字幕组 等）
    score: float = 0.0          # 与目标视频的匹配度评分（0~1）
    detail_url: str = ""        # 详情页绝对 URL
    site: str = "subhd"         # 站点标识（subhd/opensubtitles/shooter）


@dataclass(frozen=True)
class VideoQuery:
    """从视频文件名解析出的搜索元数据。"""

    title: str                  # 片名（英文优先）
    year: int | None
    season: int | None
    episode: int | None
    resolution: str | None      # 1080p / 2160p / 720p ...
    source: str | None          # BluRay / WEB-DL / HDTV ...
    release_group: str | None   # 发布组（如 NOGRP / CRFW）


# ---------------------------------------------------------------- 文件名解析

_YEAR_RE = re.compile(r"(?:^|[\s\(\[\._\-])((?:19|20)\d{2})(?:$|[\s\)\]\._\-])")
_SEASON_EP_RE = re.compile(
    r"[Ss](\d{1,2})\s?[Ee](\d{1,3})"  # S02E13 / S2E3
    r"|[Ss](\d{1,2})\b"               # S02（整季包）
    r"|[Ee][Pp]?(\d{1,3})\b"          # EP13
)
_RESOLUTION_RE = re.compile(r"(\d{3,4})[pPiI]\b")
_SOURCE_RE = re.compile(
    r"\b(BluRay|Blu[-.]?Ray|BDRip|BRRip|WEB[-.]?DL|WEBRip|HDTV|HDRip|DVDRip|"
    r"REMUX|AMZN|NF|DSNP|ATVP|HMAX|PCOK)\b",
    re.IGNORECASE,
)
_GROUP_RE = re.compile(r"[-\[]([A-Za-z0-9_]+)[\]\.]?\s*$")
# 视频编码/音频编码/位深等技术标记：不是片名的一部分
_CODEC_RE = re.compile(
    r"\b(?:x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx|av1|"
    r"aac|ac3|eac3|dts|ddp?5[. ]?1|truehd|atmos|flac|"
    r"10bit|8bit|hdr10\+?|hdr|sdr|dv|dolby)\b",
    re.IGNORECASE,
)
# 中文剧纯数字集号：stem 末尾 1~3 位数字、前一字符非字母数字（如 雍正王朝01 → E01）。
# 仅在含 CJK 字符且无 SxxEyy 标记时启用——避免 Apollo 13 等英文片名被误判为剧集。
_NUM_EP_CJK_RE = re.compile(r"(?<![\da-zA-Z])(\d{1,3})$")
_CJK_RE = re.compile(r"[一-鿿]")
# 常见发布组黑名单（从文件名尾部剥离）
_GROUP_BLACKLIST = frozenset({
    "mkv", "mp4", "avi", "ts", "m2ts", "webm", "wmv", "flv",
    "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "dts",
    "10bit", "8bit", "hdr", "sdr", "dv", "dolby", "atmos", "ma",
    "proper", "repack", "internal", "limited", "extended", "uncut",
    "chd", "chp", "cmct", "frds", "beast", "hdhome", "hdctv",
})


# 已知媒体扩展名白名单：仅这些后缀被视为"文件扩展名"从 stem 截去。
# NEW-16 根因修复：parse_video_filename 也会被用于【目录名】（_show_dir_fingerprint、
# _is_misnamed_show_root 等），Path.stem 会把带点目录名的最后一段当扩展名截掉
# （"Repro.Show (2024)" → stem "Repro"，title/year 全部失真 → resolver 匹配失败
# → 幻影规范目录）。白名单只截真实媒体后缀，目录名不再失真；文件名行为不变。
_KNOWN_MEDIA_EXT_RE = re.compile(
    r"\.(?:mkv|mp4|avi|mov|wmv|flv|webm|mpg|mpeg|m2ts|mts|m2t|ts|vob|iso|"
    r"rmvb|rm|3gp|ogm|m4v|divx|f4v|asf|qt|tp|trp|"
    r"srt|ass|ssa|sub|sup|idx|vtt|smi|sbv|scc)$",
    re.IGNORECASE,
)


def parse_video_filename(filename: str) -> VideoQuery:
    """从视频文件名解析出搜索元数据（片名/年份/季集/分辨率/来源/发布组）。"""
    name = str(filename)
    m_ext = _KNOWN_MEDIA_EXT_RE.search(name)
    stem = name[:m_ext.start()] if m_ext else name

    year_m = _YEAR_RE.search(stem)
    year = int(year_m.group(1)) if year_m else None

    se_m = _SEASON_EP_RE.search(stem)
    season = int(se_m.group(1)) if se_m and se_m.group(1) else None
    episode = int(se_m.group(2)) if se_m and se_m.group(2) else None
    if season is None and se_m and se_m.group(3):
        season = int(se_m.group(3))
    if episode is None and se_m and se_m.group(4):
        episode = int(se_m.group(4))

    # 中文剧纯数字集号兜底：无 SxxEyy/EP 标记、stem 含 CJK、末尾 1~3 位数字
    # 且数字前非字母数字 → 第 1 季第 N 集（雍正王朝01 → S01E01）。
    # SxxEyy 优先；4 位年份/分辨率（1080p 等）天然不匹配；纯英文片名不启用。
    num_ep_span = None
    if season is None and episode is None and _CJK_RE.search(stem):
        m_num = _NUM_EP_CJK_RE.search(stem)
        if m_num and 1 <= int(m_num.group(1)) <= 999:
            season, episode = 1, int(m_num.group(1))
            num_ep_span = m_num.span()

    res_m = _RESOLUTION_RE.search(stem)
    resolution = f"{res_m.group(1)}p" if res_m else None

    src_m = _SOURCE_RE.search(stem)
    source = src_m.group(1).upper().replace("BLU-RAY", "BLURAY") if src_m else None

    grp_m = _GROUP_RE.search(stem)
    group = grp_m.group(1) if grp_m else None
    if group and group.lower() in _GROUP_BLACKLIST:
        group = None

    # 片名：有季集标记时取标记**之前**的部分——`剧名 SxxEyy 集标题 (...)` 结构里，
    # SxxEyy 之后是集标题+技术字段+发布组，整段丢弃（否则会拼进片名，BUG-7）。
    # 无季集标记（电影等）则从整个 stem 剥离技术字段。
    se_match = _SEASON_EP_RE.search(stem)
    title_part = stem[:se_match.start()] if se_match else stem
    if num_ep_span is not None:
        # 剥离中文纯数字集号尾巴（雍正王朝01 的 "01" 不进片名/TMDB 查询）
        title_part = title_part[: num_ep_span[0]]
    title = title_part
    for pat in (_YEAR_RE, _SEASON_EP_RE, _RESOLUTION_RE, _SOURCE_RE, _CODEC_RE):
        title = pat.sub(" ", title)
    if group:
        title = title[: title.rfind(group)].rstrip("-.[ ")
    title = re.sub(r"[._\-\[\]()]", " ", title)
    title = re.sub(r"\s{2,}", " ", title).strip()

    return VideoQuery(
        title=title,
        year=year,
        season=season,
        episode=episode,
        resolution=resolution,
        source=source,
        release_group=group,
    )


# ---------------------------------------------------------------- 搜索页解析


class _SearchResultParser(HTMLParser):
    """解析 SubHD 搜索结果页，提取候选条目。

    每个结果块大致结构：
      <div class="bg-white shadow-sm rounded-3 mb-4">
        <div class="row">
          <div class="col-2 ...">  <!-- 海报 -->
            <a href='/d/{id}'><img ... alt="中文名 English Name"></a>
          </div>
          <div class="col-lg-10">
            <a class="link-dark align-middle" href='/a/{sid}'>片名</a>
            <div class="view-text ...">
              <a href='/a/{sid}'>Release.Name.1080p.BluRay.x264-GROUP</a>
            </div>
            <div class="text-truncate py-2 f11">
              <span ...>其他来源</span>
              <span ...>双语</span>
              <span ...>简体</span><span ...>英语</span>
              <span ...>ASS</span>
            </div>
          </div>
        </div>
      </div>
    """

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.results: list[dict[str, Any]] = []
        self._in_result = False
        self._current: dict[str, Any] | None = None
        self._depth = 0
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._in_link = False
        self._img_alt: str | None = None
        self._in_span = False
        self._span_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)

        # 结果块入口
        if tag == "div" and "bg-white" in (attrs_dict.get("class") or ""):
            self._in_result = True
            self._depth = 1
            self._current = {
                "sid": "",
                "title": "",
                "release_name": "",
                "languages": [],
                "format": "",
                "source": "",
                "detail_url": "",
            }
            self._img_alt = None
            return

        if not self._in_result:
            return

        if tag == "div":
            self._depth += 1

        # 语言/格式/来源 badge
        if self._in_result and tag == "span":
            self._in_span = True
            self._span_text = []

        # 海报 img（alt 含中英片名）
        if tag == "img" and self._img_alt is None:
            self._img_alt = attrs_dict.get("alt") or ""

        # 链接：/a/{sid} 是详情页，/d/{id} 是条目页
        if tag == "a":
            href = attrs_dict.get("href") or ""
            cls = attrs_dict.get("class") or ""
            if "/a/" in href and self._current is not None:
                if not self._current["sid"]:
                    self._current["sid"] = href.rsplit("/a/", 1)[-1].strip("/")
                    self._current["detail_url"] = self.base_url + href
                self._in_link = True
                self._link_href = href
                self._link_text = []
            elif "/d/" in href and "link-dark" in cls and self._current is not None:
                # /d/ 链接的文本是条目名（如 绝命毒师 第一季）
                self._in_link = True
                self._link_href = href
                self._link_text = []

    def handle_data(self, data: str) -> None:
        if not self._in_result or self._current is None:
            return
        if self._in_link:
            self._link_text.append(data)
        if self._in_span:
            self._span_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_result or self._current is None:
            return

        if tag == "span" and self._in_span:
            self._in_span = False
            _classify_badge("".join(self._span_text).strip(), self._current)
            self._span_text = []
            return

        if tag == "a" and self._in_link:
            text = "".join(self._link_text).strip()
            if self._link_href and "/a/" in self._link_href:
                if not self._current["title"]:
                    self._current["title"] = text
                else:
                    # 第二个 /a/ 链接是 release name
                    self._current["release_name"] = text
            self._in_link = False
            self._link_href = None
            self._link_text = []
            return

        if tag == "div":
            self._depth -= 1
            if self._depth <= 0:
                self._in_result = False
                if self._current["sid"]:
                    # 从 poster alt 补全中英片名
                    if self._img_alt and not self._current["title"]:
                        self._current["title"] = self._img_alt
                    self.results.append(self._current)
                self._current = None

    def get_candidates(self) -> list[SubtitleCandidate]:
        out: list[SubtitleCandidate] = []
        for r in self.results:
            langs = _parse_language_badges(r.get("languages", []))
            out.append(
                SubtitleCandidate(
                    sid=r["sid"],
                    title=r["title"],
                    release_name=r["release_name"],
                    languages=langs,
                    format=r.get("format", ""),
                    source=r.get("source", ""),
                    detail_url=r["detail_url"],
                )
            )
        return out


_LANGUAGE_BADGE_TEXTS = frozenset({"简体", "繁体", "双语", "英语", "日文", "韩语"})
_FORMAT_BADGE_TEXTS = frozenset({"srt", "ass", "ssa", "sup", "vtt", "sub", "idx"})


def _classify_badge(text: str, current: dict[str, Any]) -> None:
    """把结果块里的 span badge 文本归类到 languages/format/source。

    真实站点来源标签不限于含"来源"二字（如 AI校对、YYeTs字幕组），
    因此只要不是语言和格式，就归入 source。
    """
    if not text:
        return
    if text in _LANGUAGE_BADGE_TEXTS or (
        text.lower() not in _FORMAT_BADGE_TEXTS and extract_language_tags(text)
    ):
        # 语言 badge（简体/繁体/双语/中英/简英/CHT/Big5/正體…）；
        # 未知写法只要能被语言识别器解析出标签，同样按语言归类
        if text not in current["languages"]:
            current["languages"].append(text)
    elif text.lower() in _FORMAT_BADGE_TEXTS:
        if not current["format"]:
            current["format"] = text.upper()
    else:
        # 来源/标记（AI校对、YYeTs字幕组、其他来源 等）；
        # 跳过纯数字/时间类噪声（评论数、点赞数、下载数、发布时间）
        if not current["source"] and not re.fullmatch(r"[\d\s:.\-/年月日]+", text):
            current["source"] = text


def _parse_language_badges(badges: list[str]) -> tuple[str, ...]:
    """把 SubHD 的语言 badge（简体/繁体/英语/双语…）映射为内部语言代码。"""
    mapping = {
        "简体": "zh-Hans", "简中": "zh-Hans", "简体中文": "zh-Hans",
        "gb": "zh-Hans", "gb2312": "zh-Hans",
        "繁体": "zh-Hant", "繁體": "zh-Hant", "繁體中文": "zh-Hant",
        "正體": "zh-Hant", "正体": "zh-Hant", "cht": "zh-Hant", "big5": "zh-Hant",
        "双语": "zh-Hans+en", "中英": "zh-Hans+en", "中英双语": "zh-Hans+en",
        "简英": "zh-Hans+en",
        "英语": "en", "英": "en", "日文": "ja", "日语": "ja", "韩语": "ko",
    }
    found: set[str] = set()
    for b in badges:
        b = b.strip()
        if not b:
            continue
        key = b.lower()
        if key in mapping:
            mapped = mapping[key]
            if "+" in mapped:
                found.update(mapped.split("+"))
            else:
                found.add(mapped)
        else:
            # 尝试从 badge 文本提取语言标签
            for tag in extract_language_tags(b):
                found.add(tag)
    return tuple(sorted(found))


# ---------------------------------------------------------------- 详情页解析


class _DetailPageParser(HTMLParser):
    """解析详情页，提取字幕文件列表（真实文件名）与 data-sid。

    支持两种官方形态：
      A（单文件）：<span class="subtitle-file-name">文件名.sup</span>
      B（多集）：  <a class="preview-file subtitle-file-name" data-filename="...">文件名.ass</a>
    核心规则：class 含 "subtitle-file-name" 的元素即文件名容器；
    优先取 data-filename 属性，fallback 取标签文本。
    """

    _SUBTITLE_EXTS = frozenset({".srt", ".ass", ".ssa", ".sup", ".vtt", ".sub", ".idx"})

    def __init__(self):
        super().__init__()
        self.sid: str = ""
        self.filenames: list[str] = []
        self._in_file_tag = False
        self._file_tag_text: list[str] = []
        self._pending_filename: str | None = None

    def _maybe_add(self, text: str) -> None:
        text = text.strip()
        if text and any(text.lower().endswith(ext) for ext in self._SUBTITLE_EXTS):
            self.filenames.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "button" and "data-sid" in attrs_dict:
            self.sid = attrs_dict["data-sid"] or ""

        cls = attrs_dict.get("class") or ""
        if "subtitle-file-name" in cls:
            # 优先用 data-filename 属性；否则进文本收集模式
            data_fn = attrs_dict.get("data-filename")
            if data_fn:
                self._pending_filename = data_fn.strip()
            else:
                self._in_file_tag = True
                self._file_tag_text = []

    def handle_data(self, data: str) -> None:
        if self._in_file_tag:
            self._file_tag_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._in_file_tag:
            self._in_file_tag = False
            self._maybe_add("".join(self._file_tag_text))
            self._file_tag_text = []
        if self._pending_filename is not None:
            self._maybe_add(self._pending_filename)
            self._pending_filename = None


# ---------------------------------------------------------------- HTTP 客户端


# 重试退避阶梯（秒），上限 3s：只对瞬时故障重试，避免一次链路停摆把整条
# 字幕链拖长。仅瞬时故障才重试：网络层失败（无 HTTP 状态码——超时/断连/
# 不可达）或服务端 5xx；4xx（认证/不存在/超限）与解析错误不重试。
_RETRY_BACKOFFS = (1.0, 2.0, 3.0)

# SubHD 专用 TLS 指纹：chrome/edge 指纹会被 prepare-download 判为爬虫返回 403，
# safari/firefox 指纹可全链路（搜索→详情→准备→验证→直链下载）通过。
_SUBDH_IMPERSONATE = "safari"


def _retry_backoff(attempt: int) -> float:
    """第 attempt 次重试前的退避秒数（0 基，上限 3s）。"""
    return _RETRY_BACKOFFS[min(max(attempt, 0), len(_RETRY_BACKOFFS) - 1)]


def _network_error_message(exc: BaseException, cfg: SubhdConfig) -> str:
    """把底层网络异常归一成用户可读文案（超时/其他连接错误）。"""
    text = str(exc)
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return (
            f"请求超时（连接 {cfg.connect_timeout_seconds:.1f}s / "
            f"读取 {cfg.timeout_seconds:.1f}s）: {text}"
        )
    return f"网络请求失败: {text}"


def _subhd_retryable(exc: "SubhdError") -> bool:
    """该错误是否属于值得重试的瞬时故障。"""
    return exc.status_code is None or exc.status_code >= 500


# Cloudflare 挑战页的特征标记：命中且会话无 cf_clearance 时直接判挑战，
# 不再针对 5xx 做连接重建重试（挑战不会因为重试而消失）。
_CHALLENGE_MARKERS = ("challenge-platform", "cf-chl-", "_cf_chl_opt")


def _body_has_challenge(body: bytes | str | None) -> bool:
    if not body:
        return False
    if isinstance(body, bytes):
        text = body[:20000].decode("utf-8", errors="replace")
    else:
        text = str(body)[:20000]
    return any(marker in text for marker in _CHALLENGE_MARKERS)


# 站点自身的下载频率限制（与 Cloudflare 无关）：403 + 该文案时不要续签浏览器，
# 那既不会让限流消失，还会浪费一次挑战。
_RATE_LIMIT_MARKERS = ("下载频率过高", "频率过高", "请求过于频繁", "too many requests")


def _body_has_rate_limit(body: bytes | str | None) -> bool:
    if not body:
        return False
    if isinstance(body, bytes):
        text = body[:2000].decode("utf-8", errors="replace")
    else:
        text = str(body)[:2000]
    text = text.lower()
    return any(marker.lower() in text for marker in _RATE_LIMIT_MARKERS)


def _rate_limit_error(endpoint: str, body: bytes | str | None = None) -> "SubhdError":
    return SubhdError(
        f"SubHD {endpoint} 被站点限流（HTTP 403）：下载频率过高，请稍后重试",
        status_code=403,
        body=body,
        code="subhd_rate_limited",
        site="subhd",
        endpoint=endpoint,
        retryable=True,
        detail="rate_limited",
    )


class SubhdClient:
    """SubHD HTTP 客户端：持久 Session（Cookie + 连接复用）+ Chrome UA + 限速。"""

    def __init__(self, cfg: SubhdConfig):
        self.cfg = cfg
        self._last_request_at = 0.0
        # Cloudflare 挑战凭据：浏览器续签成功后注入 Session，后续请求复用；
        # 单次客户端实例最多续签一次（_renewed），避免 403 时无限重试。
        self._clearance: clearance_mod.Clearance | None = None
        self._renewed = False
        self._clearance_error: clearance_mod.ClearanceError | None = None
        self._user_agent = cfg.user_agent
        if _cf_requests is not None:
            self._session = self._new_session()
            self._jar = None
            self._opener = None
        else:
            logger.warning("curl_cffi 未安装，SubHD 回退 urllib（每个请求新建连接）")
            self._session = None
            self._jar = CookieJar()
            self._opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(self._jar),
            )

    def _new_session(self):
        # SubHD 的 /api/sub/prepare-download 对 Chrome/Edge 系指纹返回 403
        # 「无法准备下载」（站点自身反爬，实测 safari/firefox 指纹可全链路通过），
        # 因此固定用 safari 指纹；UA 仍按配置覆盖。
        try:
            session = _cf_requests.Session(impersonate=_SUBDH_IMPERSONATE)
        except Exception:  # pragma: no cover - 老版本 curl_cffi 无该指纹
            logger.warning("curl_cffi 不支持 impersonate=%s，回退默认指纹", _SUBDH_IMPERSONATE)
            session = _cf_requests.Session()
        session.headers.update({"User-Agent": self._user_agent})
        return session

    def _reset_session(self) -> None:
        """重建连接池：重试时让 libcurl 重新解析 DNS / 落到另一个边缘 IP。

        HTTP 4xx 不重试；只有传输层失败才会走到这里。旧连接可能已半死，
        直接丢弃比在上头继续重试更可靠。
        """
        if self._session is None:
            return
        try:
            self._session.close()
        except Exception:  # pragma: no cover - 关闭失败不影响后续重建
            pass
        self._session = self._new_session()
        if self._clearance is not None:
            self._apply_clearance(self._clearance)

    def _rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.cfg.min_request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    # ---- Cloudflare 人机挑战（cf_clearance） ----

    def _warmup_url(self, sid: str) -> str:
        """首次挑战用详情页：与后续 prepare-download 的 Referer 同源。"""
        return f"{self.cfg.base_url}/a/{sid}"

    def _apply_clearance(self, clearance: clearance_mod.Clearance) -> None:
        """把浏览器 UA 与 CF cookie 注入当前 Session（curl_cffi / urllib 两条路径）。"""
        self._clearance = clearance
        self._user_agent = clearance.user_agent or self.cfg.user_agent
        if self._session is not None:
            try:
                self._session.headers["User-Agent"] = self._user_agent
            except Exception:  # pragma: no cover - 老版本 curl_cffi 头部接口差异
                pass
            for name, value, domain, path, _expires in clearance.cookies:
                try:
                    self._session.cookies.set(name, value, domain=domain, path=path or "/")
                except Exception:  # pragma: no cover - cookie jar 拒绝异常域
                    logger.warning("SubHD cf_clearance cookie 注入失败: %s", name)
            return

        # urllib 兼容路径：直接写入 CookieJar
        if self._jar is None:  # pragma: no cover - 理论不可达
            return
        for name, value, domain, path, expires in clearance.cookies:
            try:
                self._jar.set_cookie(Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=bool(domain),
                    domain_initial_dot=str(domain).startswith("."),
                    path=path or "/",
                    path_specified=True,
                    secure=True,
                    expires=int(expires) if expires else None,
                    discard=not expires,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": None},
                    rfc2109=False,
                ))
            except Exception:  # pragma: no cover - 兼容路径的容错
                logger.warning("SubHD cf_clearance cookie 注入失败: %s", name)

    def _ensure_clearance(self, sid: str) -> None:
        """首次请求前准备凭据：优先复用落盘，缺失/过期才启动浏览器。

        失败不在这里抛出：先按无凭据请求，若站点返回 403 再由
        ``_challenge_error`` 映射成可判断的错误码（browser_unavailable /
        cloudflare_challenge / clearance_expired）。
        """
        if self._clearance is not None:
            return
        try:
            stored = clearance_mod.load(self.cfg)
        except Exception as exc:  # pragma: no cover - 读盘异常不应阻断请求
            logger.warning("SubHD cf_clearance 读取异常: %s", exc)
            stored = None
        if stored is not None and stored.is_valid():
            self._apply_clearance(stored)
            logger.info(
                "SubHD 复用已落盘的 cf_clearance（到期 %s）",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stored.expires_at)),
            )
            return

        if not self.cfg.browser_renew_enabled:
            logger.info("SubHD 浏览器续签已关闭（subhd.browser_renew_enabled=false）")
            return
        if not clearance_mod.playwright_available():
            self._clearance_error = clearance_mod.ClearanceError(
                "未安装 playwright，无法自动通过 Cloudflare 挑战"
                "（pip install playwright 后再执行 playwright install chromium）",
                code="browser_unavailable",
                retryable=False,
            )
            logger.warning("SubHD 无法自动续签 cf_clearance：%s", self._clearance_error)
            return
        try:
            clearance = clearance_mod.acquire(self.cfg, self._warmup_url(sid))
        except clearance_mod.ClearanceError as exc:
            self._clearance_error = exc
            logger.warning("SubHD cf_clearance 获取失败（%s）：%s", exc.code, exc)
            return
        self._apply_clearance(clearance)

    def _can_renew(self) -> bool:
        return (
            self.cfg.browser_renew_enabled
            and clearance_mod.playwright_available()
        )

    def _renew_clearance(self, sid: str, endpoint: str = "prepare-download") -> None:
        """403 后强制续签一次；失败转结构化 SubhdError。"""
        self._renewed = True
        self._clearance = None
        self._user_agent = self.cfg.user_agent
        if self._session is not None:
            self._reset_session()
        try:
            clearance = clearance_mod.acquire(
                self.cfg, self._warmup_url(sid), force=True
            )
        except clearance_mod.ClearanceError as exc:
            clearance_mod.invalidate(self.cfg)
            raise SubhdError(
                f"Cloudflare 人机挑战未通过（{exc.code}）：{exc}",
                status_code=403,
                code=exc.code,
                site="subhd",
                endpoint=endpoint,
                retryable=exc.retryable,
                detail=exc.detail,
            ) from exc
        self._apply_clearance(clearance)

    def _challenge_error(self, original: "SubhdError", endpoint: str) -> "SubhdError":
        """把 403/挑战页映射为可判断的挑战错误（不泄露 cookie 值）。"""
        status = original.status_code or 403
        if self._clearance is None and self._clearance_error is not None:
            cause = self._clearance_error
            code = cause.code
            retryable = cause.retryable
            detail = cause.detail
            message = (
                f"SubHD {endpoint} 被 Cloudflare 挑战拦截（HTTP {status}），"
                f"且无法自动续签：{cause}"
            )
        elif self._renewed:
            code = "clearance_expired"
            retryable = True
            detail = "renewed_once"
            message = (
                f"SubHD {endpoint} 在重新签发 cf_clearance 后仍返回 {status}"
                "（站点挑战策略可能已升级，例如交互式 Turnstile）"
            )
        elif not self.cfg.browser_renew_enabled:
            code = "cloudflare_challenge"
            retryable = True
            detail = "browser_renew_disabled"
            message = (
                f"SubHD {endpoint} 被 Cloudflare 挑战拦截（HTTP {status}）；"
                "浏览器续签已关闭（subhd.browser_renew_enabled=false）"
            )
        else:
            code = "cloudflare_challenge"
            retryable = True
            detail = "clearance_missing"
            message = f"SubHD {endpoint} 被 Cloudflare 挑战拦截（HTTP {status}）"
        return SubhdError(
            message,
            status_code=status,
            body=original.body,
            code=code,
            site="subhd",
            endpoint=endpoint,
            retryable=retryable,
            detail=detail,
        )

    def _request_with_clearance(
        self,
        url: str,
        *,
        endpoint: str,
        sid: str,
        method: str = "GET",
        data: bytes | None = None,
        referer: str | None = None,
        parse_json: bool = False,
    ) -> str | dict | bytes:
        """带 CF 凭据的请求：403 时续签一次并重试，仍失败则给明确错误码。"""
        try:
            return self._request(
                url, method=method, data=data, referer=referer, parse_json=parse_json
            )
        except SubhdError as exc:
            if exc.status_code == 403 and _body_has_rate_limit(exc.body):
                # 站点自身的下载频控：与 CF 无关，续签浏览器无意义。
                raise _rate_limit_error(endpoint, exc.body) from exc
            if exc.status_code != 403:
                # 非 403 但响应体是 CF 挑战页（如 503）：会话无有效凭据时直接
                # 判挑战错误，避免 _request 对 5xx 做无谓的连接重建重试。
                if self._clearance is None and _body_has_challenge(exc.body):
                    raise self._challenge_error(exc, endpoint) from exc
                raise
            if not self._renewed and self._can_renew():
                logger.warning(
                    "SubHD %s 返回 403，清除旧凭据并重新签发 cf_clearance 后重试一次",
                    endpoint,
                )
                self._renew_clearance(sid, endpoint)
                try:
                    return self._request(
                        url, method=method, data=data, referer=referer, parse_json=parse_json
                    )
                except SubhdError as retry_exc:
                    if retry_exc.status_code == 403:
                        raise self._challenge_error(retry_exc, endpoint) from retry_exc
                    raise
            raise self._challenge_error(exc, endpoint) from exc

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        referer: str | None = None,
        parse_json: bool = False,
    ) -> str | dict | bytes:
        last_exc: SubhdError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            self._rate_limit()
            if attempt:
                # 传输层失败才走到这里：丢旧连接，重建后再试（重新解析 DNS）。
                self._reset_session()
                backoff = _retry_backoff(attempt - 1)
                time.sleep(backoff)
            try:
                body, content_type = self._open_once(
                    url, method=method, data=data, referer=referer
                )
                break
            except SubhdError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries and _subhd_retryable(exc):
                    logger.warning(
                        "SubHD 请求失败（第 %d/%d 次尝试，%.1fs 后重建连接重试）: %s",
                        attempt + 1, self.cfg.max_retries + 1,
                        _retry_backoff(attempt), exc,
                    )
                    continue
                raise
        else:
            raise last_exc

        if parse_json:
            try:
                return json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SubhdError(f"JSON 解析失败: {exc}") from exc

        if "html" in content_type or "text" in content_type:
            return body.decode("utf-8", errors="replace")
        return body

    def _open_once(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        referer: str | None = None,
    ) -> tuple[bytes, str]:
        """单次 HTTP 请求；失败统一转 SubhdError（带 status_code 供重试判定）。"""
        if self._session is not None:
            return self._open_once_cffi(url, method=method, data=data, referer=referer)
        return self._open_once_urllib(url, method=method, data=data, referer=referer)

    def _open_once_cffi(
        self,
        url: str,
        *,
        method: str,
        data: bytes | None,
        referer: str | None,
    ) -> tuple[bytes, str]:
        """curl_cffi 单次请求：连接超时涵盖 TCP 建连 + TLS 握手。"""
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = self._session.request(
                method,
                url,
                headers=headers,
                data=data,
                timeout=(self.cfg.connect_timeout_seconds, self.cfg.timeout_seconds),
            )
        except Exception as exc:  # curl 传输层异常统一归为可重试的网络失败
            raise SubhdError(_network_error_message(exc, self.cfg)) from exc
        status = int(getattr(resp, "status_code", 0) or 0)
        body = resp.content or b""
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if status >= 400:
            reason = getattr(resp, "reason", "") or ""
            raise SubhdError(
                f"HTTP {status}{f': {reason}' if reason else ''}",
                status_code=status,
                body=body[:500],
            )
        return body, content_type

    def _open_once_urllib(
        self,
        url: str,
        *,
        method: str,
        data: bytes | None,
        referer: str | None,
    ) -> tuple[bytes, str]:
        """无 curl_cffi 时的兼容路径（每个请求新建连接）。"""
        headers = {"User-Agent": self._user_agent}
        if referer:
            headers["Referer"] = referer
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.cfg.timeout_seconds) as resp:
                body = resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            raise SubhdError(
                f"HTTP {exc.code}: {exc.reason}",
                status_code=exc.code,
                body=body[:500],
            ) from exc
        except TimeoutError as exc:
            raise SubhdError(f"请求超时（>{self.cfg.timeout_seconds}s）") from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            # 3.12 起 RemoteDisconnected 等 ConnectionError 可能不经 URLError 包装直接冒泡
            reason = getattr(exc, "reason", exc)
            raise SubhdError(f"网络请求失败: {reason}") from exc
        return body, content_type

    # ---- 高层 API ----

    def search(self, keyword: str, page: int = 1) -> list[SubtitleCandidate]:
        """搜索 SubHD，返回候选列表。page 从 1 开始，路径式分页。"""
        encoded = urllib.parse.quote(keyword, safe="")
        if page <= 1:
            url = f"{self.cfg.base_url}/search/{encoded}"
        else:
            url = f"{self.cfg.base_url}/search/{encoded}/{page}"
        html = self._request(url)
        parser = _SearchResultParser(self.cfg.base_url)
        parser.feed(html)
        candidates = parser.get_candidates()
        logger.info("SubHD 搜索 %r 第 %d 页：%d 条", keyword, page, len(candidates))
        return candidates

    def get_detail(self, sid: str) -> dict:
        """获取详情页：sid、字幕文件列表。"""
        url = f"{self.cfg.base_url}/a/{sid}"
        html = self._request(url)
        parser = _DetailPageParser()
        parser.feed(html)
        return {
            "sid": parser.sid or sid,
            "filenames": parser.filenames,
        }

    def prepare_download(self, sid: str) -> str:
        """步骤 3：POST /api/sub/prepare-download，返回 /down/{sid} 中转路径。

        该端点受 Cloudflare JS 挑战保护：先确保会话带 cf_clearance，
        403 时只续签一次再试。挑战特征明显时不做无谓网络重试。
        """
        self._ensure_clearance(sid)
        url = f"{self.cfg.base_url}/api/sub/prepare-download"
        data = json.dumps({"sid": sid}).encode("utf-8")
        resp = self._request_with_clearance(
            url,
            endpoint="api/sub/prepare-download",
            sid=sid,
            method="POST",
            data=data,
            referer=f"{self.cfg.base_url}/a/{sid}",
            parse_json=True,
        )
        if not isinstance(resp, dict):
            raise SubhdError("prepare-download 返回非 JSON")
        if not resp.get("success"):
            msg = str(resp.get("msg", "未知错误"))
            if _body_has_rate_limit(msg):
                raise _rate_limit_error("api/sub/prepare-download")
            raise SubhdError(f"prepare-download 失败: {msg}")
        download_path = resp.get("url", "")
        if not download_path.startswith("/down/"):
            raise SubhdError(f"prepare-download 返回非法下载路径: {download_path!r}")
        return download_path

    def verify_download(self, sid: str, download_path: str) -> str:
        """步骤 4-5：访问中转页 → POST /api/sub/down，返回直链 URL。

        中转页必须带同一个 CookieJar + Referer，否则 403「下载页面已失效」。
        这两步同样受 cf_clearance 保护，403 时续签一次。
        """
        self._ensure_clearance(sid)
        # 步骤 4：GET /down/{sid}（中转页，内嵌 JS 指向验证接口）
        down_url = f"{self.cfg.base_url}{download_path}"
        referer = f"{self.cfg.base_url}/a/{sid}"
        self._request_with_clearance(
            down_url,
            endpoint=f"down/{sid}",
            sid=sid,
            referer=referer,
        )  # 保持会话

        # 步骤 5：POST /api/sub/down
        api_url = f"{self.cfg.base_url}/api/sub/down"
        data = json.dumps({"sid": sid}).encode("utf-8")
        resp = self._request_with_clearance(
            api_url,
            endpoint="api/sub/down",
            sid=sid,
            method="POST",
            data=data,
            referer=down_url,
            parse_json=True,
        )
        if not isinstance(resp, dict):
            raise SubhdError("down 验证返回非 JSON")
        if not resp.get("pass"):
            msg = str(resp.get("msg", "未知错误"))
            if _body_has_rate_limit(msg):
                raise _rate_limit_error("api/sub/down")
            raise SubhdError(f"down 验证失败: {msg}")
        direct_url = resp.get("url", "")
        if not direct_url.startswith(("http://", "https://")):
            raise SubhdError(f"down 验证返回非法直链: {direct_url!r}")
        return direct_url

    def download_file(self, direct_url: str, referer: str) -> tuple[bytes, dict]:
        """步骤 6：下载直链（RAR/ZIP 字幕包），带大小上限。

        返回 (内容, 响应元信息)；元信息含 status_code/content_type/content_length，
        供 NEW-19 非归档校验失败时给出可诊断错误。
        """
        last_exc: SubhdError | None = None
        for attempt in range(self.cfg.max_retries + 1):
            self._rate_limit()
            if attempt:
                self._reset_session()
                time.sleep(_retry_backoff(attempt - 1))
            try:
                return self._download_once(direct_url, referer)
            except SubhdError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries and _subhd_retryable(exc):
                    logger.warning(
                        "SubHD 下载失败（第 %d/%d 次尝试，%.1fs 后重建连接重试）: %s",
                        attempt + 1, self.cfg.max_retries + 1,
                        _retry_backoff(attempt), exc,
                    )
                    continue
                raise
        else:
            raise last_exc

    def _download_once(self, direct_url: str, referer: str) -> tuple[bytes, dict]:
        if self._session is not None:
            return self._download_once_cffi(direct_url, referer)
        return self._download_once_urllib(direct_url, referer)

    def _download_once_cffi(self, direct_url: str, referer: str) -> tuple[bytes, dict]:
        try:
            resp = self._session.get(
                direct_url,
                headers={"Referer": referer},
                stream=True,
                timeout=(self.cfg.connect_timeout_seconds, self.cfg.timeout_seconds),
            )
        except Exception as exc:
            raise SubhdError(f"下载网络失败: {_network_error_message(exc, self.cfg)}") from exc

        try:
            status = int(getattr(resp, "status_code", 0) or 0)
            if status >= 400:
                reason = getattr(resp, "reason", "") or ""
                raise SubhdError(
                    f"下载失败 HTTP {status}{f': {reason}' if reason else ''}",
                    status_code=status,
                )
            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > self.cfg.max_download_bytes:
                # 413 语义：非瞬时故障，不可重试
                raise SubhdError(
                    f"下载包超过大小上限（{self.cfg.max_download_bytes} bytes）",
                    status_code=413,
                )
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.cfg.max_download_bytes:
                    raise SubhdError(
                        f"下载包超过大小上限（{self.cfg.max_download_bytes} bytes）",
                        status_code=413,
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
            meta = {
                "status_code": status,
                "content_type": (resp.headers.get("Content-Type") or "").lower(),
                "content_length": int(content_length) if content_length else len(body),
            }
            return body, meta
        finally:
            try:
                resp.close()
            except Exception:  # pragma: no cover
                pass

    def _download_once_urllib(self, direct_url: str, referer: str) -> tuple[bytes, dict]:
        headers = {"User-Agent": self._user_agent, "Referer": referer}
        req = urllib.request.Request(direct_url, headers=headers)
        try:
            with self._opener.open(req, timeout=self.cfg.timeout_seconds) as resp:
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > self.cfg.max_download_bytes:
                    # 413 语义：非瞬时故障，不可重试
                    raise SubhdError(
                        f"下载包超过大小上限（{self.cfg.max_download_bytes} bytes）",
                        status_code=413,
                    )
                body = resp.read(self.cfg.max_download_bytes + 1)
                if len(body) > self.cfg.max_download_bytes:
                    raise SubhdError(
                        f"下载包超过大小上限（{self.cfg.max_download_bytes} bytes）",
                        status_code=413,
                    )
                meta = {
                    "status_code": getattr(resp, "status", None),
                    "content_type": (resp.headers.get("Content-Type") or "").lower(),
                    "content_length": int(content_length) if content_length else len(body),
                }
        except urllib.error.HTTPError as exc:
            raise SubhdError(
                f"下载失败 HTTP {exc.code}: {exc.reason}", status_code=exc.code
            ) from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SubhdError(f"下载网络失败: {reason}") from exc
        return body, meta


class SubhdError(Exception):
    """SubHD 操作失败（网络/解析/验证等）。

    code 用于 server 层错误映射（v0.11.18 NEW-19）：默认 "subhd_error"；
    可区分场景传专用码（如下载响应非归档的 "download_not_archive"），
    Agent 按错误码决策而不是解析笼统 message。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: bytes = b"",
        code: str = "subhd_error",
        site: str = "subhd",
        endpoint: str = "",
        retryable: bool | None = None,
        detail: str = "",
    ):
        self.status_code = status_code
        self.body = body
        self.code = code
        # 结构化错误元信息：让上层（编排层/Agent）能判断是哪个站点、哪个端点、
        # 是否值得重试，而不是只能解析笼统 message。
        self.site = site
        self.endpoint = endpoint
        self.retryable = retryable
        self.detail = detail
        super().__init__(message)


# ---------------------------------------------------------------- 评分排序

# 逐字段一致的"直接命中"分数线：最佳候选达到该分即不再发起别名/回退轮次
# （v0.11.18 NEW-20）
_EXACT_HIT_SCORE = 0.9

# #8：候选显式标了别的集/季（E11 vs 目标 E01）→ 最终分乘以该系数。
# 只惩罚"冲突"（显式不同），不惩罚"缺失"（全季包没有集号，由 pack_hint 交
# 调用方决策）——二者语义不同，混为一谈会让正确的单集字幕被全季包压过。
_CONFLICT_PENALTY = 0.5

# 候选文本中的季集标记：S02E11 / s02 e11（点号/空格分隔均可）
_CAND_EP_RE = re.compile(r"(?:^|[^a-z0-9])s(\d{1,2})\s*e(\d{1,3})", re.IGNORECASE)
# 独立集号：E11 / e11（前面不能是 s02 的数字部分）
_CAND_EP_ONLY_RE = re.compile(r"(?:^|[^a-z0-9])e(\d{1,3})(?![0-9])", re.IGNORECASE)
# 全季关键词（中英）：命中且无单集号 → 全季包
_PACK_KEYWORD_RE = re.compile(r"全季|整季|全集|合集|complete", re.IGNORECASE)
# 季标记（无集号也可能是全季包）：S02 / Season 2 / 第 2 季
_SEASON_ONLY_RE = re.compile(
    r"(?:^|[^a-z0-9])s\d{1,2}(?![0-9])|season\s*\d+|第\s*\d+\s*季", re.IGNORECASE
)


def _candidate_episodes(text: str) -> tuple[set[int], set[int]]:
    """从候选标题/发布名提取 (季集合, 集集合)；无标记则集合为空。"""
    seasons = {int(s) for s, _ in _CAND_EP_RE.findall(text)}
    episodes = {int(e) for _, e in _CAND_EP_RE.findall(text)}
    episodes |= {int(e) for e in _CAND_EP_ONLY_RE.findall(text)}
    return seasons, episodes


def is_season_pack(release_name: str, title: str = "") -> bool:
    """候选是否为全季/整季包：有季标记但无单集号，或含整季关键词（#8）。

    全季包在单集补全场景下不一定可用（需按集拆分，见 #10），评分不加不减，
    仅打 pack_hint 标记交调用方决策；显式冲突的单集（别的集号）另行降权。
    """
    text = f"{release_name} {title}"
    if _CAND_EP_RE.search(text):
        return False  # 有明确单集号 → 单集候选
    if _PACK_KEYWORD_RE.search(text):
        return True
    return bool(_SEASON_ONLY_RE.search(text))


def _norm_release(text: str) -> str:
    """发布名/标题规范化：小写、点/横线/下划线/括号统一为空格、压缩空白。

    SubHD 的 release_name 用点号分词（Special.Ops.Lioness.S01E02），
    而文件名解析出的片名是空格分词——不规范化则逐字段一致的候选也无法命中。
    """
    s = re.sub(r"[._\-\[\]()]+", " ", str(text).lower())
    return re.sub(r"\s{2,}", " ", s).strip()


def score_candidate(
    candidate: SubtitleCandidate,
    query: VideoQuery,
    release_hint: str | None = None,
) -> float:
    """按片名/年份/季集/分辨率/来源/发布组给候选打分（0~1）。

    避免只按文件名模糊匹配（规格 §19）。
    release_hint（v0.11.18 NEW-20）：video_path 模式传入视频 stem，
    候选 release_name 与之规范化相等（逐字段一致，含发布组/REPACK 标记）→ 直接满分。
    """
    # 规范化精确匹配：release_name 与 hint 逐字段一致 ≈ 满分
    if release_hint and _norm_release(candidate.release_name) == _norm_release(release_hint):
        return 1.0

    score = 0.0
    total_weight = 0.0

    def _add(weight: float, matched: bool) -> None:
        nonlocal score, total_weight
        total_weight += weight
        if matched:
            score += weight

    release = candidate.release_name.lower()
    title_lower = candidate.title.lower()
    release_norm = _norm_release(release)
    title_norm = _norm_release(title_lower)

    # 片名匹配（权重最高）——两侧规范化后子串匹配（NEW-20：空格 vs 点号）
    q_title = query.title.lower()
    q_title_norm = _norm_release(q_title)
    if q_title:
        _add(0.35, q_title_norm in release_norm or q_title_norm in title_norm)

    # 年份
    if query.year:
        _add(0.15, str(query.year) in release)

    # 季集
    if query.season is not None:
        s_pat = f"s{query.season:02d}"
        _add(0.10, s_pat in release or s_pat in title_lower)
    if query.episode is not None:
        e_pat = f"e{query.episode:02d}"
        _add(0.10, e_pat in release or e_pat in title_lower)

    # 分辨率
    if query.resolution:
        _add(0.10, query.resolution.lower() in release)

    # 来源（BluRay/WEB-DL 等）
    if query.source:
        src_lower = query.source.lower().replace("-", "").replace(".", "")
        release_dense = release.replace("-", "").replace(".", "").replace(" ", "")
        _add(0.10, src_lower in release_dense)

    # 发布组
    if query.release_group:
        _add(0.05, query.release_group.lower() in release)

    # 中文字幕加分（优先推荐有中文字幕的）
    has_zh = any(is_chinese(lang) for lang in candidate.languages)
    _add(0.05, has_zh)

    if total_weight == 0:
        return 0.0
    final = score / total_weight

    # #8：显式冲突降权——候选标了集/季但与目标不同（E11 vs E01）。
    # 候选完全没标集号（全季包）不算冲突，不降权（pack_hint 单独标记）；
    # 这样"冲突的单集"必然排在"正确单集"和"全季包"之后，
    # 调用方取 top 1 不会下到错误集数的字幕。
    cand_seasons, cand_episodes = _candidate_episodes(f"{release} {title_lower}")
    episode_conflict = (
        query.episode is not None
        and bool(cand_episodes)
        and query.episode not in cand_episodes
    )
    season_conflict = (
        query.season is not None
        and bool(cand_seasons)
        and query.season not in cand_seasons
    )
    if episode_conflict or season_conflict:
        final *= _CONFLICT_PENALTY
    return round(final, 3)


# ---------------------------------------------------------------- 解压与转码

# 归档魔数（NEW-19）：下载响应必须是其中之一，否则是反爬页/失效链接等
# —— RAR4/RAR5 共享前 7 字节 "Rar!\x1a\x07"
_ARCHIVE_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"Rar!\x1a\x07", "rar"),
    (b"PK\x03\x04", "zip"),
    (b"7z\xbc\xaf'\x1c", "7z"),
    (b"\x1f\x8b", "gzip"),
)


def sniff_archive_format(data: bytes) -> str | None:
    """按魔数嗅探归档真实格式（rar/zip/7z/gzip），不匹配返回 None（NEW-19）。"""
    for magic, fmt in _ARCHIVE_MAGICS:
        if data.startswith(magic):
            return fmt
    return None


def _body_summary(data: bytes, limit: int = 200) -> str:
    """响应 body 的 text 安全摘要：剥 HTML 标签、剔除不可打印字符、截断。"""
    text = data[: limit * 2].decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", " ", text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return re.sub(r"\s{2,}", " ", text).strip()[:limit]


# 反爬/登录页常见关键词（NEW-19）：命中则附进错误 message 帮助判断拦截原因
_HTML_HINT_KEYWORDS = (
    "登录", "登陆", "验证", "积分", "签到", "404", "not found",
    "forbidden", "captcha", "login", "cloudflare", "错误",
)


def extract_html_hints(data: bytes, content_type: str = "") -> str:
    """响应疑似 HTML 时提取 <title> 与拦截关键词，返回可读提示串；否则空串。"""
    text = data[:4096].decode("utf-8", errors="replace")
    if "html" not in content_type and not text.lstrip().startswith("<"):
        return ""
    parts: list[str] = []
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s{2,}", " ", m.group(1)).strip()[:120]
        if title:
            parts.append(f"title={title!r}")
    lowered = text.lower()
    hits = [kw for kw in _HTML_HINT_KEYWORDS if kw in lowered]
    if hits:
        parts.append(f"关键词={hits}")
    return " ".join(parts)


# ---------------------------------------------------------------- 归档内选择（#10）

# 字幕扩展名优先级：同分时优先 srt（播放器兼容性最好）
_SUBTITLE_EXT_PRIORITY = (
    "srt", "ass", "ssa", "vtt", "smi", "sbv", "scc", "sub", "idx", "sup",
)

# 语言标签 → 包内文件名常见写法（词边界匹配；判定顺序：简/繁/英 先于通用 zh）
_LANG_NAME_TOKENS: dict[str, tuple[str, ...]] = {
    "zh-Hans": ("chs", "sc", "gb", "gb2312", "zh-cn", "zh-hans", "简体", "简中"),
    "zh-Hant": ("cht", "tc", "big5", "zh-tw", "zh-hant", "繁体", "繁體", "繁中", "正體", "正体"),
    "zh": ("zh", "chi", "chinese", "中文"),
    "en": ("en", "eng", "english", "英文", "英语"),
}
# 双语标记：内容含中文，可满足中文请求
_BILINGUAL_TOKENS = ("dual", "bilingual", "双语", "中英")


def _name_has_token(low_name: str, tokens: tuple[str, ...]) -> bool:
    """在小写文件名中按词边界匹配任一标记（避免 en 命中 repack 等子串）。"""
    return any(
        re.search(rf"(?:^|[^a-z0-9]){re.escape(t)}(?:[^a-z0-9]|$)", low_name)
        for t in tokens
    )


def infer_language_tag(filename: str) -> str:
    """从包内字幕文件名推断语言标签（zh-Hans/zh-Hant/zh/en），判定不了返回空串。"""
    low = Path(str(filename)).name.lower()
    if not low:
        return ""
    for tag in ("zh-Hans", "zh-Hant", "en", "zh"):
        if _name_has_token(low, _LANG_NAME_TOKENS[tag]):
            return tag
    return ""


def language_compatible(actual: str, requested: str) -> bool:
    """实际语言与请求语言是否兼容：通用 zh 兼容任一中文请求；简繁互不兼容。"""
    if not actual or actual == requested:
        return True
    if requested.startswith("zh") and actual.startswith("zh"):
        return actual == "zh" or requested == "zh"
    return False


def report_language_tag(actual: str, requested: str) -> str:
    """OPEN-4：返回体 actual_language 的展示归一（不改变 language_compatible 判定）。

    - 判定不了语言（文件名无 token）→ 回退请求语言，避免返回空串；
    - 通用 zh 与请求 zh-Hans/zh-Hant 并存 → 按请求侧规范标签统一展示。
    """
    if not actual:
        return requested or "unknown"
    if actual == "zh" and requested in ("zh-Hans", "zh-Hant"):
        return requested
    return actual


def _archive_entry_score(name: str, video_stem: str, lang: str) -> int:
    """包内字幕文件与目标视频的匹配分（#10 选择依据，越大越优）。

    维度从重到轻：集号匹配/冲突（复用 #8 的 _candidate_episodes）>
    video_stem（release 级）整体命中 > 语言匹配 > 扩展名优先级。
    集号缺失（全季包/单文件）不罚——与 #8 同一语义，只有显式冲突才重罚。
    """
    low = Path(str(name)).name.lower()
    score = 0

    # 1) 集/季匹配：目标 E01 拿到 E11 直接出局级降权；无集号不加不减
    query = parse_video_filename(video_stem) if video_stem else None
    if query is not None and query.episode is not None:
        seasons, episodes = _candidate_episodes(low)
        if episodes:
            score += 100 if query.episode in episodes else -1000
        if query.season is not None and seasons:
            score += 40 if query.season in seasons else -400

    # 2) release 级精确命中：video_stem 整体出现在包内文件名里
    stem_key = _norm_release(Path(video_stem).name) if video_stem else ""
    if stem_key and stem_key in _norm_release(low):
        score += 60

    # 3) 语言匹配：明确兼容加分、明确冲突减分、判定不了不加不减
    if lang:
        actual = infer_language_tag(low)
        if actual and language_compatible(actual, lang):
            score += 30 if actual == lang else 15
        elif lang.startswith("zh") and _name_has_token(low, _BILINGUAL_TOKENS):
            score += 15
        elif actual:  # 明确但不兼容（如请求中文拿到纯英文）
            score -= 50

    # 4) 扩展名优先级兜底
    ext = Path(str(name)).suffix.lstrip(".").lower()
    if ext in _SUBTITLE_EXT_PRIORITY:
        score += 10 - _SUBTITLE_EXT_PRIORITY.index(ext)
    return score


def collect_archive_entries(
    archive_data: bytes,
    target_exts: frozenset[str],
) -> list[tuple[str, bytes]]:
    """收集归档内全部字幕条目，返回 [(包内文件名, 内容)]。

    ZIP 用 zipfile 标准库；ZIP 无命中则按 RAR 用 unar 解压到临时目录读取
    （7z/unrar-free 对 SubHD 的 RAR 有兼容问题，unar 不支持输出到 stdout）。
    归档损坏/无字幕条目/解压失败抛 SubhdError。

    供 extract_subtitle_from_archive 单条选优与整季包批量提取
    （fill_missing_subtitles 批处理策略）复用同一套解压逻辑。
    """
    entries: list[tuple[str, bytes]] = []

    # 尝试 ZIP
    try:
        import io

        with ZipFile(io.BytesIO(archive_data)) as zf:
            for name in zf.namelist():
                ext = Path(name).suffix.lstrip(".").lower()
                if ext in target_exts and not name.endswith("/"):
                    entries.append((name, zf.read(name)))
    except Exception:
        pass

    # ZIP 无命中则尝试 RAR（调用 unar，解压到临时目录再读取——unar 不支持输出到 stdout）
    if not entries:
        unar = shutil.which("unar")
        if not unar:
            raise SubhdError("解压 RAR 需要 unar（请安装 the-unarchiver/unar）")

        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_rar = Path(tmp_dir) / "archive.rar"
            tmp_rar.write_bytes(archive_data)
            proc = subprocess.run(
                [unar, "-o", str(tmp_dir), str(tmp_rar)],
                capture_output=True,
                timeout=30,
            )
            if proc.returncode != 0:
                # BUG-7：unar 常把错误写到 stdout、stderr 可能为空——全部带上，
                # 避免只回一句"unar 解压失败: "无法定位问题。
                # NEW-19：stderr 为空时补充 returncode 含义与 unar 版本自检提示。
                err = proc.stderr.decode(errors="replace").strip()
                out = proc.stdout.decode(errors="replace").strip()
                stderr_note = "" if err else (
                    "（stderr 为空；returncode=1 常见于归档格式无法识别或解码器缺失，"
                    "可运行 unar -V 检查版本，并确认下载内容确实是压缩归档）"
                )
                raise SubhdError(
                    f"unar 解压失败 returncode={proc.returncode}; "
                    f"stderr={err[:200] or '(空)'}; stdout={out[:200] or '(空)'}{stderr_note}"
                )
            for f in Path(tmp_dir).rglob("*"):
                if f.is_file() and f.suffix.lstrip(".").lower() in target_exts:
                    entries.append((f.name, f.read_bytes()))
            if not entries:
                inside = [str(f.relative_to(tmp_dir))
                          for f in Path(tmp_dir).rglob("*") if f.is_file()]
                raise SubhdError(
                    f"压缩包中没有找到字幕文件（包内 {len(inside)} 个文件: {inside[:20]}）"
                )

    return entries


def extract_subtitle_from_archive(
    archive_data: bytes,
    target_exts: frozenset[str],
    video_stem: str,
    lang: str = "",
) -> tuple[str, bytes]:
    """从 RAR/ZIP 压缩包中提取字幕文件，返回 (文件名, 内容)。

    #10：包内多个字幕文件时按 _archive_entry_score 选最优（集号 > stem 命中 >
    语言 > 扩展名），不再取"第一个匹配"——多语言包不再语言错标，整季包不再
    拿错集号（E10 vs 目标 E01）。同分保持包内原始顺序（max 稳定）。
    """
    entries = collect_archive_entries(archive_data, target_exts)
    # 两级选择（#10）：先集号/stem 匹配，再语言，最后扩展名优先
    return max(
        entries,
        key=lambda e: _archive_entry_score(e[0], video_stem, lang),
    )


# v0.11.8 NEW-1：文本类字幕格式（需要做编码统一）；其余（sup 位图等）原样落盘
_TEXT_SUBTITLE_FORMATS = frozenset({"srt", "ass", "ssa", "vtt"})


def sniff_subtitle_format(data: bytes) -> str | None:
    """按内容头嗅探字幕真实格式（ass/ssa/vtt/srt），识别不出返回 None。

    SubHD 存在"压缩包内文件名 .srt、内容却是 ASS"的情况（NEW-1），
    扩展名不可信，必须以内容为准。
    """
    head = data
    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if head.startswith(bom):
            head = head[len(bom):]
            break
    head = head.lstrip()[:256].lower()
    if head.startswith(b"[script info]"):
        return "ass" if b"v4.00+" in head else "ssa"
    if head.startswith(b"webvtt"):
        return "vtt"
    if b"-->" in head:
        return "srt"
    return None


def convert_to_utf8(data: bytes) -> bytes:
    """把字幕内容统一转为 UTF-8。

    SubHD 常见 UTF-16LE 带 BOM，很多播放器不认；落盘前统一转换。
    """
    if data.startswith(b"\xff\xfe"):
        # UTF-16LE BOM
        return data[2:].decode("utf-16-le").encode("utf-8")
    if data.startswith(b"\xfe\xff"):
        # UTF-16BE BOM
        return data[2:].decode("utf-16-be").encode("utf-8")
    if data.startswith(b"\xef\xbb\xbf"):
        # UTF-8 BOM，直接返回（多数播放器兼容）
        return data
    # 尝试 UTF-8 解码；失败则尝试 GBK/GB2312
    try:
        data.decode("utf-8")
        return data
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk").encode("utf-8")
    except UnicodeDecodeError:
        pass
    # 最后尝试 latin-1（不会失败）
    return data.decode("latin-1").encode("utf-8")


# ---------------------------------------------------------------- 工具函数


def suggest_subtitle_name(
    template: str,
    video_path: Path,
    lang: str,
    ext: str,
) -> str:
    """按命名模板生成字幕文件名。"""
    stem = video_path.stem
    return template.format(stem=stem, lang=lang, ext=ext)


# ---------------------------------------------------------------- 工具入口


def _resolve_chinese_alias(tmdb_cfg: TmdbConfig, title: str) -> str:
    """NEW-20：解析片名的 TMDB zh-CN 标准名，作为中文别名搜索词。

    SubHD 站内以中文标题索引为主（英文官方名常召不回条目），中文别名
    （如 "母狮"）往往才能命中 release_name 完全一致的候选。
    TMDB 未启用/网络失败/无结果时返回空串，绝不影响主搜索流程。
    """
    if not title:
        return ""
    try:
        from . import resolver as resolver_mod

        result = resolver_mod.resolve(tmdb_cfg, title)
    except Exception as exc:  # ResolverError/InvalidArguments/网络——降级不阻断
        logger.debug("TMDB 中文别名解析失败（跳过别名轮次）: %s", exc)
        return ""
    identity = result.get("identity") or {}
    alias = str(identity.get("canonical_title") or "").strip()
    return alias if alias and alias != title else ""


def search_subtitles(
    guard: PathGuard,
    cfg: SubhdConfig,
    *,
    video_path: str = "",
    keyword: str = "",
    max_pages: int = 3,
    tmdb_cfg: TmdbConfig | None = None,
) -> dict:
    """搜索 SubHD 字幕（只读网络搜索）。

    必须提供 video_path（从文件名解析搜索词）或 keyword（自由搜索）。

    多轮召回（v0.11.18 NEW-20）：
      第 1 轮主关键词（video_path 模式为 片名+年份）；
      最佳候选未达直接命中分数线时——
      第 2 轮用 TMDB zh-CN 中文别名（需 tmdb_cfg 且 enabled）；
      第 3 轮（仅 video_path 模式）用规范化 release_name（文件 stem）精确匹配。
    """
    if not cfg.enabled:
        raise InvalidArguments("SubHD 功能未启用（subhd.enabled=false）")

    release_hint: str | None = None
    if video_path:
        resolved = guard.check(video_path)
        if not resolved.is_file():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")
        query = parse_video_filename(resolved.name)
        search_keyword = query.title
        if query.year:
            search_keyword += f" {query.year}"
        release_hint = resolved.stem
    elif keyword:
        query = parse_video_filename(keyword)
        search_keyword = keyword
    else:
        raise InvalidArguments("必须提供 video_path 或 keyword")

    if not search_keyword.strip():
        raise InvalidArguments("搜索关键词为空")

    client = SubhdClient(cfg)
    used_keywords: list[str] = []
    by_sid: dict[str, SubtitleCandidate] = {}

    def _run_round(kw: str) -> int:
        """跑一轮搜索（最多 max_pages 页），按 sid 去重保留最高分；返回上游新增条数。"""
        if not kw or kw in used_keywords:
            return 0
        used_keywords.append(kw)
        added = 0
        for page in range(1, max_pages + 1):
            page_results = client.search(kw, page=page)
            added += len(page_results)
            for c in page_results:
                sc = replace(c, score=score_candidate(c, query, release_hint=release_hint))
                old = by_sid.get(c.sid)
                if old is None or sc.score > old.score:
                    by_sid[c.sid] = sc
            if len(page_results) < 10:  # 不足一页，说明已到末页
                break
        # NEW-20：每轮把实际下发的 query 与上游返回条数记入 debug 日志
        logger.debug(
            "SubHD 搜索轮次 keyword=%r 上游返回=%d 累计候选=%d", kw, added, len(by_sid),
        )
        return added

    # 第 1 轮：主关键词
    _run_round(search_keyword)
    best = max((c.score for c in by_sid.values()), default=0.0)

    # 第 2 轮：中文别名（TMDB zh-CN 译名）
    if best < _EXACT_HIT_SCORE and tmdb_cfg is not None and tmdb_cfg.enabled:
        _run_round(_resolve_chinese_alias(tmdb_cfg, query.title))
        best = max((c.score for c in by_sid.values()), default=0.0)

    # 第 3 轮（仅 video_path 模式）：规范化 release_name 精确匹配回退
    if best < _EXACT_HIT_SCORE and release_hint:
        _run_round(release_hint)

    # 评分排序（按 sid 去重后取最高分，按分降序）
    scored = sorted(by_sid.values(), key=lambda x: x.score, reverse=True)

    return {
        "ok": True,
        "query": {
            "title": query.title,
            "year": query.year,
            "season": query.season,
            "episode": query.episode,
            "resolution": query.resolution,
            "source": query.source,
            "release_group": query.release_group,
            "keyword": search_keyword,
        },
        "total": len(scored),
        "candidates": [
            {
                "sid": c.sid,
                "title": c.title,
                "release_name": c.release_name,
                "languages": list(c.languages),
                "format": c.format,
                "source": c.source,
                "score": c.score,
                "detail_url": c.detail_url,
                "site": c.site,
            }
            for c in scored[: cfg.max_results]
        ],
    }


def fetch_and_process(
    raw_data: bytes,
    subtitle_exts: frozenset[str],
    video_stem: str,
    content_type: str = "",
    lang: str = "",
) -> tuple[str, bytes, str]:
    """从原始下载内容提取字幕：归档解压或裸字幕，返回 (文件名, 内容, 实际格式)。

    复用 NEW-19/21 的内容判定逻辑：
      - 归档（rar/zip/7z/gzip）→ extract_subtitle_from_archive
        （#10：包内多文件按 video_stem 集号 + lang 语言两级选择，非首个匹配）
      - 裸字幕（ass/ssa/vtt/srt）→ 直接使用
      - 非归档非字幕 → 抛 SubhdError(code="download_not_archive")

    实际格式以内容嗅探为准（NEW-1），其次压缩包内扩展名；
    若都无法判定则返回空串，由调用方回退到默认扩展名。
    """
    if sniff_archive_format(raw_data) is not None:
        filename, content = extract_subtitle_from_archive(
            raw_data, subtitle_exts, video_stem, lang
        )
    else:
        bare_format = sniff_subtitle_format(raw_data)
        if bare_format is not None:
            # NEW-21：裸字幕正文，直接当字幕内容落盘（复用命名/UTF-8/白名单逻辑）
            filename, content = "", raw_data
        else:
            summary = _body_summary(raw_data)
            hints = extract_html_hints(raw_data, content_type)
            raise SubhdError(
                f"下载内容既不是字幕归档也不是字幕正文"
                f"（content-type={content_type!r}）"
                f" body摘要={summary!r}" + (f" HTML提示: {hints}" if hints else ""),
                code="download_not_archive",
                body=raw_data[:500],
            )

    # NEW-1：以内容真实格式为准——内容嗅探 > 压缩包内扩展名；
    # 都无法判定时返回空串，由调用方回退到默认扩展名。
    archive_ext = Path(filename).suffix.lstrip(".").lower()
    actual_format = (
        sniff_subtitle_format(content)
        or (archive_ext if archive_ext in subtitle_exts else None)
        or ""
    )
    return filename, content, actual_format


def download_subtitle(
    guard: PathGuard,
    cfg: SubhdConfig,
    subtitle_exts: frozenset[str],
    enable_write: bool,
    *,
    video_path: str,
    sid: str,
    lang: str = "zh-Hans",
    ext: str = "srt",
    overwrite: bool = False,
) -> dict:
    """从 SubHD 下载字幕并保存到视频同目录（写操作，受 enable_write 门控）。

    链路：prepare-download → /down 中转页 → down 验证 → 直链下载 → 解压 → 转 UTF-8 → 落盘。
    """
    if not cfg.enabled:
        raise InvalidArguments("SubHD 功能未启用（subhd.enabled=false）")
    if not enable_write:
        raise InvalidArguments("写操作未启用（enable_write=false）")

    resolved_video = guard.check(video_path)
    if not resolved_video.is_file():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    # 目标文件名只依赖模板，提前生成：已存在且不允许覆盖时在网络请求前就拒绝
    target_name = suggest_subtitle_name(cfg.filename_template, resolved_video, lang, ext)
    target_path = resolved_video.parent / target_name
    guard.check(str(target_path))
    if target_path.exists() and not overwrite:
        raise FileExistsError(
            f"目标文件已存在: {target_name}（如需覆盖请设 overwrite=true）"
        )

    # 落盘前权限预检（v0.11.7 BUG-6）：只读环境下下载链路本身能跑通，失败只会
    # 发生在最后的 write_bytes。先 os.access 探活，把失败提前到网络请求之前，
    # PermissionError 由 server/plugin 层映射为结构化 permission_denied。
    if not os.access(resolved_video.parent, os.W_OK):
        raise PermissionError(
            errno.EACCES,
            permission_hint_text(resolved_video.parent),
            str(resolved_video.parent),
        )

    client = SubhdClient(cfg)

    # 步骤 3-5：获取直链
    download_path = client.prepare_download(sid)
    direct_url = client.verify_download(sid, download_path)

    # 步骤 6：下载 + 内容识别（NEW-19 + NEW-21）。
    # 三类下载内容：
    #   (a) 字幕归档（rar/zip/7z/gzip）→ 解压；
    #   (b) 裸字幕正文（ASS/SRT/VTT，SubHD 部分条目直接返回字幕文件本身，
    #       content-type 恒为 application/octet-stream）→ 直接落盘（NEW-21）；
    #   (c) 非归档非字幕（反爬占位页/失效链接）→ 自动重试 1 次后报
    #       download_not_archive（带响应元信息与 HTML 提示，与笼统解压错误区分）。
    # 合法内容（a/b）不重试；只有疑似占位页才重试一次。
    not_archive_err: SubhdError | None = None
    filename: str = ""       # 归档内文件名（裸字幕为空串）
    content: bytes | None = None  # 解压后/裸字幕正文
    actual_format: str = ""
    for attempt in range(2):
        archive_data, dl_meta = client.download_file(
            direct_url, referer=f"{cfg.base_url}/a/{sid}"
        )
        try:
            filename, content, actual_format = fetch_and_process(
                archive_data,
                subtitle_exts,
                resolved_video.stem,
                str(dl_meta.get("content_type", "")),
                lang,
            )
            not_archive_err = None
            break
        except SubhdError as exc:
            if exc.code != "download_not_archive":
                raise
            not_archive_err = exc
            if attempt == 0:
                logger.warning(
                    "SubHD 直链返回非归档非字幕内容，自动重试一次: %s", exc
                )
    if not_archive_err is not None:
        raise not_archive_err
    assert content is not None
    # 调用方默认扩展名兜底（fetch_and_process 仅依据内容/归档扩展名判定）
    actual_format = actual_format or ext
    if actual_format != ext:
        target_name = suggest_subtitle_name(
            cfg.filename_template, resolved_video, lang, actual_format
        )
        target_path = resolved_video.parent / target_name
        guard.check(str(target_path))
        if target_path.exists() and not overwrite:
            raise FileExistsError(
                f"目标文件已存在: {target_name}（如需覆盖请设 overwrite=true）"
            )

    # 文本类字幕统一 UTF-8；位图等二进制格式原样落盘（转换会破坏内容）
    if actual_format in _TEXT_SUBTITLE_FORMATS:
        content = convert_to_utf8(content)

    # fnOS 会把新文件裁成 0555：写完显式恢复 0644，便于后续覆盖/追加
    safe_write_bytes(target_path, content)

    # #10：落盘名按请求 lang 生成，但包内实际文件可能多语言/无语言标记——
    # 返回体标注实际文件名与实际语言，与请求不一致时显式告警（不做假阳性）。
    # OPEN-4：判定不了时回退请求语言，避免 actual_language 返回空串
    actual_language = report_language_tag(infer_language_tag(filename), lang)
    result = {
        "ok": True,
        "video_path": str(resolved_video),
        "subtitle_path": str(target_path),
        "source_sid": sid,
        "archive_filename": filename,
        "actual_filename": filename,
        "actual_language": actual_language,
        "size_bytes": len(content),
        "language": lang,
        "format": actual_format,
        **({"requested_format": ext} if actual_format != ext else {}),
    }
    if not language_compatible(actual_language, lang):
        result["language_warning"] = (
            f"包内实际文件 {filename!r} 推断语言为 {actual_language}，"
            f"与请求 {lang} 不一致（落盘文件名仍按请求语言生成，请核对内容）"
        )
    return result
