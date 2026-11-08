# 04 · 工具目录

本章按业务分组列出三种形态各自的工具。参数默认值以插件 `main.py` 签名与 nas-app `toolkit.py` 注册为准。

## 1. 形态与工具数

| 分组 | 插件 | nas-app | nas-mcp |
| --- | --- | --- | --- |
| 配置 | 2 | 1 | 1 |
| 文件 | 3 | 3 | 3 |
| 媒体 | 8 | 8 | 6 |
| 字幕 | 4 | 4 | 3 |
| 资源 | 8 | 8 | 8 |
| 整理 | 2 | 2 | 2 |
| 运维 | 2 | 2 | 0 |
| 任务 | 0 | 2（仅 MCP 端点） | 0 |
| **合计** | **29** | **28 + 2** | **23** |

命名差异：

| 插件 / nas-app | nas-mcp |
| --- | --- |
| `subhd_search` | `subtitle_search` |
| `subhd_download` | `subtitle_download` |

## 2. 配置组

### `nas_save_config`（仅插件）

保存/更新插件配置并写入插件目录 `config.yaml`，**即时生效，无需重启**。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `allowed_paths` | str | 路径白名单，换行/逗号分隔；至少一条 |
| `media_roots` | str | 媒体搜索根；留空 = 与白名单相同 |
| `exclude_paths` | str | 排除目录；留空保留现有值 |
| `enable_write` | 任意 | 允许字幕落盘（true/false/是/否） |
| `enable_organize` | 任意 | 允许整理执行移动 |
| `tmdb_enabled` / `tmdb_api_key` | 任意/str | TMDB 开关与 v3 Key |
| `jackett_enabled` / `jackett_url` / `jackett_api_key` | 任意/str | Jackett |
| `subhd_enabled` | 任意 | SubHD 开关 |
| `junk_extensions` | str | 垃圾扩展名（逗号/换行） |
| `active_download_grace_minutes` | 任意 | 活跃下载宽限分钟数 |
| `enable_delete` | 任意 | **显式拒绝**：高危开关只能手改 `config.yaml` |

写回策略：

1. 读出旧 YAML → 按行定位顶层 key / 段内 key → 只改受影响的行，**保留注释与未知字段**；
2. 写回后用 `yaml.safe_load` 回读校验；
3. 校验失败 → 回退为全量 `safe_dump`（牺牲注释，保证可用）。

两种调用方式：

- Agent 带参数调用；
- 管理员在 Octop「工具管理」表单填写后，Agent 无参调用（`_injected_config()` 读取注入值）。

### `nas_get_config`

只读，返回配置摘要：

```json
{
  "ok": true,
  "media_roots_expanded": ["/vol1/1000/media"],
  "limits": {...},
  "media": {...},
  "subhd": {"enabled": true, "base_url": "..."},
  "jackett": {"enabled": false, "url": "..."},
  "tmdb": {"enabled": false, "api_key_configured": false},
  "enable_write": false, "enable_organize": false, "enable_delete": false,
  "organizer": {...}, "cleanup": {...},
  "known_unreadable": ["/vol1/1000/locked"]
}
```

密钥只回显 `api_key_configured` 布尔值，**绝不回显明文**。

## 3. 文件组

### `nas_list_directory(path, recursive=False, limit=0, offset=0)`

列目录。`limit=0` 用 `limits.max_list_entries`（500）。返回 `entries[]` + `truncated` + `scan_truncated` + 权限错误摘要。

### `nas_find_files(keyword="", category="", extensions=None, root="", recursive=True, limit=0, offset=0)`

按文件名/类别/扩展名搜索。

- `category="video"|"subtitle"` 使用配置的扩展名集合；
- `extensions` 优先于 `category`，支持 `list` 或 JSON 字符串；
- 三者至少给一个；
- 命中 `skip_filename_patterns` 的文件被隐藏，只计入 `skipped_by_filter`。

### `nas_disk_usage(path="")`

按 `st_dev` 去重统计文件系统，返回 `filesystems[]`（总容量/已用/可用/挂载点/使用率）。

## 4. 媒体组

### `nas_scan_media(path="", recursive=True, probe=True, missing_subtitle_only=False, media_type="", limit=0, offset=0)`

扫描视频并分析媒体信息与字幕状态。`probe=false` 时只按外挂字幕判断（快），`probe=true` 调 ffprobe（慢）。

