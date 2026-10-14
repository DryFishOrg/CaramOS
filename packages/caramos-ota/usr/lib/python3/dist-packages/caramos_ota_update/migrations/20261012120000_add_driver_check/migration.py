"""Driver Check: hw-probe for its hardware reports.

Driver Check (caramos-driver-check, shipped in this package) runs at the first login after this update
and tells which drivers are missing and how to install them. Its "Send a hardware report" button runs
hw-probe, the tool the VNLF community asks for when someone needs driver help; this installs it when it
is missing, in ISO builds too.

hw-probe is installed without its recommended packages: tools Driver Check does not use, and sysstat,
whose timer wakes the computer every 10 minutes. A failed install does not fail the update: Driver Check
works without hw-probe and tells how to install it.
"""

from __future__ import annotations

from pathlib import Path

from caramos_ota_update.context import MigrationContext, MigrationContextError

DESCRIPTION = "Install hw-probe for Driver Check's hardware reports"

PACKAGE = "hw-probe"
COMMAND = Path("/usr/bin/hw-probe")


def run(context: MigrationContext) -> None:
    if COMMAND.exists():
        context.log(f"{PACKAGE} is already installed")
        return
    try:
        context.apt_install([PACKAGE], recommends=False)
    except MigrationContextError as exc:
        context.log(f"could not install {PACKAGE} ({exc}); Driver Check tells how to install it")
