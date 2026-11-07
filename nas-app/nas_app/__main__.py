"""应用入口：参数解析、日志、首次配置生成、启动 HTTP 服务。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import __version__
from .jobs import JobManager
from .server import serve
from .settings import build_settings, ensure_config
from .toolkit import ToolRegistry


def setup_logging(log_file) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def main(argv: list[str] | None = None) -> int:
    settings = build_settings(argv)
    setup_logging(settings.log_file)
    logger = logging.getLogger("nas_app")

    created, message = ensure_config(settings)
    logger.info("nas-app %s 启动：%s", __version__, message)
    logger.info("配置=%s 数据目录=%s", settings.config_path, settings.data_dir)
    if created:
        logger.info("提示：首次生成的白名单可在 Web UI「配置」页调整")

    registry = ToolRegistry(settings.config_path)
    jobs = JobManager(registry, settings.data_dir)
    if not settings.auth_required:
        logger.warning(
            "未设置 NAS_APP_TOKEN，任何能访问 %s:%s 的设备都可使用本应用；"
            "写操作仍受 enable_write/enable_organize/enable_delete 门控。",
            settings.host, settings.port,
        )

    endpoint = _start_mcp(logger, settings, registry, jobs)
    try:
        serve(settings, registry, jobs)
    except OSError as exc:
        logger.error("启动失败：%s", exc)
        return 2
    finally:
        if endpoint is not None:
            endpoint.stop()
    return 0


def _start_mcp(logger, settings, registry, jobs):
    """MCP 端点启动失败只记日志，绝不影响 Web UI / REST。"""
    if not settings.mcp_ready:
        if settings.mcp_enabled:
            logger.warning("未设置 NAS_APP_TOKEN，MCP 端点未启动（MCP 必须鉴权）")
        return None
    try:
        from .mcp_endpoint import McpEndpoint

        endpoint = McpEndpoint(settings, registry, jobs)
        endpoint.start()
        return endpoint
    except Exception:  # noqa: BLE001 - 依赖缺失/端口占用都不应阻止主服务
        logger.exception("MCP 端点启动失败，Web UI/REST 继续运行")
        return None


if __name__ == "__main__":
    raise SystemExit(main())
