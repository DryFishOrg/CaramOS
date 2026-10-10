"""How the ISO build ties in the App Center (repository VN-Linux-Family/CaramOS-app-center).

Updates from its GitHub releases are tested in test_app_center_update.py.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
REPO = ROOT.parents[1]


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

    def test_new_users_get_the_app_center_instead_of_the_mint_welcome_screen(self) -> None:
        # mintwelcome-launcher skips users with this flag (the "show at startup" checkbox writes it);
        # the package stays installed because mint-meta-core depends on it.
        self.assertTrue((REPO / "config/includes.chroot/etc/skel/.linuxmint/mintwelcome/norun.flag").is_file())

    def test_ci_checks_out_the_submodule(self) -> None:
        workflow = (REPO / ".github/workflows/build.yml").read_text(encoding="utf-8")
        self.assertIn("submodules: true", workflow)

    def test_wps_and_chrome_are_no_longer_preinstalled(self) -> None:
        self.assertFalse((REPO / "config/hooks/live/0250-wps-office.hook.chroot").exists())
        hook = (REPO / "config/hooks/live/0100-caramos-setup.hook.chroot").read_text(encoding="utf-8")
        self.assertNotIn("google-chrome-stable_current_amd64.deb", hook)
        # The Desktop shortcut would point at a browser that is not installed; the App Center launcher
        # takes its place (migration 20261009120000_add_app_center_launcher adds it to /etc/skel).
        self.assertFalse((REPO / "config/includes.chroot/etc/skel/Desktop/google-chrome.desktop").exists())


if __name__ == "__main__":
    unittest.main()
