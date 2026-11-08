"""NAS MCP Server —— 面向飞牛 NAS / Octop 的媒体库安全工具层。

独立项目，不依赖 Octop 源代码；路径白名单 + 排除目录强制安全边界。
写操作三级分级：L1 整理计划（只读）恒可用，L2 整理移动需 enable_organize，
L3 删除需 enable_delete；字幕下载需 subhd.enabled + enable_write 双开关。
"""

__version__ = "0.19.29"
