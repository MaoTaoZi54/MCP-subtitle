"""运行参数与首次启动的配置引导。

所有可变项都走环境变量 / 命令行参数，容器与飞牛 fpk 都不需要改镜像内文件：

- NAS_APP_CONFIG       配置文件路径（默认 <data>/config.yaml）
- NAS_APP_DATA         数据目录（默认 /data，本地运行默认 ./data）
- NAS_APP_HOST / PORT  监听地址与端口（默认 0.0.0.0:8669）
- NAS_APP_TOKEN        访问令牌；留空表示不鉴权（仅建议内网使用）
- NAS_APP_MCP_ENABLED  是否启用内置 MCP 端点（默认开启；无 token 时不启动）
- NAS_APP_MCP_HOST / PORT / PATH  MCP 监听地址、端口与路径（默认 127.0.0.1:8670/mcp）
- NAS_APP_ALLOWED_PATHS 首次生成配置时的白名单（逗号/换行分隔）
- NAS_APP_EXCLUDE_PATHS 首次生成配置时的排除目录
- NAS_APP_ENABLE_WRITE / ORG / DELETE 首次生成配置时的写开关
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import DEFAULT_MCP_PORT, DEFAULT_PORT


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "是", "开"}


def _normalize_mcp_path(raw: str) -> str:
    text = (raw or "").strip() or "/mcp"
    if not text.startswith("/"):
        text = "/" + text
    text = text.rstrip("/")
    return text or "/"


def _split_paths(raw: str) -> list[str]:
    items: list[str] = []
    for chunk in raw.replace(",", "\n").splitlines():
        item = chunk.strip()
        if item and item not in items:
            items.append(item)
    return items


@dataclass(frozen=True)
class AppSettings:
    host: str
    port: int
    data_dir: Path
    config_path: Path
    static_dir: Path
    log_file: Path
    token: str
    mcp_enabled: bool
    mcp_host: str
    mcp_port: int
    mcp_path: str

    @property
    def auth_required(self) -> bool:
        return bool(self.token)

    @property
    def mcp_ready(self) -> bool:
        """MCP 必须鉴权；没有 token 时宁可不启动，避免裸奔。"""
        return self.mcp_enabled and self.auth_required


def _default_data_dir() -> Path:
    env = os.environ.get("NAS_APP_DATA")
    if env:
        return Path(env).expanduser().resolve()
    if os.name != "nt" and Path("/data").is_dir():
        return Path("/data")
    return (Path.cwd() / "data").resolve()


def build_settings(argv: list[str] | None = None) -> AppSettings:
    parser = argparse.ArgumentParser(
        prog="nas-app",
        description="NAS 媒体工具箱 · 独立应用版（Web UI + HTTP API）",
    )
    parser.add_argument("--host", default=os.environ.get("NAS_APP_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("NAS_APP_PORT", DEFAULT_PORT))
    )
    parser.add_argument("--data", default=None, help="数据目录（默认 $NAS_APP_DATA 或 ./data）")
    parser.add_argument(
        "--config",
        default=os.environ.get("NAS_APP_CONFIG"),
        help="配置文件路径（默认 <data>/config.yaml）",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("NAS_APP_TOKEN", ""),
        help="访问令牌；留空则不鉴权（也可用 NAS_APP_TOKEN_FILE）",
    )
    mcp_default = _env_bool("NAS_APP_MCP_ENABLED", True)
    parser.add_argument(
        "--mcp-host",
        default=os.environ.get("NAS_APP_MCP_HOST", "127.0.0.1"),
        help="MCP 监听地址（默认 127.0.0.1；容器内用 0.0.0.0）",
    )
    parser.add_argument(
        "--mcp-port",
        type=int,
        default=int(os.environ.get("NAS_APP_MCP_PORT", DEFAULT_MCP_PORT)),
        help=f"MCP 监听端口（默认 {DEFAULT_MCP_PORT}）",
    )
    parser.add_argument(
        "--mcp-path",
        default=os.environ.get("NAS_APP_MCP_PATH", "/mcp"),
        help="MCP Streamable HTTP 路径（默认 /mcp）",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        default=not mcp_default,
        help="禁用内置 MCP 端点",
    )
    parser.add_argument("--version", action="version", version="nas-app")
    args = parser.parse_args(argv)

    data_dir = Path(args.data).expanduser().resolve() if args.data else _default_data_dir()
    config_path = (
        Path(args.config).expanduser().resolve()
        if args.config
        else data_dir / "config.yaml"
    )
    token = args.token or ""
    token_file = os.environ.get("NAS_APP_TOKEN_FILE")
    if not token and token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"[nas-app] 无法读取 NAS_APP_TOKEN_FILE: {exc}", file=sys.stderr)
    return AppSettings(
        host=args.host,
        port=args.port,
        data_dir=data_dir,
        config_path=config_path,
        static_dir=Path(__file__).resolve().parent / "web",
        log_file=data_dir / "logs" / "nas-app.log",
        token=token,
        mcp_enabled=not args.no_mcp,
        mcp_host=args.mcp_host,
        mcp_port=args.mcp_port,
        mcp_path=_normalize_mcp_path(args.mcp_path),
    )


def ensure_config(settings: AppSettings) -> tuple[bool, str]:
    """首次启动生成可用的 config.yaml，返回 (created, message)。

    已存在的配置一律不动，避免覆盖用户/插件已有的白名单与密钥。
    """
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    settings.config_path.parent.mkdir(parents=True, exist_ok=True)

    if settings.config_path.is_file():
        return False, f"使用现有配置 {settings.config_path}"

    allowed = _split_paths(os.environ.get("NAS_APP_ALLOWED_PATHS", ""))
    if not allowed:
        default_root = "/media" if Path("/media").is_dir() else str(Path.cwd())
        allowed = [default_root]
    excludes = _split_paths(os.environ.get("NAS_APP_EXCLUDE_PATHS", ""))
    logs_dir = (settings.data_dir / "logs").as_posix()

    data: dict = {
        "allowed_paths": allowed,
        "exclude_paths": excludes,
        "enable_write": _env_bool("NAS_APP_ENABLE_WRITE", False),
        "enable_organize": _env_bool("NAS_APP_ENABLE_ORGANIZE", False),
        "enable_delete": _env_bool("NAS_APP_ENABLE_DELETE", False),
        "tmdb": {"enabled": False, "api_key": ""},
        "jackett": {"enabled": False, "url": "http://127.0.0.1:9117", "api_key": ""},
        "subhd": {"enabled": True},
        "opensubtitles": {"enabled": False},
        "assrt": {
            "enabled": False,
            "token": "",
            "proxy": "",
            "insecure_no_sni_direct": False,
        },
        "subdl": {"enabled": False, "api_key": ""},
        "seedhub": {"enabled": True},
        "dysbk": {"enabled": True},
        "organizer": {"audit_log_file": f"{logs_dir}/organize-audit.jsonl"},
        "logging": {"file": f"{logs_dir}/nas-app.log", "level": "INFO"},
    }
    text = (
        "# nas-app 首次启动自动生成。可在 Web UI「配置」页修改，\n"
        "# 也可直接编辑本文件（高级字段：limits/media/jackett 细节）。\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    )
    _write_private(settings.config_path, text)
    return True, f"已生成初始配置 {settings.config_path}（白名单：{', '.join(allowed)}）"


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
