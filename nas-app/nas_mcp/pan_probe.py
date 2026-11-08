# -*- coding: utf-8 -*-
"""网盘分享链接透视与真实画质校验探针（pan_probe）。

解决痛点：网盘分享链接（如 SeedHub / dysbk）常见标题夸大/虚标（如标题声称 4K DV，
实际内部文件为 1080p 或单集几百兆低清压制）。

核心功能：
1. 主流网盘轻量穿透（以夸克 pan.quark.cn 为主，免登录 REST 接口）：
   - 解析分享链接的真实文件清单、文件大小、视频文件分布；
   - 支持单级子文件夹自动下探（如根目录下仅有单个剧集主目录时）；
   - 超时与错误强隔离（fail-fast，默认 3.0s 超时），探测失败优雅降级；
2. 真实文件画质与物理体积交叉校验：
   - 从真实视频文件名中提取真实分辨率（4K / 1080p / 720p）、HDR / DV、来源规格；
   - 物理体积下限常识校验（4K 剧集单集 < 1.2GB、4K 电影 < 3.0GB 判定为虚标/假 4K）；
   - 确凿不符时标记 mismatch=True 与 mismatch_reason；
3. 多网盘置信度治理：
   - 夸克探针验证且吻合 → confidence="high", verified=True；
   - 确凿虚标 → confidence="mismatch", mismatch=True；
   - 非夸克（百度/迅雷/UC 等暂无免登录探针）或探针失败 → confidence="unverified",
     附带风险提示，保持未验证标记。
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("nas_mcp.pan_probe")

DEFAULT_PROBE_TIMEOUT = 3.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 "
    "QuarkCloudDrive/2.5.20"
)

VIDEO_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".ts", ".avi", ".mov", ".m2ts",
    ".iso", ".flv", ".wmv", ".strm",
})

_RES_RE = re.compile(r"(?<![A-Za-z0-9])(?:HD)?(4K|2160p|UHD|1080p|1080i|FHD|720p)(?![A-Za-z0-9])", re.I)
_DV_RE = re.compile(r"(?<![A-Za-z0-9])(Dolby[ ._-]?Vision|DV)(?![A-Za-z0-9])", re.I)
_HDR_RE = re.compile(r"(?<![A-Za-z0-9])(HDR10\+?|HDR)(?![A-Za-z0-9])", re.I)
_SOURCE_RE = re.compile(r"(?<![A-Za-z0-9])(REMUX|BluRay|BD|BDRip|WEB-DL|WEBRip|HDTV)(?![A-Za-z0-9])", re.I)
_CODEC_RE = re.compile(r"(?<![A-Za-z0-9])(HEVC|x265|H\.?265|AVC|x264|H\.?264)(?![A-Za-z0-9])", re.I)


@dataclass
class PanProbeResult:
    """网盘链接透视与校验结果。"""
    supported: bool = False
    verified: bool = False
    pan_type: str = "unknown"
    share_title: str | None = None
    total_files: int = 0
    total_size: int = 0
    total_size_human: str = "0 B"
    files: list[dict[str, Any]] = field(default_factory=list)
    real_files: list[str] = field(default_factory=list)
    actual_quality: str | None = None
    claimed_quality: str | None = None
    mismatch: bool = False
    mismatch_reason: str | None = None
    confidence: str = "unverified"  # "high" | "mismatch" | "unverified"
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "verified": self.verified,
            "pan_type": self.pan_type,
            "share_title": self.share_title,
            "total_files": self.total_files,
            "total_size": self.total_size,
            "total_size_human": self.total_size_human,
            "real_files": self.real_files,
            "actual_quality": self.actual_quality,
            "claimed_quality": self.claimed_quality,
            "mismatch": self.mismatch,
            "mismatch_reason": self.mismatch_reason,
            "confidence": self.confidence,
            "warning": self.warning,
        }


def format_size(num_bytes: int) -> str:
    """格式化字节大小为可读字符串。"""
    if num_bytes <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num_bytes)
    idx = 0
    while size >= 1024.0 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f"{size:.1f} {units[idx]}" if idx > 0 else f"{int(size)} B"


def parse_pan_url(url: str) -> dict[str, str]:
    """解析网盘 URL，识别网盘类型与提取码。"""
    if not url:
        return {"pan_type": "unknown"}
    u = url.strip()
    low = u.lower()

    # 提取提取码（URL query 中的 pwd / passcode / code，或 hash 后的参数）
    passcode = ""
    parsed = urllib.parse.urlparse(u)
    qs = urllib.parse.parse_qs(parsed.query)
    for k in ("pwd", "passcode", "code"):
        if k in qs and qs[k]:
            passcode = qs[k][0]
            break

    if "pan.quark.cn" in low or "drive.quark.cn" in low:
        m = re.search(r"/s/([A-Za-z0-9_-]+)", u)
        pwd_id = m.group(1) if m else ""
        return {"pan_type": "quark", "pwd_id": pwd_id, "passcode": passcode}
    if "pan.baidu.com" in low:
        return {"pan_type": "baidu", "passcode": passcode}
    if "pan.xunlei.com" in low:
        return {"pan_type": "xunlei", "passcode": passcode}
    if "drive.uc.cn" in low:
        return {"pan_type": "uc", "passcode": passcode}
    if "aliyundrive.com" in low or "alipan.com" in low:
        return {"pan_type": "aliyun", "passcode": passcode}
    if "115.com" in low:
        return {"pan_type": "115", "passcode": passcode}
    if "cloud.189.cn" in low:
        return {"pan_type": "tianyi", "passcode": passcode}
    return {"pan_type": "unknown", "passcode": passcode}


def _extract_quality_tags(filename: str) -> dict[str, str | None]:
    """从真实文件名抽取画质维度。"""
    res_m = _RES_RE.search(filename)
    resolution = None
    if res_m:
        val = res_m.group(1).upper()
        if val in ("4K", "2160P", "UHD"):
            resolution = "4K"
        elif val in ("1080P", "1080I", "FHD"):
            resolution = "1080p"
        elif val in ("720P", "HD"):
            resolution = "720p"

    dv = bool(_DV_RE.search(filename))
    hdr = bool(_HDR_RE.search(filename))
    src_m = _SOURCE_RE.search(filename)
    source = src_m.group(1).upper() if src_m else None
    codec_m = _CODEC_RE.search(filename)
    codec = codec_m.group(1).upper() if codec_m else None

    return {
        "resolution": resolution,
        "dv": "DV" if dv else None,
        "hdr": "HDR" if hdr else None,
        "source": source,
        "codec": codec,
    }


def _fetch_quark_files(
    pwd_id: str,
    passcode: str = "",
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> tuple[bool, str | None, list[dict[str, Any]], str | None]:
    """通过夸克免登录 REST 接口抓取分享页真实文件列表。

    返回 (ok, share_title, files, error_message)。
    """
    if not pwd_id:
        return False, None, [], "缺少 pwd_id"

    base = "https://drive-pc.quark.cn/1/clouddrive/share/sharepage"
    headers = {
        "User-Agent": _USER_AGENT,
        "Origin": "https://pan.quark.cn",
        "Referer": f"https://pan.quark.cn/s/{pwd_id}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }
    t_ms = int(time.time() * 1000)

    # 1. 换取 stoken
    token_url = f"{base}/token?pr=ucpro&fr=pc&__t={t_ms}"
    payload = json.dumps({"pwd_id": pwd_id, "passcode": passcode}).encode("utf-8")
    req = urllib.request.Request(token_url, data=payload, headers=headers)
    half_to = max(1.0, timeout / 2.0)
    try:
        with urllib.request.urlopen(req, timeout=half_to) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        return False, None, [], f"token 接口请求异常: {type(exc).__name__}"

    if data.get("status") != 200:
        msg = data.get("message") or f"status {data.get('status')}"
        return False, None, [], f"夸克分享失效或密码错误 ({msg})"

    stoken = (data.get("data") or {}).get("stoken")
    share_title = (data.get("data") or {}).get("title")
    if not stoken:
        return False, share_title, [], "获取 stoken 为空"

    # 2. 获取根目录文件详情
    def _query_detail(pdir_fid: str) -> list[dict[str, Any]]:
        qs = urllib.parse.urlencode({
            "pr": "ucpro",
            "fr": "pc",
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": pdir_fid,
            "force": 0,
            "_page": 1,
            "_size": 50,
            "_fetch_banner": 0,
            "_fetch_share": 0,
            "_fetch_total": 1,
            "_sort": "file_type:asc,updated_at:desc",
            "__t": int(time.time() * 1000),
        })
        d_req = urllib.request.Request(f"{base}/detail?{qs}", headers=headers)
        with urllib.request.urlopen(d_req, timeout=half_to) as d_resp:
            d_data = json.loads(d_resp.read().decode("utf-8", "replace"))
        return (d_data.get("data") or {}).get("list") or []

    try:
        entries = _query_detail("0")
    except Exception as exc:  # noqa: BLE001
        return False, share_title, [], f"detail 接口请求异常: {type(exc).__name__}"

    # 3. 单层目录穿透：如果根目录无文件但有 1 个文件夹，下探 1 层
    files: list[dict[str, Any]] = []
    folders: list[dict[str, Any]] = []
    for item in entries:
        is_dir = bool(item.get("dir") or item.get("file_type") == 0)
        if is_dir:
            folders.append(item)
        else:
            files.append(item)

    if not files and len(folders) == 1:
        # 下探单个子目录
        sub_fid = str(folders[0].get("fid") or "")
        if sub_fid:
            try:
                sub_entries = _query_detail(sub_fid)
                for item in sub_entries:
                    if not (item.get("dir") or item.get("file_type") == 0):
                        files.append(item)
            except Exception:  # noqa: BLE001
                pass

    return True, share_title, files, None


def probe_pan_url(
    url: str,
    claimed_title: str = "",
    timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> PanProbeResult:
    """对网盘直链进行轻量级穿透透视与真实画质校验。"""
    parsed = parse_pan_url(url)
    ptype = parsed.get("pan_type", "unknown")

    # 提取宣称画质（从标题中匹配）
    claimed_q_tags: list[str] = []
    for reg, label in (
        (re.compile(r"4K|2160p|UHD", re.I), "4K"),
        (re.compile(r"Dolby[ ._-]?Vision|DV|杜比视界", re.I), "DV"),
        (re.compile(r"HDR10\+?|HDR", re.I), "HDR"),
        (re.compile(r"REMUX", re.I), "REMUX"),
        (re.compile(r"1080p|1080i|蓝光|BluRay", re.I), "1080p"),
        (re.compile(r"720p", re.I), "720p"),
    ):
        if reg.search(claimed_title):
            claimed_q_tags.append(label)
    claimed_quality = " ".join(dict.fromkeys(claimed_q_tags)) if claimed_q_tags else None

    # 非夸克网盘（暂无免登录接口）：优雅降级
    if ptype != "quark":
        host_names = {
            "baidu": "百度网盘", "xunlei": "迅雷网盘", "uc": "UC网盘",
            "aliyun": "阿里云盘", "115": "115网盘", "tianyi": "天翼云盘",
        }
        hname = host_names.get(ptype, "该网盘")
        return PanProbeResult(
            supported=False,
            verified=False,
            pan_type=ptype,
            claimed_quality=claimed_quality,
            confidence="unverified",
            warning=f"{hname}暂不支持免登录文件透视，当前规格仅为发布者宣称，存在虚标风险",
        )

    # 夸克网盘：执行免登录探针
    pwd_id = parsed.get("pwd_id", "")
    passcode = parsed.get("passcode", "")
    ok, share_title, raw_files, err_msg = _fetch_quark_files(pwd_id, passcode, timeout=timeout)
    if not ok:
        return PanProbeResult(
            supported=True,
            verified=False,
            pan_type="quark",
            share_title=share_title,
            claimed_quality=claimed_quality,
            confidence="unverified",
            warning=f"夸克分享链接透视失败（{err_msg}），当前规格仅为发布者宣称",
        )

    # 过滤出真实视频文件并分析
    video_files: list[dict[str, Any]] = []
    total_size = 0
    for f in raw_files:
        name = str(f.get("file_name") or "")
        size = int(f.get("size") or 0)
        total_size += size
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        is_video = ext in VIDEO_EXTENSIONS or f.get("format_type") == "video"
        if is_video:
            meta = _extract_quality_tags(name)
            video_files.append({
                "name": name,
                "size": size,
                "size_human": format_size(size),
                **meta,
            })

    total_files = len(raw_files)
    total_size_h = format_size(total_size)
    real_sample_names = [f"{v['name']} ({v['size_human']})" for v in video_files[:3]]

    if not video_files:
        # 分享内没有直接识别为视频的文件（可能是纯压缩包、文档或空目录）
        return PanProbeResult(
            supported=True,
            verified=False,
            pan_type="quark",
            share_title=share_title,
            total_files=total_files,
            total_size=total_size,
            total_size_human=total_size_h,
            files=video_files,
            real_files=[f.get("file_name", "") for f in raw_files[:3]],
            claimed_quality=claimed_quality,
            confidence="unverified",
            warning=f"网盘内未发现可直接识别的视频文件（共 {total_files} 个文件，总大小 {total_size_h}）",
        )

    # 统计真实最高规格
    resolutions = {v["resolution"] for v in video_files if v.get("resolution")}
    has_4k = "4K" in resolutions
    has_1080p = "1080p" in resolutions
    has_720p = "720p" in resolutions
    has_dv = any(v.get("dv") for v in video_files)
    has_hdr = any(v.get("hdr") for v in video_files)

    act_res = "4K" if has_4k else ("1080p" if has_1080p else ("720p" if has_720p else None))
    actual_parts: list[str] = []
    if act_res:
        actual_parts.append(act_res)
    if has_dv:
        actual_parts.append("DV")
    elif has_hdr:
        actual_parts.append("HDR")
    actual_quality = " ".join(actual_parts) if actual_parts else None

    # 计算平均单集视频大小
    avg_v_size = total_size / len(video_files)
    avg_v_size_h = format_size(int(avg_v_size))

    # ---------------- 核心：虚标对比研判 ---------------- #
    mismatch = False
    mismatch_reason = None
    claimed_has_4k = bool(re.search(r"(?<![A-Za-z0-9])(4K|2160p|UHD)(?![A-Za-z0-9])", claimed_title, re.I))
    claimed_has_dv = bool(re.search(r"Dolby[ ._-]?Vision|(?<![A-Za-z0-9])DV(?![A-Za-z0-9])|杜比视界", claimed_title, re.I))

    if claimed_has_4k:
        if act_res and act_res != "4K":
            mismatch = True
            mismatch_reason = (
                f"标题宣称 4K，但网盘实际文件为 {actual_quality or act_res} "
                f"（单集约 {avg_v_size_h}）"
            )
        elif len(video_files) >= 2 and avg_v_size < 1_200_000_000:  # 剧集平均 < 1.2GB
            mismatch = True
            mismatch_reason = (
                f"标题宣称 4K，但单集体积仅 {avg_v_size_h}，"
                "严重低于 4K 正常码率下限，判定为转压低清假标"
            )
        elif len(video_files) == 1 and total_size < 3_000_000_000:  # 电影总大小 < 3.0GB
            mismatch = True
            mismatch_reason = (
                f"标题宣称 4K，但电影体积仅 {total_size_h}，"
                "严重低于 4K 正常码率下限，判定为假标"
            )

    if not mismatch and claimed_has_dv and not has_dv:
        mismatch = True
        mismatch_reason = "标题宣称支持杜比视界(DV)，但网盘实际文件未见 DV 标记"

    confidence = "mismatch" if mismatch else "high"
    return PanProbeResult(
        supported=True,
        verified=True,
        pan_type="quark",
        share_title=share_title,
        total_files=total_files,
        total_size=total_size,
        total_size_human=total_size_h,
        files=video_files,
        real_files=real_sample_names,
        actual_quality=actual_quality or act_res or "未知画质",
        claimed_quality=claimed_quality,
        mismatch=mismatch,
        mismatch_reason=mismatch_reason,
        confidence=confidence,
        warning=mismatch_reason if mismatch else None,
    )
