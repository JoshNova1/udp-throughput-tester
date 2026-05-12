"""UDP / SRT Throughput Tester — Flask backend.

Same binary runs on both Pis. Either can be the "sender" (which initiates
the test) — the other becomes the "receiver" via peer coordination.

Endpoints:
   GET  /                  — UI
   GET  /api/status        — current test state, config
   POST /api/config        — save peer host, default settings
   POST /api/test/start    — kick off a sender-side test (coordinates with peer)
   POST /api/test/stop     — stop in-progress test
   GET  /api/history       — past test summaries
   POST /api/peer/listen   — peer-only: start receiver-side tool
   POST /api/peer/stop     — peer-only: stop receiver-side tool
   GET  /metrics           — Prometheus
   WS   /ws                — live samples
"""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock
from prometheus_client import (
    CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest,
)

from tests._common import Runner
from tests._ffmpeg_source import has_v4l2_h264, list_clips, render_preview_jpeg
from tests.iperf3_test import Iperf3Receiver, Iperf3Sender
from tests.ping_test import PingRunner
from tests.srt_test import SrtReceiver, SrtSender
from tests.ffmpeg_udp_test import FfmpegUdpReceiver, FfmpegUdpSender
from tests.auto_test import AutoTestSender

try:
    from _buildinfo import APP_VERSION, GITHUB_REPO
except ImportError:
    APP_VERSION = "0.0.0-dev"
    GITHUB_REPO = "REPLACE_ME/REPLACE_ME"

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIPS_DIR = Path(os.environ.get("CLIPS_DIR", str(DATA_DIR / "clips")))
CLIPS_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR = DATA_DIR / "previews"
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "history.db"
CONFIG_PATH = DATA_DIR / "config.json"
PORT = int(os.environ.get("PORT", 8080))

app = Flask(__name__, static_folder="static", template_folder="templates")
sock = Sock(app)

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "peer_host": os.environ.get("PEER_HOST", ""),
    "peer_api_port": 8080,
    "default_mode": "srt",
    "default_bitrate_mbps": 10,
    "default_duration_s": 30,
    # Operator-set role for this node. "sender" or "receiver" gates the UI;
    # "either" keeps the symmetric behaviour where any node can initiate.
    "node_role": "either",
    "node_name": os.environ.get("NODE_NAME", ""),
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
        except json.JSONDecodeError:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# History (SQLite)
