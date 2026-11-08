"""配置加载：路径、扩展名、限额等易变内容一律来自 YAML 配置文件，代码不写死。

安全底线：没有合法的 allowed_paths 白名单时拒绝启动，绝不默认放开权限。
"""

from __future__ import annotations

import contextvars
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .security import is_pattern, is_within, expand_roots


MAX_CONFIG_ISSUES = 20

# 顶层合法键：未知键（拼写错误/已废弃）会被静默忽略，写错 enable_write 之类的
# 开关名会导致行为与预期不符，因此按收集式校验上报，和其它问题一起返回。
KNOWN_TOP_LEVEL_KEYS = frozenset(
    {
        "allowed_paths",
        "exclude_paths",
        "media_roots",
        "video_extensions",
        "subtitle_extensions",
        "limits",
        "media",
        "cleanup",
        "organizer",
        "subhd",
        "opensubtitles",
        "shooter",
        "subtitle",
        "jackett",
        "tmdb",
        "tvmaze",
        "seedhub",
        "dysbk",
        "media_search",
        "logging",
        "enable_write",
        "enable_organize",
        "enable_delete",
    }
)

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|password|passwd|secret|cookie|authorization)", re.I
)
# 从旧式错误消息里提取「a.b.c」形式的键路径（用于无 issues 的错误降级定位）
_KEY_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]*)+")
_UNSET = object()


@dataclass(frozen=True)
class ConfigIssue:
    """一条配置问题：定位到键与行号，并给出期望值/当前值。

    severity: error（阻断加载）/ warning（可加载但需关注）。
    敏感键（api_key/token/password 等）的 actual 一律为 ***。
    """

    key: str
    message: str
    section: str = ""
    expected: str = ""
    actual: str = ""
    line: int | None = None
    column: int | None = None
    severity: str = "error"
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "section": self.section,
            "expected": self.expected,
            "actual": self.actual,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
            "hint": self.hint,
            "message": self.message,
        }

    def render(self) -> str:
        where = f"第{self.line}行 " if self.line else ""
        text = f"{where}{self.key}: {self.message}"
        if self.hint:
            text += f"（建议: {self.hint}）"
        return text


class ConfigError(Exception):
    """配置缺失或非法。

    issues 为空表示旧式单条错误（message 即全部信息）；收集式校验会带上
    结构化 issues（键/行号/期望值/当前值），由装配层映射为 config_invalid。
    missing=True 仅用于「配置文件不存在」，装配层据此返回 not_configured。
    """

    def __init__(
        self,
        message: str,
        *,
        issues: list[ConfigIssue] | None = None,
        file: str = "",
        missing: bool = False,
    ) -> None:
        super().__init__(message)
        self.issues = list(issues or [])
        self.file = file
        self.missing = missing


class _SourceIndex:
    """从 YAML 文本建立「点分键路径 → (行, 列)」索引（1 基），用于错误定位。"""

    def __init__(self, text: str) -> None:
        self._marks: dict[str, tuple[int, int]] = {}
        try:
            root = yaml.compose(text)
        except yaml.YAMLError:
            root = None
        if root is not None:
            self._walk(root, "")

    def _walk(self, node: Any, prefix: str) -> None:
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                key = str(getattr(key_node, "value", ""))
                path = f"{prefix}.{key}" if prefix else key
                self._marks.setdefault(
                    path, (value_node.start_mark.line + 1, value_node.start_mark.column + 1)
                )
                self._walk(value_node, path)
        elif isinstance(node, yaml.SequenceNode):
            for position, item in enumerate(node.value):
                path = f"{prefix}[{position}]"
                self._marks.setdefault(
                    path, (item.start_mark.line + 1, item.start_mark.column + 1)
                )
                self._walk(item, path)
        elif isinstance(node, yaml.ScalarNode) and prefix:
            self._marks.setdefault(
                prefix, (node.start_mark.line + 1, node.start_mark.column + 1)
            )

    def locate(self, key: str) -> tuple[int | None, int | None]:
        """按键路径查行号；查不到时逐级回退到父节点（如 allowed_paths[3] → allowed_paths）。"""
        if key in self._marks:
            return self._marks[key]
        parts = [re.sub(r"\[\d+\]$", "", part) for part in key.split(".")]
        for cut in range(len(parts) - 1, 0, -1):
            parent = ".".join(parts[:cut])
            if parent in self._marks:
                return self._marks[parent]
        return None, None


_INDEX: contextvars.ContextVar[_SourceIndex | None] = contextvars.ContextVar(
    "nas_config_index", default=None
)
_CONFIG_FILE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nas_config_file", default=""
)
_COLLECTOR: contextvars.ContextVar[list[ConfigIssue] | None] = contextvars.ContextVar(
    "nas_config_issues", default=None
)


def _redact_actual(key: str, value: Any) -> str:
    """敏感键的当前值一律替换为 ***（错误信息/日志绝不泄露密钥）。"""
    if _SECRET_KEY_RE.search(key):
        return "***"
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _issue_for(
    key: str,
    message: str,
    *,
    expected: str = "",
    actual: Any = _UNSET,
    hint: str = "",
    severity: str = "error",
) -> ConfigIssue:
    index = _INDEX.get()
    line, column = index.locate(key) if index is not None else (None, None)
    return ConfigIssue(
        key=key,
        message=message,
        section=key.split(".")[0].split("[")[0],
        expected=expected,
        actual="" if actual is _UNSET else _redact_actual(key, actual),
        line=line,
        column=column,
        severity=severity,
        hint=hint,
    )


def _fail(
    key: str,
    message: str,
    *,
    expected: str = "",
    actual: Any = _UNSET,
    hint: str = "",
    severity: str = "error",
) -> None:
    """抛出带键/行号/期望值/当前值的配置错误（由所在 section 收集）。"""
    issue = _issue_for(
        key, message, expected=expected, actual=actual, hint=hint, severity=severity
    )
    raise ConfigError(message, issues=[issue], file=_CONFIG_FILE.get())


def _issue_from_error(exc: ConfigError, label: str) -> ConfigIssue:
    """把没有结构化 issues 的旧式错误降级为 issue（键取消息里的点分路径）。"""
    message = str(exc)
    match = _KEY_PATH_RE.search(message)
    key = match.group(0) if match else label
    if _SECRET_KEY_RE.search(key):
        message = "配置项取值不合法（敏感值已隐藏）"
    return _issue_for(key, message)


def _try(label: str, fn, fallback: Any) -> Any:
    """执行一个校验片段：收集错误并返回默认值；无收集上下文时保持 fail-fast。"""
    try:
        return fn()
    except ConfigError as exc:
        collector = _COLLECTOR.get()
        if collector is None:
            raise
        for issue in (exc.issues or [_issue_from_error(exc, label)]):
            if len(collector) < MAX_CONFIG_ISSUES:
                collector.append(issue)
        return fallback


DEFAULT_VIDEO_EXTENSIONS = ("mkv", "mp4", "avi", "mov", "ts", "m2ts", "webm", "wmv", "flv")
DEFAULT_SUBTITLE_EXTENSIONS = ("srt", "ass", "ssa", "sub", "vtt", "sup")

# 文件名跳过默认模式：家庭录像（手机/相机/无人机直出命名）与成人内容常见标记。
# 对文件名 stem 大小写不敏感匹配；配置 media.skip_filename_patterns 可整体覆盖
# （显式空列表 = 关闭过滤）。番号通式等易误杀的模式默认不收录，用户可按需添加。
DEFAULT_SKIP_PATTERNS = (
    # 拍摄设备直出命名（几乎零误杀）
    r"^IMG_",                    # iPhone / 佳能
    r"^VID[_ ]",                 # Android
    r"^DJI_",                    # 大疆
    r"^PXL_",                    # Google Pixel
    r"^MVI_", r"^MOV_",          # 佳能/尼康相机
    r"^\d{8}[-_]\d{6}",          # 20230101_123456
    r"^\d{4}-\d{2}-\d{2}[-_T]",  # 2023-01-01_123456
    r"^\d{13}$",                 # 毫秒时间戳
    # 成人内容常见发行标记（低误杀；不收录宽泛番号通式）
    r"\bfc2\b",
    r"\b1pondo\b", r"\bcaribbeancom\b", r"\bheydouga\b", r"\b10musume\b",
    r"\btokyo[ -]?hot\b", r"\bbrazzers\b", r"\bbangbros\b",
    r"无码|無碼",
)

