# 06 · 运行与部署

## 1. 前置依赖

| 依赖 | 必需性 | 说明 |
| --- | --- | --- |
| Python 3.10+（插件、nas-app）/ 3.12+（nas-mcp） | 必需 | 代码使用 `X \| None` 注解；版本下限以各 `pyproject.toml` 为准 |
| `PyYAML>=6.0` | 必需 | 配置读写 |
| `curl_cffi>=0.10` | SeedHub 工具必需 | 懒加载；未安装时仅 `seedhub_*` 报 `dependency_missing` |
| `ffprobe` | 媒体分析类必需 | 建议随 `ffmpeg` 一起安装 |
| `ffmpeg` | `media_mux_subtitles` 必需 | 混流 |
| `unar` | 解压 RAR 字幕包时需要 | 缺失时 `SubhdError` 提示安装 |
| `mcp` | nas-app / nas-mcp | nas-app 固定 `mcp==1.28.1`；nas-mcp `mcp>=2.3.0` |
| NAS 存储挂载 | 必需 | 白名单目录需对服务进程可读（写操作另需可写） |

检查 ffprobe：

```bash
ffprobe -version
ffmpeg -version
```

## 2. 形态 A：Octop 插件

### 2.1 安装

两种方式（二选一）：

1. **ZIP 安装**：把 `nas-octop-plugin/` 打包为 zip（或直接用仓库根目录的 `nas-octop-plugin.zip`），在 Octop 插件页上传安装。
2. **目录安装**：把整个 `nas-octop-plugin/` 目录放到 `~/.octop/plugins/nas-media/`。

关键文件：

```
~/.octop/plugins/nas-media/
├── plugin.yaml        # id: nas-media, version: 0.19.0, entry: main.py
├── main.py
├── config.yaml        # 首次需从 config.example.yaml 复制并修改
└── nas_mcp/
```

安装依赖（在插件目录内）：

```bash
pip install -r requirements.txt
```

### 2.2 配置

三选一：

1. **对话式**：让 Agent 调用 `nas_save_config` 配置常规项（开关、密钥、路径）。注意白名单 `allowed_paths` 只能由管理员在表单或 `config.yaml` 中扩大/写 glob——Agent 通道只能在现有白名单内收窄，且不能自行开启 `enable_write`。
2. **表单**：在 Octop「工具管理 → nas-media」填写 `CONFIG_FIELDS` 表单，然后无参调用 `nas_save_config`。
3. **手写**：复制 `config.example.yaml` 为 `config.yaml` 并修改。

最小可用配置：

```yaml
allowed_paths:
  - /vol[1-9]*/1000
media_roots:
  - /vol[1-9]*/1000
exclude_paths:
  - /vol0*
  - /vol[1-9]*/1000-*
subhd:
  enabled: true
enable_write: false
enable_organize: false
enable_delete: false
```

### 2.3 验证

```text
nas_get_config()
nas_list_directory(path="/vol1/1000")
nas_disk_usage()
nas_scan_media(path="/vol1/1000/media", probe=false, limit=5)
```

### 2.4 升级

- 直接替换插件目录/ZIP 即可；配置在插件目录内，不会被覆盖。
- **红线**：`main.py` 第 34–41 行在**任何 `nas_mcp` import 之前**清理 `sys.modules` 中的旧 `nas_mcp*`。这段逻辑不能移动、不能删除，否则 Octop 热升级后会加载到内存中的旧包（`ImportError` 或类身份不一致），只能重启 Octop 才能恢复。

## 3. 形态 B：nas-app

### 3.1 裸机 / 虚拟环境

```bash
cd nas-app
python -m venv .venv
.venv/Scripts/activate       # Windows
# source .venv/bin/activate  # Linux
pip install -r requirements.txt

export NAS_APP_TOKEN="至少8位随机串"
export NAS_APP_ALLOWED_PATHS="/vol1/1000/media"
export NAS_APP_ENABLE_WRITE=0
python -m nas_app --host 0.0.0.0 --port 8669
```

首次启动会：

