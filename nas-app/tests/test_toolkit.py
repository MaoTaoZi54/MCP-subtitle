import json

import pytest

from nas_app.toolkit import InvalidArguments, ToolRegistry


def test_all_tools_registered(registry):
    names = {spec.name for spec in registry.specs()}
    assert len(names) == 28
    for expected in {
        "nas_get_config",
        "nas_list_directory",
        "nas_scan_media",
        "media_library_audit",
        "media_cleanup",
        "media_rollback",
        "subhd_search",
        "media_mux_subtitles",
        "media_search",
    }:
        assert expected in names
    for spec in registry.specs():
        public = spec.public()
        assert public["title"]
        assert public["risk"] in {"read", "write", "delete"}
        assert isinstance(public["params"], list)


def test_call_inside_whitelist(registry, settings):
    media = settings.config_path.parent.parent / "media"
    result = registry.call("nas_list_directory", {"path": str(media)})
    assert result["ok"] is True
    assert result["entries"][0]["name"] == "movie.mkv"


def test_call_outside_whitelist_is_denied(registry, settings):
    outside = settings.data_dir
    result = registry.call("nas_list_directory", {"path": str(outside)})
    assert result["ok"] is False
    assert result["error"]["code"]


def test_unknown_tool(registry):
    with pytest.raises(InvalidArguments):
        registry.get_spec("nope")


def test_unknown_argument(registry, settings):
    media = settings.config_path.parent.parent / "media"
    result = registry.call("nas_list_directory", {"path": str(media), "bogus": 1})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_legacy_string_error_is_normalized(registry, settings):
    media = settings.config_path.parent.parent / "media"
    result = registry.call(
        "media_mux_subtitles",
        {"video_path": str(media / "movie.mkv"), "dry_run": False},
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "write_disabled"
    assert "enable_write" in result["error"]["message"]


def test_save_config_merges_and_hides_secrets(registry):
    view = registry.save_config({
        "enable_write": True,
        "jackett": {"enabled": True, "url": "http://127.0.0.1:9117", "api_key": "super-secret"},
        "opensubtitles": {"enabled": True, "username": "user", "password": "p4ss-plaintext", "api_key": "os-secret"},
    })
    assert view["enable_write"] is True
    assert view["jackett"]["url"] == "http://127.0.0.1:9117"
    assert view["jackett"]["api_key_configured"] is True
    assert view["opensubtitles"]["username_configured"] is True
    assert view["opensubtitles"]["password_configured"] is True
    assert "super-secret" not in json.dumps(view)
    assert "os-secret" not in json.dumps(view)
    assert "p4ss-plaintext" not in json.dumps(view)

    view = registry.save_config({"jackett": {"enabled": True, "url": "http://127.0.0.1:9117", "api_key": ""}})
    assert view["jackett"]["api_key_configured"] is True


def test_save_config_enabling_service_without_key_is_rejected(registry):
    with pytest.raises(InvalidArguments) as excinfo:
        registry.save_config({"opensubtitles": {"enabled": True}})
    assert "API Key" in str(excinfo.value)
    assert registry.config_view()["opensubtitles"]["enabled"] is False


def test_save_config_rejects_unknown(registry):
    with pytest.raises(InvalidArguments):
        registry.save_config({"nope": 1})


def test_save_config_requires_allowed_paths(registry, settings):
    settings.config_path.unlink()
    with pytest.raises(InvalidArguments):
        registry.save_config({"enable_write": True})


def test_config_cache_reloads_on_mtime(registry, settings):
    media = settings.config_path.parent.parent / "media"
    assert registry.call("nas_list_directory", {"path": str(media)})["ok"] is True
    text = settings.config_path.read_text(encoding="utf-8").replace("enable_write: false", "enable_write: true")
    settings.config_path.write_text(text, encoding="utf-8")
    assert registry.config_view()["enable_write"] is True
