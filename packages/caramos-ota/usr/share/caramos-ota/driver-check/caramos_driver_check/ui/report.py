"""hw-probe on its own: offline, the hardware profile is saved to send from another device."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from gi.repository import Adw, Gdk, GLib, Gtk

from caramos_ota.i18n import _

from .. import actions, hwprobe
from .common import (
    append_log,
    bottom_bar,
    clamp,
    header,
    icon_bubble,
    label,
    log_view,
    page_box,
    pill,
    scrolled,
)


def _page(content: Gtk.Widget, bar: Gtk.Widget | None) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    view = Adw.ToolbarView()
    view.add_top_bar(header(_("Hardware report")))
    page = scrolled(clamp(content, 820))
    page.add_css_class("welcome")
    view.set_content(page)
    view.set_vexpand(True)
    box.append(view)
    if bar is not None:
        box.append(bar)
    return box


class ProbeRun:
    """Runs hw-probe in a thread and builds the progress and result pages."""

    def __init__(self, upload: bool, *, on_finished: Callable[[hwprobe.ProbeResult], None]) -> None:
        self.upload = upload
        self.on_finished = on_finished
        content = page_box(12)
        content.append(label(_("Collecting hardware information…"), "step-title"))
        content.append(label(_("This takes 1–2 minutes. Please wait until it finishes."), "dim-label"))
        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("overall")
        self.progress.set_margin_top(14)
        self.progress.set_pulse_step(0.04)
        content.append(self.progress)
        details, self.log = log_view()
        content.append(details)
        self.page = _page(content, None)
        self.running = False

    def start(self) -> None:
        self.running = True
        pulse = GLib.timeout_add(120, lambda: (self.progress.pulse(), True)[1])

        def on_line(line: str) -> None:
            GLib.idle_add(lambda: (append_log(self.log, line), False)[1])

        def work() -> None:
            result = hwprobe.upload(on_line) if self.upload else hwprobe.save(on_line)
            GLib.idle_add(done, result)

        def done(result: hwprobe.ProbeResult) -> bool:
            GLib.source_remove(pulse)
            self.running = False
            self.on_finished(result)
            return False

        threading.Thread(target=work, daemon=True).start()


def result_page(result: hwprobe.ProbeResult, *, on_back: Callable[[], None], on_retry: Callable[[], None]) -> Gtk.Widget:
    content = page_box(18)
    if result.ok and result.url:
        content.append(icon_bubble("emblem-ok-symbolic", 40, "status-bubble", "status-ok"))
        content.append(label(_("Your hardware report is ready"), "hero-title", xalign=0.5))
        content.append(label(_("Paste this link into your question on vietnamlinuxfamily.net, the VNLF Facebook "
                               "group, Telegram or Discord."), "hero-subtitle", xalign=0.5))
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("issue-card")
        card.append(label(result.url, "probe-url", selectable=True, xalign=0.5))
        buttons = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        copy = pill(_("Copy link"), lambda: _copy(copy, result.url), suggested=True, icon_name="edit-copy-symbolic")
        buttons.append(copy)
        buttons.append(pill(_("Open the link"), lambda: actions.open_url(result.url)))
        buttons.append(pill(_("Open VNLF"), lambda: actions.open_url(hwprobe.VNLF_URL)))
        card.append(buttons)
        content.append(card)
        bar = bottom_bar(_("Report uploaded"), "", [pill(_("Back to Driver Check"), on_back, suggested=True)])
    elif result.ok and result.saved:
        content.append(icon_bubble("document-save-symbolic", 40, "status-bubble", "status-ok"))
        content.append(label(_("Report saved"), "hero-title", xalign=0.5))
        content.append(label(_("Copy the file to a device with internet, upload it on linux-hardware.org and share "
                               "the link with VNLF. Or ask for help again once this computer is online: the "
                               "profile is then made and attached for you."), "hero-subtitle", xalign=0.5))
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("issue-card")
        card.append(label(result.saved, "status-value", selectable=True, xalign=0.5))
        folder = str(Path(result.saved).parent)
        buttons = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        buttons.append(pill(_("Open folder"), lambda: actions.open_path(folder), icon_name="folder-open-symbolic"))
        card.append(buttons)
        content.append(card)
        bar = bottom_bar(_("Report saved"), "", [pill(_("Back to Driver Check"), on_back, suggested=True)])
    else:
        content.append(icon_bubble("dialog-warning-symbolic", 40, "status-bubble", "status-warning"))
        content.append(label(_("The hardware report did not finish"), "hero-title", xalign=0.5))
        content.append(label(_("hw-probe stopped before it was done, or the password was not entered. Check the "
                               "internet connection and try again."), "hero-subtitle", xalign=0.5))
        details, _view = log_view(result.output)
        details.set_expanded(True)
        content.append(details)
        bar = bottom_bar(_("Not finished"), "", [pill(_("Back"), on_back), pill(_("Try again"), on_retry,
                                                                                  suggested=True)])
    return _page(content, bar)


def _copy(button: Gtk.Button, text: str) -> None:
    clipboard = Gdk.Display.get_default().get_clipboard()
    clipboard.set(text)
    child = button.get_child()
    if isinstance(child, Gtk.Box):
        last = child.get_last_child()
        if isinstance(last, Gtk.Label):
            last.set_label(_("Copied"))
