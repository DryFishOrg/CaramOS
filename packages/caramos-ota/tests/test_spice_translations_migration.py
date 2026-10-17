"""Migration 20261016220000_translate_cinnamon_spices compiles the po/ folders of the spices CaramOS copies into
/usr/share/cinnamon, where nothing compiled them, into ~/.local/share/locale like System Settings does."""

from __future__ import annotations

import gettext
import importlib.util
import json
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caramos_ota_update.context import MigrationContext

ROOT = Path(__file__).parents[1]
MIGRATION_DIR = (
    ROOT / "usr/lib/python3/dist-packages/caramos_ota_update/migrations/20261016220000_translate_cinnamon_spices"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("translate_cinnamon_spices_test", MIGRATION_DIR / "migration.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = load_migration()

PO = """\
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
"Plural-Forms: nplurals=1; plural=0;\\n"

msgid "All applications"
msgstr "{all_applications}"

msgid "%d app"
msgid_plural "%d apps"
msgstr[0] "%d ứng dụng"
"""


@unittest.skipUnless(Path(migration.MSGFMT).is_file(), "msgfmt (package gettext) is not installed")
class TranslateCinnamonSpicesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.owner = (os.getuid(), os.getgid())
        self.users: list[pwd.struct_passwd] = []
        (self.root / "etc/skel/.local/share").mkdir(parents=True)
        for name, value in {
            "SPICES": self.root / "usr/share/cinnamon",
            "SKEL": self.root / "etc/skel",
            "SKEL_OWNER": self.owner,
            "PASSWD_HOME_ROOT": self.root / "home",
            "_desktop_users": lambda: self.users,
        }.items():
            patcher = patch.object(migration, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.spice("applets", "Cinnamenu@json", {"vi": "Tất cả ứng dụng", "fr": "Toutes les applications"})
        self.spice("extensions", "blur-overview@nailfarmer.nailfarmer.com", {"vi": "Tất cả"})

    def spice(self, kind: str, uuid: str, translations: dict[str, str]) -> Path:
        folder = self.root / "usr/share/cinnamon" / kind / uuid
        (folder / "po").mkdir(parents=True)
        (folder / "metadata.json").write_text(json.dumps({"uuid": uuid}), encoding="utf-8")
        for language, text in translations.items():
            (folder / "po" / f"{language}.po").write_text(PO.format(all_applications=text), encoding="utf-8")
        return folder

    def add_user(self, name: str) -> Path:
        home = self.root / "home" / name
        home.mkdir(parents=True)
        self.users.append(pwd.struct_passwd((name, "x", *self.owner, name, str(home), "/bin/bash")))
        return home

    def run_migration(self, *, dry_run: bool = False) -> None:
        migration.run(MigrationContext(dry_run=dry_run))

    def translate(self, base: Path, uuid: str, language: str, text: str) -> str:
        locale = base / ".local/share/locale"
        return gettext.translation(uuid, localedir=str(locale), languages=[language]).gettext(text)

    def test_the_start_menu_gets_its_translations_for_users_and_new_accounts(self) -> None:
        home = self.add_user("caram")
        self.run_migration()
        for base in (self.root / "etc/skel", home):
            self.assertEqual("Tất cả ứng dụng", self.translate(base, "Cinnamenu@json", "vi", "All applications"))
            self.assertEqual("Toutes les applications", self.translate(base, "Cinnamenu@json", "fr", "All applications"))
            self.assertEqual("Tất cả", self.translate(base, "blur-overview@nailfarmer.nailfarmer.com", "vi", "All applications"))
            translation = gettext.translation("Cinnamenu@json", localedir=str(base / ".local/share/locale"), languages=["vi"])
            self.assertEqual("%d ứng dụng", translation.ngettext("%d app", "%d apps", 3))
            mo = base / ".local/share/locale/vi/LC_MESSAGES/Cinnamenu@json.mo"
            self.assertEqual(os.getuid(), mo.stat().st_uid)
            self.assertEqual(0o644, mo.stat().st_mode & 0o777)
            self.assertEqual([], [path.name for path in mo.parent.iterdir() if path.name.startswith(".")])

    def test_a_translation_already_there_is_kept(self) -> None:
        home = self.add_user("caram")
        mine = home / ".local/share/locale/vi/LC_MESSAGES/Cinnamenu@json.mo"
        mine.parent.mkdir(parents=True)
        mine.write_bytes(b"not ours")
        self.run_migration()
        self.assertEqual(b"not ours", mine.read_bytes())
        self.assertEqual("Tất cả", self.translate(home, "blur-overview@nailfarmer.nailfarmer.com", "vi", "All applications"))

    def test_a_spice_the_user_installed_again_keeps_its_own_translations(self) -> None:
        home = self.add_user("caram")
        (home / ".local/share/cinnamon/applets/Cinnamenu@json").mkdir(parents=True)
        self.run_migration()
        self.assertFalse((home / ".local/share/locale/vi/LC_MESSAGES/Cinnamenu@json.mo").exists())
        self.assertTrue((home / ".local/share/locale/vi/LC_MESSAGES/blur-overview@nailfarmer.nailfarmer.com.mo").is_file())
        self.assertTrue((self.root / "etc/skel/.local/share/locale/vi/LC_MESSAGES/Cinnamenu@json.mo").is_file())

    def test_nothing_is_written_through_a_symlink(self) -> None:
        home = self.add_user("caram")
        outside = self.root / "outside"
        outside.mkdir()
        (home / ".local/share").mkdir(parents=True)
        (home / ".local/share/locale").symlink_to(outside)
        self.run_migration()
        self.assertEqual([], list(outside.iterdir()))

    def test_a_folder_of_someone_else_is_left_alone(self) -> None:
        home = self.add_user("caram")
        with patch.object(migration, "_desktop_users", return_value=[
            pwd.struct_passwd(("caram", "x", os.getuid() + 1, os.getgid(), "caram", str(home), "/bin/bash")),
        ]):
            self.run_migration()
        self.assertFalse((home / ".local").exists())

    def test_a_po_file_msgfmt_rejects_is_skipped(self) -> None:
        folder = self.root / "usr/share/cinnamon/applets/Cinnamenu@json/po"
        (folder / "de.po").write_text('msgid "All applications"\nmsgstr "Alle\n', encoding="utf-8")
        (folder / "bad name.po").write_text(PO.format(all_applications="x"), encoding="utf-8")
        self.run_migration()
        locale = self.root / "etc/skel/.local/share/locale"
        self.assertFalse((locale / "de").exists())
        self.assertEqual({"fr", "vi"}, {path.name for path in locale.iterdir()})

    def test_dry_run_writes_nothing(self) -> None:
        self.add_user("caram")
        self.run_migration(dry_run=True)
        self.assertFalse((self.root / "etc/skel/.local/share/locale").exists())
        self.assertFalse((self.root / "home/caram/.local").exists())

    def test_without_spices_nothing_happens(self) -> None:
        with patch.object(migration, "SPICES", self.root / "missing"):
            self.run_migration()
        self.assertFalse((self.root / "etc/skel/.local/share/locale").exists())


class ManifestTests(unittest.TestCase):
    def test_the_registry_knows_the_migration_in_both_languages(self) -> None:
        from caramos_ota_update.registry import discover_migrations

        item = {entry.migration_id: entry for entry in discover_migrations()}[MIGRATION_DIR.name]
        self.assertEqual("normal", item.severity)
        self.assertIn("Start Menu", item.title)
        self.assertTrue(item.title_en and item.summary_en and item.size_en)
        self.assertIn("Tất cả ứng dụng", item.summary)


if __name__ == "__main__":
    unittest.main()