# ---------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tests (
                id          TEXT PRIMARY KEY,
                role        TEXT NOT NULL,
                mode        TEXT NOT NULL,
                started     REAL NOT NULL,
                ended       REAL,
                params      TEXT,
                summary     TEXT
            )"""
        )


init_db()


# ---------------------------------------------------------------------------
# Live test state + WebSocket hub
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self):
        self.subscribers: list[queue.Queue] = []
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast(self, msg: dict) -> None:
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass  # drop slow consumer


HUB = Hub()


class TestSession:
    """Tracks the currently running test on this node.

    Supports multiple parallel runners (one per SRT stream) so that the
    same session represents an N-stream parallel test.
    """
    def __init__(self):
        self.id: Optional[str] = None
        self.role: Optional[str] = None        # 'sender' | 'receiver'
        self.mode: Optional[str] = None
        self.params: dict = {}
        self.runners: list[Runner] = []
        self.started: Optional[float] = None
        self.ended: Optional[float] = None
        self.last_summary: dict = {}
        self.lock = threading.Lock()
        # Side-process for the live preview JPEG (separate ffmpeg). Tracked
        # here so it gets cleaned up alongside the main runners.
        self.preview_proc = None

    def is_active(self) -> bool:
        return any(
            r is not None and r._thread is not None and r._thread.is_alive()
            for r in self.runners
        )

    def stop_all(self):
        for r in self.runners:
            try:
                r.stop()
            except Exception:
                pass
        if self.preview_proc is not None:
            try:
                from tests.preview import stop_preview
                stop_preview(self.preview_proc)
            except Exception:
                pass
            self.preview_proc = None

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "mode": self.mode,
            "params": self.params,
            "started": self.started,
            "ended": self.ended,
            "active": self.is_active(),
            "summary": self.last_summary,
            "streams": len(self.runners),
        }


SESSION = TestSession()


# Prometheus
PROM = CollectorRegistry()
G_LAST_THROUGHPUT = Gauge("udp_tester_last_throughput_mbps", "Last test throughput in Mbps", ["mode"], registry=PROM)
G_LAST_LOSS = Gauge("udp_tester_last_loss_pct", "Last test packet loss %", ["mode"], registry=PROM)
G_LAST_JITTER = Gauge("udp_tester_last_jitter_ms", "Last test jitter (ms)", ["mode"], registry=PROM)
G_LAST_RTT = Gauge("udp_tester_last_rtt_ms", "Last test RTT (ms)", ["mode"], registry=PROM)
G_ACTIVE = Gauge("udp_tester_test_active", "1 if a test is in progress", registry=PROM)


# ---------------------------------------------------------------------------
# Runner factory
# ---------------------------------------------------------------------------

SENDER_RUNNERS = {
    "iperf3": Iperf3Sender,
    "srt": SrtSender,
    "ffmpeg_udp": FfmpegUdpSender,
    "ping": PingRunner,
    "auto": AutoTestSender,
}
RECEIVER_RUNNERS = {
    "iperf3": Iperf3Receiver,
    "srt": SrtReceiver,
    "ffmpeg_udp": FfmpegUdpReceiver,
    # ping needs no receiver
}
DEFAULT_PORTS = {
    "iperf3": 5201,
    "srt": 9000,
    "ffmpeg_udp": 9100,
    # auto-test orchestrates SRT sub-tests internally, so it reuses the SRT
    # port range (9000 + stream_id). Without this entry the body.get default
    # falls through to 0 and ffmpeg rejects "srt://host:0".
    "auto": 9000,
}


def build_runner(role: str, mode: str, stream_id: int = 0) -> Runner:
    if role == "sender":
        cls = SENDER_RUNNERS.get(mode)
    else:
        cls = RECEIVER_RUNNERS.get(mode)
    if not cls:
        raise ValueError(f"no runner for role={role} mode={mode}")
    runner = cls()
    runner.log_path = str(LOG_DIR / f"test_{SESSION.id}.log")

    test_id = SESSION.id

    def on_sample(s):
        msg = {"type": "sample", "id": test_id, "role": role, "mode": mode,
               "stream_id": stream_id, "data": s}
        HUB.broadcast(msg)

    def on_done(summary):
        # The first finishing runner sets last_summary; subsequent ones merge.
        SESSION.last_summary[f"stream_{stream_id}"] = summary
        # All streams done when every runner has populated its slot in
        # last_summary. Don't use SESSION.is_active() here: this callback
        # runs inside the runner's own thread's `finally` block, so that
        # thread is still alive and is_active() always returns True --
        # which means the history INSERT below would never fire.
        all_done = len(SESSION.last_summary) >= len(SESSION.runners) > 0
        if all_done:
            SESSION.ended = time.time()
            # Stop the side-running preview ffmpeg if there is one.
            if SESSION.preview_proc is not None:
                try:
                    from tests.preview import stop_preview
                    stop_preview(SESSION.preview_proc)
                except Exception:
                    pass
                SESSION.preview_proc = None
        # Update Prometheus
        try:
            G_ACTIVE.set(0)
            if "throughput_mbps" in summary and summary["throughput_mbps"] is not None:
                G_LAST_THROUGHPUT.labels(mode=mode).set(summary["throughput_mbps"])
            if "loss_pct" in summary and summary["loss_pct"] is not None:
                G_LAST_LOSS.labels(mode=mode).set(summary["loss_pct"])
            if "jitter_ms" in summary and summary["jitter_ms"] is not None:
                G_LAST_JITTER.labels(mode=mode).set(summary["jitter_ms"])
            if "rtt_avg_ms" in summary and summary["rtt_avg_ms"] is not None:
                G_LAST_RTT.labels(mode=mode).set(summary["rtt_avg_ms"])
        except Exception:
            pass
        # Persist (only once per session — when all streams finish)
        if all_done:
            try:
                with db() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO tests(id, role, mode, started, ended, params, summary)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (test_id, role, mode,
                         SESSION.started,
                         SESSION.ended,
                         json.dumps(SESSION.params),
                         json.dumps(SESSION.last_summary, default=str)),
                    )
            except Exception as exc:
                print(f"[history] write failed: {exc}")
        HUB.broadcast({"type": "done", "id": test_id, "role": role, "mode": mode,
                       "stream_id": stream_id, "summary": summary})

    runner.on_sample = on_sample
    runner.on_done = on_done
    return runner


# ---------------------------------------------------------------------------
# Routes — UI + config
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        cfg = load_config()
        cfg.update({k: v for k, v in request.get_json(force=True).items()
                    if k in DEFAULT_CONFIG})
        save_config(cfg)
        return jsonify(cfg)
    return jsonify(load_config())


@app.route("/api/status")
def api_status():
    return jsonify({
        "session": SESSION.snapshot(),
        "config": load_config(),
        "tools": {
            "ffmpeg": _have("ffmpeg"),
            "iperf3": _have("iperf3"),
            "srt-live-transmit": _have("srt-live-transmit"),
            "ping": _have("ping"),
        },
        "encoders": {
            "h264_v4l2m2m": has_v4l2_h264(),
        },
        "network": _get_local_network(),
        "clips_dir": str(CLIPS_DIR),
        "app": {
            "version": APP_VERSION,
            "repo": GITHUB_REPO,
            "platform": sys.platform,
            # Only Windows uses the Setup.exe / in-app updater. Linux
            # (Pi / Docker / bare-metal) update via git pull / docker
            # pull / apt; macOS desktop builds aren't published yet.
            "updater_supported": sys.platform == "win32",
            # True for the FIRST /api/status call after a successful
            # in-app update (consumed-on-read). UI uses this to show a
            # one-shot "Updated to vX" toast.
            "update_just_completed": _consume_just_updated(),
            "previous_version": _UPDATED_FROM_VERSION,
        },
    })


@app.route("/api/clips")
def api_clips():
    """List video files available as custom test sources."""
    return jsonify({
        "clips_dir": str(CLIPS_DIR),
        "clips": list_clips(str(CLIPS_DIR)),
    })


@app.route("/api/preview")
def api_preview():
    """Render one frame of the chosen test pattern as a JPEG.

    Cached per (resolution, framerate) combo to keep this cheap.
    """
    resolution = request.args.get("resolution", "1280x720")
    framerate = int(request.args.get("framerate", 30))
    # Lightweight cache: name encodes the params
    cache_name = f"testpattern_{resolution}_{framerate}.jpg"
    out_path = PREVIEW_DIR / cache_name
    if not out_path.exists():
        render_preview_jpeg(
            {"resolution": resolution, "framerate": framerate},
            str(out_path),
        )
    if not out_path.exists():
        return jsonify({"error": "preview generation failed"}), 500
    return send_from_directory(str(PREVIEW_DIR), cache_name)


def _have(tool: str) -> bool:
    from shutil import which
    return which(tool) is not None


_NETWORK_CACHE: dict = {"value": None, "ts": 0.0}
_NETWORK_TTL_S = 30.0


def _get_local_network() -> dict:
    """Return the host's primary outbound IPv4 + all non-loopback IPv4s.

    Used by /api/status so the UI can show "Local IP: x.x.x.x" -- saves the
    operator from having to dig through Settings or `ipconfig` to find what
    to type into the sender's "peer host" field on the other machine.

    Result is cached for _NETWORK_TTL_S because /api/status is polled every
    5s and gethostbyname_ex can take double-digit milliseconds on misconfigured
    machines."""
    now = time.time()
    if _NETWORK_CACHE["value"] is not None and (now - _NETWORK_CACHE["ts"]) < _NETWORK_TTL_S:
        return _NETWORK_CACHE["value"]
    import socket
    primary = None
    try:
        # Connect-to-anywhere trick: the socket never actually sends, but the
        # OS picks the interface it WOULD route to 8.8.8.8 through. That's
        # the same interface SRT/UDP test traffic will leave by, so it's the
        # right IP to show as "this machine's address".
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 53))
            primary = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    try:
        _, _, all_ips = socket.gethostbyname_ex(socket.gethostname())
        all_ips = [ip for ip in all_ips if not ip.startswith("127.")]
    except Exception:
        all_ips = []
    if primary and primary not in all_ips:
        all_ips.insert(0, primary)
    result = {"primary": primary, "all": all_ips}
    _NETWORK_CACHE["value"] = result
    _NETWORK_CACHE["ts"] = now
    return result


# ---------------------------------------------------------------------------
# Routes — test control (sender side)
# ---------------------------------------------------------------------------

@app.route("/api/test/start", methods=["POST"])
def api_test_start():
    if SESSION.is_active():
        return jsonify({"error": "test already in progress"}), 409
    body = request.get_json(force=True) or {}
    mode = body.get("mode", "iperf3")
    if mode not in SENDER_RUNNERS:
        return jsonify({"error": f"unknown mode {mode}"}), 400

    cfg = load_config()
    peer = body.get("peer") or cfg.get("peer_host")
    if not peer and mode != "ping":
        return jsonify({"error": "peer host required"}), 400

    duration = int(body.get("duration", cfg.get("default_duration_s", 30)))
    port = int(body.get("port", DEFAULT_PORTS.get(mode, 0)))

    # Source selection for SRT/ffmpeg_udp modes. When source=file with no
    # bitrate override, the runner will pass `-c copy` (no Pi-side encoding).
    source = body.get("source", "testpattern")
    source_file = body.get("source_file") or None
    # If file is just a basename, look it up in CLIPS_DIR.
    if source == "file" and source_file and not os.path.isabs(source_file):
        candidate = CLIPS_DIR / source_file
        if candidate.exists():
            source_file = str(candidate)

    # Bitrate: optional when file mode (None -> -c copy).
    bitrate_in = body.get("bitrate_mbps")
    if bitrate_in in (None, "", "auto", "native"):
        bitrate_mbps = None if source == "file" else float(cfg.get("default_bitrate_mbps", 10))
    else:
        bitrate_mbps = float(bitrate_in)

    streams = int(body.get("streams", 1))
    streams = max(1, min(streams, 4))  # bound 1..4

    base_params = {
        "peer": peer,
        "peer_api_port": int(cfg.get("peer_api_port", 8080)),
        "duration": duration,
        "bitrate_mbps": bitrate_mbps,
        "source": source,
        "source_file": source_file,
        "resolution": body.get("resolution", "1280x720"),
        "framerate": int(body.get("framerate", 30)),
        "video_codec": body.get("video_codec", "auto"),
        "latency_ms": int(body.get("latency_ms", 120)),
        "passphrase": body.get("passphrase", ""),
        "count": int(body.get("count", 20)) if mode == "ping" else None,
        "interval": float(body.get("interval", 0.2)) if mode == "ping" else None,
        "streams": streams,
    }

    # Auto-test mode: pass through tuneables (anything the user didn't
    # provide takes the default from tests/auto_test.py:DEFAULTS).
    # The `streams` field from the body is honoured — the auto-test will
    # run N concurrent SRT senders per sub-test and find the per-stream
    # maximum at which the bundle remains stable.
    if mode == "auto":
        for k in ("start_mbps", "ceiling_mbps", "probe_duration_s",
                  "soak_duration_s", "soak_ratio", "step_mbps",
                  "loss_pct_max", "rtt_ms_max", "deliver_pct_min",
                  "max_attempts"):
            if body.get(k) is not None:
                base_params[k] = body[k]
        # AutoTestSender manages its own per-sub-test peer handshake AND
        # runs N senders internally — so at the orchestration layer we
        # still only spawn ONE runner (the auto runner itself), and the
        # user-chosen stream count is forwarded via base_params.
        streams = 1

    base_params = {k: v for k, v in base_params.items() if v is not None}

    # 1. Tell the peer to start N receivers (unless ping or auto-test —
    #    auto-test internally manages its own peer handshake per sub-run).
    peer_test_id = None
    if mode not in ("ping", "auto"):
        peer_url = f"http://{peer}:{cfg.get('peer_api_port', 8080)}/api/peer/listen"
        try:
            r = requests.post(peer_url,
                              json={"mode": mode, "duration": duration + 5,
                                    "port": port,
                                    "streams": streams,
                                    "latency_ms": base_params["latency_ms"],
                                    "passphrase": base_params.get("passphrase", "")},
                              timeout=5)
            r.raise_for_status()
            peer_test_id = r.json().get("id")
        except requests.RequestException as exc:
            return jsonify({"error": f"could not start peer listener: {exc}"}), 502
        time.sleep(0.8)

    # 2. Start local senders (one per stream)
    test_id = uuid.uuid4().hex[:12]
    SESSION.id = test_id
    SESSION.role = "sender"
    SESSION.mode = mode
    SESSION.params = base_params
    SESSION.started = time.time()
    SESSION.ended = None
    SESSION.last_summary = {}
    SESSION.runners = []

    for i in range(streams):
        stream_port = port + i
        stream_params = {**base_params, "port": stream_port}
        # Inline preview JPEGs are intentionally NOT part of the runner's
        # ffmpeg cmd (mixed outputs poison -stats with size=N/A). The
        # preview now runs as a *separate* low-priority ffmpeg below.
        runner = build_runner("sender", mode, stream_id=i)
        runner.start(stream_params)
        SESSION.runners.append(runner)

    # Sender-side live preview (1 fps JPEG, separate ffmpeg, low CPU)
    if mode in ("srt", "ffmpeg_udp"):
        from tests.preview import start_send_preview, stop_preview
        SESSION.preview_proc = start_send_preview(
            base_params, PREVIEW_DIR / "send_latest.jpg",
        )
    else:
        SESSION.preview_proc = None

    G_ACTIVE.set(1)
    HUB.broadcast({"type": "start", "id": test_id, "role": "sender",
                   "mode": mode, "params": base_params, "streams": streams,
                   "peer_test_id": peer_test_id})
    return jsonify({"id": test_id, "peer_test_id": peer_test_id,
                    "params": base_params, "streams": streams})


@app.route("/api/test/stop", methods=["POST"])
def api_test_stop():
    if not SESSION.is_active():
        return jsonify({"ok": True, "note": "no active test"})
    SESSION.stop_all()
    cfg = load_config()
    if SESSION.params.get("peer"):
        try:
            requests.post(
                f"http://{SESSION.params['peer']}:{cfg.get('peer_api_port', 8080)}/api/peer/stop",
                timeout=3,
            )
        except requests.RequestException:
            pass
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Routes — peer (receiver) side
# ---------------------------------------------------------------------------

@app.route("/api/peer/listen", methods=["POST"])
def api_peer_listen():
    if SESSION.is_active():
        SESSION.stop_all()
        for r in SESSION.runners:
            r.join(timeout=2)
    body = request.get_json(force=True) or {}
    mode = body.get("mode")
    if mode not in RECEIVER_RUNNERS:
        return jsonify({"error": f"unknown receiver mode {mode}"}), 400

    duration = int(body.get("duration", 60))
    port = int(body.get("port", DEFAULT_PORTS.get(mode, 0)))
    latency_ms = int(body.get("latency_ms", 120))
    passphrase = body.get("passphrase", "")
    streams = max(1, min(int(body.get("streams", 1)), 4))

    test_id = uuid.uuid4().hex[:12]
    SESSION.id = test_id
    SESSION.role = "receiver"
    SESSION.mode = mode
    SESSION.params = {"port": port, "duration": duration,
                     "latency_ms": latency_ms, "passphrase": passphrase,
                     "streams": streams}
    SESSION.started = time.time()
    SESSION.ended = None
    SESSION.last_summary = {}
    SESSION.runners = []

    for i in range(streams):
        stream_port = port + i
        rparams = {"port": stream_port, "duration": duration,
                   "latency_ms": latency_ms, "passphrase": passphrase}
        # No inline preview output for the same reason as the sender side
        # (see comment in /api/test/start): the dual-output -update 1 jpeg
        # makes ffmpeg report bitrate=N/A, killing single-stream throughput.
        runner = build_runner("receiver", mode, stream_id=i)
        runner.start(rparams)
        SESSION.runners.append(runner)

    G_ACTIVE.set(1)
    HUB.broadcast({"type": "start", "id": test_id, "role": "receiver",
                   "mode": mode, "params": SESSION.params, "streams": streams})
    return jsonify({"id": test_id, "params": SESSION.params, "streams": streams})


@app.route("/api/peer/stop", methods=["POST"])
def api_peer_stop():
    if SESSION.is_active():
        SESSION.stop_all()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Live preview endpoints — current send/receive frame as JPEG
# ---------------------------------------------------------------------------

@app.route("/api/preview-send")
def api_preview_send():
    p = PREVIEW_DIR / "send_latest.jpg"
    if not p.exists():
        return jsonify({"error": "no send preview yet"}), 404
    return send_from_directory(str(PREVIEW_DIR), "send_latest.jpg")


@app.route("/api/preview-recv")
def api_preview_recv():
    p = PREVIEW_DIR / "recv_latest.jpg"
    if not p.exists():
        return jsonify({"error": "no receive preview yet"}), 404
    return send_from_directory(str(PREVIEW_DIR), "recv_latest.jpg")


# ---------------------------------------------------------------------------
# Routes — logs
# ---------------------------------------------------------------------------

@app.route("/api/logs")
def api_logs():
    """Return raw test log output.

    Params:
        id        Test id to fetch; defaults to current/latest.
        tail      Last N lines only.
        list      If 'list=1', return available log ids.
    """
    if request.args.get("list"):
        items = []
        for p in sorted(LOG_DIR.glob("test_*.log"), reverse=True):
            items.append({
                "id": p.stem.removeprefix("test_"),
                "path": str(p),
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            })
        return jsonify({"logs": items[:50]})

    test_id = request.args.get("id") or SESSION.id
    if not test_id:
        # Fall back to most recent
        candidates = sorted(LOG_DIR.glob("test_*.log"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            return jsonify({"id": None, "lines": []})
        log_path = candidates[0]
        test_id = log_path.stem.removeprefix("test_")
    else:
        log_path = LOG_DIR / f"test_{test_id}.log"

    if not log_path.exists():
        return jsonify({"id": test_id, "lines": []})

    tail = request.args.get("tail")
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    lines = text.splitlines()
    if tail:
        try:
            lines = lines[-int(tail):]
        except ValueError:
            pass
    return jsonify({"id": test_id, "lines": lines, "size": log_path.stat().st_size})


# ---------------------------------------------------------------------------
# Routes — role config helper
# ---------------------------------------------------------------------------

@app.route("/api/role", methods=["GET", "POST"])
def api_role():
    cfg = load_config()
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        role = body.get("role", "either")
        if role not in ("either", "sender", "receiver"):
            return jsonify({"error": "role must be either|sender|receiver"}), 400
        cfg["node_role"] = role
        if "node_name" in body:
            cfg["node_name"] = str(body["node_name"])
        save_config(cfg)
    return jsonify({
        "role": cfg.get("node_role", "either"),
        "node_name": cfg.get("node_name", ""),
    })


# ---------------------------------------------------------------------------
# Routes — history / metrics
# ---------------------------------------------------------------------------

@app.route("/api/history")
def api_history():
    limit = int(request.args.get("limit", 50))
    with db() as conn:
        rows = conn.execute(
            "SELECT id, role, mode, started, ended, params, summary"
            " FROM tests ORDER BY started DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "role": r["role"],
            "mode": r["mode"],
            "started": r["started"],
            "ended": r["ended"],
            "params": json.loads(r["params"]) if r["params"] else {},
            "summary": json.loads(r["summary"]) if r["summary"] else {},
        })
    return jsonify(out)


@app.route("/metrics")
def metrics():
    return generate_latest(PROM), 200, {"Content-Type": CONTENT_TYPE_LATEST}


# ---------------------------------------------------------------------------
# Routes — clip file picker (desktop only, opens a native dialog)
# ---------------------------------------------------------------------------

@app.route("/api/pick-clip", methods=["POST"])
def api_pick_clip():
    """Open a native Open-File dialog via pywebview, copy the picked file
    into CLIPS_DIR, and return its metadata. Only works when the app is
    running under the pywebview desktop launcher (`desktop.py`)."""
    try:
        import webview
    except Exception:
        return jsonify({"error": "file picker only available in desktop mode"}), 400
    if not getattr(webview, "windows", None):
        return jsonify({"error": "no pywebview window context"}), 400

    file_types = (
        "Video files (*.mp4;*.mkv;*.ts;*.mov;*.m2ts;*.mpg;*.mpeg;*.webm)",
        "All files (*.*)",
    )
    try:
        paths = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=file_types,
        )
    except Exception as exc:
        return jsonify({"error": f"picker failed: {exc}"}), 500

    if not paths:
        return jsonify({"path": None, "name": None, "cancelled": True})

    src = Path(paths[0])
    if not src.is_file():
        return jsonify({"error": f"not a file: {src}"}), 400

    # Copy into CLIPS_DIR so subsequent runs find it via list_clips, and
    # so deletes / moves of the source file don't break ongoing tests.
    dst = CLIPS_DIR / src.name
    if not dst.exists() or dst.stat().st_size != src.stat().st_size:
        import shutil
        shutil.copy2(src, dst)
    return jsonify({
        "path": str(dst),
        "name": src.name,
        "size": dst.stat().st_size,
        "cancelled": False,
    })


# ---------------------------------------------------------------------------
# Routes — in-app updater
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> tuple:
    """Crude semver parse: '1.2.3' or 'v1.2.3' -> (1,2,3). Returns () for
    non-numeric (dev / pre-release / weird) so they always compare as older."""
    if not v:
        return ()
    v = v.lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return ()


_RELEASE_CACHE: dict = {"data": None, "ts": 0.0, "error": None, "error_ts": 0.0}
_RELEASE_CACHE_TTL = 300       # cache a 200-ok response for 5 minutes
_RELEASE_ERROR_TTL = 60        # cache a 403/error response for 1 minute
                               # (don't keep hammering when we're rate-limited)


def _latest_release() -> dict:
    """Fetch the latest published release from GitHub, with a 5-minute
    cache to avoid hammering api.github.com's 60-req/hr anonymous quota.
    Errors (incl. 403 rate-limit) are cached briefly and re-raised so
    callers see a consistent failure without firing more requests."""
    now = time.time()
    if _RELEASE_CACHE["data"] and (now - _RELEASE_CACHE["ts"]) < _RELEASE_CACHE_TTL:
        return _RELEASE_CACHE["data"]
    # If we recently failed, replay the same error rather than re-hitting
    # the rate-limited endpoint -- otherwise the silent-check-on-boot +
    # any UI poll multiplies the rate-limit hits.
    if _RELEASE_CACHE["error"] and (now - _RELEASE_CACHE["error_ts"]) < _RELEASE_ERROR_TTL:
        raise _RELEASE_CACHE["error"]
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        _RELEASE_CACHE["data"] = data
        _RELEASE_CACHE["ts"] = now
        _RELEASE_CACHE["error"] = None
        return data
    except requests.RequestException as exc:
        _RELEASE_CACHE["error"] = exc
        _RELEASE_CACHE["error_ts"] = now
        raise


@app.route("/api/check-update")
def api_check_update():
    """Compare the running version against GitHub's latest release tag."""
    if "REPLACE_ME" in GITHUB_REPO:
        return jsonify({
            "status": "not-configured",
            "current": APP_VERSION,
            "message": "Update repo not configured at build time.",
        })
    current = _parse_version(APP_VERSION)
    if not current:
        return jsonify({
            "status": "dev-build",
            "current": APP_VERSION,
            "message": "Running a dev / unversioned build — auto-update disabled.",
        })
    try:
        rel = _latest_release()
    except requests.HTTPError as exc:
        sc = exc.response.status_code if exc.response is not None else 0
        if sc == 404:
            return jsonify({
                "status": "no-releases",
                "current": APP_VERSION,
                "message": "No releases published yet.",
            })
        if sc == 403:
            # GitHub anonymous API limit is 60/hr per IP. Surface that
            # specifically so the UI can render it as informational
            # rather than scary-red.
            return jsonify({
                "status": "rate-limited",
                "current": APP_VERSION,
                "message": "GitHub API rate limit reached. Try again in a few minutes — the app caches errors briefly to avoid making it worse.",
            })
        return jsonify({
            "status": "error",
            "current": APP_VERSION,
            "message": f"GitHub API error: {exc}",
        }), 502
    except requests.RequestException as exc:
        return jsonify({
            "status": "error",
            "current": APP_VERSION,
            "message": f"Network error: {exc}",
        }), 502

    tag = rel.get("tag_name", "")
    latest = _parse_version(tag)
    # Find the Setup.exe asset in the release.
    asset_url = None
    asset_name = None
    asset_size = None
    for a in rel.get("assets", []):
        name = a.get("name", "")
        if name.lower().endswith(".exe") and "setup" in name.lower():
            asset_url = a.get("browser_download_url")
            asset_name = name
            asset_size = a.get("size")
            break

    if not latest or latest <= current:
        return jsonify({
            "status": "up-to-date",
            "current": APP_VERSION,
            "latest": tag,
            "message": f"You're on the latest version ({APP_VERSION}).",
        })

    return jsonify({
        "status": "update-available",
        "current": APP_VERSION,
        "latest": tag,
        "release_url": rel.get("html_url"),
        "release_notes": rel.get("body") or "",
        "asset_url": asset_url,
        "asset_name": asset_name,
        "asset_size": asset_size,
        "published_at": rel.get("published_at"),
        "message": f"Update available: {tag} (you have {APP_VERSION}).",
    })


