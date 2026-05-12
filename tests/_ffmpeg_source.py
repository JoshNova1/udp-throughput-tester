"""Shared ffmpeg input/encoder builder.

Used by both the SRT and raw-UDP runners. Two source modes:

  source="testpattern"  — generate a deterministic testsrc2 + sine tone,
                          re-encoded to H.264 at the target bitrate.

  source="file"         — read a video file (looped if shorter than the
                          test duration). If no bitrate is specified, the
                          file's existing bitstream is passed through with
                          `-c copy` — zero CPU on the Pi, and the test
                          reflects exactly what that file's bytes-on-wire
                          look like (ideal: feed in a clip from a previous
                          event so the content is representative).

Hardware encoder auto-detection: on Pi 4 (and other ARM SBCs with V4L2 M2M),
`h264_v4l2m2m` is picked automatically. Pi 5 falls back to libx264 because
its VideoCore VII dropped the legacy H.264 encoder in hardware — libx264
software is more than fast enough on the Pi 5 cores for 1080p30 anyway.
"""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from typing import Optional

from ._common import hidden_subprocess_kwargs


@lru_cache(maxsize=1)
def has_v4l2_h264() -> bool:
    """True if ffmpeg has the V4L2 hardware H.264 encoder *and* the kernel
    exposes a usable M2M encoder device.

    Pi 4 ticks both boxes. Pi 5 typically has the encoder compiled into
    ffmpeg but no usable H.264 encoder device (VideoCore VII dropped legacy
    H.264 hardware encode), so we fall back to libx264 — which the Pi 5's
    A76 cores handle comfortably for our test patterns.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
            **hidden_subprocess_kwargs(),
        )
        if "h264_v4l2m2m" not in r.stdout:
            return False
    except Exception:
        return False
    # Pi 4 encoder M2M device is at /dev/video11 (decoder at /dev/video10).
    # Require its presence to avoid picking an encoder that fails at runtime.
    return os.path.exists("/dev/video11")


def pick_encoder(preference: str = "auto") -> str:
    """Resolve 'auto' to the best available H.264 encoder on this host."""
    if preference and preference != "auto":
        return preference
    return "h264_v4l2m2m" if has_v4l2_h264() else "libx264"


def list_clips(clips_dir: str) -> list[dict]:
    """List video files available in clips_dir."""
    out = []
    if not os.path.isdir(clips_dir):
        return out
    for name in sorted(os.listdir(clips_dir)):
        path = os.path.join(clips_dir, name)
        if not os.path.isfile(path):
            continue
        if not name.lower().endswith(
            (".mp4", ".mkv", ".ts", ".mov", ".m2ts", ".mpg", ".mpeg", ".webm")
        ):
            continue
        out.append({"name": name, "path": path, "size": os.path.getsize(path)})
    return out


def build_ffmpeg_input(params: dict, output_url: str) -> tuple[list[str], dict]:
    """Build the full ffmpeg command line.

    Parameters consumed (with sensible defaults):
        source         "testpattern" | "file"     (default: testpattern)
        source_file    path to file               (required if source=file)
        resolution     "1280x720" | "1920x1080" | ...  (default: 1280x720)
        framerate      int                        (default: 30)
        bitrate_mbps   float | None
                       - testpattern: required, defaults to 10
                       - file: if None and source=file, use -c copy
                               (push original bitstream untouched)
        duration       int seconds | None
        video_codec    "auto" | "h264_v4l2m2m" | "libx264"  (default: auto)
        pkt_size       int (mpegts UDP packetisation, default 1316)

    Returns (cmd_list, info_dict).
    """
    source = params.get("source", "testpattern")
    resolution = params.get("resolution", "1280x720")
    framerate = int(params.get("framerate", 30))
    bitrate_mbps = params.get("bitrate_mbps")
    duration = params.get("duration")
    video_codec_pref = params.get("video_codec", "auto")
    pkt_size = int(params.get("pkt_size", 1316))

    # -y: overwrite preview JPEGs from prior runs without prompting (ffmpeg
    # otherwise refuses to overwrite and exits 1 on the first such output).
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-stats"]
    info: dict = {
        "source": source,
        "resolution": resolution,
        "framerate": framerate,
    }

    # ---- Input ----
    if source == "file":
        source_file = params.get("source_file")
        if not source_file:
            raise RuntimeError("source_file required when source=file")
        if not os.path.isfile(source_file):
            raise RuntimeError(f"source_file does not exist: {source_file}")
        cmd += ["-re", "-stream_loop", "-1", "-i", source_file]
        info["source_file"] = source_file
    else:  # testpattern
        cmd += [
            "-re",
            "-f", "lavfi", "-i",
            f"testsrc2=size={resolution}:rate={framerate},format=yuv420p",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        ]

    # ---- Build codec opts (applied to the UDP output, NOT to the preview).
    # ffmpeg's per-output options apply to the NEXT output specified, so
    # codec flags must immediately precede the UDP MPEG-TS output. Putting
    # them before the preview JPEG output meant the UDP output silently
    # fell back to the mpegts default codec (mpeg2video).
    udp_codec_args: list = []
    if source == "file" and bitrate_mbps is None:
        udp_codec_args += ["-c", "copy"]
        info["encoder"] = "copy (no re-encode)"
        info["effective_bitrate"] = "native (from file)"
    else:
        rate = float(bitrate_mbps if bitrate_mbps is not None else 10)
        enc = pick_encoder(video_codec_pref)
        udp_codec_args += ["-c:v", enc]
        if enc == "libx264":
            udp_codec_args += ["-preset", "ultrafast", "-tune", "zerolatency"]
        udp_codec_args += [
            "-b:v", f"{rate}M",
            "-maxrate", f"{rate}M",
            "-bufsize", f"{int(rate * 2)}M",
            "-g", str(framerate * 2),
            "-c:a", "aac", "-b:a", "128k",
        ]
        info["encoder"] = enc
        info["effective_bitrate"] = f"{rate} Mbps"

    # -t is per-output too; emit before each output to bound them all.
    duration_args = ["-t", str(int(duration))] if duration else []

    # Optional sender-side preview: 1 fps JPEG so the operator can see what's
    # being sent (especially useful in file mode — confirms the right clip).
    # No codec opts here -- .jpg auto-selects mjpeg via the image2 muxer.
    preview_path = params.get("preview_path")
    if preview_path and not (source == "file" and bitrate_mbps is None):
        cmd += duration_args + [
            "-map", "0:v:0",
            "-vf", "fps=1,scale=480:-2",
            "-update", "1", "-q:v", "5",
            preview_path,
        ]

    # Main MPEG-TS output to the network -- this is where the codec opts go.
    cmd += udp_codec_args + duration_args + [
        "-f", "mpegts",
        f"{output_url}?pkt_size={pkt_size}"
        if "?" not in output_url
        else f"{output_url}&pkt_size={pkt_size}",
    ]

    return cmd, info


def render_preview_jpeg(params: dict, out_path: str) -> Optional[str]:
    """Render one frame of the chosen test pattern to a JPEG.

    Returns the path on success, None on failure.
    """
    resolution = params.get("resolution", "1280x720")
    framerate = int(params.get("framerate", 30))
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"testsrc2=size={resolution}:rate={framerate},format=yuv420p",
        "-frames:v", "1",
        "-q:v", "3",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       **hidden_subprocess_kwargs())
        return out_path
    except Exception:
        return None
