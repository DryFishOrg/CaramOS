"""Entry point of Driver Check.

    caramos-driver-check              open the app: scan, results, one-click driver installs, hardware reports
    caramos-driver-check --autostart  at login: open the app or notify only when there is news (state.py)
    caramos-driver-check --json       print the report, e.g. for a bug report; no window

CARAMOS_DRIVER_CHECK_DEMO=<case> shows made-up hardware (demo.py) and CARAMOS_DRIVER_CHECK_FAKE_HELPER=1
pretends to install, to work on the window without the hardware or root.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from caramos_ota.i18n import _

from . import notify
from . import state as user_state
from .analysis import Report, analyze
from .drivers import find_proposals
from .hardware import SystemReader
from .knowledge import read_libfprint_support

APP_ID = "net.vietnamlinuxfamily.CaramOSDriverCheck"


class Scanner:
    """What the window and the login check read; demo.DemoScanner replaces it for development."""

    def __init__(self) -> None:
        self.fingerprints = read_libfprint_support()

    def snapshot(self):
        return SystemReader().snapshot()

    def proposals(self):
        return find_proposals()

    def analyze(self, snapshot, proposals) -> Report:
        return analyze(snapshot, self.fingerprints, proposals)

    def connected(self) -> bool:
        reader = SystemReader()
        return reader.connected(reader.net_interfaces())


def make_scanner():
    case = os.environ.get("CARAMOS_DRIVER_CHECK_DEMO", "")
    if case:
        from .demo import DemoScanner

        return DemoScanner(case)
    return Scanner()


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_window() -> int:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib

    from .ui.common import load_css
    from .ui.window import MainWindow

    GLib.set_prgname("caramos-driver-check")
    GLib.set_application_name(_("Driver Check"))
    application = Adw.Application(application_id=APP_ID)

    def activate(app) -> None:
        window = app.get_active_window()
        if window is not None:
            window.present()
            return
        load_css()
        MainWindow(app, make_scanner()).present()

    application.connect("activate", activate)
    return application.run([sys.argv[0]])


def autostart() -> int:
    # No UbuntuDrivers here: loading the APT cache at every login would cost a second or two.
    scanner = make_scanner()
    report = scanner.analyze(scanner.snapshot(), ())
    path = user_state.state_path()
    previous = user_state.load(path)
    decision = user_state.decide(report, previous)
    try:
        user_state.save(path, user_state.remember(report, previous))
    except OSError:
        pass
    if decision.action == user_state.QUIET:
        return 0
    if decision.action == user_state.SHOW_WINDOW:
        return open_window()
    title, body = notify.text_for(decision, report)
    return notify.show(title, body, notify.WAIT_SECONDS[decision.action])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caramos-driver-check")
    parser.add_argument("--autostart", action="store_true",
                        help="login mode: open the app or notify only when something changed")
    parser.add_argument("--json", action="store_true", help="print the report as JSON and exit")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.json:
        scanner = make_scanner()
        report = scanner.analyze(scanner.snapshot(), scanner.proposals())
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if not has_display():
        return 0
    if args.autostart:
        return autostart()
    return open_window()
