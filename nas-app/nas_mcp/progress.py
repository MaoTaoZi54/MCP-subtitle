"""任务进度与取消上下文（可选，缺失上下文时全部静默降级）。

长耗时工具在循环里调用 ``report`` / ``check_canceled``；由 nas-app 的
JobManager 在执行线程内 ``bind_control`` 注入实现。插件、stdio MCP 或
对话里直接调用工具时没有绑定，两个函数都是无副作用的空操作。
"""

from __future__ import annotations

import contextvars
from typing import Any

_CONTROL: contextvars.ContextVar[Any] = contextvars.ContextVar("nas_job_control", default=None)


def bind_control(control: Any):
    """绑定当前线程/上下文的进度控制器，返回 reset 用的 token。"""
    return _CONTROL.set(control)


def reset_control(token) -> None:
    _CONTROL.reset(token)


def current_control() -> Any:
    return _CONTROL.get()


def report(
    stage: str,
    label: str,
    *,
    done: int | None = None,
    total: int | None = None,
    current: str | None = None,
) -> None:
    """上报一次进度；必须在循环里保持廉价（只改内存，不做磁盘写）。"""
    control = _CONTROL.get()
    if control is not None:
        control.report(stage=stage, label=label, done=done, total=total, current=current)


def check_canceled() -> bool:
    """合作式取消检查：没有任务上下文时恒为 False。"""
    control = _CONTROL.get()
    return bool(control is not None and control.canceled())


def mark_partial(result: Any) -> Any:
    """给被取消的结果打上明确标记，调用方不会把半截结果当完整结果。"""
    if isinstance(result, dict):
        result["canceled"] = True
        result["partial"] = True
    return result
