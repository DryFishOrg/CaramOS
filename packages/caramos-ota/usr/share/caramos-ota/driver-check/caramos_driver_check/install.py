"""Run the privileged helper through pkexec and stream its events to the window (no blocking calls)."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .events import parse_event

APP_DIR = Path(__file__).resolve().parents[1]
HELPER_PATH = "/usr/share/caramos-ota/driver-check/caramos-driver-check-helper"
PKEXEC = "/usr/bin/pkexec"
# pkexec exit codes when the user dismisses or fails the password dialog.
AUTH_CANCELLED = (126, 127)


def helper_command(targets: list[str]) -> list[str]:
    fake = os.environ.get("CARAMOS_DRIVER_CHECK_FAKE_HELPER")
    if fake:   # development only, see fake_helper.py
        return [sys.executable, "-m", "caramos_driver_check.fake_helper", "install", *targets]
    return [PKEXEC, HELPER_PATH, "install", *targets]


class HelperRun:
    """One helper run. on_event gets each event; on_exit gets (exit code, password dialog cancelled)."""

    def __init__(self, targets: list[str], on_event: Callable[[dict[str, Any]], None],
                 on_exit: Callable[[int, bool], None]) -> None:
        from gi.repository import Gio, GLib

        self._GLib = GLib
        self.on_event = on_event
        self.on_exit = on_exit
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        # The fake helper imports this package from the source tree or /usr/share.
        launcher.setenv("PYTHONPATH", str(APP_DIR), True)
        self.process = launcher.spawnv(helper_command(targets))
        self.reader = Gio.DataInputStream.new(self.process.get_stdout_pipe())
        self._read_next()

    def _read_next(self) -> None:
        self.reader.read_line_async(self._GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _on_line(self, reader, result) -> None:
        try:
            line, _length = reader.read_line_finish_utf8(result)
        except self._GLib.Error:
            line = None
        if line is None:
            self.process.wait_async(None, self._on_wait)
            return
        event = parse_event(line)
        if event is not None:
            self.on_event(event)
        self._read_next()

    def _on_wait(self, process, result) -> None:
        try:
            process.wait_finish(result)
        except self._GLib.Error:
            pass
        code = process.get_exit_status() if process.get_if_exited() else 1
        self.on_exit(code, code in AUTH_CANCELLED)
