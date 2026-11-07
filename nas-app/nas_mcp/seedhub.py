# -*- coding: utf-8 -*-
"""SeedHub 站点适配（S2）：中文片名搜索、磁力列表、网盘列表（return dict，不 print）。

站点事实（2026-10 实测，勿推翻）：
- Cloudflare 严格 TLS 指纹拦截：只有 curl_cffi（impersonate="chrome"）能过墙，
  裸 requests/urllib 必 403；
- 搜索只吃中文片名（/s/<中文>/），英文片名恒 0 结果（站点限制，不是 bug）；
- 搜索页卡片：<a title="中文名 英文名" href="/movies/<id>/">；
- 详情页 .seeds 磁力经 /link_start/?seed_id=N 中转，页面 data 变量 base64
  解码即 magnet（**无 tracker、无做种数**，属站点原貌，绝不臆造 seeders）；
- 网盘经 /link_start/?redirect_to=pan_id_N 中转，base64 解码出带提取码直链；
- 2026-10 站点改版：详情页 href 尾部带 &movie_title= 等尾随参数（正则必须允许
  id 之后到引号前的任意内容）；条目元数据（发布名/大小/日期）挂在 <a title="...">
  属性上、网盘 host 挂 data-link 属性——优先取属性值，上下文窗口只作兜底；
- 正则 0 命中但页面含对应 id 标记 → 返回 parse_mismatch 显式错误，
  绝不伪装成 ok/count=0（否则站点再改版又是静默失败，bug-report-2026-10-12）；
- 逐条 /link_start 转跳间隔必须 ≥1s（request_delay_seconds 下限 1.0），
  否则触发 CF 挑战，挑战升级后整站一段时间全 403。

S1 懒加载：curl_cffi 缺失时本模块照常可 import，仅工具返回 dependency_missing，
绝不因缺这一个依赖打挂插件存量工具。
"""

from __future__ import annotations

import base64
import binascii
import html as html_lib
import logging
import re
import threading
import time
import urllib.parse
from datetime import date, timedelta
from typing import Any

from .config import SeedhubConfig
from .pan_probe import probe_pan_url

try:  # S1：懒加载 curl_cffi（导入失败只让 seedhub_* 报 dependency_missing）
    from curl_cffi import requests as cf_requests
except ImportError:  # pragma: no cover - 取决于运行环境是否装了 curl_cffi
    cf_requests = None  # type: ignore[assignment]

logger = logging.getLogger("nas_mcp.seedhub")

BASE = "https://www.seedhub.cc"          # 站点默认地址（cfg.base_url 覆盖）
DEFAULT_TIMEOUT_SECONDS = 20.0           # 单请求默认超时（= media_search 预算）
MAX_ITEMS_DEFAULT = 50                   # limit=0 时的条数上限（超出 truncated）
_RETRY_DELAYS = (3.0, 6.0, 9.0)          # GET 传输失败退避：重试 3 次、3/6/9s
# CF 挑战页特征（只用于判定，绝不把原文写进日志/返回体——全局约束 5）
_CF_MARKERS = ("just a moment", "cf-chl", "challenge-platform", "_cf_chl")

