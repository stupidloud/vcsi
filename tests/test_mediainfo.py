import argparse
from argparse import ArgumentTypeError

import pytest

from vcsi.vcsi import MediaInfo
from vcsi.vcsi import Grid, grid_desired_size
from vcsi.vcsi import timestamp_generator


def test_display_resolution(sample_video):
    mi = MediaInfo(sample_video)
    assert mi.display_width == 320
    assert mi.display_height == 180
    assert mi.sample_width == 320
    assert mi.sample_height == 180


def test_filename(sample_video):
    mi = MediaInfo(sample_video)
    assert mi.filename == "sample.mp4"


def test_duration_seconds(sample_video):
    mi = MediaInfo(sample_video)
    # The fixture is ~2 s; allow a tiny tolerance because container framing
    # can round up/down slightly versus the requested duration.
    assert 1.5 < mi.duration_seconds < 2.5


def test_pretty_duration(sample_video):
    mi = MediaInfo(sample_video)
    assert mi.duration in ("00:01", "00:02")


def test_size_bytes(sample_video):
    mi = MediaInfo(sample_video)
    assert mi.size_bytes > 0


def test_template_attributes(sample_video):
    mi = MediaInfo(sample_video)
    attributes = mi.template_attributes()
    assert attributes["video_codec"] == "h264"
    assert attributes["audio_codec"] == "aac"
    assert attributes["frame_rate"] == 24.0


def test_grid_desired_size(sample_video):
    mi = MediaInfo(sample_video)
    x, y = 2, 3
    grid = Grid(x, y)
    width = 800
    hmargin = 20
    s = grid_desired_size(grid, mi, width=width, horizontal_margin=hmargin)
    expected_width = (width - (x - 1) * hmargin) / x

    assert s[0] == expected_width


def test_desired_size(sample_video):
    mi = MediaInfo(sample_video)
    # 1280 width on a 320x180 source → 1280 * (180/320) = 720
    s = mi.desired_size(width=1280)
    assert s[1] == 720


def test_timestamps(sample_video):
    mi = MediaInfo(sample_video)
    mi.duration_seconds = 100
    start_delay_percent = 7
    end_delay_percent = 7
    interval = mi.duration_seconds - (start_delay_percent + end_delay_percent)
    num_samples = interval - 1

    args = argparse.Namespace()
    args.interval = None
    args.num_samples = num_samples
    args.start_delay_percent = start_delay_percent
    args.end_delay_percent = end_delay_percent

    expected_timestamp = start_delay_percent + 1
    for t in timestamp_generator(mi, args):
        assert int(t[0]) == expected_timestamp
        expected_timestamp += 1


def test_pretty_duration_centis_limit():
    pretty_duration = MediaInfo.pretty_duration(1.9999, show_centis=True)
    assert pretty_duration == "00:01.99"


def test_pretty_duration_millis_limit():
    pretty_duration = MediaInfo.pretty_duration(1.9999, show_millis=True)
    assert pretty_duration == "00:01.999"


def test_pretty_to_seconds():
    assert MediaInfo.pretty_to_seconds("1:11:11.111") == 4271.111
    assert MediaInfo.pretty_to_seconds("1:11:11") == 4271
    assert MediaInfo.pretty_to_seconds("1:01:00") == 3660
    pytest.raises(ArgumentTypeError, MediaInfo.pretty_to_seconds, "1:01:01:01:00")
