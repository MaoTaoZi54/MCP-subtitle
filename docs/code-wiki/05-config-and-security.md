# 05 · 配置与安全模型

## 1. 配置文件位置

| 形态 | 默认路径 | 可否在线修改 |
| --- | --- | --- |
| Octop 插件 | `<插件目录>/config.yaml` | 可以：`nas_save_config`（保留注释的行级写回） |
| nas-app | `<data>/config.yaml`（容器内 `/data/config.yaml`） | 可以：`PUT /api/config` 或 Web UI |
| nas-mcp | `--config` 指定 | 否（改完重启） |

插件目录中的 `config.example.yaml` 是完整模板；nas-app 首次启动会用 `ensure_config()` 生成一份最小可用配置并 `chmod 600`。

## 2. 配置 schema

### 2.1 顶层键

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `allowed_paths` | list[str] | **必填** | 路径白名单，支持 glob；为空直接拒绝启动 |
| `media_roots` | list[str] | = `allowed_paths` | 默认搜索根，必须是白名单子集 |
| `exclude_paths` | list[str] | `[]` | 排除目录，优先级高于白名单 |
| `video_extensions` | list[str] | `[mkv, mp4, avi, mov, ts, m2ts, webm, wmv, flv]` | 视频扩展名 |
| `subtitle_extensions` | list[str] | `[srt, ass, ssa, sub, vtt, sup]` | 字幕扩展名 |
| `enable_write` | bool | `false` | 允许字幕落盘 |
| `enable_organize` | bool | `false` | 允许整理执行移动 |
| `enable_delete` | bool | `false` | 允许 L3 删除（高危） |
| `limits` | map | 见下 | 性能/结果限额 |
| `media` | map | 见下 | ffprobe / ffmpeg / 跳过模式 |
| `logging` | map | `file=logs/nas-mcp.log, level=INFO` | 日志 |
| `subtitle` | map | 见下 | 多站点字幕编排 |
| `subhd` | map | `enabled=true` | SubHD |
| `opensubtitles` | map | `enabled=false` | OpenSubtitles |
| `shooter` | map | `enabled=false` | assrt.net |
| `tmdb` | map | `enabled=false` | TMDB |
| `jackett` | map | `enabled=false` | Jackett |
| `seedhub` | map | `enabled=true` | SeedHub |
| `dysbk` | map | `enabled=true` | 网盘资源分享社 |
| `media_search` | map | `dysbk=10s, seedhub=20s` | 聚合搜索单路预算 |
| `organizer` | map | 见下 | 整理 |
| `cleanup` | map | 见下 | 清理 |

### 2.2 `limits`

```yaml
limits:
  max_list_entries: 500      # nas_list_directory 单页上限
  max_search_results: 100    # nas_find_files 单页上限
  max_scan_files: 100000     # 单次扫描最多访问条目数
  max_list_depth: 10         # 最大递归深度
  max_errors_reported: 20    # 单次结果最多报告多少条单文件错误
```

### 2.3 `media`

```yaml
media:
  ffprobe_path: ffprobe
  ffprobe_timeout_seconds: 20
  ffmpeg_path: ffmpeg
  ffmpeg_timeout_seconds: 300
  max_probe_files: 200       # 单次扫描最多 probe 的文件数（失败也占预算）
  max_classify_batch: 20
  skip_filename_patterns: [] # 留空 = 内置默认集（IMG_/VID_/DJI_/时间戳/成人标记）
  skip_pattern_categories: [] # (pattern, category) 对，供 cleanup 做保护判断
```

`skip_filename_patterns` 命中后文件被**隐藏**（不返回），只在 `skipped_by_filter` 计数——用于过滤家庭录像与成人内容，不是删除。

### 2.4 `subtitle`（注意：单数，不是 `subtitles`）

```yaml
subtitle:
  site_order: [subhd, opensubtitles, shooter]
  switch_min_score: 0.9
  season_pack_threshold: 3
```

### 2.5 `subhd`

```yaml
subhd:
  enabled: true
  base_url: https://subhd.tv
  timeout_seconds: 30
  max_retries: 1
  max_results: 20
  max_download_bytes: 20971520
  min_request_interval_seconds: 1.0
  filename_template: "{stem}.{lang}.{ext}"   # 必须含 {stem}/{lang}/{ext}
```

### 2.6 `opensubtitles` / `shooter`

