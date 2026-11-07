from pathlib import Path
from unittest.mock import patch

import pytest

from nas_mcp.config import Limits, MediaConfig, OrganizerConfig, TmdbConfig
from nas_mcp.organizer import plan_session
from nas_mcp.security import PathGuard


@pytest.fixture
def test_env(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    guard = PathGuard((str(media_dir),), ())
    limits = Limits()
    video_exts = frozenset({"mkv", "mp4"})
    subtitle_exts = frozenset({"srt", "ass"})
    media_cfg = MediaConfig()
    org_cfg = OrganizerConfig()
    return media_dir, guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg


def test_language_split_movie_domestic(test_env):
    media_dir, guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg = test_env
    movie_folder = media_dir / "流浪地球.2019.国语"
    movie_folder.mkdir()
    video = movie_folder / "流浪地球.2019.国语.mkv"
    video.write_text("dummy")

    # Dry-run plan with language_split=True
    res = plan_session(
        guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg,
        path=str(media_dir), language_split=True,
    )
    assert res["ok"] is True
    assert res["language_split"] is True

    move_items = [i for i in res["plan"] if i["action"] == "move"]
    assert len(move_items) == 1
    target = Path(move_items[0]["target"])
    # Target should be placed under 国语电影
    assert "国语电影" in target.parts
    assert move_items[0]["language_split"]["region"] == "domestic"
    assert move_items[0]["language_split"]["subdir"] == "国语电影"


def test_language_split_movie_foreign_by_audio(test_env):
    media_dir, guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg = test_env
    movie_folder = media_dir / "Inception.2010"
    movie_folder.mkdir()
    video = movie_folder / "Inception.2010.mkv"
    video.write_text("dummy")

    fake_pv = {
        "video": {"codec": "h264", "width": 1920, "height": 1080},
        "audio": [{"language": "eng"}],
        "duration_seconds": 120.0,
    }

    with patch("nas_mcp.media.probe_video", return_value=fake_pv):
        res = plan_session(
            guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg,
            path=str(media_dir), language_split=True,
        )
        move_items = [i for i in res["plan"] if i["action"] == "move"]
        assert len(move_items) == 1
        target = Path(move_items[0]["target"])
        assert "外语电影" in target.parts
        assert move_items[0]["language_split"]["region"] == "non_domestic"
        assert move_items[0]["language_split"]["subdir"] == "外语电影"


def test_language_split_tv_unknown_fallback(test_env):
    media_dir, guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg = test_env
    show_folder = media_dir / "MysteryShow.S01E01"
    show_folder.mkdir()
    video = show_folder / "MysteryShow.S01E01.mkv"
    video.write_text("dummy")

    # Probe fails/unknown
    with patch("nas_mcp.media.probe_video", side_effect=OSError("no probe")):
        res = plan_session(
            guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg,
            path=str(media_dir), language_split=True,
        )
        move_items = [i for i in res["plan"] if i["action"] == "move"]
        assert len(move_items) == 1
        target = Path(move_items[0]["target"])
        assert "未知语言" in target.parts
        assert move_items[0]["language_split"]["region"] == "unknown"
        assert move_items[0]["language_split"]["subdir"] == "未知语言"


def test_language_split_idempotent(test_env):
    media_dir, guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg = test_env
    # Library root already inside 国语电影
    domestic_root = media_dir / "国语电影"
    domestic_root.mkdir()
    movie_folder = domestic_root / "战狼.2015"
    movie_folder.mkdir()
    video = movie_folder / "战狼.2015.mkv"
    video.write_text("dummy")

    res = plan_session(
        guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg,
        path=str(domestic_root), language_split=True,
    )
    move_items = [i for i in res["plan"] if i["action"] == "move"]
    assert len(move_items) == 1
    target = Path(move_items[0]["target"])
    # Should NOT have nested 国语电影/国语电影
    count = sum(1 for part in target.parts if part == "国语电影")
    assert count == 1


def test_execute_language_split_physical_move(test_env):
    from nas_mcp.organizer import execute_plan

    media_dir, guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg = test_env
    movie_folder = media_dir / "红海行动.2018.国配"
    movie_folder.mkdir()
    video = movie_folder / "红海行动.2018.国配.mkv"
    video.write_text("dummy-video-content")

    plan_res = plan_session(
        guard, limits, video_exts, subtitle_exts, media_cfg, org_cfg,
        path=str(media_dir), language_split=True,
    )
    assert plan_res["ok"] is True
    move_items = [i for i in plan_res["plan"] if i["action"] == "move"]
    assert len(move_items) == 1

    exec_res = execute_plan(guard, plan_res, org_cfg, enable_organize=True, enable_delete=True)
    assert exec_res["ok"] is True
    assert exec_res["executed"]["moved"] == 1

    expected_file = media_dir / "国语电影" / "红海行动.2018.国配.mkv"
    assert expected_file.exists()
    assert expected_file.read_text() == "dummy-video-content"


def test_toolkit_organize_media_language_split(registry, settings):
    media = settings.config_path.parent.parent / "media"
    movie_folder = media / "战狼2.2017.国语"
    movie_folder.mkdir()
    video = movie_folder / "战狼2.2017.国语.mkv"
    video.write_text("zhanlang")

    res = registry.call(
        "organize_media",
        {"path": str(media), "dry_run": True, "language_split": True},
    )
    assert res["ok"] is True
    assert res["language_split"] is True
    move_items = [i for i in res["plan"] if i["action"] == "move"]
    assert len(move_items) == 1
    assert "国语电影" in Path(move_items[0]["target"]).parts


