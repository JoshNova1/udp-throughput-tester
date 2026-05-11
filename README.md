# UDP / SRT Throughput Tester

A small app for two Raspberry Pis, Windows PCs, or Linux desktops that
measures end-to-end UDP and SRT throughput between them. Designed to be run
pre-event to confirm what the network is actually capable of carrying —
through the SpeedFusion tunnel, across the bonded Starlinks, or just on the
local LAN.

The same code runs on both nodes (web service on Pi, or installable desktop
app on Windows/Linux). Either node can act as the *sender*; the other
auto-spins-up the matching *receiver* over a small HTTP handshake. Web UI on
both, live charts via WebSocket, optional Prometheus `/metrics` endpoint so
the existing monitoring Pi at `192.168.10.6` can scrape results into Grafana.

## Features at a glance

- **Either-end install**: one binary on each node, picks role (Sender / Receiver / Either) at the top bar. Sender shows configuration + Start; Receiver shows a passive "listening" panel until traffic arrives.
- **Four-view workspace**: Test (live), Results (filterable history + CSV/JSON export), Logs (raw ffmpeg/iperf3/srt-live-transmit output, per-test), Settings (node identity, peer host, defaults, tool checklist).
- **Test modes**: iperf3 UDP, SRT (with detailed libsrt stats), raw ffmpeg UDP, ICMP ping.
- **Custom sources**: synthetic test pattern OR your previously-recorded clip (`-c copy` passthrough for zero-encode tests).
- **Resolution / framerate**: 720p / 1080p / 1440p / 2160p at 25, 30, 50 or 60 fps for the test pattern.
- **Parallel streams**: 1 to 4 concurrent SRT streams to mirror your production architecture (set 2 to match a typical 2-camera start→finish path).
- **Live preview**: JPEG snapshots from both the send and receive pipelines, refreshed in the UI every 1.5 s — visually confirm the stream is decoding cleanly, not just that bytes are moving.
- **History + logs**: every test persists to SQLite plus a per-run log file. Export CSV/JSON of history; download or live-tail logs from the UI.
- **Three deployment shapes**: Docker on a Pi, bare-metal + systemd, or a standalone desktop installer for Windows/Linux.

## UI walkthrough

The interface uses a four-tab workspace driven from a left sidebar:

- **Test** — choose Sender/Receiver/Either at the top, then configure the test (mode, target, source, bitrate). The KPI strip and chart show live stats; preview cards show what's being sent and received during a run.
- **Results** — paginated history table of every test on this node. Filter by mode, role, free-text search. Click "Logs" on any row to jump straight to that test's raw output. Export everything as CSV or JSON.
- **Logs** — dropdown lists every recorded test log. Output is syntax-highlighted (errors red, warnings amber, session markers blue). Live-tails while a test is running if auto-scroll is enabled. Download to disk for offline analysis.
- **Settings** — set this node's name and default role (persists across reboots), the peer's IP/port, and default mode/bitrate/duration. The Environment panel shows which tools are installed and whether hardware H.264 encoding (`h264_v4l2m2m`) is available.

The role switch in the top bar is the central concept: it persists per node, and gates which controls show on the Test view. With two nodes installed and named (e.g. `start-pi` set as Sender, `finish-pi` as Receiver), each one's UI configures itself appropriately.

## Test modes

| Mode | What it measures | Underlying tool |
| --- | --- | --- |
| `iperf3` | Raw UDP throughput, packet loss %, jitter (ms) | `iperf3 -u` |
| `srt` | SRT bandwidth, RTT, retransmits, drops, recovered loss | `srt-live-transmit` + `ffmpeg` test pattern |
| `ffmpeg_udp` | MPEG-TS over plain UDP — the manual approach you've used before | `ffmpeg` test pattern |
| `ping` | ICMP RTT (min / avg / max / mdev) | `ping` |

For the headline "true UDP throughput" number, **iperf3** is the right tool —
it gives you loss and jitter that plain ffmpeg can't. The **SRT** mode is the
one most representative of your production path: it goes through libsrt's
ARQ so you see what the stream would actually deliver after retransmission.

## Quick start (Docker, recommended)

On **both** Pis:

```bash
git clone <repo> /opt/udp-tester && cd /opt/udp-tester
docker compose up -d --build
```

