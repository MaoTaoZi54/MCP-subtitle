"""媒体扫描：杜比视界 / HDR 类型 / Atmos / DTS:X 等规格解析。"""

import json
from pathlib import Path
from unittest.mock import patch

import nas_mcp.media as media


class _FakeProc:
    returncode = 0
    stderr = ""

    def __init__(self, payload):
        self.stdout = json.dumps(payload, ensure_ascii=False)


def _probe(payload):
    with patch.object(media.subprocess, "run", return_value=_FakeProc(payload)):
        return media.probe_video(Path("/media/sample.mkv"), "ffprobe", 10)


def _video_stream(**extra):
    base = {
        "codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
        "pix_fmt": "yuv420p10le", "width": 3840, "height": 2160,
        "r_frame_rate": "24000/1000", "bit_rate": "65000000",
    }
    base.update(extra)
    return base


def _audio_stream(**extra):
    base = {"codec_type": "audio", "codec_name": "truehd", "channels": 8,
            "channel_layout": "7.1", "bit_rate": "4000000"}
    base.update(extra)
    return base


def test_dolby_vision_profile_8_1_and_truehd_atmos():
    payload = {
        "format": {"format_name": "matroska,webm", "duration": "7200.5", "bit_rate": "70000000"},
        "streams": [
            _video_stream(
                color_transfer="smpte2084", color_primaries="bt2020", color_space="bt2020nc",
                side_data_list=[{
                    "side_data_type": "DOVI configuration record",
                    "dv_profile": 8, "dv_level": 6,
                    "dv_bl_signal_compatibility_id": 1,
                    "bl_present_flag": 1, "el_present_flag": 0, "rpu_present_flag": 1,
                }],
            ),
            _audio_stream(profile="Dolby TrueHD + Dolby Atmos", tags={"language": "eng", "title": "Atmos"}),
            {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "chi"}},
        ],
    }
    out = _probe(payload)
    video = out["video"]
    assert video["dv_profile"] == 8
    assert video["dv_profile_label"] == "Profile 8.1"
    assert video["dv_compatibility_label"] == "兼容 HDR10"
    assert video["dv_bl_signal_compatibility_id"] == 1
    assert video["dv_layers"] == "BL+RPU"
    assert video["hdr_types"] == ["Dolby Vision", "HDR10"]
    assert video["dynamic_range"] == "Profile 8.1 · HDR10"
    assert video["hdr_primary"] == "Dolby Vision"
    assert video["bit_depth"] == 10
    assert video["fps"] == 24

    audio = out["audio"][0]
    assert audio["atmos"] is True
    assert audio["atmos_type"] == "TrueHD Atmos"
    assert audio["channel_layout_label"] == "7.1"
    assert audio["codec_label"] == "TrueHD"
    assert out["duration_seconds"] == 7200.5


def test_dolby_vision_profile_5_is_not_labelled_hdr10():
    side = {"side_data_type": "DOVI configuration record", "dv_profile": 5,
            "bl_present_flag": 1, "rpu_present_flag": 1}
    video = media._color_and_hdr(_video_stream(
        color_transfer="smpte2084", color_primaries="bt2020",
        side_data_list=[side],
    ))
    assert video["dv_profile_label"] == "Profile 5"
    assert video["hdr_types"] == ["Dolby Vision"]
    assert video["hdr10"] is False
    assert video["dv_layers"] == "BL+RPU"


def test_dd_joc_atmos_channel_layout():
    audio = media._audio_info(
        _audio_stream(codec_name="eac3", channels=6, channel_layout="5.1(side)",
                      profile="Dolby Digital Plus + Dolby Atmos"),
        {"language": "eng"}, "en",
    )
    assert audio["atmos"] is True
    assert audio["atmos_type"] == "DD+ JOC"
    assert audio["channel_layout_label"] == "5.1"
    assert audio["codec_label"] == "DD+"


def test_dts_x_and_dts_hd_ma():
    dtsx = media._audio_info(
        _audio_stream(codec_name="dts", channels=8, channel_layout="7.1", profile="DTS:X"),
        {}, None,
    )
    assert dtsx["dts_x"] is True
    assert dtsx["dts_profile"] == "DTS:X"
    assert dtsx["codec_label"] == "DTS:X"

    ma = media._audio_info(
        _audio_stream(codec_name="dts", channels=6, channel_layout="5.1", profile="DTS-HD MA"),
        {}, None,
    )
    assert ma["dts_x"] is False
    assert ma["dts_profile"] == "DTS-HD MA"


def test_hlg_and_sdr():
    hlg = media._color_and_hdr(_video_stream(color_transfer="arib-std-b67",
                                             color_primaries="bt2020"))
    assert hlg["hdr_types"] == ["HLG"]
    assert hlg["dynamic_range"] == "HLG"
    assert hlg["hlg"] is True

    sdr = media._color_and_hdr(_video_stream(color_transfer="bt709",
                                             color_primaries="bt709"))
    assert sdr["hdr"] is False
    assert sdr["sdr"] is True
    assert sdr["dynamic_range"] == "SDR"
