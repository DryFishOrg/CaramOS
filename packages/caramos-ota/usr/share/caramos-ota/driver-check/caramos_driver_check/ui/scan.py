"""The page shown while Driver Check looks at the hardware: one row per area, filled in one by one.

Reading the hardware takes a fraction of a second, so the rows are revealed at a steady pace to show what
was checked; asking Ubuntu for recommended drivers is the slow part (it loads the APT cache) and the last
row waits for it for real.
"""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import Adw, GLib, Gtk

from caramos_ota.i18n import _, ngettext

from ..analysis import ABSENT, Report, area_label
from .common import AREA_ICONS, LEVEL_ICONS, clamp, header, icon, label, logo, page_box, scrolled

SCAN_AREAS = ("wifi", "ethernet", "graphics", "audio", "bluetooth", "camera", "fingerprint")
STEP_MS = 170


class _Row:
    def __init__(self, title: str, icon_name: str) -> None:
        self.row = Adw.ActionRow(title=title, subtitle=_("Waiting…"))
        self.row.add_css_class("scan-row")
        self.row.add_prefix(icon(icon_name, 20, "scan-area-icon"))
        self.spinner = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.status = Gtk.Image(pixel_size=20, visible=False, valign=Gtk.Align.CENTER)
        self.row.add_suffix(self.spinner)
        self.row.add_suffix(self.status)

    def busy(self, text: str) -> None:
        self.row.set_subtitle(text)
        self.spinner.set_visible(True)
        self.spinner.start()

    def finish(self, text: str, level: str) -> None:
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.row.set_subtitle(text)
        self.status.set_from_icon_name(LEVEL_ICONS.get(level, "emblem-ok-symbolic"))
        for css_class in ("level-ok", "level-tip", "level-info", "level-warning", "level-critical", "level-absent"):
            self.status.remove_css_class(css_class)
        self.status.add_css_class(f"level-{level}")
        self.status.set_visible(True)


class ScanView(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        view = Adw.ToolbarView()
        view.add_top_bar(header(_("Driver Check")))

        content = page_box(22)
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER)
        top.append(logo(88))
        top.append(label(_("Checking your hardware…"), "hero-title", xalign=0.5))
        top.append(label(_("CaramOS looks at every device to see whether it has a working driver."),
                         "dim-label", xalign=0.5))
        content.append(top)

        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        self.rows = {key: _Row(area_label(key), AREA_ICONS[key]) for key in SCAN_AREAS}
        self.rows["drivers"] = _Row(_("Recommended drivers"), AREA_ICONS["drivers"])
        for row in self.rows.values():
            rows.append(row.row)
        content.append(rows)

        page = scrolled(clamp(content, 640))
        page.add_css_class("welcome")
        view.set_content(page)
        view.set_vexpand(True)
        self.append(view)

        self.queue: list[str] = []
        self.revealed = False
        self.drivers_done = False
        self.on_finished: Callable[[], None] | None = None
        self.rows[SCAN_AREAS[0]].busy(_("Checking…"))

    def reveal(self, report: Report, on_finished: Callable[[], None]) -> None:
        """Fill the area rows from the report, one every STEP_MS; then wait for the drivers row."""

        self.on_finished = on_finished
        areas = {area.key: area for area in report.areas}
        self.queue = list(SCAN_AREAS)

        def step() -> bool:
            key = self.queue.pop(0)
            area = areas.get(key)
            if area is None or area.level == ABSENT:
                self.rows[key].finish(_("Not found on this computer"), ABSENT)
            else:
                self.rows[key].finish(area.summary, area.level)
            if self.queue:
                self.rows[self.queue[0]].busy(_("Checking…"))
                return True
            self.revealed = True
            if not self.drivers_done:
                self.rows["drivers"].busy(_("Looking for drivers Ubuntu recommends…"))
            self._maybe_finish()
            return False

        GLib.timeout_add(STEP_MS, step)

    def drivers_found(self, count: int) -> None:
        self.drivers_done = True
        if count:
            text = ngettext("%d driver to install", "%d drivers to install", count) % count
            self.rows["drivers"].finish(text, "warning")
        else:
            self.rows["drivers"].finish(_("No extra driver needed"), "ok")
        self._maybe_finish()

    def _maybe_finish(self) -> None:
        if self.revealed and self.drivers_done and self.on_finished is not None:
            callback, self.on_finished = self.on_finished, None
            GLib.timeout_add(600, lambda: (callback(), False)[1])