# v1.7.2 §十八：默认模式 → 分类。camera_personal_video 命中 → personal_video；
# protected_adult 等保护类命中 → 保留 unknown + protection_reason（绝不自动升为
# personal_video）。旧配置（纯字符串条目）category 视为 protected（按 unknown 保护）。
DEFAULT_SKIP_PATTERN_CATEGORIES: dict[str, str] = {
    r"^IMG_": "camera_personal_video",
    r"^VID[_ ]": "camera_personal_video",
    r"^DJI_": "camera_personal_video",
    r"^PXL_": "camera_personal_video",
    r"^MVI_": "camera_personal_video",
    r"^MOV_": "camera_personal_video",
    r"^\d{8}[-_]\d{6}": "camera_personal_video",
    r"^\d{4}-\d{2}-\d{2}[-_T]": "camera_personal_video",
    r"^\d{13}$": "camera_personal_video",
    r"\bfc2\b": "protected_adult",
    r"\b1pondo\b": "protected_adult",
    r"\bcaribbeancom\b": "protected_adult",
    r"\bheydouga\b": "protected_adult",
    r"\b10musume\b": "protected_adult",
    r"\btokyo[ -]?hot\b": "protected_adult",
    r"\bbrazzers\b": "protected_adult",
    r"\bbangbros\b": "protected_adult",
    r"无码|無碼": "protected_adult",
}


@dataclass(frozen=True)
class Limits:
    max_list_entries: int = 500
    max_search_results: int = 100
    max_scan_files: int = 100_000
    max_list_depth: int = 10
    max_errors_reported: int = 20


@dataclass(frozen=True)
class MediaConfig:
    """媒体分析（ffprobe / ffmpeg）相关配置。"""

    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: float = 20.0
    ffmpeg_path: str = "ffmpeg"
    ffmpeg_timeout_seconds: float = 300.0
    max_probe_files: int = 200
    max_classify_batch: int = 20   # nas_classify_region 单次批量上限（§18）
    skip_filename_patterns: tuple[str, ...] = ()  # 文件名跳过模式（默认集见 DEFAULT_SKIP_PATTERNS）
    # v1.7.2 §十八：(pattern, category) 对，category ∈ camera_personal_video /
    # protected_adult / ""（按 protected 处理）。供 media_cleanup 分类与保护判断。
    skip_pattern_categories: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class OrganizerConfig:
    """媒体目录整理（M9：organize_media）相关配置。

    max_move_files 限制单次会话的计划/执行移动文件数，防止大库一次性大量移动；
    special_dir_names 命中的子树（BDMV/VIDEO_TS 等）禁止整理；follow_extensions
    是除字幕外允许随视频一起移动的关联文件扩展名；audit_log_file 记录每次
    执行的 MOVE/DELETE 审计（JSONL，开发文档 3.0 §32）。
    """

    max_move_files: int = 200
    # BDMV/VIDEO_TS 等原盘结构 + 常见花絮目录：命中后子树不整理；含这类子目录
    # 的整季目录仍可归季，花絮目录原地保留（organizer._detect_child）
    special_dir_names: tuple[str, ...] = (
        "bdmv", "video_ts", "certificate", "certificates",
        "featurettes", "extras", "behind the scenes", "deleted scenes",
        "interviews", "shorts", "samples", "sample", "trailers", "trailer", "other",
    )
    follow_extensions: tuple[str, ...] = ("nfo", "jpg", "jpeg", "png")
    audit_log_file: str = "logs/organize-audit.jsonl"


@dataclass(frozen=True)
class CleanupConfig:
    """媒体清理（media_cleanup，v1.7.2 阶段二）配置（§22/§25）。

    junk_extensions 是媒体管理垃圾扩展名（.part/.aria2/.tmp 等），命中且通过
    五重门方可删除；代码中不得硬编码第二套配置。active_download_grace_minutes
    为活跃下载宽限：mtime 在窗口内 → cleanup_allowed=false（防止删掉正在下载的
    文件）。空目录仍由 organize_media 负责；Samples 等由 special_dir_names 保护。
    """

    junk_extensions: tuple[str, ...] = (".part", ".partial", ".aria2", ".tmp")
    active_download_grace_minutes: int = 60


@dataclass(frozen=True)
class LoggingConfig:
    file: str = "logs/nas-mcp.log"
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3


@dataclass(frozen=True)
class SubhdConfig:
    """SubHD 字幕搜索/下载（M4）相关配置。

    enabled 为总开关：false 时搜索/下载工具直接拒绝，不产生任何网络请求。
    base_url 默认指向官方站，测试/镜像时可改为本地桩地址。
    """

    enabled: bool = False
    base_url: str = "https://subhd.tv"
    # 读超时（下载大包用）；连接超时单独设置——libcurl 的连接超时涵盖 TCP 建连
    # 与 TLS 握手，跨境链路握手停顿不会再吃掉一整个读超时。
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 8.0
    max_retries: int = 2            # 失败后自动重试次数（0 关闭；超时/连接错误/5xx 才重试）
    max_results: int = 20
    max_download_bytes: int = 20 * 1024 * 1024
    min_request_interval_seconds: float = 1.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    filename_template: str = "{stem}.{lang}.{ext}"


@dataclass(frozen=True)
class OpensubtitlesConfig:
    """OpenSubtitles 字幕搜索/下载配置。

    官方 REST API（api.opensubtitles.com）。
    enabled=false 时搜索/下载工具直接跳过该站点。
    """
    enabled: bool = False
    base_url: str = "https://api.opensubtitles.com"
    api_key: str = ""
    api_key_env: str = "OPENSUBTITLES_API_KEY"
    username: str = ""
    password: str = ""
    language: str = "zh-cn"
    timeout_seconds: float = 30.0
    max_retries: int = 1
    min_request_interval_seconds: float = 1.0


@dataclass(frozen=True)
class ShooterConfig:
    """射手网（伪，assrt.net）字幕搜索/下载配置。

    API 仅需 token。
    enabled=false 时搜索/下载工具直接跳过该站点。
    """
    enabled: bool = False
    base_url: str = "https://api.assrt.net"
    token: str = ""
    token_env: str = "ASSRT_TOKEN"
    timeout_seconds: float = 30.0
    max_retries: int = 1
    min_request_interval_seconds: float = 1.0


@dataclass(frozen=True)
class SubtitlesConfig:
    """多站点字幕编排配置（subtitle_search / subtitle_download）。"""
    site_order: tuple[str, ...] = ("subhd", "opensubtitles", "shooter")
    switch_min_score: float = 0.9
    # 批处理策略（media_fill_subtitles）：季内缺失数 ≥ max(本值, 50%×季总集数)
    # 才走整季包路径；缺 1 集恒单集（不变量），配额紧张时可调高。
    season_pack_threshold: int = 3


# T4：聚合端点慢时的并行降级源（torznab 单 indexer，通常响应快）
DEFAULT_FAST_INDEXERS: tuple[str, ...] = (
    "thepiratebay", "knaben", "limetorrents", "torrentscsv", "yts",
    "52bt", "bigfangroup", "torrent9", "wolfmax4k", "linuxtracker",
)