```yaml
opensubtitles:
  enabled: false
  base_url: https://api.opensubtitles.com
  api_key_env: OPENSUBTITLES_API_KEY
  api_key: ""
  username: ""          # 下载必需（换 JWT）
  password: ""
  language: zh-cn
  timeout_seconds: 30
  max_retries: 1
  min_request_interval_seconds: 1.0

shooter:
  enabled: false
  base_url: https://api.assrt.net
  token_env: ASSRT_TOKEN
  token: ""
  timeout_seconds: 30
  max_retries: 1
  min_request_interval_seconds: 1.0
```

### 2.7 `tmdb` / `jackett`

```yaml
tmdb:
  enabled: false
  base_url: https://api.themoviedb.org/3
  language: zh-CN
  timeout_seconds: 15
  api_key_env: TMDB_API_KEY
  api_key: ""            # 支持 ${TMDB_API_KEY} 语法

jackett:
  enabled: false
  url: http://127.0.0.1:9117
  url_env: JACKETT_URL
  timeout_seconds: 90
  deadline_seconds: 50      # 单次 search() 的绝对总预算
  max_retries: 1
  max_results: 100          # ≤1000
  max_queries: 4            # ≤10
  min_request_interval_seconds: 0.5
  parallel_fallback: true
  fast_indexers: [thepiratebay, knaben, ...]
  sort: seeders             # seeders | quality | hybrid
  follow_magnet_redirects: true
  follow_timeout: 3
  api_key_env: JACKETT_API_KEY
  api_key: ""
```

### 2.8 `organizer` / `cleanup`

```yaml
organizer:
  max_move_files: 200
  special_dir_names: [bdmv, video_ts, certificate, certificates,
                      featurettes, extras, behind the scenes, deleted scenes,
                      interviews, shorts, samples, sample, trailers, trailer, other]
  follow_extensions: [nfo, jpg, jpeg, png]
  audit_log_file: logs/organize-audit.jsonl

cleanup:
  junk_extensions: [.part, .partial, .aria2, .tmp]
  active_download_grace_minutes: 60
```

### 2.9 加载与校验

`load_config()` 做的事：

1. `yaml.safe_load` → 类型必须为 dict；
2. 路径字段逐条 `expanduser` + `resolve`（`_resolve`），拒绝空串/非字符串；
3. 整数校验（`_positive_int` / `_nonneg_int`），布尔严格校验（`_strict_bool` 只接受真 bool）；
4. `allowed_paths` 非空；`media_roots` 必须是白名单子集（`is_within`）；
5. 密钥解析（`_resolve_api_key`）：先读 `*_env` 环境变量，再读字面量；字面量支持 `${ENV}` 替换；
6. 启用了需要密钥的功能但取不到密钥 → `ConfigError`（拒绝启动，而不是运行时报 401）。

`preflight_check(cfg)` 供启动/诊断使用，返回**警告列表**（空列表 = 全部通过）。检查项只有两类：白名单/搜索范围展开后的目录是否可列、日志目录是否可写。它只警告不抛异常（目录可能暂时离线，不应阻止启动）。

## 3. 环境变量（nas-app）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NAS_APP_CONFIG` | `<data>/config.yaml` | 配置文件路径 |
| `NAS_APP_DATA` | `/data`（容器内）或 `./data` | 数据目录 |
| `NAS_APP_HOST` | `0.0.0.0` | 监听地址 |
| `NAS_APP_PORT` | `8669` | Web/HTTP 端口 |
| `NAS_APP_TOKEN` | 空 | 访问令牌；空 = 不鉴权（仅建议内网） |
| `NAS_APP_TOKEN_FILE` | 空 | 从文件读令牌（Docker secret） |
| `NAS_APP_MCP_ENABLED` | `true` | 是否启用 MCP 端点 |
| `NAS_APP_MCP_HOST` | `127.0.0.1` | MCP 监听地址（容器内用 `0.0.0.0`） |
| `NAS_APP_MCP_PORT` | `8670` | MCP 端口 |
| `NAS_APP_MCP_PATH` | `/mcp` | Streamable HTTP 路径 |
| `NAS_APP_ALLOWED_PATHS` | `/media` 或 cwd | 首次生成配置时的白名单 |
| `NAS_APP_EXCLUDE_PATHS` | 空 | 首次生成配置时的排除目录 |
| `NAS_APP_ENABLE_WRITE` | `false` | 首次生成配置时的写开关 |
| `NAS_APP_ENABLE_ORGANIZE` | `false` | 整理开关 |
| `NAS_APP_ENABLE_DELETE` | `false` | 删除开关 |

核心密钥环境变量（三种形态通用）：`TMDB_API_KEY`、`JACKETT_API_KEY`、`JACKETT_URL`、`OPENSUBTITLES_API_KEY`、`ASSRT_TOKEN`。

## 4. 路径安全（PathGuard）

### 4.1 判定规则

