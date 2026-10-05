"""资源标题（release name）结构化解析与质量评分（需求 2.0 §25）。

把 Jackett/Prowlarr 常见的发布名拆成机器可用的质量维度，而不是让 LLM 猜。
纯函数、无网络、无副作用，便于单测与后续 media_quality_analyze 复用。

示例：
    Interstellar.2014.2160p.UHD.BluRay.REMUX.HEVC.DTS-HD.MA.5.1-FraMeSTO
    → resolution=2160p source_type=REMUX video_codec=HEVC
      audio_codec=DTS-HD MA channels=5.1 score=100 tier=excellent
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 归一化：统一大写，分隔符 . _ - 与多空格统一为单空格，便于整串匹配
_SEP_RE = re.compile(r"[._\-]+")
_SPACE_RE = re.compile(r"\s+")


def _norm(title: str) -> str:
    return _SPACE_RE.sub(" ", _SEP_RE.sub(" ", title.upper())).strip()


# ------------------------------------------------------------- 各维度匹配

# 分辨率：4320p/8K → 2160p/4K/UHD → 1080p(FHD) → 720p → 480/576p/SD
_RES_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("4320p", re.compile(r"\b(?:4320P|8K)\b")),
    ("2160p", re.compile(r"\b(?:2160P|4K|UHD)\b")),
    ("1080p", re.compile(r"\b(?:1080[PI]|FHD)\b")),
    ("720p", re.compile(r"\b720P\b")),
    ("480p", re.compile(r"\b(?:480[PI]|576[PI])\b")),
    ("SD", re.compile(r"\bSD\b")),
)

# 来源类型：必须先判多词/特定形态，顺序即质量从高到低
# REMUX 是蓝光无损直灌；BDRip/BRRip 属蓝光转压；CAM/TS 为枪版
_SOURCE_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("REMUX", re.compile(r"\bREMUX\b")),
    ("BluRay", re.compile(r"\b(?:BLU?\s?RAY|BDRIP|BRRIP)\b")),
    ("WEB-DL", re.compile(r"\b(?:WEB[-\s]?DL|WEBDL)\b")),
    ("WEBRip", re.compile(r"\bWEB\s?RIP\b")),
    ("HDTV", re.compile(r"\bHDTV\b")),
    ("DVDRip", re.compile(r"\bDVD(?:RIP)?\b")),
    ("CAM", re.compile(r"\b(?:HDTC|HDCAM|TELESYNC|\bTS\b|\bTC\b|\bCAM\b)\b")),
)

_VIDEO_CODEC_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("AV1", re.compile(r"\bAV1\b")),
    ("HEVC", re.compile(r"\b(?:X265|H\.?265|HEVC)\b")),
    ("AVC", re.compile(r"\b(?:X264|H\.?264|AVC)\b")),
    ("XviD", re.compile(r"\b(?:XVID|DIVX)\b")),
)

# HDR 形态：DV / HDR10+ 优先于 HDR10 优先于泛 HDR
_HDR_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("Dolby Vision", re.compile(r"\b(?:DOLBY\s?VISION|DOLBYVISION|DOVI|\bDV\b)\b")),
    ("HDR10+", re.compile(r"\bHDR\s?10\s?\+|HDR10PLUS\b")),
    ("HDR10", re.compile(r"\bHDR\s?10\b")),
    ("HLG", re.compile(r"\bHLG\b")),
    ("HDR", re.compile(r"\bHDR\b")),
)

# 音频编码：从最具体到最宽泛（Atmos 是对象层，单独标记）
_AUDIO_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("DTS-HD MA", re.compile(r"\bDTS[\s\-]?HD[\s\.]+MA(?:STER)?\b")),
    ("TrueHD", re.compile(r"\bTRUE[\s\-]?HD\b")),
    ("DTS-HD", re.compile(r"\bDTS[\s\-]?HD\b")),
    ("E-AC-3", re.compile(r"\b(?:E[\s\-]?AC[\s\-]?3|EAC3|DD\s?\+|DOLBY\s?DIGITAL\s?PLUS)\b")),
    ("DTS", re.compile(r"\bDTS\b")),
    ("AC-3", re.compile(r"\b(?:AC[\s\-]?3|\bDD\b|DOLBY\s?DIGITAL)\b")),
    ("FLAC", re.compile(r"\bFLAC\b")),
    ("AAC", re.compile(r"\bAAC\b")),
    ("Opus", re.compile(r"\bOPUS\b")),
)
_ATMOS_RE = re.compile(r"\bATMOS\b")

# 声道：7.1.4 / 5.1.4 / 7.1 / 5.1 / 2.0 等
_CHANNELS_RE = re.compile(r"\b([2-9](?:\.\d){1,2})\b")
_BIT_DEPTH_RE = re.compile(r"\b(8|10|12)[\s\-]?BIT\b")
_3D_RE = re.compile(r"\b(?:3D|HALF[\s\-]?SBS|HSBS|FULL[\s\-]?SBS)\b")


# ------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class ReleaseInfo:
    """release name 结构化结果；无法识别的维度为 None。"""

    resolution: str | None = None
    source_type: str | None = None        # REMUX/BluRay/WEB-DL/WEBRip/HDTV/DVDRip/CAM
    video_codec: str | None = None        # HEVC/AVC/AV1/XviD
    hdr: str | None = None                # Dolby Vision/HDR10+/HDR10/HLG/HDR
    audio_codec: str | None = None
    audio_channels: str | None = None
    bit_depth: int | None = None
    is_3d: bool = False
    has_atmos: bool = False
    quality_score: int = 0                # 0-100
    quality_tier: str = "unknown"         # excellent/good/fair/poor/bad/unknown
    matched_tags: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------- 评分权重

_RES_SCORE = {"4320p": 40, "2160p": 40, "1080p": 30, "720p": 18, "480p": 8, "SD": 5}
_SOURCE_SCORE = {
    "REMUX": 35, "BluRay": 30, "WEB-DL": 25, "WEBRip": 18,
    "HDTV": 12, "DVDRip": 8, "CAM": 0,
}
_CODEC_SCORE = {"HEVC": 8, "AV1": 8, "AVC": 6, "XviD": 2}
_HDR_SCORE = {"Dolby Vision": 10, "HDR10+": 10, "HDR10": 7, "HLG": 7, "HDR": 6}
_LOSSLESS_AUDIO = {"DTS-HD MA", "TrueHD"}


def quality_score_from_dimensions(
    *,
    resolution: str | None,
    source_type: str | None,
    video_codec: str | None,
    hdr: str | None,
    audio_codec: str | None,
    has_atmos: bool,
    bit_depth: int | None,
) -> tuple[int, str]:
    """按各质量维度计算 0-100 分与档位（release 标题与本地 ffprobe 共用）。"""
    score = _RES_SCORE.get(resolution, 10)
    score += _SOURCE_SCORE.get(source_type, 10)
    score += _CODEC_SCORE.get(video_codec, 0)
    score += _HDR_SCORE.get(hdr, 0)
    if has_atmos:
        score += 7
    elif audio_codec in _LOSSLESS_AUDIO:
        score += 7
    elif audio_codec in {"DTS-HD", "DTS"}:
        score += 5
    elif audio_codec in {"E-AC-3", "AC-3"}:
        score += 3
    if bit_depth and bit_depth >= 10:
        score += 2
    score = max(0, min(100, score))

    if source_type == "CAM":
        tier = "bad"
    elif score >= 85:
        tier = "excellent"
    elif score >= 70:
        tier = "good"
    elif score >= 50:
        tier = "fair"
    elif score >= 30:
        tier = "poor"
    else:
        tier = "bad"
    return score, tier


def _score(info: dict) -> tuple[int, str]:
    return quality_score_from_dimensions(
        resolution=info["resolution"],
        source_type=info["source_type"],
        video_codec=info["video_codec"],
        hdr=info["hdr"],
        audio_codec=info["audio_codec"],
        has_atmos=info["has_atmos"],
        bit_depth=info["bit_depth"],
    )


def _first(patterns: tuple[tuple[str, re.Pattern], ...], text: str) -> tuple[str | None, str | None]:
    for label, pat in patterns:
        if pat.search(text):
            return label, pat.pattern
    return None, None


def parse_release(title: str) -> ReleaseInfo:
    """解析单个 release name，返回结构化质量信息（不抛异常）。"""
    if not title or not title.strip():
        return ReleaseInfo()
    norm = _norm(title)
    # 声道形如 5.1 / 7.1.4，点号不能被分隔符归一化吃掉，故在原始大写串上提取
    upper = title.upper()

    resolution, _ = _first(_RES_RULES, norm)
    source_type, _ = _first(_SOURCE_RULES, norm)
    video_codec, _ = _first(_VIDEO_CODEC_RULES, norm)
    hdr, _ = _first(_HDR_RULES, norm)
    audio_codec, _ = _first(_AUDIO_RULES, norm)
    has_atmos = bool(_ATMOS_RE.search(norm))

    ch = _CHANNELS_RE.search(upper)
    bd = _BIT_DEPTH_RE.search(norm)

    info = {
        "resolution": resolution,
        "source_type": source_type,
        "video_codec": video_codec,
        "hdr": hdr,
        "audio_codec": audio_codec,
        "audio_channels": ch.group(1) if ch else None,
        "bit_depth": int(bd.group(1)) if bd else None,
        "is_3d": bool(_3D_RE.search(norm)),
        "has_atmos": has_atmos,
    }
    score, tier = _score(info)

    tags = [
        v for v in (
            resolution, source_type, video_codec, hdr, audio_codec,
            info["audio_channels"],
            f"{info['bit_depth']}bit" if info["bit_depth"] else None,
            "Atmos" if has_atmos else None,
            "3D" if info["is_3d"] else None,
        ) if v
    ]
    return ReleaseInfo(
        **info,
        quality_score=score,
        quality_tier=tier,
        matched_tags=tuple(tags),
    )


def release_dict(title: str) -> dict:
    """parse_release 的 dict 形态（供 API 返回）。"""
    r = parse_release(title)
    return {
        "resolution": r.resolution,
        "source_type": r.source_type,
        "video_codec": r.video_codec,
        "hdr": r.hdr,
        "audio_codec": r.audio_codec,
        "audio_channels": r.audio_channels,
        "bit_depth": r.bit_depth,
        "is_3d": r.is_3d,
        "has_atmos": r.has_atmos,
        "quality_score": r.quality_score,
        "quality_tier": r.quality_tier,
        "matched_tags": list(r.matched_tags),
    }