1. 创建 `<data>/config.yaml`（`chmod 600`）；
2. 创建 `<data>/logs/` 与 `<data>/jobs/`；
3. 启动 HTTP 服务；
4. 若设置了 token，同时启动 MCP 端点。

浏览器打开 `http://<NAS>:8669`，用 `NAS_APP_TOKEN` 登录。

常用参数：

| 参数 | 等价环境变量 | 说明 |
| --- | --- | --- |
| `--host` | `NAS_APP_HOST` | 默认 `0.0.0.0` |
| `--port` | `NAS_APP_PORT` | 默认 `8669` |
| `--data` | `NAS_APP_DATA` | 数据目录 |
| `--config` | `NAS_APP_CONFIG` | 配置文件 |
| `--token` | `NAS_APP_TOKEN` | 访问令牌 |
| `--mcp-host` | `NAS_APP_MCP_HOST` | 默认 `127.0.0.1` |
| `--mcp-port` | `NAS_APP_MCP_PORT` | 默认 `8670` |
| `--mcp-path` | `NAS_APP_MCP_PATH` | 默认 `/mcp` |
| `--no-mcp` | `NAS_APP_MCP_ENABLED=0` | 禁用 MCP |

### 3.2 Docker

```bash
cd nas-app
cp .env.example .env
# 编辑 .env：NAS_APP_TOKEN、NAS_APP_MCP_HOST_PORT
docker compose up -d
docker compose logs -f
```

`docker-compose.yml` 的关键约定：

| 项 | 值 |
| --- | --- |
| Web 端口 | `8669:8669` |
| MCP 端口 | `127.0.0.1:${NAS_APP_MCP_HOST_PORT:-8670}:8670`（默认只发布到宿主机回环） |
| 数据卷 | `./data:/data` |
| 媒体卷 | 需自行挂载，例如 `/vol1/1000:/vol1/1000` |
| 环境变量 | `NAS_APP_TOKEN`、`NAS_APP_MCP_ENABLED`、`NAS_APP_ALLOWED_PATHS` 等 |

镜像基于 `python:3.12-slim`，已安装 `ffmpeg`、`ca-certificates`、`tzdata`；`HEALTHCHECK` 调 `/healthz`；`CMD python -m nas_app`。

直接构建：

```bash
docker build -t nas-app:0.19.0 .
docker run -d --name nas-app \
  -p 8669:8669 -p 127.0.0.1:8670:8670 \
  -e NAS_APP_TOKEN=change-me \
  -v "$PWD/data:/data" \
  -v /vol1/1000:/vol1/1000 \
  nas-app:0.19.0
```

### 3.3 飞牛 fnOS（.fpk）

打包脚本位于 `nas-app/packaging/fnos/`：

```bash
cd nas-app/packaging/fnos
./build-fpk.sh
# 产物在 nas-app/dist/ 或脚本输出目录
```

包内结构：

| 文件/目录 | 作用 |
| --- | --- |
| `manifest` | `appname=nas-media`、`version=0.19.0`、`service_port=8669`、`desktop_uidir=ui` |
| `cmd/main` | 服务启动入口 |
| `cmd/common.sh` | 公共函数 |
| `cmd/install` / `upgrade` / `uninstall` / `config` | 生命周期脚本 |
| `wizard` | 安装向导（端口/目录/令牌） |
| `config/privilege` `config/resource` | 权限与资源声明 |
| `app/docker/image.conf` | 容器镜像配置 |
| `app/ui` | 桌面图标与入口 |

安装后：应用中心打开「NAS 媒体工具箱」→ 用安装时设置的令牌登录 → 在「配置」页维护白名单与开关。

### 3.4 MCP 接入（Octop / 其他客户端）

Octop「自定义 MCP」填：

```
URL:    http://127.0.0.1:8670/mcp
Header: Authorization: Bearer <NAS_APP_TOKEN>
```

- 只支持 Streamable HTTP；
- 未设 token 时 MCP 端点**不启动**；
- 长任务最多等 240 秒，超时后用 `nas_job_status` 轮询。

## 4. 形态 C：nas-mcp（stdio）

```bash
cd nas-mcp
uv sync                       # 或 pip install -e .
cp config.example.yaml config.yaml
python -m nas_mcp --config ./config.yaml
```

