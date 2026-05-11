"""Live-preview helpers.

The test runners themselves don't write preview JPEGs anymore (mixing
a `-update 1 jpeg` output with the throughput output poisons ffmpeg's
aggregate -stats line with size=N/A bitrate=N/A, which silently breaks
single-stream throughput parsing on Windows). Instead the API layer
spawns a separate, low-priority ffmpeg process per test that renders
1 JPEG/sec into the previews directory.

Sender preview: generates the same testsrc2 pattern (or reads the same
clip) the main runner is sending, so what you see is what's going down
the wire content-wise. No re-encoding to libx264 -- just decode +
scale + mjpeg, so CPU cost is negligible.

Receiver preview: deliberately skipped. Tapping the live UDP / SRT
stream would either require ffmpeg's tee muxer (which has the same
aggregate-stats poisoning problem we just fixed) or a side UDP
forwarder. Throughput numbers + chart movement on the receiver side
are sufficient evidence that bytes are arriving.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from ._common import popen, kill_tree


def start_send_preview(params: dict, out_path: Union[str, Path]):
    """Spawn ffmpeg generating 1 JPEG/sec to `out_path`. Returns the Popen
    handle or None if we couldn't / shouldn't preview."""
    source = params.get("source", "testpattern")
    out_path = str(out_path)

    if source == "file":
        source_file = params.get("source_file")
        if not source_file or not Path(source_file).is_file():
            return None
        input_args = [
            "-re", "-stream_loop", "-1", "-i", str(source_file),
        ]
    else:
        resolution = params.get("resolution", "1280x720")
        framerate = int(params.get("framerate", 30))
        input_args = [
            "-re", "-f", "lavfi", "-i",
            f"testsrc2=size={resolution}:rate={framerate},format=yuv420p",
        ]

    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-loglevel", "error",
        *input_args,
        "-vf", "fps=1,scale=480:-2",
        "-update", "1", "-q:v", "5",
        out_path,
    ]
    try:
        return popen(cmd)
    except Exception:
        return None


def stop_preview(proc) -> None:
    """Best-effort terminate a preview ffmpeg started above."""
    if proc is not None:
        kill_tree(proc)
