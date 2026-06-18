#!/usr/bin/env python3

"""Create a video contact sheet.
"""

from __future__ import print_function

import datetime
import os
import sys
from argparse import ArgumentTypeError
from copy import deepcopy
from typing import List, Iterable
from urllib.parse import urlparse

import argparse
import configparser
import math
import textwrap
import queue
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from fractions import Fraction
from glob import glob
from glob import escape

import av
from av.codec.hwaccel import HWAccel
from av.filter import Graph
from PIL import Image, ImageDraw, ImageFont
import numpy
from jinja2 import Template
import texttable
import parsedatetime

here = os.path.abspath(os.path.dirname(__file__))

with open(os.path.join(here, "VERSION")) as f:
    VERSION = f.readline().strip()
__version__ = VERSION
__author__ = "Nils Amiet"


class Grid(namedtuple('Grid', ['x', 'y'])):
    def __str__(self):
        return "%sx%s" % (self.x, self.y)


class Frame(namedtuple('Frame', ['image', 'blurriness', 'timestamp', 'avg_color'])):
    """A captured frame held in memory as a PIL.Image."""
    pass


class Color(namedtuple('Color', ['r', 'g', 'b', 'a'])):
    def to_hex(self, component):
        h = hex(component).replace("0x", "").upper()
        return h if len(h) == 2 else "0" + h

    def __str__(self):
        return "".join([self.to_hex(x) for x in [self.r, self.g, self.b, self.a]])


TimestampPosition = Enum('TimestampPosition', "north south east west ne nw se sw center")
VALID_TIMESTAMP_POSITIONS = [x.name for x in TimestampPosition]

DEFAULT_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".config/vcsi.conf")
DEFAULT_CONFIG_SECTION = "vcsi"

DEFAULT_METADATA_FONT_SIZE = 16
DEFAULT_TIMESTAMP_FONT_SIZE = 12

# Defaults
DEFAULT_METADATA_FONT = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
DEFAULT_TIMESTAMP_FONT = "/usr/share/fonts/TTF/DejaVuSans.ttf"
FALLBACK_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial Unicode.ttf"]

# Replace defaults on Windows to support unicode/CJK and multiple fallbacks
if os.name == 'nt':
    DEFAULT_METADATA_FONT = "C:/Windows/Fonts/msgothic.ttc"
    DEFAULT_TIMESTAMP_FONT = "C:/Windows/Fonts/msgothic.ttc"
    FALLBACK_FONTS = [
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/Everson Mono.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/arial.ttf"
    ]

DEFAULT_CONTACT_SHEET_WIDTH = 1500
DEFAULT_DELAY_PERCENT = None
DEFAULT_START_DELAY_PERCENT = 7
DEFAULT_END_DELAY_PERCENT = DEFAULT_START_DELAY_PERCENT
DEFAULT_GRID_SPACING = None
DEFAULT_GRID_HORIZONTAL_SPACING = 5
DEFAULT_GRID_VERTICAL_SPACING = DEFAULT_GRID_HORIZONTAL_SPACING
DEFAULT_METADATA_POSITION = "top"
DEFAULT_METADATA_FONT_COLOR = "ffffff"
DEFAULT_BACKGROUND_COLOR = "000000"
DEFAULT_TIMESTAMP_FONT_COLOR = "ffffff"
DEFAULT_TIMESTAMP_BACKGROUND_COLOR = "000000aa"
DEFAULT_TIMESTAMP_BORDER_COLOR = "000000"
DEFAULT_TIMESTAMP_BORDER_SIZE = 1
DEFAULT_ACCURATE_DELAY_SECONDS = 1
DEFAULT_METADATA_MARGIN = 10
DEFAULT_METADATA_HORIZONTAL_MARGIN = DEFAULT_METADATA_MARGIN
DEFAULT_METADATA_VERTICAL_MARGIN = DEFAULT_METADATA_MARGIN
DEFAULT_CAPTURE_ALPHA = 255
DEFAULT_GRID_SIZE = Grid(4, 4)
DEFAULT_TIMESTAMP_HORIZONTAL_PADDING = 3
DEFAULT_TIMESTAMP_VERTICAL_PADDING = 3
DEFAULT_TIMESTAMP_HORIZONTAL_MARGIN = 5
DEFAULT_TIMESTAMP_VERTICAL_MARGIN = 5
DEFAULT_IMAGE_QUALITY = 100
DEFAULT_IMAGE_FORMAT = "jpg"
DEFAULT_TIMESTAMP_POSITION = TimestampPosition.se
DEFAULT_INTERVAL = None
DEFAULT_HWACCEL = None
DEFAULT_HWACCEL_DEVICE = None


class Config:
    metadata_font_size = DEFAULT_METADATA_FONT_SIZE
    metadata_font = DEFAULT_METADATA_FONT
    timestamp_font_size = DEFAULT_TIMESTAMP_FONT_SIZE
    timestamp_font = DEFAULT_TIMESTAMP_FONT
    fallback_fonts = FALLBACK_FONTS
    contact_sheet_width = DEFAULT_CONTACT_SHEET_WIDTH
    delay_percent = DEFAULT_DELAY_PERCENT
    start_delay_percent = DEFAULT_START_DELAY_PERCENT
    end_delay_percent = DEFAULT_END_DELAY_PERCENT
    grid_spacing = DEFAULT_GRID_SPACING
    grid_horizontal_spacing = DEFAULT_GRID_HORIZONTAL_SPACING
    grid_vertical_spacing = DEFAULT_GRID_VERTICAL_SPACING
    metadata_position = DEFAULT_METADATA_POSITION
    metadata_font_color = DEFAULT_METADATA_FONT_COLOR
    background_color = DEFAULT_BACKGROUND_COLOR
    timestamp_font_color = DEFAULT_TIMESTAMP_FONT_COLOR
    timestamp_background_color = DEFAULT_TIMESTAMP_BACKGROUND_COLOR
    timestamp_border_color = DEFAULT_TIMESTAMP_BORDER_COLOR
    timestamp_border_size = DEFAULT_TIMESTAMP_BORDER_SIZE
    accurate_delay_seconds = DEFAULT_ACCURATE_DELAY_SECONDS
    metadata_margin = DEFAULT_METADATA_MARGIN
    metadata_horizontal_margin = DEFAULT_METADATA_HORIZONTAL_MARGIN
    metadata_vertical_margin = DEFAULT_METADATA_VERTICAL_MARGIN
    capture_alpha = DEFAULT_CAPTURE_ALPHA
    grid_size = DEFAULT_GRID_SIZE
    timestamp_horizontal_padding = DEFAULT_TIMESTAMP_HORIZONTAL_PADDING
    timestamp_vertical_padding = DEFAULT_TIMESTAMP_VERTICAL_PADDING
    timestamp_horizontal_margin = DEFAULT_TIMESTAMP_HORIZONTAL_MARGIN
    timestamp_vertical_margin = DEFAULT_TIMESTAMP_VERTICAL_MARGIN
    quality = DEFAULT_IMAGE_QUALITY
    format = DEFAULT_IMAGE_FORMAT
    timestamp_position = DEFAULT_TIMESTAMP_POSITION
    interval = DEFAULT_INTERVAL
    hwaccel = DEFAULT_HWACCEL
    hwaccel_device = DEFAULT_HWACCEL_DEVICE

    @classmethod
    def load_configuration(cls, filename=DEFAULT_CONFIG_FILE):
        config = configparser.ConfigParser(default_section=DEFAULT_CONFIG_SECTION)
        config.read(filename)

        for config_entry in cls.__dict__.keys():
            # skip magic attributes
            if config_entry.startswith('__'):
                continue
            setattr(cls, config_entry, config.get(
                DEFAULT_CONFIG_SECTION,
                config_entry,
                fallback=getattr(cls, config_entry)
            ))
        # special cases
        # fallback_fonts is an array, it's reflected as comma separated list in config file
        fallback_fonts = config.get(DEFAULT_CONFIG_SECTION, 'fallback_fonts', fallback=None)
        if fallback_fonts:
            cls.fallback_fonts = comma_separated_string_type(fallback_fonts)


