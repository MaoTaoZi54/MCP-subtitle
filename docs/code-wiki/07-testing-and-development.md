# 07 · 测试与开发

## 1. 测试布局

三个子项目各自独立测试，互不共享 pytest 配置。

### 1.1 Octop 插件（`nas-octop-plugin/tests/`）

| 文件 | 覆盖内容 |
| --- | --- |
| `test_phase1_acceptance.py` | 阶段一验收：文件/磁盘/扫描基础能力 |
| `test_phase2_acceptance.py` | 阶段二验收：画质/查重/体检 |
| `test_phase34_acceptance.py` | 阶段三/四验收：字幕、资源搜索 |
| `test_multisite.py` | 多站点字幕编排、failover、`site_attempts` |
| `test_fill_subtitles.py` | 批量补字幕的整季包/单集分档 |
| `test_mux_subtitles.py` | 混流双轨/单轨、原子替换 |
| `test_open_issues.py` | OPEN 系列问题的回归 |
| `test_issues_v0140.py` / `test_issues_7_10.py` | 特定版本问题回归 |
| `test_pan_probe.py` | 网盘链接探测 |
| `test_s2_seedhub.py` | SeedHub 搜索/磁力/网盘 |
| `test_s6_dysbk.py` | 网盘社搜索/直链 |
| `test_s7_federated.py` | 三路聚合、去重、语言路由 |
| `test_t1_deadline.py` | Jackett 绝对 deadline 预算 |
| `test_t2_apikey.py` | API Key 不进结果/日志 |
| `test_t3_incomplete.py` | 权限错误 → `incomplete` 标记 |
| `test_t4_fallback.py` | 聚合超时后的并行单源降级 |
| `test_t5_sort.py` | 排序策略 seeders/quality/hybrid |
| `test_t6_magnet.py` | 302 跟进补磁力 |
| `test_t7_keeper.py` | 重复版本 keeper 仲裁 |
| `test_t8_circuit.py` | 熔断（阈值/冷却/时间窗） |
| `test_t9_bitrate.py` | 同档质量用码率 tiebreak |
| `test_t10_reload.py` | 插件热升级的 `sys.modules` 清理 |
| `test_t11_orphan_season.py` | 孤儿季归位 |

辅助脚本与桩：

| 文件 | 作用 |
| --- | --- |
| `subhd_stub.py` / `opensubtitles_stub.py` / `shooter_stub.py` | 本地 HTTP 桩，避免测试打真实站点 |
| `harness_check.py` | 模拟 Octop harness 加载插件 |
| `local_load_check.py` | 本地直接 import 检查 |
| `reload_check.py` | 验证热重载行为 |

### 1.2 nas-app（`nas-app/tests/`）

| 文件 | 覆盖内容 |
| --- | --- |
| `conftest.py` | 临时数据目录、配置、registry 夹具 |
| `test_settings.py` | 环境变量/参数解析、首次生成配置、`chmod 600` |
| `test_toolkit.py` | 参数准备、类型转换、错误信封、工具元数据 |
| `test_jobs.py` | 任务提交/执行/持久化/重启载入 |
| `test_server.py` | HTTP 路由、鉴权、CSRF、配置读写 |
| `test_mcp_endpoint.py` | MCP 工具暴露、长任务转队列、240s 等待 |
| `test_fpk_package.py` | FPK 包结构/脚本 |
| `test_fpk_lifecycle.py` | 安装/升级/卸载生命周期 |

辅助脚本（`nas-app/scripts/`）：

| 文件 | 作用 |
| --- | --- |
| `probe_mcp.py` | 连上本地 MCP 端点列工具/调工具 |
| `live_api_test.py` | 对运行中的 nas-app 做 HTTP 冒烟 |
| `make_icons.py` | 生成 FPK/UI 图标 |
| `build-image.sh` | 构建 Docker 镜像 |
| `sync-core.sh` | 从插件同步 `nas_mcp` 内核 |

### 1.3 nas-mcp（`nas-mcp/tests/`）

| 文件 | 覆盖内容 |
| --- | --- |
| `smoke_test.py` | 启动/注册工具冒烟 |
| `test_core_sync.py` | 内核文件与插件版一致性检查（版本/关键符号） |
| `test_multisite.py` | 多站点字幕 |
| `test_mux_subtitles.py` | 混流 |
| `subhd_stub.py` / `opensubtitles_stub.py` / `shooter_stub.py` / `jackett_stub.py` | 站点桩 |
| `real_subhd_check.py` | 可选：真实站点联通性检查（需网络） |
| `mux_real_sim.py` | 可选：真实 ffmpeg 混流模拟 |

## 2. 运行测试

### 2.1 插件