- `media_type` 过滤 `movie/tv`；
- `missing_subtitle_only=true` 只返回缺中文字幕的条目；
- 探测预算 `media.max_probe_files`。

### `nas_check_subtitle(path)`

单文件字幕详情：外挂字幕列表、内嵌字幕轨、是否含中文、当前选中的字幕。

### `nas_classify_region(path="", paths=None)`

判断国产/非国产。

证据优先级：**音轨语言 > TMDB（original_language / origin_country）> 文件名配音提示**。字幕语言不作为产地证据。输出 `region ∈ domestic/non_domestic/unknown` 与 `confidence`；批量上限 `media.max_classify_batch`。

### `media_quality_analyze(path="", paths=None)`

画质/音质标签。ffprobe 为准（分辨率/HDR/编码/位深/音轨/声道/Atmos），文件名补充来源类型。输出 `quality_score`（0-100）与 `quality_tier`。

### `duplicate_media_detect(path="", paths=None, recursive=True)`

同一作品多版本聚合。

- TMDB 启用 → 优先按 TMDB ID 归并（剧集细分到季集）；
- 未命中 → 退化为"规范化片名 + 年份 + 季集"，**绝不跨语言凭文件名合并**；
- 同组用时长做合理性校验（允许 ±15%，绝对差 ≥2s 才可能判 mismatch）。

### `media_library_audit(path="", paths=None, recursive=True)`