class MediaInfo(object):
    """Collect information about a video file using PyAV (libav* bindings).
    """

    def __init__(self, path, verbose=False):
        self.file_path = os.path.abspath(path)
        self.filename = os.path.basename(path)
        try:
            self.size_bytes = os.path.getsize(self.file_path)
        except OSError:
            self.size_bytes = 0
        self.size = self.human_readable_size(self.size_bytes)

        try:
            with av.open(path) as container:
                self._extract(container)
        except av.FFmpegError as ex:
            error_exit("Could not open '%s' with PyAV: %s" % (path, ex))

        if verbose:
            print(self.filename)
            print("%sx%s" % (self.sample_width, self.sample_height))
            print("%sx%s" % (self.display_width, self.display_height))
            print(self.duration)
            print(self.size)

    def _extract(self, container):
        # overall duration / bit rate from the container, with stream fallback below
        if container.duration is not None:
            self.duration_seconds = container.duration / float(av.time_base)
        else:
            self.duration_seconds = 0.0
        self.overall_bit_rate = int(container.bit_rate or 0)

        v_streams = [s for s in container.streams if s.type == 'video']
        a_streams = [s for s in container.streams if s.type == 'audio']

        if v_streams:
            self._extract_video(v_streams[0])
        else:
            self.video_codec = None
            self.video_codec_long = None
            self.video_bit_rate = None
            self.sample_aspect_ratio = None
            self.display_aspect_ratio = None
            self.frame_rate = None
            self.sample_width = self.sample_height = 0
            self.display_width = self.display_height = 0

        if a_streams:
            self._extract_audio(a_streams[0])
        else:
            self.audio_codec = None
            self.audio_codec_long = None
            self.audio_sample_rate = None
            self.audio_bit_rate = 256

        # cosmetic
        self.duration = MediaInfo.pretty_duration(self.duration_seconds)

        if self.display_width == 0:
            self.display_width = self.sample_width
        if self.display_height == 0:
            self.display_height = self.sample_height

    def _extract_video(self, stream):
        cc = stream.codec_context
        codec = cc.codec if cc else None
        self.video_codec = codec.name if codec else None
        self.video_codec_long = codec.long_name if codec else None
        self.video_bit_rate = int(stream.bit_rate) if stream.bit_rate else None

        # Prefer the video stream's own duration when available
        if stream.duration and stream.time_base:
            stream_secs = float(stream.duration * stream.time_base)
            if stream_secs > 0:
                self.duration_seconds = stream_secs

        self.sample_width = int(cc.width) if cc and cc.width else 0
        self.sample_height = int(cc.height) if cc and cc.height else 0

        rotation = self._read_rotation(stream)
        if rotation in (90, 270):
            self.sample_width, self.sample_height = self.sample_height, self.sample_width

        sar = cc.sample_aspect_ratio if cc else None
        if (sar is not None and sar.numerator and sar.denominator
                and sar.numerator != sar.denominator):
            self.sample_aspect_ratio = "%d:%d" % (sar.numerator, sar.denominator)
            self.display_width = int(self.sample_width * sar.numerator / sar.denominator)
            self.display_height = self.sample_height
        else:
            self.sample_aspect_ratio = (
                "%d:%d" % (sar.numerator, sar.denominator) if sar else "1:1"
            )
            self.display_width = self.sample_width
            self.display_height = self.sample_height

        dar = cc.display_aspect_ratio if cc else None
        if dar is not None and dar.numerator and dar.denominator:
            self.display_aspect_ratio = "%d:%d" % (dar.numerator, dar.denominator)
        else:
            self.display_aspect_ratio = None

        rate = stream.average_rate or stream.guessed_rate
        if rate and rate.denominator:
            try:
                self.frame_rate = round(float(rate), 3)
            except (ValueError, ZeroDivisionError):
                self.frame_rate = None
        else:
            self.frame_rate = None

    def _extract_audio(self, stream):
        cc = stream.codec_context
        codec = cc.codec if cc else None
        self.audio_codec = codec.name if codec else None
        self.audio_codec_long = codec.long_name if codec else None
        self.audio_sample_rate = int(cc.sample_rate) if cc and cc.sample_rate else None
        self.audio_bit_rate = int(stream.bit_rate) if stream.bit_rate else 256

    @staticmethod
    def _read_rotation(stream):
        """Return rotation in {0,90,180,270} or 0 if unknown."""
        rotation = 0
        try:
            rotate_meta = stream.metadata.get('rotate')
            if rotate_meta is not None:
                rotation = int(rotate_meta) % 360
        except (ValueError, TypeError, AttributeError):
            rotation = 0

        if rotation == 0:
            try:
                for sd in stream.side_data:
                    sd_type = getattr(sd, 'type', None)
                    name = getattr(sd_type, 'name', str(sd_type)) if sd_type else ''
                    if 'DISPLAYMATRIX' in name.upper() or 'DISPLAY_MATRIX' in name.upper():
                        deg = getattr(sd, 'rotation', None)
                        if deg is None and hasattr(sd, 'to_ndarray'):
                            # As a last resort try to parse from the matrix itself
                            continue
                        if deg is not None:
                            rotation = int(round(float(deg))) % 360
                            if rotation < 0:
                                rotation += 360
                        break
            except (AttributeError, TypeError):
                pass

        if rotation not in (0, 90, 180, 270):
            rotation = 0
        return rotation

    def human_readable_size(self, num, suffix='B'):
        """Converts a number of bytes to a human readable format
        """
        for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
            if abs(num) < 1024.0:
                return "%3.1f %s%s" % (num, unit, suffix)
            num /= 1024.0
        return "%.1f %s%s" % (num, 'Yi', suffix)

    @staticmethod
    def pretty_to_seconds(
            pretty_duration):
        """Converts pretty printed timestamp to seconds
        """
        millis_split = pretty_duration.split(".")
        millis = 0
        if len(millis_split) == 2:
            millis = int(millis_split[1])
            left = millis_split[0]
        else:
            left = pretty_duration

        left_split = left.split(":")

        if len(left_split) > 3:
            e = f"Timestamp {pretty_duration} ill formatted"
            raise ArgumentTypeError(e)

        if len(left_split) < 3:
            hours = 0
            minutes = int(left_split[0])
            seconds = int(left_split[1])
        else:
            hours = int(left_split[0])
            minutes = int(left_split[1])
            seconds = int(left_split[2])

        result = (millis / 1000.0) + seconds + minutes * 60 + hours * 3600
        return result

    @staticmethod
    def pretty_duration(
            seconds,
            show_centis=False,
            show_millis=False):
        """Converts seconds to a human readable time format
        """
        hours = int(math.floor(seconds / 3600))
        remaining_seconds = seconds - 3600 * hours

        minutes = math.floor(remaining_seconds / 60)
        remaining_seconds = remaining_seconds - 60 * minutes

        duration = ""

        if hours > 0:
            duration += "%s:" % (int(hours),)

        duration += "%s:%s" % (str(int(minutes)).zfill(2), str(int(math.floor(remaining_seconds))).zfill(2))

        if show_centis or show_millis:
            coeff = 1000 if show_millis else 100
            digits = 3 if show_millis else 2
            centis = math.floor((remaining_seconds - math.floor(remaining_seconds)) * coeff)
            duration += ".%s" % (str(int(centis)).zfill(digits))

        return duration

    @staticmethod
    def parse_duration(seconds):
        hours = int(math.floor(seconds / 3600))
        remaining_seconds = seconds - 3600 * hours

        minutes = math.floor(remaining_seconds / 60)
        remaining_seconds = remaining_seconds - 60 * minutes
        seconds = math.floor(remaining_seconds)

        millis = math.floor((remaining_seconds - math.floor(remaining_seconds)) * 1000)
        centis = math.floor((remaining_seconds - math.floor(remaining_seconds)) * 100)

        return {
            "hours": hours,
            "minutes": minutes,
            "seconds": seconds,
            "centis": centis,
            "millis": millis
        }

    def desired_size(self, width=Config.contact_sheet_width, vr_mode=False):
        """Computes the height based on a given width and fixed aspect ratio.
        Returns (width, height)
        """
        ratio = width / float(self.display_width)
        if vr_mode:
            desired_height = width
        else:
            desired_height = int(math.floor(self.display_height * ratio))
        return (width, desired_height)

    def template_attributes(self):
        """Returns the template attributes and values ready for use in the metadata header
        """
        return dict((x["name"], getattr(self, x["name"])) for x in MediaInfo.list_template_attributes())

    @staticmethod
    def list_template_attributes():
        """Returns a list a of all supported template attributes with their description and example
        """
        table = []
        table.append({"name": "size", "description": "File size (pretty format)", "example": "128.3 MiB"})
        table.append({"name": "size_bytes", "description": "File size (bytes)", "example": "4662788373"})
        table.append({"name": "filename", "description": "File name", "example": "video.mkv"})
        table.append({"name": "file_path", "description": "Full file path", "example": "/home/user/vids/video.mkv"})
        table.append({"name": "duration", "description": "Duration (pretty format)", "example": "03:07"})
        table.append({"name": "sample_width", "description": "Sample width (pixels)", "example": "1920"})
        table.append({"name": "sample_height", "description": "Sample height (pixels)", "example": "1080"})
        table.append({"name": "display_width", "description": "Display width (pixels)", "example": "1920"})
        table.append({"name": "display_height", "description": "Display height (pixels)", "example": "1080"})
        table.append({"name": "video_codec", "description": "Video codec", "example": "h264"})
        table.append({"name": "video_codec_long", "description": "Video codec (long name)",
                      "example": "H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10"})
        table.append({"name": "video_bit_rate", "description": "Video bitrate", "example": "4000"})
        table.append({"name": "display_aspect_ratio", "description": "Display aspect ratio", "example": "16:9"})
        table.append({"name": "sample_aspect_ratio", "description": "Sample aspect ratio", "example": "1:1"})
        table.append({"name": "audio_codec", "description": "Audio codec", "example": "aac"})
        table.append({"name": "audio_codec_long", "description": "Audio codec (long name)",
                      "example": "AAC (Advanced Audio Coding)"})
        table.append({"name": "audio_sample_rate", "description": "Audio sample rate (Hz)", "example": "44100"})
        table.append({"name": "audio_bit_rate", "description": "Audio bit rate (bits/s)", "example": "192000"})
        table.append({"name": "frame_rate", "description": "Frame rate (frames/s)", "example": "23.974"})
        table.append({"name": "overall_bit_rate", "description": "over_bit_rate", "example": 6000000})
        return table


