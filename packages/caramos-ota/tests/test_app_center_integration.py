"""How caramos-ota and the ISO build tie in the App Center (repository VN-Linux-Family/CaramOS-app-center)."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from caramos_ota import apt

ROOT = Path(__file__).parents[1]
REPO = ROOT.parents[1]


class CompanionUpgradeTests(unittest.TestCase):
    def run_upgrade(self, installed: str, returncode: int = 0) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, returncode, "", "")

        with patch.object(apt, "installed_version", return_value=installed), \
                patch.object(apt, "run_command", side_effect=fake_run), patch.object(apt, "log_error"):
            apt.upgrade_companion_packages()
        return calls

    def test_upgrades_the_app_center_only_when_installed(self) -> None:
        self.assertEqual([["apt-get", "install", "--only-upgrade", "--yes", "--", "caramos-app-center"]],
                         self.run_upgrade("0.1.0"))
        self.assertEqual([], self.run_upgrade(""))

    def test_a_failed_upgrade_does_not_raise(self) -> None:
        self.run_upgrade("0.1.0", returncode=100)

    def test_self_update_runs_the_companion_upgrade(self) -> None:
        source = (ROOT / "usr/lib/python3/dist-packages/caramos_ota/cli.py").read_text(encoding="utf-8")
        update = source.split("def self_update_ota_if_needed", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("upgrade_companion_packages()", update)

    def test_package_recommends_the_app_center(self) -> None:
        control = (ROOT / "debian/control").read_text(encoding="utf-8")
        self.assertIn("Recommends: caramos-app-center", control)
        self.assertNotIn("caramos_app_center", (ROOT / "debian/install").read_text(encoding="utf-8"))


@unittest.skipUnless((REPO / "config").is_dir(), "ISO tree is not in the source package")
class IsoIntegrationTests(unittest.TestCase):
    def test_submodule_points_at_the_app_center_repository(self) -> None:
        gitmodules = (REPO / ".gitmodules").read_text(encoding="utf-8")
        self.assertIn("path = packages/caramos-app-center", gitmodules)
        self.assertIn("url = https://github.com/VN-Linux-Family/CaramOS-app-center.git", gitmodules)

    def test_iso_build_installs_the_app_center_next_to_caramos_ota(self) -> None:
        bootstrap = (REPO / "scripts/ota_bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn('make -C "$app_dir" build', bootstrap)
        self.assertIn("install -y /tmp/caramos-ota-local.deb /tmp/caramos-app-center-local.deb", bootstrap)
        self.assertIn("git submodule update --init packages/caramos-app-center", bootstrap)

    def test_iso_enables_the_first_run_picker(self) -> None:
        self.assertTrue((REPO / "config/includes.chroot/etc/caramos-app-center/first-run").is_file())

    def test_ci_checks_out_the_submodule(self) -> None:
        workflow = (REPO / ".github/workflows/build.yml").read_text(encoding="utf-8")
        self.assertIn("submodules: true", workflow)

    def test_wps_and_chrome_are_no_longer_preinstalled(self) -> None:
        self.assertFalse((REPO / "config/hooks/live/0250-wps-office.hook.chroot").exists())
        hook = (REPO / "config/hooks/live/0100-caramos-setup.hook.chroot").read_text(encoding="utf-8")
        self.assertNotIn("google-chrome-stable_current_amd64.deb", hook)


if __name__ == "__main__":
    unittest.main()
