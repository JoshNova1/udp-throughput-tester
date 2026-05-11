"""SRT throughput test.

Uses srt-live-transmit on both ends when available -- that gives detailed
libsrt per-second stats (RTT, retransmits, recovered packets) that nothing
else exposes. Without it (typical on Windows where libsrt doesn't publish
binaries), we fall back to ffmpeg's native SRT protocol support: still a
real SRT flow on the wire, but only bandwidth-level stats from ffmpeg's
-stats output, no per-second RTT / retransmit numbers.

Sender (slt):
   ffmpeg (testsrc2 + sine, libx264) ──UDP local──► srt-live-transmit ──SRT──► peer:port
Sender (ffmpeg-only fallback):
   ffmpeg ── SRT ──► peer:port      (single process, ffmpeg muxes mpegts over SRT)

Receiver (slt):
   srt-live-transmit (listener) ──UDP local──► ffmpeg (-> NUL sink, parses bitrate)
Receiver (ffmpeg-only fallback):
   ffmpeg ── SRT listener ──► NUL    (single process)
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time

from ._common import Runner, have, popen, kill_tree
from ._ffmpeg_source import build_ffmpeg_input

# ffmpeg progress-line regex: matches "frame= N ... bitrate= X kbits/s"
_FF_PROGRESS_RE = re.compile(
    r"frame=\s*(?P<frame>\d+).*?"
    r"bitrate=\s*(?P<bitrate>[\d.]+)\s*(?P<bunit>[kKmMgG]?)bits/s",
    re.IGNORECASE,
)


def _ff_bitrate_to_mbps(value: float, unit: str) -> float:
    u = unit.lower()
    if u == "k": return value / 1000.0
    if u == "m": return value
    if u == "g": return value * 1000.0
    return value / 1_000_000.0

STAT_KEYS_SENDER = [
    "msRTT", "mbpsBandwidth", "mbpsSendRate",
    "pktSentTotal", "pktSndLossTotal", "pktRetransTotal",
    "pktSndDropTotal", "byteSent",
]
STAT_KEYS_RECEIVER = [
    "msRTT", "mbpsBandwidth", "mbpsRecvRate",
    "pktRecvTotal", "pktRcvLossTotal", "pktRcvRetransTotal",
    "pktRcvDropTotal", "byteRecv",
]


def _find_free_udp_port() -> int:
    """Pick a high random UDP port for the local ffmpeg<->srt-live-transmit hop."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _parse_stats_stream(stream, on_sample, keys, stop_flag):
    """Read JSON-stats output from srt-live-transmit and emit samples.

    srt-live-transmit -pf:json emits one JSON object per report interval,
    each on its own line OR pretty-printed. We accumulate braces.
    """
    buf = ""
    depth = 0
    in_obj = False
    for raw in stream:
        if stop_flag.is_set():
            break
        for ch in raw:
            if ch == "{":
                depth += 1
                in_obj = True
            if in_obj:
                buf += ch
            if ch == "}":
                depth -= 1
                if depth == 0 and in_obj:
                    try:
                        obj = json.loads(buf)
                    except json.JSONDecodeError:
                        obj = None
                    if obj:
                        sample = {"ts": time.time()}
                        for k in keys:
                            if k in obj:
                                sample[k] = obj[k]
                        on_sample(sample)
                    buf = ""
                    in_obj = False


