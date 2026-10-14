"""What CaramOS knows about specific hardware beyond what the running kernel says.

Kept small on purpose: whether the kernel has a driver for a device comes from the device's modalias at
run time (hardware.SystemReader.modules_for), which stays right as kernels change. Listed here is only
what the kernel cannot tell: hardware that needs a driver from outside the kernel, cameras that need more
than a kernel driver, and which fingerprint readers libfprint supports.
"""

from __future__ import annotations

import re
from pathlib import Path

from .hardware import Device

LIBFPRINT_HWDB = "usr/lib/udev/hwdb.d/60-autosuspend-libfprint-2.hwdb"

BROADCOM = "14e4"
NVIDIA = "10de"
INTEL = "8086"

# Intel IPU6/IPU7 (drivers/media/pci/intel/ipu6): the image processor behind the MIPI cameras of many
# laptops since 2021. The kernel driver alone does not give apps a webcam.
INTEL_IPU = frozenset({
    "9a19",  # Tiger Lake
    "4e19",  # Jasper Lake
    "465d",  # Alder Lake-P
    "462e",  # Alder Lake-N
    "a75d",  # Raptor Lake-P
    "7d19",  # Meteor Lake
    "645d",  # Lunar Lake (IPU7)
    "b05d",  # Panther Lake (IPU7.5)
})

_IPU_NAME = re.compile(r"\bIPU\b|Imaging Signal Processor|Image Processing Unit", re.IGNORECASE)

# USB vendors whose devices in laptops are fingerprint readers (touchpads are I2C or PS/2, not USB).
FINGERPRINT_VENDORS = frozenset({"27c6", "06cb", "138a", "2808", "1c7a", "10a5", "298d", "2541", "08ff", "147e"})
ELAN = "04f3"   # ELAN fingerprint readers are 04f3:0cxx; other ELAN USB devices are touchscreens


def broadcom_wifi(device: Device) -> bool:
    """Broadcom Wi-Fi: needs Broadcom's driver (bcmwl) or downloaded b43 firmware, both outside the kernel."""

    return device.bus == "pci" and device.vendor == BROADCOM and device.pci_class.startswith("0280")


def nvidia_gpu(device: Device) -> bool:
    return device.bus == "pci" and device.vendor == NVIDIA and device.pci_class.startswith(("0300", "0302", "0380"))


def intel_ipu(device: Device) -> bool:
    if device.bus != "pci" or device.vendor != INTEL or not device.pci_class.startswith("0480"):
        return False
    return device.product in INTEL_IPU or bool(_IPU_NAME.search(device.name))


def fingerprint_like(device: Device) -> bool:
    """A USB device that is very likely a fingerprint reader, whether or not libfprint knows it."""

    if device.bus != "usb":
        return False
    if device.vendor in FINGERPRINT_VENDORS:
        return True
    return device.vendor == ELAN and device.product.startswith("0c")


def read_libfprint_support(root: str | Path = "/") -> dict[str, str]:
    """{"04f3:0c7e": "elanmoc", "27c6:5395": ""}: the libfprint driver of each known reader, "" if unsupported.

    libfprint's hwdb lists the supported readers under "# Supported by libfprint driver <name>" and the
    readers it knows it cannot drive under "# Known unsupported devices".
    """

    try:
        text = (Path(root) / LIBFPRINT_HWDB).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    support: dict[str, str] = {}
    driver: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("# Supported by libfprint driver "):
            driver = line.rsplit(" ", 1)[1]
        elif line.lower().startswith("# known unsupported"):
            driver = ""
        elif line.startswith("usb:v") and driver is not None and len(line) >= 14:
            # usb:v04F3p0C7E*
            vendor, product = line[5:9].lower(), line[10:14].lower()
            support.setdefault(f"{vendor}:{product}", driver)
    return support
