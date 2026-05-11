"""Raw ffmpeg UDP throughput test.

Closest to the manual "ffmpeg send / ffmpeg receive over UDP" pattern.
Less metric-rich than iperf3, but kept because it's the original approach
some operators reach for and because it directly exercises the MPEG-TS
delivery path. Loss visibility comes from ffmpeg's `-stats` continuity-counter
errors on the receive side.
"""
from __future__ import annotations

import os
import re
import time

from ._common import Runner, have, popen
from ._ffmpeg_source import build_ffmpeg_input

# Parses "frame= 152 fps= 30 q=24.0 size= 1234kB time=00:00:05.06 bitrate=2000.0kbits/s speed=1x"
PROGRESS_RE = re.compile(
    r"frame=\s*(?P<frame>\d+).*?"
    r"size=\s*(?P<size>\d+)(?:KiB|kB)?.*?"
    r"time=(?P<time>[0-9:.]+).*?"
    r"bitrate=\s*(?P<bitrate>[\d.]+)\s*(?P<bunit>[kKmMgG]?)bits/s",
    re.IGNORECASE,
)
CC_ERROR_RE = re.compile(r"continuity check failed|missing.*PES packet|corrupt input")


def _to_mbps(value: float, unit: str) -> float:
    u = unit.lower()
    if u == "k":
        return value / 1000.0
    if u == "m":
        return value
    if u == "g":
        return value * 1000.0
    return value / 1_000_000.0


class FfmpegUdpSender(Runner):
    def _run(self, params: dict) -> None:
        if not have("ffmpeg"):
            raise RuntimeError("ffmpeg not installed")
        peer = params["peer"]
        port = int(params.get("port", 9100))

        cmd, ff_info = build_ffmpeg_input(
            params, output_url=f"udp://{peer}:{port}",
        )
        self.summary["cmd"] = " ".join(cmd)
        self.summary["ffmpeg_info"] = ff_info
        self.summary["started"] = time.time()
        self._proc = popen(cmd)
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            if self._stopping:
                break
            self.log(raw, tag="ffmpeg")
            m = PROGRESS_RE.search(raw)
            if not m:
                continue
            mbps = _to_mbps(float(m.group("bitrate")), m.group("bunit"))
            self.on_sample({
                "ts": time.time(),
                "frame": int(m.group("frame")),
                "send_mbps": mbps,
                "elapsed": m.group("time"),
                "role": "sender",
            })
        rc = self._proc.wait()
        self.summary["return_code"] = rc


class FfmpegUdpReceiver(Runner):
    def _run(self, params: dict) -> None:
        if not have("ffmpeg"):
            raise RuntimeError("ffmpeg not installed")
        port = int(params.get("port", 9100))
        duration = int(params.get("duration", 30)) + 5
        preview_path = params.get("preview_path")

        # timeout=5000000us (5s): if no UDP packets arrive for 5s, ffmpeg
        # gives up the read and exits. Required on Windows where -t below is
        # PRESENTATION-time, not wall-clock -- without it, after the sender
        # finishes, ffmpeg blocks on the socket forever waiting for more
        # input that will never come.
        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "info",
            "-stats",
            "-fflags", "+discardcorrupt",
            "-i", f"udp://0.0.0.0:{port}?fifo_size=1000000&overrun_nonfatal=1&timeout=5000000",
        ]
        # -t is per-output; emit it before each output so both terminate.
        # Without this, the preview output hits its limit but the sink runs
        # until input EOF, and ffmpeg waits for both -> hang.
        duration_args = ["-t", str(duration)]
        if preview_path:
            cmd += duration_args + [
                "-map", "0:v:0",
                "-vf", "fps=1,scale=480:-2",
                "-update", "1", "-q:v", "5",
                preview_path,
            ]
        # Mux to the OS null device with -c copy. We previously used "-f null
        # -" which truly discards bytes and so ffmpeg reports size=N/A
        # bitrate=N/A in its progress line -- our PROGRESS_RE then can't parse
        # the receive throughput and the UI shows nothing. Writing to NUL
        # (os.devnull) via the mpegts muxer with -c copy gives ffmpeg a real
        # byte count for its bitrate stat, at near-zero CPU cost (no decode,
        # no re-encode -- just remux to the OS null device).
        cmd += ["-c", "copy"] + duration_args + [
            "-f", "mpegts", os.devnull,
        ]
        self.summary["cmd"] = " ".join(cmd)
        self.summary["started"] = time.time()
        cc_errors = 0
        self._proc = popen(cmd)
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            if self._stopping:
                break
            self.log(raw, tag="ffmpeg")
            if CC_ERROR_RE.search(raw):
                cc_errors += 1
            m = PROGRESS_RE.search(raw)
            if not m:
                continue
            mbps = _to_mbps(float(m.group("bitrate")), m.group("bunit"))
            self.on_sample({
                "ts": time.time(),
                "frame": int(m.group("frame")),
                "recv_mbps": mbps,
                "elapsed": m.group("time"),
                "cc_errors": cc_errors,
                "role": "receiver",
            })
        rc = self._proc.wait()
        self.summary["return_code"] = rc
        self.summary["cc_errors"] = cc_errors
