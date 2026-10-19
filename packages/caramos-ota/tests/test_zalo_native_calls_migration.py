"""Migration 20261018120100_update_zalo_native_calls and caramos_ota_update.zalo_update: the Zalo CaramOS or the
App Center installed moves to 26.10.11, which calls without Wine (tickets H-1039 and H-1042). The download runs
in the background and the daily check retries it, so a failed or slow download never fails the OTA run."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from caramos_ota_update import zalo_update
from caramos_ota_update.context import MigrationContext, MigrationContextError

ROOT = Path(__file__).parents[1]
migration = importlib.import_module("caramos_ota_update.migrations.20261018120100_update_zalo_native_calls.migration")

FULL = b"zalo 26.10.10 Full with Wine, from the App Center"
LIGHT = b"zalo 26.10.10 light, from migration 20261005110000"
NEW = b"zalo 26.10.11 with native calls"
NEWER = b"zalo 26.10.12 the App Center installed later"
REBUILT = b"zalo 26.10.11 rebuilt upstream"
USER = b"zalo the user installed"
REBUILT_NAME = "Zalo-26.10.11+ZaDark-26.2.1-abc1234-x86_64.AppImage"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


PIN = zalo_update.Pin("VN-Linux-Family/zalo-for-linux", "26.10.11", migration.PIN.asset, sha(NEW), len(NEW),
                      (sha(LIGHT),))


class FakeGitHub:
    """Serves release URLs from a dict; a missing URL is a 404 and an exception value is raised."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def __call__(self, url: str, accept: str | None = None):
        self.requests.append(url)
        value = self.responses.get(url)
        if value is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value)


