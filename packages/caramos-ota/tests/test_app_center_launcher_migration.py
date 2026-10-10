"""Tests for the migration that installs the App Center and puts it on the Desktop."""

from __future__ import annotations

import importlib.util
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caramos_ota_update.context import MigrationContext

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "usr/lib/python3/dist-packages/caramos_ota_update/migrations"
    / "20261009120000_add_app_center_launcher/migration.py"
)
LAUNCHER = "[Desktop Entry]\nType=Application\nName=CaramOS App Center\nExec=/usr/bin/caramos-app-center\n"


def load_migration():
    spec = importlib.util.spec_from_file_location("add_app_center_launcher_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = load_migration()


class AppCenterLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.system = self.root / "usr/share/applications/caramos-app-center.desktop"
        self.system.parent.mkdir(parents=True)
        self.system.write_text(LAUNCHER, encoding="utf-8")
        self.skel = self.root / "etc/skel/Desktop"
        self.home_root = self.root / "home"
        uid, gid = os.getuid(), os.getgid()
        self.users = []
        for name in ("an", "binh"):
            home = self.home_root / name
            home.mkdir(parents=True)
            self.users.append(pwd.struct_passwd((name, "x", uid, gid, name, str(home), "/bin/bash")))
        for name, value in {
            "SYSTEM_LAUNCHER": self.system,
            "SKEL_DESKTOP": self.skel,
            "PASSWD_HOME_ROOT": self.home_root,
            "_desktop_users": lambda: self.users,
        }.items():
            patcher = patch.object(migration, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def home(self, name: str) -> Path:
        return self.home_root / name

    def test_adds_an_executable_launcher_to_skel_and_each_desktop(self) -> None:
        (self.home("an") / "Desktop").mkdir()
        (self.home("binh") / ".config").mkdir()
        (self.home("binh") / ".config/user-dirs.dirs").write_text(
            'XDG_DOWNLOAD_DIR="$HOME/Tải về"\nXDG_DESKTOP_DIR="$HOME/Màn hình nền"\n', encoding="utf-8")
        (self.home("binh") / "Màn hình nền").mkdir()

        migration.run(MigrationContext())

        for path in (self.skel / "caramos-app-center.desktop",
                     self.home("an") / "Desktop/caramos-app-center.desktop",
                     self.home("binh") / "Màn hình nền/caramos-app-center.desktop"):
            self.assertEqual(LAUNCHER, path.read_text(encoding="utf-8"), path)
            self.assertEqual(0o755, path.stat().st_mode & 0o777, path)
        self.assertEqual([], list((self.home("an") / "Desktop").glob(".caramos-app-center.desktop.*")))

    def test_keeps_existing_launchers_and_skips_unsafe_folders(self) -> None:
        desktop = self.home("an") / "Desktop"
        desktop.mkdir()
        (desktop / "caramos-app-center.desktop").write_text("mine", encoding="utf-8")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (self.home("binh") / "Desktop").symlink_to(elsewhere)

        migration.run(MigrationContext())

        self.assertEqual("mine", (desktop / "caramos-app-center.desktop").read_text(encoding="utf-8"))
        self.assertEqual([], list(elsewhere.iterdir()))

    def test_dry_run_changes_nothing(self) -> None:
        (self.home("an") / "Desktop").mkdir()
        migration.run(MigrationContext(dry_run=True))
        self.assertFalse(self.skel.exists())
        self.assertEqual([], list((self.home("an") / "Desktop").iterdir()))

    def test_installs_the_release_when_missing_and_survives_a_failed_install(self) -> None:
        self.system.unlink()
        calls: list[dict] = []

        def install(**kwargs):
            calls.append(kwargs)
            self.system.write_text(LAUNCHER, encoding="utf-8")
            return "installed"

        with patch.object(migration, "update_app_center", install):
            migration.run(MigrationContext())
        self.assertEqual([{"install_missing": True}], calls)
        self.assertTrue((self.skel / "caramos-app-center.desktop").is_file())

        self.system.unlink()
        (self.skel / "caramos-app-center.desktop").unlink()

        def offline(**_kwargs):
            raise migration.AppCenterUpdateError("cannot download latest.json")

        with patch.object(migration, "update_app_center", offline):
            migration.run(MigrationContext())
        self.assertFalse((self.skel / "caramos-app-center.desktop").exists())

    def test_desktop_dir_ignores_paths_outside_the_home(self) -> None:
        home = self.home("an")
        (home / ".config").mkdir()
        (home / ".config/user-dirs.dirs").write_text('XDG_DESKTOP_DIR="/etc"\n', encoding="utf-8")
        self.assertEqual(home / "Desktop", migration.desktop_dir(home))


if __name__ == "__main__":
    unittest.main()