一键体检，八项统计（见 [03-api-reference.md §5.3](./03-api-reference.md#53-libraryaudit媒体库体检)）。TMDB 未启用时"无法识别"为 `null`。

### `media_missing_episodes(title="", season=None, path="", paths=None, limit=100, offset=0)`

以 TMDB 官方集数为基线检测缺集。TMDB 不可用时返回 `null` + `reason`，**不用文件名猜测缺失集**。

### `media_integrity_check(path="", paths=None, mode="basic", limit=100, offset=0)`

完整性检查。状态：`healthy / warning / corrupted / incomplete / unknown`。

- `basic`：看文件大小、扩展名（`.part` 等）、明显 0 字节；
- `deep`：ffprobe 探测，时长过短（`_VERY_SHORT_DURATION`）判异常。

## 5. 字幕组

### `subhd_search(video_path="", keyword="")` / `subtitle_search`

多站点字幕搜索（只读网络）。必须给 `video_path` 或 `keyword`。

- 按 `subtitle.site_order` 串行搜索；
- 统一评分，最高分 ≥ `switch_min_score`（0.9）提前停止；
- 返回 `search_status` / `site_attempts` / `candidates[].pack_hint`（详见 [03 §5.4](./03-api-reference.md#54-subtitlesearch_subtitles多站点字幕编排)）。

### `subhd_download(video_path, sid, lang="zh-Hans", ext="srt", overwrite=False)` / `subtitle_download`

下载字幕到视频同目录。

门控：目标站点 `enabled` + `enable_write=true` + 视频路径通过 `PathGuard`。

流程：

1. 按 `sid` 前缀路由站点；
2. 失败 → 按 `site_order` 换站重搜重下（failover）；
3. 解压归档（zip/rar/7z 嗅探）→ 选最佳条目 → 转 UTF-8 → 按 `filename_template` 命名；
4. 默认不覆盖已存在文件。

### `media_fill_subtitles(path="", dry_run=True, lang="zh-Hans", ext="srt", check_embedded=True)`

批量补全缺失中文字幕，按 `{剧, 季}` 聚合。

- `check_embedded=true`（默认）：通过 ffprobe 检查是否已有内嵌中文字幕，已有内嵌中字则跳过，避免重复下载。
- 策略分档：
  - `missing ≥ max(subtitle.season_pack_threshold=3, 50% × 季总集数)` → 走整季包；
  - 否则逐集补；
  - 缺 1 集**恒走单集**；
  - 默认 `dry_run`。

### `media_mux_subtitles(path="", video_path="", dry_run=True, delete_external=False, replace_original=False, force=False, limit=50)`

把外挂字幕混流进视频（MKV）。

- ASS 特效轨 + SRT 保底轨双轨，自动降级为单轨；
- 流拷贝（不重编码）；
- 写临时隐藏文件 → ffprobe 二次校验 → 原子覆盖；
- `delete_external=true` 才会删除原外挂字幕（默认 false，需 `enable_delete=true`）；
- `replace_original=true` 才会在 mp4 等格式成功产出 mkv 后删除原视频（默认 false，需 `enable_delete=true`）；
- 上述删除动作各自单独写审计（`MUX_DELETE_EXTERNAL` / `MUX_DELETE_ORIGINAL`）；
- `force=true` 才允许覆盖已含中字的文件；
- **MUX 操作不支持回滚**（返回体显式 `rollback_supported=false`）。

## 6. 资源组

### `resolve_media(query, year=0, media_type="")`

TMDB 片名解析。返回 `resolved/identity`，包含 `tmdb_id/media_type/title/original_title/year/popularity/vote_count/match_type/low_confidence`。

- `infer_media_type` 看到 `SxxExx` / `Season N` / `第N季` 会强制判为 `tv`；
- 票数 < 10（`_LOW_CONFIDENCE_VOTES`）标记 `low_confidence`；
- 进程级缓存。

### `jackett_search(query, year=0, media_type="", limit=0)`

Jackett 资源搜索（只返回磁力，不下载）。返回见 [03 §5.5](./03-api-reference.md#55-jackettsearch)。

内部：TMDB 解析（低置信不替换关键词）→ 生成多关键词 → deadline 预算内搜索 → 去重 → 排序。

### `seedhub_search(keyword)`

SeedHub 站内搜索（磁力 + 网盘入口）。依赖 `curl_cffi`，懒加载；`request_delay_seconds` 下限 1.0s。

### `seedhub_magnets(movie_id, limit=0, offset=0)`

取某作品的磁力列表。

### `seedhub_pans(movie_id, limit=0, offset=0)`

取某作品的网盘链接列表，并调用 `pan_probe` 探测可用性。

### `dysbk_search(query, limit=20)`

网盘资源分享社（Flarum）搜索。

### `dysbk_links(discussion_id)`

取帖子中的网盘直链，过滤 quark / baidu / xunlei / uc。

### `media_search(query)`

**找资源的首选入口**：三路并行（SeedHub + 网盘社 + Jackett）。

- 单路失败不影响其他路；
- 中文查询会跳过 SeedHub（英文站），`seedhub_skipped_by_language=true`；
- 三重去重（infohash / 网盘链接 / 标题），每作品 top3，总闸 20；
- 各路预算：dysbk 10s、SeedHub 20s、Jackett 沿用自身 deadline。

## 7. 整理组

### `organize_media(path, mode="all", dry_run=True, preexisting_empty_scope="library_root", language_split=False)`

把杂乱下载目录整理成标准媒体库结构。

- `language_split=true`：按地区/语言分流（例如分为 `华语电影`/`外语电影` 或 `国剧`/`欧美剧` 等）；剧集整季归入统一目录，避免单集跨语言割裂。
- `mode`：

| 值 | 行为 |
| --- | --- |
| `all` | 电影 + 剧集都规划 |
| `movie` | 只规划电影 |
| `tv` | 只规划剧集 |

`dry_run=true`（默认）时只生成计划（L1），不执行；`dry_run=false` 才进入执行（L2/L3，需 `enable_organize` / `enable_delete`）。

计划动作：`move`、`rename_dir`、`identical`、`duplicate`、`blocked`、`cleanup_dir`、`skip`。

执行前统一写权限预检；逐动作写审计；`operation_id` 可用于回滚。

### `find_media(title, season=0, episode=0)`

在媒体库中定位某影片/剧集文件，返回 `matches[]` + `truncated` + 错误摘要。

## 8. 运维组

### `media_cleanup(path="", files=None, dry_run=True, limit=100, offset=0)`

垃圾清理分类：`media` / `media_junk` / `personal_video` / `unknown`。

五重门（全部通过才允许真删）：

1. 路径在白名单；
2. `enable_delete=true`；
3. 证据字段完整（分类依据齐全）；
4. 不在活跃下载宽限窗口内（`mtime` 距今 > `active_download_grace_minutes`）；
5. 未命中特殊目录（`special_dir_names`）与 `skip_filename_patterns`。

**永久删除不可回滚**。

### `media_rollback(operation_id="", dry_run=True)`

按 `operation_id` 回滚整理动作。

支持：`MOVE` / `MOVE_RELATED` / `RENAME_DIR` / `CREATE` / `RMDIR_EMPTY` / `MKDIR`。
不支持：任何删除类动作、`MUX`。

8 项检查：`operation_id_exists`、`action_supported`、`source_target_exists`、`path_allowed`、`conflict`、`permission`、`size`、`mtime`。批内任一冲突 → **整体中止**（不做部分回滚）。默认 `dry_run`。

## 9. 任务组（nas-app MCP 端点 + Octop 插件）

### `nas_job_status(job_id)`

查询任务状态：`queued / running / succeeded / failed / canceled / interrupted`，含 `result` / `error` / `duration_ms` / `progress`。
`interrupted` 是独立终态：插件进程重启后未完成任务恢复为此状态，并带 `error.code=interrupted`。

### `nas_job_list(limit=20)`

列出最近任务（最多 100 条，列表不内联完整结果）。

### `nas_job_cancel(job_id)`（插件专属）

合作式取消：排队任务立即结束；运行中任务在工具循环边界停下，返回已完成部分
（`result.canceled/partial=true`），并清理混流临时文件 `.<stem>.mux_tmp.mkv`。

插件侧 6 个长工具（`nas_scan_media` / `media_integrity_check` / `media_cleanup` /
`media_fill_subtitles` / `media_mux_subtitles` / `organize_media`）支持可选
`background=true`：立即返回 `job_id`，不传时保持同步调用行为。队列落盘在
`$OCTOP_DATA_DIR/nas-media/jobs/`，2 个 worker、写类任务共用一把写锁、待处理上限 20、保留最近 100 个或 7 天。

## 10. 典型工作流

### 10.1 媒体库体检

```text
nas_scan_media(probe=false)            # 快速摸清规模与字幕缺失
media_library_audit()                  # 八项体检
duplicate_media_detect()               # 重复版本
media_missing_episodes(title="...")    # 缺集（需 TMDB）
media_integrity_check(mode="basic")    # 损坏/不完整
```

### 10.2 补字幕

```text
subhd_search(video_path="/vol1/1000/tv/Show/S01E01.mkv")
  → 取最高分 candidate.sid
subhd_download(video_path="...", sid="subhd:xxxx")
  → 或 media_fill_subtitles(path="/vol1/1000/tv/Show", dry_run=false)
  → 或 media_mux_subtitles(video_path="...", dry_run=false)
```

### 10.3 整理下载目录

```text
organize_media(path="/vol1/1000/downloads", dry_run=True)      # L1：审阅 plan[]
organize_media(path="...", dry_run=false)                     # L2：执行
# 出问题：
media_rollback(operation_id="op-...", dry_run=true)           # 先 dry_run
media_rollback(operation_id="op-...", dry_run=false)          # 再执行
```

### 10.4 找资源

```text
media_search(query="沙丘 2")            # 首选：三路聚合
# 需要更细的筛选时：
jackett_search(query="Dune Part Two", year=2024)
seedhub_search(keyword="沙丘2")
dysbk_search(query="沙丘2")
```

## 11. 风险等级与异步标记（nas-app）

| 工具 | risk | long_running |
| --- | --- | --- |
| `nas_get_config` / `nas_list_directory` / `nas_find_files` / `nas_disk_usage` | read | 否 |
| `nas_scan_media` | read | **是** |
| `nas_check_subtitle` / `nas_classify_region` | read | 否 |
| `media_quality_analyze` / `duplicate_media_detect` / `media_library_audit` | read | **是** |
| `media_missing_episodes` / `media_integrity_check` | read | **是** |
| `media_cleanup` | **delete** | **是** |
| `media_rollback` | write | **是** |
| `subhd_search` | read | **是** |
| `subhd_download` | write | 否 |
| `media_fill_subtitles` / `media_mux_subtitles` | write | **是** |
| `resolve_media` | read | 否 |
| `jackett_search` | read | **是** |
| `organize_media` | write | **是** |
| `find_media` | read | **是** |
| `seedhub_search` / `seedhub_magnets` / `seedhub_pans` | read | 否 |
| `dysbk_search` / `dysbk_links` | read | 否 |
| `media_search` | read | **是** |

`risk` 只用于前端提示与确认弹窗，**真正的门控在内核**（`enable_*` + `PathGuard`），不能仅靠前端拦截。
