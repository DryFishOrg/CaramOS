"""Replace the Zalo AppImage that CaramOS put on the computer with a pinned build, in the background.

A migration pins a build of Zalo for Linux (VN-Linux-Family/zalo-for-linux) and calls schedule(). The download
is about 170 MB from GitHub, so it does not run inside the update: on a slow line it would hit the Update
Center's 10-minute limit, and a failed download would fail the migration, which stops the OTA run with every
later update waiting behind it. schedule() writes the pin to PENDING_FILE and starts this module as the
background unit caramos-zalo-update:

    python3 -I -m caramos_ota_update.zalo_update

When the download fails (offline, GitHub unreachable, not enough disk space), the pin stays in PENDING_FILE
and the daily check (caramos-ota --check) starts the unit again, until the pinned build is installed, Zalo is
removed or replaced by the user, or PENDING_DAYS have passed.

Only a Zalo that CaramOS or the App Center installed is replaced: its sha256 is the one in STATE_FILE, or one
of the pin's known CaramOS builds. A build that is already newer than the pin is kept. The download is
checked against the pinned size and sha256, or, when upstream rebuilt the tag, against the sha256 GitHub
gives for the same build of the same tag.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from caramos_ota.constants import STATE_DIR
from caramos_ota.logging_utils import init_log, log_error

from .context import MigrationContext, MigrationContextError

ZALO_APPIMAGE = Path("/usr/local/bin/Zalo.AppImage")
# What CaramOS installed, so an update can tell its own build from one the user put there. "flavour": "full"
# marks the build with calls, which the App Center looks for.
STATE_FILE = STATE_DIR / "zalo-appimage.json"
PENDING_FILE = STATE_DIR / "zalo-update.json"
LOCK_FILE = Path("/run/caramos-ota-zalo.lock")
UNIT = "caramos-zalo-update"
PENDING_DAYS = 30
FLAVOUR = "full"
CALL_PACKAGES = ("python3", "libopus0", "pulseaudio-utils")

USER_AGENT = "caramos-ota"
TIMEOUT_SECONDS = 60
DOWNLOAD_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 5
FREE_SPACE_MARGIN = 64 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
TAG_RE = re.compile(r"^\d+(\.\d+)*$")


class Pin(NamedTuple):
    repository: str
    tag: str
    asset: str
    sha256: str
    size: int
    known_builds: tuple[str, ...] = ()   # sha256 of builds CaramOS installed, for a missing state file

    def download_url(self, name: str | None = None) -> str:
        return f"https://github.com/{self.repository}/releases/download/{self.tag}/{urllib.parse.quote(name or self.asset)}"

    @property
    def rebuilt_pattern(self) -> re.Pattern[str]:
        """The same build of the same tag with another commit hash: upstream CI may rebuild a tag."""

        match = re.fullmatch(r"(Zalo-.+?\+ZaDark-)[0-9.]+-[0-9a-f]{7}(-x86_64\.AppImage)", self.asset)
        if match is None:
            return re.compile(re.escape(self.asset))
        return re.compile(re.escape(match.group(1)) + r"[0-9.]+-[0-9a-f]{7}" + re.escape(match.group(2)))


class Asset(NamedTuple):
    name: str
    url: str
    sha256: str
    size: int


def _tag_key(tag: object) -> tuple[int, ...] | None:
    text = str(tag or "")
    return tuple(int(part) for part in text.split(".")) if TAG_RE.fullmatch(text) else None


def plan_action(installed_sha256: str | None, state: dict, pin: Pin) -> str:
    """Return absent, current, replace or user for what is at ZALO_APPIMAGE."""

    if installed_sha256 is None:
        # Removed by the user or never installed: CaramOS 1.0.21 and later ship no Zalo.
        return "absent"
    recorded = state.get("sha256")
    if installed_sha256 == pin.sha256:
        return "current"
    if installed_sha256 == recorded:
        installed_tag, pinned_tag = _tag_key(state.get("tag")), _tag_key(pin.tag)
        if installed_tag is not None and pinned_tag is not None and installed_tag >= pinned_tag:
            return "current"   # this build, or a newer one the App Center installed
        return "replace"
    if installed_sha256 in pin.known_builds:
        return "replace"
    return "user"


def file_sha256(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_state() -> dict:
    return _load_json(STATE_FILE)


def missing_packages(packages: tuple[str, ...] = CALL_PACKAGES) -> list[str]:
    missing = []
    for package in packages:
        result = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Status}", package],
                                check=False, capture_output=True, text=True)
        if result.returncode != 0 or result.stdout.strip() != "installed":
            missing.append(package)
    return missing


def install_call_packages(context: MigrationContext) -> None:
    missing = missing_packages()
    if not missing:
        return
    try:
        context.apt_install(missing, recommends=False)
    except MigrationContextError as exc:
        # Messages work without them, and Zalo's first call says what is missing.
        context.log(f"could not install {', '.join(missing)} for Zalo calls ({exc})")


def record(context: MigrationContext, repository: str, tag: str, asset_name: str, sha256: str) -> None:
    content = {"repository": repository, "tag": tag, "asset": asset_name, "sha256": sha256, "flavour": FLAVOUR}
    context.write_file_if_changed(STATE_FILE, json.dumps(content, indent=2) + "\n")


# Waiting for the background download -----------------------------------------------------------

def save_pending(pin: Pin, since: str | None = None) -> None:
    data = {**pin._asdict(), "known_builds": list(pin.known_builds),
            "since": since or datetime.now(timezone.utc).isoformat()}
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = PENDING_FILE.with_name(PENDING_FILE.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, PENDING_FILE)


def load_pending() -> tuple[Pin, datetime] | None:
    data = _load_json(PENDING_FILE)
    try:
        pin = Pin(str(data["repository"]), str(data["tag"]), str(data["asset"]), str(data["sha256"]),
                  int(data["size"]), tuple(str(item) for item in data.get("known_builds", ())))
        since = datetime.fromisoformat(str(data["since"]))
    except (KeyError, TypeError, ValueError):
        return None
    if since.tzinfo is None:
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", pin.sha256) or "/" in pin.asset or pin.repository.count("/") != 1:
        return None
    return pin, since


def clear_pending() -> None:
    PENDING_FILE.unlink(missing_ok=True)


def start_background(context: MigrationContext) -> bool:
    """Start the background unit; False when systemd cannot (an ISO build): the daily check starts it."""

    try:
        if subprocess.run(["systemctl", "is-active", "--quiet", f"{UNIT}.service"], check=False,
                          timeout=30).returncode == 0:
            return True   # already downloading
    except (OSError, subprocess.SubprocessError):
        pass
    command = ["systemd-run", "--no-block", "--quiet", "--collect", f"--unit={UNIT}",
               "--description=CaramOS: update Zalo", "--property=Nice=10",
               "/usr/bin/python3", "-I", "-m", "caramos_ota_update.zalo_update"]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        context.log(f"Zalo downloads with the next daily check (systemd-run: {exc})")
        return False
    if result.returncode != 0:
        context.log(f"Zalo downloads with the next daily check (systemd-run: {result.stderr.strip()[:200]})")
        return False
    return True


def schedule(context: MigrationContext, pin: Pin) -> str:
    """For a migration: queue the pinned build for the background unit when the installed Zalo needs it."""

    if ZALO_APPIMAGE.is_symlink():
        context.log(f"{ZALO_APPIMAGE} is a symlink managed outside CaramOS; leaving Zalo as it is")
        return "user"
    state = load_state()
    installed = file_sha256(ZALO_APPIMAGE)
    action = plan_action(installed, state, pin)
    if action == "absent":
        context.log("Zalo is not installed; nothing to do")
    elif action == "user":
        context.log(f"{ZALO_APPIMAGE} (sha256 {installed}) is not a CaramOS build; leaving it as it is")
    elif action == "current":
        context.log(f"Zalo {state.get('tag') or pin.tag} from {pin.repository} is already installed")
        if not context.dry_run:
            install_call_packages(context)
            if installed == pin.sha256:
                record(context, pin.repository, pin.tag, pin.asset, pin.sha256)
            elif not state.get("flavour"):
                record(context, pin.repository, str(state.get("tag")), str(state.get("asset") or pin.asset),
                       str(installed))
    else:
        context.log(f"Zalo {pin.tag} ({pin.size // (1024 * 1024)} MiB) downloads in the background; "
                    f"the Zalo in use keeps working until it is replaced")
        if not context.dry_run:
            install_call_packages(context)
            save_pending(pin)
            start_background(context)
    return action


def resume_in_background(context: MigrationContext | None = None) -> None:
    """Daily check: start the background unit again while a pinned build waits; never fails the OTA run."""

    try:
        if PENDING_FILE.exists():
            start_background(context or MigrationContext())
    except Exception as exc:  # noqa: BLE001 - the OTA run must go on
        log_error(f"Zalo update not resumed: {exc}")


# The download ---------------------------------------------------------------------------------

def _open(url: str, accept: str | None = None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_SECONDS)


def resolve_rebuilt_asset(pin: Pin) -> Asset:
    """Find the build of the pinned tag in the release when the pinned file has been replaced."""

    api_url = f"https://api.github.com/repos/{pin.repository}/releases/tags/{pin.tag}"
    with _open(api_url, accept="application/vnd.github+json") as response:
        release = json.load(response)
    matches = [item for item in release.get("assets", []) if pin.rebuilt_pattern.fullmatch(str(item.get("name", "")))]
    if len(matches) != 1:
        raise MigrationContextError(f"expected one Zalo AppImage in release {pin.tag}, found {len(matches)}")
    item = matches[0]
    digest = str(item.get("digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise MigrationContextError(f"release {pin.tag} gives no sha256 for {item['name']}")
    url = str(item.get("browser_download_url", ""))
    if url != pin.download_url(item["name"]):
        raise MigrationContextError(f"unexpected download URL for {item['name']}: {url}")
    return Asset(item["name"], url, digest.removeprefix("sha256:"), int(item["size"]))


def download(asset: Asset, directory: Path) -> Path:
    """Download asset next to its destination and return the verified, executable temporary file."""

    fd, name = tempfile.mkstemp(prefix=".Zalo.AppImage.", dir=directory)
    tmp = Path(name)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as out, _open(asset.url) as response:
            while chunk := response.read(CHUNK_SIZE):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        if size != asset.size or digest.hexdigest() != asset.sha256:
            raise MigrationContextError(
                f"{asset.name} failed verification: got {size} bytes sha256 {digest.hexdigest()}, "
                f"expected {asset.size} bytes sha256 {asset.sha256}"
            )
        tmp.chmod(0o755)
        return tmp
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def fetch(context: MigrationContext, pin: Pin, directory: Path) -> tuple[Asset, Path]:
    pinned = asset = Asset(pin.asset, pin.download_url(), pin.sha256, pin.size)
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            context.log(f"download {asset.url} ({asset.size // (1024 * 1024)} MiB), attempt {attempt}")
            return asset, download(asset, directory)
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code == 404 and asset == pinned:
                context.log(f"{asset.name} is no longer published; looking up the {pin.tag} release")
                try:
                    asset = resolve_rebuilt_asset(pin)
                except (OSError, ValueError, KeyError, MigrationContextError) as resolve_error:
                    last_error = resolve_error
                else:
                    continue
        except (OSError, MigrationContextError) as error:
            last_error = error
        context.log(f"download did not work: {last_error}")
        if attempt < DOWNLOAD_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise MigrationContextError(f"could not download Zalo {pin.tag} from {pin.repository}: {last_error}")


def finish_pending(context: MigrationContext) -> str:
    """Install the pinned build PENDING_FILE waits for: none, dropped, current, installed, failed or expired."""

    pending = load_pending()
    if pending is None:
        clear_pending()
        return "none"
    pin, since = pending
    if datetime.now(timezone.utc) - since > timedelta(days=PENDING_DAYS):
        context.log(f"Zalo {pin.tag} could not be downloaded for {PENDING_DAYS} days; stop trying")
        clear_pending()
        return "expired"
    if ZALO_APPIMAGE.is_symlink():
        clear_pending()
        return "dropped"
    state = load_state()
    installed = file_sha256(ZALO_APPIMAGE)
    action = plan_action(installed, state, pin)
    if action in ("absent", "user"):
        context.log(f"Zalo was {'removed' if action == 'absent' else 'replaced'} meanwhile; nothing to download")
        clear_pending()
        return "dropped"
    if action == "current":
        if installed == pin.sha256:
            record(context, pin.repository, pin.tag, pin.asset, pin.sha256)
        clear_pending()
        return "current"

    directory = ZALO_APPIMAGE.parent
    try:
        free = shutil.disk_usage(directory).free
        if free < pin.size + FREE_SPACE_MARGIN:
            raise MigrationContextError(
                f"not enough free space in {directory} for Zalo: {free // (1024 * 1024)} MiB free, "
                f"{(pin.size + FREE_SPACE_MARGIN) // (1024 * 1024)} MiB needed"
            )
        asset, tmp = fetch(context, pin, directory)
    except (OSError, MigrationContextError) as exc:
        log_error(f"Zalo {pin.tag} not installed yet, the daily check tries again: {exc}")
        return "failed"
    # A running Zalo keeps the old file open, so replacing it is safe; the new one is used on next start.
    os.replace(tmp, ZALO_APPIMAGE)
    record(context, pin.repository, pin.tag, asset.name, asset.sha256)
    clear_pending()
    context.log(f"installed {asset.name} at {ZALO_APPIMAGE}")
    return "installed"


def main() -> int:
    if os.geteuid() != 0:
        print("caramos_ota_update.zalo_update must run as root")
        return 2
    init_log()
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another Zalo update is running")
            return 0
        result = finish_pending(MigrationContext())
    return 1 if result == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
