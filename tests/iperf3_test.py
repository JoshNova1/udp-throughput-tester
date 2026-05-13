"""iperf3 UDP test runner.

Sender mode (this Pi) -> connects to receiver running `iperf3 -s` on PORT.
Receiver mode -> spawns `iperf3 -s -1` (one-shot) so subsequent tests get a clean server.

Live samples are parsed from text output (`-i 1`). The final summary is
parsed from the trailing JSON block produced by `--json` which iperf3
emits after the human-readable lines when both flags are passed.

We actually run iperf3 twice-piped: once in text mode for live samples,
and capture the per-interval lines via regex. The final summary line
includes total loss / jitter / throughput which we parse separately.
"""
from __future__ import annotations

import re
import time

from ._common import Runner, have, popen

# Example lines we parse (iperf3 -u -i 1):
#   Sender per-interval (no loss/jitter, just total datagrams):
#     [  5]   0.00-1.00   sec  1.25 MBytes  10.5 Mbits/sec  901
#   Receiver per-interval (jitter + loss):
#     [  5]   0.00-1.00   sec  1.19 MBytes  10.0 Mbits/sec  0.043 ms  0/865 (0%)
#   Summary lines (tagged sender/receiver):
#     [  5]   0.00-10.00  sec  11.9 MBytes  10.0 Mbits/sec  0.123 ms  3/8650 (0.035%)  sender
LINE_RE = re.compile(
    r"^\[\s*\d+\]\s+"
    r"(?P<start>\d+\.\d+)-(?P<end>\d+\.\d+)\s+sec\s+"
    r"(?P<xfer>[\d.]+)\s+(?P<xfer_u>[KMG]?Bytes)\s+"
    r"(?P<rate>[\d.]+)\s+(?P<rate_u>[KMG]?bits/sec)"
    r"(?:\s+(?P<jitter>[\d.]+)\s+ms)?"
    r"(?:\s+(?P<lost>\d+)/(?P<total>\d+)\s+\((?P<lossp>[\d.]+)%\))?"
    r"(?:\s+(?P<datagrams>\d+))?"
    r"(?:\s+(?P<tag>sender|receiver))?\s*$"
)


def _to_mbps(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit.startswith("kbits"):
        return value / 1000.0
    if unit.startswith("mbits"):
        return value
    if unit.startswith("gbits"):
        return value * 1000.0
    return value


class Iperf3Sender(Runner):
    def _run(self, params: dict) -> None:
        if not have("iperf3"):
            raise RuntimeError("iperf3 not installed")

        peer = params["peer"]
        port = int(params.get("port", 5201))
        duration = int(params.get("duration", 30))
        bitrate_mbps = float(params.get("bitrate_mbps", 10))
        parallel = int(params.get("parallel", 1))
        reverse = bool(params.get("reverse", False))

        cmd = [
            "iperf3", "-c", peer, "-p", str(port),
            "-u", "-b", f"{bitrate_mbps}M",
            "-t", str(duration),
            "-i", "1",
            "-P", str(parallel),
            # --forceflush is only in iperf3 >= 3.6; the bundled iperf3-Cygwin
            # 3.1.3 chokes on it. Cygwin builds line-buffer enough for our
            # 1s -i interval samples to come through promptly without it.
        ]
        if reverse:
            cmd.append("-R")

        self.summary["cmd"] = " ".join(cmd)
        self.summary["started"] = time.time()
        self.summary["params"] = params

        self._proc = popen(cmd)

        samples = []
        last_summary_line = None
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            if self._stopping:
                break
            line = raw.rstrip()
            self.log(line, tag="iperf3")
            m = LINE_RE.match(line)
            if not m:
                continue
            d = m.groupdict()
            tag = d.get("tag")
            rate = _to_mbps(float(d["rate"]), d["rate_u"])
            sample = {
                "ts": time.time(),
                "start_s": float(d["start"]),
                "end_s": float(d["end"]),
                "throughput_mbps": rate,
                "jitter_ms": float(d["jitter"]) if d.get("jitter") else None,
                "lost": int(d["lost"]) if d.get("lost") else 0,
                "total": int(d["total"]) if d.get("total") else 0,
                "loss_pct": float(d["lossp"]) if d.get("lossp") else 0.0,
                "tag": tag,
            }
            if tag in ("sender", "receiver"):
                last_summary_line = sample
                self.summary.setdefault("totals", {})[tag] = sample
            else:
                # iperf3 3.1.3 (Cygwin Windows build) doesn't tag its summary
                # line; spot it by start_s==0 over the full test window plus
                # the presence of lost/total fields, and treat as summary.
                if d.get("lost") is not None and sample["start_s"] == 0.0:
                    last_summary_line = sample
                    self.summary.setdefault("totals", {}).setdefault("sender", sample)
                else:
                    samples.append(sample)
                    self.on_sample(sample)

        rc = self._proc.wait()
        self.summary["return_code"] = rc
        self.summary["samples_count"] = len(samples)
        if last_summary_line:
            self.summary["throughput_mbps"] = last_summary_line["throughput_mbps"]
            self.summary["loss_pct"] = last_summary_line["loss_pct"]
            self.summary["jitter_ms"] = last_summary_line["jitter_ms"]


class Iperf3Receiver(Runner):
    """Run an iperf3 server for a single client connection then exit."""

    def _run(self, params: dict) -> None:
        if not have("iperf3"):
            raise RuntimeError("iperf3 not installed")
        port = int(params.get("port", 5201))
        # -1 = one-shot, then exits. Avoids lingering servers.
        cmd = ["iperf3", "-s", "-p", str(port), "-1", "-i", "1"]
        self.summary["cmd"] = " ".join(cmd)
        self.summary["started"] = time.time()
        self._proc = popen(cmd)
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            if self._stopping:
                break
            line = raw.rstrip()
            self.log(line, tag="iperf3-s")
            m = LINE_RE.match(line)
            if not m:
                continue
            d = m.groupdict()
            rate = _to_mbps(float(d["rate"]), d["rate_u"])
            sample = {
                "ts": time.time(),
                "start_s": float(d["start"]),
                "end_s": float(d["end"]),
                "throughput_mbps": rate,
                "jitter_ms": float(d["jitter"]) if d.get("jitter") else None,
                "lost": int(d["lost"]) if d.get("lost") else 0,
                "total": int(d["total"]) if d.get("total") else 0,
                "loss_pct": float(d["lossp"]) if d.get("lossp") else 0.0,
                "tag": d.get("tag"),
                "role": "receiver",
            }
            self.on_sample(sample)
        rc = self._proc.wait()
        self.summary["return_code"] = rc
