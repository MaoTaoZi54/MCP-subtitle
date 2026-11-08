# NAS 媒体工具箱 · 独立应用版（备选形态）

> **维护模式说明**：本项目主力形态是 Octop 插件 [nas-octop-plugin](../nas-octop-plugin)；本应用作为备选保留，继续修复缺陷与安全问题时维护，**不再优先扩展新业务工具**。能力内核与插件同源（0.19.29）。

把媒体能力包装成一个可独立部署的应用，同时服务 **Web UI** 与 **Octop（MCP）**：

- **飞牛应用商店（fnOS）**：`.fpk` 安装包，安装向导设置访问令牌，应用内授权媒体目录后自动以 Docker 容器运行。
- **Docker**：单镜像部署，自带 ffmpeg，挂载媒体库即可使用。
- **Web UI + HTTP API**：浏览器操作全部工具；长任务转后台队列，避免反向代理超时。
- **内置 MCP Streamable HTTP**：`127.0.0.1:8670/mcp`，Octop 作为自定义 MCP 直接连接，与 Web UI 共用同一套配置和后台任务，无需另装插件。

核心能力与 Octop 插件 `nas-octop-plugin` 同源（内核 0.19.29），共 **28 个业务工具**；MCP 端点额外提供 `nas_job_status` / `nas_job_list` 两个任务查询工具（共 30 个）。插件版多一个 `nas_save_config`，本应用刻意不开放，配置只在 Web UI / HTTP API 完成。

### 三种形态怎么选

| 形态 | 工具与传输 | 配置方式 | 适合谁 |
| --- | --- | --- | --- |
| `nas-octop-plugin` | 29 工具（含 `nas_save_config`），Octop 进程内插件 | 对话 / 工具表单 / 编辑插件 config.yaml | 只用 Octop，追求零额外进程 |
| `nas-mcp` | **23 工具**（stdio 装配层），独立进程 | `--config` + 环境变量 | 客户端只支持 stdio；接受能力较少 |
| `nas-app`（本应用） | 28 业务工具 + 2 任务工具；8669 Web/REST + 8670 MCP | Web UI「配置」页 | 要 Web UI / Docker / 飞牛商店，或 Octop 与远程客户端共用 |

三者可以同时安装、独立配置；不要让它们同时写同一批媒体目录（整理/清理/混流），只读操作可并行。

## 快速开始（Docker）

需求：Docker 24+；媒体目录所在磁盘可被 Docker 挂载。

```bash
cd nas-app
cp .env.example .env
# 编辑 .env：把 NAS_APP_TOKEN 改成至少 8 位的随机字符串
# 编辑 docker-compose.yml：把 /vol1/1000/媒体 换成你的媒体库路径（可多行）
docker compose up -d --build
```

打开 `http://<NAS-IP>:8669`，输入 `NAS_APP_TOKEN` 登录。

不用 compose 时（镜像需已发布，或先执行 `scripts/build-image.sh` 构建本地镜像）：

```bash
docker run -d --name nas-app --restart unless-stopped \
  -p 8669:8669 \
  -p 127.0.0.1:8670:8670 \
  -e NAS_APP_TOKEN='至少8位的随机令牌' \
  -e NAS_APP_ALLOWED_PATHS='/media,/vol1/1000/媒体' \
  -e NAS_APP_MCP_ENABLED=1 \
  -e NAS_APP_MCP_HOST=0.0.0.0 \
  -e NAS_APP_MCP_PORT=8670 \
  -e TZ=Asia/Shanghai \
  -v "$PWD/data:/data" \
  -v /vol1/1000/媒体:/vol1/1000/媒体 \
  ghcr.io/maotaozi54/nas-app:0.19.0
```

要点：

- 容器内路径与宿主机保持一致时，Web 界面里填写的路径无需换算；也可以把媒体挂到 `/media` 并只授权 `/media`。
- `NAS_APP_ALLOWED_PATHS` 是路径白名单（逗号或换行分隔），所有工具只能访问白名单内路径；后续可在「配置」页修改。
- `/data` 存放 `config.yaml`（白名单、数据源密钥、写开关）与 `jobs/` 任务记录。首次启动自动生成，写开关默认**全部关闭**。
- 健康检查：`/healthz`（Dockerfile 已内置 HEALTHCHECK）。

构建/推送多架构镜像：

```bash
scripts/build-image.sh --tag 0.19.0 --image ghcr.io/maotaozi54/nas-app --push
```

## Octop 直接调用（内置 MCP）