class MediaCapture(object):
    """Capture frames from a video using PyAV.

    One container is opened per ``iter_captures``/``make_captures`` call and
    reused across every requested timestamp, so capturing N frames costs one
    decoder init, N seeks, and the corresponding decode windows.
    """

    def __init__(self, path, accurate=False, skip_delay_seconds=Config.accurate_delay_seconds,
                 hwaccel=None, hwaccel_device=None):
        self.path = path
        self.accurate = accurate
        self.skip_delay_seconds = skip_delay_seconds
        self.hwaccel = hwaccel
        self.hwaccel_device = hwaccel_device

    # ---- container / hwaccel plumbing ----

    def _open_container(self):
        kwargs = {}
        if self.hwaccel:
            hw_kwargs = {"device_type": self.hwaccel, "allow_software_fallback": True}
            if self.hwaccel_device is not None:
                hw_kwargs["device"] = self.hwaccel_device
            kwargs["hwaccel"] = HWAccel(**hw_kwargs)
        try:
            return av.open(self.path, **kwargs)
        except av.FFmpegError as ex:
            raise RuntimeError(
                "PyAV failed to open '%s': %s" % (self.path, ex)
            )

    @staticmethod
    def _build_vr_graph(stream):
        graph = Graph()
        src = graph.add_buffer(template=stream)
        v360 = graph.add(
            "v360",
            "input=hequirect:output=flat:h_fov=100:v_fov=100:in_stereo=sbs",
        )
        sink = graph.add("buffersink")
        src.link_to(v360)
        v360.link_to(sink)
        graph.configure()
        return graph

    # ---- capture API ----

    def iter_captures(self, timestamps, width, height, vr_mode=False):
        """Yield ``(original_index, PIL.Image)`` for every timestamp.

        The decoder runs in a single background thread (AUTO-threaded
        libavcodec internally) and pushes each frame onto a bounded queue;
        the main thread yields frames as they arrive so the caller's
        per-frame work overlaps with the next decode. Timestamps are
        decoded in ascending order so the demuxer makes one forward pass.
        """
        if not timestamps:
            return

        indexed = sorted(enumerate(timestamps), key=lambda kv: kv[1])

        try:
            cpu = os.cpu_count() or 1
        except Exception:
            cpu = 1
        q = queue.Queue(maxsize=max(1, cpu))
        ITEM, ERROR, DONE = "item", "error", "done"

        def producer():
            try:
                for item in self._iter_chunk(indexed, width, height, vr_mode):
                    q.put((ITEM, item))
            except BaseException as exc:
                q.put((ERROR, exc))
            finally:
                q.put((DONE, None))

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(producer)
            while True:
                kind, payload = q.get()
                if kind is ITEM:
                    yield payload
                elif kind is ERROR:
                    raise payload
                else:  # DONE
                    return

    def _iter_chunk(self, indexed, width, height, vr_mode):
        with self._open_container() as container:
            v_streams = [s for s in container.streams if s.type == "video"]
            if not v_streams:
                raise RuntimeError("No video stream in '%s'" % self.path)
            stream = v_streams[0]
            stream.thread_type = "AUTO"
            # Tell libavcodec to drop non-keyframes at the decoder level:
            # seek lands on the IDR at-or-before ts and the first frame the
            # decoder emits is that IDR, so we avoid catching up the GOP.
            stream.codec_context.skip_frame = "NONKEY"

            graph = self._build_vr_graph(stream) if vr_mode else None

            for orig_idx, ts in indexed:
                frame = self._capture_one(container, stream, ts)
                if vr_mode:
                    graph.vpush(frame)
                    frame = graph.vpull()
                img = frame.to_image()
                if img.size != (width, height):
                    img = img.resize((width, height), Image.BILINEAR)
                yield orig_idx, img

    def make_captures(self, timestamps, width, height, vr_mode=False):
        """Materialize captures as a list ordered by the input timestamps."""
        out = [None] * len(timestamps)
        for idx, img in self.iter_captures(timestamps, width, height, vr_mode):
            out[idx] = img
        return out

    def _capture_one(self, container, stream, ts):
        seek_ts = ts - self.skip_delay_seconds if self.accurate else ts
        if seek_ts < 0:
            seek_ts = 0.0

        offset = int(seek_ts / stream.time_base)
        container.seek(offset, stream=stream, backward=True, any_frame=False)

        for frame in container.decode(stream):
            if frame.time is None:
                continue
            return frame
        raise RuntimeError(
            "Could not decode any keyframe near %.3fs in '%s'"
            % (ts, self.path)
        )


# ---- module-level frame analyzers (work on PIL.Image directly) ----

def compute_avg_color(image):
    """Computes the average color of a PIL.Image."""
    i = image.convert("P")
    p = i.getcolors()
    total_count = 0
    avg_color = 0
    for count, color in p:
        total_count += count
        avg_color += count * color
    return avg_color / total_count


def compute_blurriness(image):
    """Computes the blurriness of a PIL.Image. Smaller means less blurry."""
    i = image.convert("L")
    a = numpy.asarray(i)
    b = abs(numpy.fft.rfft2(a))
    xs = b.flatten()
    srt = numpy.sort(xs)[::-1]
    length = int(math.floor(0.05 * len(srt)))
    if length == 0:
        return 1
    max_freq = numpy.median(srt[:length])
    return 1 / max_freq if max_freq != 0 else 1