```python
guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
p = guard.check(raw_path)   # 通过返回 resolved Path；否则抛 AccessDenied
```

顺序：

1. **排除优先**：命中 `exclude_paths`（字面量或 glob）→ 拒绝；
2. **白名单**：命中 `allowed_paths` 任一模式 → 通过；
3. 否则拒绝。

匹配语义（`matches_pattern`）：

- 路径**自身**匹配，或路径的**任一祖先**匹配；
- 含 `*` `?` `[` 的按 glob 处理（`fnmatch`），否则按字面量前缀/相等处理；
- 所有路径先 `expanduser().resolve()`，因此软链接逃逸会被解析到真实路径再判定。

举例（fnOS）：

```yaml
allowed_paths: ["/vol[1-9]*/1000"]
exclude_paths: ["/vol0*", "/vol[1-9]*/1000-*"]
```

- `/vol1/1000/media/a.mkv` ✅
- `/vol2/1000/media/a.mkv` ✅（新盘插入自动纳入）
- `/vol02/1000-1-abc/media/a.mkv` ❌（网盘挂载，命中排除）
- `/vol1/1000-1-abc/media/a.mkv` ❌（纵深防御）
- `/etc/passwd` ❌

### 4.2 media_roots 的展开

`expand_roots()` 在**每次调用**时把 glob 展开为当前实际存在的目录。因此加盘/卸盘即时生效，无需改配置。

`media_roots` 必须落在 `allowed_paths` 内；不在白名单的 root 会在加载期被拒绝。

### 4.3 为什么"排除优先"

fnOS 的网盘挂载点位于本地卷目录之下（`/vol1/1000-1-<hash>`）。如果只做白名单，`/vol[1-9]*/1000` 会把网盘也放进来。排除优先 + `1000-*` 模式形成纵深防御。

## 5. 门控矩阵

| 工具 | `enable_write` | `enable_organize` | `enable_delete` | 其他前置条件 |
| --- | --- | --- | --- | --- |
| 所有只读工具 | — | — | — | 路径通过 PathGuard |
| `subhd_download` | ✅ 必需 | — | — | 至少一个站点 `enabled` |
| `media_fill_subtitles` | ✅ 必需（非 dry_run） | — | — | 同上 |
| `media_mux_subtitles` | ✅ 必需（非 dry_run） | — | ✅ 必需（`replace_original`/`delete_external`） | 视频/字幕在库内 |
| `organize_media(dry_run=true)` | — | — | — | 无（L1 恒可用） |
| `organize_media(dry_run=false)` 移动 | — | ✅ 必需 | — | `write_preflight.ok` |
| `organize_media` 中 `identical`/`duplicate` 真删 | — | ✅ | ✅ 必需 | 否则计划标 `blocked` |
| `media_cleanup(dry_run=false)` | — | — | ✅ 必需 | 五重门 |
| `media_rollback(dry_run=false)` | — | ✅ 必需 | — | 有效 `operation_id` + 8 项检查 |
| `nas_save_config` | 仅管理员 | 可由 Agent 修改 | — | **不允许**修改 `enable_delete`；Agent 通道还不能扩 `allowed_paths`、不能置 `enable_write=true`、不能把 `junk_extensions` 设为视频/字幕扩展名、不能把 `jackett_url` 指向非本机 |

## 6. 清理的五重门

`media_cleanup` 要真正删除一个文件，必须全部满足：

1. 路径通过 `PathGuard`；
2. `enable_delete=true`；
3. 证据字段完整（分类依据、扩展名、mtime、大小、命中规则可追溯）；
4. 不在活跃下载宽限窗口内：`now - mtime > active_download_grace_minutes × 60`；
5. 未命中 `special_dir_names`（BDMV/VIDEO_TS/花絮/Samples…）与 `skip_filename_patterns`。

此外：

- `dry_run` 默认 `True`；
- 删除是**永久删除**，不进回收站，不可回滚；
- 空目录不由 cleanup 处理（归 `organize_media` 的 `cleanup_dir`）。

## 7. 密钥与敏感信息

| 措施 | 实现 |
| --- | --- |
| 配置文件权限 | 首次生成时 `chmod 600` |
| 环境变量优先 | 所有站点密钥支持 `*_env`，推荐用环境变量 |
| 字面量占位符 | `api_key: "${TMDB_API_KEY}"` 由加载器替换 |
| 只回显布尔 | `nas_get_config` 返回 `api_key_configured`，不返回明文 |
| 日志脱敏 | Jackett 的 API Key 在 URL query 中，**绝不把 URL 写日志**；错误消息只含状态码 |
| 结果脱敏 | Jackett 返回前用 `_strip_jackett_apikey()` 剥离 `torrent_url`/`details_url` 中的 key |
| 配置写回 | `nas_save_config` 只写已知字段，`enable_delete` 显式拒绝 |

