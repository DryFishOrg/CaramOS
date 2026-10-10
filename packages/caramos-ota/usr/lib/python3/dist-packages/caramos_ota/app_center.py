"""Install and update the CaramOS App Center from its GitHub releases.

The App Center (repository VN-Linux-Family/CaramOS-app-center) is not on the PPA. Each release tag
publishes caramos-app-center_<version>_all.deb plus latest.json, which names that file with its version
and SHA-256, and latest.json.sig, a detached signature by the App Center release key. The public part of
that key ships in this package (KEYRING).

This runs as root from the daily check, so nothing is installed unless latest.json verifies against
KEYRING with gpgv and the downloaded .deb matches the signed checksum. A version that is not newer than
the installed one is never installed, so an old signed manifest cannot downgrade the App Center.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .apt import installed_version, run_command
from .logging_utils import log_error, log_info

PACKAGE = "caramos-app-center"
RELEASES_URL = "https://github.com/VN-Linux-Family/CaramOS-app-center/releases"
MANIFEST_URL = f"{RELEASES_URL}/latest/download/latest.json"
KEYRING = Path("/usr/share/caramos-ota/keys/caramos-app-center-release.gpg")

VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+~]{0,40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_LIMIT = 64 * 1024
SIGNATURE_LIMIT = 16 * 1024
DEB_LIMIT = 50 * 1024 * 1024
TIMEOUT_SECONDS = 60

Fetch = Callable[[str, Path, int], None]


class AppCenterUpdateError(RuntimeError):
    """The release could not be downloaded or did not verify."""


def download(url: str, target: Path, limit: int) -> None:
    """Save url to target, refusing more than limit bytes."""

    request = urllib.request.Request(url, headers={"User-Agent": "caramos-ota"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response, target.open("wb") as handle:
            total = 0
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > limit:
                    raise AppCenterUpdateError(f"{url} is larger than {limit} bytes")
                handle.write(chunk)
    except OSError as exc:
        raise AppCenterUpdateError(f"cannot download {url}: {exc}") from exc


def read_manifest(manifest: Path, signature: Path, keyring: Path = KEYRING) -> dict[str, str]:
    """Verify latest.json against the release key and return its checked fields."""

    if not keyring.is_file():
        raise AppCenterUpdateError(f"release key {keyring} is missing")
    check = subprocess.run(
        ["gpgv", "--keyring", str(keyring), str(signature), str(manifest)],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if check.returncode != 0:
        raise AppCenterUpdateError("latest.json is not signed by the App Center release key")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AppCenterUpdateError(f"latest.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("package") != PACKAGE:
        raise AppCenterUpdateError("latest.json does not describe caramos-app-center")
    version, file_name, sha256 = (str(data.get(key, "")) for key in ("version", "file", "sha256"))
    if not VERSION_RE.fullmatch(version):
        raise AppCenterUpdateError(f"invalid version {version!r} in latest.json")
    if file_name != f"{PACKAGE}_{version}_all.deb":
        raise AppCenterUpdateError(f"unexpected file name {file_name!r} in latest.json")
    if not SHA256_RE.fullmatch(sha256):
        raise AppCenterUpdateError("invalid sha256 in latest.json")
    return {"version": version, "file": file_name, "sha256": sha256}


def _newer(candidate: str, installed: str) -> bool:
    return run_command(["dpkg", "--compare-versions", candidate, "gt", installed], allow_fail=True).returncode == 0


def update_app_center(*, install_missing: bool = False, keyring: Path = KEYRING, fetch: Fetch = download) -> str:
    """Install the latest App Center release when it is newer than the installed one.

    Returns "not-installed" (and does nothing) when the App Center is absent and install_missing is
    false, "up-to-date", "updated" or "installed". Raises AppCenterUpdateError when the release cannot
    be fetched or verified, or apt fails.
    """

    installed = installed_version(PACKAGE)
    if not installed and not install_missing:
        return "not-installed"
    with tempfile.TemporaryDirectory(prefix="caramos-app-center-") as tmp:
        directory = Path(tmp)
        manifest, signature = directory / "latest.json", directory / "latest.json.sig"
        fetch(MANIFEST_URL, manifest, MANIFEST_LIMIT)
        fetch(f"{MANIFEST_URL}.sig", signature, SIGNATURE_LIMIT)
        release = read_manifest(manifest, signature, keyring)
        if installed and not _newer(release["version"], installed):
            log_info(f"App Center {installed} is up to date (latest release {release['version']})")
            return "up-to-date"

        deb = directory / release["file"]
        fetch(f"{RELEASES_URL}/download/v{release['version']}/{release['file']}", deb, DEB_LIMIT)
        if hashlib.sha256(deb.read_bytes()).hexdigest() != release["sha256"]:
            raise AppCenterUpdateError(f"{release['file']} does not match the signed checksum")
        log_info(f"Installing App Center {release['version']} (was {installed or 'not installed'})")
        try:
            run_command(["apt-get", "install", "--yes", "--", str(deb)])
        except subprocess.CalledProcessError as exc:
            raise AppCenterUpdateError(f"apt-get could not install {release['file']}") from exc
    return "updated" if installed else "installed"


def update_installed_app_center() -> None:
    """Daily check: update an installed App Center; never fails the OTA run."""

    try:
        update_app_center()
    except AppCenterUpdateError as exc:
        log_error(f"App Center update skipped: {exc}")