Open `http://<pi-ip>:8080` in a browser on either Pi. Enter the *other* Pi's
IP in the "Peer host" field, save, then pick a mode + bitrate and hit Start.

Host networking is used so UDP, SRT, and ICMP work without NAT translation.

## Quick start — Windows installer

This produces a single `ThroughputTester-Setup-1.0.0.exe` that installs the
app like any other Windows program: Start Menu entry, "Apps & features"
listing with uninstaller, optional desktop shortcut, optional firewall rules.

**The build script bootstraps everything it needs.** On a fresh Windows
box, open a PowerShell window in the repo folder and run:

```
deploy\build-windows.bat
```

That single command does the whole thing end-to-end:

1. **Detects Python** — if missing, installs Python 3.12 via `winget`
   (preferred) or by downloading the official installer from python.org
   and running it silently. User-scope, no admin needed.
2. **Detects Inno Setup** — if missing, installs Inno Setup 6 the same way
   (winget or direct download). Inno Setup needs admin to install, so
   Windows will prompt for UAC the first time.
3. **Creates a Python venv** and installs the project's Python dependencies
   into it.
4. **Downloads ffmpeg + iperf3** Windows builds into `.\bin\` (only the
   first time, or if the folder is empty).
5. **Runs PyInstaller** to bundle Python + Flask + your code into a single
   distributable folder.
6. **Runs Inno Setup** to wrap that folder into
   `dist\ThroughputTester-Setup-1.0.0.exe`.

On subsequent runs, steps 1, 2 and 4 are skipped — only changes get rebuilt
(typically 30–60 seconds for an incremental build).

If a step needs to install something, you'll see clear "==>" status output
telling you what's happening. If you'd rather install prerequisites yourself
and have the script fail rather than auto-install, pass `-NoAutoInstall`:

```
deploy\build-windows.bat -NoAutoInstall
```

Output is one installer file (~120–180 MB depending on what's bundled).
Distribute that single `.exe` — your users just double-click it. **They do
not need Python or anything else installed** — PyInstaller bundles a full
Python runtime inside the executable.

**What the installer does on the target machine:**

- Per-user install by default, no admin needed — files go to
  `%LOCALAPPDATA%\Programs\ThroughputTester\`
- Adds a Start Menu shortcut under "Throughput Tester"
- Registers in "Apps & features" with a proper uninstaller
- Offers (optional) a desktop shortcut
- Offers (optional, admin only) to pre-authorise the firewall rules so the
  user doesn't get a Defender popup on first launch
- Per-user runtime data lives at `%LOCALAPPDATA%\ThroughputTester\`
  (config, history.db, logs/, clips/, previews/) — survives reinstalls,
  removed only on uninstall if the user chooses

**A note on SmartScreen and code signing.** Without an Authenticode
certificate, Windows shows a "Windows protected your PC" SmartScreen warning
the first time someone runs the installer — they have to click "More info"
→ "Run anyway". This is true for *every* unsigned Windows installer. To make
the warning go away you need either a standard Authenticode certificate
(£200–300/year from Sectigo, DigiCert, etc., trust earned over time) or an
EV certificate (immediate reputation, £400+/year, hardware token). For
internal use within your team this is fine to skip — for wider distribution,
budget for a cert.

**srt-live-transmit on Windows.** The libsrt project doesn't publish
Windows binaries directly. The fetch script omits it; without it, SRT mode
falls back to ffmpeg's native SRT support, which gives you basic throughput
but not the detailed per-second RTT/retransmit stats. To get those on
Windows you'd build libsrt from source once and drop the resulting `.exe`
into `.\bin\` before running the build — instructions on the libsrt repo.

## Quick start — Linux desktop app

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-desktop.txt
deploy/build-linux.sh
# -> dist/UDPThroughputTester/UDPThroughputTester
```

The PyInstaller output is a portable folder; for distribution you can
tar.gz it, or wrap it with `linuxdeploy` for an AppImage. ffmpeg/iperf3/
srt-tools should be installed via apt on the target machine.

When launched, the desktop app opens a native window with the same UI as
the web service. It picks a random localhost port, starts Flask, and
connects. Per-user data lives at `~/.local/share/udp-throughput-tester/`
on Linux — that's also where you drop `clips/<name>.mp4` for file-source
tests.