注意：`nas_save_config` 允许写入 `jackett_api_key` / `tmdb_api_key`，这些值会落到 `config.yaml`。如果 NAS 上还有其他用户可读该文件，优先用环境变量方式。

## 8. 错误码表

| code | 触发 | 来源 |
| --- | --- | --- |
| `not_configured` | 配置缺失/非法/未启用 | `ConfigError` |
| `access_denied` | 路径不在白名单/命中排除 | `AccessDenied`（附 `path`） |
| `not_found` | 文件不存在 / 非目录 | `FileNotFoundError`、`NotADirectoryError` |
| `already_exists` | 目标已存在 | `FileExistsError` |
| `permission_denied` | OS 权限不足 | `PermissionError` |
| `invalid_arguments` | 参数非法 | `InvalidArguments` |
| `internal_error` | 未归类的 `ValueError` | 插件 `_safe` |
| `subhd_error` | SubHD 请求/解析失败 | `SubhdError` 默认码 |
| `download_not_archive` | 下载响应不是归档 | `SubhdError(code=...)` |
| `all_sites_failed` | 所有字幕站点失败 | `SubtitleError`（附 `site_errors`） |
| `resolver_error` | TMDB 解析失败 | `ResolverError` |
| `JACKETT_UNAVAILABLE` | Jackett 不可达 | `JackettError` |
| `JACKETT_TIMEOUT` | 请求超时/deadline 耗尽 | `JackettError` |
| `JACKETT_AUTH` | API Key 无效（401/403） | `JackettError` |
| `JACKETT_HTTP` | 其他 HTTP 错误 | `JackettError`（附 `status_code`） |
| `JACKETT_PARSE` | 返回格式异常 | `JackettError` |
| `JACKETT_CIRCUIT_OPEN` | 熔断中 | `JackettError`（附 `retry_after`） |
| `dependency_missing` | 缺 `curl_cffi` 等可选依赖 | SeedHub |
| `cf_blocked` | Cloudflare 拦截 | SeedHub |
| `disabled` | 对应站点/功能关闭 | SeedHub / dysbk |
| `csrf` | 写请求缺 `X-Nas-App` 头 | nas-app HTTP 层 |

错误信封扩展字段（按需出现）：`path`、`status_code`、`retryable`、`retry_after`、`site_errors`。

## 9. 审计

### 9.1 整理/清理/混流审计

- 文件：`organizer.audit_log_file`（默认 `logs/organize-audit.jsonl`）；
- 格式：每行一条 JSON（见 [03 §7](./03-api-reference.md#7-审计记录格式jsonl)）；
- 写入者：`organizer._audit()`，被 `cleanup`、`mux`、`rollback` 复用；
- 用途：`media_rollback` 的唯一数据源。

### 9.2 应用日志

| 形态 | 位置 |
| --- | --- |
| 插件 | 宿主日志（Octop） |
| nas-app | `<data>/logs/nas-app.log`（RotatingFileHandler，5MB × 3） |
| nas-mcp | `logging.file` 配置 |

### 9.3 审计读取

`GET /api/logs?kind=audit` 可读审计日志；`rollback.read_audit_records()` 在代码里读同一文件。

## 10. 威胁模型与已知边界

| 风险 | 现有防护 | 残留风险 |
| --- | --- | --- |
| Agent 误删媒体 | 默认只读 + 三级开关 + 五重门 + dry_run | 用户显式打开 `enable_delete` 后仍可能误删 |
| 路径穿越 / 软链逃逸 | `resolve()` 后再校验白名单 | 无（白名单外的真实路径一律拒绝） |
| 公网暴露 nas-app | 无 token 不启动 MCP；有 token 才鉴权 Web | 无 token 的 Web 端口裸奔（日志会警告） |
| CSRF | 写请求必须带 `X-Nas-App` | 自定义客户端需自行带头 |
| API Key 泄漏到日志 | 不记录 URL；返回前剥离 | 配置文件中仍存明文（建议环境变量） |
| 上游站点封禁 | 限速、UA、熔断 | SeedHub 的 CF 需要 `curl_cffi` |
| 大目录扫爆 | `max_scan_files` / `max_list_depth` / 分页 | 超限只是截断，不会 OOM |

**明确不支持**：

- 多用户权限体系（只有"有 token / 无 token"）；
- 删除回收站；
- 跨设备移动（整理只在同一文件系统内 `os.replace`/`shutil.move`）；
- 混流回滚。
