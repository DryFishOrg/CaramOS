"""Migration 20261018120000_restore_taskbar_after_cinnamon_update and caramos_ota_update.cinnamon_patches: the
CaramOS taskbar after Mint updates Cinnamon (ticket H-1046), and the dpkg trigger that keeps it."""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from caramos_ota_update import cinnamon_patches
from caramos_ota_update.context import MigrationContext

ROOT = Path(__file__).parents[1]
REPO = ROOT.parents[1]
migration = importlib.import_module("caramos_ota_update.migrations.20261018120000_restore_taskbar_after_cinnamon_update.migration")
taskbar = importlib.import_module("caramos_ota_update.migrations.20260803120000_apply_three_dock_taskbar.migration")
wallpaper = importlib.import_module("caramos_ota_update.migrations.20260804223346_change_default_wallpaper.migration")

OLD = "'[{\"panelId\": 1, \"left\": 32, \"right\": 18}]'"
NEW = "'[{\"panelId\": 1, \"left\": 32, \"center\": 32, \"right\": 18}]'"

# The lines of Cinnamon 6.6.9's appGroup.js that the running-dot change edits (they are the same in 6.6.4).
APP_GROUP_669 = (
    "        this.actor.add_child(this.label);\n"
    "        this.iconSize = iconSize;\n"
    "        this.actor.style = existingStyle + 'margin-' + direction + ':' + spacing + 'px;';\n"
    "            this.actor.height = panelHeight;\n"
    "        const iconYPadding = Math.floor(Math.max(0, allocHeight - naturalHeight) / 2);\n"
    "        const notifBadgeBox = new Clutter.ActorBox();\n"
    "                alloc.natural_size = iconNaturalSize + 6 * global.ui_scale;\n"
    "        this.iconBox.allocate(childBox, flags);\n"
    "        this.groupState.metaWindows.splice(refWindow, 1);\n"
    "    setActiveStatus(state) {\n"
    "        if (state && !this.actor.has_style_pseudo_class('active')) {\n"
    "            this.actor.add_style_pseudo_class('active');\n"
    "        } else {\n"
    "            this.actor.remove_style_pseudo_class('active');\n"
    "        }\n"
    "    }\n"
    "        this.groupState.set({windowCount: this.groupState.metaWindows ? this.groupState.metaWindows.length : 0});\n"
)
SYSTRAY_664 = (
    "            icon.set_y_align(Clutter.ActorAlign.CENTER);\n"
    "            button.set_y_align(Clutter.ActorAlign.CENTER);\n"
)
SYSTRAY_669 = (
    "            icon.set_x_align(Clutter.ActorAlign.CENTER);\n"
    "            icon.set_y_align(Clutter.ActorAlign.FILL);\n"
    "            button.set_y_align(Clutter.ActorAlign.FILL);\n"
)
BACKGROUNDS = wallpaper.PATCH_ANCHOR + "                    else:\n"


class IconSizeTests(unittest.TestCase):
    def test_gsettings_values(self) -> None:
        self.assertTrue(migration.is_caramos_value(OLD + "\n"))
        self.assertFalse(migration.is_caramos_value(NEW))
        self.assertFalse(migration.is_caramos_value("'[{\"panelId\": 1, \"left\": 24, \"right\": 18}]'"))
        self.assertFalse(migration.is_caramos_value("not a value"))
        self.assertEqual(json.loads(migration.ICON_SIZES), migration.parse_icon_sizes(NEW))

    def test_system_defaults(self) -> None:
        source = f"[org/cinnamon]\npanels-height=['1:48']\npanel-zone-icon-sizes={OLD}\nfavorite-apps=@as []\n"
        updated = migration.updated_defaults(source)
        self.assertEqual(f"[org/cinnamon]\npanels-height=['1:48']\npanel-zone-icon-sizes={NEW}\nfavorite-apps=@as []\n",
                         updated)
        self.assertEqual(updated, migration.updated_defaults(updated))
        custom = "[org/cinnamon]\npanel-zone-icon-sizes='[{\"panelId\": 1, \"left\": 24}]'\n"
        self.assertIsNone(migration.updated_defaults(custom))
        self.assertIsNone(migration.updated_defaults(f"[org/other]\npanel-zone-icon-sizes={OLD}\n"))

    @unittest.skipUnless((REPO / "config").is_dir(), "ISO tree is not in the source package")
    def test_new_installs_get_the_middle_size(self) -> None:
        # The migration runs in ISO builds too, but the defaults the ISO writes already carry the size.
        for path in ("config/includes.chroot/etc/dconf/db/local.d/00-caramos-theme",
                     "config/includes.chroot/etc/dconf/db/local.d/01-caramos-task17-panel",
                     "config/hooks/live/0170-cinnamon-task17-spices.hook.chroot"):
            text = (REPO / path).read_text(encoding="utf-8")
            self.assertIn(f"panel-zone-icon-sizes={NEW}\n", text, path)

    def test_a_user_with_the_caramos_sizes_gets_the_middle_size(self) -> None:
        context = MigrationContext()
        answers = [subprocess.CompletedProcess([], 0, OLD + "\n", ""), subprocess.CompletedProcess([], 0, "", "")]
        with mock.patch.object(migration.subprocess, "run", side_effect=answers) as run, \
                mock.patch.object(context, "log"):
            migration._update_user(context, "caram", 1000, Path("/home/caram"))
        set_call = run.call_args_list[1].args[0]
        self.assertEqual(["runuser", "-u", "caram", "--"], set_call[:4])
        self.assertEqual(["gsettings", "set", "org.cinnamon", "panel-zone-icon-sizes", NEW], set_call[-5:])

    def test_own_sizes_and_the_new_size_are_only_read(self) -> None:
        context = MigrationContext()
        for value in (NEW, "'[{\"panelId\": 1, \"left\": 24, \"center\": 40, \"right\": 22}]'"):
            with mock.patch.object(migration.subprocess, "run",
                                   side_effect=[subprocess.CompletedProcess([], 0, value + "\n", "")]) as run, \
                    mock.patch.object(context, "log"):
                migration._update_user(context, "caram", 1000, Path("/home/caram"))
            self.assertEqual(1, run.call_count)


