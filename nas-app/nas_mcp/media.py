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
from .fs_utils import ErrorSink, make_record, make_skip_matcher, mark_incomplete_from_sink
from .progress import check_canceled, mark_partial, report
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
                "bit_depth": _bit_depth(stream.get("pix_fmt"), stream.get("bits_per_raw_sample")),
                "language": lang,
                **_color_and_hdr(stream),
            }
        elif stype == "audio":
            audio.append(_audio_info(stream, tags, lang))
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


# 杜比视界兼容层标识（dv_bl_signal_compatibility_id）→ 中文说明
_DV_COMPAT_LABELS = {
    0: "仅杜比视界（无兼容层）",
    1: "兼容 HDR10",
    2: "兼容 SDR",
    4: "兼容 HLG",
}
# ffprobe codec_name → 紧凑音频编码名
_AUDIO_CODEC_LABELS = {
    "truehd": "TrueHD",
    "eac3": "DD+",
    "ac3": "AC-3",
    "dts": "DTS",
    "aac": "AAC",
    "flac": "FLAC",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "mp3": "MP3",
    "pcm_s16le": "PCM",
    "pcm_s24le": "PCM",
    "pcm_bluray": "PCM",
}
_DTS_PROFILE_RE = re.compile(r"dts[:\-\s]?x|dts[\s\-]?hd[\s\-]?ma|dts[\s\-]?hd", re.IGNORECASE)
_ATMOS_RE = re.compile(r"atmos|joc", re.IGNORECASE)


def _bit_depth(pix_fmt, bits_per_raw_sample=None) -> int | None:
    """从像素格式/原始位深字段取位深（8/10/12 bit）。"""
    raw = _to_int(bits_per_raw_sample)
    if raw and 0 < raw <= 32:
        return raw
    m = re.search(r"p(\d{2,3})(?:le|be)?$", str(pix_fmt or ""))
    return int(m.group(1)) if m else None


