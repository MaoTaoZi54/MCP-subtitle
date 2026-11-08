"""媒体目录整理（M9：organize_media / find_media）。

把下载目录整理成标准媒体库结构（开发文档 3.0 §16-§24）：
- 电影单文件夹展开：Movie/Movie.1080p/Movie.mkv(+srt+nfo) → Movie/Movie.mkv
- 剧集归季一律保留剧名：<show>/Season XX/Episode.mkv——单集文件夹（库根层的
  slow.horses.s06e01.../ 等）按解析剧名聚合，复用同名已存在目录；多视频剧集
  目录的 Season 建在该目录自身内部
- 字幕强制跟随，nfo/封面等按配置跟随；空目录清理与相同文件去重属 L3 删除
- 花絮目录（Featurettes/Extras 等 special_dir_names）原地保留，不阻断整季归季
- 计划阶段做写权限预检（os.access；fnOS 的授权在应用访问权限里，os.access 与真实写一致，getfacl 不可信），不可写项降级 skip

三级分级（§28）：L1 计划（dry_run=true）只读恒可用；L2 执行移动需 enable_organize；
L3 删除需 enable_delete，默认只在计划/结果中报告 level3_pending。

安全原则（§33）：宁可少整理也不要整理错；默认不删除用户数据；移动前确认目标；
冲突即停该文件；无法高置信度识别时返回计划说明，绝不猜测。所有路径源+目标都过
PathGuard 校验（含 Season 目录创建），单文件失败不中断整个会话。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shlex
import shutil
import stat as stat_module
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import media as media_tools
from . import region as region_tools
from . import resolver
from .config import Limits, MediaConfig, OrganizerConfig, TmdbConfig
from .fs_utils import ErrorSink, ensure_dir, iter_tree, mark_incomplete_from_sink
from .media import ProbeError, find_external_subtitles, probe_video
from .platform_hints import permission_hint_text
from .progress import check_canceled, mark_partial, report
from .release import parse_release
from .resolver import ResolverError
from .security import AccessDenied, InvalidArguments, PathGuard, expand_roots
from .subhd import parse_video_filename

logger = logging.getLogger("nas_mcp.organizer")

# ---------------------------------------------------------------------------
# V2 媒体实体模型（开发文档 §3/§4/§5）
#
# 核心原则：先识别媒体（MediaFile → MediaEntity），再决定目录
# （canonical_root），最后决定动作（move/identical/conflict/skip）。
# MediaFile 负责描述"这个文件是什么"，不负责决定放在哪里；
# MediaEntity 负责描述"这是哪部剧/电影"，其 canonical_root 是
# 由 title+year 确定性计算出来的，不再靠猜目录名。
# ---------------------------------------------------------------------------

_MOVIE = "movie"
_TV = "tv"

# 目录角色（§10 classify_directory）
DIR_ROLE_SHOW_ROOT = "show_root"
DIR_ROLE_SEASON = "season"
DIR_ROLE_EPISODE = "episode"
DIR_ROLE_MOVIE_ROOT = "movie_root"
DIR_ROLE_UNKNOWN = "unknown"


@dataclass
class MediaFile:
    """一个扫描到的视频文件的解析结果（§4 MediaFile）。

    只描述"这个文件是什么"，不决定最终放在哪里。
    """
    source_path: str
    filename: str
    media_type: str
    title: str
    year: int | None
    season: int | None
    episode: int | None
    episode_end: int | None = None
    parse_confidence: float = 0.0


@dataclass
class MediaEntity:
    """一部影视作品（§3.1 MediaEntity）。

    canonical_root 由 title+year 确定性计算（如 "Lioness (2023)"），
    是后续目录解析的唯一依据——_resolve_show_dir 不再猜剧名。
    """
    media_type: str
    title: str
    normalized_title: str
    year: int | None
    canonical_root: str
    confidence: float = 0.0

# 1x01 形式与中文"第X季第X集"：SxxEyy 之外的补充识别（仅 organizer 内使用，
# 负向断言排除 1920x1080 这类分辨率写法）
_ALT_EP_RE = re.compile(r"(?<![\dxX])(\d{1,2})x(\d{1,3})(?!\d)")
_CN_EP_RE = re.compile(r"第\s*(\d{1,2})\s*季.*第\s*(\d{1,3})\s*[集话話]")
# 中文电视剧纯数字集号：剧名 + 1~3 位数字结尾（如 雍正王朝01 → E01）。
# 前一个字符不得是字母/数字（避免 Movie2024 误判；4 位年份/分辨率天然不匹配 \d{1,3}）。
_NUM_EP_RE = re.compile(r"(?<![\da-zA-Z])(\d{1,3})$")
# 已经是"Season 01 / S01 / 第1季"形态的目录视为已整理，跳过
_SEASON_DIR_RE = re.compile(r"^(?:season[ ._-]?\d{1,2}|s\d{1,2}|第\s*\d+\s*季)$", re.IGNORECASE)
# 分类目录（电视剧/电影/动漫/国产剧……）下探层数：请求目录之下最多再穿 2 层纯分类层。
# 典型结构 /move/电视剧/<剧名>/Season XX/文件 = 1 层；/movie/华语/<片名> = 2 层。
_MAX_CATEGORY_DEPTH = 2
# 明确的分类层目录名关键词（Jellyfin/Emby/Plex 的媒体库分类、Sonarr/Radarr root folder
# 都按"库根→剧目目录"两级组织）。命中即允许穿透，即使该目录直接散落着视频文件——
# 现场 /move/电视剧 根就散落着 Lioness S03E04，旧规则因"含直接视频"拒绝下探（v0.11.7 BUG-5）。
_CATEGORY_KEYWORD_CN_RE = re.compile(
    r"电视剧|剧集|连续剧|电视剧集|综艺|动漫|动画|动画片|纪录片|电影|影片|国产剧|日剧|韩剧|美剧|英剧|港剧|泰剧|未知语言"
)
# 英文分类名必须"整名即分类词"（允许 4K/1080p/HD 等质量尾巴），避免
# "A Series of Unfortunate Events (2017)" 这类含 series 的剧名被误判为分类层。
_CATEGORY_EN_NAMES = frozenset({
    "movie", "movies", "film", "films", "tv", "show", "shows",
    "tv show", "tv shows", "series", "tv series",
    "anime", "cartoon", "cartoons", "animation",
    "documentary", "documentaries", "variety", "variety show", "variety shows",
})
_CATEGORY_QUALITY_RE = re.compile(r"\b(?:4k|8k|2160p|1080p|720p|uhd|fhd|hd|remux)\b", re.IGNORECASE)
# 剧名目录名里残留的季标记文本（Season 10 / S10 / 第10季），用于计算规范剧集根时剥离，
# 避免 "Supernatural (2005) Season 10 S10" 被改名成 "Supernatural Season 10 (2005)"（套娃）。
_SEASON_WORD_RE = re.compile(r"(?:season\s*\d{1,2}|\bs\d{1,2}\b|第\s*\d+\s*季)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# 语言分层：国语 / 外语 / 未知语言
# ---------------------------------------------------------------------------
# 按媒体 kind 和 region 决定子目录名
_LANG_SUBDIR: dict[str, dict[str, str]] = {
    "movie": {
        "domestic":     "国语电影",
        "non_domestic": "外语电影",
        "unknown":      "未知语言",
    },
    "tv": {
        "domestic":     "国语剧集",
        "non_domestic": "外语剧集",
        "unknown":      "未知语言",
    },
}

_ALL_LANG_SUBDIRS: frozenset[str] = frozenset({
    "国语电影", "外语电影", "国语剧集", "外语剧集", "未知语言",
})


def _classify_region_safe(
    video: Path,
    media_cfg: MediaConfig,
    tmdb_cfg: TmdbConfig | None,
    cache: dict[Path, str],
) -> str:
    """对单个视频调用 nas_classify_region 综合判定核心，返回 domestic / non_domestic / unknown。

    结合音轨语言 + 文件名配音标记（国语/普通话等）+ TMDB（若启用）。
    失败或证据不足时安全返回 unknown，不阻断整理流程。
    """
    if video in cache:
        return cache[video]
    tcfg = tmdb_cfg if tmdb_cfg is not None else TmdbConfig(enabled=False)
    try:
        res = region_tools._classify_one(video, media_cfg, tcfg)
        region = res.get("region", "unknown")
    except Exception as exc:
        logger.warning("语言分类判定异常 %s: %s", video.name, exc)
        region = "unknown"
    cache[video] = region
    return region


def _apply_language_split(
    plan: list[dict],
    library_roots: list[Path],
    media_cfg: MediaConfig,
    video_exts: frozenset[str],
    tmdb_cfg: TmdbConfig | None = None,
) -> None:
    """后处理：将 move 与 rename_dir 动作的目标路径按语言分层重写（原地修改 plan）。

    目标路径结构变化：
      电影：<library_root>/<片名>/...  →  <library_root>/<国语电影|外语电影|未知语言>/<片名>/...
      剧集：<library_root>/<剧名>/Season XX/ep.mkv  →
            <library_root>/<国语剧集|外语剧集|未知语言>/<剧名>/Season XX/ep.mkv
      剧集目录改名：<library_root>/<旧目录> → <library_root>/<国语剧集|外语剧集|未知语言>/<规范剧名>

    幂等性：如果目标路径或匹配到的库根本身已包含语言分层子目录，则不重复插入。
    """
    lr_set = {lr for lr in library_roots}
    region_cache: dict[Path, str] = {}
    series_region_cache: dict[tuple[Path, str], str] = {}

    for item in plan:
        act = item.get("action")
        if act not in ("move", "rename_dir"):
            continue
        kind = item.get("kind", _TV)
        tgt = Path(item["target"])
        src = Path(item["source"])

        # 找到该 target 属于哪个 library_root（最长匹配祖先）
        matched_lr: Path | None = None
        for lr in sorted(lr_set, key=lambda p: len(p.parts), reverse=True):
            try:
                tgt.relative_to(lr)
                matched_lr = lr
                break
            except ValueError:
                continue
        if matched_lr is None:
            continue

        # 库根本身已经是语言子目录，或者相对路径中已包含语言子目录 → 幂等跳过
        if matched_lr.name in _ALL_LANG_SUBDIRS:
            continue
        try:
            rel = tgt.relative_to(matched_lr)
        except ValueError:
            continue
        if any(part in _ALL_LANG_SUBDIRS for part in rel.parts):
            continue

        # 确定需要探测的代表性视频文件
        sample_video: Path | None = None
        if act == "move":
            sample_video = src if _is_video_file(src, video_exts) else None
        elif act == "rename_dir" and src.is_dir():
            # 取该剧集目录下任意一个视频文件作为代表探测音轨与产地
            for root_dir, _, files in os.walk(src):
                for f in sorted(files):
                    fpath = Path(root_dir) / f
                    if _is_video_file(fpath, video_exts):
                        sample_video = fpath
                        break
                if sample_video:
                    break

        # 剧集按 (matched_lr, 剧名目录) 统一聚合语言判定，防止同剧各集因音轨顺序差异被分入不同目录
        group_key = (matched_lr, rel.parts[0]) if kind == _TV and len(rel.parts) > 1 else None
        if group_key and group_key in series_region_cache:
            region = series_region_cache[group_key]
        else:
            region = (
                _classify_region_safe(sample_video, media_cfg, tmdb_cfg, region_cache)
                if sample_video is not None
                else "unknown"
            )
            if group_key and region != "unknown":
                series_region_cache[group_key] = region
        subdir_name = _LANG_SUBDIR.get(kind, _LANG_SUBDIR["tv"]).get(region, "未知语言")

        # 重写目标路径：在 library_root 与 rel 之间插入语言子目录
        new_tgt = matched_lr / subdir_name / rel
        item["target"] = str(new_tgt)
        item.setdefault("language_split", {})["region"] = region
        item["language_split"]["subdir"] = subdir_name

        # move 项的伴生文件（related: 字幕、nfo、封面等）同步重写目标
        if act == "move":
            for r in item.get("related", []):
                r_tgt = Path(r["target"])
                try:
                    r_rel = r_tgt.relative_to(matched_lr)
                except ValueError:
                    continue
                if any(part in _ALL_LANG_SUBDIRS for part in r_rel.parts):
                    continue
                r["target"] = str(matched_lr / subdir_name / r_rel)


def _norm_name(text: str) -> str:
    """目录名/文件 stem 归一化：小写、分隔符转空格、压缩空白。"""
    t = re.sub(r"[._\-\[\]()]+", " ", (text or "").lower())
    return re.sub(r"\s{2,}", " ", t).strip()


def extract_episode_info(*names: str) -> tuple[int, int] | None:
    """从多个候选名称中提取唯一的 (季, 集)。

    依次尝试每个名称：SxxEyy（parse_video_filename）→ 1x01 → 中文"第X季第X集"。
    所有非空结果必须一致；出现冲突或无结果时返回 None（无法确定 → 不整理）。
    """
    found: tuple[int, int] | None = None
    for name in names:
        if not name:
            continue
        pair: tuple[int, int] | None = None
        q = parse_video_filename(name)
        if q.season is not None and q.episode is not None:
            pair = (q.season, q.episode)
        if pair is None:
            m = _ALT_EP_RE.search(name)
            if m:
                pair = (int(m.group(1)), int(m.group(2)))
        if pair is None:
            m = _CN_EP_RE.search(name)
            if m:
                pair = (int(m.group(1)), int(m.group(2)))
        if pair is None:
            # 中文纯数字集号：剧名01 → (1, 1)；仅当无其它季集标记时才采用，
            # 且要求数字前为非字母数字（排除 Movie2024/1080p 等）
            stem = Path(name).stem
            m = _NUM_EP_RE.search(stem)
            if m:
                ep = int(m.group(1))
                if 1 <= ep <= 999:
                    pair = (1, ep)
        if pair is None:
            continue
        if found is None:
            found = pair
        elif found != pair:
            return None
    return found


def _best_episode(path: Path) -> tuple[int, int] | None:
    """单个视频的季集推断：优先文件名，其次父目录名（找到即用，不要求一致）。"""
    pair = extract_episode_info(path.name)
    if pair is not None:
        return pair
    return extract_episode_info(path.parent.name)


def is_special_dir(name: str, cfg: OrganizerConfig) -> bool:
    """目录名（不区分大小写）命中特殊目录清单 → 子树禁整理。"""
    return name.strip().lower() in cfg.special_dir_names


def _is_hidden_name(name: str) -> bool:
    """隐藏/临时目录（.deletedByTMM、.DS_Store、~$tmp 等）不参与整理与库根发现。"""
    n = (name or "").strip()
    return n.startswith(".") or n.startswith("~$")


def _is_category_name(name: str) -> bool:
    """目录名是否明确是分类层（电视剧/电影/TV Shows/Movies/Anime 等）。"""
    if _CATEGORY_KEYWORD_CN_RE.search(name):
        return True
    stripped = _CATEGORY_QUALITY_RE.sub(" ", (name or "").lower())
    token = re.sub(r"[^a-z0-9]+", " ", stripped).strip()
    return token in _CATEGORY_EN_NAMES


def _show_title_from_folder(name: str) -> tuple[str, int | None]:
    """从单集/季命名的剧集根目录提取纯剧名与年份。

    parse_video_filename 会切掉 SxxEyy 标记，但目录名里以 *文本* 形式残留的
    "Season 10" 仍会污染片名（Supernatural (2005) Season 10 S10 → 规范根必须
    是 "Supernatural (2005)"，不是 "Supernatural Season 10 (2005)"）。
    """
    q = parse_video_filename(name)
    title = _SEASON_WORD_RE.sub(" ", q.title or "")
    title = re.sub(r"\s{2,}", " ", title).strip()
    return title, q.year


def _in_special_parts(rel_parts: tuple[str, ...], cfg: OrganizerConfig) -> bool:
    return any(is_special_dir(p, cfg) for p in rel_parts)


def _related_files(
    video: Path, subtitle_exts: frozenset[str], follow_exts: tuple[str, ...]
) -> list[Path]:
    """与视频同目录、应随视频一起移动的关联文件。

    字幕（主名前缀匹配，find_external_subtitles 逻辑）必须跟随；
    nfo/封面等 follow_extensions 仅在 stem 前缀无歧义时跟随。
    """
    files: list[Path] = [Path(s["path"]) for s in find_external_subtitles(video, subtitle_exts)]
    stem = video.stem.lower()
    taken = {p.name for p in files}
    try:
        siblings = sorted(os.scandir(video.parent), key=lambda e: e.name)
    except OSError:
        return files
    for entry in siblings:
        name = entry.name
        if name in taken or not entry.is_file(follow_symlinks=False):
            continue
        ext = Path(name).suffix.lstrip(".").lower()
        if ext not in follow_exts:
            continue
        if Path(name).stem.lower().startswith(stem):
            files.append(Path(entry.path))
    return files


def _item(
    action: str,
    *,
    source: str,
    target: str | None = None,
    related: list[tuple[Path, Path]] | None = None,
    kind: str = _MOVIE,
    level: int = 2,
    reason: str = "",
    confidence: float = 1.0,
) -> dict:
    return {
        "action": action,            # move | skip | identical | duplicate | conflict | blocked | cleanup_dir
        "kind": kind,                # movie | tv
        "level": level,              # 2=移动（enable_organize），3=删除类（enable_delete）
        "source": source,
        "target": target,
        "related": [
            {"source": str(s), "target": str(t)} for s, t in (related or [])
        ],
        "reason": reason,
        "confidence": confidence,
        "executed": None,
    }


def _show_title_for(child_name: str | None, video_name: str) -> str:
    """单集文件夹/根散集的剧名判定（BUG-2：归季必须保留剧名）。

    目录名无季集标记时以目录名为准（下载器常用 release 名建目录，内部文件可能
    被改名成只有季集）；目录名本身带季集（如 slow.horses.s06e01.1080p/）则用
    视频文件名解析出的片名。都解析不出返回空串（调用方应 skip 不猜）。
    """
    if child_name is not None and extract_episode_info(child_name) is None:
        t = (parse_video_filename(child_name).title or "").strip()
        if t:
            return t
    return (parse_video_filename(video_name).title or "").strip()


def _show_dir_fingerprint(name: str) -> tuple[str, int | None]:
    """已有剧集目录的匹配指纹：(归一化剧名, 年份)。

    复用 parse_video_filename：BUG-7 后其 title 取季集标记之前的干净剧名，
    故 'Lioness (2023) - S03E08 - The Unravelling (...)' → ('lioness', 2023)，
    'Lioness (2023)' → ('lioness', 2023)，'Slow Horses' → ('slow horses', None)。
    """
    q = parse_video_filename(name)
    return _norm_name(q.title), q.year


def _is_episode_dir_name(name: str) -> bool:
    """目录名含季集标记 → 单集/历史错误目录，不能作为剧集根目录（BUG-4）。"""
    return extract_episode_info(name) is not None


def _is_misnamed_show_root(name: str) -> bool:
    """内含 Season 子目录的目录，其名字本身带单集或季信息 → 错误的剧集根命名，
    应 rename 为规范根。

    覆盖三类：单集名（Lioness … - S03E08 - …）、整季包名
    （Supernatural (2005) Season 10 S10 …，只有季没有集，extract_episode_info
    返回 None，v0.11.7 BUG-3）、中文第X季。
    """
    if _is_episode_dir_name(name):
        return True
    if _SEASON_WORD_RE.search(name or ""):
        return True
    return parse_video_filename(name).season is not None


def _canonical_show_name(title: str, year: int | None) -> str:
    """规范剧集根目录名：Title 或 Title (Year)。"""
    t = (title or "").strip()
    return f"{t} ({year})" if t and year else t


def classify_directory(name: str) -> str:
    """目录角色分类（§10 classify_directory）。

    返回 DIR_ROLE_* 之一：
      - season：Season 01 / S01 / 第1季 等已整理季目录
      - episode：含季集标记（SxxEyy / 1x01 / 中文季集 / 纯数字集号）的单集目录
      - show_root：含年份且无季集标记的剧集根候选（如 "Lioness (2023)"）
      - movie_root：无季集无年份的单目录（弱信号，仅当内容是电影时成立）
      - unknown：无法判定
    注意：这只是目录名层面的角色提示，最终是否为 show root 还要结合
    MediaEntity 的 canonical_root 匹配结果。
    """
    n = (name or "").strip()
    if not n:
        return DIR_ROLE_UNKNOWN
    if _SEASON_DIR_RE.match(n):
        return DIR_ROLE_SEASON
    if _is_episode_dir_name(n):
        return DIR_ROLE_EPISODE
    # 含 (Year) 且无季集 → 剧集根候选
    q = parse_video_filename(n)
    if q.year is not None and q.title:
        return DIR_ROLE_SHOW_ROOT
    return DIR_ROLE_UNKNOWN


def resolve_media_entity(
    title: str,
    year: int | None,
    season: int | None = None,
    episode: int | None = None,
) -> MediaEntity:
    """根据解析出的媒体信息确定 MediaEntity（§5 Media Identity）。

    第一阶段不依赖外部 ID，仅用 title + year 建立实体；
    canonical_root 由 title+year 确定性计算，不再靠猜目录名。
    season/episode 用于判定 media_type（有季集 → tv，否则 → movie）。
    """
    t = (title or "").strip()
    media_type = _TV if season is not None else _MOVIE
    return MediaEntity(
        media_type=media_type,
        title=t,
        normalized_title=_norm_name(t),
        year=year,
        canonical_root=_canonical_show_name(t, year),
        confidence=0.9 if t else 0.0,
    )


def _resolve_show_dir_for_entity(
    entity: MediaEntity,
    root: Path,
    child_dirs: set[Path],
) -> Path | None:
    """V2 核心：给已确定的 MediaEntity，找到或计算其 canonical root 目录。

    不再猜剧名/年份——entity.canonical_root 已是确定性结果。这里只做：
      1. 在已有目录中找属于同一实体的目录（剧名指纹 + 年份兼容）；
      2. 优先精确匹配 canonical_root 名的合格 show root；
      3. 排除 episode 目录（单集目录绝不当 show root）；
      4. 多个合格 show root 无法区分 → None（歧义安全退出）；
      5. 只有 episode 目录候选 → 返回 root/canonical_root（不复用单集目录）；
      6. 无候选 → 返回 root/canonical_root。
    返回 None = 歧义 → 调用方 skip，绝不新建误导目录。
    """
    wanted = entity.normalized_title
    year = entity.year

    # NEW-14：root 自身已是该剧的规范 show root（organize_media path 直接指向
    # 剧根，library_root == 剧根）。此时 target 公式 root+canonical_name+Season XX
    # 应退化为 root/Season XX，否则产生 root/canonical_name/Season XX 双层剧名。
    # 年份兼容规则与下方步骤2一致（任一方无年份即兼容，有年份须相等）。
    root_title, root_year = _show_dir_fingerprint(root.name)
    if (
        root_title == wanted
        and not _is_episode_dir_name(root.name)
        and (year is None or root_year is None or root_year == year)
    ):
        return root

    # 1) 归一化剧名精确匹配（裸剧名，如已有目录恰好叫 "Lioness"），且不是 episode 目录
    for d in sorted(child_dirs):
        if _norm_name(d.name) == wanted and not _is_episode_dir_name(d.name):
            return d

    # 2) 剧名指纹匹配 + 年份证据排除，区分 show root 与 episode 目录
    show_roots: list[tuple[Path, int | None]] = []
    ep_dirs: list[tuple[Path, int | None]] = []
    for d in sorted(child_dirs):
        d_title, d_year = _show_dir_fingerprint(d.name)
        if not d_title or d_title != wanted:
            continue
        if year is not None and d_year is not None and d_year != year:
            continue
        if _is_episode_dir_name(d.name):
            ep_dirs.append((d, d_year))
        else:
            show_roots.append((d, d_year))

    # 3) 合格 show root：年份一致优先，其次含 Season 子目录
    def _rank(c: tuple[Path, int | None]) -> tuple[int, int]:
        d, d_year = c
        y = 1 if (year is not None and d_year == year) else 0
        try:
            s = 1 if any(
                _SEASON_DIR_RE.match(x.name) for x in d.iterdir() if x.is_dir()
            ) else 0
        except OSError:
            s = 0
        return (y, s)

    if show_roots:
        best = max(_rank(c) for c in show_roots)
        top = [d for c in show_roots if _rank(c) == best for d in [c[0]]]
        if len(top) == 1:
            return top[0]
        return None  # 多个合格 show root 无法区分

    # 4) 只有 episode 目录候选（历史错误结构）：不复用单集目录，返回规范名路径。
    #    若 entity 自身无年份但 episode 目录提供了年份证据，借用该年份计算
    #    canonical_root（同一部剧的年份证据）。
    if ep_dirs:
        canon = entity.canonical_root
        if not canon or (entity.year is None):
            ep_year = next((y for _, y in ep_dirs if y is not None), None)
            canon = _canonical_show_name(entity.title, ep_year)
        return root / canon

    # 5) 无任何候选 → 新建规范剧名目录
    return root / (entity.canonical_root or entity.title)


def _resolve_show_dir(
    root: Path,
    title: str,
    child_dirs: set[Path],
    year: int | None = None,
) -> Path | None:
    """向后兼容包装：从 (title, year) 构造 MediaEntity 再解析 canonical root。

    新代码应直接调用 resolve_media_entity() + _resolve_show_dir_for_entity()，
    让"媒体身份"与"目录解析"在调用点显式分离。
    """
    entity = resolve_media_entity(title, year)
    return _resolve_show_dir_for_entity(entity, root, child_dirs)


def _existing_ancestor(p: Path) -> Path:
    """目标路径上最近的已存在祖先（待创建目录的实际写权限点）。"""
    cur = p
    while not cur.exists():
        cur = cur.parent
    return cur


def _is_video_file(path: Path, video_exts: frozenset[str]) -> bool:
    return path.suffix.lstrip(".").lower() in video_exts


def _write_points(item: dict) -> list[Path]:
    """计划项执行时需要写权限的目录（fnOS trim_acl 下 getfacl 不可信，但 os.access 与真实写一致）。

    move/rename_dir：源父目录（取走/改名条目）+ 目标最近存在祖先（创建目录/写入）
    + 各关联文件；identical/duplicate：仅源父目录（删除文件）；
    cleanup_dir：源目录的父目录（rmdir 删条目）。
    """
    action = item.get("action")
    source = Path(str(item.get("source") or "."))
    pts: list[Path] = []
    if action in ("move", "identical", "duplicate"):
        pts.append(source.parent)
    if action in ("identical", "duplicate"):
        # NEW-15：伴生文件随 keeper 移动/冗余删除，写点含伴生源父目录+目标祖先
        for r in item.get("related", []):
            pts.append(Path(r["source"]).parent)
            pts.append(_existing_ancestor(Path(r["target"])))
    if action in ("move", "rename_dir"):
        if item.get("target"):
            pts.append(_existing_ancestor(Path(item["target"])))
    if action == "move":
        for r in item.get("related", []):
            pts.append(Path(r["source"]).parent)
            pts.append(_existing_ancestor(Path(r["target"])))
    if action == "cleanup_dir":
        pts.append(source.parent)
    return pts


# ---------------------------------------------------------------------------
# 副本裁决（参考 Sonarr/Radarr 的 import 去重）：
# 同字节大小 + ffprobe 指纹一致的一组文件只保留一个 keeper，其余为冗余副本。
# keeper 优先选择已经位于规范结构（<剧名>/Season XX/）内的副本，其次路径更浅、
# 字典序更小——确定性裁决，绝不允许把同组两份都标成可删（双删事故）。
# ---------------------------------------------------------------------------

def _probe_fp_cached(video: Path, media_cfg: MediaConfig, cache: dict) -> tuple | None:
    if video not in cache:
        cache[video] = _probe_fingerprint(video, media_cfg)
    return cache[video]


def _in_organized_layout(p: Path) -> bool:
    """副本是否已位于 <非单集名剧名目录>/Season XX/ 规范结构内。"""
    return (
        _SEASON_DIR_RE.match(p.parent.name.strip()) is not None
        and not _is_episode_dir_name(p.parent.parent.name)
    )


def _keeper_quality_key(p: Path) -> tuple[int, int]:
    """T7：keeper 裁决的质量维度（与 library.py 查重同口径：质量分高者优先，
    平分取文件更大者）。返回可直接放进 min()/sorted() 升序键的负向元组。"""
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    return (-parse_release(p.name).quality_score, -size)


# 未命中 TMDB 做退化归并时，标题里需要剥离的纯技术词（与 library.py 同步）
_TITLE_NOISE_RE_ORG = re.compile(r"\b(?:uhd|fhd|4k|8k)\b", re.IGNORECASE)


def _work_identity_key(p: Path) -> tuple:
    """organizer 侧"同作品"归并键（NEW-11）：解析文件名得到 (剧名, 年份, 季, 集)，
    归一化后作为指纹组的细分键——不同作品即便字节+指纹一致，也不归为一组，
    绝不互删（对应 duplicate_media_detect 用 TMDB ID 做身份边界的语义）。

    退化实现（organizer 不引入 TMDB 依赖）：用名片键 (title, year, season, episode)，
    title 经 _TITLE_NOISE_RE_ORG 剥离 + _norm_name 归一化。
    """
    q = parse_video_filename(p.name)
    raw_title = _TITLE_NOISE_RE_ORG.sub(" ", q.title or p.stem)
    title = _norm_name(raw_title)
    return (title, q.year, q.season, q.episode)


def _fingerprint_groups(
    size_index: dict[int, list[Path]],
    media_cfg: MediaConfig,
    fp_cache: dict,
) -> list[list[Path]]:
    """同字节大小 + ffprobe 指纹一致 + 同作品身份键的视频分组。

    指纹 probe 失败的文件不参与；指纹相同但作品身份不同（Interstellar vs Inception）
    的文件拆为独立组（NEW-11），避免 _arbitrate_duplicates 跨作品误删。
    """
    groups: list[list[Path]] = []
    for group in size_index.values():
        if len(group) <= 1:
            continue
        by_fp: dict[tuple, list[Path]] = {}
        for f in sorted(group):
            fp = _probe_fp_cached(f, media_cfg, fp_cache)
            if fp is not None:
                # NEW-11：指纹键叠加作品身份键——指纹相同但作品不同不并组
                work_key = _work_identity_key(f)
                by_fp.setdefault((fp, work_key), []).append(f)
        groups.extend(sorted(same) for same in by_fp.values() if len(same) > 1)
    return groups


def _build_keeper_map(
    size_index: dict[int, list[Path]],
    media_cfg: MediaConfig,
    fp_cache: dict,
) -> dict[Path, Path]:
    """基于当前磁盘布局的唯一 keeper 裁决（供单元测试/无结构变更场景）。

    实际规划使用 _arbitrate_duplicates 做"规划后布局"裁决——rename_dir 会把
    单集名目录升级为规范剧集根，当前布局下的判定会选出错误 keeper（v0.11.7 BUG-1）。
    """
    keeper_of: dict[Path, Path] = {}
    for same in _fingerprint_groups(size_index, media_cfg, fp_cache):
        # T7：规范位置规则优先不变；位置并列时与 library 同口径——质量分高 →
        # 文件大（指纹组字节数相同，大小键用于跨尺寸的 identical 裁决），再路径序
        keeper = sorted(
            same,
            key=lambda p: (0 if _in_organized_layout(p) else 1,
                           *_keeper_quality_key(p),
                           len(p.parts), str(p).lower()),
        )[0]
        for f in same:
            if f != keeper:
                keeper_of[f] = keeper
    return keeper_of


def _duplicate_item(v: Path, keeper: Path, keeper_target: str | None = None) -> dict:
    """生成 duplicate 计划项：target 恒指向磁盘上（计划/执行阶段1时刻）真实存在的
    保留副本；keeper 自身会随 rename/move 改变位置时用 keeper_target 注明最终位置。"""
    where = (f"（该保留副本将随目录改名/移动最终位于 {keeper_target}）"
             if keeper_target else "")
    return _item(
        "duplicate", source=str(v), target=str(keeper), kind=_TV, level=3,
        reason=(
            f"与库内保留副本 {keeper.name}（{keeper}）{where}字节大小与 ffprobe 指纹完全一致；"
            f"整理时保留该副本、本文件为冗余副本（执行顺序：先删冗余，再改名/移动，"
            f"故删除时保留副本仍在上述路径）。自动删除需同时满足 enable_delete=true "
            f"且本文件所在目录可写；不满足时不会动文件，可核对后手动处理："
            f"rm -- {shlex.quote(str(v))}"
        ),
    )


def _inside_rename_subtree(p: Path, renames: list[tuple[Path, Path]]) -> bool:
    """p 是否位于本批任一 rename_dir 源子树内（随目录改名整体带走）。"""
    return any(rs == p or rs in p.parents for rs, _rt in renames)


def _arbitrate_duplicates(
    plan: list[dict],
    files_by_dir: dict[Path, list[Path]],
    fp_groups: list[list[Path]],
    video_exts: frozenset[str],
) -> list[dict]:
    """规划后、结构感知的副本裁决（参考 Sonarr import：先用改名后的最终结构判断
    哪份文件已经在正确位置，再决定冗余方）。

    输入是已完成 move/rename_dir 规划、模式过滤、磁盘三态判定后的计划：
    - 每份视频的"规划后位置"：rename 子树内 → 随目录改名到 canonical 下；
      move 项 → target；其余 → 原地。
    - keeper 优先序：规划后位于规范 <show>/Season XX/ 布局 > 无需移动（随 rename
      带走或本就原地）> 当前路径更浅 > 字典序。
      → Lioness 现场：单集名目录 Season 03 内的副本 rename 后即在规范位，是 keeper；
        库根散件的 move 转 duplicate（删散件），与 rename_dir 方向一致。
    - 只对"本批会发生结构变化"的组裁决；无人移动/改名的存量重复留给
      duplicate_media_detect 库治理，绝不在 organizer 里静默删存量。
    返回需要追加到计划的 duplicate 项（move→duplicate 的转换就地修改）。
    """
    move_by_src: dict[Path, dict] = {
        Path(i["source"]): i for i in plan if i["action"] == "move"
    }
    renames: list[tuple[Path, Path]] = [
        (Path(i["source"]), Path(i["target"]))
        for i in plan if i["action"] == "rename_dir"
    ]
    # rename 子树内随目录整体带走的视频（它们没有独立 move 项）
    carried: set[Path] = set()
    for rs, _rt in renames:
        for d, fs in files_by_dir.items():
            if d == rs or rs in d.parents:
                carried.update(f for f in fs
                               if f.suffix.lstrip(".").lower() in video_exts)

    def _post(f: Path) -> Path:
        for rs, rt in renames:
            try:
                return rt / f.relative_to(rs)
            except ValueError:
                continue
        mv = move_by_src.get(f)
        return Path(mv["target"]) if mv else f

    appended: list[dict] = []
    for same in fp_groups:
        active = [f for f in same if f in move_by_src or f in carried]
        if not active:
            continue  # 存量重复、本批不触及 → 不判定
        posts = {f: _post(f) for f in same}
        # T7（与 library.py 查重同口径）：先满足"规划后规范位置"规则（前两级
        # 布局键不变），位置并列时质量分高者优先、平分取文件更大者，仍并列才走
        # 路径深度/字典序——两工具对同一多版本场景结论一致。
        keeper = min(
            same,
            key=lambda f: (
                0 if _in_organized_layout(posts[f]) else 1,
                0 if (f in carried or posts[f] == f) else 1,
                *_keeper_quality_key(f),
                len(f.parts), str(f).lower(),
            ),
        )
        keeper_post = posts[keeper]
        for f in same:
            if f == keeper:
                continue
            if f in move_by_src:
                item = move_by_src[f]
                item["action"] = "duplicate"
                item["level"] = 3
                item["target"] = str(keeper)
                item["reason"] = _duplicate_item(
                    f, keeper,
                    str(keeper_post) if keeper_post != keeper else None,
                )["reason"]
                if keeper_post != keeper:
                    item["keeper_target"] = str(keeper_post)
                # NEW-16：related target 一律取 keeper 实际（规划后）父目录，
                # 绝不用规范名拼路径（规范名在 resolver 未命中时是幻影目录）。
                # 随 rename 子树整体带走的伴生文件剔除——rename 已携带，无需单独动作。
                keeper_dir = keeper_post.parent
                item["related"] = [
                    {"source": r["source"],
                     "target": str(keeper_dir / Path(r["source"]).name)}
                    for r in item.get("related", [])
                    if not _inside_rename_subtree(Path(r["source"]), renames)
                ]
            elif f in carried:
                # 随 rename 带走的冗余副本：执行阶段 1（改名前按原路径删除）处理；
                # 其同目录伴生文件随 rename 整体带走，保持原位即可，不列 related。
                appended.append(_duplicate_item(
                    f, keeper,
                    str(keeper_post) if keeper_post != keeper else None,
                ))
                if keeper_post != keeper:
                    appended[-1]["keeper_target"] = str(keeper_post)
            # 其余为原地存量副本：本批不删（保守，交库治理）
    return appended


# ---------------------------------------------------------------------------
# 库根（library root）发现：
# 请求目录本身是库根；其下"纯分类层"（无直接视频、子目录都不是 Season 形态、
# 至少含一个普通子目录）自动下探——电视剧/电影/动漫/国产剧 这类 Plex/Jellyfin
# 式分类目录不必逐个子目录手动跑。含直接视频或含 Season 子目录的是剧集/电影
# 目录本身，绝不当作分类层穿透。
# ---------------------------------------------------------------------------

def _show_like_dir(
    d: Path,
    files_by_dir: dict[Path, list[Path]],
    subdirs_by_dir: dict[Path, set[Path]],
    video_exts: frozenset[str],
) -> bool:
    """子目录是否呈现"一个剧目"的结构：单集名目录 / 直接含 Season 子目录 /
    直接视频带季集标记。用于把"含多个剧目的分类层"与"单个混合内容目录"区分开。"""
    if _is_misnamed_show_root(d.name):
        return True
    if any(_SEASON_DIR_RE.match(s.name.strip()) for s in subdirs_by_dir.get(d, set())):
        return True
    return any(
        _is_video_file(f, video_exts) and extract_episode_info(f.name) is not None
        for f in files_by_dir.get(d, [])
    )


def _category_like(
    d: Path,
    files_by_dir: dict[Path, list[Path]],
    subdirs_by_dir: dict[Path, set[Path]],
    video_exts: frozenset[str],
    org_cfg: OrganizerConfig,
) -> bool:
    """目录是否为可下探的分类层（库根的容器）。

    - 含 Season 形态子目录 → 它自己就是剧集根，绝不穿透；
    - 无直接视频、有正常子目录 → 纯中间层（FM/inner、空分类），穿透；
    - 有直接散落视频时（v0.11.7 BUG-5：电视剧根散着单集）仅当
      ① 名字是明确分类名（电视剧/电影/TV Shows…）或
      ② ≥2 个呈现剧目结构的子目录（多剧聚合是分类层的强信号）才穿透；
      否则按"混合内容、关系不明"保守 skip（如冲突三态夹具 cft）。
    隐藏目录（. 开头，如 .deletedByTMM）一律不算。
    """
    subs = [s for s in subdirs_by_dir.get(d, set())
            if not is_special_dir(s.name, org_cfg) and not _is_hidden_name(s.name)]
    if not subs:
        return False
    if any(_SEASON_DIR_RE.match(s.name.strip()) for s in subs):
        return False  # <show>/Season XX 是剧集根，不是分类层
    direct_videos = [f for f in files_by_dir.get(d, []) if _is_video_file(f, video_exts)]
    if not direct_videos:
        return True
    if _is_category_name(d.name):
        return True
    return sum(1 for s in subs
               if _show_like_dir(s, files_by_dir, subdirs_by_dir, video_exts)) >= 2


def _discover_library_roots(
    root: Path,
    files_by_dir: dict[Path, list[Path]],
    subdirs_by_dir: dict[Path, set[Path]],
    video_exts: frozenset[str],
    org_cfg: OrganizerConfig,
) -> tuple[list[Path], set[Path]]:
    """返回 (库根有序列表, 被下探的分类目录集合)。"""
    roots: list[Path] = [root]
    categories: set[Path] = set()
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        d, depth = queue.pop(0)
        if depth >= _MAX_CATEGORY_DEPTH:
            continue
        for s in sorted(subdirs_by_dir.get(d, set())):
            if is_special_dir(s.name, org_cfg) or _is_hidden_name(s.name):
                continue
            if not _category_like(s, files_by_dir, subdirs_by_dir, video_exts, org_cfg):
                continue
            categories.add(s)
            roots.append(s)
            queue.append((s, depth + 1))
    return roots, categories


# --------------------------------------------------------------------------- #
# T11：分类层/库根层"孤儿 Season 目录"归位
#
# 下载器按 <分类>/<Season NN>/<SxxExx>.mkv 落盘时剧名根缺失，Season 目录直接
# 躺在分类层（如 电视剧/Season 06/），旧逻辑一律 skip"已是 Season 目录形态"，
# 只能手工 mv。父级**无剧名**（目录名 token 不含文件解析出的剧名）时才进本分支：
#   a. 目录内视频 title+season 解析不完整/互相不一致 → 保守 skip；
#   b. 父级下找同名已有剧根（归一化匹配 <剧名> (年份)/<剧名>，命中多个 → skip）；
#   c. 未命中已有剧根时 tmdb.enabled 且 resolved=true 才按标准名建根，否则 skip。
# 保守分支的 skip 一律用 orphan_season_unresolved 前缀（便于调用方区分"缺陷"
# 与"保守跳过"）；父目录名就是剧名的正常剧根完全不进本分支，默认行为不变。
# 季目录搬空后由现有 cleanup 闭包自动出 level3 cleanup_dir（enable_delete 门控）。
# --------------------------------------------------------------------------- #

# 季目录名中的季号（Season 06 / S06 / 第6季），与文件解析出的季号交叉核对
_SEASON_NUM_RE = re.compile(
    r"^(?:season[ ._-]?(\d{1,2})|s(\d{1,2})|第\s*(\d{1,2})\s*季)$", re.IGNORECASE)


def _season_num_of_dir(name: str) -> int | None:
    m = _SEASON_NUM_RE.match((name or "").strip())
    if not m:
        return None
    return int(next(g for g in m.groups() if g))


def _parent_named_like_show(parent_name: str, title_norm: str) -> bool:
    """父目录名是否就是该剧的剧名（token 包含关系）。

    'Slow Horses (2022)' ⊇ 'slow horses' → 正常剧根（非孤儿）；
    '电视剧'/'move'/Downloads 等都不含 → 孤儿候选。
    """
    if not title_norm:
        return False
    p_tokens = set(_norm_name(parent_name).split())
    if not p_tokens:
        return False
    return set(title_norm.split()) <= p_tokens


def _orphan_skip(season_dir: Path, detail: str) -> dict:
    """孤儿分支保守 skip：reason 固定以 orphan_season_unresolved 起头。"""
    return _item("skip", source=str(season_dir), kind=_TV,
                 reason=f"orphan_season_unresolved：{detail}")


def _detect_orphan_season(
    season_dir: Path,
    parent: Path,
    videos: list[Path],
    sibling_pool: set[Path],
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    tmdb_cfg: TmdbConfig | None,
) -> list[dict] | None:
    """单个孤儿 Season 目录归位（T11）。

    返回 None = 非孤儿/无法定位，交回常规流程（保持旧行为）；
    返回列表 = 归位 move 计划或 orphan_season_unresolved 保守 skip。
    """
    # a) 逐文件解析 title + season（保守：不完整/不一致都不动）
    titles: set[str] = set()
    seasons: set[int] = set()
    display_title = ""
    year: int | None = None
    parse_ok = True
    for v in sorted(videos):
        pair = _best_episode(v)
        title = _show_title_for(None, v.name)
        if pair is None or not title:
            parse_ok = False
            continue
        titles.add(_norm_name(title))
        seasons.add(pair[0])
        if not display_title:
            display_title = title
        if year is None:
            year = parse_video_filename(v.name).year

    # 位置门①：父目录名命中任一解析剧名 → 正常 <剧名>/Season XX 结构，非孤儿
    if any(_parent_named_like_show(parent.name, t) for t in titles):
        return None
    if not titles:
        # 完全解析不出剧名：无法定位归属。仅当父级是明确分类层时给可区分
        # reason（分类层位置本身就是孤儿证据），否则交回旧流程保持旧文案。
        if _is_category_name(parent.name):
            return [_orphan_skip(
                season_dir,
                f"季目录内视频无法解析 title/season，保守跳过（{season_dir.name}）")]
        return None
    # 位置门②：父目录自带年份（形如 <片名> (年份) 媒体根）→ 不是分类层/库根层，
    # 绝不在别的媒体根里再建剧名根
    if _show_title_from_folder(parent.name)[1] is not None:
        return None
    if not parse_ok:
        return [_orphan_skip(
            season_dir,
            f"季目录内视频 title/season 解析不完整，保守跳过（{season_dir.name}）")]
    if len(titles) > 1:
        return [_orphan_skip(
            season_dir,
            f"季目录内混杂多个剧名，保守跳过"
            f"（{season_dir.name}：{'、'.join(sorted(titles))}）")]
    if len(seasons) > 1:
        shown = "/".join(f"S{s:02d}" for s in sorted(seasons))
        return [_orphan_skip(
            season_dir, f"季目录内季号不一致，保守跳过（{season_dir.name}：{shown}）")]
    file_season = next(iter(seasons))
    dir_season = _season_num_of_dir(season_dir.name)
    if dir_season is not None and dir_season != file_season:
        return [_orphan_skip(
            season_dir,
            f"目录名季号与文件不符，保守跳过（{season_dir.name} 内为 S{file_season:02d}）")]
    title_norm = next(iter(titles))

    # b) 父级（分类层/库根层）下找同名已有剧根：归一化匹配 <剧名> (年份)/<剧名>
    matches: list[Path] = []
    for s in sorted(sibling_pool):
        if s == season_dir or _is_hidden_name(s.name):
            continue
        if is_special_dir(s.name, org_cfg) or _SEASON_DIR_RE.match(s.name.strip()):
            continue
        if _is_episode_dir_name(s.name):
            continue  # 单集命名目录不是剧根
        s_title, s_year = _show_title_from_folder(s.name)
        if (s_title and _norm_name(s_title) == title_norm
                and _year_compatible(s_year, year)):
            matches.append(s)
    if len(matches) > 1:
        return [_orphan_skip(
            season_dir,
            "分类层下有多个同名候选剧根，无法区分（"
            + "、".join(m.name for m in matches) + "）")]

    # c) 未命中已有剧根：tmdb.enabled 且 resolved=true 才按标准名建根，否则保守 skip
    if matches:
        show_root = matches[0]
    else:
        if tmdb_cfg is None or not tmdb_cfg.enabled:
            return [_orphan_skip(
                season_dir,
                f"未找到已有剧名根且 tmdb 未启用，不建根（{season_dir.name}）")]
        try:
            r = resolver.resolve_cached(
                tmdb_cfg, display_title, year=year, media_type="tv")
        except (ResolverError, InvalidArguments):
            r = {}
        ident = r.get("identity") if isinstance(r, dict) else None
        if not (isinstance(r, dict) and r.get("resolved")
                and isinstance(ident, dict) and ident.get("tmdb_id")):
            return [_orphan_skip(
                season_dir,
                f"未找到已有剧名根且 TMDB 未解析出标准名，不建根（{season_dir.name}）")]
        canonical = _canonical_show_name(
            str(ident.get("canonical_title") or display_title),
            ident.get("year") or year,
        )
        show_root = parent / canonical

    # 归位 move 计划：字幕/nfo/jpg 照常跟随；搬空后的季目录由 cleanup 闭包收尾
    items: list[dict] = []
    for v in sorted(videos):
        pair = _best_episode(v)
        if pair is None:  # parse_ok=True 时不可达，防御性兜底
            return [_orphan_skip(
                season_dir,
                f"季目录内视频 title/season 解析不完整，保守跳过（{season_dir.name}）")]
        related = _related_files(v, subtitle_exts, org_cfg.follow_extensions)
        season_target = show_root / f"Season {pair[0]:02d}"
        items.append(_item(
            "move", source=str(v), target=str(season_target / v.name), kind=_TV,
            related=[(r, season_target / r.name) for r in related],
            reason=(f"孤儿季归位（{display_title} "
                    f"S{pair[0]:02d}E{pair[1]:02d} → {show_root.name}）"),
            confidence=0.9,
        ))
    return items


def _detect_orphan_season_dirs(
    parent: Path,
    season_dirs: set[Path],
    sibling_pool: set[Path],
    files_by_dir: dict[Path, list[Path]],
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    tmdb_cfg: TmdbConfig | None,
) -> list[dict] | None:
    """父目录下全部 Season 子目录的孤儿归位聚合（入口 B：无直接视频的分类层）。

    全部交回常规流程 → None（外层保持旧的静默 []）；
    有任一产出 → 汇总列表，个别无法定位的季目录补旧文案 skip 保持可见。
    """
    items: list[dict] = []
    unresolved: list[Path] = []
    for sd in sorted(season_dirs):
        videos = [f for f in files_by_dir.get(sd, [])
                  if _is_video_file(f, video_exts)]
        if not videos:
            unresolved.append(sd)
            continue
        got = _detect_orphan_season(
            sd, parent, videos, sibling_pool, subtitle_exts, org_cfg, tmdb_cfg)
        if got is None:
            unresolved.append(sd)
        else:
            items.extend(got)
    if not items:
        return None
    for sd in unresolved:
        items.append(_item("skip", source=str(sd), kind=_TV,
                           reason="已是 Season 目录形态"))
    return items


def _detect_child(
    child: Path,
    files: list[Path],
    subdirs: set[Path],
    root: Path,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    media_cfg: MediaConfig,
    child_dirs: set[Path],
    files_by_dir: dict[Path, list[Path]] | None = None,
    fp_groups: list[list[Path]] | None = None,
    fp_cache: dict | None = None,
    tmdb_cfg: TmdbConfig | None = None,
) -> list[dict]:
    """对库根的一个直接子目录做整理判定，返回该目录的计划项列表。"""
    rel_parts = child.relative_to(root).parts
    if _is_hidden_name(child.name):
        return []  # 隐藏目录不规划（扫描层已过滤，防御性兜底）
    if _in_special_parts(rel_parts, org_cfg):
        return [_item("skip", source=str(child), kind=_MOVIE,
                      reason=f"特殊目录（{child.name}）子树禁止整理")]
    if _SEASON_DIR_RE.match(child.name.strip()):
        # T11：分类层/库根层孤儿 Season 目录归位；正常 <剧名>/Season XX（父级
        # 有剧名）由位置门交回旧 skip，默认行为不变
        season_videos = [f for f in files if _is_video_file(f, video_exts)]
        if season_videos:
            orphan = _detect_orphan_season(
                child, root, season_videos, child_dirs - {child},
                subtitle_exts, org_cfg, tmdb_cfg,
            )
            if orphan is not None:
                return orphan
        return [_item("skip", source=str(child), kind=_TV, reason="已是 Season 目录形态")]

    videos = [f for f in files if f.suffix.lstrip(".").lower() in video_exts]
    if not videos:
        return _detect_child_without_videos(
            child, subdirs, root, video_exts, subtitle_exts, org_cfg, media_cfg,
            child_dirs, files_by_dir, tmdb_cfg=tmdb_cfg,
        )

    # 花絮/原盘类特殊子目录不阻断整季归季（原地保留）；只有非特殊子目录才触发
    # 「关系不明」熔断（BUG-5）
    real_subdirs = {d for d in subdirs
                    if not is_special_dir(d.name, org_cfg) and not _is_hidden_name(d.name)}

    # 仅"恰 1 个视频"时目录名才参与季集共识；多视频按散集/多版本处理，
    # 不用目录名做共识（目录名可能来自其中任意一个文件，无鉴别力）
    dir_episode = extract_episode_info(child.name) if len(videos) == 1 else None

    if len(videos) == 1 and not real_subdirs:
        return _detect_single_video_dir(
            child, videos[0], dir_episode, root, subtitle_exts, org_cfg, child_dirs
        )

    # 多视频（或仅含特殊子目录）目录：逐文件独立判定季集，只有唯一确定 (s,e) 的才动
    items: list[dict] = []
    seen_pairs: dict[tuple[int, int], str] = {}
    if len(videos) > 1 and not real_subdirs and dir_episode is None:
        unmarked: list[Path] = []
        for video in sorted(videos):
            pair = extract_episode_info(video.name)
            if pair is None:
                unmarked.append(video)
                continue
            if pair in seen_pairs:
                # 同季集多版本：双双放弃（§22 多版本影片禁止误整理）
                items.append(_item("blocked", source=str(video), kind=_TV,
                                   reason=f"同批存在另一版本 {Path(seen_pairs[pair]).name}（季集重复）"))
                continue
            seen_pairs[pair] = str(video)
            related = _related_files(video, subtitle_exts, org_cfg.follow_extensions)
            # Season 目录建在子目录内部：保持剧集目录分组，避免不同剧集在 root 层互相撞车
            season_dir = child / f"Season {pair[0]:02d}"
            target = season_dir / video.name
            items.append(_item(
                "move", source=str(video), target=str(target), kind=_TV,
                related=[(r, season_dir / r.name) for r in related],
                reason=f"散集归季（S{pair[0]:02d}E{pair[1]:02d}）", confidence=0.9,
            ))
        # 无季集标记的视频：若解析为同一部电影且多份共存，按指纹给
        # identical/conflict 明确信号（v0.11.8 NEW-3），不再一律 skip
        items.extend(_detect_same_movie_videos(
            child, unmarked, root, video_exts, fp_groups or [],
            media_cfg, fp_cache or {},
        ))
        return items

    # 正常剧集根下散落视频 + Season 子目录（v0.11.8 NEW-4）：
    # 与 Season 内副本指纹一致 → 根层冗余 duplicate；有季集标记 → 归季。
    if any(_SEASON_DIR_RE.match(s.name.strip()) for s in real_subdirs):
        return _detect_show_root_strays(
            child, videos, real_subdirs, root, video_exts, subtitle_exts,
            org_cfg, files_by_dir or {}, fp_groups or [],
        )

    # O-4：含非特殊子目录的多视频目录，先尝试同片判重（identical/conflict），
    # 避免对电影目录含 Bonus/Extra 等子目录时一律笼统 skip。若全为 skip 则
    # 保留目录级 skip（形态不明不整理）。
    smv = _detect_same_movie_videos(
        child, videos, root, video_exts, fp_groups or [],
        media_cfg, fp_cache or {},
    )
    if any(i["action"] in ("identical", "conflict") for i in smv):
        return smv

    # 含非特殊子目录或形态不明的多视频目录：不确定就不动（§22）
    return [_item("skip", source=str(child), kind=_TV,
                  reason="目录内含非花絮子目录或多视频且关系无法确认，不整理")]


def _detect_child_without_videos(
    child: Path,
    subdirs: set[Path],
    root: Path,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    media_cfg: MediaConfig,
    child_dirs: set[Path],
    files_by_dir: dict[Path, list[Path]] | None = None,
    tmdb_cfg: TmdbConfig | None = None,
) -> list[dict]:
    """无直接视频的子目录：区分『剧集目录（内含 Season）』与『分类/空/不明目录』。"""
    real_subdirs = {d for d in subdirs
                    if not is_special_dir(d.name, org_cfg) and not _is_hidden_name(d.name)}
    if not real_subdirs:
        return []  # 空目录或仅花絮/原盘/隐藏子目录：原地保留
    if any(_SEASON_DIR_RE.match(d.name.strip()) for d in real_subdirs):
        return _detect_show_folder(
            child, real_subdirs, root, video_exts, subtitle_exts,
            org_cfg, media_cfg, child_dirs, files_by_dir or {},
            tmdb_cfg=tmdb_cfg,
        )
    # 纯分类层由库根发现统一处理；走到这里说明超过下探深度或形态不明
    return [_item("skip", source=str(child), kind=_TV,
                  reason=(f"目录无直接视频且子目录关系无法确认（分类下探最多 "
                          f"{_MAX_CATEGORY_DEPTH} 层），请对该目录单独运行整理"))]


def _fp_peer_map(fp_groups: list[list[Path]]) -> dict[Path, set[Path]]:
    """path → 同字节大小+同 ffprobe 指纹的其他文件集合（复用步骤0已probe的分组）。"""
    peers: dict[Path, set[Path]] = {}
    for same in fp_groups:
        for f in same:
            peers.setdefault(f, set()).update(x for x in same if x != f)
    return peers


def _detect_same_movie_videos(
    child: Path,
    videos: list[Path],
    root: Path,
    video_exts: frozenset[str],
    fp_groups: list[list[Path]],
    media_cfg: MediaConfig,
    fp_cache: dict,
) -> list[dict]:
    """无季集标记的多视频：按作品身份（_work_identity_key）+ ffprobe 指纹
    两级判定（v0.11.8 NEW-3，v0.11.14 O-4）：

    - 同身份 + 同指纹 → identical（keeper 保留，其余为冗余副本）；
    - 同身份 + 不同指纹 → conflict（同片不同版本，保留双方人工选择）；
    - 跨身份 + 同指纹 → conflict（O-4：同目录字节相同但片名不同，疑似同片异名
      副本或跨作品误标，按 NEW-11 安全原则不跨作品互删，保留双方人工确认）；
    - 单个无关联视频 → skip。
    """
    by_identity: dict[tuple, list[Path]] = {}
    for v in videos:
        by_identity.setdefault(_work_identity_key(v), []).append(v)

    fp_of: dict[Path, tuple | None] = {
        v: _probe_fp_cached(v, media_cfg, fp_cache) for v in videos
    }
    fp_peers = _fp_peer_map(fp_groups)
    items: list[dict] = []
    handled: set[Path] = set()

    # 1) 同身份组内判定
    for key, group in by_identity.items():
        if len(group) < 2:
            continue  # 单文件留到跨身份检测或最终 skip
        title = (parse_video_filename(group[0].name).title
                 or group[0].stem)
        fps = {fp_of[v] for v in group if fp_of[v] is not None}
        if len(fps) <= 1:
            # 同身份同指纹（或指纹缺失无法区分）→ identical；
            # T7：keeper 与 library 同口径——质量分高 → 文件大 → 均等才字典序
            cluster = sorted(group,
                             key=lambda x: (*_keeper_quality_key(x), str(x).lower()))
            keeper = cluster[0]
            for v in cluster[1:]:
                items.append(_item(
                    "identical", source=str(v), target=str(keeper),
                    kind=_MOVIE, level=3,
                    reason=(f"目录内同片《{title}》的副本，字节大小与 ffprobe "
                            f"指纹完全一致，保留 {keeper.name}、本文件为冗余"
                            f"（删除需 enable_delete=true 且本目录可写）；"
                            f"手动处理：rm -- {shlex.quote(str(v))}"),
                ))
            handled.update(cluster)
        else:
            # 同身份不同指纹 → conflict（同片不同版本）
            for v in group:
                items.append(_item(
                    "conflict", source=str(v), kind=_MOVIE,
                    reason=(f"目录内同为《{title}》但 ffprobe 指纹不同的版本，"
                            f"内容冲突，保留双方不移动、不删除，请人工选择保留版本"),
                ))
            handled.update(group)

    # 2) O-4：跨身份同指纹检测——同目录内字节+指纹相同但作品身份不同的文件，
    #    按 NEW-11 安全原则判为 conflict（不跨作品互删），对齐 detect 口径。
    by_fp: dict[tuple, list[Path]] = {}
    for v in videos:
        fp = fp_of[v]
        if fp is not None and v not in handled:
            by_fp.setdefault(fp, []).append(v)
    for cluster in by_fp.values():
        if len(cluster) >= 2:
            for v in cluster:
                items.append(_item(
                    "conflict", source=str(v), kind=_MOVIE,
                    reason=(f"目录内与其他文件字节+ffprobe 指纹完全一致但作品"
                            f"身份不同（疑似同片异名副本或跨作品误标），保留双方"
                            f"不删除，请人工确认归属"),
                ))
            handled.update(cluster)

    # 3) 未处理的单个视频 → skip
    for v in videos:
        if v not in handled:
            items.append(_item(
                "skip", source=str(v), kind=_MOVIE,
                reason="目录内单个无季集标记视频，无法确认归属，不整理",
            ))
    return items


def _detect_show_root_strays(
    child: Path,
    videos: list[Path],
    real_subdirs: set[Path],
    root: Path,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    files_by_dir: dict[Path, list[Path]],
    fp_groups: list[list[Path]],
) -> list[dict]:
    """正常名称剧集根的根层散落视频处理（v0.11.8 NEW-4，参考 Sonarr import）。

    - 与 Season 子树内某视频字节大小+ffprobe 指纹一致 → 根层副本为冗余，
      出 duplicate（target 指向 Season 内规范位副本），同目录伴生字幕随 keeper
      移动（keeper 已有同名伴生时冗余删除，NEW-15/17）；
    - 无指纹一致但文件名有唯一季集标记 → move 进本目录 Season XX/。
    """
    season_subs = {s for s in real_subdirs
                   if _SEASON_DIR_RE.match(s.name.strip())}
    inside: set[Path] = set()
    for d, fs in files_by_dir.items():
        if any(d == s or s in d.parents for s in season_subs):
            inside.update(f for f in fs if _is_video_file(f, video_exts))

    fp_peers = _fp_peer_map(fp_groups)
    items: list[dict] = []
    for v in sorted(videos):
        match = next((p for p in fp_peers.get(v, set()) if p in inside), None)
        if match is not None:
            related = _related_files(v, subtitle_exts, org_cfg.follow_extensions)
            dup = _item(
                "duplicate", source=str(v), target=str(match), kind=_TV, level=3,
                reason=(
                    f"剧集根目录散落视频与 {match.relative_to(root)} 的字节大小与 "
                    f"ffprobe 指纹完全一致；Season 子树内是规范位副本，本文件为"
                    f"冗余（保留 Season 副本）。自动删除需 enable_delete=true 且"
                    f"本目录可写；可手动处理：rm -- {shlex.quote(str(v))}"
                ),
            )
            dup["related"] = [
                {"source": str(r), "target": str(match.parent / r.name)}
                for r in related
            ]
            items.append(dup)
            continue
        pair = _best_episode(v)
        if pair is None:
            items.append(_item(
                "skip", source=str(v), kind=_TV,
                reason="剧根散落视频无季集标记、且与 Season 内副本指纹不一致，不整理",
            ))
            continue
        related = _related_files(v, subtitle_exts, org_cfg.follow_extensions)
        season_dir = child / f"Season {pair[0]:02d}"
        items.append(_item(
            "move", source=str(v), target=str(season_dir / v.name), kind=_TV,
            related=[(r, season_dir / r.name) for r in related],
            reason=f"剧根散落视频归季（S{pair[0]:02d}E{pair[1]:02d}）", confidence=0.9,
        ))
    return items


def _detect_show_folder(
    child: Path,
    real_subdirs: set[Path],
    root: Path,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    media_cfg: MediaConfig,
    child_dirs: set[Path],
    files_by_dir: dict[Path, list[Path]],
    tmdb_cfg: TmdbConfig | None = None,
) -> list[dict]:
    """含 Season 子目录的剧集目录处理。

    名称正常（无季集标记）→ 已是 <show>/Season XX 规范结构，不动；
    名称是单集/季命名（含 SxxExx/Sxx/Season NN 文本等标记）→ 历史错误结构
    （参考 Sonarr "Rename Series Folder"）：
      - 规范根不存在：出 rename_dir（L2，整目录改名，文件随目录整体保留）；
      - 规范根已存在：逐集并入 <canonical>/Season XX/（普通 move），
        搬空后由 cleanup 闭包预览壳目录清理（L3）。
    冗余副本（含 rename 子树与库根散件指纹相同的情形）不在此判定——
    统一在规划完成后做"结构感知副本裁决"（_arbitrate_duplicates），避免
    基于当前错误结构选出错误 keeper（v0.11.7 BUG-1）。
    """
    season_subs = {d for d in real_subdirs if _SEASON_DIR_RE.match(d.name.strip())}
    if not season_subs:
        return []
    if not _is_misnamed_show_root(child.name):
        # T11：分类层/库根层孤儿 Season 目录归位（本目录无剧名才进新分支；
        # 正常 <剧名>/Season XX 由 _detect_orphan_season 的位置门交回，行为不变）
        orphan = _detect_orphan_season_dirs(
            child, season_subs, real_subdirs, files_by_dir,
            video_exts, subtitle_exts, org_cfg, tmdb_cfg,
        )
        return orphan if orphan is not None else []

    videos = sorted(
        f for d, fs in files_by_dir.items()
        if d == child or child in d.parents
        for f in fs if _is_video_file(f, video_exts)
    )
    if not videos:
        return []

    # 剧名剥离目录名里残留的季信息文本：
    # "Supernatural (2005) Season 10 S10 (...)" → ("Supernatural", 2005)
    title, year = _show_title_from_folder(child.name)
    if not title:
        return [_item("skip", source=str(child), kind=_TV,
                      reason="单集命名的剧集目录但无法解析出剧名，不整理")]
    entity = resolve_media_entity(title, year)
    canonical = _resolve_show_dir_for_entity(entity, root, child_dirs)
    if canonical is None:
        return [_item("skip", source=str(child), kind=_TV,
                      reason=f"剧名 {title} 有多个候选目录且无法区分，不整理")]

    if canonical == child:
        # v0.11.9 NEW-12：规范根解析结果就是当前目录自身——文件已在
        # <本目录>/Season NN/ 就位（无其他候选剧根）。纯 no-op：不生成自指
        # move（自比对必为 identical），更不能连锁 cleanup 删光整部剧。
        return []

    if not canonical.exists():
        return [_item(
            "rename_dir", source=str(child), target=str(canonical), kind=_TV, level=2,
            reason=(f"单集/季命名的剧集目录（{child.name}）应改为规范剧集根 "
                    f"{canonical.name}（仅改目录名，Season 与文件随目录整体保留，"
                    f"对应 Sonarr 的 Rename Series Folder）"),
            confidence=0.95,
        )]

    # 规范根已存在：逐集并入（不重名目录，避免覆盖已整理内容）；同目标的指纹
    # 冗余由规划后裁决统一转为 duplicate，这里每个视频照常出 move。
    items: list[dict] = []
    for v in videos:
        pair = _best_episode(v)
        if pair is None:
            items.append(_item("skip", source=str(v), kind=_TV,
                               reason="无法从文件名/目录识别季集，不并入规范剧集根"))
            continue
        related = _related_files(v, subtitle_exts, org_cfg.follow_extensions)
        season_dir = canonical / f"Season {pair[0]:02d}"
        items.append(_item(
            "move", source=str(v), target=str(season_dir / v.name), kind=_TV,
            related=[(r, season_dir / r.name) for r in related],
            reason=f"并入规范剧集根 {canonical.name}（S{pair[0]:02d}E{pair[1]:02d}）",
            confidence=0.9,
        ))
    return items



# ---------------------------------------------------------------------------
# NEW-18：跨根实体归并——同一部剧分居多个剧集根的发现与合并。
#
# 现有检测链逐根独立判定：候选根只与"和自己本地 norm 相同"的目录归并
# （_resolve_show_dir_for_entity）。"Lioness (2023)"（norm 'lioness'）与
# "Special.Ops.Lioness.S01...[TGx]"（norm 'special ops lioness'）是同一 TMDB
# 实体的同义名，本地归一不可感知 → 两根各自 NEW-12 no-op → organize 完全静默。
# 本 pass 在逐根检测之后运行：
#   - tmdb 启用：候选根解析 tmdb_id，按 id 归并（同义名打通）；
#   - tmdb 关闭/失败：本地启发式只生成 blocked 疑似提示，绝不自动合并
#     （异名误合并是数据级事故）；
#   - tmdb 明确异 id：不处理。
# ---------------------------------------------------------------------------

# 疑似判定的英文停用词（启发式仅用于 blocked 提示）
_SUSPECT_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "in", "on", "to", "for", "with", "at",
    "is", "it", "its", "by", "or", "be", "as", "from", "s",
})


def _content_tokens(norm: str) -> set[str]:
    return {t for t in norm.split() if t and t not in _SUSPECT_STOPWORDS}


def _suspect_same_show(n1: str, n2: str) -> bool:
    """本地疑似同剧（仅降级提示用）：一方内容词集合是另一方子集。

    "lioness" ⊂ "special ops lioness" → 疑似；绝不用于生成 merge 计划。
    """
    t1, t2 = _content_tokens(n1), _content_tokens(n2)
    if not t1 or not t2:
        return False
    return t1 <= t2 or t2 <= t1


def _year_compatible(y1: int | None, y2: int | None) -> bool:
    return y1 is None or y2 is None or y1 == y2


def _root_touched(path: Path, handled_srcs: list[Path]) -> bool:
    """该根（或其子树文件）是否已被现有检测链的 move/rename_dir 触及。"""
    return any(path == p or path in p.parents for p in handled_srcs)


def _collect_show_root_candidates(
    lr: Path,
    lr_children: set[Path],
    subdirs_by_dir: dict[Path, set[Path]],
    org_cfg: OrganizerConfig,
) -> list[dict]:
    """lr 直接子目录中含 Season XX 的候选剧集根，附本地 (title,norm,year)。"""
    out: list[dict] = []
    for child in sorted(lr_children):
        subs = subdirs_by_dir.get(child, set())
        if not any(_SEASON_DIR_RE.match(d.name.strip()) for d in subs):
            continue
        if _is_hidden_name(child.name):
            continue
        if _in_special_parts(child.relative_to(lr).parts, org_cfg):
            continue
        title, year = _show_title_from_folder(child.name)
        if not title:
            continue
        out.append({
            "path": child, "title": title, "year": year,
            "norm": _norm_name(title),
            "tmdb_id": None, "orig_title": "", "ent_year": None,
            "resolved": False,
        })
    return out


def _resolve_candidate_identities(
    candidates: list[dict], tmdb_cfg: TmdbConfig | None
) -> None:
    """候选根 TMDB 解析（带缓存）；未启用/失败一律保持未确认（降级不静默）。"""
    if tmdb_cfg is None or not tmdb_cfg.enabled:
        return
    cache: dict[tuple[str, int | None], dict] = {}
    for c in candidates:
        key = (c["norm"], c["year"])
        if key not in cache:
            try:
                cache[key] = resolver.resolve(
                    tmdb_cfg, c["title"], year=c["year"], media_type="tv")
            except (ResolverError, InvalidArguments):
                cache[key] = {}
        r = cache[key]
        ident = r.get("identity") if isinstance(r, dict) else None
        if isinstance(ident, dict) and ident.get("tmdb_id"):
            c["tmdb_id"] = int(ident["tmdb_id"])
            c["orig_title"] = str(ident.get("original_title") or "")
            c["ent_year"] = ident.get("year")
            c["resolved"] = True


def _plan_merge_roots(
    roots: list[dict],
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    files_by_dir: dict[Path, list[Path]],
) -> list[dict]:
    """同实体多根：裁决唯一 keeper，非 keeper 根视频逐集 move 并入。

    keeper 裁决（工单 §4）：根名匹配实体规范根（TMDB original_title 构造）
    > 含 Season 数多 > 年份精确匹配；平局取目录名字典序最小（确定性兜底）。
    目标同名冲突由现有 judge_conflict 链处理（identical/conflict/blocked）；
    源根搬空后由 cleanup 闭包预览（内有杂项文件时被阻断，源根保留）。
    """
    tmdb_years = [r["ent_year"] for r in roots if r["ent_year"] is not None]
    own_years = [r["year"] for r in roots if r["year"] is not None]
    if tmdb_years:
        ent_year = max(set(tmdb_years), key=tmdb_years.count)
    elif own_years:
        ent_year = max(set(own_years), key=own_years.count)
    else:
        ent_year = None
    tmdb_id = next((r["tmdb_id"] for r in roots if r["tmdb_id"]), None)
    canon_norm = None
    if tmdb_id:
        orig = next((r["orig_title"] for r in roots if r["orig_title"]), "")
        canon_norm = _norm_name(orig) if orig else None

    def _season_count(d: Path) -> int:
        try:
            return sum(1 for x in d.iterdir()
                       if x.is_dir() and _SEASON_DIR_RE.match(x.name.strip()))
        except OSError:
            return 0

    def _rank(r: dict) -> tuple[int, int, int]:
        d = r["path"]
        ft, fy = _show_dir_fingerprint(d.name)
        if canon_norm is not None:
            s_canon = 1 if ft == canon_norm and _year_compatible(fy, ent_year) else 0
        else:
            s_canon = 1 if ft == r["norm"] and fy is not None and fy == ent_year else 0
        s_year = 1 if r["year"] is not None and r["year"] == ent_year else 0
        return (s_canon, _season_count(d), s_year)

    ordered = sorted(roots, key=lambda r: r["path"].name)  # 字典序（稳定排序兜底）
    ordered.sort(key=lambda r: _rank(r), reverse=True)
    keeper_path = ordered[0]["path"]

    items: list[dict] = []
    for r in ordered[1:]:
        src_root = r["path"]
        videos = sorted(
            f for d, fs in files_by_dir.items()
            if d == src_root or src_root in d.parents
            for f in fs if _is_video_file(f, video_exts))
        for v in videos:
            pair = _best_episode(v)
            if pair is None:
                items.append(_item("skip", source=str(v), kind=_TV,
                                   reason="同剧多根合并但无法识别季集，不移动"))
                continue
            related = _related_files(v, subtitle_exts, org_cfg.follow_extensions)
            season_dir = keeper_path / f"Season {pair[0]:02d}"
            id_hint = f"tmdb {tmdb_id}，" if tmdb_id else ""
            items.append(_item(
                "move", source=str(v), target=str(season_dir / v.name), kind=_TV,
                related=[(rl, season_dir / rl.name) for rl in related],
                reason=(f"同剧多根合并（{id_hint}{src_root.name} 的 "
                        f"S{pair[0]:02d}E{pair[1]:02d} 并入 keeper "
                        f"{keeper_path.name}）"),
                confidence=0.95,
            ))
    return items


def _detect_multi_show_roots(
    lr: Path,
    lr_children: set[Path],
    files_by_dir: dict[Path, list[Path]],
    subdirs_by_dir: dict[Path, set[Path]],
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    tmdb_cfg: TmdbConfig | None,
    handled_srcs: list[Path],
) -> list[dict]:
    """跨根实体归并 pass（NEW-18），返回 merge move / blocked 提示项。"""
    candidates = _collect_show_root_candidates(
        lr, lr_children, subdirs_by_dir, org_cfg)
    if len(candidates) < 2:
        return []
    # 已被现有链路触及的根不参与（同 norm 多根现有链路可处理，避免重复出项）
    candidates = [c for c in candidates if not _root_touched(c["path"], handled_srcs)]
    if len(candidates) < 2:
        return []
    _resolve_candidate_identities(candidates, tmdb_cfg)

    # 高置信分组（union-find）：同 tmdb_id 或同 norm 且年兼容
    parent = list(range(len(candidates)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            ci, cj = candidates[i], candidates[j]
            if ci["tmdb_id"] and ci["tmdb_id"] == cj["tmdb_id"]:
                union(i, j)
            elif ci["norm"] == cj["norm"] and _year_compatible(ci["year"], cj["year"]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(candidates)):
        groups.setdefault(find(i), []).append(i)

    items: list[dict] = []
    merged_pairs: set[frozenset] = set()
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        grp = [candidates[i] for i in idxs]
        for a in idxs:
            for b in idxs:
                if a != b:
                    merged_pairs.add(frozenset((a, b)))
        items.extend(_plan_merge_roots(
            grp, video_exts, subtitle_exts, org_cfg, files_by_dir))

    # 疑似对：未高置信归并、至少一方 TMDB 未确认、本地启发式命中 → blocked 提示
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            if frozenset((i, j)) in merged_pairs:
                continue
            ci, cj = candidates[i], candidates[j]
            if ci["resolved"] and cj["resolved"]:
                continue  # TMDB 均成功且 id 不同 → 明确异剧
            if not _suspect_same_show(ci["norm"], cj["norm"]):
                continue
            id_hint = (f"（tmdb {ci['tmdb_id']}）"
                       if ci["tmdb_id"] or cj["tmdb_id"] else "")
            items.append(_item(
                "blocked", source=str(cj["path"]), kind=_TV, level=2,
                reason=("同剧多根分居疑似且 TMDB 未确认，请人工处理："
                        f"{ci['path'].name} | {cj['path'].name}{id_hint}；"
                        "确认同剧后可将季目录合并到规范根"),
                confidence=0.5,
            ))
    return items


def _detect_single_video_dir(
    child: Path,
    video: Path,
    dir_episode: tuple[int, int] | None,
    root: Path,
    subtitle_exts: frozenset[str],
    org_cfg: OrganizerConfig,
    child_dirs: set[Path],
) -> list[dict]:
    """恰含 1 个视频、无（非花絮）子目录的目录：优先按剧集，其次按电影包装目录。"""
    related = _related_files(video, subtitle_exts, org_cfg.follow_extensions)

    # 剧集单集文件夹：文件名与目录名的季集共识（§18/§19）
    file_pair = extract_episode_info(video.name)
    pair: tuple[int, int] | None
    if file_pair is not None and dir_episode is not None:
        pair = file_pair if file_pair == dir_episode else None  # 冲突 → 放弃
    else:
        pair = file_pair or dir_episode
    if pair is not None:
        # BUG-2：目标必须带剧名 <show>/Season XX/，禁止裸 root/Season XX 丢剧名
        title = _show_title_for(child.name, video.name)
        if not title:
            return [_item("skip", source=str(video), kind=_TV,
                          reason="无法从目录名/文件名解析出剧名，不整理")]
        year = (parse_video_filename(child.name).year
                or parse_video_filename(video.name).year)
        # V2：先确定 MediaEntity（canonical_root 由 title+year 算出），
        # 再由实体解析 canonical root 目录——身份与目录显式分离。
        entity = resolve_media_entity(title, year, season=pair[0], episode=pair[1])
        show_dir = _resolve_show_dir_for_entity(entity, root, child_dirs)
        if show_dir is None:
            return [_item("skip", source=str(video), kind=_TV,
                          reason=f"剧名 {title} 有多个候选目录且无法区分，不整理")]
        season_dir = show_dir / f"Season {pair[0]:02d}"
        return [_item(
            "move", source=str(video), target=str(season_dir / video.name), kind=_TV,
            related=[(r, season_dir / r.name) for r in related],
            reason=f"单集文件夹归季（{title} S{pair[0]:02d}E{pair[1]:02d}）", confidence=0.95,
        )]

    # 电影包装目录：目录名与视频名解析出的片名一致（年份一致时更稳），且双方无季集标记（§17）
    q_dir = parse_video_filename(child.name)
    q_vid = parse_video_filename(video.name)
    dir_has_ep = extract_episode_info(child.name) is not None
    if (
        not dir_has_ep
        and q_dir.title and q_vid.title
        and q_dir.title.lower() == q_vid.title.lower()
        and (q_dir.year is None or q_vid.year is None or q_dir.year == q_vid.year)
    ):
        return [_item(
            "move", source=str(video), target=str(root / video.name), kind=_MOVIE,
            related=[(r, root / r.name) for r in related],
            reason="电影包装目录展开（目录名与片名一致）", confidence=0.95,
        )]

    return [_item("skip", source=str(video), kind=_MOVIE,
                  reason="目录名与视频名无法建立可靠关联，不整理")]


def _probe_fingerprint(video: Path, media_cfg: MediaConfig) -> tuple | None:
    """ffprobe 指纹（时长/宽高/编码），用于同名冲突的内容级比较；失败返回 None。"""
    try:
        pv = probe_video(video, media_cfg.ffprobe_path, media_cfg.ffprobe_timeout_seconds)
    except (ProbeError, OSError):
        return None
    v = pv.get("video") or {}
    return (pv.get("duration_seconds"), v.get("width"), v.get("height"), v.get("codec"))


def judge_conflict(
    src: Path, dst: Path, media_cfg: MediaConfig, fp_cache: dict | None = None
) -> tuple[str, str]:
    """目标同名冲突三态判定（§23）：proceed / identical / conflict / blocked。

    大小不同 → conflict；大小相同 → ffprobe 比时长（±1s）与宽高编码，
    一致 → identical（可删源，L3）；probe 失败 → blocked（无法判断不操作）。
    """
    if not dst.exists():
        return "proceed", ""
    try:
        if src.stat().st_size != dst.stat().st_size:
            return "conflict", "size_mismatch"
    except OSError as exc:
        return "blocked", f"stat_failed: {exc}"
    if fp_cache is not None:
        fp_src = _probe_fp_cached(src, media_cfg, fp_cache)
        fp_dst = _probe_fp_cached(dst, media_cfg, fp_cache)
    else:
        fp_src = _probe_fingerprint(src, media_cfg)
        fp_dst = _probe_fingerprint(dst, media_cfg)
    if fp_src is None or fp_dst is None:
        return "blocked", "probe_failed_cannot_verify"
    dur_s, dur_d = fp_src[0], fp_dst[0]
    dur_equal = dur_s is None or dur_d is None or abs(dur_s - dur_d) <= 1.0
    if fp_src[1:] == fp_dst[1:] and dur_equal:
        return "identical", ""
    return "conflict", "media_mismatch"


def plan_session(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_cfg: MediaConfig,
    org_cfg: OrganizerConfig,
    *,
    path: str,
    mode: str = "all",
    preexisting_empty_scope: str = "library_root",
    tmdb_cfg: TmdbConfig | None = None,
    language_split: bool = False,
) -> dict:
    """扫描目录并生成整理计划（只读，L1）。

    自动下探纯分类层（电视剧/电影/动漫等，最多 %d 层），对每个库根分别规划；
    单集命名的错误剧集根（含 SxxExx 且内含 Season）出 rename_dir 改名项。
    mode: all | movie | tv。preexisting_empty_scope: 预存空目录收集范围，
    library_root（默认，全库根扫描）| target_subtree（仅本轮操作目标子树）。
    language_split: 开启时按国语/外语在每个库根下建子目录分层（ffprobe 音轨判断）。
    返回 {ok, dry_run, mode, library_roots, counts,
    plan[], write_preflight, scan_truncated, plan_truncated, errors}；
    同名目标撞车、季集重复等一律 blocked，冗余副本由 keeper 唯一裁决为 duplicate。
    """ % _MAX_CATEGORY_DEPTH
    root = guard.check(path)
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在: {path}")
    mode = (mode or "all").strip().lower()
    if mode not in ("all", _MOVIE, _TV):
        raise InvalidArguments(f"mode 只支持 all/movie/tv，得到: {mode!r}")
    preexisting_empty_scope = (preexisting_empty_scope or "library_root").strip().lower()
    if preexisting_empty_scope not in ("library_root", "target_subtree"):
        raise InvalidArguments(
            f"preexisting_empty_scope 只支持 library_root/target_subtree，"
            f"得到: {preexisting_empty_scope!r}")

    errors: list[str] = []
    state: dict = {}
    # 遍历错误走 ErrorSink：白名单内固有不可读目录首次上报，之后只计数（#5）
    walk_sink = ErrorSink(limits.max_errors_reported)

    def on_error(p: Path, msg: str) -> None:
        walk_sink.add(p, msg)

    files_by_dir: dict[Path, list[Path]] = {}
    subdirs_by_dir: dict[Path, set[Path]] = {}
    hidden_dirs: set[Path] = set()  # 不参与规划，但物理存在（阻断空目录清理闭包）
    size_index: dict[int, list[Path]] = {}  # 视频文件按字节大小分组，用于副本裁决
    for fpath, st in iter_tree(
        root, guard,
        max_files=limits.max_scan_files,
        max_depth=limits.max_list_depth,
        include_dirs=True,
        on_error=on_error,
        state=state,
    ):
        if stat_module.S_ISDIR(st.st_mode):
            if _is_hidden_name(fpath.name):
                hidden_dirs.add(fpath)  # 不进规划结构；空目录清理闭包仍会考虑其物理存在
                continue
            subdirs_by_dir.setdefault(fpath.parent, set()).add(fpath)
            continue
        if not stat_module.S_ISREG(st.st_mode):
            continue
        rel_parts = fpath.relative_to(root).parts
        if len(rel_parts) > 1 and (
            _in_special_parts(rel_parts[:-1], org_cfg)
            or any(_is_hidden_name(p) for p in rel_parts[:-1])
        ):
            continue  # 特殊/隐藏目录子树内的文件不参与判定
        if _is_hidden_name(fpath.name):
            continue  # 隐藏文件（.DS_Store 等）不整理
        files_by_dir.setdefault(fpath.parent, []).append(fpath)
        if fpath.suffix.lstrip(".").lower() in video_exts:
            size_index.setdefault(st.st_size, []).append(fpath)

    # 0) 指纹分组（同大小+ffprobe）+ 库根发现（分类目录下探）。
    #    副本裁决推迟到规划完成后（rename_dir 会改变"规范位"语义，见 BUG-1）。
    fp_cache: dict = {}
    fp_groups = _fingerprint_groups(size_index, media_cfg, fp_cache)
    library_roots, category_dirs = _discover_library_roots(
        root, files_by_dir, subdirs_by_dir, video_exts, org_cfg,
    )

    plan: list[dict] = []
    move_budget = org_cfg.max_move_files
    plan_truncated = False

    def _append(items: list[dict]) -> None:
        nonlocal move_budget, plan_truncated
        for item in items:
            if item["action"] == "move":
                if move_budget <= 0:
                    plan_truncated = True
                    continue
                move_budget -= 1
            plan.append(item)

    for lr in library_roots:
        lr_children = subdirs_by_dir.get(lr, set())

        # 1) 该库根直接散落的视频：有唯一季集标记 → 按剧名归季（禁止裸 Season）
        seen_pairs: dict[tuple[str, tuple[int, int]], str] = {}
        for video in sorted(files_by_dir.get(lr, [])):
            if not _is_video_file(video, video_exts):
                continue
            pair = extract_episode_info(video.name)
            if pair is None:
                plan.append(_item("skip", source=str(video), kind=_TV,
                                  reason="库根散落文件且无季集标记，不整理"))
                continue
            title = _show_title_for(None, video.name)
            if not title:
                plan.append(_item("skip", source=str(video), kind=_TV,
                                  reason="库根散落文件无法解析出剧名，不整理"))
                continue
            dedup_key = (_norm_name(title), pair)
            if dedup_key in seen_pairs:
                plan.append(_item("blocked", source=str(video), kind=_TV,
                                  reason=f"同剧同批存在另一版本 {Path(seen_pairs[dedup_key]).name}（季集重复）"))
                continue
            seen_pairs[dedup_key] = str(video)
            year = parse_video_filename(video.name).year
            # V2：先确定 MediaEntity，再解析 canonical root
            entity = resolve_media_entity(title, year, season=pair[0], episode=pair[1])
            show_dir = _resolve_show_dir_for_entity(entity, lr, lr_children)
            if show_dir is None:
                plan.append(_item("skip", source=str(video), kind=_TV,
                                  reason=f"剧名 {title} 有多个候选目录且无法区分，不整理"))
                continue
            season_dir = show_dir / f"Season {pair[0]:02d}"
            related = _related_files(video, subtitle_exts, org_cfg.follow_extensions)
            plan.append(_item(
                "move", source=str(video), target=str(season_dir / video.name), kind=_TV,
                related=[(r, season_dir / r.name) for r in related],
                reason=f"根散集归季（{title} S{pair[0]:02d}E{pair[1]:02d}）", confidence=0.9,
            ))

        # 2) 该库根的每个直接子目录（分类目录自身已作为独立库根处理，跳过；
        #    隐藏目录不进 subdirs_by_dir，天然不会出现在这里）
        for child in sorted(lr_children):
            if child in category_dirs:
                continue
            items = _detect_child(
                child, files_by_dir.get(child, []), subdirs_by_dir.get(child, set()),
                lr, video_exts, subtitle_exts, org_cfg, media_cfg, lr_children,
                files_by_dir, fp_groups, fp_cache, tmdb_cfg=tmdb_cfg,
            )
            _append(items)

        # 3) NEW-18：跨根实体归并——同一部剧的同义名根（TMDB id 相同但本地
        #    norm 不等）逐根检测会双双 no-op 静默；此 pass 出合并计划，
        #    TMDB 不可用时降级为 blocked 疑似提示（不自动合并）。
        handled_srcs = [
            Path(i["source"]) for i in plan
            if i["action"] in ("move", "rename_dir")
        ]
        _append(_detect_multi_show_roots(
            lr, lr_children, files_by_dir, subdirs_by_dir,
            video_exts, subtitle_exts, org_cfg, tmdb_cfg, handled_srcs,
        ))

    # 3.5) 语言分层（language_split=True）：按音轨判断国语/外语，重写 move 目标路径
    if language_split:
        _apply_language_split(plan, library_roots, media_cfg, video_exts, tmdb_cfg)

    # 4) 同批目标撞车：两个 move 指向同一目标 → 双双 blocked（多版本保护）
    by_target: dict[str, list[dict]] = {}
    for item in plan:
        if item["action"] == "move":
            by_target.setdefault(item["target"].lower(), []).append(item)
    for group in by_target.values():
        if len(group) > 1:
            for item in group:
                item["action"] = "blocked"
                item["reason"] = f"同批另一项指向相同目标（{Path(item['target']).name}）"

    # 5) 模式过滤 + 磁盘三态判定（不存在/一致/冲突）+ 结构感知副本裁决。
    if mode != "all":
        plan = [i for i in plan if i["kind"] == mode or i["action"] in ("skip", "blocked")]

    for item in plan:
        if item["action"] != "move":
            continue
        src, dst = Path(item["source"]), Path(item["target"])
        verdict, why = judge_conflict(src, dst, media_cfg, fp_cache)
        if verdict == "identical":
            item["action"] = "identical"
            item["level"] = 3
            item["reason"] = "目标已存在且内容一致，可删除源文件（需 enable_delete=true 且源目录可写）"
        elif verdict == "conflict":
            item["action"] = "conflict"
            item["reason"] = f"目标同名冲突（{why}），保留双方不移动"
        elif verdict == "blocked":
            item["action"] = "blocked"
            item["reason"] = f"无法判断冲突：{why}"

    # 5b) 规划后结构感知裁决：rename 后落在规范位的副本为 keeper（BUG-1）。
    #     只处理本批确有 move/rename 触及的指纹组，存量重复不静默处理。
    plan.extend(_arbitrate_duplicates(plan, files_by_dir, fp_groups, video_exts))

    # 6) 写权限预检（事实判断已在步骤 5 完成，权限不短路事实）：
    #    - move / rename_dir / cleanup_dir：不可写 → 降级 skip；
    #    - identical / duplicate：保持事实 action，仅在 reason 标注删除受限。
    unwritable: set[str] = set()

    def _apply_preflight(items: list[dict]) -> None:
        for item in items:
            action = item["action"]
            if action not in ("move", "rename_dir", "identical", "duplicate", "cleanup_dir"):
                continue
            bad = sorted({str(d) for d in _write_points(item)
                          if not os.access(d, os.W_OK)})
            if not bad:
                continue
            unwritable.update(bad)
            if action in ("move", "rename_dir", "cleanup_dir"):
                item["action"] = "skip"
                item["level"] = 2
                item["reason"] = (
                    f"无写权限（{'、'.join(bad)} 不可写），已降级为不整理；"
                    f"{permission_hint_text(bad)}"
                )
            else:  # identical / duplicate：保留事实，标注删除受限
                item["reason"] = (item.get("reason") or "") + (
                    f"（源目录 {'、'.join(bad)} 不可写，删除需先使其可写：{permission_hint_text(bad)}）"
                )

    _apply_preflight(plan)

    # 常驻探活（v0.11.7 BUG-4）：即使本轮计划没有任何写动作（全部 skip），
    # root 与每个库根自身"若执行整理就必须可写"，不可写时 write_preflight
    # 必须如实报 ok=false——禁止用"空写动作集"推导出虚假的可写结论。
    for d in sorted({root, *library_roots}):
        try:
            if not os.access(d, os.W_OK):
                unwritable.add(str(d))
        except OSError:
            unwritable.add(str(d))

    # 7) 空目录清理预览（L3）：移动/删除后将变空的目录自底向上闭包求解。
    #    库根与分类层、rename_dir 子树、有文件迁入的目录永不清理；任何无法
    #    消失的现存子目录（花絮等）都会阻断其所有祖先（执行时再重查真·空）。
    vanish_files: set[Path] = set()
    incoming_dirs: set[Path] = set()
    for i in plan:
        if i["action"] == "move":
            for src_str, tgt_str in [(i["source"], i["target"])] + [
                (r["source"], r["target"]) for r in i.get("related", [])
            ]:
                vanish_files.add(Path(src_str))
                incoming_dirs.add(Path(tgt_str).parent)
        elif i["action"] in ("identical", "duplicate"):
            vanish_files.add(Path(i["source"]))
            for r in i.get("related", []):
                # NEW-15：伴生文件随 keeper 移动（或冗余删除）——均视为将消失，
                # 目标父目录为接收目录（影响空目录清理判定）
                vanish_files.add(Path(r["source"]))
                incoming_dirs.add(Path(r["target"]).parent)

    rename_subtrees = [Path(i["source"]) for i in plan if i["action"] == "rename_dir"]
    protected_dirs = set(library_roots)

    def _protected(d: Path) -> bool:
        if d in protected_dirs:
            return True
        if d in incoming_dirs or any(d in x.parents for x in incoming_dirs):
            return True
        return any(d == rs or rs in d.parents for rs in rename_subtrees)

    removed_dirs: set[Path] = set()
    candidates = {f.parent for f in vanish_files}
    changed = True
    while changed:
        changed = False
        for d in sorted(candidates - removed_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                d.relative_to(root)
            except ValueError:
                continue
            if _protected(d):
                continue
            if any(f not in vanish_files for f in files_by_dir.get(d, [])):
                continue  # 还有不移动/不删除的文件
            if any(s not in removed_dirs for s in subdirs_by_dir.get(d, set())):
                continue  # 还有现存子目录（花絮/其他季/未腾空目录）
            if any(h == d or d in h.parents for h in hidden_dirs):
                continue  # 内含隐藏目录（.deletedByTMM 等），物理上不会空
            removed_dirs.add(d)
            candidates.add(d.parent)
            changed = True

    # O-3：除"本轮被腾空"目录外，扫描库根内"预存空目录"（此前运行/外部工具留下的
    # 真空目录）也进入清理计划。判定：物理真空（无文件+无现存子目录+无隐藏目录）、
    # 非库根本身、不在 incoming_dirs/rename_subtrees 内、不在本轮 move/rename_dir
    # 目标祖先里。先把真空目录加入 removed_dirs，再走一次祖先冒泡闭包——父目录
    # 因子目录被清理也变空的（如 Inner/Deep.Empty → Inner）会一并加入。
    move_targets: set[Path] = set()
    for i in plan:
        if i["action"] == "move":
            move_targets.add(Path(i["target"]))
            for r in i.get("related", []):
                move_targets.add(Path(r["target"]))
    incoming_or_target_ancestors: set[Path] = set()
    for t in move_targets:
        incoming_or_target_ancestors.update(t.parents)
    rename_targets = {Path(i["target"]) for i in plan if i["action"] == "rename_dir"}

    # O-3 开关（v0.11.12）：target_subtree 时预存空目录只在"本轮操作目标子树"
    # 内收集——范围根 = 本轮被腾空目录（move/identical/duplicate 源父目录）∪
    # move 接收目录；library_root（默认）保持全库根扫描。
    scope_roots: set[Path] = set()
    if preexisting_empty_scope == "target_subtree":
        scope_roots = {f.parent for f in vanish_files} | incoming_dirs

    def _is_preexisting_empty(d: Path) -> bool:
        # 特殊目录（BDMV/花絮等）子树被扫描层跳过，files_by_dir 里查不到条目，
        # 直接以"物理真实"再确认一遍——避免误判特殊目录子树为真空。
        try:
            d.relative_to(root)
        except ValueError:
            return False
        rel_parts = d.relative_to(root).parts
        if _in_special_parts(rel_parts, org_cfg):
            return False
        if files_by_dir.get(d):
            return False
        if subdirs_by_dir.get(d):
            return False
        if any(h == d or d in h.parents for h in hidden_dirs):
            return False
        if d in protected_dirs or _protected(d):
            return False
        if d in incoming_or_target_ancestors or d in rename_targets:
            return False
        if scope_roots and not any(d == r or r in d.parents for r in scope_roots):
            return False  # target_subtree 范围外（本轮完全不触及的子树）
        # 最后一道防线：物理真实真空（防御性，正常情况前面已挡掉）
        try:
            if os.listdir(d):
                return False
        except OSError:
            return False
        return True

    preexisting_empties: set[Path] = set()
    for parent, kids in subdirs_by_dir.items():
        for d in kids:
            if d in removed_dirs:
                continue
            if _is_preexisting_empty(d):
                preexisting_empties.add(d)
    removed_dirs.update(preexisting_empties)

    # 预存空目录加入后再走一次祖先冒泡：父目录因"所有子目录都进了 removed_dirs"
    # 也变空的，需要一并清理（如 Pre.Empty.2022/Inner/Deep.Empty + Inner 都真空）。
    if preexisting_empties:
        candidates = {d.parent for d in preexisting_empties}
        changed = True
        while changed:
            changed = False
            for d in sorted(candidates - removed_dirs,
                            key=lambda p: len(p.parts), reverse=True):
                try:
                    d.relative_to(root)
                except ValueError:
                    continue
                if _protected(d):
                    continue
                if any(f not in vanish_files for f in files_by_dir.get(d, [])):
                    continue
                if any(s not in removed_dirs for s in subdirs_by_dir.get(d, set())):
                    continue
                if any(h == d or d in h.parents for h in hidden_dirs):
                    continue
                removed_dirs.add(d)
                candidates.add(d.parent)
                changed = True

    cleanup_preview = [
        _item("cleanup_dir", source=str(d), kind=_TV, level=3,
              reason=("预存空目录（非本轮操作产生），enable_delete=true 时可清理"
                      if d in preexisting_empties
                      else "该目录内文件将全部移出/删除，enable_delete=true 时可清理空目录"))
        for d in sorted(removed_dirs, key=lambda p: len(p.parts), reverse=True)
    ]
    plan.extend(cleanup_preview)
    _apply_preflight(cleanup_preview)  # 清理项自身也可能因父目录只读被降级

    counts: dict[str, int] = {}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    result = {
        "ok": True,
        "dry_run": True,
        "mode": mode,
        "preexisting_empty_scope": preexisting_empty_scope,
        "language_split": language_split,
        "path": str(root),
        "library_roots": [str(r) for r in library_roots],
        "counts": counts,
        "plan": plan,
        "write_preflight": {
            "ok": not unwritable,
            "status": "ok" if not unwritable else "not_writable",
            "unwritable_dirs": sorted(unwritable),
            **({"hint": permission_hint_text(unwritable)} if unwritable else {}),
        },
        "scan_truncated": bool(state.get("truncated")),
        "plan_truncated": plan_truncated,
        "errors": walk_sink.messages + errors,
        "errors_summary": walk_sink.summary(),
    }
    # T3：有权限错误时标注规划结果可信度
    return mark_incomplete_from_sink(result, walk_sink, empty=not plan)


def audit_log_path(cfg_org: OrganizerConfig, channel: str = "organize") -> Path:
    """审计文件按通道分流（OPEN-8）：organize → 配置的 audit_log_file；
    cleanup → 同目录 cleanup-audit.jsonl（两类操作不混同一文件）。"""
    if channel == "cleanup":
        return Path(cfg_org.audit_log_file).with_name("cleanup-audit.jsonl")
    return Path(cfg_org.audit_log_file)


def _audit(cfg_org: OrganizerConfig, *, action: str, source: str, target: str,
           result: str, error: str = "", operation_id: str = "",
           size: int | None = None, mtime: int | None = None,
           channel: str = "organize") -> None:
    """追加一条 JSONL 审计记录（§32/§34）；写失败只警告，绝不影响整理主流程。

    §34：所有写操作记录至少含 operation_id / source / target / status（result）/
    size（st_size）/ mtime（st_mtime_ns），供 media_rollback 按 size+mtime 快速校验。
    channel 标识写入的审计通道（organize/cleanup），记录内同步携带。
    """
    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "action": action,
        "source": source,
        "target": target,
        "result": result,
        "error": error,
        "channel": channel,
    }
    if operation_id:
        record["operation_id"] = operation_id
    if size is not None:
        record["size"] = size
    if mtime is not None:
        record["mtime"] = mtime  # §34：mtime = st_mtime_ns
    try:
        log_path = audit_log_path(cfg_org, channel)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("审计日志写入失败: %s", exc)


def read_audit_records(cfg_org: OrganizerConfig, channel: str = "organize") -> list[dict]:
    """读取指定通道审计日志全部记录（JSONL，坏行跳过）；文件不存在返回空列表。"""
    log_path = audit_log_path(cfg_org, channel)
    if not log_path.is_file():
        return []
    records: list[dict] = []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


# operation_id 字符集去掉易混的 I/O/0/1，人工核对不易看错（§46）
_OPID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_operation_id(cfg_org: OrganizerConfig) -> str:
    """生成 OP-YYYYMMDD-XXXXX 格式的密码学安全唯一操作 ID（§46）。

    与现有审计记录中的 operation_id 冲突时重新生成；所有写操作共用同一命名空间。
    """
    # §46 共用命名空间：审计文件虽按通道分流（OPEN-8），operation_id 不分家——
    # 两个通道的已用 ID 都计入，避免 cleanup 与 organize 生成重复 OP 号。
    taken = {
        r.get("operation_id")
        for channel in ("organize", "cleanup")
        for r in read_audit_records(cfg_org, channel)
        if r.get("operation_id")
    }
    day = datetime.now().strftime("%Y%m%d")
    for _ in range(64):
        suffix = "".join(secrets.choice(_OPID_ALPHABET) for _ in range(5))
        candidate = f"OP-{day}-{suffix}"
        if candidate not in taken:
            return candidate
    raise RuntimeError("operation_id 生成冲突重试耗尽（极不可能）")


def _move_file(src: Path, dst: Path) -> None:
    """移动单个文件：优先同卷原子 rename，失败（跨卷等）回退 shutil.move。"""
    ensure_dir(dst.parent)
    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))


def execute_plan(
    guard: PathGuard,
    plan_result: dict,
    org_cfg: OrganizerConfig,
    *,
    enable_organize: bool,
    enable_delete: bool,
    operation_id: str = "",
) -> dict:
    """执行 plan_session 生成的计划（L2/L3 写操作）。

    门控在函数内（与 subhd.download_subtitle 同范式）：执行移动需 enable_organize；
    identical 源删除与空目录清理需 enable_delete，未开启时报告 level3_pending。
    每个动作写审计日志（含 operation_id/size/mtime，§32/§34，供 media_rollback
    按 ID 回滚）；单文件失败计入 errors 并继续。operation_id 缺省时自动生成，
    成功执行的每个动作都带同一个 ID（共享命名空间，§46）。
    """
    if not enable_organize:
        raise InvalidArguments("整理执行未启用（enable_organize=false）；可先 dry_run=true 审阅计划")
    if not plan_result.get("ok") or not isinstance(plan_result.get("plan"), list):
        raise InvalidArguments("无效的整理计划：请先调用 organize_media(dry_run=true) 生成")

    if not operation_id:
        operation_id = new_operation_id(org_cfg)

    def _log(action: str, source: str, target: str, result: str, error: str = "",
             size: int | None = None, mtime: int | None = None) -> None:
        _audit(org_cfg, action=action, source=source, target=target, result=result,
               error=error, operation_id=operation_id, size=size, mtime=mtime)

    def _stat_pair(p: Path) -> tuple[int | None, int | None]:
        try:
            st = p.stat()
            return st.st_size, st.st_mtime_ns
        except OSError:
            return None, None

    # 执行前总预检（BUG-3）：对计划内所有写入点重新 os.access（dry_run 与执行之间
    # 权限可能已变化），任一不可写就整体拒绝、不动任何文件，避免半途失败。
    bad_dirs = sorted({
        str(d)
        for item in plan_result["plan"]
        if item.get("action") in ("move", "rename_dir", "identical", "duplicate", "cleanup_dir")
        for d in _write_points(item)
        if not os.access(d, os.W_OK)
    })
    if bad_dirs:
        raise InvalidArguments(
            "执行前写权限预检失败，已整体拒绝（未改动任何文件）。不可写目录："
            + "、".join(bad_dirs)
            + f"。{permission_hint_text(bad_dirs)}"
        )

    executed = {"moved": 0, "failed": 0, "renamed_dirs": 0,
                "cleaned_dirs": 0, "deleted_files": 0, "level3_pending": 0,
                "blocked": 0}
    errors: list[str] = []
    plan_items = plan_result.get("plan", [])
    for item in plan_items:
        item["executed"] = False

    actionable_total = sum(
        1 for item in plan_items
        if item.get("action") in ("move", "rename_dir", "identical", "duplicate", "cleanup_dir")
    )
    canceled = False
    done = 0
    report("execute", "执行整理计划", done=0, total=actionable_total)

    def _fail(item: dict, action: str, source: str, target: str, exc: Exception) -> None:
        executed["failed"] += 1
        errors.append(f"{source}: {exc}")
        item["executed"] = False
        _log(action=(action or "").upper(), source=source,
             target=target, result="FAILED", error=str(exc))

    def _mkdir_logged(target_dir: Path) -> None:
        """创建目标侧目录并逐级审计（#9）。

        organize 会为目标侧新建 Season XX 等目录；不审计的话回滚后这些目录
        只剩空壳（源侧 RMDIR_EMPTY 有逆操作，目标侧 mkdir 没有）。逐级记录
        实际新建的目录（仅原本不存在的），media_rollback 按 MKDIR 逆向删除
        回滚后为空的壳。
        """
        missing: list[Path] = []
        p = target_dir
        while not p.exists():
            missing.append(p)
            if p.parent == p:
                break
            p = p.parent
        if not missing:
            return
        # fnOS 新建目录是 0505（无写位），必须 chmod 0755 才能继续往里移动
        ensure_dir(target_dir)
        for d in reversed(missing):  # 自顶向下记录，与物理创建顺序一致
            _log(action="MKDIR", source="", target=str(d), result="SUCCESS")

    # 阶段 1（L3）：先删冗余副本——被 rename_dir 整目录带走的冗余文件必须在
    # 目录改名前按原路径删除；identical 源删除同理。enable_delete=false → pending。
    # NEW-15：冗余副本的伴生字幕不再在此连坐删除——计划承诺"随 keeper 移动"，
    # 由阶段 2b（改名后）执行，计划与执行语义一致。
    for item in plan_items:
        if item.get("action") not in ("identical", "duplicate"):
            continue
        if canceled or check_canceled():
            canceled = True
            break
        source, target = str(item.get("source") or ""), str(item.get("target") or "")
        report("execute", "执行整理计划", done=done, total=actionable_total,
               current=Path(source).name if source else None)
        done += 1
        try:
            src = guard.check(source)
            if enable_delete:
                # v0.11.9 NEW-12 守卫：删除冗余副本前必须确认 target 是另一个
                # 真实存在的 keeper。自指（source==target，含硬链接/路径差异）
                # 或 keeper 缺失 → blocked 拦截，绝不删除（数据丢失最后防线）。
                tgt = guard.check(target) if target else None
                same_path = tgt is not None and (
                    str(src) == str(tgt)
                    or (src.is_file() and tgt.is_file()
                        and os.path.samefile(src, tgt))
                )
                if same_path:
                    why = "source_equals_target"
                elif tgt is None or not tgt.is_file():
                    why = "keeper_target_missing"
                else:
                    why = ""
                if why:
                    executed["blocked"] += 1
                    errors.append(
                        f"{source}: 删除冗余被拦截（{why}），请重新 dry_run 生成计划"
                    )
                    _log(action="DELETE_DUPLICATE", source=source,
                           target=target, result="BLOCKED", error=why)
                    continue
                if src.is_file():
                    sz, mt = _stat_pair(src)
                    os.remove(src)
                    executed["deleted_files"] += 1
                    item["executed"] = True
                    _log(action="DELETE_DUPLICATE", source=source,
                         target=target, result="SUCCESS", size=sz, mtime=mt)
            else:
                executed["level3_pending"] += 1
                _log(action="DELETE_DUPLICATE", source=source,
                       target=target, result="PENDING")
        except (OSError, AccessDenied) as exc:
            _fail(item, item["action"], source, target, exc)

    # 阶段 2（L2）：剧集目录改名（rename_dir）。目标已存在（计划过期）→ 拒绝并跳过，
    # 绝不覆盖；要求源仍是目录。
    for item in plan_items:
        if item.get("action") != "rename_dir":
            continue
        if canceled or check_canceled():
            canceled = True
            break
        source, target = str(item.get("source") or ""), str(item.get("target") or "")
        report("execute", "执行整理计划", done=done, total=actionable_total,
               current=Path(source).name if source else None)
        done += 1
        try:
            src, dst = guard.check(source), guard.check(target)
            if dst.exists():
                errors.append(f"目标目录已存在，跳过改名（请重新 dry_run）: {target}")
                _log(action="RENAME_DIR", source=source, target=target,
                       result="SKIPPED", error="target_exists")
                continue
            if not src.is_dir():
                errors.append(f"源目录已不存在，跳过改名: {source}")
                _log(action="RENAME_DIR", source=source, target=target,
                       result="SKIPPED", error="source_missing")
                continue
            _mkdir_logged(dst.parent)
            sz, mt = _stat_pair(src)
            os.rename(src, dst)  # 同卷原子改名；跨卷会抛 OSError（同库根内不会跨卷）
            executed["renamed_dirs"] += 1
            item["executed"] = True
            _log(action="RENAME_DIR", source=source, target=target,
                 result="SUCCESS", size=sz, mtime=mt)
        except (OSError, AccessDenied) as exc:
            _fail(item, "rename_dir", source, target, exc)

    # 阶段 2b（L3，NEW-15/16/17）：冗余副本的伴生文件随 keeper 走——仅在对应
    # duplicate/identical 项的视频删除成功（executed=True，即通过 NEW-12 守卫
    # 且未失败）后执行。放在改名之后：keeper 可能随 rename_dir 改变位置。
    # - 目标空闲 → 移动到 keeper 侧（与计划端 related 承诺一致）；
    # - keeper 已有同名伴生 → 冗余删除（Sonarr ExtraFileExtensions 语义），
    #   NEW-17：计入 deleted_files，数据丢失动作必须在统计中可见。
    for item in plan_items:
        if item.get("action") not in ("identical", "duplicate"):
            continue
        if not item.get("related") or item.get("executed") is not True:
            continue
        if canceled or check_canceled():
            canceled = True
            break
        source, target = str(item.get("source") or ""), str(item.get("target") or "")
        for rel in item.get("related", []):
            try:
                r_src = guard.check(rel["source"])
                if not r_src.is_file():
                    continue
                r_dst = guard.check(rel["target"])
                if (str(r_src) == str(r_dst)
                        or (r_dst.is_file()
                            and os.path.samefile(r_src, r_dst))):
                    _log(action="MOVE_RELATED",
                           source=rel["source"], target=rel["target"],
                           result="SKIPPED", error="same_path")
                    continue
                if r_dst.exists():
                    # keeper 侧已有同名伴生：本冗余伴生为重复字幕/附件，删除
                    sz, mt = _stat_pair(r_src)
                    os.remove(r_src)
                    executed["deleted_files"] += 1
                    _log(action="DELETE_RELATED",
                         source=rel["source"], target=rel["target"],
                         result="SUCCESS", size=sz, mtime=mt)
                else:
                    sz, mt = _stat_pair(r_src)
                    _mkdir_logged(r_dst.parent)
                    _move_file(r_src, r_dst)
                    _log(action="MOVE_RELATED",
                         source=rel["source"], target=rel["target"],
                         result="SUCCESS", size=sz, mtime=mt)
            except (OSError, AccessDenied) as exc:
                executed["failed"] += 1
                errors.append(f"{rel['source']}: 伴生文件处理失败: {exc}")
                _log(action="MOVE_RELATED", source=rel["source"],
                       target=rel["target"], result="FAILED", error=str(exc))

    # 阶段 3（L2）：文件移动（dst.exists() 拒覆盖；关联文件跟随）
    for item in plan_items:
        if item.get("action") != "move":
            continue
        if canceled or check_canceled():
            canceled = True
            break
        source, target = str(item.get("source") or ""), str(item.get("target") or "")
        report("execute", "执行整理计划", done=done, total=actionable_total,
               current=Path(source).name if source else None)
        done += 1
        try:
            src = guard.check(source)
            dst = guard.check(target)
            if dst.exists():
                # 计划生成后目标才出现（或计划已过期）：绝不覆盖，整项跳过
                errors.append(f"目标已存在，跳过: {target}")
                _log(action="MOVE", source=source, target=target,
                       result="SKIPPED", error="target_exists")
                continue
            if src.is_file():
                sz, mt = _stat_pair(src)
                _mkdir_logged(dst.parent)
                _move_file(src, dst)
                executed["moved"] += 1
                item["executed"] = True
                _log(action="MOVE", source=source, target=target, result="SUCCESS",
                     size=sz, mtime=mt)
            for rel in item.get("related", []):
                r_src = guard.check(rel["source"])
                r_dst = guard.check(rel["target"])
                if not r_src.is_file():
                    continue
                if r_dst.exists():
                    errors.append(f"关联文件目标已存在，跳过: {rel['target']}")
                    _log(action="MOVE_RELATED", source=rel["source"],
                         target=rel["target"], result="SKIPPED", error="target_exists")
                    continue
                sz, mt = _stat_pair(r_src)
                _mkdir_logged(r_dst.parent)
                _move_file(r_src, r_dst)
                _log(action="MOVE_RELATED",
                     source=rel["source"], target=rel["target"],
                     result="SUCCESS", size=sz, mtime=mt)
        except (OSError, AccessDenied) as exc:
            _fail(item, "move", source, target, exc)

    # 阶段 4（L3）：空目录清理（计划已按自底向上排序；执行前再重查真·空，绝不递归）
    for item in plan_items:
        if item.get("action") != "cleanup_dir":
            continue
        if canceled or check_canceled():
            canceled = True
            break
        source = str(item.get("source") or "")
        report("execute", "执行整理计划", done=done, total=actionable_total,
               current=Path(source).name if source else None)
        done += 1
        try:
            d = guard.check(source)
            if enable_delete:
                if d.is_dir() and os.listdir(d) == []:
                    sz, mt = _stat_pair(d)
                    os.rmdir(d)
                    executed["cleaned_dirs"] += 1
                    item["executed"] = True
                    _log(action="RMDIR_EMPTY", source=source,
                         target="", result="SUCCESS", size=sz, mtime=mt)
                elif d.is_dir():
                    executed["level3_pending"] += 1
                    _log(action="RMDIR_EMPTY", source=source,
                           target="", result="SKIPPED", error="dir_not_empty")
            else:
                executed["level3_pending"] += 1
                _log(action="RMDIR_EMPTY", source=source,
                       target="", result="PENDING")
        except (OSError, AccessDenied) as exc:
            _fail(item, "cleanup_dir", source, "", exc)
            # skip / conflict / blocked 不执行，保持 executed=False

    result = dict(plan_result)
    result["dry_run"] = False
    result["operation_id"] = operation_id  # §32：执行必返回操作 ID（审计与回滚凭据）
    result["executed"] = executed
    result["errors"] = (plan_result.get("errors") or []) + errors
    counts = dict(result.get("counts") or {})
    # O-1：counts.executed 必须含 L2/L3 全部已执行动作（moved/renamed/cleaned/deleted），
    # 不能只看 moved——否则一轮"只清了 2 个空目录+删了 1 个冗余"会汇报成"executed=0"。
    counts["executed"] = (
        executed["moved"] + executed["renamed_dirs"]
        + executed["cleaned_dirs"] + executed["deleted_files"]
    )
    result["counts"] = counts
    logger.info("整理执行完成 moved=%s renamed=%s failed=%s cleaned=%s deleted=%s pending=%s",
                executed["moved"], executed["renamed_dirs"], executed["failed"],
                executed["cleaned_dirs"], executed["deleted_files"],
                executed["level3_pending"])
    if canceled:
        report("execute", "已取消，保留已完成部分", done=done, total=actionable_total)
        return mark_partial(result)
    report("execute", "执行整理计划", done=done, total=actionable_total)
    return result


def find_media(
    guard: PathGuard,
    limits: Limits,
    video_exts: frozenset[str],
    subtitle_exts: frozenset[str],
    media_roots: tuple[str, ...],
    *,
    title: str,
    season: int = 0,
    episode: int = 0,
) -> dict:
    """按片名（+可选季/集）在 media_roots 中定位视频（只读）。

    title 做归一化后的包含匹配（文件 stem 与解析出的片名都参与）；
    season/episode > 0 时精确过滤（文件名或父目录名推断）。
    """
    needle = _norm_name(title)
    if not needle:
        raise InvalidArguments("title 不能为空")
    if season < 0 or episode < 0:
        raise InvalidArguments("season/episode 不能为负数")

    state: dict = {}
    # 遍历错误走 ErrorSink：固有不可读目录首次上报，之后只计数（#5）
    walk_sink = ErrorSink(limits.max_errors_reported)

    def on_error(p: Path, msg: str) -> None:
        walk_sink.add(p, msg)

    matches: list[dict] = []
    for root_str in expand_roots(media_roots):
        root = Path(root_str)
        for fpath, st in iter_tree(
            root, guard,
            max_files=limits.max_scan_files,
            max_depth=limits.max_list_depth,
            include_dirs=False,
            on_error=on_error,
            state=state,
        ):
            if not stat_module.S_ISREG(st.st_mode):
                continue
            if fpath.suffix.lstrip(".").lower() not in video_exts:
                continue
            parsed = parse_video_filename(fpath.name)
            pair = _best_episode(fpath)
            if season > 0 and (pair[0] if pair else parsed.season) not in (None, season):
                continue
            if episode > 0 and (pair[1] if pair else parsed.episode) not in (None, episode):
                continue
            if (needle not in _norm_name(fpath.stem)
                    and needle not in _norm_name(parsed.title or "")):
                continue
            matches.append({
                "name": fpath.name,
                "path": str(fpath),
                "relative_path": fpath.relative_to(root).as_posix(),
                "size_bytes": st.st_size,
                "parsed": {
                    "title": parsed.title,
                    "year": parsed.year,
                    "season": parsed.season,
                    "episode": parsed.episode,
                    "resolution": parsed.resolution,
                },
                "external_subtitles": [
                    s["name"] for s in find_external_subtitles(fpath, subtitle_exts)
                ],
            })
            if len(matches) >= limits.max_search_results:
                state["truncated"] = True
                break
        if state.get("truncated"):
            break

    result = {
        "ok": True,
        "query": {"title": title, "season": season, "episode": episode},
        "returned": len(matches),
        "matches": matches,
        "truncated": bool(state.get("truncated")),
        "errors": walk_sink.messages,
        "errors_summary": walk_sink.summary(),
    }
    # T3：空结果 + 权限错误 → result_incomplete，别让调用方读成"库里没有"
    return mark_incomplete_from_sink(result, walk_sink, empty=not matches)
