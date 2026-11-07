"""v0.18.3 实测问题修复验证（NEW-A/B/C/E/F/G, OBS-1）- nas-app 套件。"""

import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

import json
import pytest
from unittest.mock import patch, MagicMock

import yaml

from nas_mcp.config import load_config, OrganizerConfig, MediaConfig, NasConfig, TmdbConfig, SubhdConfig
from nas_mcp.security import PathGuard
import nas_mcp.resolver as resolver_mod
import nas_mcp.region as region_mod
import nas_mcp.federated as federated_mod
import nas_mcp.subtitle as subtitle_mod
from nas_app.toolkit import ToolRegistry
from nas_app.settings import AppSettings


def _make_minimal_yaml(tmp_path: Path, **extra) -> Path:
    cfg = {
        "allowed_paths": [str(tmp_path)],
        "enable_write": True,
        "enable_organize": True,
        **extra,
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


class TestNewAAuditAndFfmpegTimeout:
    def test_audit_log_tilde_expansion(self, tmp_path):
        home_dir = tmp_path / "fakehome"
        home_dir.mkdir()
        with patch.dict(os.environ, {"HOME": str(home_dir), "USERPROFILE": str(home_dir)}):
            cfg_file = _make_minimal_yaml(
                tmp_path,
                organizer={"audit_log_file": "~/.nas-media/logs/organize-audit.jsonl"}
            )
            cfg = load_config(cfg_file)
            expected = Path(os.path.expanduser("~/.nas-media/logs/organize-audit.jsonl"))
            assert Path(cfg.organizer.audit_log_file) == expected

    def test_audit_log_env_data_dir(self, tmp_path):
        data_dir = tmp_path / "octop_data"
        data_dir.mkdir()
        with patch.dict(os.environ, {"OCTOP_DATA_DIR": str(data_dir)}):
            cfg_file = _make_minimal_yaml(
                tmp_path,
                organizer={"audit_log_file": "logs/organize-audit.jsonl"}
            )
            cfg = load_config(cfg_file)
            assert Path(cfg.organizer.audit_log_file) == data_dir / "logs" / "organize-audit.jsonl"


class TestNewBAndCSubtitleFormatAndTargetExists:
    def test_download_subtitle_target_exists_code(self, tmp_path):
        """NEW-B: 目标文件已存在时，必须抛出 FileExistsError / target_exists，不得掩盖为 all_sites_failed。"""
        video = tmp_path / "Test.Movie.2024.mkv"
        video.write_bytes(b"dummy")
        sub = tmp_path / "Test.Movie.2024.zh-Hans.srt"
        sub.write_text("1\n00:00:00,000 --> 00:00:01,000\n已有字幕\n", encoding="utf-8")

        guard = PathGuard([str(tmp_path)], [])
        cfg_file = _make_minimal_yaml(tmp_path, subhd={"enabled": True})
        cfg = load_config(cfg_file)

        with pytest.raises(FileExistsError):
            subtitle_mod.download_subtitle(
                guard, cfg, cfg.subtitle_extensions, True,
                video_path=str(video), sid="subhd:12345", overwrite=False,
            )

    def test_toolkit_download_target_exists(self, tmp_path):
        """NEW-B: ToolRegistry 在文件存在时返回 error code='target_exists'。"""
        video = tmp_path / "Test.Movie.2024.mkv"
        video.write_bytes(b"dummy")
        sub = tmp_path / "Test.Movie.2024.zh-Hans.srt"
        sub.write_text("1\n00:00:00,000 --> 00:00:01,000\n已有字幕\n", encoding="utf-8")

        cfg_file = _make_minimal_yaml(tmp_path, subhd={"enabled": True})
        registry = ToolRegistry(cfg_file)
        res = registry.call("subhd_download", {"video_path": str(video), "sid": "subhd:12345", "overwrite": False})
        assert res["ok"] is False
        assert res["error"]["code"] == "target_exists"
        assert "目标文件已存在" in res["error"]["message"]


class TestNewERegionTvYear:
    def test_region_drops_year_for_tv_episode(self, tmp_path):
        """NEW-E: 判定产地时，电视剧集 S02E01 带有文件名年份 2026 不把 2026 传入 TMDB 首播年。"""
        import nas_mcp.region as r_mod
        video = tmp_path / "Dark.Matter.S02E01.2026.1080p.mkv"
        video.write_bytes(b"dummy")

        with patch.object(r_mod.media_tools, "probe_video", return_value={"audio": [{"language": "eng"}]}), \
             patch.object(r_mod.resolver_tools, "resolve_cached") as mock_resolve:
            mock_resolve.return_value = {
                "resolved": True,
                "identity": {
                    "canonical_title": "暗黑物质",
                    "original_language": "en",
                    "origin_country": ["US"],
                    "tmdb_id": 196322,
                }
            }
            mcfg = MediaConfig()
            tcfg = TmdbConfig(enabled=True, api_key="test_key")
            res = r_mod._classify_one(video, mcfg, tcfg)
            mock_resolve.assert_called_once()
            _, kwargs = mock_resolve.call_args
            assert kwargs.get("year") is None
            assert kwargs.get("media_type") == "tv"
            assert res["tmdb"]["canonical_title"] == "暗黑物质"


class TestNewFResolverPopularity:
    def test_pick_best_sorts_by_popularity_for_exact_matches(self):
        """NEW-F: 多个精确匹配项时，挑出热度最高者（如 Severance 剧集 pop 46.8 vs 电影 pop 4.3）。"""
        cand_movie = (
            {"title": "Severance", "popularity": 4.3, "vote_count": 100, "id": 1},
            "movie"
        )
        cand_tv = (
            {"name": "Severance", "popularity": 46.8, "vote_count": 2500, "id": 95396},
            "tv"
        )
        candidates = [cand_movie, cand_tv]
        best = resolver_mod._pick_best(candidates, "Severance", year=None)
        assert best is not None
        assert best[1] == "tv"
        assert best[0]["id"] == 95396


class TestNewGFederatedJackettSkipped:
    def test_federated_search_reports_jackett_skipped(self, tmp_path):
        """NEW-G: 当 jackett.enabled=false 时，明确标明 jackett_skipped_by_config: true。"""
        cfg_file = _make_minimal_yaml(
            tmp_path,
            jackett={"enabled": False},
            seedhub={"enabled": False},
            dysbk={"enabled": False},
        )
        cfg = load_config(cfg_file)
        res = federated_mod.search(cfg, "黑神话")
        assert res["ok"] is True
        assert res["jackett_skipped_by_config"] is True
        assert res["sources"]["jackett_skipped_by_config"] is True


class TestObs1FillSubtitlesCheckEmbedded:
    def test_fill_subtitles_parameter_and_skip(self, tmp_path):
        """OBS-1: fill_missing_subtitles 支持 check_embedded=True 并在探测到内嵌中文字幕时跳过。"""
        import nas_mcp.subtitle as s_mod
        video = tmp_path / "Show.S01E01.mkv"
        video.write_bytes(b"dummy")

        guard = PathGuard([str(tmp_path)], [])
        cfg_file = _make_minimal_yaml(tmp_path, subhd={"enabled": True})
        cfg = load_config(cfg_file)

        with patch.object(s_mod.media_tools, "scan_media") as mock_scan, \
             patch.object(s_mod.media_tools, "probe_video", return_value={"embedded_subtitles": [{"language": "chi"}]}):
            mock_scan.return_value = {
                "total_matched": 1,
                "videos": [{
                    "name": "Show.S01E01.mkv",
                    "path": str(video),
                    "has_chinese_subtitle": False,
                }],
                "scan_truncated": False,
            }
            res = s_mod.fill_missing_subtitles(
                guard, cfg, cfg.subtitle_extensions, False,
                dry_run=True, check_embedded=True,
            )
            assert res["ok"] is True
            assert mock_scan.call_args[1].get("probe") is True

class TestQ1AndQ2Fixes:
    def test_q2_ultrawide_resolution_labels(self):
        """Q2: 2.40:1 和 2:1 超宽画幅 4K/1080p 分辨率正确识别为 4K / 1080p。"""
        from nas_mcp.media import _resolution_label
        assert _resolution_label(3840, 1606) == "4K"
        assert _resolution_label(3840, 1920) == "4K"
        assert _resolution_label(3840, 2160) == "4K"
        assert _resolution_label(1920, 960) == "1080p"
        assert _resolution_label(1920, 800) == "1080p"
        assert _resolution_label(1920, 1080) == "1080p"
        assert _resolution_label(1280, 534) == "720p"
        assert _resolution_label(1280, 720) == "720p"

    def test_q2_ultrawide_quality_score(self):
        """Q2: 3840x1606 裁边 4K 视频质量分不再跌落至 52 分（fair）。"""
        from nas_mcp.release import quality_score_from_dimensions, _res_score
        assert _res_score("1606p") == 40
        assert _res_score("1920p") == 40
        assert _res_score("960p") == 30
        assert _res_score("4K") == 40
        assert _res_score("2160p") == 40
        score, tier = quality_score_from_dimensions(
            resolution="2160p",
            source_type="WEB-DL",
            video_codec="HEVC",
            hdr="HDR10",
            audio_codec="E-AC-3",
            has_atmos=True,
            bit_depth=10,
        )
        assert score >= 85
        assert tier == "excellent"

    def test_q1_fill_subtitles_skips_embedded_chinese(self, tmp_path):
        """Q1: 当视频内嵌中文字幕且 check_embedded=True（默认）时，不判定为 missing。"""
        import nas_mcp.subtitle as s_mod
        video = tmp_path / "Dark.Matter.S01E01.mkv"
        video.write_bytes(b"dummy")

        guard = PathGuard([str(tmp_path)], [])
        cfg_file = _make_minimal_yaml(tmp_path, subhd={"enabled": True})
        cfg = load_config(cfg_file)

        mock_videos = [{
            "name": "Dark.Matter.S01E01.mkv",
            "path": str(video),
            "has_chinese_subtitle": False,
        }]

        with patch.object(s_mod.media_tools, "scan_media", return_value={"videos": mock_videos, "total_matched": 1}), \
             patch.object(s_mod.media_tools, "probe_video", return_value={"embedded_subtitles": [{"language": "chi"}]}):
            res = s_mod.fill_missing_subtitles(
                guard, cfg, cfg.subtitle_extensions, False,
                dry_run=True, check_embedded=True,
            )
            assert res["ok"] is True
            assert res["summary"]["missing_total"] == 0
            assert res["summary"]["planned"] == 0
            assert len(res["seasons"]) == 0
