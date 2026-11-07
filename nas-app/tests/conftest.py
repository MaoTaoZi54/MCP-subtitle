import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nas_app.jobs import JobManager  # noqa: E402
from nas_app.settings import build_settings, ensure_config  # noqa: E402
from nas_app.toolkit import ToolRegistry  # noqa: E402


@pytest.fixture
def settings(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "movie.mkv").write_bytes(b"not really a video")
    monkeypatch.setenv("NAS_APP_ALLOWED_PATHS", str(media))
    monkeypatch.setenv("NAS_APP_EXCLUDE_PATHS", str(media / "excluded"))
    cfg = build_settings(["--data", str(tmp_path / "data"), "--token", "secret-token"])
    created, _ = ensure_config(cfg)
    assert created
    return cfg


@pytest.fixture
def registry(settings):
    return ToolRegistry(settings.config_path)


@pytest.fixture
def jobs(registry, settings):
    return JobManager(registry, settings.data_dir, max_workers=1)