## Quick start (bare metal)

```bash
git clone <repo> ~/udp-tester
cd ~/udp-tester
sudo deploy/install-bare-metal.sh
```

This installs `ffmpeg`, `iperf3`, `srt-tools`, sets up a Python venv, and
registers a systemd unit (`udp-tester.service`). Logs: `journalctl -u udp-tester -f`.

## Topology — what the test is actually measuring

```
   ┌──────────────┐     SpeedFusion tunnel       ┌──────────────┐
   │  Pi @ venue  │ ◄──────────────────────────► │ Pi @ remote  │
   │  (sender)    │   (bonded Starlinks)         │ (receiver)   │
   └──────────────┘                              └──────────────┘
        ▲                                              ▲
        │   HTTP (port 8080)   ─────  start handshake  │
```

The sender Pi's web UI calls `POST /api/peer/listen` on the receiver Pi to
start the matching listener (`iperf3 -s`, `srt-live-transmit -mode=listener`,
or `ffmpeg -f mpegts udp://0.0.0.0:port`). Once that's bound, the sender
launches its client.

For tests through the SpeedFusion tunnel, both Pis sit on the inside of the
Peplink LAN. SpeedFusion's outbound policy must route the test traffic over
the tunnel — if it falls through to a direct WAN, you're measuring
something else.

## Ports

| Port | Proto | Used for |
| --- | --- | --- |
| 8080 | TCP | Web UI + handshake API + WebSocket |
| 5201 | UDP | iperf3 default |
| 9000 | UDP | SRT default |
| 9100 | UDP | ffmpeg UDP default |

All test ports are configurable in the UI. Make sure the SpeedFusion firewall
doesn't drop them between the two Pis.

## Prometheus integration

The monitoring Pi at `192.168.10.6` can scrape `http://<pi>:8080/metrics` from
each test Pi. Gauges:

- `udp_tester_last_throughput_mbps{mode=...}`
- `udp_tester_last_loss_pct{mode=...}`
- `udp_tester_last_jitter_ms{mode=...}`
- `udp_tester_last_rtt_ms{mode=...}`
- `udp_tester_test_active`

These update at the end of each test, so you get a per-event Grafana history
without needing to look at the web UI.

## API (if you want to fire it from Stream Deck / Companion)

```bash
# Kick off an iperf3 test from the sender side
curl -X POST http://<sender>:8080/api/test/start \
  -H 'Content-Type: application/json' \
  -d '{"mode":"iperf3","peer":"192.168.10.7","bitrate_mbps":20,"duration":30}'

# Stop in-progress test
curl -X POST http://<sender>:8080/api/test/stop

# Get history
curl http://<sender>:8080/api/history?limit=10
```

## Custom test sources (use your own clip)

For SRT and ffmpeg UDP modes, you can choose between two sources:

- **Test pattern** (`testsrc2` + sine tone) — re-encoded at the bitrate/resolution/framerate you pick. Good for finding the network ceiling at arbitrary bitrates.
- **Video file** — drop a clip from a previous event into `./clips/` on the host (Docker) or `$CLIPS_DIR` (bare metal). When "use file's native bitrate" is ticked, the file is sent with `-c copy` — **the Pi does no encoding at all**, just remuxes the existing H.264 bitstream into MPEG-TS and pushes it down the wire.

The file-with-`-c copy` mode is the most representative test you can run:

- The exact bytes that hit the wire are production bytes — same encoder profile, same I-frame intervals, same bitrate variation
- The Pi is removed as a variable — even a Pi 3 has plenty of headroom for remuxing
- SRT's ARQ and packet pacing exercise on real video bitrate lumpiness, not a smooth test pattern

If you want to test *above* the clip's native bitrate (e.g. headroom check), untick "native" and the file is re-encoded at your chosen bitrate — but that brings the encoder back into the loop and you'll want Pi 4+ for 1080p30.

### Hardware encoder auto-detection