@dataclass(frozen=True)
class JackettConfig:
    """Jackett 资源搜索（P4：jackett_search）配置。

    enabled 为总开关：false 时搜索工具直接拒绝，不产生任何网络请求。
    api_key 支持两种方式：api_key_env 显式指定环境变量，或 api_key 字面量
    中写 ${JACKETT_API_KEY} 由加载器做环境变量替换（两者均避免 key 入 Git）。
    api_key 绝不进入日志（§42）。
    url 同样支持两种方式：url_env 环境变量（如 JACKETT_URL）或 url 字面量
    中写 ${JACKETT_URL}——方便在 Octop 连接器创建时连同 key 一起在 env 里填写。
    """

    enabled: bool = False
    url: str = "http://127.0.0.1:9117"
    url_env: str = "JACKETT_URL"
    api_key: str = ""
    api_key_env: str = "JACKETT_API_KEY"
    timeout_seconds: float = 90.0
    # 单次 search() 的总时间预算（#3）：逐关键词循环与重试都在此 deadline 内收口，
    # 保证超时以结构化错误/部分结果在插件内返回，而不是被上层 harness 掐断。
    deadline_seconds: float = 50.0
    max_retries: int = 1            # 失败后自动重试次数（0 关闭；超时/连接错误/5xx 才重试）
    max_results: int = 100
    max_queries: int = 4          # 单次搜索最多生成的关键词组合数（§27）
    min_request_interval_seconds: float = 0.5
    # T4：聚合端点（indexers/all）被慢源拖到超时且 0 结果时，
    # 自动降级为 fast_indexers 的并行单源查询（torznab 单 indexer 端点）。
    parallel_fallback: bool = True
    fast_indexers: tuple[str, ...] = DEFAULT_FAST_INDEXERS
    # T5：结果排序策略。seeders=做种数降序（默认，行为不变）；
    # quality=质量分降序→seeders→title；hybrid=0.8*质量分+20*min(log2(1+做种数),10)
    sort: str = "seeders"
    # T6：magnet 缺失时跟进 torrent_url 的 302 跳转提取 infohash 补磁力
    # （仅对 {url}/dl/ 链接发请求，SSRF 收敛）；follow_timeout 为单条跟进超时
    follow_magnet_redirects: bool = True
    follow_timeout: float = 3.0


@dataclass(frozen=True)
class TmdbConfig:
    """TMDB 影视识别（P3：resolve_media）配置。"""

    enabled: bool = False
    api_key: str = ""
    api_key_env: str = "TMDB_API_KEY"
    timeout_seconds: float = 15.0
    base_url: str = "https://api.themoviedb.org/3"
    language: str = "zh-CN"


@dataclass(frozen=True)
class TvmazeConfig:
    """TVmaze 影视识别回落源配置（TMDB 不可达/查不到时启用）。

    TVmaze 免 API key，`/shows/{id}/episodes` 提供季+集号级数据，可支撑缺集检测。
    enabled=false（默认）时解析链路完全不产生 TVmaze 请求。
    """

    enabled: bool = False
    base_url: str = "https://api.tvmaze.com"
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class SeedhubConfig:
    """SeedHub 磁力+网盘搜索（S2/S3：seedhub_search/magnets/pans）配置。

    enabled=false 时对应工具直接返回 disabled 错误码，不产生任何网络请求。
    request_delay_seconds 为逐条 /link_start 转跳解析之间的限速间隔，
    下限 1.0 秒——调小会触发 Cloudflare 挑战连坐整站（站点实测）。
    """

    enabled: bool = True
    base_url: str = "https://www.seedhub.cc"
    request_delay_seconds: float = 1.0


@dataclass(frozen=True)
class DysbkConfig:
    """网盘资源分享社（bbs.dysbk.com，Flarum 论坛）配置（S6：dysbk_search/links）。

    enabled=false 时对应工具直接返回 disabled 错误码。
    """

    enabled: bool = True
    base_url: str = "https://bbs.dysbk.com"


@dataclass(frozen=True)
class MediaSearchConfig:
    """三路并行合并搜索（S7：media_search）的单路时间预算（方案 A）。

    dysbk 10s / SeedHub 20s；Jackett 一路不设新键，沿用 jackett 段配置。
    """

    dysbk_timeout_seconds: float = 10.0
    seedhub_timeout_seconds: float = 20.0


@dataclass(frozen=True)
class NasConfig:
    config_path: str
    allowed_paths: tuple[str, ...]   # 白名单（已 resolve 为绝对路径字符串）
    exclude_paths: tuple[str, ...]   # 排除目录（已 resolve）
    media_roots: tuple[str, ...]     # 默认搜索范围（allowed_paths 子集）
    video_extensions: frozenset[str]
    subtitle_extensions: frozenset[str]
    limits: Limits
    media: MediaConfig
    organizer: OrganizerConfig
    cleanup: CleanupConfig
    logging: LoggingConfig
    subhd: SubhdConfig
    opensubtitles: OpensubtitlesConfig
    shooter: ShooterConfig
    subtitles: SubtitlesConfig
    jackett: JackettConfig
    tmdb: TmdbConfig
    # S4 新增源配置（均带默认值，老 config.yaml 缺失这些段不报错）
    tvmaze: TvmazeConfig = TvmazeConfig()
    seedhub: SeedhubConfig = SeedhubConfig()
    dysbk: DysbkConfig = DysbkConfig()
    media_search: MediaSearchConfig = MediaSearchConfig()
    enable_write: bool = False
    enable_organize: bool = False
    enable_delete: bool = False


def _require_existing(paths: tuple[str, ...], field: str) -> None:
    """字面量路径必须存在；glob 条目留到运行时展开（卷未挂载不应阻断启动）。"""
    for p in paths:
        if is_pattern(p):
            continue
        if not Path(p).exists():
            _fail(
                field,
                f"{field} 中的路径不存在: {p}",
                expected="已存在的目录",
                actual=p,
                hint="确认存储卷已挂载；可能临时缺失的卷请改用 glob，如 /vol[1-9]*/1000",
            )


def _resolve(p: Any, field: str) -> str:
    if not isinstance(p, str) or not p.strip():
        _fail(
            field,
            f"{field} 中的每一项都必须是非空字符串路径",
            expected="非空绝对路径字符串",
            actual=p,
        )
    path = Path(os.path.expanduser(p.strip()))
    if not path.is_absolute():
        _fail(
            field,
            f"{field} 必须是绝对路径: {p!r}",
            expected="绝对路径（以 / 开头）",
            actual=p,
        )
    return str(path.resolve())


