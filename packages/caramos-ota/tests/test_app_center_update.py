"""The App Center comes from signed GitHub releases, verified before anything runs as root."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caramos_ota import app_center

ROOT = Path(__file__).parents[1]
SHIPPED_KEYRING = ROOT / "usr/share/caramos-ota/keys/caramos-app-center-release.gpg"


def _gpg(home: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, GNUPGHOME=str(home))
    return subprocess.run(["gpg", "--batch", "--quiet", *args], input=data, env=env, check=True, capture_output=True)


@unittest.skipUnless(shutil.which("gpg") and shutil.which("gpgv"), "gpg and gpgv are needed")
class AppCenterUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.keys = {}
        for name in ("release", "attacker"):
            home = root / name
            home.mkdir(mode=0o700)
            _gpg(home, "--passphrase", "", "--quick-gen-key", f"{name} <{name}@example.org>", "ed25519", "sign", "never")
            keyring = root / f"{name}.gpg"
            keyring.write_bytes(_gpg(home, "--export").stdout)
            cls.keys[name] = (home, keyring)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.installed = "0.1.0"
        self.apt_calls: list[list[str]] = []
        self.fetched: list[str] = []
        self.deb = b"!<arch>\nnew App Center\n"
        self.set_release("0.2.0")
        patches = [
            patch.object(app_center, "installed_version", lambda _package: self.installed),
            patch.object(app_center, "run_command", self.fake_run),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_run(self, args, **_kwargs):
        if args[:2] == ["dpkg", "--compare-versions"]:
            newer = tuple(int(part) for part in args[2].split(".")) > tuple(int(part) for part in args[4].split("."))
            return subprocess.CompletedProcess(args, 0 if newer else 1)
        self.apt_calls.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    def set_release(self, version: str, *, signer: str = "release", sha256: str | None = None, **extra) -> None:
        manifest = {"package": "caramos-app-center", "version": version,
                    "file": f"caramos-app-center_{version}_all.deb",
                    "sha256": sha256 or hashlib.sha256(self.deb).hexdigest(), **extra}
        self.manifest = json.dumps(manifest).encode()
        self.signature = _gpg(self.keys[signer][0], "--detach-sign", "--output", "-", data=self.manifest).stdout

    def fetch(self, url: str, target: Path, _limit: int) -> None:
        self.fetched.append(url)
        if url.endswith("latest.json"):
            target.write_bytes(self.manifest)
        elif url.endswith("latest.json.sig"):
            target.write_bytes(self.signature)
        else:
            target.write_bytes(self.deb)

    def update(self, **kwargs) -> str:
        return app_center.update_app_center(keyring=self.keys["release"][1], fetch=self.fetch, **kwargs)

    def test_installs_a_newer_signed_release(self) -> None:
        self.assertEqual("updated", self.update())
        self.assertEqual(
            "https://github.com/VN-Linux-Family/CaramOS-app-center/releases/download/v0.2.0/"
            "caramos-app-center_0.2.0_all.deb", self.fetched[-1])
        self.assertEqual(1, len(self.apt_calls))
        self.assertEqual(["apt-get", "install", "--yes", "--"], self.apt_calls[0][:4])
        self.assertTrue(self.apt_calls[0][4].endswith("/caramos-app-center_0.2.0_all.deb"))

    def test_same_or_older_release_downloads_no_package(self) -> None:
        for version in ("0.1.0", "0.0.9"):
            self.fetched.clear()
            self.set_release(version)
            self.assertEqual("up-to-date", self.update())
            self.assertEqual(2, len(self.fetched))
        self.assertEqual([], self.apt_calls)

    def test_refuses_a_manifest_signed_by_another_key(self) -> None:
        self.set_release("0.2.0", signer="attacker")
        with self.assertRaisesRegex(app_center.AppCenterUpdateError, "not signed"):
            self.update()
        self.assertEqual([], self.apt_calls)

    def test_refuses_a_package_that_does_not_match_the_signed_checksum(self) -> None:
        self.set_release("0.2.0", sha256="0" * 64)
        with self.assertRaisesRegex(app_center.AppCenterUpdateError, "checksum"):
            self.update()
        self.assertEqual([], self.apt_calls)

    def test_refuses_unexpected_file_names(self) -> None:
        self.manifest = json.dumps({"package": "caramos-app-center", "version": "0.2.0",
                                    "file": "../../evil.deb", "sha256": "0" * 64}).encode()
        self.signature = _gpg(self.keys["release"][0], "--detach-sign", "--output", "-", data=self.manifest).stdout
        with self.assertRaisesRegex(app_center.AppCenterUpdateError, "file name"):
            self.update()

    def test_does_nothing_when_not_installed_unless_asked(self) -> None:
        self.installed = ""
        self.assertEqual("not-installed", self.update())
        self.assertEqual([], self.fetched)
        self.assertEqual("installed", self.update(install_missing=True))

    def test_daily_check_logs_failures_instead_of_raising(self) -> None:
        def offline(url, _target, _limit):
            raise app_center.AppCenterUpdateError(f"cannot download {url}")

        with patch.object(app_center, "update_app_center", lambda: offline("x", None, 0)), \
                patch.object(app_center, "log_error") as log_error:
            app_center.update_installed_app_center()
        log_error.assert_called_once()


class PackagingTests(unittest.TestCase):
    def test_ships_the_release_key_and_no_longer_needs_the_ppa_for_the_app_center(self) -> None:
        self.assertTrue(SHIPPED_KEYRING.is_file())
        self.assertIn("usr/share/caramos-ota/keys usr/share/caramos-ota/", (ROOT / "debian/install").read_text())
        control = (ROOT / "debian/control").read_text(encoding="utf-8")
        self.assertNotIn("caramos-app-center", control)
        self.assertIn(" gpgv,", control)

    def test_daily_self_update_also_updates_the_app_center(self) -> None:
        source = (ROOT / "usr/lib/python3/dist-packages/caramos_ota/cli.py").read_text(encoding="utf-8")
        update = source.split("def self_update_ota_if_needed", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("update_installed_app_center()", update)

    @unittest.skipUnless(shutil.which("gpg"), "gpg is needed")
    def test_shipped_key_is_the_app_center_release_key(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            os.chmod(home, 0o700)
            listing = subprocess.run(["gpg", "--batch", "--with-colons", "--show-keys", str(SHIPPED_KEYRING)],
                                     env=dict(os.environ, GNUPGHOME=home), check=True, capture_output=True,
                                     text=True).stdout
        self.assertIn("9326538C708A3A6533D98EAFCC6853D8DA2155B6", listing)
        self.assertIn("CaramOS App Center release", listing)


if __name__ == "__main__":
    unittest.main()
