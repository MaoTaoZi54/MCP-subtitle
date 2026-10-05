"""SubHD 字幕搜索与下载（M4）。

链路（规格 §19/§20）：
  搜索页 → 详情页 → prepare-download → /down 中转页 → down 验证 → 直链下载

安全约束：
  - 总开关 subhd.enabled=false 时所有工具直接拒绝；
  - 下载是写操作，受 enable_write 与路径白名单双重门控；
  - 全程共享 CookieJar + Chrome UA，按 min_request_interval_seconds 限速；
  - RAR 用 unar 解（7z/unrar-free 对 SubHD 的 RAR 有兼容问题），ZIP 用 zipfile；
  - 字幕落盘前统一转 UTF-8（SubHD 常见 UTF-16LE BOM，很多播放器不认）。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from .config import SubhdConfig
from .media import extract_language_tags, is_chinese
from .security import InvalidArguments, PathGuard

logger = logging.getLogger("nas_mcp.subhd")


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
# 常见发布组黑名单（从文件名尾部剥离）
_GROUP_BLACKLIST = frozenset({
    "mkv", "mp4", "avi", "ts", "m2ts", "webm", "wmv", "flv",
    "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "dts",
    "10bit", "8bit", "hdr", "sdr", "dv", "dolby", "atmos", "ma",
    "proper", "repack", "internal", "limited", "extended", "uncut",
    "chd", "chp", "cmct", "frds", "beast", "hdhome", "hdctv",
})


def parse_video_filename(filename: str) -> VideoQuery:
    """从视频文件名解析出搜索元数据（片名/年份/季集/分辨率/来源/发布组）。"""
    stem = Path(filename).stem

    year_m = _YEAR_RE.search(stem)
    year = int(year_m.group(1)) if year_m else None

    se_m = _SEASON_EP_RE.search(stem)
    season = int(se_m.group(1)) if se_m and se_m.group(1) else None
    episode = int(se_m.group(2)) if se_m and se_m.group(2) else None
    if season is None and se_m and se_m.group(3):
        season = int(se_m.group(3))
    if episode is None and se_m and se_m.group(4):
        episode = int(se_m.group(4))

    res_m = _RESOLUTION_RE.search(stem)
    resolution = f"{res_m.group(1)}p" if res_m else None

    src_m = _SOURCE_RE.search(stem)
    source = src_m.group(1).upper().replace("BLU-RAY", "BLURAY") if src_m else None

    grp_m = _GROUP_RE.search(stem)
    group = grp_m.group(1) if grp_m else None
    if group and group.lower() in _GROUP_BLACKLIST:
        group = None

    # 片名：去掉年份/季集/分辨率/来源/发布组/编码等技术字段，再清理分隔符
    title = stem
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
    if text in _LANGUAGE_BADGE_TEXTS:
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
        "简体": "zh-Hans", "繁体": "zh-Hant", "双语": "zh-Hans+en",
        "英语": "en", "日文": "ja", "韩语": "ko",
    }
    found: set[str] = set()
    for b in badges:
        b = b.strip()
        if not b:
            continue
        if b in mapping:
            mapped = mapping[b]
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


class SubhdClient:
    """SubHD HTTP 客户端：共享 CookieJar + Chrome UA + 限速。"""

    def __init__(self, cfg: SubhdConfig):
        self.cfg = cfg
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
        )
        self._last_request_at = 0.0

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
        referer: str | None = None,
        parse_json: bool = False,
    ) -> str | dict | bytes:
        self._rate_limit()
        headers = {"User-Agent": self.cfg.user_agent}
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
        except urllib.error.URLError as exc:
            raise SubhdError(f"网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise SubhdError(f"请求超时（>{self.cfg.timeout_seconds}s）") from exc

        if parse_json:
            try:
                return json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SubhdError(f"JSON 解析失败: {exc}") from exc

        if "html" in content_type or "text" in content_type:
            return body.decode("utf-8", errors="replace")
        return body

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
        """步骤 3：POST /api/sub/prepare-download，返回 /down/{sid} 中转路径。"""
        url = f"{self.cfg.base_url}/api/sub/prepare-download"
        data = json.dumps({"sid": sid}).encode("utf-8")
        resp = self._request(
            url,
            method="POST",
            data=data,
            referer=f"{self.cfg.base_url}/a/{sid}",
            parse_json=True,
        )
        if not isinstance(resp, dict):
            raise SubhdError("prepare-download 返回非 JSON")
        if not resp.get("success"):
            raise SubhdError(f"prepare-download 失败: {resp.get('msg', '未知错误')}")
        download_path = resp.get("url", "")
        if not download_path.startswith("/down/"):
            raise SubhdError(f"prepare-download 返回非法下载路径: {download_path!r}")
        return download_path

    def verify_download(self, sid: str, download_path: str) -> str:
        """步骤 4-5：访问中转页 → POST /api/sub/down，返回直链 URL。

        中转页必须带同一个 CookieJar + Referer，否则 403「下载页面已失效」。
        """
        # 步骤 4：GET /down/{sid}（中转页，内嵌 JS 指向验证接口）
        down_url = f"{self.cfg.base_url}{download_path}"
        referer = f"{self.cfg.base_url}/a/{sid}"
        self._request(down_url, referer=referer)  # 保持会话

        # 步骤 5：POST /api/sub/down
        api_url = f"{self.cfg.base_url}/api/sub/down"
        data = json.dumps({"sid": sid}).encode("utf-8")
        resp = self._request(
            api_url,
            method="POST",
            data=data,
            referer=down_url,
            parse_json=True,
        )
        if not isinstance(resp, dict):
            raise SubhdError("down 验证返回非 JSON")
        if not resp.get("pass"):
            raise SubhdError(f"down 验证失败: {resp.get('msg', '未知错误')}")
        direct_url = resp.get("url", "")
        if not direct_url.startswith(("http://", "https://")):
            raise SubhdError(f"down 验证返回非法直链: {direct_url!r}")
        return direct_url

    def download_file(self, direct_url: str, referer: str) -> bytes:
        """步骤 6：下载直链（RAR/ZIP 字幕包），带大小上限。"""
        self._rate_limit()
        headers = {"User-Agent": self.cfg.user_agent, "Referer": referer}
        req = urllib.request.Request(direct_url, headers=headers)
        try:
            with self._opener.open(req, timeout=self.cfg.timeout_seconds) as resp:
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > self.cfg.max_download_bytes:
                    raise SubhdError(
                        f"下载包超过大小上限（{self.cfg.max_download_bytes} bytes）"
                    )
                body = resp.read(self.cfg.max_download_bytes + 1)
                if len(body) > self.cfg.max_download_bytes:
                    raise SubhdError(
                        f"下载包超过大小上限（{self.cfg.max_download_bytes} bytes）"
                    )
        except urllib.error.HTTPError as exc:
            raise SubhdError(f"下载失败 HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise SubhdError(f"下载网络失败: {exc.reason}") from exc
        return body


class SubhdError(Exception):
    """SubHD 操作失败（网络/解析/验证等）。"""

    def __init__(self, message: str, *, status_code: int | None = None, body: bytes = b""):
        self.status_code = status_code
        self.body = body
        super().__init__(message)


# ---------------------------------------------------------------- 评分排序


def score_candidate(candidate: SubtitleCandidate, query: VideoQuery) -> float:
    """按片名/年份/季集/分辨率/来源/发布组给候选打分（0~1）。

    避免只按文件名模糊匹配（规格 §19）。
    """
    score = 0.0
    total_weight = 0.0

    def _add(weight: float, matched: bool) -> None:
        nonlocal score, total_weight
        total_weight += weight
        if matched:
            score += weight

    release = candidate.release_name.lower()
    title_lower = candidate.title.lower()

    # 片名匹配（权重最高）
    q_title = query.title.lower()
    if q_title:
        _add(0.35, q_title in release or q_title in title_lower)

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
        release_norm = release.replace("-", "").replace(".", "").replace(" ", "")
        _add(0.10, src_lower in release_norm)

    # 发布组
    if query.release_group:
        _add(0.05, query.release_group.lower() in release)

    # 中文字幕加分（优先推荐有中文字幕的）
    has_zh = any(is_chinese(lang) for lang in candidate.languages)
    _add(0.05, has_zh)

    if total_weight == 0:
        return 0.0
    return round(score / total_weight, 3)


# ---------------------------------------------------------------- 解压与转码


def extract_subtitle_from_archive(
    archive_data: bytes,
    target_exts: frozenset[str],
    video_stem: str,
) -> tuple[str, bytes]:
    """从 RAR/ZIP 压缩包中提取字幕文件，返回 (文件名, 内容)。

    RAR 必须用 unar（7z/unrar-free 对 SubHD 的 RAR 有兼容问题）；
    ZIP 用 zipfile 标准库。
    """
    # 尝试 ZIP
    try:
        import io

        with ZipFile(io.BytesIO(archive_data)) as zf:
            for name in zf.namelist():
                ext = Path(name).suffix.lstrip(".").lower()
                if ext in target_exts:
                    return name, zf.read(name)
    except Exception:
        pass

    # 尝试 RAR（调用 unar，解压到临时目录再读取——unar 不支持输出到 stdout）
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
            raise SubhdError(f"unar 解压失败: {proc.stderr.decode(errors='replace')[:200]}")
        for f in Path(tmp_dir).rglob("*"):
            if f.is_file() and f.suffix.lstrip(".").lower() in target_exts:
                return f.name, f.read_bytes()

    raise SubhdError("压缩包中没有找到字幕文件")


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


def search_subtitles(
    guard: PathGuard,
    cfg: SubhdConfig,
    *,
    video_path: str = "",
    keyword: str = "",
    max_pages: int = 3,
) -> dict:
    """搜索 SubHD 字幕（只读网络搜索）。

    必须提供 video_path（从文件名解析搜索词）或 keyword（自由搜索）。
    """
    if not cfg.enabled:
        raise InvalidArguments("SubHD 功能未启用（subhd.enabled=false）")

    if video_path:
        resolved = guard.check(video_path)
        if not resolved.is_file():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")
        query = parse_video_filename(resolved.name)
        search_keyword = query.title
        if query.year:
            search_keyword += f" {query.year}"
    elif keyword:
        query = parse_video_filename(keyword)
        search_keyword = keyword
    else:
        raise InvalidArguments("必须提供 video_path 或 keyword")

    if not search_keyword.strip():
        raise InvalidArguments("搜索关键词为空")

    client = SubhdClient(cfg)
    all_candidates: list[SubtitleCandidate] = []
    for page in range(1, max_pages + 1):
        page_results = client.search(search_keyword, page=page)
        all_candidates.extend(page_results)
        if len(page_results) < 10:  # 不足一页，说明已到末页
            break

    # 评分排序（SubtitleCandidate 是 frozen dataclass，用 replace 生成新对象）
    scored = [replace(c, score=score_candidate(c, query)) for c in all_candidates]
    scored.sort(key=lambda x: x.score, reverse=True)

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
            }
            for c in scored[: cfg.max_results]
        ],
    }


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

    client = SubhdClient(cfg)

    # 步骤 3-5：获取直链
    download_path = client.prepare_download(sid)
    direct_url = client.verify_download(sid, download_path)

    # 步骤 6：下载
    archive_data = client.download_file(direct_url, referer=f"{cfg.base_url}/a/{sid}")

    # 解压 + 转 UTF-8（SubHD 常见 UTF-16LE BOM）
    filename, content = extract_subtitle_from_archive(archive_data, subtitle_exts, resolved_video.stem)
    content = convert_to_utf8(content)

    target_path.write_bytes(content)

    return {
        "ok": True,
        "video_path": str(resolved_video),
        "subtitle_path": str(target_path),
        "source_sid": sid,
        "archive_filename": filename,
        "size_bytes": len(content),
        "language": lang,
        "format": ext,
    }
