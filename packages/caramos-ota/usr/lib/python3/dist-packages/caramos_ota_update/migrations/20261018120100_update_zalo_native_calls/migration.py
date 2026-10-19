"""Update the Zalo CaramOS installed to 26.10.11, which makes calls without Wine and opens quickly.

Zalo for Linux (VN-Linux-Family/zalo-for-linux) came in two builds until 26.10.10: a light one without calls,
which CaramOS 1.0.x installed and migration 20261005110000 updated, and a "Full" one that ran its calls
through a bundled Wine, which the App Center installed. The Full build takes a long time to open (ticket
H-1042). Since 26.10.11 every build makes voice and video calls with its own Linux engine and the Full
build is no longer published, which also broke installing Zalo from App Center 0.2.1 (ticket H-1039).

This replaces either build with the ZaDark x86_64 build of 26.10.11 when CaramOS or the App Center put it
there, and installs the call engine's packages (python3, libopus0, pulseaudio-utils, which CaramOS has) if
they were removed. The download (about 170 MB) runs in the background after the update, and the daily check
tries again until it works (caramos_ota_update.zalo_update), so a slow or missing connection neither fails
nor holds up the update. A Zalo the user put at the CaramOS path stays as it is, and a computer without
Zalo gets nothing.
"""

from __future__ import annotations

from caramos_ota_update import zalo_update
from caramos_ota_update.context import MigrationContext

DESCRIPTION = "Update Zalo to the 26.10.11 build that makes calls without Wine"

PIN = zalo_update.Pin(
    repository="VN-Linux-Family/zalo-for-linux",
    tag="26.10.11",
    # Upstream rebuilt the tag on 18 Oct (glibc 2.35 build container); the first build, 285ba91, is gone.
    asset="Zalo-26.10.11+ZaDark-26.2.1-412e35f-x86_64.AppImage",
    sha256="9bf18f28770d1ff9faade4c32416c56b825f81966edddefde51a6c51261d8174",
    size=167082703,
    # sha256 of AppImages CaramOS installed, for a machine whose state file is missing.
    known_builds=(
        "9e1c58db3d4c373ef87c578bacb448eab92afe4d6d4d892ecf36f26ea8362983",   # 26.5.10 (hthienloc, ISOs up to 1.0.16)
        "2cb85a26e77cc0e580743d5a20fffffce83f70e1ed81cb88238424ac4115b71d",   # 26.5.10 (VN-Linux-Family rebuild)
        "b57e5e23eff15c25263dab604a2881068e17a57999a57e172e23e176cac00c0e",   # 26.10.10 light (migration 20261005110000)
        "99b5eab7dbe5100b9e7c4aafb2f003514fbc0f08d3e779fb9451a0ba77d33fa7",   # 26.10.10 light (rebuild e80931a)
        "d298a0f0d5977cb641e1a36d4cd551e484782f5adec34622a17942015ac9ed9c",   # 26.10.10 Full (App Center)
    ),
)


def run(context: MigrationContext) -> None:
    zalo_update.schedule(context, PIN)
