"""Auto-test orchestrator.

Finds the maximum stable bandwidth between this node and the peer when
running `streams` concurrent SRT flows. The number reported is the
*per-stream* maximum — the total bandwidth carried by the network is
`per-stream × streams`.

Three phases:

  1. Probe   — start at `start_mbps` per stream, double after each pass
               until a sub-test fails or `ceiling_mbps` is reached.

  2. Narrow  — binary search between the last passing per-stream rate
               and the first failing rate until the range is below
               `step_mbps` (default 1 Mbps). The lower bound at the end
               of the search is the maximum stable per-stream rate.

  3. Soak    — run a sustained test at `max_stable * soak_ratio`
               per stream (default 0.95, 5% safety margin) for
               `soak_duration_s` seconds. Confirms the network can
               sustain that combined rate over time.

A test "passes" when all of the following hold across the bundle:

  - aggregate packet loss ≤ loss_pct_max         (default: 0.5%)
  - worst-case RTT        ≤ rtt_ms_max           (default: 250 ms)
  - aggregate delivered   ≥ deliver_pct_min      (default: 90%)
    (of the per-stream × streams target)

Each phase emits progress samples via on_sample. The final summary
includes per-stream stats AND a quality-recommendations block keyed
to common H.264 resolution/framerate presets.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from typing import Optional

import requests

from ._common import Runner, kill_tree, hidden_subprocess_kwargs
from .srt_test import SrtSender

_IS_WINDOWS = sys.platform == "win32"

# Windows: "Average = 12ms"  /  POSIX: "rtt min/avg/max/mdev = 1.0/12.3/100.0/5.0 ms"
_PING_AVG_WIN = re.compile(r"Average\s*=\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
_PING_AVG_POSIX = re.compile(r"min/avg/max[^=]*=\s*[\d.]+/([\d.]+)/")


def _ping_avg_ms(peer: str, duration_s: int) -> Optional[float]:
    """Run ping for the duration of the probe and return the average RTT
    in milliseconds. Done in parallel with the SRT load test so the
    RTT reflects performance UNDER LOAD, which is what matters."""
    count = max(3, duration_s)
    if _IS_WINDOWS:
        # -n count, -w timeout-per-reply-ms. ICMP echo, blocks until done.
        cmd = ["ping", "-n", str(count), "-w", "2000", peer]
    else:
        cmd = ["ping", "-c", str(count), "-W", "2", peer]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=count + 10,
            **hidden_subprocess_kwargs(),
        )
        out = (r.stdout or "") + (r.stderr or "")
        m = _PING_AVG_WIN.search(out) if _IS_WINDOWS else _PING_AVG_POSIX.search(out)
        return float(m.group(1)) if m else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Defaults — tuneable from params
# ---------------------------------------------------------------------------
DEFAULTS = {
    "start_mbps":          5.0,
    "ceiling_mbps":        100.0,
    "probe_duration_s":    15,
    "soak_duration_s":     60,
    "soak_ratio":          0.95,
    "step_mbps":           1.0,
    "loss_pct_max":        0.5,
    "rtt_ms_max":          250.0,
    "deliver_pct_min":     90.0,
    "max_attempts":        20,
    "settle_s":            1.0,
}


# ---------------------------------------------------------------------------
# Video quality presets — H.264 industry midpoints (Mbps)
# Used to map a measured per-stream max into "you could comfortably send..."
# ---------------------------------------------------------------------------
QUALITY_PRESETS = [
    ("480p30",  1.5,  "Standard definition, smooth motion"),
    ("480p60",  2.5,  "Standard definition, sports/fast motion"),
    ("720p30",  3.0,  "HD, smooth motion (typical OBS / Zoom)"),
    ("720p60",  4.5,  "HD, sports/fast motion"),
    ("1080p30", 5.0,  "Full HD, smooth motion (YouTube recommended)"),
    ("1080p60", 7.5,  "Full HD, sports/fast motion (YouTube recommended)"),
    ("1440p30", 12.0, "2K, smooth motion"),
    ("1440p60", 18.0, "2K, sports/fast motion"),
    ("2160p30", 30.0, "4K, smooth motion"),
    ("2160p60", 50.0, "4K, sports/fast motion"),
]


def recommend_quality(per_stream_mbps: float, safety_factor: float = 0.85) -> dict:
    """Given a per-stream maximum stable bitrate, return preset recommendations.

    `safety_factor` is the headroom factor — we don't recommend running a
    preset right at the network's edge. 0.85 keeps 15% margin for the bursty
    nature of real H.264 encoding (I-frames cost more than the average).
    """
    headroom = per_stream_mbps * safety_factor
    comfortable, tight, infeasible = [], [], []
    for name, bitrate, desc in QUALITY_PRESETS:
        entry = {"preset": name, "bitrate_mbps": bitrate, "description": desc}
        if bitrate <= headroom:
            comfortable.append({**entry, "fit": "comfortable"})
        elif bitrate <= per_stream_mbps:
            tight.append({**entry, "fit": "tight"})
        else:
            infeasible.append({**entry, "fit": "infeasible"})
    return {
        "per_stream_max_mbps":  round(per_stream_mbps, 2),
        "safety_factor":        safety_factor,
        "headroom_mbps":        round(headroom, 2),
        "highest_safe":         comfortable[-1] if comfortable else None,
        "comfortable":          comfortable,
        "tight":                tight,
        "infeasible":           infeasible,
    }


class AutoTestSender(Runner):
    """Sender-side auto-test. Each sub-test runs N concurrent SRT senders."""

    def _run(self, params: dict) -> None:
        cfg = {**DEFAULTS}
        for k in DEFAULTS:
            if params.get(k) is not None:
                cfg[k] = type(DEFAULTS[k])(params[k])

        peer = params["peer"]
        peer_api_port = int(params.get("peer_api_port", 8080))
        port_base = int(params.get("port", 9000))
        latency_ms = int(params.get("latency_ms", 120))
        passphrase = params.get("passphrase", "")
        streams = max(1, min(int(params.get("streams", 1)), 8))
        source = params.get("source", "testpattern")
        source_file = params.get("source_file")
        resolution = params.get("resolution", "1280x720")
        framerate = int(params.get("framerate", 30))

        peer_listen_url = f"http://{peer}:{peer_api_port}/api/peer/listen"

        self.summary["params"] = params
        self.summary["streams"] = streams
        self.summary["thresholds"] = {
            "loss_pct_max":    cfg["loss_pct_max"],
            "rtt_ms_max":      cfg["rtt_ms_max"],
            "deliver_pct_min": cfg["deliver_pct_min"],
        }
        self.summary["attempts"] = []
        self.summary["started"] = time.time()

        def emit(phase: str, status: str, **extra) -> None:
            self.on_sample({
                "ts": time.time(),
                "phase": phase,
                "status": status,
                "streams": streams,
                **extra,
            })

        def run_subtest(per_stream_mbps: float, duration_s: int, phase: str) -> dict:
            """Run a single N-stream sub-test at a given per-stream rate."""
            if self._stopping:
                return {"error": "stopped"}

            # 1. Tell the peer to start N receivers on consecutive ports.
            try:
                r = requests.post(peer_listen_url, json={
                    "mode":       "srt",
                    "duration":   duration_s + 8,
                    "port":       port_base,
                    "streams":    streams,
                    "latency_ms": latency_ms,
                    "passphrase": passphrase,
                }, timeout=5)
                r.raise_for_status()
            except requests.RequestException as exc:
                return {"error": f"peer listener failed: {exc}",
                        "phase": phase, "streams": streams,
                        "target_per_stream_mbps": per_stream_mbps}

            time.sleep(0.8)  # let SRT listeners bind

            # 1b. Kick off an RTT probe in parallel with the load test.
            #     ffmpeg-native SRT doesn't expose per-packet libsrt stats
            #     (msRTT etc.) so we measure RTT independently via ICMP
            #     echo, concurrent with the stream so the result reflects
            #     latency UNDER LOAD -- which is the only useful kind.
            ping_result: dict = {"avg_rtt_ms": None}
            def _ping_worker():
                ping_result["avg_rtt_ms"] = _ping_avg_ms(peer, duration_s)
            ping_thread = threading.Thread(target=_ping_worker, daemon=True)
            ping_thread.start()

            # 2. Spawn N local senders, each reading the same source pattern,
            #    each pointed at its own port on the peer.
            senders: list[SrtSender] = []
            sample_buckets: list[list[dict]] = [[] for _ in range(streams)]
            done_events = [threading.Event() for _ in range(streams)]

            def make_callbacks(idx: int):
                def on_s(s):
                    sample_buckets[idx].append(s)
                def on_d(summary):
                    done_events[idx].set()
                return on_s, on_d

            for i in range(streams):
                sender = SrtSender()
                sender.log_path = self.log_path
                sender.on_sample, sender.on_done = make_callbacks(i)
                sender.start({
                    "peer":           peer,
                    "port":           port_base + i,
                    "duration":       duration_s,
                    "bitrate_mbps":   per_stream_mbps,
                    "latency_ms":     latency_ms,
                    "passphrase":     passphrase,
                    "source":         source,
                    "source_file":    source_file,
                    "resolution":     resolution,
                    "framerate":      framerate,
                })
                senders.append(sender)

            # 3. Wait for all senders to complete (or until stopping).
            deadline = time.time() + duration_s + 25
            while time.time() < deadline:
                if self._stopping or all(e.is_set() for e in done_events):
                    break
                time.sleep(0.3)
            if self._stopping:
                for s in senders:
                    s.stop()
            for s in senders:
                s.join(timeout=5)

            # Let the ping probe and the receiver's history write finish.
            ping_thread.join(timeout=2)
            time.sleep(0.6)  # peer's on_done -> history INSERT settles

            # Fetch the receiver's view of this probe so we can compare what
            # was actually delivered vs what we attempted to send. ffmpeg-
            # native gives us the cumulative average bitrate on both ends;
            # subtracting yields a real packet-loss approximation that we
            # otherwise lost when dropping srt-live-transmit.
            recv_mbps_total: Optional[float] = None
            try:
                hist_url = f"http://{peer}:{peer_api_port}/api/history?limit=1"
                rh = requests.get(hist_url, timeout=5).json()
                if rh and isinstance(rh, list):
                    rec_summary = rh[0].get("summary", {}) or {}
                    totals = [
                        (v or {}).get("throughput_mbps")
                        for v in rec_summary.values()
                        if isinstance(v, dict)
                    ]
                    totals = [t for t in totals if t is not None]
                    if totals:
                        recv_mbps_total = round(sum(totals), 2)
            except Exception:
                recv_mbps_total = None

            measured_rtt_ms = ping_result["avg_rtt_ms"]

            # 4. Reduce samples to per-stream and aggregate metrics.
            #    Accept samples from either pipeline:
            #      - srt-live-transmit: has msRTT + pktSent* + mbpsSendRate
            #      - ffmpeg-native fallback: mbpsSendRate only (no RTT/loss)
            per_stream = []
            for i, samples in enumerate(sample_buckets):
                useful = [s for s in samples if ("msRTT" in s) or ("mbpsSendRate" in s)]
                # Diagnostic carry-over from the per-sender summary: lets us
                # see whether a "no samples" or low-sample probe was caused
                # by slt emitting little, the callback chain dropping, or
                # the wrong pipeline being picked.
                snd_summary = senders[i].summary if i < len(senders) else {}
                diag = {
                    "samples_received":   len(samples),
                    "sender_samples_count": snd_summary.get("samples_count"),
                    "sender_pipeline":      snd_summary.get("pipeline"),
                }
                if not useful:
                    per_stream.append({"stream_id": i, "error": "no samples", **diag})
                    continue
                sent      = max((s.get("pktSentTotal") or 0)     for s in useful)
                lost      = max((s.get("pktSndLossTotal") or 0)  for s in useful)
                bytes_max = max((s.get("byteSent") or 0)         for s in useful)
                rates     = [s.get("mbpsSendRate") for s in useful if s.get("mbpsSendRate")]
                rtts      = [s.get("msRTT") for s in useful if s.get("msRTT")]
                # Throughput computation depends on which pipeline produced
                # the samples:
                #   ffmpeg-native:  mbpsSendRate IS the running cumulative
                #                   average bitrate from ffmpeg -stats; the
                #                   last sample is the test's overall mean,
                #                   so averaging (or even using max) is
                #                   meaningful.
                #   slt path:       mbpsSendRate is an instant rate over the
                #                   inter-packet interval (noisy); use the
                #                   integral byteSent/duration instead.
                if bytes_max:
                    true_mbps = round((bytes_max * 8 / 1_000_000) / duration_s, 2) if duration_s else 0.0
                elif rates:
                    true_mbps = round(sum(rates) / len(rates), 2)
                else:
                    true_mbps = 0.0
                # In-stream libsrt RTT (slt path) or fall back to the ICMP
                # measurement we ran in parallel. ffmpeg-native always
                # falls back since it doesn't surface SRT-level RTT.
                if rtts:
                    rtt_max = round(max(rtts), 1)
                    rtt_avg = round(sum(rtts) / len(rtts), 1)
                elif measured_rtt_ms is not None:
                    rtt_max = rtt_avg = round(measured_rtt_ms, 1)
                else:
                    rtt_max = rtt_avg = 0.0
                per_stream.append({
                    "stream_id":    i,
                    "sent_total":   sent,
                    "loss_total":   lost,
                    "byte_total":   bytes_max,
                    "loss_pct":     round((100.0 * lost / sent) if sent else 0.0, 3),
                    "avg_send_mbps": true_mbps,
                    "max_rtt_ms":   rtt_max,
                    "avg_rtt_ms":   rtt_avg,
                    **diag,
                })

            errored = [s for s in per_stream if "error" in s]
            if errored:
                return {
                    "error": f"{len(errored)} of {streams} streams produced no samples",
                    "phase": phase, "streams": streams,
                    "target_per_stream_mbps": per_stream_mbps,
                    "per_stream": per_stream,
                }

            sent_sum   = sum(p["sent_total"]    for p in per_stream)
            loss_sum   = sum(p["loss_total"]    for p in per_stream)
            send_sum   = sum(p["avg_send_mbps"] for p in per_stream)
            worst_rtt  = max(p["max_rtt_ms"]    for p in per_stream)
            target_total = per_stream_mbps * streams
            # Loss: prefer the *real* sender-vs-receiver throughput delta
            # (now that ffmpeg surfaces throughput_mbps on both ends). Fall
            # back to slt's pktSndLossTotal-derived loss if the receiver
            # query failed, and to 0 if neither is available.
            if recv_mbps_total is not None and send_sum > 0:
                loss_pct = max(0.0, (send_sum - recv_mbps_total) / send_sum * 100.0)
            elif sent_sum:
                loss_pct = 100.0 * loss_sum / sent_sum
            else:
                loss_pct = 0.0
            delivered_pct = (100.0 * send_sum / target_total) if target_total else 100.0

            return {
                "phase":                  phase,
                "streams":                streams,
                "target_per_stream_mbps": per_stream_mbps,
                "target_total_mbps":      round(target_total, 2),
                "avg_send_total_mbps":    round(send_sum, 2),
                "avg_send_per_stream_mbps": round(send_sum / streams, 2),
                "recv_total_mbps":        recv_mbps_total,
                "icmp_rtt_ms":            (round(measured_rtt_ms, 1)
                                            if measured_rtt_ms is not None else None),
                "max_rtt_ms":             round(worst_rtt, 1),
                "loss_pct":               round(loss_pct, 3),
                "delivered_pct":          round(delivered_pct, 1),
                "per_stream":             per_stream,
                "duration_s":             duration_s,
            }

        def is_pass(r: dict) -> bool:
            if r.get("error"):                              return False
            if r["loss_pct"]      > cfg["loss_pct_max"]:    return False
            if r["max_rtt_ms"]    > cfg["rtt_ms_max"]:      return False
            if r["delivered_pct"] < cfg["deliver_pct_min"]: return False
            return True

        # ===================================================================
        # PHASE 1 — Probe upward
        # ===================================================================
        rate = cfg["start_mbps"]
        last_pass: Optional[float] = None
        first_fail: Optional[float] = None
        attempts = 0

        emit("probe", "started",
             start_mbps=cfg["start_mbps"],
             ceiling_mbps=cfg["ceiling_mbps"])

        while rate <= cfg["ceiling_mbps"] and attempts < cfg["max_attempts"]:
            if self._stopping:
                break
            emit("probe", "running",
                 target_mbps=round(rate, 2),
                 target_total_mbps=round(rate * streams, 2))
            result = run_subtest(rate, cfg["probe_duration_s"], "probe")
            attempts += 1
            self.summary["attempts"].append(result)
            passed = is_pass(result)
            emit("probe", "pass" if passed else "fail",
                 target_mbps=round(rate, 2),
                 target_total_mbps=round(rate * streams, 2),
                 result=result)
            if not passed:
                first_fail = rate
                break
            last_pass = rate
            rate *= 2
            time.sleep(cfg["settle_s"])

        if self._stopping:
            self.summary["status"] = "stopped"
            self._finalize(emit)
            return

        if first_fail is None:
            self.summary["max_stable_per_stream_mbps"] = last_pass or cfg["start_mbps"]
            self.summary["ceiling_hit"] = True
        else:
            # ===============================================================
            # PHASE 2 — Narrow with binary search
            # ===============================================================
            lo = last_pass if last_pass is not None else 1.0
            hi = first_fail
            emit("narrow", "started",
                 lo_mbps=round(lo, 2), hi_mbps=round(hi, 2))

            while (hi - lo) > cfg["step_mbps"] and attempts < cfg["max_attempts"]:
                if self._stopping:
                    break
                mid = round((lo + hi) / 2, 2)
                emit("narrow", "running",
                     target_mbps=mid,
                     target_total_mbps=round(mid * streams, 2),
                     lo_mbps=round(lo, 2), hi_mbps=round(hi, 2))
                result = run_subtest(mid, cfg["probe_duration_s"], "narrow")
                attempts += 1
                self.summary["attempts"].append(result)
                passed = is_pass(result)
                emit("narrow", "pass" if passed else "fail",
                     target_mbps=mid,
                     target_total_mbps=round(mid * streams, 2),
                     result=result)
                if passed:
                    lo = mid
                else:
                    hi = mid
                time.sleep(cfg["settle_s"])
            self.summary["max_stable_per_stream_mbps"] = round(lo, 2)
            self.summary["ceiling_hit"] = False

        # ===================================================================
        # PHASE 3 — Soak test
        # ===================================================================
        max_per_stream = self.summary.get("max_stable_per_stream_mbps", 0)
        if (not self._stopping and cfg["soak_duration_s"] > 0
                and max_per_stream > 0):
            soak_rate = round(max_per_stream * cfg["soak_ratio"], 2)
            self.summary["soak_rate_per_stream_mbps"] = soak_rate
            self.summary["soak_duration_s"] = cfg["soak_duration_s"]
            emit("soak", "running",
                 target_mbps=soak_rate,
                 target_total_mbps=round(soak_rate * streams, 2),
                 duration_s=cfg["soak_duration_s"])
            result = run_subtest(soak_rate, cfg["soak_duration_s"], "soak")
            self.summary["attempts"].append(result)
            passed = is_pass(result)
            self.summary["soak_passed"] = passed
            self.summary["soak_result"] = result
            emit("soak", "pass" if passed else "fail",
                 target_mbps=soak_rate,
                 target_total_mbps=round(soak_rate * streams, 2),
                 result=result)

        self.summary["status"] = "completed"
        self.summary["attempts_count"] = len(self.summary["attempts"])
        self._finalize(emit)

    def _finalize(self, emit) -> None:
        """Compute totals + recommendations and emit the final 'done' sample."""
        per_stream = self.summary.get("max_stable_per_stream_mbps", 0) or 0
        streams    = self.summary.get("streams", 1)
        total      = round(per_stream * streams, 2)
        self.summary["max_stable_total_mbps"] = total
        if per_stream > 0:
            self.summary["recommendations"] = recommend_quality(per_stream)
        emit("done", "final",
             max_stable_per_stream_mbps=per_stream,
             max_stable_total_mbps=total,
             streams=streams,
             soak_passed=self.summary.get("soak_passed"),
             attempts=self.summary.get("attempts_count", 0),
             recommendations=self.summary.get("recommendations"))
