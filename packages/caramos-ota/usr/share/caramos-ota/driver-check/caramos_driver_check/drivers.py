"""Driver packages Ubuntu recommends for this computer, the same list Driver Manager shows.

UbuntuDrivers (ubuntu-drivers-common) matches each device's modalias against the Modaliases field of the
driver packages in the APT lists, and marks one NVIDIA driver as recommended. This runs as the user and
takes a second or two (it loads the APT cache), so the window does it while it shows the scan.

Only drivers Ubuntu itself installs automatically are offered for one-click install (NVIDIA, Broadcom
Wi-Fi, VirtualBox and VMware guest tools); anything else stays with Driver Manager.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from .hardware import PCI_ADDRESS, USB_DEVICE

# Ubuntu's own auto-install list (UbuntuDrivers.detect.auto_install_filter), without the OEM archives.
ONE_CLICK = ("nvidia-driver-*", "bcmwl*", "broadcom-sta-dkms", "virtualbox-guest*", "open-vm-tools*")
# Packages the CaramOS USB stick carries: Driver Check can install them without internet.
ON_INSTALL_MEDIA = ("bcmwl-kernel-source", "broadcom-sta-dkms")
PREFERRED = ("bcmwl-kernel-source", "broadcom-sta-dkms")
NVIDIA_DRIVER = re.compile(r"^nvidia-driver-(?P<flavour>\d+(?:-server)?(?:-open)?)$")


@dataclass(frozen=True)
class DriverProposal:
    package: str          # "nvidia-driver-580-open", "bcmwl-kernel-source"
    target: str           # what the helper installs: "nvidia:580-open" or the package name
    address: str          # the PCI address or USB device it drives ("0000:01:00.0", "1-2"), "" if unknown
    vendor: str = ""
    model: str = ""
    free: bool = False
    offline: bool = False  # can be installed from the CaramOS USB stick without internet

    @property
    def nvidia(self) -> bool:
        return self.package.startswith("nvidia-driver-")

    @property
    def version(self) -> str:
        """580 for nvidia-driver-580-open; "" for other drivers."""

        match = NVIDIA_DRIVER.match(self.package)
        return match.group("flavour").split("-")[0] if match else ""

    @property
    def open_kernel_modules(self) -> bool:
        return self.nvidia and self.package.endswith("-open")


def one_click(package: str) -> bool:
    return any(fnmatch.fnmatch(package, pattern) for pattern in ONE_CLICK)


def install_target(package: str) -> str:
    """nvidia-driver-580-open -> nvidia:580-open (ubuntu-drivers then adds the signed kernel modules)."""

    match = NVIDIA_DRIVER.match(package)
    return f"nvidia:{match.group('flavour')}" if match else package


def address_of(syspath: str) -> str:
    for part in reversed(Path(syspath).parts):
        if PCI_ADDRESS.match(part) or USB_DEVICE.match(part):
            return part
    return ""


def choose(drivers: dict[str, dict]) -> str:
    """The package to propose among a device's drivers: the recommended desktop one."""

    candidates = [name for name, info in drivers.items() if not info.get("builtin") and one_click(name)]
    # Server drivers are for headless machines; Driver Manager hides them too.
    desktop = [name for name in candidates if not re.search(r"-server(-open)?$", name)]
    recommended = [name for name in desktop if drivers[name].get("recommended")]
    if recommended:
        return recommended[0]
    if len(desktop) == 1:
        return desktop[0]
    # Ubuntu's Broadcom package first; broadcom-sta-dkms is Debian's build of the same driver.
    for name in PREFERRED:
        if name in desktop:
            return name
    nvidia = sorted((name for name in desktop if NVIDIA_DRIVER.match(name)),
                    key=lambda name: (int(NVIDIA_DRIVER.match(name).group("flavour").split("-")[0]),
                                      name.endswith("-open")))
    return nvidia[-1] if nvidia else ""


def proposals_from(devices: dict[str, dict], installed) -> list[DriverProposal]:
    """Proposals from UbuntuDrivers.detect.system_device_drivers() output; installed(package) -> bool."""

    proposals = []
    for syspath, info in sorted(devices.items()):
        if info.get("manual_install"):
            continue   # a driver installed by hand from NVIDIA's site: leave it alone
        drivers = info.get("drivers") or {}
        if any(installed(name) for name, details in drivers.items() if not details.get("builtin")):
            continue   # one of its drivers is already installed
        package = choose(drivers)
        if not package:
            continue
        proposals.append(DriverProposal(
            package=package,
            target=install_target(package),
            address=address_of(syspath),
            vendor=str(info.get("vendor") or ""),
            model=str(info.get("model") or ""),
            free=bool(drivers[package].get("free")),
            offline=package in ON_INSTALL_MEDIA,
        ))
    return proposals


def find_proposals() -> list[DriverProposal]:
    """Ask UbuntuDrivers; an empty list when it is missing or the APT cache cannot be read."""

    try:
        import apt_pkg
        from UbuntuDrivers import detect
    except ImportError:
        return []
    try:
        apt_pkg.init_config()
        apt_pkg.init_system()
        cache = apt_pkg.Cache(None)
        devices = detect.system_device_drivers(cache)
    except Exception:
        return []

    def installed(name: str) -> bool:
        try:
            return cache[name].current_ver is not None
        except KeyError:
            return False

    return proposals_from(devices, installed)
