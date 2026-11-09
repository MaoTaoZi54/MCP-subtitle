"""兼容层：assrt（伪射手）站点适配器已迁至 :mod:`nas_mcp.assrt`。

历史代码/配置里仍可能使用 ``shooter`` 这个叫法（站点名、``shooter:`` sid 前缀、
旧配置段名），因此保留本模块做单向转发，不再维护第二份实现：

- ``ShooterClient`` -> :class:`nas_mcp.assrt.AssrtClient`
- ``ShooterError``  -> :class:`nas_mcp.assrt.AssrtError`
- ``search`` / ``fetch`` / ``transport_info`` 原样转发

新代码请直接 ``from nas_mcp import assrt``。
"""

from __future__ import annotations

import logging

from .assrt import (
    AssrtClient as ShooterClient,
    AssrtError,
    fetch,
    search,
    transport_info,
)

ShooterError = AssrtError

logger = logging.getLogger("nas_mcp.shooter")

__all__ = [
    "ShooterClient",
    "ShooterError",
    "fetch",
    "search",
    "transport_info",
]