# Module-level update state. Updated by the worker thread, polled by the UI.
_UPDATE_STATE_LOCK = threading.Lock()
_UPDATE_STATE: dict = {
    "phase": "idle",          # idle | downloading | installing | error | done
    "downloaded": 0,
    "total": 0,
    "message": "",
    "error": None,
}

# "We just got updated" flag — written by the launcher script after a
# successful install, read once at app boot, exposed in /api/status so the
# UI can show a one-time "✓ Updated to vX" toast.
import tempfile as _tempfile
_UPDATE_TMP = Path(_tempfile.gettempdir()) / "throughput-tester-update"
_UPDATE_FLAG = _UPDATE_TMP / "update-complete.flag"
UPDATE_JUST_COMPLETED = False
_UPDATED_FROM_VERSION = None
if _UPDATE_FLAG.exists():
    try:
        _UPDATED_FROM_VERSION = _UPDATE_FLAG.read_text(encoding="ascii", errors="replace").strip()
        UPDATE_JUST_COMPLETED = True
        _UPDATE_FLAG.unlink()
    except Exception:
        pass


def _consume_just_updated() -> bool:
    """Return True once, then False thereafter — so /api/status fires the
    'just-updated' toast only on the first poll after launch."""
    global UPDATE_JUST_COMPLETED
    if UPDATE_JUST_COMPLETED:
        UPDATE_JUST_COMPLETED = False
        return True
    return False