def grid_desired_size(
        grid,
        media_info,
        width=Config.contact_sheet_width,
        horizontal_margin=Config.grid_horizontal_spacing,
        vr_mode=False):
    """Computes the size of the images placed on a mxn grid with given fixed width.
    Returns (width, height)
    """
    desired_width = (width - (grid.x - 1) * horizontal_margin) / grid.x
    desired_width = int(math.floor(desired_width))

    return media_info.desired_size(width=desired_width, vr_mode=vr_mode)


def total_delay_seconds(media_info, args):
    """Computes the total seconds to skip (beginning + ending).
    """
    start_delay_seconds = math.floor(media_info.duration_seconds * args.start_delay_percent / 100)
    end_delay_seconds = math.floor(media_info.duration_seconds * args.end_delay_percent / 100)
    delay = start_delay_seconds + end_delay_seconds
    return delay


def timestamp_generator(media_info, args):
    """Generates `num_samples` uniformly distributed timestamps over time.
    Timestamps will be selected in the range specified by start_delay_percent and end_delay percent.
    For example, `end_delay_percent` can be used to avoid making captures during the ending credits.
    """
    delay = total_delay_seconds(media_info, args)
    capture_interval = (media_info.duration_seconds - delay) / (args.num_samples + 1)

    if args.interval is not None:
        capture_interval = int(args.interval.total_seconds())
    start_delay_seconds = math.floor(media_info.duration_seconds * args.start_delay_percent / 100)
    time = start_delay_seconds + capture_interval

    for i in range(args.num_samples):
        yield (time, MediaInfo.pretty_duration(time, show_millis=True))
        time += capture_interval


def select_sharpest_images(
        media_info,
        media_capture,
        args):
    """Make `num_samples` captures and select `num_selected` captures out of these
    based on blurriness and color variety.
    """

    desired_size = grid_desired_size(
        args.grid,
        media_info,
        width=args.vcs_width,
        horizontal_margin=args.grid_horizontal_spacing,
        vr_mode=args.vr_mode)

    if args.manual_timestamps is None:
        timestamps = list(timestamp_generator(media_info, args))
    else:
        timestamps = [(MediaInfo.pretty_to_seconds(x), x) for x in args.manual_timestamps]

    ts_seconds = [t[0] for t in timestamps]
    total = len(timestamps)

    blurs: List[Frame] = [None] * total

    # Frame analysis (compute_blurriness / compute_avg_color) runs in a
    # consumer pool sized to the CPU; the decoder thread inside
    # iter_captures keeps the queue fed in parallel. PIL/NumPy release the
    # GIL during the heavy ops, so threads (not processes) suffice.
    try:
        cpu = os.cpu_count() or 1
    except Exception:
        cpu = 1
    consumer_count = max(1, cpu) if not args.fast else 1
    progress_lock = threading.Lock()
    done_counter = [0]

    def process_frame(orig_idx, image):
        if args.fast:
            blurriness, avg_color = 1, 0
        else:
            blurriness = compute_blurriness(image)
            avg_color = compute_avg_color(image)
        blurs[orig_idx] = Frame(
            image=image,
            blurriness=blurriness,
            timestamp=ts_seconds[orig_idx],
            avg_color=avg_color,
        )
        with progress_lock:
            done_counter[0] += 1
            print("Sampling... {}/{}".format(done_counter[0], total), end="\r")

    with ThreadPoolExecutor(max_workers=consumer_count) as pool:
        for orig_idx, image in media_capture.iter_captures(
                ts_seconds, desired_size[0], desired_size[1], args.vr_mode):
            pool.submit(process_frame, orig_idx, image)
    print()

    time_sorted = sorted(blurs, key=lambda x: x.timestamp)

    # group into num_selected groups
    if args.num_groups > 1:
        group_size = max(1, int(math.floor(len(time_sorted) / args.num_groups)))
        groups = chunks(time_sorted, group_size)

        # find top sharpest for each group
        selected_items: List[Frame] = [best(x) for x in groups]
    else:
        selected_items = time_sorted

    selected_items = select_color_variety(selected_items, args.num_selected)

    return selected_items, time_sorted


def select_color_variety(frames: Iterable[Frame], num_selected):
    """Select captures so that they are not too similar to each other.
    """
    avg_color_sorted = sorted(frames, key=lambda x: x.avg_color)
    min_color = avg_color_sorted[0].avg_color
    max_color = avg_color_sorted[-1].avg_color
    color_span = max_color - min_color
    min_color_distance = int(color_span * 0.05)

    blurriness_sorted = sorted(frames, key=lambda x: x.blurriness, reverse=True)

    selected_items = []
    unselected_items = []
    while blurriness_sorted:
        frame = blurriness_sorted.pop()

        if not selected_items:
            selected_items += [frame]
        else:
            color_distance = min([abs(frame.avg_color - x.avg_color) for x in selected_items])
            if color_distance < min_color_distance:
                # too close to existing selected frame
                # don't select unless we run out of frames
                unselected_items += [(frame, color_distance)]
            else:
                selected_items += [frame]

    missing_items_count = num_selected - len(selected_items)
    if missing_items_count > 0:
        remaining_items = sorted(unselected_items, key=lambda x: x[0].blurriness)
        selected_items += [x[0] for x in remaining_items[:missing_items_count]]

    return selected_items


def best(captures: Frame):
    """Returns the least blurry capture
    """
    return sorted(captures, key=lambda x: x.blurriness)[0]


def chunks(l, n):
    """ Yield successive n-sized chunks from l.
    """
    for i in range(0, len(l), n):
        yield l[i:i + n]


def draw_metadata(
        draw,
        args,
        header_line_height=None,
        header_lines=None,
        header_font=None,
        header_font_color=None,
        start_height=None):
    """Draw metadata header
    """
    h = start_height
    h += args.metadata_vertical_margin

    for line in header_lines:
        draw.text((args.metadata_horizontal_margin, h), line, font=header_font, fill=header_font_color)
        h += header_line_height

    h += args.metadata_vertical_margin

    return h


def max_line_length(
        media_info,
        metadata_font,
        header_margin,
        width=Config.contact_sheet_width,
        text=None):
    """Find the number of characters that fit in width with given font.
    """
    if text is None:
        text = media_info.filename

    max_width = width - 2 * header_margin

    max_length = 0
    for i in range(len(text) + 1):
        text_chunk = text[:i]
        text_width = 0 if len(text_chunk) == 0 else metadata_font.getlength(text_chunk)

        max_length = i
        if text_width > max_width:
            break

    return max_length


def prepare_metadata_text_lines(media_info, header_font, header_margin, width, template_path=None):
    """Prepare the metadata header text and return a list containing each line.
    """
    template = ""
    if template_path is None:
        template = """{{filename}}
        File size: {{size}}
        Duration: {{duration}}
        Dimensions: {{sample_width}}x{{sample_height}}"""
    else:
        with open(template_path) as f:
            template = f.read()

    params = media_info.template_attributes()
    template = Template(template).render(params)
    template_lines = template.split("\n")
    template_lines = [x.strip() for x in template_lines if len(x) > 0]

    header_lines = []
    for line in template_lines:
        remaining_chars = line
        while len(remaining_chars) > 0:
            max_metadata_line_length = max_line_length(
                media_info,
                header_font,
                header_margin,
                width=width,
                text=remaining_chars)
            wraps = textwrap.wrap(remaining_chars, max_metadata_line_length)
            header_lines.append(wraps[0])
            remaining_chars = remaining_chars[len(wraps[0]):].strip()

    return header_lines