环境变量：`NAS_MCP_CONFIG` 可替代 `--config`。

Octop stdio 配置示例：

```json
{
  "mcpServers": {
    "nas-media": {
      "command": "python",
      "args": ["-m", "nas_mcp", "--config", "/opt/nas-mcp/config.yaml"],
      "env": {
        "TMDB_API_KEY": "...",
        "JACKETT_URL": "http://127.0.0.1:9117",
        "JACKETT_API_KEY": "..."
      }
    }
  }
}
```

启动时行为：

- 配置非法 → 打印到 stderr 并 `exit 2`（不进入半坏状态）；
- `preflight_check` 的警告（白名单不可读、日志目录不可写）打印到 stderr；
- 传输固定 `stdio`，不要把它当 HTTP 服务。

当前注册 23 个工具（见 [04-tools.md](./04-tools.md)）。

## 5. 端口 / 卷 / 健康检查速查

| 形态 | 端口 | 卷/目录 | 健康检查 |
| --- | --- | --- | --- |
| 插件 | 无（进程内） | `~/.octop/plugins/nas-media/` | 宿主状态 |
| nas-app 裸机 | 8669 / 8670 | `<data>/`、媒体目录 | `curl http://127.0.0.1:8669/healthz` |
| nas-app Docker | 8669 / 8670 | `/data`、媒体挂载 | 镜像 `HEALTHCHECK` 调 `/healthz` |
| FPK | 8669 / 8670 | FPK 声明的数据目录 + 媒体挂载 | 应用中心状态 |
| nas-mcp | 无 | 配置文件 + 媒体目录 | 进程存活 |

`/healthz` 返回服务是否就绪（不触发扫描、不访问网络）。

## 6. 首次运行检查清单

1. `ffprobe -version` / `ffmpeg -version` 可执行；
2. `allowed_paths` 至少一条，且目录真实存在、进程可读；
3. `media_roots` 是白名单子集；
4. `exclude_paths` 覆盖网盘/USB 挂载点；
5. 至少一个字幕站点 `enabled=true`（否则字幕工具直接拒绝）；
6. 需要识别/资源搜索时再开 TMDB / Jackett，并确认密钥能取到；
7. `enable_write` / `enable_organize` / `enable_delete` 保持 `false`，按需再开；
8. nas-app 必须设 `NAS_APP_TOKEN`（否则 Web 裸奔、MCP 不启动）；
9. Docker 部署时确认媒体目录已挂载且 UID 权限足够；
10. 用 `nas_get_config` 或 Web「配置」页复核最终生效值。

## 7. 常见启动失败

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `配置错误，拒绝启动: allowed_paths 不能为空` | 配置缺白名单 | 补 `allowed_paths` |
| `enabled=true 但取不到 api_key` | 环境变量没传/名称写错 | 检查 `*_env` 或字面量 |
| `AccessDenied` | 路径不在白名单 | 调整 `allowed_paths` / `exclude_paths` |
| `ffprobe not found` | 未安装或 PATH 不对 | 安装 ffmpeg 或改 `media.ffprobe_path` |
| MCP 端点没起来（nas-app） | 未设 `NAS_APP_TOKEN` 或端口占用 | 设 token；查日志「MCP 端点启动失败」 |
| 8669 无法访问 | 监听在 `127.0.0.1` 或防火墙 | `--host 0.0.0.0`；检查端口映射 |
| `dependency_missing` | 缺 `curl_cffi` | `pip install curl_cffi` |
| 热升级后 ImportError（插件） | `sys.modules` 清理被破坏 | 恢复 `main.py` 第 34–41 行并重启 Octop |

## 8. 多形态共存注意事项

- 三种形态**可以同时安装**，各自读自己的 `config.yaml`；
- **不要同时让它们对同一批媒体目录执行写操作**（整理/清理/混流），否则 `operation_id` 与审计会交叉，回滚难以判断；
- 只读操作（扫描/搜索/体检）可以并行，但会叠加 ffprobe 与网络压力；
- nas-app 与插件共用同一份媒体库时，建议只在其中一个打开 `enable_*` 开关。