def _set_update_state(**kwargs) -> None:
    with _UPDATE_STATE_LOCK:
        _UPDATE_STATE.update(kwargs)


def _get_update_state() -> dict:
    with _UPDATE_STATE_LOCK:
        return dict(_UPDATE_STATE)


def _do_update_worker(asset_url: str, asset_name: str, asset_size: int) -> None:
    """Worker thread: download installer with progress, launch the
    update-runner PS1 in detached mode, exit the process."""
    _UPDATE_TMP.mkdir(exist_ok=True)
    target = _UPDATE_TMP / asset_name
    log = _UPDATE_TMP / "update.log"
    try:
        with requests.get(asset_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            cl = r.headers.get("Content-Length")
            if cl:
                try: asset_size = int(cl)
                except ValueError: pass
            _set_update_state(phase="downloading", downloaded=0,
                              total=asset_size, message=f"Downloading {asset_name}")
            downloaded = 0
            with open(target, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    _set_update_state(downloaded=downloaded)
    except Exception as exc:
        _set_update_state(phase="error",
                          error=f"Download failed: {exc}",
                          message="Download failed")
        return

    # Build the relauncher in PowerShell -- much more reliable than .bat for
    # waiting on a PID (Win32 OpenProcess + WaitForExit) and for spawning a
    # detached child. Also logs every step to update.log so we can debug.
    import subprocess
    pid = os.getpid()

    # Derive install_dir from where THIS exe is actually running, not a
    # hard-coded guess. Inno can install per-user (%LOCALAPPDATA%\Programs)
    # or per-machine (%ProgramFiles%) and we must follow whichever path the
    # user originally chose -- otherwise Test-Path on the new exe fails and
    # we don't relaunch. Falls back to the per-user default for dev runs.
    exe_path = Path(sys.executable)
    if exe_path.name.lower() == "udpthroughputtester.exe":
        new_exe = exe_path
        install_dir = exe_path.parent
    else:
        install_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ThroughputTester"
        new_exe = install_dir / "UDPThroughputTester.exe"

    # Match the Inno scope (/CURRENTUSER vs /ALLUSERS) to the existing
    # install location. With PrivilegesRequiredOverridesAllowed=dialog the
    # silent installer needs one of these flags -- and forcing /CURRENTUSER
    # against a per-machine install fails silently (no UI -> invisible).
    localappdata = (os.environ.get("LOCALAPPDATA") or "").lower()
    if localappdata and str(install_dir).lower().startswith(localappdata):
        scope_arg = "/CURRENTUSER"
    else:
        scope_arg = "/ALLUSERS"

    runner = _UPDATE_TMP / "run-update.ps1"
    current_version = APP_VERSION
    installer_log = _UPDATE_TMP / "installer.log"
    runner.write_text(f"""
# Auto-generated update runner. Writes progress to update.log.
$ErrorActionPreference = 'Continue'
$log          = '{log}'
$installerLog = '{installer_log}'
$flag         = '{_UPDATE_FLAG}'
$exePath      = '{new_exe}'
$setup        = '{target}'
$oldPid       = {pid}
$oldVer       = '{current_version}'
$scopeArg     = '{scope_arg}'

function Log($msg) {{
  $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
  Add-Content -Path $log -Value "[$stamp] $msg" -Encoding utf8
}}

Log "runner start (waiting for pid $oldPid)"
Log "exePath=$exePath setup=$setup"
$proc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
if ($proc) {{
  try {{ $proc.WaitForExit(60000) | Out-Null }} catch {{ Log "WaitForExit threw: $_" }}
}}
# Wait for other UDPThroughputTester instances to exit gracefully (loopback
# testing leaves a second instance running). After 8s of waiting, force-kill
# any survivors -- otherwise the file lock blocks Inno's file replacement and
# the silent install aborts with exit 5 "DeleteFile failed; code 5".
$deadline = (Get-Date).AddSeconds(8)
while ((Get-Process -Name 'UDPThroughputTester' -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $deadline)) {{
  Start-Sleep -Milliseconds 500
}}
$survivors = Get-Process -Name 'UDPThroughputTester' -ErrorAction SilentlyContinue
if ($survivors) {{
  Log "force-killing $($survivors.Count) lingering UDPThroughputTester process(es)"
  $survivors | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
}}

# Wait for the install-dir exe to become writable before launching Setup.
# On Windows ARM64 emulating x64, XtaCache (the translation cache) holds
# the binary open for several seconds *after* the process exits. Inno's
# 4 internal retries (~4s) aren't enough -- it aborts with exit 5
# "DeleteFile failed; code 5. Access is denied." Probe until we can open
# the file for write ourselves, then we know Setup can replace it.
$deadline = (Get-Date).AddSeconds(60)
$unlocked = $false
while ((Get-Date) -lt $deadline) {{
  if (-not (Test-Path $exePath)) {{ $unlocked = $true; break }}
  try {{
    $fs = [System.IO.File]::Open($exePath, 'Open', 'ReadWrite', 'None')
    $fs.Close()
    $unlocked = $true
    break
  }} catch {{
    Start-Sleep -Milliseconds 500
  }}
}}
Log "exe lock check: unlocked=$unlocked"

Log "launching installer ($scopeArg)"
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $setup
$psi.Arguments = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=`"$installerLog`" $scopeArg"
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError  = $true
$p = [System.Diagnostics.Process]::Start($psi)
$p.WaitForExit()
Log "installer exited $($p.ExitCode)"
if ($p.ExitCode -ne 0) {{
  Log "installer log: $installerLog"
}}

Start-Sleep -Seconds 2

if (Test-Path $exePath) {{
  Log "writing flag + relaunching $exePath"
  Set-Content -Path $flag -Value $oldVer -Encoding ascii
  Start-Process -FilePath $exePath
  Log "relaunch issued"
}} else {{
  Log "ERROR: new exe missing at $exePath"
}}
""", encoding="utf-8")

    _set_update_state(phase="installing",
                      message="Installer launching — app will restart")
    # CREATE_NO_WINDOW: suppress the console window (the parent is a
    # windowless GUI). CREATE_NEW_PROCESS_GROUP: child outlives the parent's
    # os._exit(0) below in its own group.
    #
    # DO NOT add DETACHED_PROCESS here. DETACHED_PROCESS forces the child to
    # have no console, which powershell.exe cannot survive: it silently exits
    # 0 without executing the -File script. This was the actual reason the
    # in-app updater was failing -- not the inherited console handles, not
    # the missing DEVNULL. Popen returned a live pid, PS died immediately
    # without writing a single line to update.log, the installer never ran,
    # the user's app closed and didn't come back. CREATE_NO_WINDOW alone
    # suppresses the window without breaking PS, and DEVNULL std handles
    # belt-and-braces the inherited-handle theory away too.
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", str(runner)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    # Give the runner ~2s to spin up and start waiting on our PID, then
    # exit so it can proceed.
    time.sleep(2.0)
    os._exit(0)


@app.route("/api/update-progress")
def api_update_progress():
    return jsonify(_get_update_state())


@app.route("/api/install-update", methods=["POST"])
def api_install_update():
    """Kick off the update. Returns immediately. Progress is reported via
    /api/update-progress; the app exits ~2s after the installer is
    launched and reopens itself once the new binaries are in place."""
    if "REPLACE_ME" in GITHUB_REPO:
        return jsonify({"error": "Update repo not configured"}), 400

    state = _get_update_state()
    if state["phase"] in ("downloading", "installing"):
        return jsonify({"error": "Update already in progress",
                        "state": state}), 409

    try:
        rel = _latest_release()
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not fetch release: {exc}"}), 502

    asset_url = asset_name = None
    asset_size = 0
    for a in rel.get("assets", []):
        name = a.get("name", "")
        if name.lower().endswith(".exe") and "setup" in name.lower():
            asset_url = a.get("browser_download_url")
            asset_name = name
            asset_size = int(a.get("size") or 0)
            break
    if not asset_url:
        return jsonify({"error": "No Setup.exe asset in latest release"}), 404

    _set_update_state(phase="downloading", downloaded=0,
                      total=asset_size, message="Starting download",
                      error=None)
    threading.Thread(
        target=_do_update_worker,
        args=(asset_url, asset_name, asset_size),
        daemon=True,
    ).start()
    return jsonify({
        "ok": True,
        "message": "Update started — poll /api/update-progress",
        "total": asset_size,
    })


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@sock.route("/ws")
def ws_stream(ws):
    q = HUB.subscribe()
    try:
        # Send snapshot of current session so a freshly opened UI can resume.
        ws.send(json.dumps({"type": "hello", "session": SESSION.snapshot()}))
        while True:
            msg = q.get()
            ws.send(json.dumps(msg, default=str))
    except Exception:
        pass
    finally:
        HUB.unsubscribe(q)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Threaded dev server is fine for the volumes a pre-event check produces.
    print(f"[udp-tester] listening on 0.0.0.0:{PORT} (data={DATA_DIR})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
