"""ICMP ping RTT runner."""
from __future__ import annotations

import re
import sys
import time

from ._common import Runner, have, popen

_IS_WINDOWS = sys.platform == "win32"

# ---- Linux/macOS regexes --------------------------------------------------
# round-trip min/avg/max/mdev = 7.123/9.456/12.789/1.234 ms
POSIX_SUMMARY_RE = re.compile(
    r"min/avg/max/(?:mdev|stddev)\s*=\s*"
    r"(?P<min>[\d.]+)/(?P<avg>[\d.]+)/(?P<max>[\d.]+)/(?P<mdev>[\d.]+)"
)
# 64 bytes from 1.2.3.4: icmp_seq=1 ttl=64 time=8.42 ms
POSIX_LINE_RE = re.compile(r"icmp_seq=(?P<seq>\d+).*time=(?P<time>[\d.]+)\s*ms")

# ---- Windows regexes ------------------------------------------------------
# "Reply from 127.0.0.1: bytes=32 time<1ms TTL=128"
# "Reply from 127.0.0.1: bytes=32 time=12ms TTL=64"
WIN_LINE_RE = re.compile(
    r"Reply from\s+\S+:\s+bytes=\d+\s+time[=<](?P<time>\d+)\s*ms", re.IGNORECASE,
)
# Windows summary spans multiple lines; e.g.:
#   Minimum = 0ms, Maximum = 1ms, Average = 0ms
WIN_SUMMARY_RE = re.compile(
    r"Minimum\s*=\s*(?P<min>\d+)ms,\s*Maximum\s*=\s*(?P<max>\d+)ms,"
    r"\s*Average\s*=\s*(?P<avg>\d+)ms",
    re.IGNORECASE,
)


class PingRunner(Runner):
    def _run(self, params: dict) -> None:
        if not have("ping"):
            raise RuntimeError("ping not installed")
        peer = params["peer"]
        count = int(params.get("count", 20))
        interval = float(params.get("interval", 0.2))
        size = int(params.get("size", 56))

        if _IS_WINDOWS:
            # Windows ping uses different flags. There is no -i (interval)
            # equivalent -- Windows ping always waits ~1s between echoes
            # unless you use the undocumented `-p` or just live with it.
            # NB: -c on Windows is "routing compartment id" and demands
            # admin elevation, so we MUST translate.
            cmd = [
                "ping", "-n", str(count), "-l", str(size),
                "-w", "2000",  # 2s timeout per echo, ms on Windows
                peer,
            ]
            line_re, summary_re = WIN_LINE_RE, WIN_SUMMARY_RE
            on_posix = False
        else:
            cmd = ["ping", "-c", str(count), "-i", str(interval),
                   "-s", str(size), "-W", "2", peer]
            line_re, summary_re = POSIX_LINE_RE, POSIX_SUMMARY_RE
            on_posix = True

        self.summary["cmd"] = " ".join(cmd)
        self.summary["started"] = time.time()
        self._proc = popen(cmd)
        replies = []
        seq_counter = 0
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            if self._stopping:
                break
            line = raw.rstrip()
            self.log(line, tag="ping")
            m = line_re.search(line)
            if m:
                rtt_ms = float(m.group("time"))
                replies.append(rtt_ms)
                seq = int(m.group("seq")) if on_posix else (len(replies))
                self.on_sample({
                    "ts": time.time(),
                    "seq": seq,
                    "rtt_ms": rtt_ms,
                })
                continue
            m = summary_re.search(line)
            if m:
                self.summary["rtt_min_ms"] = float(m.group("min"))
                self.summary["rtt_avg_ms"] = float(m.group("avg"))
                self.summary["rtt_max_ms"] = float(m.group("max"))
                # Windows ping doesn't report mdev; fall back to (max-min)/2.
                self.summary["rtt_mdev_ms"] = (
                    float(m.group("mdev")) if on_posix
                    else max(0.0, (float(m.group("max")) - float(m.group("min"))) / 2)
                )
        rc = self._proc.wait()
        self.summary["return_code"] = rc
        self.summary["replies"] = len(replies)
        self.summary["expected"] = count
        self.summary["loss_pct"] = 100.0 * (count - len(replies)) / count if count else 0.0
