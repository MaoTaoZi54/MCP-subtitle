# nas-media · Octop 插件

飞牛 NAS 影视媒体智能分析 + 资源搜索工具箱，以 **Octop 原生插件**形态运行（Octop 进程内加载，无需独立服务/连接器）。

与独立版 [nas-mcp](../nas-mcp)（stdio MCP Server）**共享同一份能力包代码**（本目录 `nas_mcp/`），功能完全对齐：**15 个工具，v0.10.0**。两种形态并存，可按需选择。

```
对话："扫一下媒体库哪些片子没有中文字幕"
        │
Octop Agent ──插件进程内调用──> nas-media（本插件）──> 文件系统 / ffprobe / TMDB / Jackett / SubHD
                                              （config.yaml 白名单 + 排除目录强制隔离）
```

## 两种形态怎么选

| | 独立版 nas-mcp | 本插件 nas-media |
|---|---|---|
| 运行方式 | 独立 Python 进程，stdio 连接器 | Octop 进程内插件，零额外进程 |
| 安装 | 配 custom-mcp JSON | `octop plugin install` 或 Dashboard 上传 ZIP |
| 配置 | `--config` 指定 config.yaml + env | 对话调用 `nas_save_config` / 编辑插件目录 config.yaml |
| 升级影响 | 完全独立 | 随 Octop 插件机制加载，文件仍在独立插件目录 |
| 适用 | 多客户端共用、想严格进程隔离 | 只给 Octop 用、追求最简部署 |

## 工具（15 个）

| 工具 | 类型 | 说明 |
|---|---|---|
| `nas_save_config` | 配置 | **插件专属**：对话式保存白名单/开关/密钥到 config.yaml（支持工具管理表单注入） |
| `nas_get_config` | 只读 | 查看白名单、排除目录（含 glob 展开结果）、扩展名、限额、各服务开关与密钥状态 |
| `nas_list_directory` | 只读 | 目录浏览（递归/分页），越界符号链接自动隐藏 |
| `nas_find_files` | 只读 | 关键词/类别/扩展名搜文件；自动跳过家庭录像与成人内容命名 |
| `nas_disk_usage` | 只读 | 各卷磁盘空间（总/已用/可用/使用率） |
| `nas_scan_media` | 只读 | ffprobe 扫描：编码/分辨率/HDR10/HDR10+/Dolby Vision/HLG/码率/音轨/内嵌字幕 + 外挂字幕 |
| `nas_check_subtitle` | 只读 | 单个视频的字幕详情 |
| `nas_classify_region` | 只读 | 国产/非国产判定（音轨 > TMDB > 文件名提示，带 confidence） |
| `media_quality_analyze` | 只读 | 单/批量视频质量标签 + 0-100 分与档位、low_quality 结论 |
| `duplicate_media_detect` | 只读 | 同一作品多版本聚合、时长校验、每组建议保留的 best_path |
| `media_library_audit` | 只读 | 媒体库八项体检统计 + 低清样例与重复组 |
| `resolve_media` | 只读 | TMDB 中文片名 → 英文标准名（不猜测） |
| `jackett_search` | 只读 | Jackett 资源搜索，只返回磁力链绝不下载；附质量维度与评分、seeders 缺失计数 |
| `subhd_search` | 只读 | SubHD 字幕搜索，加权评分排序 |
| `subhd_download` | **写** | 字幕下载到视频同目录（唯一写操作，需 `enable_write` 与 `subhd.enabled` 双开关） |

## 前置条件（NAS 系统层面）

- Octop ≥ 1.0.1（插件系统 GA 的版本）
- Python ≥ 3.12（Octop 自带）
- `ffprobe`（媒体分析必需，随 ffmpeg）：fnOS 应用中心或 `apt install ffmpeg`
- `unar`（RAR 字幕解压）：`apt install unar`
- PyYAML（Octop 运行环境已自带，安装时会自动确认 requirements.txt）

## 安装

### 方式一：ZIP 安装（推荐）

ZIP 包结构为单一插件根目录（`nas-media/plugin.yaml` …）。

