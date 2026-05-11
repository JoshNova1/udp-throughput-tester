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
        "clips_dir": str(CLIPS_DIR),
        "app": {
            "version": APP_VERSION,
            "repo": GITHUB_REPO,
            "platform": sys.platform,
            # Only Windows uses the Setup.exe / in-app updater. Linux
            # (Pi / Docker / bare-metal) update via git pull / docker
            # pull / apt; macOS desktop builds aren't published yet.
            "updater_supported": sys.platform == "win32",
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


def _latest_release() -> dict:
    """Fetch the latest published release from GitHub. Returns the parsed
    JSON, or raises requests.HTTPError on non-2xx."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


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
        if exc.response is not None and exc.response.status_code == 404:
            return jsonify({
                "status": "no-releases",
                "current": APP_VERSION,
                "message": "No releases published yet.",
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


@app.route("/api/install-update", methods=["POST"])
def api_install_update():
    """Download the latest Setup.exe and launch it silently. The running
    app then exits so Inno can replace its files; the installer's
    post-install [Run] entry relaunches the new exe."""
    if "REPLACE_ME" in GITHUB_REPO:
        return jsonify({"error": "Update repo not configured"}), 400
    try:
        rel = _latest_release()
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not fetch release: {exc}"}), 502

    asset_url = None
    asset_name = None
    for a in rel.get("assets", []):
        name = a.get("name", "")
        if name.lower().endswith(".exe") and "setup" in name.lower():
            asset_url = a.get("browser_download_url")
            asset_name = name
            break
    if not asset_url:
        return jsonify({"error": "No Setup.exe asset in latest release"}), 404

    import tempfile
    tmpdir = Path(tempfile.gettempdir()) / "throughput-tester-update"
    tmpdir.mkdir(exist_ok=True)
    target = tmpdir / asset_name

    try:
        with requests.get(asset_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(target, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as exc:
        return jsonify({"error": f"Download failed: {exc}"}), 502

    # Launch installer detached and exit. The launcher .bat is the cleanest
    # way to (a) wait for the current pid to die so Inno doesn't have to
    # CloseApplications, (b) run the installer silently, (c) relaunch the
    # new exe afterwards (which Inno's skipifsilent would otherwise prevent).
    import subprocess, sys
    pid = os.getpid()
    install_dir = os.environ.get("LOCALAPPDATA", "") + r"\Programs\ThroughputTester"
    bat = tmpdir / "run-update.bat"
    bat.write_text(
        "@echo off\r\n"
        f':: wait for the running app (pid {pid}) to exit\r\n'
        ':wait\r\n'
        f'tasklist /FI "PID eq {pid}" | findstr /R /C:"{pid}" >nul 2>&1\r\n'
        'if not errorlevel 1 ( timeout /t 1 /nobreak >nul & goto wait )\r\n'
        f'start "" /wait "{target}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER\r\n'
        f'start "" "{install_dir}\\UDPThroughputTester.exe"\r\n',
        encoding="ascii",
    )
    # Detach the bat so it survives our exit. On Windows, DETACHED_PROCESS
    # + new console makes it independent.
    DETACHED = 0x00000008
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat)],
        creationflags=DETACHED | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    # Tell the UI we kicked it off, then schedule our own exit.
    def _bye():
        time.sleep(1.0)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({
        "ok": True,
        "downloaded": str(target),
        "size": target.stat().st_size,
        "message": "Update launching — this app will exit and reopen automatically.",
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
