"""配置加载：路径、扩展名、限额等易变内容一律来自 YAML 配置文件，代码不写死。

安全底线：没有合法的 allowed_paths 白名单时拒绝启动，绝不默认放开权限。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .security import is_pattern, is_within, expand_roots


class ConfigError(Exception):
    """配置缺失或非法。"""


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


@dataclass(frozen=True)
class Limits:
    max_list_entries: int = 500
    max_search_results: int = 100
    max_scan_files: int = 100_000
    max_list_depth: int = 10
    max_errors_reported: int = 20


@dataclass(frozen=True)
class MediaConfig:
    """媒体分析（ffprobe）相关配置。"""

    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_seconds: float = 20.0
    max_probe_files: int = 200
    max_classify_batch: int = 20   # nas_classify_region 单次批量上限（§18）
    skip_filename_patterns: tuple[str, ...] = ()  # 文件名跳过模式（默认集见 DEFAULT_SKIP_PATTERNS）


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
    timeout_seconds: float = 15.0
    max_results: int = 20
    max_download_bytes: int = 20 * 1024 * 1024
    min_request_interval_seconds: float = 1.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    filename_template: str = "{stem}.{lang}.{ext}"


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
    timeout_seconds: float = 20.0
    max_results: int = 100
    max_queries: int = 4          # 单次搜索最多生成的关键词组合数（§27）
    min_request_interval_seconds: float = 0.5


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
class NasConfig:
    config_path: str
    allowed_paths: tuple[str, ...]   # 白名单（已 resolve 为绝对路径字符串）
    exclude_paths: tuple[str, ...]   # 排除目录（已 resolve）
    media_roots: tuple[str, ...]     # 默认搜索范围（allowed_paths 子集）
    video_extensions: frozenset[str]
    subtitle_extensions: frozenset[str]
    limits: Limits
    media: MediaConfig
    logging: LoggingConfig
    subhd: SubhdConfig
    jackett: JackettConfig
    tmdb: TmdbConfig
    enable_write: bool = False
    enable_delete: bool = False


def _resolve(p: Any, field: str) -> str:
    if not isinstance(p, str) or not p.strip():
        raise ConfigError(f"{field} 中的每一项都必须是非空字符串路径")
    path = Path(os.path.expanduser(p.strip()))
    if not path.is_absolute():
        raise ConfigError(f"{field} 必须是绝对路径: {p!r}")
    return str(path.resolve())


def _positive_int(value: Any, default: int, field: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{field} 必须是正整数，得到: {value!r}")
    return value


def _strict_bool(value: Any, default: bool, field: str) -> bool:
    """只接受真正的 YAML 布尔值。

    bool("false") 在 Python 里是 True——用户若写成 enable_write: "false"（字符串），
    静默按 True 处理太危险（尤其未来涉及写权限），直接报错让用户修正。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{field} 必须是布尔值 true/false（不要加引号），得到: {value!r}")


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
            raise ConfigError(f"{field}.api_key 必须是字符串")
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
        raise ConfigError(
            f"{field}.enabled=true 但未配置 API Key"
            f"（设置 api_key、api_key: ${{{default_env}}} 或环境变量 {api_key_env or default_env}）"
        )
    return api_key, api_key_env


def _load_skip_patterns(media_raw: dict[str, Any]) -> tuple[str, ...]:
    """解析 media.skip_filename_patterns。

    未配置 → 使用内置默认集 DEFAULT_SKIP_PATTERNS；显式空列表 = 关闭过滤；
    非法正则在启动时 fail-fast，绝不静默忽略。
    """
    raw = media_raw.get("skip_filename_patterns")
    if raw is None:
        return DEFAULT_SKIP_PATTERNS
    if not isinstance(raw, list) or any(not isinstance(p, str) or not p.strip() for p in raw):
        raise ConfigError("media.skip_filename_patterns 必须是非空字符串列表（空列表表示关闭）")
    patterns = tuple(dict.fromkeys(p.strip() for p in raw))
    for p in patterns:
        try:
            re.compile(p, re.IGNORECASE)
        except re.error as exc:
            raise ConfigError(f"media.skip_filename_patterns 中存在非法正则 {p!r}: {exc}") from exc
    return patterns


def _timeout_field(raw: dict[str, Any], default: float, field: str) -> float:
    value = raw.get("timeout_seconds")
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{field}.timeout_seconds 必须是正数，得到: {value!r}")
    return float(value)


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
        raise ConfigError(f"{field} 的地址必须是 http/https，得到: {url!r}")
    return url, url_env