def compute_timestamp_position(args, w, h, text_size, desired_size, rectangle_hpadding, rectangle_vpadding):
    """Compute the (x,y) position of the upper left and bottom right points of the rectangle surrounding timestamp text.
    """
    position = args.timestamp_position

    x_offset = 0
    if position in [TimestampPosition.west, TimestampPosition.nw, TimestampPosition.sw]:
        x_offset = args.timestamp_horizontal_margin
    elif position in [TimestampPosition.north, TimestampPosition.center, TimestampPosition.south]:
        x_offset = (desired_size[0] / 2) - (text_size[0] / 2) - rectangle_hpadding
    else:
        x_offset = desired_size[0] - text_size[0] - args.timestamp_horizontal_margin - 2 * rectangle_hpadding

    y_offset = 0
    if position in [TimestampPosition.nw, TimestampPosition.north, TimestampPosition.ne]:
        y_offset = args.timestamp_vertical_margin
    elif position in [TimestampPosition.west, TimestampPosition.center, TimestampPosition.east]:
        y_offset = (desired_size[1] / 2) - (text_size[1] / 2) - rectangle_vpadding
    else:
        y_offset = desired_size[1] - text_size[1] - args.timestamp_vertical_margin - 2 * rectangle_vpadding

    upper_left = (
        w + x_offset,
        h + y_offset
    )

    bottom_right = (
        upper_left[0] + text_size[0] + 2 * rectangle_hpadding,
        upper_left[1] + text_size[1] + 2 * rectangle_vpadding
    )

    return upper_left, bottom_right


def load_font(args, font_path, font_size, default_font_path):
    """Loads given font and defaults to fallback fonts if that fails."""
    if args.is_verbose:
        print("Loading font...")

    fonts = [font_path] + FALLBACK_FONTS
    if font_path == default_font_path:
        for font in fonts:
            if args.is_verbose:
                print("Trying to load font:", font)
            if os.path.exists(font):
                try:
                    return ImageFont.truetype(font, font_size)
                except OSError:
                    pass
        print("Falling back to default font.")
        return ImageFont.load_default()
    else:
        try:
            return ImageFont.truetype(font_path, font_size)
        except OSError:
            error_exit("Cannot load font: {}".format(font_path))


def compose_contact_sheet(
        media_info,
        frames,
        args):
    """Creates a video contact sheet with the media information in a header
    and the selected frames arranged on a mxn grid with optional timestamps
    """
    desired_size = grid_desired_size(
        args.grid,
        media_info,
        width=args.vcs_width,
        horizontal_margin=args.grid_horizontal_spacing,
        vr_mode=args.vr_mode)
    width = args.grid.x * (desired_size[0] + args.grid_horizontal_spacing) - args.grid_horizontal_spacing
    height = args.grid.y * (desired_size[1] + args.grid_vertical_spacing) - args.grid_vertical_spacing

    header_font = load_font(args, args.metadata_font, args.metadata_font_size, Config.metadata_font)
    timestamp_font = load_font(args, args.timestamp_font, args.timestamp_font_size, Config.timestamp_font)

    header_lines = prepare_metadata_text_lines(
        media_info,
        header_font,
        args.metadata_horizontal_margin,
        width,
        template_path=args.metadata_template_path)

    line_spacing_coefficient = 1.2
    header_line_height = int(args.metadata_font_size * line_spacing_coefficient)
    header_height = 2 * args.metadata_margin + len(header_lines) * header_line_height

    if args.metadata_position == "hidden":
        header_height = 0

    final_image_width = width
    final_image_height = height + header_height
    transparent = (255, 255, 255, 0)

    image = Image.new("RGBA", (final_image_width, final_image_height), args.background_color)
    image_capture_layer = Image.new("RGBA", (final_image_width, final_image_height), transparent)
    image_header_text_layer = Image.new("RGBA", (final_image_width, final_image_height), transparent)
    image_timestamp_layer = Image.new("RGBA", (final_image_width, final_image_height), transparent)
    image_timestamp_text_layer = Image.new("RGBA", (final_image_width, final_image_height), transparent)

    draw_header_text_layer = ImageDraw.Draw(image_header_text_layer)
    draw_timestamp_layer = ImageDraw.Draw(image_timestamp_layer)
    draw_timestamp_text_layer = ImageDraw.Draw(image_timestamp_text_layer)
    h = 0

    def draw_metadata_helper():
        """Draw metadata with fixed arguments
        """
        return draw_metadata(
            draw_header_text_layer,
            args,
            header_line_height=header_line_height,
            header_lines=header_lines,
            header_font=header_font,
            header_font_color=args.metadata_font_color,
            start_height=h)

    # draw metadata
    if args.metadata_position == "top":
        h = draw_metadata_helper()

    # draw capture grid
    w = 0
    frames = sorted(frames, key=lambda x: x.timestamp)
    for i, frame in enumerate(frames):
        f = frame.image.convert("RGBA")
        f.putalpha(args.capture_alpha)
        image_capture_layer.paste(f, (w, h))

        # show timestamp
        if args.show_timestamp:
            timestamp_time = MediaInfo.pretty_duration(frame.timestamp, show_centis=True)
            timestamp_duration = MediaInfo.pretty_duration(media_info.duration_seconds, show_centis=True)
            parsed_time = MediaInfo.parse_duration(frame.timestamp)
            parsed_duration = MediaInfo.parse_duration(media_info.duration_seconds)
            timestamp_args = {
                "TIME": timestamp_time,
                "DURATION": timestamp_duration,
                "THUMBNAIL_NUMBER": i + 1,
                "H": str(parsed_time["hours"]).zfill(2),
                "M": str(parsed_time["minutes"]).zfill(2),
                "S": str(parsed_time["seconds"]).zfill(2),
                "c": str(parsed_time["centis"]).zfill(2),
                "m": str(parsed_time["millis"]).zfill(3),
                "dH": str(parsed_duration["hours"]).zfill(2),
                "dM": str(parsed_duration["minutes"]).zfill(2),
                "dS": str(parsed_duration["seconds"]).zfill(2),
                "dc": str(parsed_duration["centis"]).zfill(2),
                "dm": str(parsed_duration["millis"]).zfill(3)
            }
            timestamp_text = args.timestamp_format.format(**timestamp_args)
            text_bbox = timestamp_font.getbbox(timestamp_text)
            (left, top, right, bottom) = text_bbox
            text_width = abs(right - left)
            text_height = abs(top - bottom)
            text_size = (text_width, text_height)

            # draw rectangle
            rectangle_hpadding = args.timestamp_horizontal_padding
            rectangle_vpadding = args.timestamp_vertical_padding

            upper_left, bottom_right = compute_timestamp_position(args, w, h, text_size, desired_size,
                                                                  rectangle_hpadding, rectangle_vpadding)

            if not args.timestamp_border_mode:
                draw_timestamp_layer.rectangle(
                    [upper_left, bottom_right],
                    fill=args.timestamp_background_color
                )
            else:
                offset_factor = args.timestamp_border_size
                offsets = [
                    (1, 0),
                    (-1, 0),
                    (0, 1),
                    (0, -1),
                    (1, 1),
                    (1, -1),
                    (-1, 1),
                    (-1, -1)
                ]

                final_offsets = []
                for offset_counter in range(1, offset_factor + 1):
                    final_offsets += [(x[0] * offset_counter, x[1] * offset_counter) for x in offsets]

                for offset in final_offsets:
                    # draw border first
                    draw_timestamp_text_layer.text(
                        (
                            upper_left[0] + rectangle_hpadding + offset[0],
                            upper_left[1] + rectangle_vpadding + offset[1]
                        ),
                        timestamp_text,
                        font=timestamp_font,
                        fill=args.timestamp_border_color,
                        anchor="lt"
                    )

            # draw timestamp
            draw_timestamp_text_layer.text(
                (
                    upper_left[0] + rectangle_hpadding,
                    upper_left[1] + rectangle_vpadding
                ),
                timestamp_text,
                font=timestamp_font,
                fill=args.timestamp_font_color,
                anchor="lt"
            )

        # update x position for next frame
        w += desired_size[0] + args.grid_horizontal_spacing

        # update y position
        if (i + 1) % args.grid.x == 0:
            h += desired_size[1] + args.grid_vertical_spacing

        # update x position
        if (i + 1) % args.grid.x == 0:
            w = 0

    # draw metadata
    if args.metadata_position == "bottom":
        h -= args.grid_vertical_spacing
        h = draw_metadata_helper()

    # alpha blend
    out_image = Image.alpha_composite(image, image_capture_layer)
    out_image = Image.alpha_composite(out_image, image_header_text_layer)
    out_image = Image.alpha_composite(out_image, image_timestamp_layer)
    out_image = Image.alpha_composite(out_image, image_timestamp_text_layer)

    return out_image


