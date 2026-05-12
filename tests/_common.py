"""Shared helpers for test runners."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, TextIO

_IS_WINDOWS = sys.platform == "win32"


def hidden_subprocess_kwargs() -> dict:
    """Return kwargs that prevent subprocess.run / Popen from popping a
    visible console window on Windows. On non-Windows returns {}.

    Belt-and-braces: passes both CREATE_NO_WINDOW (suppresses the new console
    Windows would otherwise allocate for a GUI parent) AND a STARTUPINFO with
    SW_HIDE (covers the edge case where the child binary opts back into a
    console — e.g. some Cygwin-built iperf3 distributions)."""
    if not _IS_WINDOWS:
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def have(tool: str) -> bool:
    """True if a binary is on PATH."""
    return shutil.which(tool) is not None


def kill_tree(proc: Optional[subprocess.Popen]) -> None:
    """Best-effort kill of a process and its children."""
    if proc is None or proc.poll() is not None:
        return
    if _IS_WINDOWS:
        # We launched with CREATE_NEW_PROCESS_GROUP so the child is the head
        # of its own group. CTRL_BREAK_EVENT propagates to that group --
        # ffmpeg/iperf3 handle it cleanly (flush stats, exit 0).
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        return
    # POSIX: signal the process group we created via os.setsid.
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def popen(cmd: list[str], **kwargs) -> subprocess.Popen:
    """Popen wrapper that puts the child in its own process group so we can
    signal the whole tree later (ffmpeg + any -f tee children, etc.)."""
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.STDOUT)
    kwargs.setdefault("text", True)
    kwargs.setdefault("bufsize", 1)
    if _IS_WINDOWS:
        # CREATE_NEW_PROCESS_GROUP: makes the child the head of its own group
        # so kill_tree can send CTRL_BREAK_EVENT cleanly.
        # CREATE_NO_WINDOW + SW_HIDE STARTUPINFO: defence-in-depth to suppress
        # the console window that would otherwise pop up for each ffmpeg /
        # iperf3 child. CREATE_NO_WINDOW alone is *usually* sufficient when
        # the parent is a windowless GUI, but some Cygwin-based binaries
        # (iperf3 distros included) still allocate a console unless we also
        # pass a STARTUPINFO requesting SW_HIDE. This combo is what made the
        # difference on a colleague's machine where 3 stray cmd windows
        # appeared on install.
        kwargs.setdefault(
            "creationflags",
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
        if "startupinfo" not in kwargs:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = si
    else:
        kwargs.setdefault("preexec_fn", os.setsid)
    return subprocess.Popen(cmd, **kwargs)


@dataclass
class Runner:
    """Base class for a test runner. Subclasses override _run().

    Each runner can write its raw subprocess output to a per-test log file
    by setting `log_path` before calling start(). Lines are tee'd: still
    parsed for samples, also persisted for after-the-fact inspection in
    the UI's Logs view.
    """
    on_sample: Callable[[dict], None] = lambda s: None
    on_done: Callable[[dict], None] = lambda s: None
    log_path: Optional[str] = None
    _proc: Optional[subprocess.Popen] = None
    _thread: Optional[threading.Thread] = None
    _stopping: bool = False
    _log_file: Optional[TextIO] = None
    _log_lock: threading.Lock = field(default_factory=threading.Lock)
    summary: dict = field(default_factory=dict)

    def start(self, params: dict) -> None:
        self._stopping = False
        self._thread = threading.Thread(target=self._safe_run, args=(params,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        kill_tree(self._proc)

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def log(self, line: str, *, tag: str = "") -> None:
        """Append a line to the runner's log file (thread-safe, best-effort)."""
        if not self._log_file:
            return
        try:
            with self._log_lock:
                if tag:
                    self._log_file.write(f"[{tag}] {line.rstrip()}\n")
                else:
                    self._log_file.write(line.rstrip() + "\n")
                self._log_file.flush()
        except Exception:
            pass

    def _safe_run(self, params: dict) -> None:
        # Open log file if a path was assigned.
        if self.log_path:
            try:
                os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
                self._log_file = open(self.log_path, "a", encoding="utf-8")
                self._log_file.write(
                    f"\n===== test start  {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                    f"params={params}  =====\n"
                )
                self._log_file.flush()
            except Exception as exc:  # noqa: BLE001
                print(f"[runner] could not open log {self.log_path}: {exc}")
                self._log_file = None
        try:
            self._run(params)
        except Exception as exc:  # noqa: BLE001 — surface any runner failure
            self.summary.setdefault("error", str(exc))
            self.log(f"ERROR: {exc}", tag="exc")
        finally:
            self.summary.setdefault("ended", time.time())
            if self._log_file:
                try:
                    self._log_file.write(
                        f"===== test end    {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                        f"summary_keys={list(self.summary)}  =====\n"
                    )
                    self._log_file.close()
                except Exception:
                    pass
                self._log_file = None
            self.on_done(self.summary)

    # Override:
    def _run(self, params: dict) -> None:
        raise NotImplementedError
