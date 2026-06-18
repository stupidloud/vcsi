"""Shared pytest fixtures.

Generates a tiny synthetic video on demand so the test suite no longer depends
on a checked-in ffprobe JSON or a real-world video file. The generator uses
PyAV directly so the produced file matches what the refactored MediaInfo
expects to consume.
"""

import os

import numpy as np
import pytest

import av


def _make_video(path, width=320, height=180, fps=24, duration_s=2.0,
                with_audio=True):
    """Create a small mp4 with a moving gradient and optional silent AAC track."""
    total_frames = int(round(fps * duration_s))
    container = av.open(path, mode="w")
    try:
        vstream = container.add_stream("h264", rate=fps)
        vstream.width = width
        vstream.height = height
        vstream.pix_fmt = "yuv420p"

        astream = None
        if with_audio:
            astream = container.add_stream("aac", rate=44100)
            astream.layout = "stereo"

        for i in range(total_frames):
            img = np.zeros((height, width, 3), dtype=np.uint8)
            img[..., 0] = (i * 8) % 256
            img[..., 1] = ((i * 4) + 64) % 256
            img[..., 2] = ((i * 2) + 128) % 256
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            for packet in vstream.encode(frame):
                container.mux(packet)

        for packet in vstream.encode():
            container.mux(packet)

        if astream is not None:
            samples = np.zeros((2, 1024), dtype=np.float32)
            audio_total = int(44100 * duration_s)
            written = 0
            while written < audio_total:
                aframe = av.AudioFrame.from_ndarray(samples, format="fltp",
                                                   layout="stereo")
                aframe.rate = 44100
                aframe.pts = written
                for packet in astream.encode(aframe):
                    container.mux(packet)
                written += samples.shape[1]
            for packet in astream.encode():
                container.mux(packet)
    finally:
        container.close()


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    """Path to a small synthesized mp4 with audio."""
    path = tmp_path_factory.mktemp("vcsi_fixtures") / "sample.mp4"
    _make_video(str(path))
    assert os.path.getsize(path) > 0
    return str(path)