class PinTests(unittest.TestCase):
    def test_the_migration_pins_the_zadark_x86_64_build_of_26_10_11(self) -> None:
        pin = migration.PIN
        self.assertEqual(("26.10.11", 167082703), (pin.tag, pin.size))
        self.assertRegex(pin.sha256, r"^[0-9a-f]{64}$")
        self.assertEqual("https://github.com/VN-Linux-Family/zalo-for-linux/releases/download/26.10.11/"
                         "Zalo-26.10.11%2BZaDark-26.2.1-412e35f-x86_64.AppImage", pin.download_url())
        self.assertTrue(pin.rebuilt_pattern.fullmatch(pin.asset))
        self.assertTrue(pin.rebuilt_pattern.fullmatch(REBUILT_NAME))
        self.assertTrue(pin.rebuilt_pattern.fullmatch("Zalo-26.10.11+ZaDark-26.2.1-285ba91-x86_64.AppImage"))
        for other in ("Zalo-26.10.11-Original-412e35f-x86_64.AppImage",
                      "Zalo-26.10.11+ZaDark-26.2.1-412e35f-aarch64.AppImage",
                      "Zalo-26.10.11+ZaDark-26.2.1-412e35f-x86_64.AppImage.zsync",
                      "Zalo-26.10.10+ZaDark-26.2.1-e80931a-Full-x86_64.AppImage"):
            self.assertFalse(pin.rebuilt_pattern.fullmatch(other), other)

    def test_actions(self) -> None:
        cases = [
            ((None, {"sha256": sha(FULL), "flavour": "full"}), "absent"),   # removed: not put back
            ((sha(NEW), {}), "current"),
            ((sha(REBUILT), {"tag": "26.10.11", "sha256": sha(REBUILT)}), "current"),
            ((sha(NEWER), {"tag": "26.10.12", "sha256": sha(NEWER), "flavour": "full"}), "current"),   # no downgrade
            ((sha(FULL), {"tag": "26.10.10", "sha256": sha(FULL), "flavour": "full"}), "replace"),
            ((sha(LIGHT), {"tag": "26.10.10", "sha256": sha(LIGHT)}), "replace"),
            ((sha(LIGHT), {}), "replace"),
            ((sha(USER), {"tag": "26.10.10", "sha256": sha(FULL)}), "user"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(expected, zalo_update.plan_action(*args, PIN))

    def test_manifest(self) -> None:
        manifest = json.loads(Path(migration.__file__).with_name("manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(2, manifest["schema"])
        for key in ("title", "summary", "size"):
            self.assertTrue(manifest[key] and manifest[f"{key}_en"], key)
        self.assertEqual(len(manifest["release_notes_vi"]), len(manifest["release_notes_en"]))


class ZaloTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.bin = root / "usr/local/bin"
        self.bin.mkdir(parents=True)
        self.appimage = self.bin / "Zalo.AppImage"
        self.state = root / "var/lib/caramos-ota/zalo-appimage.json"
        self.pending = root / "var/lib/caramos-ota/zalo-update.json"
        self.api_url = f"https://api.github.com/repos/{PIN.repository}/releases/tags/{PIN.tag}"
        self.github = FakeGitHub({PIN.download_url(): NEW})
        self.missing: list[str] = []
        self.started: list[str] = []
        for name, value in {
            "ZALO_APPIMAGE": self.appimage,
            "STATE_FILE": self.state,
            "PENDING_FILE": self.pending,
            "_open": self.github,
            "missing_packages": lambda: list(self.missing),
        }.items():
            patcher = patch.object(zalo_update, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (patch.object(zalo_update.time, "sleep"), patch("caramos_ota_update.context.log_info"),
                        patch.object(zalo_update, "log_error"), patch("builtins.print")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, data: bytes, state: dict | None = None) -> None:
        self.appimage.write_bytes(data)
        self.appimage.chmod(0o755)
        if state is not None:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            self.state.write_text(json.dumps(state), encoding="utf-8")

    def schedule(self, dry_run: bool = False) -> str:
        with patch.object(zalo_update, "start_background", side_effect=lambda context: self.started.append("unit")):
            return zalo_update.schedule(MigrationContext(dry_run=dry_run), PIN)

    def finish(self) -> str:
        return zalo_update.finish_pending(MigrationContext())

    def assert_installed(self, data: bytes, asset_name: str) -> None:
        self.assertEqual(data, self.appimage.read_bytes())
        self.assertEqual(0o755, self.appimage.stat().st_mode & 0o7777)
        self.assertEqual({"repository": "VN-Linux-Family/zalo-for-linux", "tag": "26.10.11", "asset": asset_name,
                          "sha256": sha(data), "flavour": "full"},
                         json.loads(self.state.read_text(encoding="utf-8")))
        self.assertEqual(["Zalo.AppImage"], sorted(path.name for path in self.bin.iterdir()))


class ScheduleTests(ZaloTestCase):
    def test_the_migration_downloads_nothing_itself(self) -> None:
        self.install(FULL, {"tag": "26.10.10", "sha256": sha(FULL), "flavour": "full"})
        with patch.object(zalo_update, "start_background", side_effect=lambda context: self.started.append("unit")):
            migration.run(MigrationContext())
        self.assertEqual([], self.github.requests)
        self.assertEqual(FULL, self.appimage.read_bytes())
        self.assertEqual(["unit"], self.started)
        self.assertEqual(migration.PIN.sha256, json.loads(self.pending.read_text(encoding="utf-8"))["sha256"])

    def test_the_background_unit_then_installs_the_pinned_build(self) -> None:
        self.install(FULL, {"tag": "26.10.10", "sha256": sha(FULL), "flavour": "full"})
        self.assertEqual("replace", self.schedule())
        self.assertEqual("installed", self.finish())
        self.assert_installed(NEW, PIN.asset)
        self.assertFalse(self.pending.exists())

    def test_installs_missing_call_packages_and_carries_on_when_apt_fails(self) -> None:
        self.install(LIGHT, {"tag": "26.10.10", "sha256": sha(LIGHT)})
        self.missing = ["libopus0", "pulseaudio-utils"]
        with patch.object(MigrationContext, "apt_install", side_effect=MigrationContextError("apt failed")) as apt:
            self.assertEqual("replace", self.schedule())
        apt.assert_called_once_with(["libopus0", "pulseaudio-utils"], recommends=False)
        self.assertTrue(self.pending.exists())

    def test_a_computer_without_zalo_or_with_its_own_gets_nothing(self) -> None:
        self.assertEqual("absent", self.schedule())
        self.install(USER, {"tag": "26.10.10", "sha256": sha(FULL), "flavour": "full"})
        self.assertEqual("user", self.schedule())
        self.assertFalse(self.pending.exists())
        self.assertEqual([], self.started)
        self.assertEqual(USER, self.appimage.read_bytes())

    def test_the_build_the_app_center_installed_is_kept(self) -> None:
        state = {"repository": PIN.repository, "tag": "26.10.11", "asset": PIN.asset, "sha256": sha(NEW), "flavour": "full"}
        self.install(NEW, state)
        self.assertEqual("current", self.schedule())
        self.assert_installed(NEW, PIN.asset)
        newer = {"repository": PIN.repository, "tag": "26.10.12", "asset": "Zalo-26.10.12+ZaDark-26.2.1-1234567-x86_64.AppImage",
                 "sha256": sha(NEWER), "flavour": "full"}
        self.install(NEWER, newer)
        self.assertEqual("current", self.schedule())
        self.assertEqual(newer, json.loads(self.state.read_text(encoding="utf-8")))
        self.assertFalse(self.pending.exists())

    def test_the_new_build_without_a_flavour_gets_one(self) -> None:
        self.install(NEW, {"tag": "26.10.11", "sha256": sha(NEW)})
        self.assertEqual("current", self.schedule())
        self.assert_installed(NEW, PIN.asset)

    def test_dry_run_changes_nothing(self) -> None:
        self.install(FULL, {"tag": "26.10.10", "sha256": sha(FULL), "flavour": "full"})
        self.assertEqual("replace", self.schedule(dry_run=True))
        self.assertFalse(self.pending.exists())
        self.assertEqual([], self.started)


class FinishPendingTests(ZaloTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.install(FULL, {"tag": "26.10.10", "sha256": sha(FULL), "flavour": "full"})
        zalo_update.save_pending(PIN)

    def test_a_failed_download_keeps_the_old_zalo_and_waits_for_the_next_check(self) -> None:
        self.github.responses = {PIN.download_url(): urllib.error.URLError("Network is unreachable")}
        self.assertEqual("failed", self.finish())
        self.assertEqual(FULL, self.appimage.read_bytes())
        self.assertTrue(self.pending.exists())
        self.assertEqual(["Zalo.AppImage"], sorted(path.name for path in self.bin.iterdir()))
        # The connection is back.
        self.github.responses = {PIN.download_url(): NEW}
        self.assertEqual("installed", self.finish())
        self.assert_installed(NEW, PIN.asset)

    def test_a_corrupt_download_is_not_installed(self) -> None:
        self.github.responses = {PIN.download_url(): b"truncated"}
        self.assertEqual("failed", self.finish())
        self.assertEqual(FULL, self.appimage.read_bytes())
        self.assertEqual(["Zalo.AppImage"], sorted(path.name for path in self.bin.iterdir()))

    def test_not_enough_disk_space_waits_too(self) -> None:
        with patch.object(zalo_update.shutil, "disk_usage", return_value=SimpleNamespace(free=1024)):
            self.assertEqual("failed", self.finish())
        self.assertEqual([], self.github.requests)
        self.assertTrue(self.pending.exists())

    def test_uses_the_rebuilt_asset_of_the_tag_when_the_pinned_one_is_gone(self) -> None:
        rebuilt_url = PIN.download_url(REBUILT_NAME)
        release = {"assets": [{"name": REBUILT_NAME, "size": len(REBUILT), "digest": f"sha256:{sha(REBUILT)}",
                               "browser_download_url": rebuilt_url}]}
        self.github.responses = {self.api_url: json.dumps(release).encode(), rebuilt_url: REBUILT}
        self.assertEqual("installed", self.finish())
        self.assert_installed(REBUILT, REBUILT_NAME)

    def test_zalo_removed_or_replaced_meanwhile_is_left_alone(self) -> None:
        self.appimage.unlink()
        self.assertEqual("dropped", self.finish())
        self.assertFalse(self.pending.exists())
        zalo_update.save_pending(PIN)
        self.install(USER)
        self.assertEqual("dropped", self.finish())
        self.assertEqual(USER, self.appimage.read_bytes())
        self.assertEqual([], self.github.requests)

    def test_gives_up_after_a_month(self) -> None:
        zalo_update.save_pending(PIN, since=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat())
        self.assertEqual("expired", self.finish())
        self.assertFalse(self.pending.exists())
        self.assertEqual(FULL, self.appimage.read_bytes())

    def test_a_damaged_pending_file_is_dropped(self) -> None:
        self.pending.write_text('{"tag": "26.10.11", "asset": "../../etc/passwd"}', encoding="utf-8")
        self.assertEqual("none", self.finish())
        self.assertFalse(self.pending.exists())


class BackgroundTests(ZaloTestCase):
    def test_the_unit_is_started_through_systemd_run(self) -> None:
        calls = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 3 if command[0] == "systemctl" else 0, "", "")

        with patch.object(zalo_update.subprocess, "run", side_effect=fake_run):
            self.assertTrue(zalo_update.start_background(MigrationContext()))
        self.assertEqual(["systemd-run", "--no-block"], calls[1][:2])
        self.assertIn("--unit=caramos-zalo-update", calls[1])
        self.assertEqual(["/usr/bin/python3", "-I", "-m", "caramos_ota_update.zalo_update"], calls[1][-4:])

    def test_a_running_unit_is_not_started_twice(self) -> None:
        with patch.object(zalo_update.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertTrue(zalo_update.start_background(MigrationContext()))
        self.assertEqual(1, run.call_count)

    def test_without_systemd_the_daily_check_takes_over(self) -> None:
        with patch.object(zalo_update.subprocess, "run", side_effect=FileNotFoundError("systemctl")):
            self.assertFalse(zalo_update.start_background(MigrationContext()))

    def test_the_daily_check_resumes_only_a_waiting_download_and_never_fails(self) -> None:
        with patch.object(zalo_update, "start_background") as start:
            zalo_update.resume_in_background()
            start.assert_not_called()
            zalo_update.save_pending(PIN)
            zalo_update.resume_in_background()
            start.assert_called_once()
        with patch.object(zalo_update, "start_background", side_effect=RuntimeError("dbus")):
            zalo_update.resume_in_background()   # logged, not raised

    def test_the_check_and_the_upgrade_resume_it(self) -> None:
        cli = (ROOT / "usr/lib/python3/dist-packages/caramos_ota/cli.py").read_text(encoding="utf-8")
        self.assertEqual(2, cli.count("zalo_update.resume_in_background()"))


if __name__ == "__main__":
    unittest.main()