def _channel_layout_label(layout, channels) -> str | None:
    """声道布局标签：优先 ffprobe 的 channel_layout（保留 5.1.2/7.1.4），
    缺失时按声道数给常用布局（1.0/2.0/5.1/7.1）。"""
    text = re.sub(r"\(.*?\)", "", str(layout or "")).strip()
    if text:
        return text
    n = _to_int(channels)
    return {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(n) or (f"{n} 声道" if n else None)


def _audio_info(stream: dict, tags: dict, lang: str | None) -> dict:
    """音频流：编码、声道布局与 Dolby Atmos / DTS:X 等认证规格。"""
    codec = str(stream.get("codec_name") or "").lower()
    profile = str(stream.get("profile") or "")
    title = str(tags.get("title") or "")
    blob = f"{profile} {title} {codec}".lower()

    codec_label = _AUDIO_CODEC_LABELS.get(codec) or codec.upper() or None
    dts_profile = None
    if codec == "dts":
        m = _DTS_PROFILE_RE.search(profile or title)
        if m:
            raw = m.group(0).upper().replace("-", "-")
            if "MA" in raw:
                dts_profile = "DTS-HD MA"
            elif "X" in raw.split("DTS")[-1]:
                dts_profile = "DTS:X"
            else:
                dts_profile = "DTS-HD"
            codec_label = dts_profile

    atmos = bool(_ATMOS_RE.search(blob)) and codec in ("truehd", "eac3", "ac3")
    atmos_type = None
    if atmos:
        atmos_type = "TrueHD Atmos" if codec == "truehd" else "DD+ JOC"
    dts_x = bool(re.search(r"dts[:\-\s]?x", blob)) or dts_profile == "DTS:X"

    return {
        "codec": stream.get("codec_name"),
        "codec_label": codec_label,
        "profile": stream.get("profile"),
        "language": lang,
        "channels": stream.get("channels"),
        "channel_layout": stream.get("channel_layout"),
        "channel_layout_label": _channel_layout_label(
            stream.get("channel_layout"), stream.get("channels")
        ),
        "bit_rate": _to_int(stream.get("bit_rate")),
        "bit_depth": _bit_depth(None, stream.get("bits_per_raw_sample")),
        "sample_rate": _to_int(stream.get("sample_rate")),
        "title": tags.get("title") or None,
        "atmos": atmos,
        "atmos_type": atmos_type,
        "dts_x": dts_x,
        "dts_profile": dts_profile,
    }


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
    side_types, dv = _scan_side_data(stream.get("side_data_list"))

    is_pq = transfer == "smpte2084"
    is_hlg = transfer == "arib-std-b67"
    is_dv = (
        codec in ("dvh1", "dvhe", "dav1", "dvav")
        or "dovi configuration record" in side_types
        or "dolby-vision" in side_types
    )
    is_hdr10plus = any("hdr dynamic metadata" in t for t in side_types)

    dv_profile = dv.get("dv_profile")
    compat = dv.get("dv_bl_signal_compatibility_id")
    if compat is None:
        compat = dv.get("dv_profile_compatibility")
    profile_label = None
    if isinstance(dv_profile, int):
        profile_label = f"Profile {dv_profile}"
        # Profile 8 的 .1/.2/.4 由兼容层决定：HDR10 / SDR / HLG
        if dv_profile == 8 and compat in (1, 2, 4):
            profile_label += f".{compat}"
    compat_label = _DV_COMPAT_LABELS.get(compat) if isinstance(compat, int) else None

    layers = None
    flags = [
        (bool(dv.get("bl_present_flag")), "BL"),
        (bool(dv.get("el_present_flag")), "EL"),
        (bool(dv.get("rpu_present_flag")), "RPU"),
    ]
    if any(flag for flag, _ in flags):
        layers = "+".join(name for flag, name in flags if flag)

    # HDR 基础层（与杜比视界并列展示，不互相替代）：
    # 杜比视界 Profile 5 / 兼容标识 0 没有 HDR10 兼容层，不能标成 HDR10。
    base: list[str] = []
    if is_hdr10plus:
        base.append("HDR10+")
    elif is_pq:
        base.append("HDR10")
    if is_hlg and "HLG" not in base:
        base.append("HLG")
    if is_dv:
        if compat == 0 or (dv_profile == 5 and compat not in (1, 4)):
            base = []
        elif compat == 2:
            base = [x for x in base if x not in ("HDR10", "HLG")]
        elif compat == 4 and "HLG" not in base:
            base.append("HLG")
        elif compat == 1 and "HDR10" not in base and "HDR10+" not in base:
            base.append("HDR10")

    formats: list[str] = []
    if is_dv:
        formats.append("Dolby Vision")
    formats.extend(base)
    hdr_format = " + ".join(formats) if formats else None
    range_label = " · ".join(
        ([profile_label or "Dolby Vision"] if is_dv else []) + base
    ) if formats else "SDR"
    hdr_primary = formats[0] if formats else None

    return {
        "hdr": bool(formats),
        "hdr_format": hdr_format,
        "hdr_primary": hdr_primary,
        "hdr_types": formats or None,
        "dynamic_range": range_label,
        "sdr": not bool(formats),
        "hdr10": "HDR10" in formats,
        "hdr10_plus": "HDR10+" in formats,
        "hlg": "HLG" in formats,
        "dolby_vision": is_dv,
        "dv_profile": dv_profile if isinstance(dv_profile, int) else None,
        "dv_profile_label": profile_label,
        "dv_level": dv.get("dv_level") if isinstance(dv.get("dv_level"), int) else None,
        "dv_bl_signal_compatibility_id": compat if isinstance(compat, int) else None,
        "dv_compatibility_label": compat_label,
        "dv_layers": layers,
        "color_space": color_space or None,
        "color_primaries": primaries or None,
        "color_transfer": transfer or None,
    }


def _scan_side_data(side_list) -> tuple[set[str], dict]:
    """收集 side_data_list 中的类型名（小写）与完整 Dolby Vision 配置记录。"""
    types: set[str] = set()
    dv: dict = {}
    if not isinstance(side_list, list):
        return types, dv
    for sd in side_list:
        if not isinstance(sd, dict):
            continue
        st = str(sd.get("side_data_type") or "").lower()
        if st:
            types.add(st)
        if "dovi" in st or "dolby-vision" in st:
            for key in (
                "dv_profile", "dv_level", "dv_bl_signal_compatibility_id",
                "dv_profile_compatibility", "bl_present_flag", "el_present_flag",
                "rpu_present_flag", "dv_version_major", "dv_version_minor",
            ):
                if key in sd and sd[key] is not None and key not in dv:
                    dv[key] = sd[key]
    return types, dv


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
    """分辨率标签计算（支持 16:9 标准与 2.40:1/2:1 等超宽裁黑画幅）。

    - 8K: 宽>=7000 或 高>=4000
    - 4K: 宽>=3600 且 高>=1400（覆盖 3840x1600/1606/1920/2160 等 4K Scope）或 高>=2000
    - 1080p: 宽>=1800 且 高>=700（覆盖 1920x800/960/1080）或 高>=900
    - 720p: 宽>=1200 且 高>=500（覆盖 1280x536/720）或 高>=700
    - 480p: 宽>=800 且 高>=400 或 高>=480
    - 兜底: {height}p 或 {width}w
    """
    if not height and not width:
        return None
    w = width or 0
    h = height or 0
    if w >= 7000 or h >= 4000:
        return "8K"
    if (w >= 3600 and h >= 1400) or h >= 2000:
        return "4K"
    if (w >= 1800 and h >= 700) or h >= 900:
        return "1080p"
    if (w >= 1200 and h >= 500) or h >= 700:
        return "720p"
    if (w >= 800 and h >= 400) or h >= 480:
        return "480p"
    return f"{h}p" if h else f"{w}w"


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
    "chs": "zh-Hans", "gb": "zh-Hans", "gb2312": "zh-Hans", "sc": "zh-Hans",
    "hans": "zh-Hans", "简体": "zh-Hans", "简中": "zh-Hans", "simplified": "zh-Hans",
    "cht": "zh-Hant", "big5": "zh-Hant", "tc": "zh-Hant", "hant": "zh-Hant",
    "繁體": "zh-Hant", "繁体": "zh-Hant", "正體": "zh-Hant", "正体": "zh-Hant",
    "繁中": "zh-Hant", "traditional": "zh-Hant",
    "zh": "zh", "chi": "zh", "zho": "zh", "cmn": "zh", "cn": "zh",
    "chinese": "zh", "中文": "zh", "中字": "zh",
    "en": "en", "eng": "en", "english": "en", "英文": "en", "英语": "en",
    "英語": "en", "英": "en",
    "bilingual": "zh-Hans", "dual": "zh-Hans",
    "ja": "ja", "jp": "ja", "jpn": "ja", "japanese": "ja", "日文": "ja",
    "日语": "ja", "日語": "ja", "日": "ja",
    "ko": "ko", "kr": "ko", "kor": "ko", "korean": "ko", "韩文": "ko",
    "韩语": "ko", "韓語": "ko", "韓文": "ko",
}

