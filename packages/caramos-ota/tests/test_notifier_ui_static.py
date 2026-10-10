"""Static checks for user-facing OTA update list content."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
DIST = ROOT / "usr/lib/python3/dist-packages"
UI = DIST / "caramos_ota_notifier/ui.py"


class NotifierUiStaticTests(unittest.TestCase):
    def test_update_list_shows_descriptions_without_versions_or_badges(self) -> None:
        source = UI.read_text(encoding="utf-8")
        self.assertIn('pkg.get("description") or pkg.get("name")', source)
        self.assertIn("%d change in this update", source)
        self.assertNotIn('badge = "bắt buộc"', source)
        self.assertNotIn("ver_lbl.set_text", source)
        self.assertNotIn("set_tooltip_text(str(pkg", source)
        update_page = source.split("def build_update_page", 1)[1].split("def _screen_dialog_size", 1)[0]
        self.assertNotIn("Gtk.Paned", update_page)
        self.assertNotIn("Nội dung cập nhật", update_page)
        self.assertNotIn("notes_panel", update_page)

    def test_every_page_keeps_its_buttons_outside_the_scrolled_body(self) -> None:
        # Issue #77: on small screens the update button fell below the window.
        source = UI.read_text(encoding="utf-8")
        self.assertIn("window.set_size_request(380, 360)", source)
        for builder in ("build_update_page", "build_progress_page", "build_result_page", "build_no_update_page"):
            body = source.split(f"def {builder}", 1)[1].split("\ndef ", 1)[0]
            self.assertIn("return _page(Gtk", body.replace("page = _page(Gtk", "return _page(Gtk"), builder)


class GtkImportTests(unittest.TestCase):
    def test_gtk3_imports_even_when_gtk4_typelibs_are_installed(self) -> None:
        code = (
            "import gi\n"
            "from caramos_ota_notifier.ui import import_gtk\n"
            "Gtk, Gdk, _ = import_gtk()\n"
            "print(Gtk.get_major_version(), gi.get_required_version('Gdk'))\n"
        )
        env = dict(os.environ, PYTHONPATH=str(DIST))
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        if "Namespace Gtk not available" in result.stderr or "No module named 'gi'" in result.stderr:
            self.skipTest("GTK 3 introspection is not installed")
        self.assertEqual("3 3.0", result.stdout.strip(), result.stderr)


if __name__ == "__main__":
    unittest.main()