```bash
cd nas-octop-plugin
python -m pytest tests -q
# 只跑某一组
python -m pytest tests/test_t1_deadline.py -q
```

### 2.2 nas-app

```bash
cd nas-app
pip install -e ".[dev]"
python -m pytest -q
```

`pyproject.toml` 已配置 `testpaths = ["tests"]`、`addopts = "-q"`。

### 2.3 nas-mcp

```bash
cd nas-mcp
uv sync
python -m pytest tests -q
```

### 2.4 可选的真实环境检查

```bash
# 真实 SubHD 联通（会发网络请求）
python nas-mcp/tests/real_subhd_check.py
# 真实 ffmpeg 混流模拟
python nas-mcp/tests/mux_real_sim.py
# nas-app 运行中冒烟（需先启动服务并设 token）
python nas-app/scripts/live_api_test.py
# 探测 MCP 端点
python nas-app/scripts/probe_mcp.py
```

这些脚本**不要**放进 CI 默认路径，它们依赖网络或外部二进制。

## 3. 测试设计约定

从现有测试可以总结出几条明确模式，新增测试请沿用：

1. **网络一律用桩**：字幕/Jackett/SeedHub 的测试都起本地 `http.server` 或 monkeypatch `urllib`，不打真实站点。
2. **文件系统用 `tmp_path`**：构造媒体库目录树、假 mkv/srt 文件；ffprobe 用 monkeypatch 或假 JSON。
3. **进程级状态必须重置**：
   - `jackett.reset_circuit()`（熔断是模块全局）；
   - `fs_utils.reset_known_denied()`（权限错误缓存）；
   - `resolver` 的解析缓存（同一测试内多次 resolve 需注意）。
4. **错误码断言优先**：断言 `result["error"]["code"]`，不要断言中文 message。
5. **incomplete 语义要测**：权限错误场景必须验证 `incomplete` / `errors_summary`，否则调用方会把"没权限"读成"没有"。
6. **命名带问题编号**：`test_t1_deadline.py`、`test_open_issues.py` 这种命名对应具体的 bug/需求编号，回归时直接定位。

## 4. 新增一个工具的流程

以新增 `xxx_tool` 为例：

### 4.1 内核

1. 在合适的模块（或新模块）实现函数，签名遵循：
   ```python
   def xxx(guard: PathGuard, cfg: NasConfig, *, ...) -> dict:
       ...
       return {"ok": True, ...}
   ```
2. 必须：
   - 所有路径先 `guard.check()`；
   - 写操作检查 `enable_*`；
   - 不抛业务异常给上层（或抛已定义的 `*Error`）；
   - 返回 dict 带 `ok`；
   - 遍历类结果带 `truncated` / `errors` / `errors_summary`；
   - 可能不完整时用 `mark_incomplete*()`。
3. 需要新配置项 → 加到 `config.py` 的 dataclass（给默认值，保证老配置能加载）+ 校验。

### 4.2 插件装配

1. 在 `main.py` 的 `setup()` 内定义闭包：
   ```python
   @_safe
   def xxx_tool(param: str = "") -> str:
       """一句话说明 + Args 段。"""
       cfg = _get_cfg()
       guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
       return _json(xxx_tools.xxx(guard, cfg, ...))
   ```
2. 把 `("xxx_tool", xxx_tool)` 加进 `setup()` 末尾的元组列表。
3. 更新 `README.md` 的工具表与 `plugin.yaml` 版本号。

### 4.3 nas-app

1. 在 `toolkit.py::_register_all()` 注册：
   ```python
   @self.tool("xxx_tool", "标题", "分类", risk="read", long_running=True)
   def xxx_tool(param: str = "") -> dict:
       """..."""
       cfg = self.get_config()
       guard = PathGuard(cfg.allowed_paths, cfg.exclude_paths)
       return xxx_tools.xxx(guard, cfg, ...)
   ```
2. 选择 `risk`（`read/write/delete`）与 `long_running`（超过 ~5s 或涉及全库遍历建议 `True`）。
3. 前端会从 `ToolSpec.public()` 自动渲染，无需改 HTML。

### 4.4 nas-mcp（可选）

在 `src/nas_mcp/server.py` 用 `@mcp.tool()` 注册；注意其内核是旧快照，新工具可能依赖不存在的模块——先决定是否同步内核。

### 4.5 测试

至少补：

- 正常路径；
- 白名单外路径 → `access_denied`；
- 开关关闭 → `not_configured` / 拒绝；
- 权限不足 → `incomplete`；
- 分页/截断。

## 5. 内核同步

`nas-octop-plugin/nas_mcp/` 是主版本，`nas-app/nas_mcp/` 与 `nas-mcp/src/nas_mcp/` 是副本。

```bash
# 从插件同步到 nas-app（脚本已存在）
cd nas-app
./scripts/sync-core.sh
```

