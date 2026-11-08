"""NAS 媒体工具箱 · 独立应用版。

在共享能力层 ``nas_mcp``（与 Octop 插件同源）之上提供一个带 Web UI 的
独立 HTTP 服务：飞牛应用中心 / Docker / 裸机均可运行。

与另外两种形态的差异仅在装配层：
- Octop 插件：进程内注册工具；
- nas-mcp（stdio）：MCP stdio 传输；
- 本应用：HTTP API + Web UI + MCP Streamable HTTP。
"""

__version__ = "0.19.29"

APP_NAME = "nas-app"
APP_DISPLAY_NAME = "NAS 媒体工具箱"
DEFAULT_PORT = 8669
DEFAULT_MCP_PORT = 8670