def save_image(args, image, media_info, output_path):
    """Save the image to `output_path`
    """
    image = image.convert("RGB")
    try:
        image.save(output_path, optimize=True, quality=args.image_quality)
        return True
    except KeyError:
        return False


def print_template_attributes():
    """Display all the available template attributes in a tabular format
    """
    table = MediaInfo.list_template_attributes()

    tab = texttable.Texttable()
    tab.set_cols_dtype(["t", "t", "t"])
    rows = [[x["name"], x["description"], x["example"]] for x in table]
    tab.add_rows(rows, header=False)
    tab.header(["Attribute name", "Description", "Example"])
    print(tab.draw())


def mxn_type(string):
    """Type parser for argparse. Argument of type "mxn" will be converted to Grid(m, n).
    An exception will be thrown if the argument is not of the required form
    """
    try:
        split = string.split("x")
        assert (len(split) == 2)
        m = int(split[0])
        assert (m >= 0)
        n = int(split[1])
        assert (n >= 0)
        return Grid(m, n)
    except (IndexError, ValueError, AssertionError):
        error = "Grid must be of the form mxn, where m is the number of columns and n is the number of rows."
        raise argparse.ArgumentTypeError(error)


def metadata_position_type(string):
    """Type parser for argparse. Argument of type string must be one of ["top", "bottom", "hidden"].
    An exception will be thrown if the argument is not one of these.
    """
    valid_metadata_positions = ["top", "bottom", "hidden"]

    lowercase_position = string.lower()
    if lowercase_position in valid_metadata_positions:
        return lowercase_position
    else:
        error = 'Metadata header position must be one of %s' % (str(valid_metadata_positions, ))
        raise argparse.ArgumentTypeError(error)


def hex_color_type(string):
    """Type parser for argparse. Argument must be an hexadecimal number representing a color.
    For example 'AABBCC' (RGB) or 'AABBCCFF' (RGBA). An exception will be raised if the argument
    is not of that form.
    """
    try:
        components = tuple(bytearray.fromhex(string))
        if len(components) == 3:
            components += (255,)
        c = Color(*components)
        return c
    except:
        error = "Color must be an hexadecimal number, for example 'AABBCC'"
        raise argparse.ArgumentTypeError(error)


def manual_timestamps(string):
    """Type parser for argparse. Argument must be a comma-separated list of frame timestamps.
    For example 1:11:11.111,2:22:22.222
    """
    try:
        timestamps = string.split(",")
        timestamps = [x.strip() for x in timestamps if x]

        # check whether timestamps are valid
        for t in timestamps:
            MediaInfo.pretty_to_seconds(t)

        return timestamps
    except Exception as e:
        print(e)
        error = "Manual frame timestamps must be comma-separated and of the form h:mm:ss.mmmm"
        raise argparse.ArgumentTypeError(error)


def timestamp_position_type(string):
    """Type parser for argparse. Argument must be a valid timestamp position"""
    try:
        return getattr(TimestampPosition, string)
    except AttributeError:
        error = "Invalid timestamp position: %s. Valid positions are: %s" % (string, VALID_TIMESTAMP_POSITIONS)
        raise argparse.ArgumentTypeError(error)


def interval_type(string):
    """Type parser for argparse. Argument must be a valid interval format.
    Supports any format supported by `parsedatetime`, including:
        * "30sec" (every 30 seconds)
        * "5 minutes" (every 5 minutes)
        * "1h" (every hour)
        * "2 hours 1 min and 30 seconds"
    """
    m = datetime.datetime.min
    cal = parsedatetime.Calendar()
    interval = cal.parseDT(string, sourceTime=m)[0] - m
    if interval == m:
        error = "Invalid interval format: {}".format(string)
        raise argparse.ArgumentTypeError(error)

    return interval


def comma_separated_string_type(string):
    """Type parser for argparse. Argument must be a comma-separated list of strings."""
    splits = string.split(",")
    splits = [x.strip() for x in splits]
    splits = [x for x in splits if len(x) > 0]
    return splits


def error(message):
    """Print an error message."""
    print("[ERROR] %s" % (message,))


def error_exit(message):
    """Print an error message and exit"""
    error(message)
    sys.exit(-1)


