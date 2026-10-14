"""The Feedback & Help window: the help pages (HelpPages) on their own, for help, bug reports and feedback."""

from __future__ import annotations

import threading

from gi.repository import Adw, GLib, Gtk

from caramos_ota.i18n import _

from ..analysis import Report
from .help import HelpPages


class HelpWindow(HelpPages, Adw.ApplicationWindow):
    help_choices = ("os", "bug", "feedback")
    help_app = "caramos-help"

    def __init__(self, application: Adw.Application, scanner, kind: str) -> None:
        super().__init__(application=application, title=_("Feedback & Help"), default_width=920, default_height=800)
        self.set_size_request(360, 480)
        self.scanner = scanner
        self.report: Report | None = None   # None when the scan failed: requests go without the results
        self.computer_read = False
        self.busy = False            # hw-probe or the request is running
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=220,
                               vexpand=True)
        self.toasts = Adw.ToastOverlay(child=self.stack)
        self.set_content(self.toasts)
        self.connect("close-request", self._on_close_request)
        self.show_help(kind)
        threading.Thread(target=self._read_computer, daemon=True).start()

    def _set_page(self, name: str, widget: Gtk.Widget) -> None:
        old = self.stack.get_child_by_name(name)
        if old is not None:
            self.stack.remove(old)
        self.stack.add_named(widget, name)
        self.stack.set_visible_child_name(name)

    def _read_computer(self) -> None:
        # What every request says about the computer (kernel, live USB, virtual machine) and the results
        # that can be attached. No driver proposals: they load the APT cache and only Driver Check needs them.
        try:
            report = self.scanner.analyze(self.scanner.snapshot(), ())
        except Exception:   # noqa: BLE001 - a scan bug must not keep the user from reporting it
            report = None
        GLib.idle_add(self._computer_read, report)

    def _computer_read(self, report: Report | None) -> bool:
        self.report = report
        self.computer_read = True
        if self.help_form is not None:
            self.help_form.set_report(report)
        return False

    def select(self, kind: str) -> None:
        """Started again from the panel or the menu: the open form switches to the kind asked for."""

        if self.busy:
            return
        if self.help_form is None:
            self.show_help(kind)
            return
        self.stack.set_visible_child_name("help")
        self.help_form.select(kind)

    # HelpPages ---------------------------------------------------------------------------------

    def help_title(self) -> str:
        return _("Feedback & Help")

    def help_back_label(self) -> str:
        return _("Close")

    def help_done_label(self) -> str:
        return _("Close")

    def help_report(self) -> Report | None:
        return self.report

    def help_report_ready(self) -> bool:
        return self.computer_read

    def help_online(self) -> bool:
        return self.scanner.connected()

    def leave_help(self) -> None:
        self.close()

    def _on_close_request(self, _window) -> bool:
        if self.busy:
            self.toasts.add_toast(Adw.Toast(title=_("Please wait until it finishes.")))
            return True
        self.help_cleanup()
        return False
