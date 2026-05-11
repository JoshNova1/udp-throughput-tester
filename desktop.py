"""Desktop launcher — boots the Flask server on a random localhost port
and opens a native window pointing at it.

Run directly with `python desktop.py`, or build a single-file Windows/Linux
binary with PyInstaller (see build-windows.bat / build-linux.sh).

When frozen by PyInstaller, sys._MEIPASS contains the temp extraction dir
where templates/, static/, and bundled tool binaries live.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path


def _frozen_base() -> Path:
    """Return the directory holding bundled resources.

    - When running normally: this file's directory.
    - When frozen by PyInstaller: the _MEIPASS extraction dir.
    """
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _port_is_free(port: int) -> bool:
    """Try to bind on all interfaces so we know nothing else is listening."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _prepend_to_path(d: Path) -> None:
    """Make bundled binaries (ffmpeg, iperf3, srt-live-transmit) discoverable."""
    if d.exists():
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def main() -> int:
    base = _frozen_base()

    # Bundled binaries live next to the executable in a 'bin' directory.
    # On Pi/Linux installs you'd just use system packages; this is for the
    # standalone desktop build.
    bin_dir = base / "bin"
    _prepend_to_path(bin_dir)

    # Per-user data dir so the installed app doesn't try to write inside
    # Program Files / /opt. Override with DATA_DIR if you want a custom path.
    if "DATA_DIR" not in os.environ:
        if sys.platform == "win32":
            data_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
            data_dir = data_root / "NovaConnect" / "ThroughputTester"
        elif sys.platform == "darwin":
            data_dir = (Path.home() / "Library" / "Application Support"
                        / "Nova Connect" / "Throughput Tester")
        else:
            data_dir = (Path(os.environ.get("XDG_DATA_HOME",
                                            str(Path.home() / ".local/share")))
                        / "nova-connect" / "throughput-tester")
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["DATA_DIR"] = str(data_dir)
        (data_dir / "clips").mkdir(parents=True, exist_ok=True)

    # Pick a port. Priority order:
    #   1. caller-set $env:PORT (loopback testing pins to 8080 this way)
    #   2. 8080 if it's free — matches DEFAULT peer_api_port everywhere so
    #      a second machine on the LAN can hit this host's API by IP alone
    #   3. a random free port (last-resort, e.g. when the user already has
    #      another instance running for loopback)
    env_port = os.environ.get("PORT")
    if env_port:
        port = int(env_port)
    elif _port_is_free(8080):
        port = 8080
    else:
        port = _pick_free_port()
    os.environ["PORT"] = str(port)

    # Ensure cwd is the resources dir so Flask finds templates/, static/.
    os.chdir(str(base))

    # Late-import to make sure env vars are set before app.py reads them.
    import app as flask_app

    # Bind on all interfaces so a peer on the LAN can hit /api/peer/listen
    # for the handshake. The pywebview window still loads via 127.0.0.1.
    # Windows Defender will prompt on first launch -- click "Allow on
    # private networks". (Inno's optional firewall task pre-authorises
    # this when installed with admin privileges; per-user installs get
    # the runtime prompt instead.)
    server_thread = threading.Thread(
        target=lambda: flask_app.app.run(
            host="0.0.0.0", port=port, threaded=True,
            use_reloader=False, debug=False,
        ),
        daemon=True,
    )
    server_thread.start()

    # Wait until the server is responding (max 5s).
    deadline = time.time() + 5
    import urllib.request
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    # Now open the native window.
    import webview  # pywebview
    webview.create_window(
        title="Throughput Tester — NCTech",
        url=f"http://127.0.0.1:{port}",
        width=1240, height=900,
        min_size=(900, 600),
        background_color="#08090a",
    )
    # gui="edgechromium" on Windows, "qt"/"gtk" on Linux. Default auto-pick.
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
