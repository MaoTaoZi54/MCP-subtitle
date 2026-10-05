"""媒体分析与字幕检测（M3）：nas_scan_media / nas_check_subtitle 的实现。

设计要点：
- 媒体信息一律通过 ffprobe 获取（规格 §15/§16），不自己解析二进制格式，
  更不让 LLM 猜；
- 内嵌字幕必须用 ffprobe 的字幕流检测，不能只看目录里有没有 .srt；
- 外挂字幕按“视频主名前缀”在同一目录匹配，语言标签从文件名识别；
- 单文件失败只进 errors 列表，绝不让整个扫描失败（规格 §27）；
- 输出全部为结构化 JSON（规格 §29），分页 + 探测预算（规格 §28）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .config import Limits, MediaConfig
from .fs import _flat_records, _walk_records
from .fs_utils import make_record, make_skip_matcher
from .security import InvalidArguments, PathGuard, expand_roots

# ---------------------------------------------------------------- ffprobe


class ProbeError(Exception):
    """ffprobe 调用失败（不可用/超时/退出码非零/输出解析失败）。

    dedupe_key 非空时，扫描过程中相同 key 的错误只报告一次
    （例如 ffprobe 二进制不存在时避免每个文件重复刷屏）。
    """

    def __init__(self, message: str, *, dedupe_key: str | None = None):
        super().__init__(message)
        self.dedupe_key = dedupe_key


def probe_video(video: Path, ffprobe_path: str, timeout: float) -> dict:
    """调用 ffprobe 获取单个视频的结构化信息；失败抛 ProbeError。"""
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(video),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ProbeError(
            f"ffprobe 不可用（{ffprobe_path!r}）：请在 NAS 上安装 ffmpeg/ffprobe，"
            "或修正配置 media.ffprobe_path",
            dedupe_key="ffprobe-unavailable",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe 超时（>{timeout}s）") from exc
    except OSError as exc:
        raise ProbeError(f"ffprobe 启动失败: {exc}") from exc

    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        tail = lines[-1] if lines else "unknown error"
        raise ProbeError(f"ffprobe 退出码 {proc.returncode}: {tail[:200]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe 输出不是合法 JSON: {exc}") from exc

    fmt = data.get("format") or {}
    video_info: dict | None = None
    audio: list[dict] = []
    embedded: list[dict] = []

    for stream in data.get("streams") or []:
        stype = stream.get("codec_type")
        tags = stream.get("tags") or {}
        lang = _normalize_stream_language(
            str(tags.get("language") or ""), str(tags.get("title") or "")
        )
        if stype == "video" and video_info is None:
            video_info = {
                "codec": stream.get("codec_name"),
                "profile": stream.get("profile"),
                "pixel_format": stream.get("pix_fmt"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "resolution": _resolution_label(stream.get("width"), stream.get("height")),
                "fps": _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
                "bit_rate": _to_int(stream.get("bit_rate")),
                "language": lang,
                **_color_and_hdr(stream),
            }
        elif stype == "audio":
            audio.append({
                "codec": stream.get("codec_name"),
                "profile": stream.get("profile"),
                "language": lang,
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
                "bit_rate": _to_int(stream.get("bit_rate")),
                "title": tags.get("title") or None,
            })
        elif stype == "subtitle":
            disp = stream.get("disposition") or {}
            embedded.append({
                "codec": stream.get("codec_name"),
                "language": lang,
                "title": tags.get("title") or None,
                "default": bool(disp.get("default")),
                "forced": bool(disp.get("forced")),
            })

    duration = fmt.get("duration")
    size = fmt.get("size")
    return {
        "container": fmt.get("format_name"),
        "duration_seconds": round(float(duration), 3) if duration else None,
        "size_bytes": int(size) if size else None,
        "bit_rate": _to_int(fmt.get("bit_rate")),
        "video": video_info,
        "audio": audio,
        "embedded_subtitles": embedded,
    }


def _to_int(value) -> int | None:
    """ffprobe 的码率字段可能是字符串/None/非法值，统一安全转 int。"""
    if value in (None, "", "N/A"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _color_and_hdr(stream: dict) -> dict:
    """从视频流提取色彩空间与 HDR/Dolby Vision 信息（需求 2.0 §10/§31）。

    ffprobe 判定信号：
    - color_transfer=smpte2084（PQ）+ bt2020  → HDR10
    - color_transfer=arib-std-b67（HLG）      → HLG
    - side_data "HDR dynamic metadata ..."    → HDR10+
    - codec dvhe/dvh1/dav1 或 side_data
      "DOVI configuration record"            → Dolby Vision（带 dv_profile）
    兼容旧版 ffprobe：优先 *_name 字段，回退到无后缀字段。
    """
    def _field(*names: str) -> str:
        for n in names:
            v = stream.get(n)
            if v:
                return str(v)
        return ""

    transfer = _field("color_transfer_name", "color_transfer").lower()
    primaries = _field("color_primaries_name", "color_primaries").lower()
    color_space = _field("color_space_name", "color_space")
    codec = str(stream.get("codec_name") or "").lower()
    side_types, dv_profile = _scan_side_data(stream.get("side_data_list"))

    is_pq = transfer == "smpte2084"
    is_hlg = transfer == "arib-std-b67"
    is_dv = (
        codec in ("dvh1", "dvhe", "dav1", "dvav")
        or "dovi configuration record" in side_types
        or "dolby-vision" in side_types
    )
    is_hdr10plus = any("hdr dynamic metadata" in t for t in side_types)

    formats: list[str] = []
    if is_dv:
        formats.append("Dolby Vision")
    if is_hdr10plus:
        formats.append("HDR10+")
    elif is_pq:
        formats.append("HDR10")
    if is_hlg:
        formats.append("HLG")

    hdr_format = " + ".join(formats) if formats else None
    return {
        "hdr": bool(formats),
        "hdr_format": hdr_format,
        "color_space": color_space or None,
        "color_primaries": primaries or None,
        "color_transfer": transfer or None,
        "dv_profile": dv_profile,
    }


def _scan_side_data(side_list) -> tuple[set[str], int | None]:
    """收集 side_data_list 中的类型名（小写）与 Dolby Vision profile。"""
    types: set[str] = set()
    dv_profile: int | None = None
    if not isinstance(side_list, list):
        return types, dv_profile
    for sd in side_list:
        if not isinstance(sd, dict):
            continue
        st = str(sd.get("side_data_type") or "").lower()
        if st:
            types.add(st)
        if "dovi" in st and dv_profile is None:
            for key in ("dv_profile", "dv_profile_compatibility"):
                v = sd.get(key)
                if isinstance(v, int):
                    dv_profile = v
                    break
    return types, dv_profile


def _parse_fps(value: str | None) -> float | None:
    """把 ffprobe 的 '24000/1001' 形式转成浮点帧率。"""
    if not value:
        return None
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            if float(den) == 0:
                return None
            return round(float(num) / float(den), 3)
        return round(float(value), 3)
    except (ValueError, ZeroDivisionError):
        return None


def _resolution_label(width: int | None, height: int | None) -> str | None:
    if not height:
        return None
    if height >= 2160:
        return "4K"
    return f"{height}p"


# ---------------------------------------------------------------- 语言识别

# 三字母 ISO 639-2 → 两字母；ffprobe 流的 language 标签常用三字母
_ISO_MAP = {
    "chi": "zh", "zho": "zh", "cmn": "zh",
    "eng": "en", "jpn": "ja", "kor": "ko",
    "fre": "fr", "fra": "fr", "ger": "de", "deu": "de",
    "spa": "es", "rus": "ru", "tha": "th",
}

# 文件名语言标签（小写）→ 规范化代码
_TOKEN_MAP = {
    "chs": "zh-Hans", "gb": "zh-Hans", "sc": "zh-Hans", "hans": "zh-Hans",
    "简体": "zh-Hans", "简中": "zh-Hans", "simplified": "zh-Hans",
    "cht": "zh-Hant", "big5": "zh-Hant", "tc": "zh-Hant", "hant": "zh-Hant",
    "繁體": "zh-Hant", "繁中": "zh-Hant", "traditional": "zh-Hant",
    "zh": "zh", "chi": "zh", "zho": "zh", "cmn": "zh", "cn": "zh",
    "chinese": "zh", "中文": "zh",
    "en": "en", "eng": "en", "english": "en", "英文": "en", "英语": "en",
    "ja": "ja", "jp": "ja", "jpn": "ja", "japanese": "ja", "日文": "ja", "日语": "ja",
    "ko": "ko", "kr": "ko", "kor": "ko", "korean": "ko", "韩文": "ko",
}

# 复合标签（在分词之前整体匹配，避免 zh-CN 被拆散）
_COMPOSITE_PATTERNS: list[tuple[str, str]] = [
    (r"zh[-_. ]?hans|zh[-_. ]?cn|zh[-_. ]?sg", "zh-Hans"),
    (r"zh[-_. ]?hant|zh[-_. ]?tw|zh[-_. ]?hk|zh[-_. ]?mo", "zh-Hant"),
]


def is_chinese(lang: str | None) -> bool:
    return bool(lang) and (lang == "zh" or lang.startswith("zh-"))


def extract_language_tags(text: str) -> list[str]:
    """从文件名片段（如 '.zh-CN.chs&eng'）中识别语言代码，具体标签优先。"""
    low = text.lower()
    found: set[str] = set()
    for pattern, code in _COMPOSITE_PATTERNS:
        if re.search(pattern, low):
            found.add(code)
    for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", low):
        if token in _TOKEN_MAP:
            found.add(_TOKEN_MAP[token])
    if "zh-Hans" in found or "zh-Hant" in found:
        found.discard("zh")
    return sorted(found)


def _normalize_stream_language(raw: str, title: str) -> str | None:
    """把 ffprobe 流的 language 标签规范化；und/空 视为未知。"""
    low = (raw or "").strip().lower()
    if low in ("", "und", "undetermined"):
        # 语言缺失时回退到流标题（如 "简体中文"）
        tags = extract_language_tags(title or "")
        return tags[0] if tags else None
    if low in _ISO_MAP:
        return _ISO_MAP[low]
    if is_chinese(low):
        return low
    return low or None


# ---------------------------------------------------------------- 外挂字幕

def find_external_subtitles(video: Path, subtitle_exts: frozenset[str]) -> list[dict]:
    """在同一目录中找“视频主名前缀”匹配的外挂字幕（规格 §16.1）。"""
    stem = video.stem.lower()
    result: list[dict] = []
    try:
        siblings = sorted(os.scandir(video.parent), key=lambda e: e.name)
    except OSError:
        return result
    for entry in siblings:
        if not entry.is_file(follow_symlinks=False):
            continue
        name = entry.name
        ext = Path(name).suffix.lstrip(".").lower()
        if ext not in subtitle_exts:
            continue
        sub_stem = str(Path(name).stem)
        if not sub_stem.lower().startswith(stem):
            continue
        remainder = sub_stem[len(stem):]
        # 后缀若以字母数字开头，说明是另一部片名（如 Movie.mkv vs Movie2.srt），不算匹配
        if remainder and remainder[0].isalnum():
            continue
        # remainder 为空表示完全同名（Movie.mkv + Movie.srt），同样有效
        languages = extract_language_tags(remainder) if remainder else []
        try:
            size = entry.stat(follow_symlinks=False).st_size
        except OSError:
            size = None
        result.append({
            "name": name,
            "path": str(Path(entry.path)),
            "extension": ext,
            "size_bytes": size,
            "languages": languages,
        })
    return result


# ---------------------------------------------------------------- 类型启发式

_TV_PATTERNS = [
    re.compile(r"s\d{1,2}\s?e\d{1,3}", re.IGNORECASE),
    re.compile(r"\b(?:ep?|episode)\s?\d{1,3}\b", re.IGNORECASE),
    re.compile(r"season\s*\d+", re.IGNORECASE),
    re.compile(r"第\s*\d+\s*[集话話季期]"),
]
_YEAR_PATTERN = re.compile(r"(?:^|[\s\(\[\._\-])((?:19|20)\d{2})(?:$|[\s\)\]\._\-])")
_ANIME_MARKERS = (
    "baha", "b-global", "at-x", "vcb-studio", "lolihouse", "nekomoe",
    "sumisora", "dmns", "ma10p", "anime",
)
_DOC_MARKERS = ("documentary", "纪录片", "bbc", "nhk", "national geographic", "discovery")
_VARIETY_MARKERS = ("variety", "综艺")

_MEDIA_TYPES = {"movie", "tv", "anime", "documentary", "variety", "unknown"}


def classify_media(path: Path) -> dict:
    """基于文件名/父目录名的启发式分类（规格 §15）。

    注意：这只是粗分类，国产/非国产识别属于后续阶段（规格 §18）。
    """
    texts = [path.stem] + [p.name for p in path.parents[:3]]
    blob = " ".join(texts).lower()

    for marker in _ANIME_MARKERS:
        if marker in blob:
            return {"type": "anime", "confidence": 0.75}
    for marker in _DOC_MARKERS:
        if marker in blob:
            return {"type": "documentary", "confidence": 0.7}
    for marker in _VARIETY_MARKERS:
        if marker in blob:
            return {"type": "variety", "confidence": 0.6}
    for pattern in _TV_PATTERNS:
        if pattern.search(blob):
            return {"type": "tv", "confidence": 0.9}
    if _YEAR_PATTERN.search(path.stem):
        return {"type": "movie", "confidence": 0.7}
    return {"type": "unknown", "confidence": 0.2}


# ---------------------------------------------------------------- 扫描主流程

def scan_media(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    media_cfg: MediaConfig,
    *,
    path: str = "",
    recursive: bool = True,
    probe: bool = True,
    missing_subtitle_only: bool = False,
    media_type: str = "",
    limit: int = 0,
    offset: int = 0,
) -> dict:
    if media_type and media_type.strip().lower() not in _MEDIA_TYPES:
        raise InvalidArguments(
            f"media_type 只支持 {'/'.join(sorted(_MEDIA_TYPES))}，得到: {media_type!r}"
        )
    type_filter = media_type.strip().lower() or None

    if path:
        base = guard.check(path)
        if not base.is_dir():
            raise NotADirectoryError(f"不是目录: {path}")
        bases = [base]
    else:
        bases = []
        for r in expand_roots(media_roots):
            b = guard.check(r)
            if b not in bases:
                bases.append(b)

    limit = min(limit if limit > 0 else limits.max_search_results, limits.max_search_results)
    offset = max(offset, 0)
    window_end = offset + limit

    errors: list[dict] = []
    seen_dedupe: set[str] = set()

    def add_error(p: Path, message: str, dedupe_key: str | None = None) -> None:
        if dedupe_key:
            if dedupe_key in seen_dedupe:
                return
            seen_dedupe.add(dedupe_key)
        if len(errors) < limits.max_errors_reported:
            errors.append({"path": str(p), "error": message})

    on_walk_error = lambda p, msg: add_error(p, msg)  # noqa: E731

    state = {"truncated": False}
    videos: list[dict] = []
    total_matched = 0
    scanned_files = 0
    skipped_by_filter = 0
    probe_attempts = 0
    probed_files = 0
    is_skipped = make_skip_matcher(media_cfg.skip_filename_patterns)

    def try_probe(fpath: Path, info: dict) -> None:
        """对单个视频跑 ffprobe。预算按“尝试次数”计（含失败），
        防止大量坏文件/超时文件导致扫描无限阻塞；失败只进 errors。"""
        nonlocal probe_attempts, probed_files
        if probe_attempts >= media_cfg.max_probe_files:
            return
        probe_attempts += 1
        try:
            pv = probe_video(fpath, media_cfg.ffprobe_path, media_cfg.ffprobe_timeout_seconds)
        except ProbeError as exc:
            add_error(fpath, str(exc), exc.dedupe_key)
            return
        probed_files += 1
        info["probed"] = True
        info["subtitle"]["embedded"] = pv["embedded_subtitles"]
        info["subtitle"]["embedded_checked"] = True
        info["media"] = {
            "container": pv["container"],
            "duration_seconds": pv["duration_seconds"],
            "video": pv["video"],
            "audio": pv["audio"],
        }
        if any(is_chinese(s["language"]) for s in pv["embedded_subtitles"]):
            info["chinese_subtitle_sources"] = info["chinese_subtitle_sources"] + ["embedded"]
            info["has_chinese_subtitle"] = True

    for base in bases:
        if not base.is_dir():
            add_error(base, "目录不存在或不可访问，已跳过")
            continue
        if recursive:
            records = _walk_records(
                base, guard, limits.max_scan_files, limits.max_list_depth,
                include_dirs=False, video_exts=video_exts, subtitle_exts=subtitle_exts,
                on_error=on_walk_error, state=state,
            )
        else:
            records = _flat_records(base, guard, video_exts, subtitle_exts, on_walk_error)

        for record in records:
            if record["media_category"] != "video":
                continue
            # 文件名跳过模式（家庭录像/成人内容）：不计入结果，单独计数
            if is_skipped(record["name"]):
                skipped_by_filter += 1
                continue
            scanned_files += 1
            fpath = Path(record["path"])

            external = find_external_subtitles(fpath, subtitle_exts)
            chinese_sources: list[str] = [
                lang for sub in external for lang in sub["languages"] if is_chinese(lang)
            ]
            info: dict = {
                "name": record["name"],
                "path": record["path"],
                "relative_path": record["relative_path"],
                "size_bytes": record["size_bytes"],
                "mtime": record["mtime"],
                "media_type": classify_media(fpath),
                "probed": False,
                "has_chinese_subtitle": bool(chinese_sources),
                "chinese_subtitle_sources": chinese_sources,
                "subtitle": {
                    "external": external,
                    "embedded": [],
                    "embedded_checked": False,
                },
                "media": None,
            }

            # 类型过滤是纯文件名启发式，不需要 ffprobe，先做
            if type_filter and info["media_type"]["type"] != type_filter:
                continue

            if probe and missing_subtitle_only:
                # 过滤依赖内嵌字幕检测结果：外挂已有中文字幕的直接排除（无需探测），
                # 其余候选必须探测才能判定；预算耗尽后按外挂结果乐观纳入，
                # 由 probe_budget_exhausted 标记提示结果可能偏多
                if not info["has_chinese_subtitle"]:
                    try_probe(fpath, info)
                if info["has_chinese_subtitle"]:
                    continue
                total_matched += 1
                if offset <= total_matched - 1 < window_end:
                    videos.append(info)
            else:
                if missing_subtitle_only and info["has_chinese_subtitle"]:
                    continue  # probe=false：仅按外挂字幕判断
                total_matched += 1
                if offset <= total_matched - 1 < window_end:
                    # 只探测落在返回窗口内的条目：
                    # 分页翻页时窗口前的条目不再重复 ffprobe
                    if probe:
                        try_probe(fpath, info)
                    videos.append(info)

    return {
        "ok": True,
        "roots": [str(b) for b in bases],
        "recursive": recursive,
        "probe": probe,
        "missing_subtitle_only": missing_subtitle_only,
        "media_type": type_filter,
        "offset": offset,
        "limit": limit,
        "total_matched": total_matched,
        "returned": len(videos),
        "truncated": total_matched > window_end,
        "scanned_files": scanned_files,
        "skipped_by_filter": skipped_by_filter,
        "probe_attempts": probe_attempts,
        "probed_files": probed_files,
        "probe_budget": media_cfg.max_probe_files,
        "probe_budget_exhausted": probe_attempts >= media_cfg.max_probe_files,
        "scan_truncated": state["truncated"],
        "errors": errors,
        "videos": videos,
    }


def check_subtitle(
    guard: PathGuard,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_cfg: MediaConfig,
    *,
    path: str,
) -> dict:
    """单视频文件的字幕详情：外挂 + 内嵌（ffprobe）。单文件失败不抛断路异常。"""
    resolved = guard.check(path)
    if not resolved.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if not resolved.is_file():
        raise InvalidArguments(f"不是文件: {path}")
    ext = resolved.suffix.lstrip(".").lower()
    if ext not in video_exts:
        raise InvalidArguments(f"不是支持的视频文件（.{ext}）；支持的扩展名: {sorted(video_exts)}")

    st = os.lstat(resolved)
    record = make_record(
        path=resolved, st=st, relative_to=resolved.parent,
        video_exts=video_exts, subtitle_exts=subtitle_exts,
    )
    external = find_external_subtitles(resolved, subtitle_exts)
    chinese_sources = [
        lang for sub in external for lang in sub["languages"] if is_chinese(lang)
    ]

    result: dict = {
        "ok": True,
        "name": record["name"],
        "path": record["path"],
        "size_bytes": record["size_bytes"],
        "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
        "media_type": classify_media(resolved),
        "probed": False,
        "probe_error": None,
        "has_chinese_subtitle": bool(chinese_sources),
        "chinese_subtitle_sources": chinese_sources,
        "subtitle": {
            "external": external,
            "embedded": [],
            "embedded_checked": False,
        },
        "media": None,
    }

    try:
        pv = probe_video(resolved, media_cfg.ffprobe_path, media_cfg.ffprobe_timeout_seconds)
    except ProbeError as exc:
        result["probe_error"] = str(exc)
    else:
        result["probed"] = True
        result["subtitle"]["embedded"] = pv["embedded_subtitles"]
        result["subtitle"]["embedded_checked"] = True
        result["media"] = {
            "container": pv["container"],
            "duration_seconds": pv["duration_seconds"],
            "video": pv["video"],
            "audio": pv["audio"],
        }
        embedded_zh = any(is_chinese(s["language"]) for s in pv["embedded_subtitles"])
        if embedded_zh:
            result["chinese_subtitle_sources"] = chinese_sources + ["embedded"]
            result["has_chinese_subtitle"] = True

    return result