# 复合标签（在分词之前整体匹配，避免 zh-CN 被拆散）
_COMPOSITE_PATTERNS: list[tuple[str, str]] = [
    (r"zh[-_. ]?hans|zh[-_. ]?cn|zh[-_. ]?sg", "zh-Hans"),
    (r"zh[-_. ]?hant|zh[-_. ]?tw|zh[-_. ]?hk|zh[-_. ]?mo", "zh-Hant"),
]

# CJK 关键词：中文标签常把多个词连写（简体中文/中英双语/中文繁体），
# 按空白分词会把整串当成一个 token 而查不到表，这里改用子串匹配。
# 顺序敏感：繁体/简繁复合词必须先于“中文/简体”匹配。
_BILINGUAL = ("zh-Hans", "en")
_CJK_KEYWORDS: list[tuple[str, str | tuple[str, ...]]] = [
    ("简繁", ("zh-Hans", "zh-Hant")), ("簡繁", ("zh-Hans", "zh-Hant")),
    ("繁體中文", "zh-Hant"), ("繁体中文", "zh-Hant"),
    ("中文繁体", "zh-Hant"), ("中文繁體", "zh-Hant"),
    ("繁體", "zh-Hant"), ("繁体", "zh-Hant"),
    ("正體", "zh-Hant"), ("正体", "zh-Hant"),
    ("简体中文", "zh-Hans"), ("簡體", "zh-Hans"),
    ("简体", "zh-Hans"), ("简中", "zh-Hans"),
    ("中英双语", _BILINGUAL), ("中英雙語", _BILINGUAL), ("中英", _BILINGUAL),
    ("简英", _BILINGUAL), ("簡英", _BILINGUAL),
    ("双语", _BILINGUAL), ("雙語", _BILINGUAL),
    ("中文字幕", "zh"), ("中文", "zh"), ("中字", "zh"),
    ("英文字幕", "en"), ("英文", "en"), ("英语", "en"), ("英語", "en"),
    ("日文字幕", "ja"), ("日文", "ja"), ("日语", "ja"), ("日語", "ja"),
    ("韩文字幕", "ko"), ("韩文", "ko"), ("韩语", "ko"), ("韓語", "ko"), ("韓文", "ko"),
]

# 繁体/正体标记：一律不接受（补全判定与下载过滤都以简中为准）
_TRADITIONAL_CODES = frozenset({
    "cht", "big5", "tc", "hant", "traditional",
    "zh-hant", "zh-tw", "zh-hk", "zh-mo",
    "繁体", "繁體", "繁中", "繁体中文", "繁體中文",
    "正体", "正體", "中文繁体", "中文繁體",
})
# 通用/简体中文代码：视为“确有中文字幕”
_SIMPLIFIED_CODES = frozenset({
    "zh", "chi", "zho", "cmn", "chinese", "cn", "中文", "中字",
    "zh-hans", "chs", "sc", "gb", "gb2312", "simplified", "hans",
    "简体", "简中", "简体中文", "簡體", "簡中", "簡體中文",
})


