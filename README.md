# MCP-subtitle · NAS 媒体工具箱

**主力形态是 Octop 插件** [nas-octop-plugin](nas-octop-plugin)；[nas-app](nas-app) 与 [nas-mcp](nas-mcp) 作为备选保留。三种交付形态共享同一份媒体能力内核 **0.19.17**（扫描/字幕/资源搜索/整理），装配层能力分工如下：

| 子项目 | 形态 | 说明 |
| --- | --- | --- |
| [nas-octop-plugin](nas-octop-plugin) | Octop 进程内插件 | **29 个工具**（28 业务 + `nas_save_config`），配置可用对话/表单/编辑 config.yaml |
| [nas-mcp](nas-mcp) | 独立 stdio MCP 服务 | **当前 23 个工具**，stdio 装配层覆盖主流媒体/字幕/搜索能力（比插件少 5 个业务工具：缺集检测、完整性检查、清理、回滚、批量补字幕，另无 `nas_save_config`） |
| [nas-app](nas-app) | 独立应用 | **28 个业务工具**；8669 提供 Web UI + HTTP API，8670 内置 MCP Streamable HTTP；支持 **Docker** 与 **飞牛 fnOS 应用商店（.fpk）** |

### 怎么选

- **默认选择** → `nas-octop-plugin`（主力形态：零额外进程，29 个工具，配置可在对话里完成；config 存 `$OCTOP_DATA_DIR`，升级不丢配置）。
- **要 Web UI / 手机访问 / Docker / 飞牛商店**，或想让 Octop 与其他远程 MCP 客户端共用一个服务 → `nas-app`（备选，一个服务同时覆盖 Web UI 和 MCP，配置只在 Web UI 维护）。
- **客户端只支持 stdio** → `nas-mcp`（备选，包含 23 个核心媒体与搜索工具）。

三种形态可以同时安装、独立配置；不要同时让它们写同一批媒体目录（整理/清理/混流），只读操作可并行。

独立应用快速开始：

```bash
cd nas-app
cp .env.example .env    # 设置 NAS_APP_TOKEN
docker compose up -d    # 打开 http://<NAS>:8669
```

Octop 在「自定义 MCP」里填 `http://127.0.0.1:8670/mcp` + `Authorization: Bearer <令牌>` 即可直连同一服务。

飞牛安装包构建、授权目录、安全边界、MCP 连接与 API 文档见 [nas-app/README.md](nas-app/README.md)。
