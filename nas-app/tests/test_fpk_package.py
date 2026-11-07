import configparser
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FPK = ROOT / "packaging" / "fnos"


def read_manifest() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read_string("[app]\n" + (FPK / "manifest").read_text(encoding="utf-8"))
    return parser["app"]


def test_manifest_fields():
    manifest = read_manifest()
    assert manifest["appname"] == "nas-media"
    assert manifest["version"] == "0.19.17"
    assert "MCP" in manifest["changelog"]
    assert manifest["platform"] == "all"
    assert manifest["desktop_applaunchname"] == "nas-media.main"
    assert manifest["service_port"] == "8669"
    assert manifest["disable_authorization_path"] == "false"


def test_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.19.17"' in text


def test_json_files_valid():
    config = json.loads((FPK / "app" / "ui" / "config").read_text(encoding="utf-8"))
    entry = config[".url"]["nas-media.main"]
    assert entry["port"] == "8669"
    assert entry["url"] == "/"
    for name in ("wizard/install", "wizard/config", "wizard/uninstall"):
        data = json.loads((FPK / name).read_text(encoding="utf-8"))
        assert isinstance(data, list) and data and "items" in data[0]
    json.loads((FPK / "config" / "privilege").read_text(encoding="utf-8"))
    json.loads((FPK / "config" / "resource").read_text(encoding="utf-8"))


def test_wizard_item_types_supported_by_fnpack():
    # fnOS fnpack 1.2.4 会拒绝未知控件类型（例如 switch），必须在打包前拦住
    allowed = {"tips", "password", "radio", "checkbox", "text", "input", "select", "number"}
    for name in ("wizard/install", "wizard/config", "wizard/uninstall"):
        data = json.loads((FPK / name).read_text(encoding="utf-8"))
        for step in data:
            for item in step.get("items", []):
                assert item.get("type") in allowed, f"{name} 含不支持的控件类型: {item.get('type')}"
    uninstall = json.loads((FPK / "wizard" / "uninstall").read_text(encoding="utf-8"))
    fields = [item.get("field") for step in uninstall for item in step.get("items", [])]
    assert "wizard_remove_data" in fields


def test_entry_files_exist():
    for name in ("ICON.PNG", "ICON_256.PNG", "app/ui/images/icon_64.png", "app/ui/images/icon_256.png"):
        data = (FPK / name).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n"), name
    for name in (
        "main", "install_init", "install_callback", "upgrade_init", "upgrade_callback",
        "uninstall_init", "uninstall_callback", "config_init", "config_callback",
    ):
        assert (FPK / "cmd" / name).is_file(), name


def test_image_conf_matches_version():
    manifest = read_manifest()
    lines = (FPK / "app" / "docker" / "image.conf").read_text(encoding="utf-8").splitlines()
    image = next(line for line in lines if line.startswith("IMAGE="))
    assert image == f"IMAGE=ghcr.io/maotaozi54/nas-app:{manifest['version']}"