def _split_lang_codes(lang: str | None) -> list[str]:
    """把 'zh-Hans+en' / 'chs&eng' 这类组合标签拆成单个代码。"""
    return [p for p in re.split(r"[+&/,|;]+", (lang or "").strip().lower()) if p]


def is_traditional(lang: str | None) -> bool:
    """是否带繁体/正体标记（CHT/Big5/zh-Hant…）。"""
    for part in _split_lang_codes(lang):
        if part in _TRADITIONAL_CODES or part.startswith("zh-hant"):
            return True
    return False


def is_chinese(lang: str | None) -> bool:
    """是否包含“可接受的简中”（zh-Hans 或通用 zh）；繁体/正体一律不算。"""
    for part in _split_lang_codes(lang):
        if part in _SIMPLIFIED_CODES:
            return True
        if part.startswith("zh-") and not is_traditional(part):
            return True
    return False


def extract_language_tags(text: str) -> list[str]:
    """从字幕文件名/流标题中识别语言代码，具体标签优先（中英双语→zh-Hans+en）。"""
    low = (text or "").lower()
    found: set[str] = set()
    for pattern, code in _COMPOSITE_PATTERNS:
        if re.search(pattern, low):
            found.add(code)
    for keyword, code in _CJK_KEYWORDS:
        if keyword in low:
            if isinstance(code, tuple):
                found.update(code)
            else:
                found.add(code)
    for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", low):
        if token in _TOKEN_MAP:
            found.add(_TOKEN_MAP[token])
    if "zh-Hans" in found or "zh-Hant" in found:
        found.discard("zh")
    return sorted(found)


def _normalize_stream_language(raw: str, title: str) -> str | None:
    """把 ffprobe 流的 language 标签规范化；und/空 时回退流标题。"""
    low = (raw or "").strip().lower()
    if low in ("", "und", "undetermined"):
        # 语言缺失时回退到流标题（如 "简体中文"/"中英双语"）
        tags = extract_language_tags(title or "")
        return "+".join(tags) if tags else None
    if low in _ISO_MAP:
        return _ISO_MAP[low]
    if is_chinese(low):
        return low
    return low or None


# 通用中文代码（不区分简繁）：语言标签只给这些时需再看流标题确认
_GENERIC_ZH_CODES = frozenset({"zh", "chi", "zho", "cmn", "chinese", "cn", "中文", "中字"})


def stream_has_chinese(stream: dict) -> bool:
    """内嵌字幕流是否提供“可接受的简中”（繁体/正体一律不算）。

    - forced 轨（仅特效/强制字幕）不算完整简中；
    - 明确简中（zh-Hans/chs/gb…）直接接受；
    - 明确繁体（zh-Hant/CHT/Big5/正體…）直接拒绝，标题不翻案；
    - 通用 zh/chi/zho 或 und/空：再看流标题（简体/中英双语→接受，
      繁體/正體→拒绝，无标题语言信息时沿用“通用 zh 视为中文”）。
    """
    if stream.get("forced"):
        return False
    lang_parts = _split_lang_codes(stream.get("language"))
    title_tags = extract_language_tags(stream.get("title") or "")
    explicit_simplified = any(
        p in _SIMPLIFIED_CODES and p not in _GENERIC_ZH_CODES for p in lang_parts
    )
    if explicit_simplified:
        return True
    if any(is_traditional(p) for p in lang_parts):
        return False
    if title_tags:
        return any(is_chinese(t) for t in title_tags)
    return any(is_chinese(p) for p in lang_parts)


def stream_is_traditional(stream: dict) -> bool:
    """内嵌字幕流是否明确是繁体/正体（forced 轨不计）。"""
    if stream.get("forced"):
        return False
    lang_parts = _split_lang_codes(stream.get("language"))
    if any(is_traditional(p) for p in lang_parts):
        return True
    title_tags = extract_language_tags(stream.get("title") or "")
    return any(is_traditional(t) for t in title_tags)


# 无法用于判定的语言代码（缺失/未定/多语言/无语言内容）
_UNKNOWN_LANG_CODES = frozenset({"", "und", "undetermined", "unknown", "mul", "zxx", "mis"})