def _load_jackett(data: dict[str, Any]) -> JackettConfig:
    raw = data.get("jackett") or {}
    if not isinstance(raw, dict):
        raise ConfigError("jackett 必须是映射")
    field = "jackett"
    enabled = _strict_bool(raw.get("enabled"), False, f"{field}.enabled")
    api_key, api_key_env = _resolve_api_key(raw, field, "JACKETT_API_KEY", enabled)
    max_results = _positive_int(raw.get("max_results"), 100, f"{field}.max_results")
    if max_results > 1000:  # §46/§47 上限
        raise ConfigError(f"{field}.max_results 不能超过 1000，得到: {max_results}")
    max_queries = _positive_int(raw.get("max_queries"), 4, f"{field}.max_queries")
    if max_queries > 10:
        raise ConfigError(f"{field}.max_queries 不能超过 10（避免过度请求 Jackett，§27）")
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
        timeout_seconds=_timeout_field(raw, 20.0, field),
        max_results=max_results,
        max_queries=max_queries,
        min_request_interval_seconds=min_interval,
    )


def _load_tmdb(data: dict[str, Any]) -> TmdbConfig:
    raw = data.get("tmdb") or {}
    if not isinstance(raw, dict):
        raise ConfigError("tmdb 必须是映射")
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


def load_config(path: str) -> NasConfig:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(
            f"配置文件不存在: {file}（可用 --config 参数或环境变量 NAS_MCP_CONFIG 指定）"
        )
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise ConfigError(f"配置文件读取/解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是键值映射")

    # ---- 白名单：必须配置且非空 ----
    allowed_raw = data.get("allowed_paths")
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise ConfigError("allowed_paths 必须是非空列表——没有白名单就不允许启动")
    allowed = tuple(dict.fromkeys(_resolve(p, "allowed_paths") for p in allowed_raw))

    # ---- 排除目录 ----
    exclude_raw = data.get("exclude_paths") or []
    if not isinstance(exclude_raw, list):
        raise ConfigError("exclude_paths 必须是列表")
    excluded = tuple(dict.fromkeys(_resolve(p, "exclude_paths") for p in exclude_raw))

    # 排除目录优先于白名单：若整个白名单根被排除，属于配置矛盾，直接拒绝启动。
    # 模式条目（glob）无法静态判定包含关系，交给 PathGuard 运行时实时裁决。
    for a in allowed:
        for e in excluded:
            if is_pattern(a) or is_pattern(e):
                continue
            if is_within(Path(a), Path(e)):
                raise ConfigError(f"白名单目录 {a} 位于排除目录 {e} 之内，配置矛盾")

    # ---- 搜索范围（默认等于白名单）----
    media_raw = data.get("media_roots")
    if media_raw is None:
        media_roots = allowed
    else:
        if not isinstance(media_raw, list) or not media_raw:
            raise ConfigError("media_roots 必须是非空列表（留空请删除该键，默认取 allowed_paths）")
        media_roots = tuple(dict.fromkeys(_resolve(p, "media_roots") for p in media_raw))
    for m in media_roots:
        if is_pattern(m) or any(is_pattern(a) for a in allowed):
            continue  # 含模式条目时跳过静态包含校验，运行时由 PathGuard 实时裁决
        if not any(is_within(Path(m), Path(a)) for a in allowed):
            raise ConfigError(f"media_roots 中的 {m} 不在 allowed_paths 白名单内")

    # ---- 扩展名 ----
    def _ext_set(key: str, default: tuple[str, ...]) -> frozenset[str]:
        val = data.get(key, list(default))
        if not isinstance(val, list):
            raise ConfigError(f"{key} 必须是列表")
        items = frozenset(
            str(v).lower().lstrip(".").strip() for v in val if str(v).strip()
        )
        if not items:
            raise ConfigError(f"{key} 不能为空")
        return items

    video_exts = _ext_set("video_extensions", DEFAULT_VIDEO_EXTENSIONS)
    subtitle_exts = _ext_set("subtitle_extensions", DEFAULT_SUBTITLE_EXTENSIONS)

    # ---- 限额 ----
    lim_raw = data.get("limits") or {}
    if not isinstance(lim_raw, dict):
        raise ConfigError("limits 必须是映射")
    limits = Limits(
        max_list_entries=_positive_int(lim_raw.get("max_list_entries"), 500, "limits.max_list_entries"),
        max_search_results=_positive_int(lim_raw.get("max_search_results"), 100, "limits.max_search_results"),
        max_scan_files=_positive_int(lim_raw.get("max_scan_files"), 100_000, "limits.max_scan_files"),
        max_list_depth=_positive_int(lim_raw.get("max_list_depth"), 10, "limits.max_list_depth"),
        max_errors_reported=_positive_int(lim_raw.get("max_errors_reported"), 20, "limits.max_errors_reported"),
    )

    # ---- 媒体分析（ffprobe） ----
    media_raw = data.get("media") or {}
    if not isinstance(media_raw, dict):
        raise ConfigError("media 必须是映射")
    ffprobe_path = str(media_raw.get("ffprobe_path", "ffprobe")).strip() or "ffprobe"
    timeout_raw = media_raw.get("ffprobe_timeout_seconds")
    if timeout_raw is None:
        ffprobe_timeout = 20.0
    else:
        if not isinstance(timeout_raw, (int, float)) or isinstance(timeout_raw, bool) or timeout_raw <= 0:
            raise ConfigError(f"media.ffprobe_timeout_seconds 必须是正数，得到: {timeout_raw!r}")
        ffprobe_timeout = float(timeout_raw)
    media_cfg = MediaConfig(
        ffprobe_path=ffprobe_path,
        ffprobe_timeout_seconds=ffprobe_timeout,
        max_probe_files=_positive_int(
            media_raw.get("max_probe_files"), 200, "media.max_probe_files"
        ),
        max_classify_batch=_positive_int(
            media_raw.get("max_classify_batch"), 20, "media.max_classify_batch"
        ),
        skip_filename_patterns=_load_skip_patterns(media_raw),
    )

    # ---- SubHD（M4：nas_search_subtitles / nas_download_subtitle） ----
    subhd_raw = data.get("subhd") or {}
    if not isinstance(subhd_raw, dict):
        raise ConfigError("subhd 必须是映射")
    subhd_enabled = _strict_bool(subhd_raw.get("enabled"), False, "subhd.enabled")
    base_url = str(subhd_raw.get("base_url", "https://subhd.tv")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"subhd.base_url 必须是 http/https 地址，得到: {base_url!r}")
    subhd_timeout_raw = subhd_raw.get("timeout_seconds")
    if subhd_timeout_raw is None:
        subhd_timeout = 15.0
    else:
        if not isinstance(subhd_timeout_raw, (int, float)) or isinstance(subhd_timeout_raw, bool) or subhd_timeout_raw <= 0:
            raise ConfigError(f"subhd.timeout_seconds 必须是正数，得到: {subhd_timeout_raw!r}")
        subhd_timeout = float(subhd_timeout_raw)
    interval_raw = subhd_raw.get("min_request_interval_seconds")
    if interval_raw is None:
        min_interval = 1.0
    else:
        if not isinstance(interval_raw, (int, float)) or isinstance(interval_raw, bool) or interval_raw < 0:
            raise ConfigError(
                f"subhd.min_request_interval_seconds 必须是非负数，得到: {interval_raw!r}"
            )
        min_interval = float(interval_raw)
    filename_template = str(subhd_raw.get("filename_template", "{stem}.{lang}.{ext}")).strip()
    if not filename_template:
        raise ConfigError("subhd.filename_template 不能为空")
    for placeholder in ("{stem}", "{lang}", "{ext}"):
        if placeholder not in filename_template:
            raise ConfigError(
                f"subhd.filename_template 必须包含 {placeholder} 占位符，得到: {filename_template!r}"
            )
    subhd_cfg = SubhdConfig(
        enabled=subhd_enabled,
        base_url=base_url,
        timeout_seconds=subhd_timeout,
        max_results=_positive_int(subhd_raw.get("max_results"), 20, "subhd.max_results"),
        max_download_bytes=_positive_int(
            subhd_raw.get("max_download_bytes"), 20 * 1024 * 1024, "subhd.max_download_bytes"
        ),
        min_request_interval_seconds=min_interval,
        user_agent=str(subhd_raw.get("user_agent", SubhdConfig.user_agent)),
        filename_template=filename_template,
    )

    # ---- Jackett 资源搜索（P4：jackett_search）与 TMDB 影视识别（P3：resolve_media） ----
    jackett_cfg = _load_jackett(data)
    tmdb_cfg = _load_tmdb(data)

    # ---- 日志 ----
    log_raw = data.get("logging") or {}
    if not isinstance(log_raw, dict):
        raise ConfigError("logging 必须是映射")
    logging_cfg = LoggingConfig(
        file=str(log_raw.get("file", "logs/nas-mcp.log")),
        level=str(log_raw.get("level", "INFO")).upper(),
        max_bytes=_positive_int(log_raw.get("max_bytes"), 5 * 1024 * 1024, "logging.max_bytes"),
        backup_count=_positive_int(log_raw.get("backup_count"), 3, "logging.backup_count"),
    )

    return NasConfig(
        config_path=str(file),
        allowed_paths=allowed,
        exclude_paths=excluded,
        media_roots=media_roots,
        video_extensions=video_exts,
        subtitle_extensions=subtitle_exts,
        limits=limits,
        media=media_cfg,
        logging=logging_cfg,
        subhd=subhd_cfg,
        jackett=jackett_cfg,
        tmdb=tmdb_cfg,
        enable_write=_strict_bool(data.get("enable_write"), False, "enable_write"),
        enable_delete=_strict_bool(data.get("enable_delete"), False, "enable_delete"),
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
