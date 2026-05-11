#!/usr/bin/env bash
# Build a standalone Linux desktop binary from this repo.
#
# Prerequisites (one-time):
#   sudo apt install python3-venv python3-pip ffmpeg iperf3 srt-tools \
#                    libgtk-3-dev libwebkit2gtk-4.1-dev   # for pywebview/GTK
#   python3 -m venv .venv && source .venv/bin/activate
#   pip install -r requirements.txt pywebview[gtk] pyinstaller
#
# Output: dist/UDPThroughputTester/UDPThroughputTester
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

if [[ ! -d bin ]]; then
    echo "NOTE: ./bin/ directory not found -- ffmpeg/iperf3/srt-live-transmit will be looked up on PATH at runtime."
    echo "      To bundle them, drop the binaries into ./bin/ before running this script."
fi

echo "=== Cleaning previous build ==="
rm -rf build dist

echo "=== Running PyInstaller ==="
pyinstaller --clean --noconfirm desktop.spec

echo
echo "=== Build complete ==="
echo " -> dist/UDPThroughputTester/UDPThroughputTester"
echo
echo "To produce a portable AppImage, run linuxdeploy or appimage-builder against the dist/ folder."
echo "For Debian users: copy dist/UDPThroughputTester to /opt and add a .desktop file."