def _lang_is_explicit_other(lang: str | None) -> bool:
    """是否带明确且非中文的语言标记（如 en/ja），用于区分“外语字幕”与“语言未知”。"""
    for part in _split_lang_codes(lang):
        if part in _UNKNOWN_LANG_CODES:
            continue
        if is_chinese(part) or is_traditional(part):
            continue
        return True
    return False


def stream_is_explicit_other(stream: dict) -> bool:
    """内嵌流是否明确标注为非中文语言（forced 轨不计）。"""
    if stream.get("forced"):
        return False
    if _lang_is_explicit_other(stream.get("language")):
        return True
    return any(
        _lang_is_explicit_other(tag)
        for tag in extract_language_tags(stream.get("title") or "")
    )


def subtitle_state(
    external: list[dict],
    embedded: list[dict],
    probe_error: str | None = None,
) -> dict:
    """三态字幕语言判定：known_zh / known_non_zh / unknown / none。

    - 外挂语言为空（如 PGS `.sup` 无语言标记）不算“没有字幕”，归为 unknown；
    - 仅繁体属 known_non_zh，并单独标记 has_traditional_subtitle；
    - has_any_subtitle 只看内嵌/外挂是否存在，与语言判定解耦。
    """
    has_any = bool(external) or bool(embedded)
    external_zh = any(
        is_chinese(lang) for sub in external for lang in sub.get("languages", [])
    )
    embedded_zh = any(stream_has_chinese(s) for s in embedded)
    has_zh = external_zh or embedded_zh

    has_trad = any(
        is_traditional(lang) for sub in external for lang in sub.get("languages", [])
    ) or any(stream_is_traditional(s) for s in embedded)

    explicit_other = any(
        _lang_is_explicit_other(lang)
        for sub in external for lang in sub.get("languages", [])
    ) or any(stream_is_explicit_other(s) for s in embedded)

    if has_zh:
        status = "known_zh"
    elif has_trad or explicit_other:
        status = "known_non_zh"
    elif has_any or probe_error:
        status = "unknown"
    else:
        status = "none"

    return {
        "has_any_subtitle": has_any,
        "language_status": status,
        "has_traditional_subtitle": has_trad,
    }


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

    sink = ErrorSink(limits.max_errors_reported)
    seen_dedupe: set[str] = set()

    def add_error(p: Path, message: str, dedupe_key: str | None = None) -> None:
        if dedupe_key:
            if dedupe_key in seen_dedupe:
                return
            seen_dedupe.add(dedupe_key)
        # 权限错误（固有不可读目录）经 ErrorSink 做进程级增量上报（#5）
        sink.add(p, message)

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
        if any(stream_has_chinese(s) for s in pv["embedded_subtitles"]):
            info["chinese_subtitle_sources"] = info["chinese_subtitle_sources"] + ["embedded"]
            info["has_chinese_subtitle"] = True

    canceled = False
    for base_index, base in enumerate(bases, 1):
        if check_canceled():
            canceled = True
            break
        report(
            "scan", "扫描目录并采集视频",
            done=base_index - 1, total=len(bases), current=str(base),
        )
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

        processed_videos = 0
        for record in records:
            if record["media_category"] != "video":
                continue
            if check_canceled():
                canceled = True
                break
            # 目录遍历是惰性的：进入文件前无法预知本根总量，total 留空；
            # done 表示已处理（含跳过）的视频数，保证取消能逐文件生效。
            report(
                "analyze", "分析视频文件",
                done=processed_videos, total=None, current=record["name"],
            )
            processed_videos += 1
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

        if canceled:
            break

    result = {
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
        "errors": sink.errors,
        "errors_summary": sink.summary(),
        "videos": videos,
    }
    # T3：分页窗口可能为空但库里有数据，空的判定用 total_matched
    result = mark_incomplete_from_sink(result, sink, empty=total_matched == 0)
    if canceled:
        report("canceled", "已取消，保留已完成部分", done=scanned_files, current=None)
        mark_partial(result)
    else:
        report("done", "扫描完成", done=scanned_files, total=scanned_files)
    return result


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
        embedded_zh = any(stream_has_chinese(s) for s in pv["embedded_subtitles"])
        if embedded_zh:
            result["chinese_subtitle_sources"] = chinese_sources + ["embedded"]
            result["has_chinese_subtitle"] = True

    # 新增三态字段：不改动 has_chinese_subtitle 的既有语义，供 UI 区分
    # “没有字幕 / 只有繁体 / 语言未知 / 外语字幕”。
    result.update(
        subtitle_state(
            result["subtitle"]["external"],
            result["subtitle"]["embedded"],
            result["probe_error"],
        )
    )
    return result