应用内嵌一个 **MCP Streamable HTTP** 端点，Octop 把它当作「自定义 MCP」连接即可，不需要安装 `nas-octop-plugin`。Web UI 与 MCP 共用同一个 `ToolRegistry`、白名单、写开关和 `JobManager`，Octop 提交的任务也会出现在 Web UI 的「后台任务」里。

### Octop 配置步骤

1. 确认应用已启动，且 `NAS_APP_TOKEN` 已设置（MCP 强制鉴权，无 token 时不启动）。
2. Octop → 设置 → 连接器 → 自定义 MCP → 添加：
   - 传输：`streamable_http`
   - URL：`http://127.0.0.1:8670/mcp`
   - Header：`Authorization: Bearer <NAS_APP_TOKEN>`
3. 探测成功后，工具列表应出现 30 个带 `nas-app_` 前缀的工具（28 业务 + `nas_job_status` / `nas_job_list`）。

### 连接方式与部署边界

- 默认只监听/发布到宿主机 `127.0.0.1:8670`，Octop 必须运行在同一台机器（或与容器共享网络）才能用这个地址。
- 如果 Octop 跑在另一个 Docker 容器里，宿主 loopback 映射不可达：需要把映射改为宿主机 LAN 地址或 `0.0.0.0`，并自行加防火墙/TLS；Bearer 是唯一一道应用层保护。
- 容器内固定监听 `0.0.0.0:8670`（端口映射需要）；compose 只改宿主机端口 `NAS_APP_MCP_HOST_PORT`，容器内 `NAS_APP_MCP_PORT` 保持 8670。
- MCP 不能修改配置/白名单/密钥：没有 `nas_save_config`，这些只在 Web UI 完成，避免密钥进入对话。

### 长任务

超过 240 秒的任务会先返回 `{"status": "queued|running", "job_id": "..."}`；用 `nas_job_status` 轮询 `job_id`，不要重复提交。任务的最终结果与 Web UI「后台任务」一致。

### 诊断

```bash
# 列出工具（前 10 个）并可选调用一次
py -3 scripts/probe_mcp.py --url http://127.0.0.1:8670/mcp --token "$NAS_APP_TOKEN"
py -3 scripts/probe_mcp.py --url http://127.0.0.1:8670/mcp --token "$NAS_APP_TOKEN" \
  --call nas_list_directory --args '{"path": "/media"}'

# 未带 Bearer 应返回 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8670/mcp
```

## 飞牛应用商店（fnOS）

### 构建 .fpk

```bash
cd nas-app
packaging/fnos/build-fpk.sh                      # 在线包：安装后从镜像仓库拉取
packaging/fnos/build-fpk.sh --with-image         # 离线包：附带 docker save 的镜像 tar（体积大）
packaging/fnos/build-fpk.sh --push-image         # 构建并推送 amd64+arm64 镜像后打包
packaging/fnos/build-fpk.sh --image <镜像名> --version <版本> [--with-image]
```

产物在 `dist/nas-media-<version>.fpk`，脚本同时输出 sha256。

在线包安装时从 `app/docker/image.conf` 的 `IMAGE` 拉取镜像，因此发布前必须先把镜像推送到该地址；尚未推送时请用 `--with-image` 打离线包。

> 有官方 `fnpack` 时脚本调用它打包（上架商店建议用这种方式）；没有 `fnpack` 时按官方包结构手工打包：`app/` 内容打成外层 `app.tgz`（解压后为 `docker/`、`ui/`），并写入 `manifest` 的 `checksum`（`app.tgz` 的 md5）。上架前仍建议安装 `fnpack` 重新构建。`--with-image` / `--push-image` 需要本机 Docker。

### 安装与授权

1. fnOS「应用商店 → 手动安装」上传 `.fpk`。
2. 安装向导设置**访问令牌**（≥8 位），并留意写操作默认关闭的提示。
3. 安装完成后到「应用设置 → 授权目录」把媒体库目录授权给本应用。
   应用会把授权目录**按相同路径读写挂载**进容器；未授权目录时容器无法启动。
4. 打开应用（端口 8669）用令牌登录；可在「配置」页调整白名单、数据源与写开关。

生命周期行为：

