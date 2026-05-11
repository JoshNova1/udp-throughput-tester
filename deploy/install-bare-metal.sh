#!/usr/bin/env bash
# Bare-metal install on a Raspberry Pi (Bookworm / Debian 12).
# Run as a user with sudo. Idempotent -- safe to re-run.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/udp-tester}"
SERVICE_USER="${SERVICE_USER:-$USER}"

echo "==> Installing OS packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    ffmpeg iperf3 srt-tools iputils-ping \
    python3 python3-venv python3-pip

echo "==> Creating $APP_DIR"
sudo mkdir -p "$APP_DIR" "$APP_DIR/data"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "==> Copying app from $SRC_DIR"
cp -r "$SRC_DIR/app.py" "$SRC_DIR/tests" "$SRC_DIR/templates" \
      "$SRC_DIR/static" "$SRC_DIR/requirements.txt" "$APP_DIR/"

echo "==> Python venv + deps"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Installing systemd unit"
sudo tee /etc/systemd/system/udp-tester.service >/dev/null <<UNIT
[Unit]
Description=UDP / SRT Throughput Tester
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=PORT=8080
Environment=DATA_DIR=$APP_DIR/data
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/app.py
Restart=on-failure
RestartSec=3
# Allow ICMP without root
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now udp-tester.service

echo
echo "==> Done. Web UI: http://$(hostname -I | awk '{print $1}'):8080"
echo "    Logs:        sudo journalctl -u udp-tester -f"
