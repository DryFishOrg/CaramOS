"""The requests sent from this computer (helpdesk.sent_requests), with what VNLF says about them.

The panel applet lists the last five and leaves out the ones VNLF finished long ago; this page lists all the
file keeps. A click opens the request's page on VNLF, where VNLF answers. Opening the page asks VNLF how the
requests are doing (helpdesk.refresh), like the applet does now and then.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from gi.repository import Adw, GLib, Gtk

from caramos_ota.i18n import _, ngettext

from .. import actions, helpdesk
from .common import (
    badge,
    bottom_bar,
    clamp,
    header,
    icon,
    icon_bubble,
    label,
    link_button,
    page_box,
    pill,
    scrolled,
)

KIND_ICONS = {
    "driver": "help-browser-symbolic",
    "os": "help-browser-symbolic",
    "bug": "caramos-bug-symbolic",
    "feedback": "chat-symbolic",
}


def kind_name(kind: str) -> str:
    """As the panel menu names them (kindName in the applet)."""

    return {"driver": _("Driver help"), "bug": _("Bug report"), "feedback": _("Feedback")}.get(kind, _("Help"))


def status_text(item: dict[str, Any]) -> tuple[str, str]:
    """What VNLF says about a request, in the panel menu's words, and the badge's class; nothing while it is
    open and not answered."""

    status = item.get("status")
    if status == helpdesk.GONE:
        return _("No longer on VNLF"), "badge-absent"
    if item.get("replied") is True:
        return _("VNLF answered"), "badge-ok"
    return {
        helpdesk.IN_REVIEW: (_("VNLF is looking at it"), "badge-info"),
        helpdesk.RESOLVED: (_("Resolved"), "badge-ok"),
        helpdesk.CLOSED: (_("Closed"), "badge-absent"),
    }.get(status, ("", ""))


def sent_text(value: Any) -> str:
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return ""
    return f"{when.day}/{when.month}/{when.year} {when:%H:%M}"


def _page(content: Gtk.Widget, bar: Gtk.Widget) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    view = Adw.ToolbarView()
    view.add_top_bar(header(_("Your requests")))
    page = scrolled(clamp(content, 860))
    page.add_css_class("welcome")
    view.set_content(page)
    view.set_vexpand(True)
    box.append(view)
    box.append(bar)
    return box


def _row(item: dict[str, Any]) -> Gtk.ListBoxRow:
    box = Gtk.Box(spacing=14)
    box.set_margin_top(12)
    box.set_margin_bottom(12)
    box.set_margin_start(14)
    box.set_margin_end(14)
    kind = icon(KIND_ICONS.get(item.get("type", ""), "help-browser-symbolic"), 20)
    kind.set_valign(Gtk.Align.START)
    box.append(kind)
    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
    name = " · ".join(part for part in (str(item.get("id") or ""), kind_name(item.get("type", "")),
                                        sent_text(item.get("sent"))) if part)
    texts.append(label(name, "heading"))
    if item.get("message"):
        texts.append(label(str(item["message"]), "dim-label", lines=2))
    notes = Gtk.Box(spacing=10)
    text, css_class = status_text(item)
    if text:
        notes.append(badge(text, css_class))
    # As in the panel menu: the community's answers take the place of "shared" (they are on the public page).
    answers = item.get("answers") if isinstance(item.get("answers"), int) else 0
    if answers > 0 or item.get("public") is True:
        community = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        community.append(icon("system-users-symbolic", 14))
        if answers > 0:
            community.append(label(ngettext("%d answer from the community", "%d answers from the community",
                                            answers) % answers, wrap=False))
        else:
            community.add_css_class("dim-label")
            community.append(label(_("Shared with the community"), wrap=False))
        notes.append(community)
    if item.get("public_url"):
        notes.append(link_button(_("Community page"), str(item["public_url"])))
    if notes.get_first_child() is not None:
        notes.set_margin_top(2)
        texts.append(notes)
    box.append(texts)
    go = icon("web-browser-symbolic", 16, "dim-label")
    go.set_valign(Gtk.Align.CENTER)
    box.append(go)
    row = Gtk.ListBoxRow(child=box, activatable=True)
    row.set_tooltip_text(_("Open the request's page"))
    if item.get("status") in helpdesk.FINISHED:
        row.add_css_class("request-finished")
    return row


class RequestsPage(Gtk.Box):
    """Shows the kept list at once, then what VNLF says now; on_back leads to the form."""

    def __init__(self, *, on_back: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.items: list[dict[str, Any]] = []
        self.content = page_box(18)
        bar = bottom_bar("", "", [pill(_("Write a request"), on_back, suggested=True)])
        totals = bar.get_first_child()
        self.total = totals.get_first_child()
        self.status = label("", "dim-label", "bar-detail")
        totals.append(self.status)
        self.append(_page(self.content, bar))
        self._show(helpdesk.sent_requests())

    def _show(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        while (child := self.content.get_first_child()) is not None:
            self.content.remove(child)
        count = len(items)
        self.total.set_label(ngettext("%d request", "%d requests", count) % count)
        if not items:
            self.content.append(icon_bubble("mail-send-symbolic", 40, "status-bubble", "status-ok"))
            self.content.append(label(_("You have not sent any requests yet"), "hero-title", xalign=0.5))
            self.content.append(label(_("Requests you send from Feedback & Help or Driver Check show here."),
                                      "hero-subtitle", xalign=0.5))
            return
        self.content.append(label(_("This computer keeps up to %d of your latest requests. Click one to open its "
                                    "page, where VNLF answers.") % helpdesk.HISTORY_LIMIT, "dim-label"))
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows.add_css_class("boxed-list")
        for item in items:
            rows.append(_row(item))
        rows.connect("row-activated", lambda _list, row: actions.open_url(str(self.items[row.get_index()]["url"])))
        self.content.append(rows)

    def refresh(self) -> None:
        self.status.set_label(_("Asking VNLF how they are doing…"))
        threading.Thread(target=lambda: GLib.idle_add(self._refreshed, helpdesk.refresh()), daemon=True).start()

    def _refreshed(self, result: helpdesk.Refresh) -> bool:
        self._show(helpdesk.sent_requests())
        if result.checked and result.failed == result.checked:
            self.status.set_label(_("Could not reach VNLF: this is what it said last time."))
        elif result.checked:
            self.status.set_label(_("Updated at %s") % datetime.now().strftime("%H:%M"))
        else:
            self.status.set_label("")
        return False