手工同步时的检查项：

1. `__init__.py` 的 `__version__` 一致；
2. 文件集合一致（`diff <(ls plugin/nas_mcp) <(ls app/nas_mcp)`）；
3. `config.py` 的 dataclass 字段一致（否则会出现"配置能存不能读"）；
4. 新增模块是否被 `server.py` 引用（nas-mcp 当前注册 23 个工具，未装配的能力在 stdio 侧不可见）。

同步后必跑：

```bash
python -m pytest nas-app/tests -q
python -m pytest nas-octop-plugin/tests -q
```

`nas-mcp/tests/test_core_sync.py` 可以辅助发现版本/符号漂移，但它只做有限检查，不能替代 `diff`。

## 6. 代码约定

从现有代码观察到的稳定风格：

| 约定 | 说明 |
| --- | --- |
| 模块级 docstring 写规格来源 | 如「设计（规格 §24-§35）」；改动前先读它 |
| 冻结 dataclass | 配置、结果模型一律 `frozen=True`，避免运行期被改 |
| 全量类型注解 | `from __future__ import annotations` + `X \| None` |
| 私有函数用 `_` 前缀 | `_safe`、`_patch_config_text`、`_merge` |
| 日志用 `logging.getLogger("nas_mcp.<module>")` | 便于按模块调级别 |
| 不回显密钥 | 只输出 `api_key_configured` |
| 注释解释"为什么" | 大量 `# T1：...`、`# OPEN-2：...` 记录事故与约束，改动相关逻辑前务必读 |
| 错误码稳定 | 新增错误码要同步文档与测试 |

**不要做的事**：

- 不要把 `enable_delete` 加回 `nas_save_config`；
- 不要在工具函数里绕过 `PathGuard` 直接用 `Path`；
- 不要把网络请求写成无限重试；
- 不要在 `main.py` 顶层 `nas_mcp` import 之前插入其他 import（会破坏热升级清理的顺序约束）。

## 7. 发布检查清单

1. 三个子项目测试全绿；
2. `nas-app` 与插件内核 `diff` 为空（或差异已确认）；
3. `plugin.yaml` / `pyproject.toml` 版本号同步；
4. 新增/变更工具已更新 `nas-octop-plugin/README.md` 与 `nas-app/README.md` 的工具表；
5. `config.example.yaml` 含新配置项且带默认值；
6. `docs/code-wiki/` 对应章节已更新；
7. FPK manifest 版本号与 `nas_app.__version__` 一致；
8. Docker 镜像能通过 `/healthz`。

## 8. 已知限制与后续工作

| 限制 | 影响 | 方向 |
| --- | --- | --- |
| nas-mcp 提供 23 个工具（插件 29 个） | stdio 客户端拿不到整理/清理/混流/回滚等装配入口 | 按需补齐注册 |
| 无多用户权限 | 有 token 即全权限 | 如需多人使用，应加角色层 |
| 删除不可回滚 | `media_cleanup` 误删无法恢复 | 可考虑先移入回收目录 |
| 混流不可回滚 | 覆盖后只能重新压制 | 可考虑保留原文件备份 |
| 跨文件系统整理 | `os.replace` 跨设备会失败 | 目标与源需同一文件系统 |
| Jackett 慢索引器 | 聚合端点可能被拖慢 | 已用 deadline + 并行降级 + 熔断缓解 |
| SeedHub 依赖 CF 绕过 | 缺 `curl_cffi` 时该组不可用 | 安装依赖或关闭该源 |
| 字幕站点结构变动 | HTML 解析可能失效 | 站点适配器隔离，改单文件即可 |
| Web UI 无国际化 | 仅中文 | — |

## 9. 排障入口

| 问题 | 先看 |
| --- | --- |
| 工具返回 `not_configured` | `config.yaml` 是否被 `load_config` 拒绝；`preflight_check` 警告 |
| 工具返回 `access_denied` | `allowed_paths` / `exclude_paths` 展开结果；`nas_get_config` 的 `media_roots_expanded` |
| 扫描结果"少了很多文件" | `scan_truncated`、`truncated`、`errors_summary`、`skipped_by_filter` |
| 整理计划全是 `blocked` | `write_preflight.unwritable_dirs`、`enable_delete` |
| 回滚 `not_found` | `operation_id` 是否真实存在；审计日志路径是否被改过 |
| Jackett 一直超时 | `circuit.state`、`deadline_exceeded`、`mode` |
| 字幕搜索全是 `all_sites_failed` | `site_errors` 逐站原因；站点 `enabled` |
| MCP 客户端连不上 nas-app | 是否设 `NAS_APP_TOKEN`；8670 是否映射；路径是否 `/mcp` |