# 站点关联状态：会话复用 + 全局限速（并发工具调用共用一个节拍）
_SESSIONS: dict[str, Any] = {}
_STATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DATA_B64_RE = re.compile(r"""data\s*=\s*["']([A-Za-z0-9+/=_-]{16,})["']""")
_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[A-Fa-f0-9]{32,40}[^\s\"'<>]*", re.I)
_PAN_HREF_RE = re.compile(
    r"https?://(?:pan\.quark\.cn|pan\.baidu\.com|pan\.xunlei\.com|drive\.uc\.cn"
    r"|[a-z0-9.-]*aliyundrive\.com|115\.com|cloud\.189\.cn)"
    r"/[^\s\"'<>]+",
    re.I,
)
# 2026-10 改版：id 后允许尾随参数（&movie_title=...），捕获组序号不变
_SEED_HREF_RE = re.compile(
    r"""href=["'](/link_start/\?seed_id=(\d+)[^"']*)["']""", re.I
)
_PAN_HREF_RE2 = re.compile(
    r"""href=["'](/link_start/\?redirect_to=pan_id_(\d+)[^"']*)["']""", re.I
)
_ANCHOR_TAG_RE = re.compile(r"<a\b[^>]*>", re.I)
_ATTR_TITLE_RE = re.compile(r"""(?<![\w-])title\s*=\s*["']([^"']*)["']""", re.I)
_ATTR_DATA_LINK_RE = re.compile(r"""(?<![\w-])data-link\s*=\s*["']([^"']*)["']""", re.I)
# 大小：全写单位（12.3GB）或站点新式括号简写（[49.61G]，要求带小数点，
# 避免把画质词 4K/S06.4K 误判成大小）
_SIZE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:TB|GB|MB|KB)|\[\s*\d+\.\d+\s*[TGMK]\s*\]",
    re.I,
)
_DATE_RE = re.compile(
    # OPEN-5/BUG-5/MUX-6：补相对日期（今天/昨天/前天/N天前/N小时前/N月前/N年前/N周前 [HH:MM]）
    # 不匹配导致 updated 恒 null 且日期词混进 title。
    # 前后加 CJK 边界（lookaround 不消费）：避免把「今天不上班」这类
    # 片名里的词误判成日期并从标题中剥掉。
    r"\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2})?"
    r"|(?<![\u4e00-\u9fff])(?:今天|昨天|前天|(?:\d{1,3})(?:天|小时|月|年|周)前)(?:\s*\d{1,2}:\d{2})?(?![\u4e00-\u9fff])"
)
_REL_DATE_RE = re.compile(
    r"^(?:(今天|昨天|前天)|(\d{1,3})天前|(\d{1,2})小时前|(\d{1,3})月前|(\d{1,3})年前|(\d{1,3})周前)(?:\s*(\d{1,2}:\d{2}))?$"
)


def _normalize_updated(value: str | None) -> str | None:
    """OPEN-5/BUG-5/MUX-6：相对日期归一为 YYYY-MM-DD（今天/昨天/前天/N天前/N小时前/N月前/N年前/N周前），绝对日期原样。"""
    if not value:
        return value
    m = _REL_DATE_RE.match(value.strip())
    if not m:
        return value
    rel_word, days_ago, hours_ago, months_ago, years_ago, weeks_ago = (
        m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
    )
    time_part = m.group(7)
    if rel_word:
        days = {"今天": 0, "昨天": 1, "前天": 2}[rel_word]
    elif days_ago:
        days = int(days_ago)
    elif hours_ago:
        days = 0
    elif weeks_ago:
        days = int(weeks_ago) * 7
    elif months_ago:
        days = int(months_ago) * 30
    elif years_ago:
        days = int(years_ago) * 365
    else:
        return value
    d = date.today() - timedelta(days=days)
    return f"{d.isoformat()} {time_part}" if time_part else d.isoformat()


_QUALITY_RE = re.compile(
    r"4K|2160p|1080p|1080i|720p|蓝光|BluRay|REMUX|杜比|Dolby|无损|HDR10\+?|"
    r"DolbyVision|WEB-DL|WEBRip|HDTV|x265|x264|AVC|HiDt|iNT",
    re.I,
)
_HOST_KEYWORDS = (
    ("quark", "夸克"), ("pan.quark", "夸克"), ("夸克", "夸克"),
    ("baidu", "百度"), ("百度", "百度"),
    ("xunlei", "迅雷"), ("thunder", "迅雷"), ("迅雷", "迅雷"),
    ("uc.cn", "UC"), ("drive.uc", "UC"), ("UC网盘", "UC"), ("uc盘", "UC"),
    ("aliyundrive", "阿里"), ("aliyun", "阿里"), ("阿里", "阿里"),
    ("115", "115"), ("cloud.189", "天翼"),
)


def _err(code: str, message: str, **extra: Any) -> dict:
    """结构化错误（全局约束 1）。"""
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def dependency_missing() -> dict:
    """curl_cffi 缺失时的统一错误（S1 验收口径）。"""
    return _err(
        "dependency_missing",
        "curl_cffi 未安装，SeedHub 不可用（其余工具不受影响）",
    )