The app checks for `h264_v4l2m2m` (Pi 4's hardware H.264 encoder via VideoCore VI) by both encoder-list and `/dev/video11` presence. When detected, ffmpeg re-encodes via hardware → near-zero CPU load on the Pi.

- **Pi 3** — no hardware H.264 encoder. Use file mode with `-c copy` for full performance, or test pattern at low bitrates only.
- **Pi 4** — picks `h264_v4l2m2m` automatically. 1080p30 hardware encode is trivial.
- **Pi 5** — VideoCore VII dropped legacy H.264 hardware encode, so the app falls back to `libx264`. The A76 cores handle 1080p30 ultrafast comfortably in software.

## Parallel streams (matching your 2-stream production)

Your production runs **two SRT streams** back over the SpeedFusion tunnel
from start to finish. One stream at 2× the combined bitrate is *not*
equivalent to two streams — SpeedFusion's bonding scheduler treats each flow
(5-tuple: src/dst IP+port+proto) independently, and SRT's ARQ/jitter buffer
state is per-stream. To get a test that faithfully simulates your real path,
set **Parallel streams = 2** in the UI.

Implementation: when you ask for N streams, the sender spawns N concurrent
SRT senders on consecutive ports (9000, 9001, …) and the receiver matches.
Stats are summed for throughput / retrans / drop, and worst-case is taken
for RTT / loss / jitter — so the headline number is the system view, while
the per-stream breakdown is still available in the WebSocket events.

## Live preview

While a test is running, both sides extract one decoded JPEG per second from
the actual stream and serve it at `/api/preview-send` and
`/api/preview-recv`. The UI auto-refreshes them every 1.5 s, so you can:

- Watch the testsrc2 timecode tick on the receiver — confirms frames are arriving and decoding
- See your previous-event clip playing through — confirms the right file is in flight
- Visually catch decode corruption that wouldn't necessarily show as a stat anomaly

The preview pipeline runs at 1 fps / 480p so the additional load on the Pi
is negligible. It runs only on stream 0 when in parallel-streams mode.

## Why not just `iperf3`?

You can absolutely run `iperf3 -s` on one Pi and `iperf3 -c -u -b 20M` on the
other. This app gives you:

- A web UI you can run pre-event without SSH'ing in
- SRT testing alongside raw UDP (so the number matches production)
- RTT measured *during* the throughput test (run a ping in parallel)
- A history of every test, persisted, with Prometheus integration
- One command that fires the receiver, runs the sender, and saves the result

## SRT notes

The SRT runner uses `srt-live-transmit` from `srt-tools` so it can expose
libsrt's per-second statistics (`msRTT`, `pktRetransTotal`, `pktRcvLossTotal`,
`pktRcvDropTotal`). On the sender side, ffmpeg generates a deterministic test
pattern (`testsrc2` + `sine` tone, libx264 ultrafast) at the chosen bitrate
and pipes it via local UDP into `srt-live-transmit` for SRT-ification.

The SRT *latency* field in the UI maps to libsrt's `latency=` parameter. For
matching your production stream, set this to the same value your real SRT
sender uses (commonly 120–500 ms depending on tolerated buffer).

## Files

```
app.py                    # Flask + WebSocket backend
desktop.py                # PyWebView launcher for the standalone app
desktop.spec              # PyInstaller spec
tests/
  _common.py              # Runner base class, subprocess helpers
  _ffmpeg_source.py       # Shared ffmpeg input/encoder builder (testpattern/file)
  iperf3_test.py          # iperf3 sender/receiver runners
  srt_test.py             # SRT via srt-live-transmit (+ receive preview tap)
  ping_test.py            # ICMP RTT
  ffmpeg_udp_test.py      # ffmpeg MPEG-TS over UDP (+ receive preview tap)
templates/index.html      # Web UI
static/app.js
static/style.css
Dockerfile
docker-compose.yml
requirements.txt          # Pi/Docker dependencies
requirements-desktop.txt  # Additional deps for the desktop build
deploy/
  install-bare-metal.sh    # systemd-based install for Pi/Linux service mode
  build-windows.bat        # Windows installer entry-point (calls PowerShell)
  build-installer.ps1      # PowerShell pipeline: venv → PyInstaller → Inno Setup
  fetch-binaries.ps1       # downloads ffmpeg / iperf3 Windows builds into .\bin\
  installer.iss            # Inno Setup script — produces the final Setup.exe
  build-linux.sh           # PyInstaller wrapper for Linux desktop build
```
