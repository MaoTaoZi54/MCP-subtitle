import os
import stat
from pathlib import Path

from nas_app.settings import build_settings, ensure_config


def test_ensure_config_creates_private_file(settings):
    assert settings.config_path.is_file()
    text = settings.config_path.read_text(encoding="utf-8")
    assert "allowed_paths" in text
    assert "enable_write: false" in text
    assert "organize-audit.jsonl" in text
    if os.name != "nt":
        assert stat.S_IMODE(settings.config_path.stat().st_mode) == 0o600


def test_ensure_config_does_not_overwrite(settings):
    original = settings.config_path.read_text(encoding="utf-8")
    settings.config_path.write_text(original + "\n# marker\n", encoding="utf-8")
    created, message = ensure_config(settings)
    assert created is False
    assert "现有配置" in message
    assert "# marker" in settings.config_path.read_text(encoding="utf-8")


def test_token_file(tmp_path):
    token = tmp_path / "token"
    token.write_text("from-file\n", encoding="utf-8")
    old = os.environ.get("NAS_APP_TOKEN_FILE")
    os.environ["NAS_APP_TOKEN_FILE"] = str(token)
    try:
        cfg = build_settings(["--data", str(tmp_path / "data")])
    finally:
        if old is None:
            os.environ.pop("NAS_APP_TOKEN_FILE", None)
        else:
            os.environ["NAS_APP_TOKEN_FILE"] = old
    assert cfg.token == "from-file"
    assert cfg.auth_required is True


def test_no_token_means_open(settings):
    cfg = build_settings(["--data", str(settings.data_dir)])
    assert cfg.auth_required is False
    assert cfg.static_dir.name == "web"
    assert Path(cfg.static_dir, "index.html").is_file()


def test_mcp_defaults(settings):
    cfg = build_settings(["--data", str(settings.data_dir)])
    assert cfg.mcp_enabled is True
    assert cfg.mcp_host == "127.0.0.1"
    assert cfg.mcp_port == 8670
    assert cfg.mcp_path == "/mcp"
    # 没有 token 时 MCP 不启动
    assert cfg.mcp_ready is False


def test_mcp_env_and_path_normalization(tmp_path, monkeypatch):
    monkeypatch.setenv("NAS_APP_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("NAS_APP_MCP_PORT", "9999")
    monkeypatch.setenv("NAS_APP_MCP_PATH", "mcp/")
    cfg = build_settings(["--data", str(tmp_path / "data"), "--token", "12345678"])
    assert cfg.mcp_host == "0.0.0.0"
    assert cfg.mcp_port == 9999
    assert cfg.mcp_path == "/mcp"
    assert cfg.mcp_ready is True


def test_mcp_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("NAS_APP_MCP_ENABLED", "0")
    cfg = build_settings(["--data", str(tmp_path / "data")])
    assert cfg.mcp_enabled is False

    cfg = build_settings(["--data", str(tmp_path / "data"), "--no-mcp"])
    assert cfg.mcp_enabled is False
