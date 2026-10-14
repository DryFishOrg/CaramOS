"""The pictures and videos of a request: a screenshot, files the user picks or drops, screen recordings.

Every file is prepared (media.py: made smaller, metadata removed) as soon as it is added, so the user sees
that it is fine while still writing; the form waits for it before sending. A screen recording made with
Cinnamon's Ctrl+Shift+Alt+R while the form is open is attached by itself once it stops.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from gi.repository import Gdk, Gio, GLib, Gtk

from caramos_ota.i18n import _

from .. import media
from .common import label, pill

# A recording begun shortly before the form opened was meant for it too.
RECORDING_GRACE = timedelta(minutes=2)
STUCK_SECONDS = 30   # a recording that neither grows nor ends is no longer followed


def reason_text(error: media.MediaError) -> str:
    if error.reason == "too-long":
        if error.duration:
            return _("the video lasts %s; videos can be up to 1 minute") % media.clock(error.duration)
        return _("the video is longer than 1 minute")
    if error.reason == "too-big":
        return _("the file is too big, even made smaller")
    if error.reason == "no-ffmpeg":
        return _("videos need ffmpeg (sudo apt install ffmpeg)")
    if error.reason == "unsupported":
        return _("only pictures and videos can be attached")
    return _("the file could not be read")


class _Item:
    """A file of the form, while it is prepared and once it is ready (attachment)."""

    def __init__(self, source: Path, name: str, temporary: bool) -> None:
        self.source = source
        self.name = name
        self.temporary = temporary   # a screenshot: removed once prepared
        self.kind = media.detect(source)
        self.attachment: media.Attachment | None = None
        self.cancel = threading.Event()
        self.tile: Gtk.FlowBoxChild | None = None
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, hexpand=True, vexpand=True)
        self.spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.spinner.set_size_request(24, 24)
        self.badge = label("", "attachment-badge", wrap=False)
        self.caption = label(_("Preparing…"), "dim-label", "attachment-caption", wrap=False, lines=1)


class AttachmentsBox(Gtk.Box):
    """Pictures and videos in the form; on_changed() tells the form when the list or its state changed."""

    def __init__(self, on_changed: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.on_changed = on_changed
        self.items: list[_Item] = []
        self.folder: Path | None = None
        self.closed = False
        self.recordings: dict[Path, threading.Event] = {}
        self.monitor: Gio.FileMonitor | None = None
        self.opened = datetime.now().astimezone()

        title = label(_("Pictures or videos (optional)"), "card-title")
        title.set_margin_top(8)
        self.append(title)
        buttons = Gtk.Box(spacing=10)
        self.shot_button = pill(_("Take a screenshot"), self._screenshot, icon_name="camera-photo-symbolic")
        self.shot_button.set_visible(bool(media.screenshot_tool()))
        buttons.append(self.shot_button)
        self.choose_button = pill(_("Choose files…"), self._choose, icon_name="mail-attachment-symbolic")
        buttons.append(self.choose_button)
        self.append(buttons)
        if media.screenshot_tool() == "flameshot":
            self.append(label(_("Screenshot: drag over the part of the screen to show, draw on it or blur what is "
                                "private if you like, then press Enter."), "dim-label", "help-hint"))
        if media.ffmpeg_available():
            self.append(label(_("Screen recording: press Ctrl+Shift+Alt+R to start and again to stop; the video is "
                                "attached by itself."), "dim-label", "help-hint"))
        self.append(label(_("Up to 5 files, videos up to 1 minute. They are made smaller before sending, without "
                            "the place and time photos and videos keep. When you let the community see the "
                            "request, they are published with it once VNLF has reviewed them."),
                          "dim-label", "help-hint"))
        self.recording = label("", "recording-hint")
        self.recording.set_visible(False)
        self.append(self.recording)
        self.tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=12,
                                 row_spacing=12, min_children_per_line=2, max_children_per_line=5)
        self.tiles.set_visible(False)
        self.append(self.tiles)
        self.error = label("", "attachment-error")
        self.error.set_visible(False)
        self.append(self.error)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._dropped)
        self.add_controller(drop)
        if media.ffmpeg_available():
            self._watch_recordings()

    # What the form asks ------------------------------------------------------------------------

    def ready(self) -> list[media.Attachment]:
        return [item.attachment for item in self.items if item.attachment is not None]

    def preparing(self) -> bool:
        return any(item.attachment is None for item in self.items)

    def recording_now(self) -> bool:
        return bool(self.recordings)

    def cleanup(self) -> None:
        """The form is done: running work stops and the prepared files go."""

        self.closed = True
        for item in self.items:
            item.cancel.set()
        for stop in self.recordings.values():
            stop.set()
        if self.monitor is not None:
            self.monitor.cancel()
        media.remove_folder(self.folder)
        self.folder = None

    # Adding files ------------------------------------------------------------------------------

    def add(self, source: Path, name: str = "", *, temporary: bool = False) -> None:
        if self.closed:
            return
        self._show_error("")
        name = name or source.name
        if any(item.source == source for item in self.items):
            return
        if len(self.items) >= media.MAX_FILES:
            self._show_error(_("Up to %d pictures or videos can be attached.") % media.MAX_FILES)
            return
        item = _Item(source, name, temporary)
        if not item.kind:
            self._failed(item, media.MediaError("unsupported"))
            return
        if item.kind == media.VIDEO and not media.ffmpeg_available():
            self._failed(item, media.MediaError("no-ffmpeg"))
            return
        if self.folder is None:
            self.folder = media.new_folder()
        self.items.append(item)
        self.tiles.append(self._tile(item))
        self.tiles.set_visible(True)
        self.on_changed()
        folder = self.folder
        threading.Thread(target=self._prepare, args=(item, folder), daemon=True).start()

    def _prepare(self, item: _Item, folder: Path) -> None:
        last = [-1]

        def progress(fraction: float) -> None:
            percent = int(fraction * 100)
            if percent != last[0]:
                last[0] = percent
                GLib.idle_add(self._progress, item, percent)

        try:
            attachment = media.prepare(item.source, folder, item.name, progress, item.cancel)
        except media.MediaError as error:
            GLib.idle_add(self._prepare_failed, item, error)
        else:
            GLib.idle_add(self._prepared, item, attachment)
        finally:
            if item.temporary:
                item.source.unlink(missing_ok=True)

    def _progress(self, item: _Item, percent: int) -> bool:
        if item.attachment is None and not item.cancel.is_set():
            item.caption.set_label(_("Making it smaller… %d%%") % percent)
        return False

    def _prepared(self, item: _Item, attachment: media.Attachment) -> bool:
        if self.closed or item.cancel.is_set():
            _remove_files(attachment)
            return False
        item.attachment = attachment
        item.spinner.set_visible(False)
        if attachment.thumbnail is not None:
            item.picture.set_filename(str(attachment.thumbnail))
        kind = _("Video") if attachment.kind == media.VIDEO else _("Picture")
        item.caption.set_label(f"{kind} · {GLib.format_size(attachment.size)}")
        if attachment.kind == media.VIDEO:
            item.badge.set_label(media.clock(attachment.duration))
            item.badge.set_visible(True)
        self.on_changed()
        return False

    def _prepare_failed(self, item: _Item, error: media.MediaError) -> bool:
        if item in self.items:
            self._drop(item)
        if error.reason != "cancelled" and not self.closed:
            self._failed(item, error)
        self.on_changed()
        return False

    def _failed(self, item: _Item, error: media.MediaError) -> None:
        self._show_error(_("Could not attach “%(name)s”: %(reason)s.") % {"name": item.name,
                                                                         "reason": reason_text(error)})

    def _show_error(self, text: str) -> None:
        self.error.set_label(text)
        self.error.set_visible(bool(text))

    def _remove(self, item: _Item) -> None:
        item.cancel.set()
        self._drop(item)
        if item.attachment is not None:
            _remove_files(item.attachment)
        self._show_error("")
        self.on_changed()

    def _drop(self, item: _Item) -> None:
        self.items.remove(item)
        if item.tile is not None:
            self.tiles.remove(item.tile)
        self.tiles.set_visible(bool(self.items))

    def _tile(self, item: _Item) -> Gtk.FlowBoxChild:
        frame = Gtk.Box()
        frame.add_css_class("attachment-thumb")
        frame.set_size_request(*media.THUMBNAIL_SIZE)
        # The picture fills the tile; the tile does not grow into the free space of the page.
        frame.set_hexpand(False)
        frame.set_vexpand(False)
        frame.set_overflow(Gtk.Overflow.HIDDEN)
        frame.append(item.picture)
        overlay = Gtk.Overlay(child=frame)
        overlay.add_overlay(item.spinner)
        item.badge.set_halign(Gtk.Align.START)
        item.badge.set_valign(Gtk.Align.END)
        item.badge.set_visible(False)
        overlay.add_overlay(item.badge)
        remove = Gtk.Button(icon_name="window-close-symbolic", halign=Gtk.Align.END, valign=Gtk.Align.START,
                            tooltip_text=_("Remove"))
        remove.add_css_class("circular")
        remove.add_css_class("osd")
        remove.add_css_class("attachment-remove")
        remove.connect("clicked", lambda _button: self._remove(item))
        overlay.add_overlay(remove)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("attachment-tile")
        box.set_tooltip_text(item.name)
        box.append(overlay)
        box.append(item.caption)
        item.tile = Gtk.FlowBoxChild(child=box, focusable=False)
        return item.tile

    # A screenshot ------------------------------------------------------------------------------

    def _screenshot(self) -> None:
        if len(self.items) >= media.MAX_FILES:
            self._show_error(_("Up to %d pictures or videos can be attached.") % media.MAX_FILES)
            return
        window = self.get_root()
        if self.folder is None:
            self.folder = media.new_folder()
        target = self.folder / f"screenshot-{datetime.now():%H%M%S%f}.png"
        self.shot_button.set_sensitive(False)
        # Out of the way, so the screenshot shows what is behind the form.
        if isinstance(window, Gtk.Window):
            window.minimize()

        def work() -> None:
            taken = media.take_screenshot(target)
            GLib.idle_add(done, taken)

        def done(taken: bool) -> bool:
            if self.closed:   # the window was closed meanwhile
                return False
            self.shot_button.set_sensitive(True)
            if isinstance(window, Gtk.Window):
                window.present()
            if taken:
                self.add(target, _("Screenshot"), temporary=True)
            return False

        # The minimize animation is over before Flameshot takes the screen.
        GLib.timeout_add(450, lambda: (threading.Thread(target=work, daemon=True).start(), False)[1])

    # Files -------------------------------------------------------------------------------------

    def _choose(self) -> None:
        media_filter = Gtk.FileFilter(name=_("Pictures and videos"))
        media_filter.add_mime_type("image/*")
        media_filter.add_mime_type("video/*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(media_filter)
        dialog = Gtk.FileDialog(title=_("Choose pictures or videos"), modal=True, filters=filters,
                                default_filter=media_filter)
        root = self.get_root()
        dialog.open_multiple(root if isinstance(root, Gtk.Window) else None, None, self._chosen)

    def _chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return   # cancelled
        for index in range(files.get_n_items()):
            path = files.get_item(index).get_path()
            if path and not self.closed:
                self.add(Path(path))

    def _dropped(self, _target: Gtk.DropTarget, value: Gdk.FileList, _x: float, _y: float) -> bool:
        for file in value.get_files():
            path = file.get_path()
            if path:
                self.add(Path(path))
        return True

    # Screen recordings -------------------------------------------------------------------------

    def _watch_recordings(self) -> None:
        folder = media.recordings_folder(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS))
        try:
            self.monitor = Gio.File.new_for_path(str(folder)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except GLib.Error:
            return
        self.monitor.connect("changed", self._folder_changed)

    def _folder_changed(self, _monitor, file: Gio.File, _other, event: Gio.FileMonitorEvent) -> None:
        if event not in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.CHANGED,
                         Gio.FileMonitorEvent.CHANGES_DONE_HINT) or self.closed:
            return
        started = media.recording_started(file.get_basename() or "")
        path = Path(file.get_path() or "")
        if started is None or path in self.recordings or any(item.source == path for item in self.items):
            return
        if started < self.opened - RECORDING_GRACE:
            return   # an old recording copied or changed meanwhile
        stop = threading.Event()
        self.recordings[path] = stop
        self._show_recording(started)
        threading.Thread(target=self._follow, args=(path, started, stop), daemon=True).start()
        self.on_changed()

    def _follow(self, path: Path, started: datetime, stop: threading.Event) -> None:
        """While Cinnamon records: how long so far; once it stopped, the video is attached."""

        size, still = -1, 0
        while not stop.wait(1.0):
            try:
                now_size = path.stat().st_size
            except OSError:
                GLib.idle_add(self._recording_over, path, 0.0)
                return
            duration = media.recording_finished(path)
            if duration:
                GLib.idle_add(self._recording_over, path, duration)
                return
            still = still + 1 if now_size == size else 0
            size = now_size
            if still >= STUCK_SECONDS:
                GLib.idle_add(self._recording_over, path, 0.0)
                return
            GLib.idle_add(self._show_recording, started)

    def _show_recording(self, started: datetime) -> bool:
        if self.closed or not self.recordings:
            return False
        seconds = max(0.0, (datetime.now().astimezone() - started).total_seconds())
        if media.too_long(seconds):
            text = _("Recording the screen: %s. That is over 1 minute and cannot be sent: press Ctrl+Shift+Alt+R "
                     "to stop and record a shorter one.") % media.clock(seconds)
        else:
            text = _("Recording the screen: %s. Press Ctrl+Shift+Alt+R to stop.") % media.clock(seconds)
        self.recording.set_label(text)
        self.recording.set_visible(True)
        return False

    def _recording_over(self, path: Path, duration: float) -> bool:
        """Stopped (duration), gone or stuck (0)."""

        self.recordings.pop(path, None)
        if not self.recordings:
            self.recording.set_visible(False)
        if self.closed:
            return False
        if duration and media.too_long(duration):
            self._show_error(_("Could not attach “%(name)s”: %(reason)s.") % {
                "name": _("Screen recording"), "reason": reason_text(media.MediaError("too-long", duration))})
        elif duration:
            self.add(path, _("Screen recording"))
        self.on_changed()
        return False


def _remove_files(attachment: media.Attachment) -> None:
    attachment.path.unlink(missing_ok=True)
    if attachment.thumbnail is not None:
        attachment.thumbnail.unlink(missing_ok=True)