def main():
    """Program entry point
    """
    # Argument parser before actual argument parser to let the user overwrite the config path
    preargparser = argparse.ArgumentParser(add_help=False)
    preargparser.add_argument("-c", "--config", dest="configfile", default=None)
    preargs, _ = preargparser.parse_known_args()
    try:
        if preargs.configfile:
            # check if the given config file exists
            # abort if not, because the user wants to use a specific file and not the default config
            if os.path.exists(preargs.configfile):
                Config.load_configuration(preargs.configfile)
            else:
                error_exit("Could not find config file")
        else:
            # check if the config file exists and load it
            if os.path.exists(DEFAULT_CONFIG_FILE):
                Config.load_configuration(DEFAULT_CONFIG_FILE)
    except configparser.MissingSectionHeaderError as e:
        error_exit(e.message)

    parser = argparse.ArgumentParser(description="Create a video contact sheet",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("filenames", nargs="+")
    parser.add_argument(
        "-o", "--output",
        help="save to output file",
        dest="output_path")
    # adding --config to the main parser to display it when the user asks for help
    # the value is not important anymore
    parser.add_argument(
        "-c", "--config",
        help="Config file to load defaults from",
        default=DEFAULT_CONFIG_FILE
    )
    parser.add_argument(
        "--start-delay-percent",
        help="do not capture frames in the first n percent of total time",
        dest="start_delay_percent",
        type=int,
        default=Config.start_delay_percent)
    parser.add_argument(
        "--end-delay-percent",
        help="do not capture frames in the last n percent of total time",
        dest="end_delay_percent",
        type=int,
        default=Config.end_delay_percent)
    parser.add_argument(
        "--delay-percent",
        help="do not capture frames in the first and last n percent of total time",
        dest="delay_percent",
        type=int,
        default=Config.delay_percent)
    parser.add_argument(
        "--grid-spacing",
        help="number of pixels spacing captures both vertically and horizontally",
        dest="grid_spacing",
        type=int,
        default=Config.grid_spacing)
    parser.add_argument(
        "--grid-horizontal-spacing",
        help="number of pixels spacing captures horizontally",
        dest="grid_horizontal_spacing",
        type=int,
        default=Config.grid_horizontal_spacing)
    parser.add_argument(
        "--grid-vertical-spacing",
        help="number of pixels spacing captures vertically",
        dest="grid_vertical_spacing",
        type=int,
        default=Config.grid_vertical_spacing)
    parser.add_argument(
        "-w", "--width",
        help="width of the generated contact sheet",
        dest="vcs_width",
        type=int,
        default=Config.contact_sheet_width)
    parser.add_argument(
        "-g", "--grid",
        help="display frames on a mxn grid (for example 4x5). The special value zero (as in 2x0 or 0x5 or 0x0) is only allowed when combined with --interval or with --manual. Zero means that the component should be automatically deduced based on other arguments passed.",
        dest="grid",
        type=mxn_type,
        default=Config.grid_size)
    parser.add_argument(
        "-s", "--num-samples",
        help="number of samples",
        dest="num_samples",
        type=int,
        default=None)
    parser.add_argument(
        "-t", "--show-timestamp",
        action="store_true",
        help="display timestamp for each frame",
        dest="show_timestamp")
    parser.add_argument(
        "--metadata-font-size",
        help="size of the font used for metadata",
        dest="metadata_font_size",
        type=int,
        default=Config.metadata_font_size)
    parser.add_argument(
        "--metadata-font",
        help="TTF font used for metadata",
        dest="metadata_font",
        default=Config.metadata_font)
    parser.add_argument(
        "--timestamp-font-size",
        help="size of the font used for timestamps",
        dest="timestamp_font_size",
        type=int,
        default=Config.timestamp_font_size)
    parser.add_argument(
        "--timestamp-font",
        help="TTF font used for timestamps",
        dest="timestamp_font",
        default=Config.timestamp_font)
    parser.add_argument(
        "--metadata-position",
        help="Position of the metadata header. Must be one of ['top', 'bottom', 'hidden']",
        dest="metadata_position",
        type=metadata_position_type,
        default=Config.metadata_position)
    parser.add_argument(
        "--background-color",
        help="Color of the background in hexadecimal, for example AABBCC",
        dest="background_color",
        type=hex_color_type,
        default=hex_color_type(Config.background_color))
    parser.add_argument(
        "--metadata-font-color",
        help="Color of the metadata font in hexadecimal, for example AABBCC",
        dest="metadata_font_color",
        type=hex_color_type,
        default=hex_color_type(Config.metadata_font_color))
    parser.add_argument(
        "--timestamp-font-color",
        help="Color of the timestamp font in hexadecimal, for example AABBCC",
        dest="timestamp_font_color",
        type=hex_color_type,
        default=hex_color_type(Config.timestamp_font_color))
    parser.add_argument(
        "--timestamp-background-color",
        help="Color of the timestamp background rectangle in hexadecimal, for example AABBCC",
        dest="timestamp_background_color",
        type=hex_color_type,
        default=hex_color_type(Config.timestamp_background_color))
    parser.add_argument(
        "--timestamp-border-color",
        help="Color of the timestamp border in hexadecimal, for example AABBCC",
        dest="timestamp_border_color",
        type=hex_color_type,
        default=hex_color_type(Config.timestamp_border_color))
    parser.add_argument(
        "--template",
        help="Path to metadata template file",
        dest="metadata_template_path",
        default=None)
    parser.add_argument(
        "-m", "--manual",
        help="Comma-separated list of frame timestamps to use, for example 1:11:11.111,2:22:22.222",
        dest="manual_timestamps",
        type=manual_timestamps,
        default=None)
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="display verbose messages",
        dest="is_verbose")
    parser.add_argument(
        "--vr",
        action="store_true",
        help="vr mode",
        dest="vr_mode")
    parser.add_argument(
        "-a", "--accurate",
        action="store_true",
        help="""Make accurate captures. This capture mode is way slower than the default one
        but it helps when capturing frames from HEVC videos.""",
        dest="is_accurate")
    parser.add_argument(
        "-A", "--accurate-delay-seconds",
        type=int,
        default=Config.accurate_delay_seconds,
        help="""Fast skip to N seconds before capture time, then do accurate capture
        (decodes N seconds of video before each capture). This is used with accurate capture mode only.""",
        dest="accurate_delay_seconds")
    parser.add_argument(
        "--metadata-margin",
        type=int,
        default=Config.metadata_margin,
        help="Margin (in pixels) in the metadata header.",
        dest="metadata_margin")
    parser.add_argument(
        "--metadata-horizontal-margin",
        type=int,
        default=Config.metadata_horizontal_margin,
        help="Horizontal margin (in pixels) in the metadata header.",
        dest="metadata_horizontal_margin")
    parser.add_argument(
        "--metadata-vertical-margin",
        type=int,
        default=Config.metadata_vertical_margin,
        help="Vertical margin (in pixels) in the metadata header.",
        dest="metadata_vertical_margin")
    parser.add_argument(
        "--timestamp-horizontal-padding",
        type=int,
        default=Config.timestamp_horizontal_padding,
        help="Horizontal padding (in pixels) for timestamps.",
        dest="timestamp_horizontal_padding")
    parser.add_argument(
        "--timestamp-vertical-padding",
        type=int,
        default=Config.timestamp_vertical_padding,
        help="Vertical padding (in pixels) for timestamps.",
        dest="timestamp_vertical_padding")
    parser.add_argument(
        "--timestamp-horizontal-margin",
        type=int,
        default=Config.timestamp_horizontal_margin,
        help="Horizontal margin (in pixels) for timestamps.",
        dest="timestamp_horizontal_margin")
    parser.add_argument(
        "--timestamp-vertical-margin",
        type=int,
        default=Config.timestamp_vertical_margin,
        help="Vertical margin (in pixels) for timestamps.",
        dest="timestamp_vertical_margin")
    parser.add_argument(
        "--quality",
        type=int,
        default=Config.quality,
        help="Output image quality. Must be an integer in the range 0-100. 100 = best quality.",
        dest="image_quality")
    parser.add_argument(
        "-f", "--format",
        type=str,
        default=Config.format,
        help="Output image format. Can be any format supported by pillow. For example 'png' or 'jpg'.",
        dest="image_format")
    parser.add_argument(
        "-T", "--timestamp-position",
        type=timestamp_position_type,
        default=Config.timestamp_position,
        help="Timestamp position. Must be one of %s." % (VALID_TIMESTAMP_POSITIONS,),
        dest="timestamp_position")
    parser.add_argument(
        "-r", "--recursive",
        action="store_true",
        help="Process every file in the specified directory recursively.",
        dest="recursive")
    parser.add_argument(
        "--timestamp-border-mode",
        action="store_true",
        help="Draw timestamp text with a border instead of the default rectangle.",
        dest="timestamp_border_mode")
    parser.add_argument(
        "--timestamp-border-size",
        type=int,
        default=Config.timestamp_border_size,
        help="Size of the timestamp border in pixels (used only with --timestamp-border-mode).",
        dest="timestamp_border_size")
    parser.add_argument(
        "--capture-alpha",
        type=int,
        default=Config.capture_alpha,
        help="Alpha channel value for the captures (transparency in range [0, 255]). Defaults to 255 (opaque)",
        dest="capture_alpha")
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s version {version}".format(version=__version__))
    parser.add_argument(
        "--list-template-attributes",
        action="store_true",
        dest="list_template_attributes")
    parser.add_argument(
        "--hwaccel",
        type=str,
        default=Config.hwaccel,
        help="PyAV hardware-acceleration device_type, e.g. 'cuda', 'qsv', 'vaapi', 'drm', 'd3d11va', 'videotoolbox', 'amf'. Disabled by default.",
        dest="hwaccel")
    parser.add_argument(
        "--hwaccel-device",
        type=str,
        default=Config.hwaccel_device,
        help="Optional device index/path passed to PyAV's HWAccel (e.g. '0' for cuda, '/dev/dri/renderD128' for vaapi).",
        dest="hwaccel_device")
    parser.add_argument(
        "--interval",
        type=interval_type,
        default=Config.interval,
        help="Capture frames at specified interval. Interval format is any string supported by `parsedatetime`. For example '5m', '3 minutes 5 seconds', '1 hour 15 min and 20 sec' etc.",
        dest="interval")
    parser.add_argument(
        "--ignore-errors",
        action="store_true",
        help="Ignore any error encountered while processing files recursively and continue to the next file.",
        dest="ignore_errors")
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Do not overwrite output file if it already exists, simply ignore this file and continue processing other unprocessed files.",
        dest="no_overwrite"
    )
    parser.add_argument(
        "--exclude-extensions",
        type=comma_separated_string_type,
        default=[],
        help="Do not process files that end with the given extensions.",
        dest="exclude_extensions"
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Fast mode. Just make a contact sheet as fast as possible, regardless of output image quality. May mess up the terminal.",
        dest="fast")
    parser.add_argument(
        "-O", "--thumbnail-output",
        help="Save thumbnail files to the specified output directory. If set, the thumbnail files will not be deleted after successful creation of the contact sheet.",
        default=None,
        dest="thumbnail_output_path"
    )
    parser.add_argument(
        "-S", "--actual-size",
        help="Make thumbnails of actual size. In other words, thumbnails will have the actual 1:1 size of the video resolution.",
        action="store_true",
        dest="actual_size"
    )
    parser.add_argument(
        "--timestamp-format",
        help="Use specified timestamp format. Replaced values include: {TIME}, {DURATION}, {THUMBNAIL_NUMBER}, {H} (hours), {M} (minutes), {S} (seconds), {c} (centiseconds), {m} (milliseconds), {dH}, {dM}, {dS}, {dc} and {dm} (same as previous values but for the total duration). Example format: '{TIME} / {DURATION}'. Another example: '{THUMBNAIL_NUMBER}'. Yet another example: '{H}:{M}:{S}.{m} / {dH}:{dM}:{dS}.{dm}'.",
        default="{TIME}",
        dest="timestamp_format"
    )

    args = parser.parse_args()

    if args.list_template_attributes:
        print_template_attributes()
        sys.exit(0)

    def process_file_or_ignore(filepath, args):
        try:
            process_file(filepath, args)
        except Exception:
            if not args.ignore_errors:
                raise
            else:
                print("[WARN]: failed to process {} ... skipping.".format(filepath), file=sys.stderr)

    if args.recursive:
        for path in args.filenames:
            for root, subdirs, files in os.walk(path):
                for f in files:
                    filepath = os.path.join(root, f)
                    process_file_or_ignore(filepath, args)
    else:
        for path in args.filenames:
            if os.path.isdir(path):
                for filepath in os.listdir(path):
                    abs_filepath = os.path.join(path, filepath)
                    if not os.path.isdir(abs_filepath):
                        process_file_or_ignore(abs_filepath, args)

            else:
                files_to_process = glob(escape(path))
                if len(files_to_process) == 0:
                    files_to_process = [path]
                for filename in files_to_process:
                    process_file_or_ignore(filename, args)


