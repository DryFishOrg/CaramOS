"""Run the fixes the Driver Check window offers. They open the Linux Mint tools made for each job."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence

from .analysis import DRIVER_MANAGER, FINGERPRINTS, REBOOT, UPDATE_MANAGER, WIFI_ON

# The first command that exists is used.
COMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
    # Driver Manager installs NVIDIA and Broadcom drivers; offline it installs from the CaramOS USB.
    DRIVER_MANAGER: (("driver-manager",), ("mintdrivers",)),
    UPDATE_MANAGER: (("mintupdate",),),
    # Asks before restarting, like the menu's Restart.
    REBOOT: (("cinnamon-session-quit", "--reboot"),),
    WIFI_ON: (("nmcli", "radio", "wifi", "on"),),
    # Fingwit, Linux Mint's fingerprint setup.
    FINGERPRINTS: (("fingwit",),),
}


def command_for(kind: str, which=shutil.which) -> tuple[str, ...] | None:
    for command in COMMANDS.get(kind, ()):
        if which(command[0]):
            return command
    return None


def available(kind: str) -> bool:
    return command_for(kind) is not None


def run(kind: str, popen=subprocess.Popen) -> bool:
    """Start the tool for an action without waiting; False when it is not installed."""

    command = command_for(kind)
    if command is None:
        return False
    try:
        popen(list(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              start_new_session=True)
    except OSError:
        return False
    return True


def open_url(url: str, popen=subprocess.Popen) -> bool:
    try:
        popen(["xdg-open", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              start_new_session=True)
    except OSError:
        return False
    return True


def open_path(path: str, popen=subprocess.Popen) -> bool:
    return open_url(path, popen)


def commands_for(kinds: Sequence[str]) -> dict[str, bool]:
    return {kind: available(kind) for kind in kinds}