class SrtSender(Runner):
    """Run srt-live-transmit + ffmpeg test pattern to a remote SRT listener."""

    def _run(self, params: dict) -> None:
        if not have("ffmpeg"):
            raise RuntimeError("ffmpeg not installed")

        peer = params["peer"]
        port = int(params.get("port", 9000))
        duration = int(params.get("duration", 30))
        latency_ms = int(params.get("latency_ms", 120))
        passphrase = params.get("passphrase") or ""

        srt_url = (
            f"srt://{peer}:{port}?mode=caller&latency={latency_ms}"
            f"&transtype=live"
        )
        if passphrase:
            srt_url += f"&passphrase={passphrase}&pbkeylen=16"

        self.summary["params"] = params
        self.summary["started"] = time.time()

        if have("srt-live-transmit"):
            self._run_via_slt(params, srt_url, duration)
        else:
            self._run_via_ffmpeg_native(params, srt_url, duration)

        self.summary["ended"] = time.time()

    def _run_via_slt(self, params, srt_url, duration):
        """Original two-process pipeline: ffmpeg -> local UDP -> srt-live-transmit -> SRT.
        Gives detailed libsrt stats parsed from -pf:json."""
        self.summary["pipeline"] = "srt-live-transmit"
        local_port = _find_free_udp_port()
        srt_cmd = [
            "srt-live-transmit",
            "-s:1", "-pf:json", "-loglevel:warning",
            f"udp://127.0.0.1:{local_port}", srt_url,
        ]
        ff_cmd, ff_info = build_ffmpeg_input(
            params, output_url=f"udp://127.0.0.1:{local_port}",
        )
        self.summary["cmd_srt"] = " ".join(srt_cmd)
        self.summary["cmd_ffmpeg"] = " ".join(ff_cmd)
        self.summary["ffmpeg_info"] = ff_info

        self._proc = popen(srt_cmd)
        time.sleep(0.4)
        self._ff = popen(ff_cmd)

        stop_flag = threading.Event()
        sample_count = {"n": 0}
        def on_sample(s):
            sample_count["n"] += 1
            self.on_sample({**s, "role": "sender"})

        reader = threading.Thread(
            target=_parse_stats_stream,
            args=(self._proc.stdout, on_sample, STAT_KEYS_SENDER, stop_flag),
            daemon=True,
        )
        reader.start()

        deadline = time.time() + duration + 10
        while time.time() < deadline:
            if self._stopping: break
            if self._ff.poll() is not None: break
            time.sleep(0.2)
        stop_flag.set()
        kill_tree(self._ff)
        kill_tree(self._proc)
        reader.join(timeout=2)
        self.summary["samples_count"] = sample_count["n"]

    def _run_via_ffmpeg_native(self, params, srt_url, duration):
        """Single-process fallback: ffmpeg encodes and writes directly to the
        SRT URL (libsrt linked into ffmpeg). Emits mbpsSendRate samples
        parsed from ffmpeg's stderr -- no msRTT / pktSent etc. without
        srt-live-transmit, but enough for an auto-test sweep."""
        self.summary["pipeline"] = "ffmpeg-native"
        ff_cmd, ff_info = build_ffmpeg_input(params, output_url=srt_url)
        self.summary["cmd"] = " ".join(ff_cmd)
        self.summary["ffmpeg_info"] = ff_info

        self._proc = popen(ff_cmd)
        assert self._proc.stdout is not None
        sample_count = {"n": 0}
        for raw in self._proc.stdout:
            if self._stopping:
                break
            self.log(raw, tag="ffmpeg")
            m = _FF_PROGRESS_RE.search(raw)
            if not m:
                continue
            mbps = _ff_bitrate_to_mbps(
                float(m.group("bitrate")), m.group("bunit"),
            )
            sample_count["n"] += 1
            self.on_sample({
                "ts": time.time(),
                "mbpsSendRate": mbps,
                "role": "sender",
            })
        self._proc.wait()
        self.summary["samples_count"] = sample_count["n"]
        self.summary["return_code"] = self._proc.returncode


