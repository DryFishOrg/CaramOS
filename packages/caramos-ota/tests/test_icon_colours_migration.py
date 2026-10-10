"""Tests for the migration that removes the unused Tela circle icon colours."""

from __future__ import annotations

import importlib.util
import os
import pwd
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caramos_ota_update.context import MigrationContext

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "usr/lib/python3/dist-packages/caramos_ota_update/migrations"
    / "20261010120000_remove_unused_icon_colours/migration.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("remove_unused_icon_colours_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = load_migration()


class UnusedIconColoursTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.icons = self.root / "usr/share/icons"
        for colour in ("", "-red", "-nord", "-blue"):
            for variant in ("", "-light", "-dark"):
                folder = self.icons / f"Tela-circle{colour}{variant}"
                (folder / "scalable/apps").mkdir(parents=True)
                (folder / "index.theme").write_text("[Icon Theme]\n", encoding="utf-8")
                (folder / "scalable/apps/firefox.svg").write_text("<svg/>", encoding="utf-8")
        self.greeter = self.root / "etc/lightdm/slick-greeter.conf"
        self.greeter.parent.mkdir(parents=True)
        self.greeter.write_text("[Greeter]\nicon-theme-name=Tela-circle-light\n", encoding="utf-8")
        self.dconf_defaults = self.root / "etc/dconf/db/local.d"
        self.dconf_defaults.mkdir(parents=True)
        (self.dconf_defaults / "00-caramos-theme").write_text("icon-theme='Tela-circle-light'\n", encoding="utf-8")
        self.home = self.root / "home/an"
        (self.home / ".config/dconf").mkdir(parents=True)
        uid, gid = os.getuid(), os.getgid()
        self.users = [pwd.struct_passwd(("an", "x", uid, gid, "an", str(self.home), "/bin/bash"))]
        for name, value in {
            "ICONS_DIR": self.icons,
            "SYSTEM_SETTINGS": (self.greeter, self.dconf_defaults),
            "_desktop_users": lambda: self.users,
        }.items():
            patcher = patch.object(migration, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_migration(self, *, dry_run: bool = False, owned: tuple[str, ...] = ()) -> list[list[str]]:
        context = MigrationContext(dry_run=dry_run)
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            code = 0 if Path(args[-1]).name in owned else 1
            return subprocess.CompletedProcess(list(args), code, "", "")

        with patch.object(context, "run_command", side_effect=fake_run):
            migration.run(context)
        return calls

    def themes(self) -> list[str]:
        return sorted(path.name for path in self.icons.iterdir())

    def test_removes_unused_colours_and_keeps_the_standard_one(self) -> None:
        self.run_migration()

        self.assertEqual(["Tela-circle", "Tela-circle-dark", "Tela-circle-light"], self.themes())

    def test_keeps_a_colour_a_user_selected_with_its_three_folders(self) -> None:
        # dconf keeps string values as text inside its binary database.
        (self.home / ".config/dconf/user").write_bytes(b"GVariant\x00\x01icon-theme\x00'Tela-circle-nord-dark'\x00")

        self.run_migration()

        self.assertEqual(
            ["Tela-circle", "Tela-circle-dark", "Tela-circle-light",
             "Tela-circle-nord", "Tela-circle-nord-dark", "Tela-circle-nord-light"],
            self.themes(),
        )

    def test_keeps_a_colour_named_by_the_login_screen_or_system_defaults(self) -> None:
        self.greeter.write_text("[Greeter]\nicon-theme-name=Tela-circle-red\n", encoding="utf-8")
        (self.dconf_defaults / "99-local").write_text("icon-theme='Tela-circle-blue-light'\n", encoding="utf-8")

        self.run_migration()

        self.assertIn("Tela-circle-red-light", self.themes())
        self.assertIn("Tela-circle-blue-dark", self.themes())
        self.assertNotIn("Tela-circle-nord", self.themes())

    def test_does_not_follow_a_symlinked_dconf_database(self) -> None:
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("icon-theme='Tela-circle-nord'\n", encoding="utf-8")
        (self.home / ".config/dconf/user").symlink_to(elsewhere)

        self.run_migration()

        self.assertNotIn("Tela-circle-nord", self.themes())

    def test_keeps_folders_a_package_owns(self) -> None:
        calls = self.run_migration(owned=("Tela-circle-red-light",))

        self.assertIn("Tela-circle-red", self.themes())
        self.assertIn("Tela-circle-red-dark", self.themes())
        self.assertNotIn("Tela-circle-blue", self.themes())
        self.assertIn(["dpkg-query", "--search", str(self.icons / "Tela-circle-red")], calls)

    def test_dry_run_and_second_run_change_nothing(self) -> None:
        before = self.themes()
        self.run_migration(dry_run=True)
        self.assertEqual(before, self.themes())

        self.run_migration()
        calls = self.run_migration()
        self.assertEqual([], calls)
        self.assertEqual(["Tela-circle", "Tela-circle-dark", "Tela-circle-light"], self.themes())

    def test_leaves_a_symlinked_theme_folder_alone(self) -> None:
        target = self.icons / "Tela-circle-red"
        link = self.icons / "Tela-circle-pink"
        link.symlink_to(target, target_is_directory=True)

        self.run_migration()

        self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