def _throttle(cfg: SeedhubConfig) -> None:
    """逐请求限速：两次请求启动间隔 ≥ request_delay_seconds（下限 1.0）。"""
    global _LAST_REQUEST_AT
    with _STATE_LOCK:
        now = time.monotonic()
        wait = cfg.request_delay_seconds - (now - _LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_AT = time.monotonic()


def _session(base_url: str):
    """复用每个 host 的 curl_cffi 会话（线程安全）。"""
    sess = _SESSIONS.get(base_url)
    if sess is None:
        # impersonate="chrome" 是唯一过墙方式（TLS 指纹模拟）
        sess = cf_requests.Session(impersonate="chrome")
        _SESSIONS[base_url] = sess
    return sess


def _looks_like_cf_challenge(text: str) -> bool:
    """判定 CF 挑战页（只做判定，不保留原文）。"""
    low = text[:4000].lower()
    return any(m in low for m in _CF_MARKERS)


def http_get(
    cfg: SeedhubConfig,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    """GET 一个页面：限速 + 传输失败重试 3 次（3/6/9s 退避）。

    返回 (status, text)。非 200 直接返回状态码由调用方映射错误
    （CF 挑战 403/429/503 或挑战页 → cf_blocked）。
    任何情况下都不把响应体/挑战原文写进日志。
    """
    if cf_requests is None:  # 由调用方提前拦截，这里兜底
        raise RuntimeError("curl_cffi missing")
    _throttle(cfg)
    sess = _session(cfg.base_url)
    last_exc: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        if attempt:
            time.sleep(_RETRY_DELAYS[attempt - 1])
        try:
            resp = sess.get(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 传输层错误才重试
            last_exc = exc
            logger.warning("SeedHub 请求传输失败（第 %d 次）: %s", attempt + 1, type(exc).__name__)
            continue
        status = int(getattr(resp, "status_code", 0) or 0)
        try:
            text = resp.text or ""
        except Exception:  # noqa: BLE001
            text = ""
        if status == 200 and _looks_like_cf_challenge(text):
            return 403, text  # 200 挑战页按 403 处理
        if status == 200:
            return status, text
        # 非 200 不重试（CF 挑战重试只会升级）：由调用方映射 cf_blocked/not_found
        return status, text
    raise RuntimeError(f"SeedHub 连接失败（{type(last_exc).__name__}）")


def _map_status(status: int, what: str) -> dict:
    """非 200 统一映射：404 → not_found，其余 → cf_blocked。"""
    if status == 404:
        return _err("not_found", f"SeedHub 页面不存在（{what}）", status_code=status)
    return _err(
        "cf_blocked",
        "SeedHub 触发 Cloudflare 挑战，稍后重试",
        status_code=status,
    )


def _strip_tags(fragment: str) -> str:
    return _WS_RE.sub(" ", html_lib.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _b64decode(text: str) -> str | None:
    """base64 解码（标准/URL-safe 双兼容），失败返回 None。"""
    s = "".join(text.split())
    s += "=" * (-len(s) % 4)
    for fn in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            out = fn(s.encode("ascii"))
        except (binascii.Error, ValueError, UnicodeEncodeError):
            continue
        if out:
            return out.decode("utf-8", "replace")
    return None


def _context_text(page: str, start: int, end: int, radius: int = 450) -> str:
    """取锚点前后窗口的纯文本（标题/大小/日期都在这一带）。"""
    return _strip_tags(page[max(0, start - radius): min(len(page), end + radius)])


def _split_meta(text: str) -> tuple[str, str | None, str | None, tuple[str, ...]]:
    """从上下文文本/ title 属性值拆出 (title, size, updated, quality_tags)。"""
    size_m = _SIZE_RE.search(text)
    date_m = _DATE_RE.search(text)
    size = size_m.group(0).strip("[] ") if size_m else None
    # OPEN-5：相对日期归一（今天/昨天 → YYYY-MM-DD）
    updated = _normalize_updated(date_m.group(0)) if date_m else None
    qualities = tuple(dict.fromkeys(m.group(0) for m in _QUALITY_RE.finditer(text)))[:3]
    title = text
    for m in (size_m, date_m):
        if m:
            title = title.replace(m.group(0), " ")
    # 括号简写大小被剥掉后可能残留空括号（CRFW[49.61G] → CRFW[]）
    title = _WS_RE.sub(" ", title).strip(" -–—·|:[]")
    if len(title) > 160:
        title = title[:160].rstrip()
    return title, size, updated, qualities


def _detect_host(text: str, url: str = "") -> str:
    """识别网盘名称（上下文关键词 > 解码后 URL 域名）。"""
    hay = (text + " " + url).lower()
    for kw, name in _HOST_KEYWORDS:
        if kw.lower() in hay:
            return name
    if url:
        try:
            return urllib.parse.urlparse(url).netloc
        except ValueError:
            pass
    return "未知网盘"


def _anchor_attr(tag: str, pattern: re.Pattern[str]) -> str:
    m = pattern.search(tag)
    return html_lib.unescape(m.group(1)).strip() if m else ""


def _seed_entries(page: str) -> list[dict]:
    """扫描详情页磁力锚点（新旧 href 形态均命中），提 id + title 属性。"""
    out: list[dict] = []
    seen: set[str] = set()
    for tag_m in _ANCHOR_TAG_RE.finditer(page):
        hm = _SEED_HREF_RE.search(tag_m.group(0))
        if not hm or hm.group(2) in seen:
            continue
        seen.add(hm.group(2))
        out.append({"sid": hm.group(2), "attr_title": _anchor_attr(tag_m.group(0), _ATTR_TITLE_RE)})
    return out


def _pan_entries(page: str) -> list[dict]:
    """扫描详情网页盘锚点（新旧 href 形态均命中），提 id + title/data-link 属性。"""
    out: list[dict] = []
    seen: set[str] = set()
    for tag_m in _ANCHOR_TAG_RE.finditer(page):
        hm = _PAN_HREF_RE2.search(tag_m.group(0))
        if not hm or hm.group(2) in seen:
            continue
        seen.add(hm.group(2))
        tag = tag_m.group(0)
        out.append({
            "pid": hm.group(2),
            "attr_title": _anchor_attr(tag, _ATTR_TITLE_RE),
            "data_link": _anchor_attr(tag, _ATTR_DATA_LINK_RE),
        })
    return out


def _empty_detail_error(page: str, mid: int, kind: str) -> dict | None:
    """详情页 0 命中的分类（bug 5.3）：解析失败必须显式报错，不许伪装成功。

    kind="seed"/"pan" 分别对应 seed_id=/pan_id_ 标记。
    返回 None = 页面确实没有本类资源（纯另一类资源页），调用方按 count=0 返回。
    """
    marker = "seed_id=" if kind == "seed" else "pan_id_"
    other = "pan_id_" if kind == "seed" else "seed_id="
    if marker in page:
        return _err(
            "parse_mismatch",
            f"movie_id={mid} 页面含 {marker} 链接但解析器 0 命中——"
            "页面结构可能已变化，需更新解析器",
            movie_id=mid,
        )
    if "link_start" not in page:
        return _err("not_found", f"movie_id={mid} 不存在或无可解析资源", movie_id=mid)
    if other in page:
        return None  # 纯另一类资源页（如纯网盘页查磁力）：本类真实为 0
    return _err(
        "parse_mismatch",
        f"movie_id={mid} 页面含 link_start 但结构不可识别——"
        "页面结构可能已变化，需更新解析器",
        movie_id=mid,
    )


def _item_meta(
    page: str, attr_title: str, span: tuple[int, int]
) -> tuple[str, str | None, str | None]:
    """条目元数据：优先 <a title=> 属性（2026-10 改版），上下文窗口兜底。"""
    if attr_title:
        title, size, updated, _q = _split_meta(attr_title)
        if updated is None:  # 属性里没日期时从上下文补（updated 尽量有）
            dm = _DATE_RE.search(_context_text(page, *span))
            if dm:
                updated = _normalize_updated(dm.group(0))
        return title, size, updated
    title, size, updated, _q = _split_meta(_context_text(page, *span))
    return title, size, updated


def _resolve(cfg: SeedhubConfig, path: str, *, kind: str, timeout: float) -> dict:
    """解析 /link_start 中转页：base64 data 优先，magnet/网盘 href 兜底。

    kind: "seed" → 解出 magnet；"pan" → 解出带提取码的网盘直链。
    解码失败只标 resolve_failed，不整批报错（S2.4）。
    """
    url = f"{cfg.base_url}{path}"
    status, page = http_get(cfg, url, timeout=timeout)
    if status != 200:
        return {"resolve_failed": True, "status_code": status}
    # 1) 页面 data 变量 base64 解码（原 CLI 脚本内核）
    m = _DATA_B64_RE.search(page)
    if m:
        decoded = _b64decode(m.group(1))
        if decoded:
            decoded = decoded.strip()
            if kind == "seed" and decoded.lower().startswith("magnet:"):
                return {"magnet": decoded}
            if kind == "pan" and decoded.startswith(("http://", "https://")):
                return {"url": decoded}
            # 解码结果不是目标形态：继续尝试兜底正则
            mm = _MAGNET_RE.search(decoded)
            if kind == "seed" and mm:
                return {"magnet": mm.group(0)}
    # 2) 兜底：转跳页里直接给的 href / magnet
    if kind == "seed":
        mm = _MAGNET_RE.search(page)
        if mm:
            return {"magnet": mm.group(0)}
    else:
        pm = _PAN_HREF_RE.search(page)
        if pm:
            return {"url": html_lib.unescape(pm.group(0))}
    return {"resolve_failed": True}


# --------------------------------------------------------------------------- #
# 三个公开函数（S2：search / magnets / pans，返回 dict）
# --------------------------------------------------------------------------- #


def search(
    cfg: SeedhubConfig,
    keyword: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """按中文片名搜索 SeedHub。0 结果 → no_results（提示只支持中文片名）。"""
    if not cfg.enabled:
        return _err("disabled", "seedhub.enabled=false，SeedHub 已停用")
    if cf_requests is None:
        return dependency_missing()
    keyword = (keyword or "").strip()
    if not keyword:
        return _err("invalid_arguments", "keyword 不能为空")
    url = f"{cfg.base_url}/s/{urllib.parse.quote(keyword, safe='')}/"
    status, page = http_get(cfg, url, timeout=timeout)
    if status != 200:
        return _map_status(status, f"搜索 {keyword!r}")

    results: list[dict] = []
    seen: set[int] = set()
    for m in re.finditer(r"<a\b[^>]*>", page, re.I):
        tag = m.group(0)
        href = re.search(r'href="/movies/(\d+)/"', tag)
        if not href:
            continue
        mid = int(href.group(1))
        if mid in seen:
            continue
        seen.add(mid)
        t_m = re.search(r'title="([^"]*)"', tag)
        title = html_lib.unescape(t_m.group(1)).strip() if t_m else ""
        results.append({
            "id": mid,
            "title": title or f"SeedHub #{mid}",
            "url": f"{cfg.base_url}/movies/{mid}/",
        })
    if not results:
        return _err(
            "no_results",
            "SeedHub 无结果：该站只支持中文片名搜索（英文片名恒 0 结果），"
            "请换中文片名重试",
            keyword=keyword,
        )
    return {"ok": True, "keyword": keyword, "count": len(results), "results": results}


def magnets(
    cfg: SeedhubConfig,
    movie_id: int,
    limit: int = 0,
    offset: int = 0,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> dict:
    """取影片磁力列表（裸 magnet，无 tracker、无做种数——站点原貌）。

    OPEN-5：offset 分页——页面条数可远超默认 50，配合 limit 翻页取全量。
    """
    if not cfg.enabled:
        return _err("disabled", "seedhub.enabled=false，SeedHub 已停用")
    if cf_requests is None:
        return dependency_missing()
    try:
        mid = int(movie_id)
    except (TypeError, ValueError):
        return _err("not_found", f"movie_id 非法: {movie_id!r}")
    if mid <= 0:
        return _err("not_found", f"movie_id 非法: {movie_id!r}")

    status, page = http_get(cfg, f"{cfg.base_url}/movies/{mid}/", timeout=timeout)
    if status != 200:
        return _map_status(status, f"movie_id={mid}")
    entries = _seed_entries(page)
    if not entries:
        bad = _empty_detail_error(page, mid, kind="seed")
        if bad is not None:
            return bad  # parse_mismatch / not_found（纯网盘页则落到 count=0）

    cap = limit if limit > 0 else MAX_ITEMS_DEFAULT
    start = max(0, offset or 0)
    window = entries[start:start + cap]
    truncated = len(entries) > start + len(window)
    deadline_exceeded = False
    items: list[dict] = []
    for entry in window:
        sid = entry["sid"]
        if deadline is not None and time.monotonic() > deadline:
            deadline_exceeded = True
            truncated = True
            break
        title, size, updated = _item_meta(page, entry["attr_title"], _seed_span(page, sid))
        resolved = _resolve(cfg, f"/link_start/?seed_id={sid}", kind="seed", timeout=timeout)
        item: dict[str, Any] = {
            "title": title or f"SeedHub seed_id={sid}",
            "magnet": resolved.get("magnet"),
            "size": size,
            "updated": updated,
        }
        if resolved.get("resolve_failed"):
            item["resolve_failed"] = True
        items.append(item)
    return {
        "ok": True,
        "movie_id": mid,
        "count": len(items),
        "total": len(entries),
        "offset": start,
        "truncated": truncated,
        "deadline_exceeded": deadline_exceeded,
        "results": items,
    }


def pans(
    cfg: SeedhubConfig,
    movie_id: int,
    limit: int = 0,
    offset: int = 0,
    *,
    probe: bool = True,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> dict:
    """取影片网盘列表（直链含提取码）。逐条解析间隔 ≥1s 限速。

    OPEN-5：offset 分页——配合 limit 翻页取全量。
    """
    if not cfg.enabled:
        return _err("disabled", "seedhub.enabled=false，SeedHub 已停用")
    if cf_requests is None:
        return dependency_missing()
    try:
        mid = int(movie_id)
    except (TypeError, ValueError):
        return _err("not_found", f"movie_id 非法: {movie_id!r}")
    if mid <= 0:
        return _err("not_found", f"movie_id 非法: {movie_id!r}")

    status, page = http_get(cfg, f"{cfg.base_url}/movies/{mid}/", timeout=timeout)
    if status != 200:
        return _map_status(status, f"movie_id={mid}")
    entries = _pan_entries(page)
    if not entries:
        bad = _empty_detail_error(page, mid, kind="pan")
        if bad is not None:
            return bad  # parse_mismatch / not_found（纯磁力页则落到 count=0）

    cap = limit if limit > 0 else MAX_ITEMS_DEFAULT
    start = max(0, offset or 0)
    window = entries[start:start + cap]
    truncated = len(entries) > start + len(window)
    deadline_exceeded = False
    items: list[dict] = []
    for entry in window:
        pid = entry["pid"]
        if deadline is not None and time.monotonic() > deadline:
            deadline_exceeded = True
            truncated = True
            break
        # 中转路径只保留功能参数（movie_title 等尾随参数不带——与旧版页面
        # 实测可用的 /link_start/?redirect_to=pan_id_N 形态一致，且避免裸中文入 URL）
        path = f"/link_start/?redirect_to=pan_id_{pid}"
        span = _pan_span(page, path)
        window_text = _context_text(page, *span)
        title, _size, updated = _item_meta(page, entry["attr_title"], span)
        resolved = _resolve(cfg, path, kind="pan", timeout=timeout)
        url = resolved.get("url")
        item: dict[str, Any] = {
            "title": title or f"SeedHub 网盘 {path}",
            # data-link 属性（如 pan.quark.cn）优先，关键词/URL 域名兜底
            "host": _detect_host(f"{entry['data_link']} {window_text}", url or ""),
            "url": url,
            "updated": updated,
        }
        if resolved.get("resolve_failed"):
            item["resolve_failed"] = True
        elif url and probe:
            probe_res = probe_pan_url(url, claimed_title=item["title"], timeout=min(3.0, timeout))
            item["verified"] = probe_res.verified
            item["confidence"] = probe_res.confidence
            if probe_res.actual_quality:
                item["actual_quality"] = probe_res.actual_quality
            if probe_res.claimed_quality:
                item["claimed_quality"] = probe_res.claimed_quality
            if probe_res.mismatch:
                item["mismatch"] = True
                item["warning"] = probe_res.mismatch_reason
            elif probe_res.warning:
                item["warning"] = probe_res.warning
            if probe_res.real_files:
                item["real_files"] = probe_res.real_files
        items.append(item)
    return {
        "ok": True,
        "movie_id": mid,
        "count": len(items),
        "total": len(entries),
        "offset": start,
        "truncated": truncated,
        "deadline_exceeded": deadline_exceeded,
        "results": items,
    }


def _seed_span(page: str, seed_id: str) -> tuple[int, int]:
    """磁力锚点在详情页中的 (start, end)——供上下文取标题/大小/日期。"""
    m = re.search(
        rf"""href=["']/link_start/\?seed_id={re.escape(seed_id)}[^"']*["']""", page, re.I
    )
    if m:
        return m.start(), m.end()
    idx = page.find(f"seed_id={seed_id}")
    return (idx if idx >= 0 else 0, idx + 1 if idx >= 0 else 0)


def _pan_span(page: str, path: str) -> tuple[int, int]:
    """网盘锚点在详情页中的 (start, end)。"""
    m = re.search(re.escape(path), page, re.I)
    if m:
        return m.start(), m.end()
    idx = page.find(path)
    return (idx if idx >= 0 else 0, idx + 1 if idx >= 0 else 0)