def _positive_int(value: Any, default: int, field: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        _fail(
            field,
            f"{field} 必须是正整数，得到: {value!r}",
            expected="正整数 (>0)",
            actual=value,
        )
    return value


def _nonneg_int(value: Any, default: int, field: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(
            field,
            f"{field} 必须是非负整数，得到: {value!r}",
            expected="非负整数 (≥0)",
            actual=value,
        )
    return value


def _positive_float(value: Any, default: float, field: str) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        _fail(
            field,
            f"{field} 必须是正数，得到: {value!r}",
            expected="正数 (>0)",
            actual=value,
        )
    return float(value)


def _strict_bool(value: Any, default: bool, field: str) -> bool:
    """只接受真正的 YAML 布尔值。

    bool("false") 在 Python 里是 True——用户若写成 enable_write: "false"（字符串），
    静默按 True 处理太危险（尤其未来涉及写权限），直接报错让用户修正。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    _fail(
        field,
        f"{field} 必须是布尔值 true/false（不要加引号），得到: {value!r}",
        expected="布尔值 true/false（YAML 布尔，不加引号）",
        actual=value,
        hint="字符串 \"false\" 会被当成真值，必须写成 false",
    )


def _str_list(value: Any, default: tuple[str, ...], field: str) -> tuple[str, ...]:
    """字符串列表（如 jackett.fast_indexers）；缺失用默认值，空列表合法（=停用）。"""
    if value is None:
        return tuple(default)
    if not isinstance(value, list):
        _fail(
            field,
            f"{field} 必须是字符串列表，得到: {value!r}",
            expected="字符串列表",
            actual=value,
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            _fail(
                field,
                f"{field} 的每一项必须是非空字符串，得到: {item!r}",
                expected="非空字符串",
                actual=item,
            )
        out.append(item.strip())
    return tuple(out)


def _sort_field(value: Any, field: str) -> str:
    """jackett.sort 枚举校验；缺失用默认 seeders（老配置不报错）。"""
    if value is None:
        return "seeders"
    if not isinstance(value, str) or value.strip() not in ("seeders", "quality", "hybrid"):
        _fail(
            field,
            f"{field} 只能是 seeders/quality/hybrid，得到: {value!r}",
            expected="seeders / quality / hybrid",
            actual=value,
        )
    return value.strip()


_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _resolve_api_key(raw: dict[str, Any], field: str, default_env: str, enabled: bool) -> tuple[str, str]:
    """解析 API Key：api_key_env 环境变量 > api_key 字面量（支持 ${VAR} 替换）。

    返回 (api_key, api_key_env)。enabled=true 且解析结果为空时拒绝启动；
    api_key 本身绝不进入任何错误信息（§42）。
    """
    api_key_env = str(raw.get("api_key_env", default_env)).strip()
    api_key = ""
    if api_key_env:
        env_value = os.environ.get(api_key_env, "").strip()
        if env_value:
            api_key = env_value
    if not api_key:
        literal = raw.get("api_key", "")
        if not isinstance(literal, str):
            _fail(
                f"{field}.api_key",
                f"{field}.api_key 必须是字符串",
                expected="字符串（支持 ${{VAR}} 环境变量语法）",
                actual=literal,
            )
        literal = literal.strip()
        m = _ENV_PATTERN.match(literal)
        if m:
            # ${VAR} 语法：从环境变量替换；变量缺失等同未配置
            api_key = os.environ.get(m.group(1), "").strip()
            if not api_key_env:
                api_key_env = m.group(1)
        else:
            api_key = literal
    if enabled and not api_key:
        _fail(
            f"{field}.enabled",
            f"{field}.enabled=true 但未配置 API Key"
            f"（设置 api_key、api_key: ${{{default_env}}} 或环境变量 {api_key_env or default_env}）",
            expected="已配置 api_key / api_key_env / 对应环境变量",
            actual="<未配置>",
        )
    return api_key, api_key_env


def _load_skip_patterns(
    media_raw: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """解析 media.skip_filename_patterns（v1.7.2 §十八）。

    兼容两种条目格式（旧配置必须继续工作，不得强制用户修改）：
      - 字符串："^IMG_" → category 取默认映射 DEFAULT_SKIP_PATTERN_CATEGORIES，
        未收录的按 ""（protected，unknown 保护）；
      - 映射：{pattern: "^IMG_", category: "camera_personal_video"}（category 可选）。

    未配置 → 内置默认集 DEFAULT_SKIP_PATTERNS；显式空列表 = 关闭过滤；
    非法正则在启动时 fail-fast。返回 (patterns, (pattern, category) 对)。
    """
    raw = media_raw.get("skip_filename_patterns")
    if raw is None:
        pats = DEFAULT_SKIP_PATTERNS
        return pats, tuple(
            (p, DEFAULT_SKIP_PATTERN_CATEGORIES.get(p, "")) for p in pats
        )
    if not isinstance(raw, list):
        raise ConfigError(
            "media.skip_filename_patterns 必须是列表（空列表表示关闭）",
            issues=[
                _issue_for(
                    "media.skip_filename_patterns",
                    "必须是列表（空列表表示关闭）",
                    expected="列表",
                    actual=raw,
                )
            ],
        )
    patterns: list[str] = []
    categories: list[tuple[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            pat = entry.strip()
            if not pat:
                _fail("media.skip_filename_patterns", "media.skip_filename_patterns 中存在空模式",
                      expected="非空正则字符串", actual=entry)
            cat = DEFAULT_SKIP_PATTERN_CATEGORIES.get(pat, "")
        elif isinstance(entry, dict):
            pat = str(entry.get("pattern", "")).strip()
            if not pat:
                _fail("media.skip_filename_patterns",
                      "media.skip_filename_patterns 条目缺 pattern",
                      expected="{pattern: 非空正则, category: 可选}", actual=entry)
            cat = str(entry.get("category", "") or "").strip()
        else:
            _fail(
                "media.skip_filename_patterns",
                "media.skip_filename_patterns 每项必须是字符串或 {pattern, category} 映射",
                expected="字符串或 {pattern, category} 映射",
                actual=entry,
            )
        try:
            re.compile(pat, re.IGNORECASE)
        except re.error as exc:
            raise ConfigError(
                f"media.skip_filename_patterns 中存在非法正则 {pat!r}: {exc}",
                issues=[
                    _issue_for(
                        "media.skip_filename_patterns",
                        f"非法正则 {pat!r}: {exc}",
                        expected="可编译的正则表达式",
                        actual=pat,
                    )
                ],
            ) from exc
        if pat not in {p for p in patterns}:
            patterns.append(pat)
            categories.append((pat, cat))
    return tuple(patterns), tuple(categories)


def _load_cleanup(data: dict[str, Any]) -> CleanupConfig:
    """解析 cleanup 段（media_cleanup，v1.7.2 §22/§25）。"""
    raw = data.get("cleanup") or {}
    if not isinstance(raw, dict):
        _fail("cleanup", "cleanup 必须是映射", expected="键值映射", actual=raw)
    field = "cleanup"
    junk_raw = raw.get("junk_extensions")
    if junk_raw is None:
        junk_exts = CleanupConfig.junk_extensions
    else:
        if not isinstance(junk_raw, list) or not all(
            isinstance(x, str) and x.strip() for x in junk_raw
        ):
            _fail(
                f"{field}.junk_extensions",
                f"{field}.junk_extensions 必须是非空字符串列表",
                expected="非空字符串列表（如 [.part, .tmp]）",
                actual=junk_raw,
            )
        # 统一带点小写存储（"part" / ".part" 两种写法都接受）
        junk_exts = tuple(
            "." + x.strip().lower().lstrip(".") for x in junk_raw if x.strip()
        )
    grace = raw.get("active_download_grace_minutes")
    if grace is None:
        grace_min = CleanupConfig.active_download_grace_minutes
    else:
        grace_min = _nonneg_int(grace, CleanupConfig.active_download_grace_minutes,
                                f"{field}.active_download_grace_minutes")
    return CleanupConfig(
        junk_extensions=junk_exts,
        active_download_grace_minutes=grace_min,
    )


def _timeout_field(raw: dict[str, Any], default: float, field: str,
                   name: str = "timeout_seconds") -> float:
    return _positive_float(raw.get(name), default, f"{field}.{name}")


def _load_interval(raw: dict[str, Any], field: str, default: float) -> float:
    """解析 min_request_interval_seconds 字段。"""
    v = raw.get("min_request_interval_seconds")
    if v is None:
        return default
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
        _fail(
            f"{field}.min_request_interval_seconds",
            f"{field}.min_request_interval_seconds 必须是非负数，得到: {v!r}",
            expected="非负数 (≥0)",
            actual=v,
        )
    return float(v)


def _url_field(raw: dict[str, Any], default: str, field: str, *, default_env: str = "") -> tuple[str, str]:
    """解析服务地址：url_env 环境变量 > url/base_url 字面量（支持 ${VAR} 替换）> 默认值。

    url_env 为空字符串时关闭环境变量读取；字面量写 ${JACKETT_URL} 时从环境变量替换。
    返回 (url, url_env)；解析结果必须以 http/https 开头，否则启动时 fail-fast。
    """
    url = ""
    url_env = str(raw.get("url_env", default_env)).strip() if default_env else ""
    if url_env:
        url = os.environ.get(url_env, "").strip().rstrip("/")
    if not url:
        literal = raw.get("url", raw.get("base_url"))
        if isinstance(literal, str):
            literal = literal.strip()
            m = _ENV_PATTERN.match(literal)
            if m:
                url = os.environ.get(m.group(1), "").strip().rstrip("/")
                if not url_env:
                    url_env = m.group(1)
            else:
                url = literal.rstrip("/")
    if not url:
        url = default
    if not url.startswith(("http://", "https://")):
        _fail(
            field,
            f"{field} 的地址必须是 http/https，得到: {url!r}",
            expected="http:// 或 https:// 开头的地址",
            actual=url,
        )
    return url, url_env


def _load_jackett(data: dict[str, Any]) -> JackettConfig:
    raw = data.get("jackett") or {}
    if not isinstance(raw, dict):
        _fail("jackett", "jackett 必须是映射", expected="键值映射", actual=raw)
    field = "jackett"
    enabled = _strict_bool(raw.get("enabled"), False, f"{field}.enabled")
    api_key, api_key_env = _resolve_api_key(raw, field, "JACKETT_API_KEY", enabled)
    max_results = _positive_int(raw.get("max_results"), 100, f"{field}.max_results")
    if max_results > 1000:  # §46/§47 上限
        raise ConfigError(f"{field}.max_results 不能超过 1000，得到: {max_results}")
    max_queries = _positive_int(raw.get("max_queries"), 4, f"{field}.max_queries")
    if max_queries > 10:
        raise ConfigError(f"{field}.max_queries 不能超过 10（避免过度请求 Jackett，§27）")
    max_retries = _nonneg_int(raw.get("max_retries"), 1, f"{field}.max_retries")
    interval_raw = raw.get("min_request_interval_seconds")
    if interval_raw is None:
        min_interval = 0.5
    else:
        if not isinstance(interval_raw, (int, float)) or isinstance(interval_raw, bool) or interval_raw < 0:
            raise ConfigError(
                f"{field}.min_request_interval_seconds 必须是非负数，得到: {interval_raw!r}"
            )
        min_interval = float(interval_raw)
    url, url_env = _url_field(raw, "http://127.0.0.1:9117", field, default_env="JACKETT_URL")
    return JackettConfig(
        enabled=enabled,
        url=url,
        url_env=url_env,
        api_key=api_key,
        api_key_env=api_key_env,
        timeout_seconds=_timeout_field(raw, 90.0, field),
        deadline_seconds=_timeout_field(raw, 50.0, field, name="deadline_seconds"),
        max_retries=max_retries,
        max_results=max_results,
        max_queries=max_queries,
        min_request_interval_seconds=min_interval,
        # T4 新配置（缺失走默认值，老 config.yaml 不报错）
        parallel_fallback=_strict_bool(
            raw.get("parallel_fallback"), True, f"{field}.parallel_fallback"),
        fast_indexers=_str_list(
            raw.get("fast_indexers"), DEFAULT_FAST_INDEXERS, f"{field}.fast_indexers"),
        sort=_sort_field(raw.get("sort"), f"{field}.sort"),
        # T6 新配置（缺失走默认值，老 config.yaml 不报错）
        follow_magnet_redirects=_strict_bool(
            raw.get("follow_magnet_redirects"), True, f"{field}.follow_magnet_redirects"),
        follow_timeout=_timeout_field(
            raw, 3.0, field, name="follow_timeout"),
    )


def _load_tmdb(data: dict[str, Any]) -> TmdbConfig:
    raw = data.get("tmdb") or {}
    if not isinstance(raw, dict):
        _fail("tmdb", "tmdb 必须是映射", expected="键值映射", actual=raw)
    field = "tmdb"
    enabled = _strict_bool(raw.get("enabled"), False, f"{field}.enabled")
    api_key, api_key_env = _resolve_api_key(raw, field, "TMDB_API_KEY", enabled)
    base_url = str(raw.get("base_url", TmdbConfig.base_url)).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"tmdb.base_url 必须是 http/https 地址，得到: {base_url!r}")
    language = str(raw.get("language", "zh-CN")).strip() or "zh-CN"
    return TmdbConfig(
        enabled=enabled,
        api_key=api_key,
        api_key_env=api_key_env,
        timeout_seconds=_timeout_field(raw, 15.0, field),
        base_url=base_url,
        language=language,
    )


def _load_tvmaze(data: dict[str, Any]) -> TvmazeConfig:
    """解析 tvmaze 段（TMDB 回落源）：缺失走默认值，老 config.yaml 不报错。"""
    raw = data.get("tvmaze") or {}
    if not isinstance(raw, dict):
        _fail("tvmaze", "tvmaze 必须是映射", expected="键值映射", actual=raw)
    field = "tvmaze"
    enabled = _strict_bool(raw.get("enabled"), False, f"{field}.enabled")
    base_url = str(raw.get("base_url", TvmazeConfig.base_url)).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"tvmaze.base_url 必须是 http/https 地址，得到: {base_url!r}")
    return TvmazeConfig(
        enabled=enabled,
        base_url=base_url,
        timeout_seconds=_timeout_field(raw, 10.0, field),
    )


def _load_seedhub(data: dict[str, Any]) -> SeedhubConfig:
    """解析 seedhub 段（S4）：缺失走默认值，老 config.yaml 不报错。"""
    raw = data.get("seedhub") or {}
    if not isinstance(raw, dict):
        _fail("seedhub", "seedhub 必须是映射", expected="键值映射", actual=raw)
    field = "seedhub"
    enabled = _strict_bool(raw.get("enabled"), True, f"{field}.enabled")
    base_url = str(raw.get("base_url", SeedhubConfig.base_url)).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{field}.base_url 必须是 http/https 地址，得到: {base_url!r}")
    delay_raw = raw.get("request_delay_seconds")
    if delay_raw is None:
        delay = SeedhubConfig.request_delay_seconds
    else:
        if not isinstance(delay_raw, (int, float)) or isinstance(delay_raw, bool) or delay_raw <= 0:
            raise ConfigError(f"{field}.request_delay_seconds 必须是正数，得到: {delay_raw!r}")
        delay = float(delay_raw)
        if delay < 1.0:
            # 站点实测：逐条 /link_start 转跳间隔 <1s 会触发 CF 挑战连坐整站
            raise ConfigError(
                f"{field}.request_delay_seconds 下限为 1.0（过小会触发 Cloudflare 挑战），"
                f"得到: {delay_raw!r}"
            )
    return SeedhubConfig(enabled=enabled, base_url=base_url, request_delay_seconds=delay)


def _load_dysbk(data: dict[str, Any]) -> DysbkConfig:
    """解析 dysbk 段（S4）：缺失走默认值，老 config.yaml 不报错。"""
    raw = data.get("dysbk") or {}
    if not isinstance(raw, dict):
        _fail("dysbk", "dysbk 必须是映射", expected="键值映射", actual=raw)
    field = "dysbk"
    enabled = _strict_bool(raw.get("enabled"), True, f"{field}.enabled")
    base_url = str(raw.get("base_url", DysbkConfig.base_url)).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{field}.base_url 必须是 http/https 地址，得到: {base_url!r}")
    return DysbkConfig(enabled=enabled, base_url=base_url)


def _load_media_search(data: dict[str, Any]) -> MediaSearchConfig:
    """解析 media_search 段（S4 方案 A 预算表）：缺失走默认值。"""
    raw = data.get("media_search") or {}
    if not isinstance(raw, dict):
        _fail("media_search", "media_search 必须是映射", expected="键值映射", actual=raw)
    field = "media_search"
    return MediaSearchConfig(
        dysbk_timeout_seconds=_timeout_field(
            raw, 10.0, field, name="dysbk_timeout_seconds"),
        seedhub_timeout_seconds=_timeout_field(
            raw, 20.0, field, name="seedhub_timeout_seconds"),
    )


def _load_opensubtitles(data: dict[str, Any]) -> OpensubtitlesConfig:
    raw = data.get("opensubtitles") or {}
    if not isinstance(raw, dict):
        _fail("opensubtitles", "opensubtitles 必须是映射", expected="键值映射", actual=raw)
    field = "opensubtitles"
    enabled = _strict_bool(raw.get("enabled"), False, f"{field}.enabled")
    api_key, api_key_env = _resolve_api_key(raw, field, "OPENSUBTITLES_API_KEY", enabled)
    # enabled=true 时 username/password 必须非空（下载需要 JWT）
    username = str(raw.get("username", "")).strip()
    password = str(raw.get("password", "")).strip()
    if enabled and (not username or not password):
        raise ConfigError(
            f"{field}.enabled=true 但 username/password 未配置"
            f"（OpenSubtitles 下载需要账号换 JWT）"
        )
    base_url = str(raw.get("base_url", OpensubtitlesConfig.base_url)).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{field}.base_url 必须是 http/https 地址，得到: {base_url!r}")
    return OpensubtitlesConfig(
        enabled=enabled,
        base_url=base_url,
        api_key=api_key,
        api_key_env=api_key_env,
        username=username,
        password=password,
        language=str(raw.get("language", "zh-cn")).strip() or "zh-cn",
        timeout_seconds=_timeout_field(raw, 30.0, field),
        max_retries=_nonneg_int(raw.get("max_retries"), 1, f"{field}.max_retries"),
        min_request_interval_seconds=_load_interval(raw, field, 1.0),
    )


def _load_shooter(data: dict[str, Any]) -> ShooterConfig:
    raw = data.get("shooter") or {}
    if not isinstance(raw, dict):
        _fail("shooter", "shooter 必须是映射", expected="键值映射", actual=raw)
    field = "shooter"
    enabled = _strict_bool(raw.get("enabled"), False, f"{field}.enabled")
    # token 解析：token_env 优先，然后 ${VAR} 字面量
    token_env = str(raw.get("token_env", "ASSRT_TOKEN")).strip()
    token = ""
    if token_env:
        token = os.environ.get(token_env, "").strip()
    if not token:
        literal = raw.get("token", "")
        if isinstance(literal, str):
            literal = literal.strip()
            m = _ENV_PATTERN.match(literal)
            if m:
                token = os.environ.get(m.group(1), "").strip()
                if not token_env:
                    token_env = m.group(1)
            else:
                token = literal
    if enabled and not token:
        raise ConfigError(
            f"{field}.enabled=true 但未配置 token"
            f"（设置 token、token: ${{ASSRT_TOKEN}} 或环境变量 {token_env or 'ASSRT_TOKEN'}）"
        )
    base_url = str(raw.get("base_url", ShooterConfig.base_url)).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"{field}.base_url 必须是 http/https 地址，得到: {base_url!r}")
    return ShooterConfig(
        enabled=enabled,
        base_url=base_url,
        token=token,
        token_env=token_env,
        timeout_seconds=_timeout_field(raw, 30.0, field),
        max_retries=_nonneg_int(raw.get("max_retries"), 1, f"{field}.max_retries"),
        min_request_interval_seconds=_load_interval(raw, field, 1.0),
    )


def _load_subtitles(data: dict[str, Any]) -> SubtitlesConfig:
    raw = data.get("subtitle") or {}
    if not isinstance(raw, dict):
        _fail("subtitle", "subtitle 必须是映射", expected="键值映射", actual=raw)
    valid_sites = {"subhd", "opensubtitles", "shooter"}
    order_raw = raw.get("site_order")
    if order_raw is None:
        site_order = ("subhd", "opensubtitles", "shooter")
    else:
        if not isinstance(order_raw, list) or not order_raw:
            raise ConfigError("subtitle.site_order 必须是非空列表")
        site_order = tuple(str(s).strip() for s in order_raw)
        for s in site_order:
            if s not in valid_sites:
                raise ConfigError(
                    f"subtitle.site_order 中的 {s!r} 不是合法站点名"
                    f"（可选: {', '.join(sorted(valid_sites))}）"
                )
    score_raw = raw.get("switch_min_score")
    if score_raw is None:
        switch_min_score = 0.9
    else:
        if not isinstance(score_raw, (int, float)) or isinstance(score_raw, bool):
            raise ConfigError(f"subtitle.switch_min_score 必须是数字，得到: {score_raw!r}")
        switch_min_score = float(score_raw)
        if not 0 <= switch_min_score <= 1:
            raise ConfigError(f"subtitle.switch_min_score 必须在 0~1 范围内，得到: {switch_min_score}")
    thr_raw = raw.get("season_pack_threshold")
    if thr_raw is None:
        season_pack_threshold = 3
    else:
        if not isinstance(thr_raw, int) or isinstance(thr_raw, bool) or thr_raw < 1:
            raise ConfigError(
                f"subtitle.season_pack_threshold 必须是 ≥1 的整数，得到: {thr_raw!r}"
            )
        season_pack_threshold = thr_raw
    return SubtitlesConfig(
        site_order=site_order,
        switch_min_score=switch_min_score,
        season_pack_threshold=season_pack_threshold,
    )


def _load_organizer(data: dict[str, Any], file: Path) -> OrganizerConfig:
    """解析 organizer 段（相对审计日志路径落到数据目录/配置文件目录）。"""
    org_raw = data.get("organizer") or {}
    if not isinstance(org_raw, dict):
        _fail("organizer", "organizer 必须是映射", expected="键值映射", actual=org_raw)
    special_raw = org_raw.get("special_dir_names")
    if special_raw is None:
        special_dirs = OrganizerConfig.special_dir_names
    else:
        if not isinstance(special_raw, list) or not all(
            isinstance(x, str) and x.strip() for x in special_raw
        ):
            _fail(
                "organizer.special_dir_names",
                "organizer.special_dir_names 必须是非空字符串列表",
                expected="非空字符串列表",
                actual=special_raw,
            )
        special_dirs = tuple(x.strip().lower() for x in special_raw)
    follow_raw = org_raw.get("follow_extensions")
    if follow_raw is None:
        follow_exts = OrganizerConfig.follow_extensions
    else:
        if not isinstance(follow_raw, list) or not all(
            isinstance(x, str) and x.strip() for x in follow_raw
        ):
            _fail(
                "organizer.follow_extensions",
                "organizer.follow_extensions 必须是非空字符串列表",
                expected="非空字符串列表",
                actual=follow_raw,
            )
        follow_exts = tuple(x.strip().lstrip(".").lower() for x in follow_raw if x.strip())
    # 相对路径优先解析到 OCTOP_DATA_DIR / NAS_DATA_DIR 环境变量所指持久化目录，
    # 缺省时相对配置文件所在目录解析（避免落到调用方 cwd）。
    # 支持 ~ 路径展开（如 ~/.nas-media/logs/organize-audit.jsonl），
    # 使得插件包整包升级重装时不随插件代码目录被覆盖丢失历史审计凭据。
    audit_raw = (
        str(org_raw.get("audit_log_file", OrganizerConfig.audit_log_file)).strip()
        or OrganizerConfig.audit_log_file
    )
    audit_expanded = os.path.expanduser(audit_raw)
    audit_path = Path(audit_expanded)
    if not audit_path.is_absolute():
        data_dir = os.environ.get("OCTOP_DATA_DIR") or os.environ.get("NAS_DATA_DIR")
        if data_dir:
            audit_path = Path(data_dir) / audit_path
        else:
            audit_path = file.parent / audit_path
    return OrganizerConfig(
        max_move_files=_positive_int(
            org_raw.get("max_move_files"), 200, "organizer.max_move_files"
        ),
        special_dir_names=special_dirs,
        follow_extensions=follow_exts,
        audit_log_file=str(audit_path),
    )


def _load_subhd(data: dict[str, Any]) -> SubhdConfig:
    """解析 subhd 段（M4）。"""
    subhd_raw = data.get("subhd") or {}
    if not isinstance(subhd_raw, dict):
        _fail("subhd", "subhd 必须是映射", expected="键值映射", actual=subhd_raw)
    base_url = str(subhd_raw.get("base_url", "https://subhd.tv")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        _fail(
            "subhd.base_url",
            f"subhd.base_url 必须是 http/https 地址，得到: {base_url!r}",
            expected="http:// 或 https:// 地址",
            actual=base_url,
        )
    filename_template = str(subhd_raw.get("filename_template", "{stem}.{lang}.{ext}")).strip()
    if not filename_template:
        _fail(
            "subhd.filename_template",
            "subhd.filename_template 不能为空",
            expected="包含 {stem}/{lang}/{ext} 的文件名模板",
            actual=filename_template,
        )
    for placeholder in ("{stem}", "{lang}", "{ext}"):
        if placeholder not in filename_template:
            _fail(
                "subhd.filename_template",
                f"subhd.filename_template 必须包含 {placeholder} 占位符，得到: {filename_template!r}",
                expected="同时包含 {stem}、{lang}、{ext} 占位符",
                actual=filename_template,
            )
    return SubhdConfig(
        enabled=_strict_bool(subhd_raw.get("enabled"), False, "subhd.enabled"),
        base_url=base_url,
        timeout_seconds=_positive_float(
            subhd_raw.get("timeout_seconds"), 30.0, "subhd.timeout_seconds"
        ),
        connect_timeout_seconds=_positive_float(
            subhd_raw.get("connect_timeout_seconds"), 8.0, "subhd.connect_timeout_seconds"
        ),
        max_retries=_nonneg_int(subhd_raw.get("max_retries"), 2, "subhd.max_retries"),
        max_results=_positive_int(subhd_raw.get("max_results"), 20, "subhd.max_results"),
        max_download_bytes=_positive_int(
            subhd_raw.get("max_download_bytes"), 20 * 1024 * 1024, "subhd.max_download_bytes"
        ),
        min_request_interval_seconds=_load_interval(subhd_raw, "subhd", 1.0),
        user_agent=str(subhd_raw.get("user_agent", SubhdConfig.user_agent)),
        filename_template=filename_template,
    )


def _load_logging(data: dict[str, Any]) -> LoggingConfig:
    log_raw = data.get("logging") or {}
    if not isinstance(log_raw, dict):
        _fail("logging", "logging 必须是映射", expected="键值映射", actual=log_raw)
    return LoggingConfig(
        file=str(log_raw.get("file", "logs/nas-mcp.log")),
        level=str(log_raw.get("level", "INFO")).upper(),
        max_bytes=_positive_int(log_raw.get("max_bytes"), 5 * 1024 * 1024, "logging.max_bytes"),
        backup_count=_positive_int(log_raw.get("backup_count"), 3, "logging.backup_count"),
    )


def load_config(path: str) -> NasConfig:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(
            f"配置文件不存在: {file}（可用 --config 参数或环境变量 NAS_MCP_CONFIG 指定）",
            file=str(file),
            missing=True,
        )
    try:
        text = file.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError(f"配置文件读取/解析失败: {exc}", file=str(file)) from exc
    return _build_config(data, file, text)


def load_config_text(text: str, path_hint: str | Path = "<config>") -> NasConfig:
    """用与 load_config 完全一致的规则校验配置文本（不落盘）。

    nas_save_config 写盘前调用：校验不通过抛 ConfigError（带 issues），绝不落盘。
    """
    file = Path(path_hint)
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"配置文件读取/解析失败: {exc}",
            issues=[
                ConfigIssue(
                    key="<yaml>",
                    message=f"YAML 解析失败: {exc}",
                    expected="合法 YAML",
                    severity="error",
                )
            ],
            file=str(file),
        ) from exc
    return _build_config(data, file, text)


def _build_config(data: Any, file: Path, text: str) -> NasConfig:
    """收集式校验：一次报多条 issues（上限 MAX_CONFIG_ISSUES），无错才返回配置。"""
    if not isinstance(data, dict):
        raise ConfigError(
            "配置文件顶层必须是键值映射",
            issues=[
                ConfigIssue(
                    key="<root>",
                    message="配置文件顶层必须是键值映射",
                    expected="键值映射",
                    line=1,
                    column=1,
                )
            ],
            file=str(file),
        )
    index = _SourceIndex(text)
    collector: list[ConfigIssue] = []
    index_token = _INDEX.set(index)
    file_token = _CONFIG_FILE.set(str(file))
    collect_token = _COLLECTOR.set(collector)
    try:
        cfg = _load_all(data, file)
    finally:
        _COLLECTOR.reset(collect_token)
        _CONFIG_FILE.reset(file_token)
        _INDEX.reset(index_token)
    if collector:
        detail = "；".join(issue.render() for issue in collector)
        raise ConfigError(
            f"配置校验失败（{len(collector)} 项）：{detail}",
            issues=collector,
            file=str(file),
        )
    return cfg


def _load_all(data: dict[str, Any], file: Path) -> NasConfig:
    # ---- 未知顶层键：拼写错误/废弃键静默忽略会改变实际行为，逐条收集上报 ----
    for key in sorted(str(k) for k in set(data) - KNOWN_TOP_LEVEL_KEYS):
        _try(
            key,
            lambda k=key: _fail(
                k,
                f"未知配置键: {k}",
                expected="config.example.yaml 中定义的键",
                actual=k,
                hint="检查拼写，或该键属于旧版本已废弃字段，删除即可",
            ),
            True,
        )

    # ---- 白名单：必须配置且非空 ----
    def _allowed():
        allowed_raw = data.get("allowed_paths")
        if not isinstance(allowed_raw, list) or not allowed_raw:
            _fail(
                "allowed_paths",
                "allowed_paths 必须是非空列表——没有白名单就不允许启动",
                expected="非空列表（至少一条绝对路径或 glob）",
                actual=allowed_raw,
                hint="只填写实际已授权的目录，例如 [/vol1/1000/move]；不要写整卷 /vol1/1000",
            )
        resolved = tuple(dict.fromkeys(_resolve(p, "allowed_paths") for p in allowed_raw))
        _require_existing(resolved, "allowed_paths")
        return resolved

    allowed = _try("allowed_paths", _allowed, ())

    # ---- 排除目录 ----
    def _excluded():
        exclude_raw = data.get("exclude_paths") or []
        if not isinstance(exclude_raw, list):
            _fail(
                "exclude_paths",
                "exclude_paths 必须是列表",
                expected="路径列表",
                actual=exclude_raw,
            )
        return tuple(dict.fromkeys(_resolve(p, "exclude_paths") for p in exclude_raw))

    excluded = _try("exclude_paths", _excluded, ())

    # 排除目录优先于白名单：若整个白名单根被排除，属于配置矛盾，直接拒绝启动。
    # 模式条目（glob）无法静态判定包含关系，交给 PathGuard 运行时实时裁决。
    if allowed and excluded:
        def _conflicts():
            for a in allowed:
                for e in excluded:
                    if is_pattern(a) or is_pattern(e):
                        continue
                    if is_within(Path(a), Path(e)):
                        _fail(
                            "allowed_paths",
                            f"白名单目录 {a} 位于排除目录 {e} 之内，配置矛盾",
                            expected="白名单与排除目录互不包含",
                            actual=a,
                            hint="调整 exclude_paths，避免把整个白名单根排除",
                        )
            return True

        _try("allowed_paths", _conflicts, True)

    # ---- 搜索范围（默认等于白名单）----
    def _roots():
        media_raw = data.get("media_roots")
        if media_raw is None:
            return allowed
        if not isinstance(media_raw, list) or not media_raw:
            _fail(
                "media_roots",
                "media_roots 必须是非空列表（留空请删除该键，默认取 allowed_paths）",
                expected="非空列表（或删除该键）",
                actual=media_raw,
                hint="与 allowed_paths 一起收窄到实际授权目录，如 /vol1/1000/move",
            )
        roots = tuple(dict.fromkeys(_resolve(p, "media_roots") for p in media_raw))
        for m in roots:
            if is_pattern(m) or any(is_pattern(a) for a in allowed):
                continue  # 含模式条目时跳过静态包含校验，运行时由 PathGuard 实时裁决
            if not any(is_within(Path(m), Path(a)) for a in allowed):
                _fail(
                    "media_roots",
                    f"media_roots 中的 {m} 不在 allowed_paths 白名单内",
                    expected="allowed_paths 内的子目录",
                    actual=m,
                )
        _require_existing(roots, "media_roots")
        return roots

    media_roots = _try("media_roots", _roots, ()) if allowed else ()

    # ---- 扩展名 ----
    def _ext_set(key: str, default: tuple[str, ...]) -> frozenset[str]:
        val = data.get(key, list(default))
        if not isinstance(val, list):
            _fail(key, f"{key} 必须是列表", expected="扩展名字符串列表", actual=val)
        items = frozenset(
            str(v).lower().lstrip(".").strip() for v in val if str(v).strip()
        )
        if not items:
            _fail(key, f"{key} 不能为空", expected="至少一个扩展名", actual=val)
        return items

    video_exts = _try(
        "video_extensions",
        lambda: _ext_set("video_extensions", DEFAULT_VIDEO_EXTENSIONS),
        frozenset(DEFAULT_VIDEO_EXTENSIONS),
    )
    subtitle_exts = _try(
        "subtitle_extensions",
        lambda: _ext_set("subtitle_extensions", DEFAULT_SUBTITLE_EXTENSIONS),
        frozenset(DEFAULT_SUBTITLE_EXTENSIONS),
    )

    # ---- 限额 ----
    lim_raw = data.get("limits") or {}
    if not isinstance(lim_raw, dict):
        _try(
            "limits",
            lambda: _fail("limits", "limits 必须是映射", expected="键值映射", actual=lim_raw),
            None,
        )
        lim_raw = {}
    limits = Limits(
        max_list_entries=_try(
            "limits.max_list_entries",
            lambda: _positive_int(
                lim_raw.get("max_list_entries"), 500, "limits.max_list_entries"
            ),
            500,
        ),
        max_search_results=_try(
            "limits.max_search_results",
            lambda: _positive_int(
                lim_raw.get("max_search_results"), 100, "limits.max_search_results"
            ),
            100,
        ),
        max_scan_files=_try(
            "limits.max_scan_files",
            lambda: _positive_int(
                lim_raw.get("max_scan_files"), 100_000, "limits.max_scan_files"
            ),
            100_000,
        ),
        max_list_depth=_try(
            "limits.max_list_depth",
            lambda: _positive_int(
                lim_raw.get("max_list_depth"), 10, "limits.max_list_depth"
            ),
            10,
        ),
        max_errors_reported=_try(
            "limits.max_errors_reported",
            lambda: _positive_int(
                lim_raw.get("max_errors_reported"), 20, "limits.max_errors_reported"
            ),
            20,
        ),
    )

    # ---- 媒体分析（ffprobe） ----
    media_raw = data.get("media") or {}
    if not isinstance(media_raw, dict):
        _try(
            "media",
            lambda: _fail("media", "media 必须是映射", expected="键值映射", actual=media_raw),
            None,
        )
        media_raw = {}
    skip_pats, skip_cats = _try(
        "media.skip_filename_patterns",
        lambda: _load_skip_patterns(media_raw),
        (
            DEFAULT_SKIP_PATTERNS,
            tuple((p, DEFAULT_SKIP_PATTERN_CATEGORIES.get(p, "")) for p in DEFAULT_SKIP_PATTERNS),
        ),
    )
    media_cfg = MediaConfig(
        ffprobe_path=str(media_raw.get("ffprobe_path", "ffprobe")).strip() or "ffprobe",
        ffprobe_timeout_seconds=_try(
            "media.ffprobe_timeout_seconds",
            lambda: _positive_float(
                media_raw.get("ffprobe_timeout_seconds"), 20.0, "media.ffprobe_timeout_seconds"
            ),
            20.0,
        ),
        ffmpeg_path=str(media_raw.get("ffmpeg_path", "ffmpeg")).strip() or "ffmpeg",
        ffmpeg_timeout_seconds=_try(
            "media.ffmpeg_timeout_seconds",
            lambda: _positive_float(
                media_raw.get("ffmpeg_timeout_seconds"), 300.0, "media.ffmpeg_timeout_seconds"
            ),
            300.0,
        ),
        max_probe_files=_try(
            "media.max_probe_files",
            lambda: _positive_int(media_raw.get("max_probe_files"), 200, "media.max_probe_files"),
            200,
        ),
        max_classify_batch=_try(
            "media.max_classify_batch",
            lambda: _positive_int(
                media_raw.get("max_classify_batch"), 20, "media.max_classify_batch"
            ),
            20,
        ),
        skip_filename_patterns=skip_pats,
        skip_pattern_categories=skip_cats,
    )
    cleanup_cfg = _try("cleanup", lambda: _load_cleanup(data), CleanupConfig())

    # ---- 媒体目录整理（M9：organize_media / find_media） ----
    organizer_cfg = _try("organizer", lambda: _load_organizer(data, file), OrganizerConfig())

    # ---- SubHD（M4：nas_search_subtitles / nas_download_subtitle） ----
    subhd_cfg = _try("subhd", lambda: _load_subhd(data), SubhdConfig())

    # ---- OpenSubtitles / Shooter / 字幕编排 ----
    opensubtitles_cfg = _try(
        "opensubtitles", lambda: _load_opensubtitles(data), OpensubtitlesConfig()
    )
    shooter_cfg = _try("shooter", lambda: _load_shooter(data), ShooterConfig())
    subtitles_cfg = _try("subtitle", lambda: _load_subtitles(data), SubtitlesConfig())

    # ---- Jackett 资源搜索（P4：jackett_search）与 TMDB 影视识别（P3：resolve_media） ----
    jackett_cfg = _try("jackett", lambda: _load_jackett(data), JackettConfig())
    tmdb_cfg = _try("tmdb", lambda: _load_tmdb(data), TmdbConfig())
    tvmaze_cfg = _try("tvmaze", lambda: _load_tvmaze(data), TvmazeConfig())

    # ---- S4：SeedHub / 网盘资源分享社 / media_search 预算（缺失全走默认值） ----
    seedhub_cfg = _try("seedhub", lambda: _load_seedhub(data), SeedhubConfig())
    dysbk_cfg = _try("dysbk", lambda: _load_dysbk(data), DysbkConfig())
    media_search_cfg = _try(
        "media_search", lambda: _load_media_search(data), MediaSearchConfig()
    )

    # ---- 日志 ----
    logging_cfg = _try("logging", lambda: _load_logging(data), LoggingConfig())

    return NasConfig(
        config_path=str(file),
        allowed_paths=allowed,
        exclude_paths=excluded,
        media_roots=media_roots,
        video_extensions=video_exts,
        subtitle_extensions=subtitle_exts,
        limits=limits,
        media=media_cfg,
        organizer=organizer_cfg,
        cleanup=cleanup_cfg,
        logging=logging_cfg,
        subhd=subhd_cfg,
        opensubtitles=opensubtitles_cfg,
        shooter=shooter_cfg,
        subtitles=subtitles_cfg,
        jackett=jackett_cfg,
        tmdb=tmdb_cfg,
        tvmaze=tvmaze_cfg,
        seedhub=seedhub_cfg,
        dysbk=dysbk_cfg,
        media_search=media_search_cfg,
        enable_write=_try(
            "enable_write", lambda: _strict_bool(data.get("enable_write"), False, "enable_write"), False
        ),
        enable_organize=_try(
            "enable_organize",
            lambda: _strict_bool(data.get("enable_organize"), False, "enable_organize"),
            False,
        ),
        enable_delete=_try(
            "enable_delete", lambda: _strict_bool(data.get("enable_delete"), False, "enable_delete"), False
        ),
    )


def preflight_check(cfg: NasConfig) -> list[str]:
    """启动前权限自检：返回警告列表（空列表=全部通过）。

    检查项（只警告不抛异常——部分目录可能暂时离线，不应阻止服务启动）：
    1. 白名单/搜索范围展开后的每个目录是否存在且可列（os.listdir 可读）
    2. 日志目录是否可写（写不进去时日志会降级到 stderr）

    Octop 本地版以 octop-native 用户运行，常见故障是代码/数据目录权限
    为 ---------x 导致进程秒退、Octop 只看到 "Connection closed"。
    自检把具体哪个目录不可读打印到 stderr，便于定位。
    """
    warnings: list[str] = []

    # 1. 白名单数据目录可读性
    for root in expand_roots(cfg.allowed_paths):
        p = Path(root)
        if not p.exists():
            warnings.append(f"白名单目录不存在: {p}（fnOS 上可能是该卷未挂载）")
            continue
        try:
            next(p.iterdir(), None)  # 尝试列出目录
        except PermissionError:
            warnings.append(
                f"白名单目录不可读: {p}（当前用户无读取权限；"
                f"Octop 本地版以 octop-native 用户运行，请执行 chmod a+rx {p}）"
            )
        except OSError as exc:
            warnings.append(f"白名单目录访问失败: {p}（{exc}）")

    # 2. 日志目录可写性
    log_file = Path(cfg.logging.file)
    log_dir = log_file.parent if log_file.parent != Path(".") else Path.cwd()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / ".nas_mcp_write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        warnings.append(
            f"日志目录不可写: {log_dir}（{exc}；日志将降级输出到 stderr，"
            f"请执行 chmod a+w {log_dir}）"
        )

    return warnings