- 启动/停止/重启由应用自身的 `cmd/main` 管理容器（`--restart unless-stopped`）；升级先停旧容器，配置变更后自动重启。
- 应用数据目录固定为宿主机 `/var/apps/nas-media/var`，以数据卷挂载到容器 `/data`；首次启动会生成 `config.yaml`、`logs/nas-app.log` 与 `jobs/` 任务记录，升级保留该目录。
- 访问令牌保存在应用配置目录 `token` 文件（权限 600），卸载时默认保留应用数据；向导勾选「同时删除应用数据」才会清理容器与 `/data`。
- 端口默认 8669，可在 manifest 的 `service_port` 与 `cmd/common.sh` 的 `CONTAINER_PORT` 同步修改。
- MCP 默认映射到宿主机 `127.0.0.1:8670`；启动前会检查端口，被占用时自动禁用 MCP 并继续启动 Web UI。端口号保存在应用配置目录的 `mcp_port` 文件。

### 上架前检查

- [x] 用官方 `fnpack build` 产出 `.fpk`（已在飞牛 fnOS 12 + `fnpack 1.2.4` 验证）。
- [ ] 在目标机型走完应用商店「安装 → 授权目录 → 启动 → 升级 → 卸载」全流程。
- [ ] 确认镜像已推送到公开仓库（离线包则确认 `app/docker/nas-app-image.tar` 存在）。
- [ ] 更新 `manifest` 的 `version` / `changelog`，以及 `app/docker/image.conf` 的 `IMAGE` 标签。
- [ ] 图标：`make_icons.py` 已生成 `ICON.PNG`、`ICON_256.PNG`、`app/ui/images/icon_*.png`。

## 配置

配置文件：`<data>/config.yaml`（Docker 默认 `/data/config.yaml`）。Web「配置」页保存前会做临时文件 + 校验 + 原子替换，密钥不会回显。

| 环境变量 | 说明 |
| --- | --- |
| `NAS_APP_TOKEN` | 访问令牌；设置后 Web 与 API 均需鉴权 |
| `NAS_APP_TOKEN_FILE` | 从文件读取令牌（应用安装方式使用） |
| `NAS_APP_HOST` / `NAS_APP_PORT` | 监听地址/端口，默认 `0.0.0.0:8669` |
| `NAS_APP_DATA` | 数据目录，默认 `./data` |
| `NAS_APP_CONFIG` | 配置文件路径，默认 `<data>/config.yaml` |
| `NAS_APP_ALLOWED_PATHS` | 首次生成配置时的白名单（逗号或换行分隔） |
| `NAS_APP_EXCLUDE_PATHS` | 首次生成配置时的排除目录 |
| `NAS_APP_ENABLE_WRITE` / `_ORGANIZE` / `_DELETE` | 首次生成配置时开启写操作门（默认关闭） |
| `NAS_APP_MCP_ENABLED` | 是否启动内置 MCP 端点，默认 `1`；无 `NAS_APP_TOKEN` 时不启动 |
| `NAS_APP_MCP_HOST` | MCP 监听地址，默认 `127.0.0.1`；容器内需 `0.0.0.0` |
| `NAS_APP_MCP_PORT` | MCP 监听端口，默认 `8670`（容器内固定，宿主机映射见 `NAS_APP_MCP_HOST_PORT`） |
| `NAS_APP_MCP_PATH` | MCP 路径，默认 `/mcp` |

compose 额外提供 `NAS_APP_MCP_HOST_PORT`（只影响宿主机映射，默认 8670）。

## 界面风格：专业版 / 友好版

Web UI 提供两套可切换的界面风格，切换按钮在顶栏右上角（登录页也有一份，未登录就能先选）：

| 风格 | 特点 | 适合 |
| --- | --- | --- |
| **友好版**（默认） | 暖色底（珊瑚 + 琥珀）、大圆角、圆润导航与按钮、宽松留白、品牌标语 | 日常整理媒体库、给家人或新同事使用 |
| **专业版** | 冷色底（蓝色）、紧凑排版、小圆角、选中项带左侧竖条、表头大写 | 熟悉命令行、需要一屏看更多信息的场景 |

- 选择结果保存在浏览器 `localStorage['nas-ui-mode']`，刷新、重新登录后保持；`static/mode.js` 在首屏渲染前应用，避免主题闪烁。
- 只影响前端呈现，不改变任何工具行为、权限与配置。
- 深色模式跟随系统 `prefers-color-scheme`，两套风格各自有对应的深色调色板。

命令行：`--host --port --data --config --token --mcp-host --mcp-port --mcp-path --no-mcp`（其余首次生成参数通过上面的环境变量设置）。

安全模型：