class SrtReceiver(Runner):
    """Listen for an SRT stream; report SRT stats and (optionally) write a
    JPEG preview frame every second so you can visually confirm the stream
    is decoding cleanly."""

    def _run(self, params: dict) -> None:
        port = int(params.get("port", 9000))
        latency_ms = int(params.get("latency_ms", 120))
        passphrase = params.get("passphrase") or ""
        max_wait_s = int(params.get("max_wait_s", 30))
        run_for_s = int(params.get("duration", 30)) + 10  # connection tolerance
        preview_path = params.get("preview_path")  # set by app.py when enabled

        # srt-live-transmit accepts a host-less URL ("srt://:9000?..."),
        # but ffmpeg's libavformat SRT parser rejects it with "Bad
        # parameters". Use the explicit 0.0.0.0 form which both accept.
        slt_url = (
            f"srt://:{port}?mode=listener&latency={latency_ms}"
            f"&transtype=live"
        )
        ffmpeg_listener_url = (
            f"srt://0.0.0.0:{port}?mode=listener&latency={latency_ms}"
            f"&transtype=live"
        )
        if passphrase:
            slt_url += f"&passphrase={passphrase}&pbkeylen=16"
            ffmpeg_listener_url += f"&passphrase={passphrase}&pbkeylen=16"
        srt_url = slt_url  # used by slt path below

        self.summary["started"] = time.time()
        if not have("srt-live-transmit"):
            # ffmpeg-native fallback (Windows). Single-process: ffmpeg listens
            # on the SRT URL, mpegts-copies to NUL, emits bitrate via -stats.
            if not have("ffmpeg"):
                raise RuntimeError("neither srt-live-transmit nor ffmpeg installed")
            self.summary["pipeline"] = "ffmpeg-native"
            cmd = [
                "ffmpeg", "-hide_banner", "-nostdin", "-y",
                "-loglevel", "info", "-stats",
                "-fflags", "+discardcorrupt",
                "-i", ffmpeg_listener_url,
                "-c", "copy",
                "-t", str(run_for_s),
                "-f", "mpegts", os.devnull,
            ]
            self.summary["cmd"] = " ".join(cmd)
            self._proc = popen(cmd)
            self._preview = None
            assert self._proc.stdout is not None
            sample_count = {"n": 0}
            for raw in self._proc.stdout:
                if self._stopping:
                    break
                self.log(raw, tag="ffmpeg")
                m = _FF_PROGRESS_RE.search(raw)
                if not m:
                    continue
                mbps = _ff_bitrate_to_mbps(
                    float(m.group("bitrate")), m.group("bunit"),
                )
                sample_count["n"] += 1
                self.on_sample({
                    "ts": time.time(),
                    "mbpsRecvRate": mbps,
                    "role": "receiver",
                })
            self._proc.wait()
            self.summary["samples_count"] = sample_count["n"]
            self.summary["return_code"] = self._proc.returncode
            self.summary["ended"] = time.time()
            return

        # ---- srt-live-transmit path (with optional ffmpeg preview tap) ----
        self.summary["pipeline"] = "srt-live-transmit"
        if preview_path and have("ffmpeg"):
            # Two-process pipeline: srt-live-transmit terminates SRT and writes
            # the decoded MPEG-TS to a local UDP port; ffmpeg reads it and
            # produces both /dev/null sink and a JPEG snapshot per second.
            local_port = _find_free_udp_port()
            srt_cmd = [
                "srt-live-transmit",
                "-s:1", "-pf:json", "-loglevel:warning",
                srt_url,
                f"udp://127.0.0.1:{local_port}",
            ]
            preview_cmd = [
                "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
                "-fflags", "+discardcorrupt",
                "-i", f"udp://127.0.0.1:{local_port}?fifo_size=1000000&overrun_nonfatal=1",
                "-map", "0:v:0",
                "-vf", "fps=1,scale=480:-2",
                "-update", "1", "-q:v", "5",
                preview_path,
            ]
            self.summary["cmd"] = " ".join(srt_cmd) + "  |  " + " ".join(preview_cmd)
            self.summary["preview_path"] = preview_path
            self._proc = popen(srt_cmd)
            self._preview = popen(preview_cmd)
        else:
            cmd = [
                "srt-live-transmit",
                "-s:1", "-pf:json", "-loglevel:warning",
                srt_url,
                "file:///dev/null",
            ]
            self.summary["cmd"] = " ".join(cmd)
            self._proc = popen(cmd)
            self._preview = None

        self.summary["started"] = time.time()

        stop_flag = threading.Event()
        sample_count = {"n": 0}

        def on_sample(s):
            sample_count["n"] += 1
            self.on_sample({**s, "role": "receiver"})

        reader = threading.Thread(
            target=_parse_stats_stream,
            args=(self._proc.stdout, on_sample, STAT_KEYS_RECEIVER, stop_flag),
            daemon=True,
        )
        reader.start()

        deadline = time.time() + max_wait_s + run_for_s
        while time.time() < deadline:
            if self._stopping:
                break
            if self._proc.poll() is not None:
                break
            time.sleep(0.2)
        stop_flag.set()
        kill_tree(self._proc)
        if self._preview:
            kill_tree(self._preview)
        reader.join(timeout=2)
        self.summary["samples_count"] = sample_count["n"]
