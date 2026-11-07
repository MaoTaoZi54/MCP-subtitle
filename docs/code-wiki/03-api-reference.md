# 03 · API 参考（数据模型 / 异常 / 函数返回）

本章是"写代码时查什么"的速查表：数据结构长什么样、异常怎么映射、关键函数返回哪些字段。

## 1. 通用返回约定

### 1.1 成功

```json
{ "ok": true, "...": "各工具自有字段" }
```

### 1.2 失败

```json
{
  "ok": false,
  "error": {
    "code": "access_denied",
    "message": "路径不在白名单内: /etc/passwd"
  }
}
```

约定：

- **永不向上抛裸异常**；装配层统一 `try/except → 错误信封`。
- `code` 是稳定的机器可读标识（见 [05-config-and-security.md](./05-config-and-security.md#5-错误码表)），`message` 是给人/Agent 看的中文说明。
- 部分结果（如 Jackett 被 deadline 截断）仍返回 `ok: true`，但带 `deadline_exceeded` / `truncated` / `incomplete` 等诊断字段。

### 1.3 incomplete / truncated 语义

| 字段 | 含义 |
| --- | --- |
| `truncated` | 结果被 `limit` 截断，还有下一页 |
| `scan_truncated` | 目录遍历被深度/条目预算截断，**没扫完** |
| `plan_truncated` | 整理计划被 `max_move_files` 截断 |
| `incomplete` | 因权限/预算导致结果不完整，值为原因标识 |
| `errors_summary` | 权限错误的聚合摘要（`denied_sample` ≤5 条） |

## 2. 异常体系

| 异常 | 定义位置 | 触发场景 | 装配层映射 |
| --- | --- | --- | --- |
| `ConfigError` | `config.py` | 配置缺失/非法/YAML 错误 | `not_configured` |
| `AccessDenied` | `security.py` | 路径不在白名单 / 命中排除 | `access_denied` |
| `InvalidArguments` | `security.py` | 参数非法（空关键词、互斥参数、超上限） | `invalid_arguments`（插件侧 `ValueError` 同映射） |
| `ProbeError` | `media.py` | ffprobe 失败/超时 | 计入 `errors[]`，不整体失败 |
| `ResolverError` | `resolver.py` | TMDB 未配置/请求失败 | 通常降级为未识别 |
| `SubhdError` | `subhd.py` | SubHD 请求/解析失败 | `subhd_error` |
| `OpensubtitlesError` | `opensubtitles.py` | OpenSubtitles 失败 | 计入 `site_errors` |
| `ShooterError` | `shooter.py` | assrt 失败 | 计入 `site_errors` |
| `SubtitleError` | `subtitle.py` | 所有站点均失败 | `all_sites_failed` |
| `JackettError` | `jackett.py` | Jackett 不可用/超时/认证/解析/熔断 | `jackett_unavailable` 等细分码 |

`JackettError` 额外携带：

```python
code: str                  # JACKETT_UNAVAILABLE / TIMEOUT / AUTH / PARSE / HTTP / CIRCUIT_OPEN
status_code: int | None    # HTTP 状态码（如有）
retryable: bool            # 是否值得稍后重试
retry_after: float | None  # 建议等待秒数（熔断时为剩余冷却）
```

## 3. 配置数据模型

`NasConfig` 是冻结 dataclass（`frozen=True`），字段一次加载终身不变（进程内）。

```python
@dataclass(frozen=True)
class NasConfig:
    config_path: str
    allowed_paths: tuple[str, ...]      # 已 resolve 为绝对路径
    exclude_paths: tuple[str, ...]
    media_roots: tuple[str, ...]        # allowed_paths 的子集
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
    seedhub: SeedhubConfig = SeedhubConfig()
    dysbk: DysbkConfig = DysbkConfig()
    media_search: MediaSearchConfig = MediaSearchConfig()
    enable_write: bool = False
    enable_organize: bool = False
    enable_delete: bool = False
```

子配置的默认值（节选，完整见 [05-config-and-security.md](./05-config-and-security.md#2-配置-schema)）：

```python
Limits(max_list_entries=500, max_search_results=100, max_scan_files=100_000,
       max_list_depth=10, max_errors_reported=20)

MediaConfig(ffprobe_path="ffprobe", ffprobe_timeout_seconds=20.0,
            ffmpeg_path="ffmpeg", ffmpeg_timeout_seconds=300.0,
            max_probe_files=200, max_classify_batch=20,
            skip_filename_patterns=(), skip_pattern_categories=())

OrganizerConfig(max_move_files=200, special_dir_names=(bdmv/video_ts/extras/...),
                follow_extensions=("nfo","jpg","jpeg","png"),
                audit_log_file="logs/organize-audit.jsonl")

CleanupConfig(junk_extensions=(".part",".partial",".aria2",".tmp"),
              active_download_grace_minutes=60)

SubtitlesConfig(site_order=("subhd","opensubtitles","shooter"),
                switch_min_score=0.9, season_pack_threshold=3)

JackettConfig(enabled=False, url="http://127.0.0.1:9117", api_key="",
              timeout_seconds=90.0, deadline_seconds=50.0, max_retries=1,
              max_results=100, max_queries=4, min_request_interval_seconds=0.5,
              parallel_fallback=True, fast_indexers=DEFAULT_FAST_INDEXERS,
              sort="seeders", follow_magnet_redirects=True, follow_timeout=3.0)

TmdbConfig(enabled=False, api_key="", timeout_seconds=15.0,
           base_url="https://api.themoviedb.org/3", language="zh-CN")

MediaSearchConfig(dysbk_timeout_seconds=10.0, seedhub_timeout_seconds=20.0)
```

## 4. 业务数据模型

### 4.1 ReleaseInfo（发布名解析）

```python
@dataclass(frozen=True)
class ReleaseInfo:
    resolution: str | None      # 2160p/1080p/720p/...
    source_type: str | None     # REMUX/BluRay/WEB-DL/WEBRip/HDTV/DVDRip/CAM
    video_codec: str | None     # HEVC/AVC/AV1/XviD
    hdr: str | None             # Dolby Vision/HDR10+/HDR10/HLG/HDR
    audio_codec: str | None
    audio_channels: str | None
    bit_depth: int | None
    is_3d: bool
    has_atmos: bool
    quality_score: int          # 0-100
    quality_tier: str           # excellent/good/fair/poor/bad/unknown
    matched_tags: tuple[str, ...]
```

评分构成（`release.py`）：

| 维度 | 权重/取值 |
| --- | --- |
| 分辨率 | 4320p/2160p=40，1080p=30，720p=18，480p=8，SD=5 |
| 来源 | REMUX=35，BluRay=30，WEB-DL=25，WEBRip=18，HDTV=12，DVDRip=8，CAM=0 |
| 编码/HDR/音频 | 按规则累加，并用 `bitrate_tiebreaker` 处理同档排序 |

`release_dict(title)` 是给工具返回值用的序列化版本（Jackett 每条结果都带 `release`）。

### 4.2 SubtitleCandidate / VideoQuery

```python
@dataclass(frozen=True)
class SubtitleCandidate:
    sid: str                 # 站点内 ID；多站点时带前缀 subhd:/opensubtitles:/shooter:
    title: str               # 详情页标题
    release_name: str        # 发布文件名（用于匹配）
    languages: tuple[str, ...]
    format: str              # SRT/ASS/SSA/SUP...
    source: str              # 字幕组/来源
    score: float = 0.0
    detail_url: str = ""
    site: str = "subhd"

@dataclass(frozen=True)
class VideoQuery:
    title: str
    year: int | None
    season: int | None
    episode: int | None
    resolution: str | None
    source: str | None
    release_group: str | None
```

### 4.3 字幕评分权重

`score_candidate(candidate, query, release_hint=...)` 的权重：

| 维度 | 权重 |
| --- | --- |
| 片名 | 0.35 |
| 年份 | 0.15 |
| 季 | 0.10 |
| 集 | 0.10 |
| 分辨率 | 0.10 |
| 来源 | 0.10 |
| 发布组 | 0.05 |
| 中文字幕 | 0.05 |

规则：

- 季/集冲突时总分 ×0.5；
- `release_hint`（原视频 stem）命中可加分；
- 候选的 `pack_hint` 表示疑似整季包；
- 各站点内部评分会被编排层**覆盖重算**，保证跨站可比。

### 4.4 JackettResult

```python
@dataclass(frozen=True)
class JackettResult:
    title: str
    size: int | None
    seeders: int | None
    leechers: int | None
    indexer: str
    publish_date: str | None
    magnet: str | None          # 允许为 None（附 torrent_url/details_url）
    torrent_url: str | None
    details_url: str | None
    infohash: str | None        # 从 magnet 提取，用于去重
```

### 4.5 ToolSpec（nas-app 工具元数据）

```python
@dataclass
class ToolSpec:
    name: str
    title: str
    category: str          # 配置/文件/媒体/字幕/资源/整理/运维
    risk: str              # read / write / delete
    long_running: bool
    fn: Callable[..., dict]
    description: str
    arg_docs: dict[str, str]
    params: list[dict]     # 自动从函数签名生成
```

`public()` 输出给前端/MCP 的字段：`name/title/category/risk/risk_label/long_running/description/params`。

### 4.6 Job（nas-app 长任务）

```python
@dataclass
class Job:
    id: str
    tool: str
    title: str
    args: dict
    status: str                  # queued / running / succeeded / failed
    created_at: str
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None
    result: dict | None
    error: dict | None
```

## 5. 关键函数返回结构

### 5.1 `fs.list_directory`

```json
{
  "ok": true,
  "path": "/vol1/1000/media",
  "resolved": "/vol1/1000/media",
  "recursive": false,
  "offset": 0,
  "limit": 500,
  "total_scanned": 42,
  "returned": 42,
  "truncated": false,
  "scan_truncated": false,
  "errors": [],
  "errors_summary": {"count": 0, "denied_sample": []},
  "entries": [
    {
      "name": "Movie.mkv", "type": "file", "extension": "mkv",
      "media_category": "video", "size_bytes": 123, "mtime": 1700000000.0,
      "relative_path": "Movie.mkv", "path": "/vol1/1000/media/Movie.mkv"
    }
  ]
}
```

### 5.2 `media.scan_media`

每个视频条目的 `info` 结构：

```json
{
  "name": "Show.S01E01.1080p.mkv",
  "path": "/vol1/1000/tv/Show/Show.S01E01.1080p.mkv",
  "relative_path": "Show/S01E01.1080p.mkv",
  "size_bytes": 1234567,
  "mtime": 1700000000.0,
  "media_type": "tv",
  "probed": true,
  "has_chinese_subtitle": true,
  "chinese_subtitle_sources": ["external"],
  "subtitle": {
    "external": ["Show.S01E01.zh.srt"],
    "embedded": [{"index": 2, "language": "chi", "title": "简体"}],
    "embedded_checked": true
  },
  "media": {
    "duration_seconds": 2700.0,
    "width": 1920, "height": 1080,
    "video_codec": "hevc", "audio_codec": "aac",
    "hdr": "SDR", "bit_rate": 8000000
  }
}
```

探测预算：`media.max_probe_files`，**按尝试次数计**（失败也占预算），避免坏文件拖死整次扫描。

### 5.3 `library.audit`（媒体库体检）

八项统计（TMDB 未启用时"无法识别"为 `null`，而不是用文件名硬凑）：

1. 电影数
2. 电视剧数
3. 无法识别数
4. 无字幕数
5. 无中文字幕数
6. 低分辨率数（≤720p）
7. 重复作品组数 / 重复文件数
8. 异常文件数（损坏/不完整/时长异常）

附加：`multi_root`（同一作品散落多个剧集根）、低清样例、`incomplete`。

### 5.4 `subtitle.search_subtitles`（多站点编排）

```json
{
  "ok": true,
  "query": {
    "title": "Breaking Bad", "year": 2008, "season": 2, "episode": 13,
    "resolution": "1080p", "source": "BluRay",
    "release_group": "NOGRP", "keyword": "Breaking Bad 2008"
  },
  "total": 12,
  "search_status": "results_found",
  "site_attempts": [
    {"site": "subhd", "status": "ok", "count": 8, "best_score": 0.94},
    {"site": "_stopped", "status": "stopped_early", "count": 0,
     "reason": "best_score>=0.9"}
  ],
  "candidates": [
    {
      "sid": "subhd:OyVNRJ", "title": "...", "release_name": "...",
      "languages": ["简体", "英语"], "format": "ASS", "source": "YYeTs",
      "score": 0.94, "detail_url": "https://subhd.tv/a/OyVNRJ",
      "site": "subhd", "pack_hint": false
    }
  ],
  "tried_sites": ["subhd", "opensubtitles", "shooter"],
  "site_errors": {"opensubtitles": "timeout"}
}
```

`search_status` 四态：

| 值 | 含义 |
| --- | --- |
| `results_found` | 有结果且无站点报错 |
| `partial_failure` | 有结果但有站点报错（可能漏好候选） |
| `no_results_confirmed` | 至少一站正常返回 0 条（确认无数据） |
| `all_sites_failed` | 所有站点均报错 |

### 5.5 `jackett.search`

```json
{
  "ok": true,
  "query": "流浪地球 2",
  "resolved": {"tmdb_id": 842675, "title": "The Wandering Earth II", "...": "..."},
  "queries": ["The Wandering Earth II 2023", "The Wandering Earth II 2023 2160p"],
  "queries_planned": 2,
  "queries_executed": 2,
  "deadline_seconds": 50.0,
  "deadline_exceeded": false,
  "elapsed_seconds": 12.3,
  "mode": "aggregate",
  "circuit": {"state": "closed", "cooldown_remaining_seconds": 0.0,
             "consecutive_failures": 0},
  "total": 37,
  "seeders_missing_count": 2,
  "results": [
    {
      "title": "...", "size": 123, "seeders": 40, "leechers": 3,
      "indexer": "xxx", "publish_date": "...",
      "magnet": "magnet:?xt=urn:btih:...",
      "torrent_url": "...", "details_url": "...",
      "release": {"resolution": "2160p", "quality_score": 92, "...": "..."}
    }
  ]
}
```

`mode`：`aggregate`（常规聚合端点）或 `fallback_parallel`（聚合超时/0 结果后的单源并行降级，附 `fallback_failed_indexers`）。

### 5.6 `organizer.plan_session`

```json
{
  "ok": true,
  "dry_run": true,
  "mode": "all",
  "preexisting_empty_scope": "library_root",
  "path": "/vol1/1000/downloads",
  "library_roots": ["/vol1/1000/media"],
  "counts": {"move": 12, "rename_dir": 2, "identical": 1,
             "duplicate": 3, "blocked": 1, "cleanup_dir": 0, "skip": 5},
  "plan": [
    {"action": "move", "source": "...", "target": "...", "reason": "...", "risk": "low"}
  ],
  "write_preflight": {"ok": false, "status": "not_writable",
                      "unwritable_dirs": ["/vol1/1000/media"]},
  "scan_truncated": false,
  "plan_truncated": false,
  "errors": [],
  "errors_summary": {"count": 0, "denied_sample": []}
}
```

`write_preflight.ok=false` 时 `execute_plan` 会整体拒绝执行。

### 5.7 `cleanup.cleanup`

```json
{
  "ok": true,
  "dry_run": true,
  "mode": "scan",
  "scope": {
    "path": null, "roots": ["/vol1/1000/media"],
    "total_candidates": 12, "scan_truncated": false,
    "counts": {"media": 0, "media_junk": 9, "personal_video": 2, "unknown": 1},
    "enable_delete": false, "active_download_grace_minutes": 60,
    "junk_extensions": [".aria2", ".part", ".partial", ".tmp"],
    "evidence_fields": ["..."]
  },
  "pagination": {"limit": 100, "offset": 0, "truncated": false, "next_offset": null},
  "items": [],
  "not_found": [],
  "errors": [],
  "operation_id": null,
  "execution": {"performed": false}
}
```

### 5.8 `rollback.rollback`

```json
{
  "ok": true,
  "operation_id": "op-20261027-abcdef",
  "dry_run": true,
  "status": "planned",
  "reason": "",
  "items": [],
  "rolled_back_items": [],
  "failed_items": [],
  "errors": [],
  "checks": [
    "operation_id_exists", "action_supported", "source_target_exists",
    "path_allowed", "conflict", "permission", "size", "mtime"
  ],
  "hint": "..."
}
```

支持的 `status`：`planned`、`rolled_back`、`partial`、`not_found`、`rollback_unsupported`、`permission_denied`、`rollback_conflict` 等。出现 `MUX` 记录时直接 `rollback_unsupported`。

### 5.9 `federated.search`

```json
{
  "ok": true,
  "query": "沙丘",
  "results": [],
  "sources": {"seedhub": 5, "dysbk": 8, "jackett": 12},
  "merged": 20,
  "truncated": false,
  "site_errors": {"seedhub": "cf_blocked"},
  "seedhub_skipped_by_language": false,
  "elapsed_seconds": 18.4
}
```

合并规则：infohash / 网盘链接 / 标题三重去重，每作品 top3，总闸 20，统一 schema。

## 6. 装配层接口

### 6.1 nas-app HTTP API

基础：`http://<host>:8669`。鉴权：`Authorization: Bearer <NAS_APP_TOKEN>` 或登录 Cookie。写请求（POST/PUT/DELETE）必须带 `X-Nas-App` 头（CSRF 防护）。请求体上限 2 MB。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查（Docker HEALTHCHECK 使用） |
| GET/POST | `/login` | 登录页/提交令牌 |
| GET | `/logout` | 退出 |
| GET | `/static/*` | 前端静态资源 |
| GET | `/api/meta` | 应用版本、工具数等元信息 |
| GET | `/api/overview` | 概览数据 |
| GET/PUT | `/api/config` | 读取/保存配置（保存后即时生效） |
| GET | `/api/tools` | 工具列表（`ToolSpec.public()`） |
| GET/POST | `/api/tools/<name>` | 调用工具；`?async=1` 强制异步 |
| GET/POST | `/api/jobs` | 任务列表 / 提交 |
| GET | `/api/jobs/<id>?wait=30` | 查询任务，可阻塞等待 |
| GET | `/api/logs?kind=app\|audit` | 应用日志 / 审计日志 |

### 6.2 nas-app MCP 端点

- 地址：`http://<host>:8670/mcp`（Streamable HTTP）
- 鉴权：`Authorization: Bearer <NAS_APP_TOKEN>`；**无 token 时 MCP 不启动**（`AppSettings.mcp_ready`）。
- 工具：28 个业务工具 + `nas_job_status` + `nas_job_list`。
- 长任务：`long_running` 工具最多等待 240 秒；超时返回 `{status: "queued"|"running", job_id, ...}`。
- 不暴露配置写入工具（配置只在 Web UI 维护）。

### 6.3 Octop 插件工具注册

```python
ctx.tool(name, fn, description=fn.__doc__ or name)
```

`nas_save_config` 单独注册（带表单 schema），其余 28 个在循环中注册。

## 7. 审计记录格式（JSONL）

整理/清理/混流的审计写入 `organizer.audit_log_file`（默认 `logs/organize-audit.jsonl`），每行一条 JSON：

```json
{
  "ts": "2026-10-27T14:30:00+08:00",
  "action": "MOVE",
  "source": "/vol1/1000/downloads/a.mkv",
  "target": "/vol1/1000/media/Movie (2023)/Movie (2023).mkv",
  "result": "SUCCESS",
  "error": "",
  "channel": "organize",
  "operation_id": "op-20261027-abcdef",
  "size": 1234567,
  "mtime": 1700000000123456789
}
```

说明：

- 必填字段：`ts/action/source/target/result/error/channel`；
- `operation_id` 在有会话时写入；
- `size` = `st_size`，`mtime` = **`st_mtime_ns`**（纳秒整数），回滚时用于快速校验文件未被替换；
- `channel` 区分 `organize` / `cleanup` 通道，审计文件路径由 `audit_log_path(cfg, channel)` 决定。

`rollback` 只按 `operation_id` 聚合记录；**不会**根据 `source/target` 反推（防止误回滚无关文件）。