def process_file(path, args):
    """Generate a video contact sheet for the file at given path
    """
    if args.is_verbose:
        print("Considering {}...".format(path))

    args = deepcopy(args)

    parsed = urlparse(path)
    is_url = bool(parsed.scheme) and parsed.scheme != "file"
    url_path = parsed.path if is_url else ""

    if not is_url and not os.path.exists(path):
        if args.ignore_errors:
            print("File does not exist, skipping: {}".format(path))
            return
        else:
            error_message = "File does not exist: {}".format(path)
            error_exit(error_message)

    if not is_url:
        file_extension = path.lower().split(".")[-1]
        if file_extension in args.exclude_extensions:
            print("[WARN] Excluded extension {}. Skipping.".format(file_extension))
            return

    output_path = args.output_path
    if not output_path:
        output_path = path + "." + args.image_format
        if is_url:
            url_path = url_path.replace("/", "_")
            if len(url_path) == 0:
                url_path = "out"
            output_path = f"{url_path}.{args.image_format}"
    elif os.path.isdir(output_path):
        output_path = os.path.join(output_path, os.path.basename(path) + "." + args.image_format)

    if args.no_overwrite:
        if os.path.exists(output_path):
            print("[INFO] contact-sheet already exists, skipping: {}".format(output_path))
            return

    print("Processing {}...".format(path))

    if args.interval is not None and args.manual_timestamps is not None:
        error_exit("Cannot use --interval and --manual at the same time.")

    if args.vcs_width != DEFAULT_CONTACT_SHEET_WIDTH and args.actual_size:
        error_exit("Cannot use --width and --actual-size at the same time.")

    if args.delay_percent is not None:
        args.start_delay_percent = args.delay_percent
        args.end_delay_percent = args.delay_percent

    args.num_groups = 5

    media_info = MediaInfo(
        path,
        verbose=args.is_verbose)
    media_capture = MediaCapture(
        path,
        accurate=args.is_accurate,
        skip_delay_seconds=args.accurate_delay_seconds,
        hwaccel=args.hwaccel,
        hwaccel_device=args.hwaccel_device,
    )

    # metadata margins
    if not args.metadata_margin == DEFAULT_METADATA_MARGIN:
        args.metadata_horizontal_margin = args.metadata_margin
        args.metadata_vertical_margin = args.metadata_margin

    if args.interval is None and args.manual_timestamps is None and (args.grid.x == 0 or args.grid.y == 0):
        error = "Row or column of size zero is only supported with --interval or --manual."
        error_exit(error)

    if args.interval is not None:
        total_delay = total_delay_seconds(media_info, args)
        selected_duration = media_info.duration_seconds - total_delay
        args.num_samples = math.floor(selected_duration / args.interval.total_seconds())
        args.num_selected = args.num_samples
        args.num_groups = args.num_samples

    # manual frame selection
    if args.manual_timestamps is not None:
        mframes_size = len(args.manual_timestamps)

        args.num_selected = mframes_size
        args.num_samples = mframes_size
        args.num_groups = mframes_size

    if args.interval is not None or args.manual_timestamps is not None:
        square_side = math.ceil(math.sqrt(args.num_samples))

        if args.grid == DEFAULT_GRID_SIZE:
            args.grid = Grid(square_side, square_side)
        elif args.grid.x == 0 and args.grid.y == 0:
            args.grid = Grid(square_side, square_side)
        elif args.grid.x == 0:
            # y is fixed
            x = math.ceil(args.num_samples / args.grid.y)
            args.grid = Grid(x, args.grid.y)
        elif args.grid.y == 0:
            # x is fixed
            y = math.ceil(args.num_samples / args.grid.x)
            args.grid = Grid(args.grid.x, y)

    args.num_selected = args.grid.x * args.grid.y
    if args.num_samples is None:
        args.num_samples = args.num_selected

    if args.num_groups is None:
        args.num_groups = args.num_selected

    # make sure num_selected is not too large
    if args.interval is None and args.manual_timestamps is None:
        if args.num_selected > args.num_groups:
            args.num_groups = args.num_selected

        if args.num_selected > args.num_samples:
            args.num_samples = args.num_selected

        # make sure num_samples is large enough
        if args.num_samples < args.num_selected or args.num_samples < args.num_groups:
            args.num_samples = args.num_selected
            args.num_groups = args.num_selected

    if args.grid_spacing is not None:
        args.grid_horizontal_spacing = args.grid_spacing
        args.grid_vertical_spacing = args.grid_spacing

    if args.actual_size:
        x = args.grid.x
        width = media_info.display_width
        args.vcs_width = x * width + (x - 1) * args.grid_horizontal_spacing

    selected_frames, _all_frames = select_sharpest_images(media_info, media_capture, args)

    print("Composing contact sheet...")
    image = compose_contact_sheet(media_info, selected_frames, args)

    is_save_successful = save_image(args, image, media_info, output_path)

    # save selected frames of the contact sheet to the predefined location in thumbnail_output_path
    thumbnail_output_path = args.thumbnail_output_path
    if thumbnail_output_path is not None:
        os.makedirs(thumbnail_output_path, exist_ok=True)
        print("Copying thumbnails to {} ...".format(thumbnail_output_path))
        thumbnail_ext = (args.image_format or "png").lstrip(".")
        for i, frame in enumerate(sorted(selected_frames, key=lambda x_frame: x_frame.timestamp)):
            thumbnail_filename = "{filename}.{number}.{extension}".format(
                filename=os.path.basename(path),
                number=str(i).zfill(4),
                extension=thumbnail_ext,
            )
            thumbnail_destination = os.path.join(thumbnail_output_path, thumbnail_filename)
            print(thumbnail_destination)
            frame.image.save(thumbnail_destination)

    if not is_save_successful:
        error_exit("Unsupported image format: %s." % (args.image_format,))


if __name__ == "__main__":
    main()
