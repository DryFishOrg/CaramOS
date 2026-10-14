"""Hardware reports with hw-probe, the tool the VNLF community asks for when someone needs driver help.

`hw-probe -all -upload` collects the hardware list and driver logs and uploads them to linux-hardware.org,
which answers with a link to share on the VNLF site, Facebook group, Telegram or Discord. Offline,
`-save DIR` writes the same report to a file to upload later. hw-probe has to run as root; pkexec asks
for the password (polkit action net.vietnamlinuxfamily.caramos-ota.hw-probe).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HW_PROBE = "/usr/bin/hw-probe"
PKEXEC = "pkexec"
PKEXEC_REFUSED = (126, 127)   # the password dialog was dismissed / not authorized
TIMEOUT_SECONDS = 600
VNLF_URL = "https://vietnamlinuxfamily.net/"
GUIDE_URL = "https://vietnamlinuxfamily.net/a/cach-nhanh-nhat-e-biet-may-ban-ang-thieu-driver-nao"
UPLOAD_PAGE = "https://linux-hardware.org/?view=howto"

_PROBE_URL = re.compile(r"https?://linux-hardware\.org/\S*\?probe=\w+")
_PROBE_ID = re.compile(r"\bprobe=(\w+)")
_SAVED = re.compile(r"^Saved to: (?P<path>\S.*)$", re.MULTILINE)


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    output: str
    url: str = ""          # upload: the report on linux-hardware.org
    saved: str = ""        # save: the report file in the user's home
    refused: bool = False  # hw-probe did not run: no password


def available(path: str = HW_PROBE) -> bool:
    return Path(path).is_file()


def probe_url(output: str) -> str:
    match = _PROBE_URL.search(output)
    if match:
        return match.group(0).replace("http://", "https://", 1)
    match = _PROBE_ID.search(output)
    return f"https://linux-hardware.org/?probe={match.group(1)}" if match else ""


def saved_file(output: str) -> str:
    match = _SAVED.search(output)
    return match.group("path").strip() if match else ""


def default_save_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "hw-probe-log"


def _stream(command: list[str], on_line: Callable[[str], None]) -> tuple[int, str]:
    lines: list[str] = []
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                          errors="replace") as process:
        assert process.stdout is not None
        timer = threading.Timer(TIMEOUT_SECONDS, process.kill)
        timer.start()
        try:
            for raw in process.stdout:
                line = raw.rstrip("\n")
                lines.append(line)
                on_line(line)
            code = process.wait()
        finally:
            timer.cancel()
    return code, "\n".join(lines)


def upload(on_line: Callable[[str], None]) -> ProbeResult:
    """Probe and upload; the result carries the link to share."""

    try:
        code, output = _stream([PKEXEC, HW_PROBE, "-all", "-upload"], on_line)
    except OSError as exc:
        return ProbeResult(False, str(exc))
    url = probe_url(output)
    return ProbeResult(code == 0 and bool(url), output, url=url, refused=code in PKEXEC_REFUSED)


def save(on_line: Callable[[str], None], target_dir: Path | None = None) -> ProbeResult:
    """Probe without internet; the report is copied to ~/hw-probe-log so the user owns it."""

    target_dir = target_dir or default_save_dir()
    with tempfile.TemporaryDirectory(prefix="caramos-hw-probe-") as scratch:
        try:
            code, output = _stream([PKEXEC, HW_PROBE, "-all", "-save", scratch], on_line)
        except OSError as exc:
            return ProbeResult(False, str(exc))
        written = saved_file(output) or str(Path(scratch) / "hw.info.txz")
        if code != 0 or not Path(written).is_file():
            return ProbeResult(False, output, refused=code in PKEXEC_REFUSED)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"hw-probe-{datetime.now():%Y%m%d-%H%M%S}.txz"
        # hw-probe ran as root; a copy belongs to the user. The root-owned original goes with the
        # temporary directory, which is the user's.
        shutil.copyfile(written, target)
        return ProbeResult(True, output, saved=str(target))