```bash
# CLI
octop plugin install ./nas-media-0.10.0.zip --force
octop plugin list
```

或 **Dashboard → Admin → Plugins → 安装**，粘贴 ZIP 的直接下载地址
（GitHub 必须用 `raw.githubusercontent.com`，不能用 `/blob/` 页面）。

### 方式二：目录安装（开发/调试）

```bash
# 解压/上传整个目录到 ~/.octop/plugins/nas-media/
octop plugin install ~/.octop/plugins/nas-media --force
```

安装后在 **Dashboard → 工具管理** 里为具体 Agent 启用本插件的工具（全局启用后默认全开）。

## 配置（三种途径，任选）

**① 对话式（最省事）**——直接对 Agent 说：

> 帮我配置 NAS 插件：白名单 `/vol[1-9]*/1000`，排除 `/vol0*` 和 `/vol[1-9]*/1000-*`，TMDB key 是 xxx，Jackett 地址 http://192.168.x.x:9117 key 是 yyy

Agent 会调用 `nas_save_config` 写入，立即生效、无需重启。密钥落盘 `config.yaml` 并尝试 `chmod 600`。

**② 工具管理表单**——Admin 工具管理页 `nas_save_config` 的配置表单填好后，让 Agent 执行一次"保存 NAS 插件配置"即可。

**③ 手工编辑**——SSH 修改 `~/.octop/plugins/nas-media/config.yaml`（模板见同目录 `config.example.yaml`），改动按 mtime 即时生效。所有高级字段（限额、跳过模式、超时、文件名模板）只能走此途径。

### fnOS 路径约定（默认推荐值）

| 配置 | 推荐值 | 作用 |
|---|---|---|
| `allowed_paths` | `/vol[1-9]*/1000` | 本地存储空间管理员目录，**新盘自动纳入** |
| `exclude_paths` | `/vol0*`、`/vol[1-9]*/1000-*` | 排除 USB 外接与网盘远程挂载（hash 变化免疫） |
| `media_roots` | 同白名单 | 动态展开，加盘无需重启 |

## 权限注意（沿用 nas-mcp 部署经验）

Octop 本地版后端以 **octop-native** 用户运行，需保证其可读能力代码与白名单数据卷：

```bash
chmod -R a+rX ~/.octop/plugins/nas-media
chmod a+rx /vol1/1000 /vol2/1000     # 按实际卷调整
```

安装/改权限后在 Dashboard **重新保存一次插件/连接器配置**触发重载（Octop 会缓存失败结果）。

## 安全模型（与 MCP 版一致）

- 路径白名单为最高优先级，先解析符号链接再判定；glob 模式与字面量可混用；`exclude_paths` 无条件优先
- 默认只读，唯一写操作 `subhd_download` 需双开关且不覆盖已有文件
- 扫描/搜索有深度、条数、ffprobe 探测预算（含失败计数）等限额
- 家庭录像/成人内容文件名默认跳过
- API Key 绝不进入返回结果与日志
- 所有错误结构化：`{"ok": false, "error": {"code", "message"}}`；未配置时代码为 `not_configured`

## 开发验证

```bash
# 轻量加载验证（只需 PyYAML）
uv run --project ../nas-mcp python tests/local_load_check.py

# 真实 harness 兼容性验证（一次性临时环境）
uv venv .sdk/venv --python 3.12
uv pip install --python .sdk/venv/Scripts/python.exe "octop-harness>=1.0.1"
.sdk/venv/Scripts/python.exe tests/harness_check.py
```

能力包 `nas_mcp/` 与 `../nas-mcp/src/nas_mcp/` 保持同源；升级时同步该目录（除
`server.py` / `__main__.py` / `logging_setup.py` 等 stdio 装配层文件外，其余模块直接复制），
版本号在 `plugin.yaml` 与 `nas_mcp/__init__.py` 同步。

## 非目标（第一版）

Docker 管理、自动下载/投递、通知渠道、Shell 执行、批量改名——均不做，与独立版 nas-mcp 保持同一边界。