- 白名单 `allowed_paths` + 排除 `exclude_paths` 强制约束所有文件访问；`..` 越界与符号链接逃逸会被拒绝。
- 写操作分三道门：`enable_write`（字幕下载/混流执行）、`enable_organize`（目录整理）、`enable_delete`（清理删除、混流删除原文件/原外挂字幕），默认全关；建议先 `dry_run` 查看计划。
- 令牌登录使用 HttpOnly Cookie；Cookie 方式发起的写请求必须带 `X-Nas-App: 1` 头（CSRF 防护），Bearer 调用不受此限。

## HTTP API

- `GET /healthz`：公开健康检查。
- `GET /api/meta`：版本、工具清单与参数定义；`GET /api/config`、`PUT /api/config`：配置。
- `GET /api/overview`：概览与磁盘信息。
- `POST /api/tools/<name>`：同步执行工具；`?async=1` 提交后台任务。
- `GET /api/jobs`、`GET /api/jobs/<id>?wait=<秒>`：任务列表/等待结果。
- `GET /api/logs?kind=app|audit&lines=<n>`：日志。

鉴权：`Authorization: Bearer <token>`，或先 `POST /login` 获取 Cookie。请求体上限 2 MB。

## 工具（28 业务 + 2 MCP 任务查询）

| 分类 | 工具 |
| --- | --- |
| 文件 | `nas_list_directory` `nas_find_files` `nas_disk_usage` |
| 媒体扫描 | `nas_scan_media` `nas_check_subtitle` `nas_classify_region` `media_quality_analyze` `duplicate_media_detect` `media_library_audit` `media_missing_episodes` `media_integrity_check` |
| 字幕 | `subhd_search` `subhd_download` `media_fill_subtitles` `media_mux_subtitles` |
| 资源 | `resolve_media` `jackett_search` `seedhub_search` `seedhub_magnets` `seedhub_pans` `dysbk_search` `dysbk_links` `media_search` |
| 整理/运维 | `organize_media` `find_media` `media_cleanup` `media_rollback` |
| 配置 | `nas_get_config` |
| MCP 任务 | `nas_job_status` `nas_job_list`（仅 MCP 端点） |

## 开发与测试

```bash
# 同步内核（从 Octop 插件复制 nas_mcp）
scripts/sync-core.sh

# 安装依赖（任选其一）
uv sync --extra dev
pip install -r requirements.txt pytest

# 测试
pytest

# 本地运行
python -m nas_app --data ./data --token dev-token-123

# 生成图标
python scripts/make_icons.py
```

测试覆盖：配置生成与令牌、工具白名单/参数/密钥合并、岗位任务持久化、HTTP 鉴权/CSRF/静态目录穿越、界面风格切换（专业版/友好版入口与 mode.js 静态资源）、MCP 签名映射/Bearer 中间件/长任务路由/端到端工具调用、FPK 清单与生命周期脚本（非 Windows 环境用假 docker 验证挂载参数）。已在飞牛 fnOS（Debian 12 / Python 3.11）实机跑通 60 项测试，并用 `fnpack 1.2.4` 打包、构建 Docker 镜像、启动容器后验证 Web/MCP 鉴权与真实媒体目录调用；`scripts/live_api_test.py` 可在实机对已安装应用做 39 项在线 API 冒烟（鉴权/CSRF/只读工具/写门/异步任务）。

## 已知限制

- 开发机（Windows）未安装 Docker/fnpack/ffmpeg；镜像构建、`fnpack` 打包与容器内 MCP 此前已在飞牛 fnOS 实机验证，本轮版本号统一到 0.19.29 后仍建议在目标机型复测授权目录与端口占用场景。
- fnOS 安装方式使用 Docker 运行；首次安装必须先在 fnOS 中授权媒体目录，否则容器不会启动。
- 扫描画质/编码依赖 ffprobe：Docker 镜像已内置 ffmpeg，直接运行 Python 时需自备。
- SeedHub 的 Cloudflare 校验依赖 `curl_cffi`；缺失时仅 `seedhub_*` 工具降级，其他功能不受影响。
- 内置 MCP 依赖 `mcp==1.28.1`，镜像体积因此增加（pydantic/starlette/uvicorn 等）；`mcp` 缺失或 8670 被占用时 MCP 不启动，Web UI 照常运行。
- `nas-mcp`（stdio 版）提供 **28 个业务工具**，与插件/本应用的业务面一致（字幕两工具为 `subtitle_*`），仅缺插件专属的配置/任务/日志/诊断工具，详见根 README。
