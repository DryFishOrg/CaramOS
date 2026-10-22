"""Migration 20261022120000_start_fcitx_at_login: Fcitx 5 starts at every login, also where Cinnamon runs the
CaramOS Lotus login helper instead of org.fcitx.Fcitx5.desktop (both provide "inputmethod"). Before, a live
session and new installs whose helper came first in /etc/xdg/autostart had no Fcitx, so no Vietnamese."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from caramos_ota_update.context import MigrationContext

migration = importlib.import_module("caramos_ota_update.migrations.20261022120000_start_fcitx_at_login.migration")
lighten = importlib.import_module("caramos_ota_update.migrations.20261010120100_lighten_desktop_session.migration")

FAKE_TOOL = """#!/bin/sh
echo "$(basename "$0") $*" >> "$CALLS"
[ "$(basename "$0")" = pgrep ] && exit "$PGREP_STATUS"
exit 0
"""


@unittest.skipUnless(shutil.which("sh") and shutil.which("cmp"), "sh and cmp are needed")
class HelperScriptTests(unittest.TestCase):
    """The login helper itself, with /etc/skel and the tools it calls replaced."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.skel = self.root / "skel"
        self.home = self.root / "home"
        for base in (self.skel, self.home):
            (base / ".config/fcitx5/conf").mkdir(parents=True)
            (base / ".config/environment.d").mkdir(parents=True)
            (base / ".config/fcitx5/config").write_text("[Behavior]\nActiveByDefault=True\n", encoding="utf-8")
            (base / ".config/fcitx5/profile").write_text("[Groups/0]\nDefaultIM=lotus\n", encoding="utf-8")
            (base / ".config/fcitx5/conf/lotus.conf").write_text('Mode="Preedit"\n', encoding="utf-8")
            (base / ".config/environment.d/90-fcitx5-lotus.conf").write_text("GTK_IM_MODULE=fcitx\n",
                                                                             encoding="utf-8")
        tools = self.root / "bin"
        tools.mkdir()
        for name in ("fcitx5", "pgrep", "killall", "ibus"):
            (tools / name).write_text(FAKE_TOOL, encoding="utf-8")
            (tools / name).chmod(0o755)
        self.script = self.root / "helper"
        self.script.write_text(migration.LOGIN_HELPER_SCRIPT.replace("/etc/skel", str(self.skel)), encoding="utf-8")
        self.script.chmod(0o755)
        self.calls = self.root / "calls"
        self.env = {**os.environ, "HOME": str(self.home), "CALLS": str(self.calls),
                    "PATH": f"{tools}:{os.environ.get('PATH', '/usr/bin:/bin')}"}

    def run_helper(self, pgrep_status: int) -> list[str]:
        subprocess.run([str(self.script)], check=True, env={**self.env, "PGREP_STATUS": str(pgrep_status)},
                       timeout=30)
        return [line for line in self.calls.read_text(encoding="utf-8").splitlines() if line.startswith("fcitx5")]

    def test_starts_fcitx_when_nothing_else_did(self) -> None:
        # A live session or a new install: the files are the /etc/skel ones and Fcitx is not running.
        self.assertEqual(["fcitx5 -d"], self.run_helper(pgrep_status=1))

    def test_leaves_a_running_fcitx_alone(self) -> None:
        self.assertEqual([], self.run_helper(pgrep_status=0))

    def test_restarts_fcitx_when_a_default_changed(self) -> None:
        # Fcitx rewrites its profile when it quits, so this is the usual case on a computer used before.
        (self.home / ".config/fcitx5/profile").write_text("[Groups/0]\nDefaultIM=keyboard-us\n", encoding="utf-8")
        self.assertEqual(["fcitx5 -d --replace"], self.run_helper(pgrep_status=0))
        self.assertEqual("[Groups/0]\nDefaultIM=lotus\n",
                         (self.home / ".config/fcitx5/profile").read_text(encoding="utf-8"))


class HelperFileTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "caramos-fcitx5-lotus-enable"
        self.context = MigrationContext()

    def test_the_helper_of_the_lighter_session_is_replaced(self) -> None:
        self.path.write_text(lighten.LOTUS_LOGIN_HELPER_SCRIPT, encoding="utf-8")
        self.path.chmod(0o755)
        self.assertTrue(migration._write_helper(self.context, self.path))
        self.assertEqual(migration.LOGIN_HELPER_SCRIPT, self.path.read_text(encoding="utf-8"))
        self.assertEqual(0o755, self.path.stat().st_mode & 0o777)
        self.assertFalse(migration._write_helper(self.context, self.path))   # already done

    def test_a_script_that_is_not_the_caramos_helper_stays(self) -> None:
        self.path.write_text("#!/bin/sh\nexec fcitx5 -d\n", encoding="utf-8")
        self.assertFalse(migration._write_helper(self.context, self.path))
        self.assertEqual("#!/bin/sh\nexec fcitx5 -d\n", self.path.read_text(encoding="utf-8"))

    def test_a_missing_helper_or_a_symlink_stays(self) -> None:
        self.assertFalse(migration._write_helper(self.context, self.path))
        target = self.path.with_name("target")
        target.write_text(lighten.LOTUS_LOGIN_HELPER_SCRIPT, encoding="utf-8")
        self.path.symlink_to(target)
        self.assertFalse(migration._write_helper(self.context, self.path))
        self.assertEqual(lighten.LOTUS_LOGIN_HELPER_SCRIPT, target.read_text(encoding="utf-8"))


class SessionTests(unittest.TestCase):
    def fake_proc(self, folder: str, processes: dict[str, tuple[str, dict[str, str]]]) -> Path:
        proc = Path(folder)
        for pid, (comm, variables) in processes.items():
            (proc / pid).mkdir()
            (proc / pid / "comm").write_text(comm + "\n", encoding="utf-8")
            (proc / pid / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in variables.items()) + b"\0")
        (proc / "self").mkdir()
        return proc

    def test_the_environment_comes_from_the_running_cinnamon(self) -> None:
        session = {"DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                   "XAUTHORITY": "/home/minh/.Xauthority", "LANG": "vi_VN.UTF-8", "LC_TIME": "vi_VN.UTF-8",
                   "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh", "GTK_IM_MODULE": "fcitx"}
        with tempfile.TemporaryDirectory() as folder:
            proc = self.fake_proc(folder, {"42": ("bash", {"DISPLAY": ":9"}), "77": ("cinnamon", session)})
            found = migration.session_environment(os.getuid(), proc)
            self.assertIsNone(migration.session_environment(os.getuid() + 1, proc))
        self.assertEqual({"DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                          "XAUTHORITY": "/home/minh/.Xauthority", "LANG": "vi_VN.UTF-8", "LC_TIME": "vi_VN.UTF-8"},
                         found)

    def test_no_session_without_a_display(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            proc = self.fake_proc(folder, {"77": ("cinnamon-sessio", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"})})
            self.assertIsNone(migration.session_environment(os.getuid(), proc))

    def test_fcitx_starts_as_the_user_in_their_session(self) -> None:
        user = SimpleNamespace(pw_name="minh", pw_uid=1000)
        session = {"DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
        context = MigrationContext()
        with mock.patch.object(migration, "_desktop_users", return_value=[user]), \
                mock.patch.object(migration, "session_environment", return_value=session), \
                mock.patch.object(migration, "_fcitx_running", side_effect=[False, True]), \
                mock.patch.object(migration.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            migration._start_in_sessions(context)
            migration._start_in_sessions(context)   # now it runs: nothing more
        run.assert_called_once()
        self.assertEqual(["runuser", "-u", "minh", "--", "env", "-i", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                          "DISPLAY=:0", "fcitx5", "-d"], run.call_args.args[0])

    def test_nobody_logged_in_means_nothing_to_start(self) -> None:
        user = SimpleNamespace(pw_name="minh", pw_uid=1000)
        with mock.patch.object(migration, "_desktop_users", return_value=[user]), \
                mock.patch.object(migration, "session_environment", return_value=None), \
                mock.patch.object(migration.subprocess, "run") as run:
            migration._start_in_sessions(MigrationContext())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