class RunTests(unittest.TestCase):
    def run_migration(self, *, dry_run: bool = False, logged_in: bool = False,
                      restore_error: Exception | None = None) -> tuple[str, mock.Mock, mock.Mock, mock.Mock]:
        with tempfile.TemporaryDirectory() as temp:
            defaults = Path(temp) / "01-caramos-task17-panel"
            defaults.write_text(f"[org/cinnamon]\npanel-zone-icon-sizes={OLD}\n", encoding="utf-8")
            bus = Path(temp) / "bus"
            if logged_in:
                bus.touch()
            context = MigrationContext(dry_run=dry_run)
            with mock.patch.object(migration, "DCONF_DEFAULT_FILES", (defaults, Path(temp) / "missing")), \
                    mock.patch.object(migration, "_desktop_users", return_value=[("caram", 1000, Path(temp))]), \
                    mock.patch.object(migration, "_session_bus", return_value=bus), \
                    mock.patch.object(migration.cinnamon_patches, "restore", side_effect=restore_error) as restore, \
                    mock.patch.object(migration, "_update_user") as update_user, \
                    mock.patch.object(context, "run_command") as run_command, \
                    mock.patch.object(context, "log"):
                migration.run(context)
            return defaults.read_text(encoding="utf-8"), restore, run_command, update_user

    def test_restores_cinnamon_and_sizes_every_user(self) -> None:
        text, restore, run_command, update_user = self.run_migration()
        self.assertIn(NEW, text)
        restore.assert_called_once()
        run_command.assert_called_once_with(["dconf", "update"], allow_fail=True)
        self.assertEqual("caram", update_user.call_args.args[1])

    def test_no_dconf_update_under_a_running_session(self) -> None:
        text, _restore, run_command, update_user = self.run_migration(logged_in=True)
        self.assertIn(NEW, text)
        run_command.assert_not_called()
        update_user.assert_called_once()

    def test_the_size_is_set_even_when_cinnamon_cannot_be_changed(self) -> None:
        text, _restore, _run_command, update_user = self.run_migration(restore_error=RuntimeError("boom"))
        self.assertIn(NEW, text)
        update_user.assert_called_once()

    def test_dry_run_changes_nothing(self) -> None:
        text, _restore, run_command, update_user = self.run_migration(dry_run=True)
        self.assertIn(OLD, text)
        run_command.assert_not_called()
        update_user.assert_not_called()

    def test_manifest(self) -> None:
        manifest = json.loads(Path(migration.__file__).with_name("manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(2, manifest["schema"])
        for key in ("title", "summary", "size"):
            self.assertTrue(manifest[key] and manifest[f"{key}_en"], key)
        self.assertEqual(len(manifest["release_notes_vi"]), len(manifest["release_notes_en"]))

    def test_the_update_center_leads_with_the_taskbar_not_zalo(self) -> None:
        # Most computers have no Zalo; the Zalo update after this one must not be the headline.
        from caramos_ota import manifest
        from caramos_ota.models import ReleaseInfo
        from caramos_ota_update.registry import discover_migrations

        catalog = {item.migration_id: item for item in discover_migrations()}
        pending = [catalog["20261018120000_restore_taskbar_after_cinnamon_update"],
                   catalog["20261018120100_update_zalo_native_calls"]]
        current = ReleaseInfo(name="CaramOS", version="1.3.1", codename="noble", channel="stable")
        result = manifest._manifest_from_descriptors(pending, current, "1.3.2")
        self.assertEqual(pending[0].title, result.title)


class CinnamonPatchTests(unittest.TestCase):
    def restore(self, app_group: str, systray: str, backgrounds: str,
                applied: set[str] | None = None) -> tuple[str, str, str]:
        with tempfile.TemporaryDirectory() as temp:
            paths = [Path(temp) / name for name in ("appGroup.js", "applet.js", "cs_backgrounds.py")]
            for path, text in zip(paths, (app_group, systray, backgrounds)):
                path.write_text(text, encoding="utf-8")
            context = MigrationContext()
            with mock.patch.object(taskbar, "APP_GROUP", paths[0]), \
                    mock.patch.object(taskbar, "SYSTRAY_TARGET", paths[1]), \
                    mock.patch.object(wallpaper, "CINNAMON_BACKGROUNDS_MODULE", paths[2]), \
                    mock.patch.object(context, "log"):
                cinnamon_patches.restore(context, applied)
            return tuple(path.read_text(encoding="utf-8") for path in paths)

    def test_cinnamon_6_6_9_gets_the_32_px_icons_and_the_dot_again(self) -> None:
        app_group, systray, backgrounds = self.restore(APP_GROUP_669, SYSTRAY_669, BACKGROUNDS)
        self.assertIn("this.iconSize = this.state.isHorizontal ? 32 * global.ui_scale : iconSize;", app_group)
        self.assertEqual(1, app_group.count(taskbar.RUNNING_DOT_MARKER))
        # 6.6.9 already makes tray buttons as high as the panel: nothing to change there.
        self.assertEqual(SYSTRAY_669, systray)
        self.assertEqual(1, backgrounds.count(wallpaper.PATCH_MARKER))

    def test_cinnamon_6_6_4_tray_still_gets_the_hover(self) -> None:
        _app_group, systray, _backgrounds = self.restore(APP_GROUP_669, SYSTRAY_664, BACKGROUNDS)
        self.assertEqual(1, systray.count(taskbar.SYSTRAY_HOVER_MARKER))

    def test_files_already_changed_stay_the_same(self) -> None:
        once = self.restore(APP_GROUP_669, SYSTRAY_664, BACKGROUNDS)
        self.assertEqual(once, self.restore(*once))

    def test_unknown_files_are_left_alone(self) -> None:
        self.assertEqual(("distro update",) * 3, self.restore("distro update", "distro update", "distro update"))

    def test_only_changes_of_migrations_in_the_ledger(self) -> None:
        app_group, systray, backgrounds = self.restore(APP_GROUP_669, SYSTRAY_664, BACKGROUNDS,
                                                       applied={cinnamon_patches.WALLPAPER_MIGRATION})
        self.assertEqual((APP_GROUP_669, SYSTRAY_664), (app_group, systray))
        self.assertIn(wallpaper.PATCH_MARKER, backgrounds)

    def test_the_trigger_entry_point_reads_the_ledger(self) -> None:
        ledger = {"schema": 1, "applied_migrations": [{"id": cinnamon_patches.TASKBAR_MIGRATION}]}
        with mock.patch.object(cinnamon_patches.os, "geteuid", return_value=0), \
                mock.patch.object(cinnamon_patches, "init_log"), \
                mock.patch.object(cinnamon_patches, "load_ledger", return_value=ledger), \
                mock.patch.object(cinnamon_patches, "restore") as restore:
            self.assertEqual(0, cinnamon_patches.main())
        self.assertEqual({cinnamon_patches.TASKBAR_MIGRATION}, restore.call_args.args[1])
        with mock.patch.object(cinnamon_patches.os, "geteuid", return_value=0), \
                mock.patch.object(cinnamon_patches, "init_log"), \
                mock.patch.object(cinnamon_patches, "load_ledger", return_value=None), \
                mock.patch.object(cinnamon_patches, "restore") as restore, \
                mock.patch("builtins.print"):
            self.assertEqual(0, cinnamon_patches.main())
        restore.assert_not_called()

    def test_a_failure_is_logged_not_raised(self) -> None:
        ledger = {"schema": 1, "applied_migrations": []}
        with mock.patch.object(cinnamon_patches.os, "geteuid", return_value=0), \
                mock.patch.object(cinnamon_patches, "init_log"), \
                mock.patch.object(cinnamon_patches, "load_ledger", return_value=ledger), \
                mock.patch.object(cinnamon_patches, "restore", side_effect=OSError("read-only")), \
                mock.patch.object(cinnamon_patches, "log_error") as log_error:
            self.assertEqual(1, cinnamon_patches.main())
        log_error.assert_called_once()


class TriggerTests(unittest.TestCase):
    def test_the_package_watches_every_file_it_changes(self) -> None:
        watched = re.findall(r"(?m)^interest-noawait (\S+)$", (ROOT / "debian/triggers").read_text(encoding="utf-8"))
        for path in (taskbar.APP_GROUP, taskbar.SYSTRAY_TARGET, wallpaper.CINNAMON_BACKGROUNDS_MODULE):
            self.assertIn(str(path.parent), watched)

    def test_postinst_restores_the_changes_without_failing_the_update(self) -> None:
        postinst = (ROOT / "debian/postinst").read_text(encoding="utf-8")
        self.assertRegex(postinst, r"(?s)    triggered\)\n.*python3 -I -m caramos_ota_update\.cinnamon_patches \|\| true\n")


if __name__ == "__main__":
    unittest.main()
