"""Put the App Center on the Desktop.

CaramOS 1.3.0 stops preinstalling WPS Office and Chrome: the App Center installs them on demand. The
App Center is released on GitHub, not on the PPA, so this migration installs its latest signed release
when it is missing (caramos_ota.app_center; ISOs already ship it). It then copies its launcher to the
Desktop of every desktop user and to /etc/skel/Desktop for accounts created later, including the live
session user of ISOs built with this migration.

A launcher that is already there is left alone. The Desktop folder comes from XDG_DESKTOP_DIR in
~/.config/user-dirs.dirs (localized names such as "Màn hình nền"); a folder that is a symlink, lies
outside the home or belongs to someone else is skipped, since this runs as root.
"""

from __future__ import annotations

import os
import pwd
import tempfile
from pathlib import Path

from caramos_ota.app_center import AppCenterUpdateError, update_app_center
from caramos_ota_update.context import MigrationContext

DESCRIPTION = "Install the App Center and put its launcher on the Desktop"

PACKAGE = "caramos-app-center"
LAUNCHER_NAME = "caramos-app-center.desktop"
SYSTEM_LAUNCHER = Path("/usr/share/applications") / LAUNCHER_NAME
SKEL_DESKTOP = Path("/etc/skel/Desktop")
PASSWD_HOME_ROOT = Path("/home")


def _desktop_users() -> list[pwd.struct_passwd]:
    users = []
    for info in pwd.getpwall():
        if not 1000 <= info.pw_uid < 60000 or info.pw_shell.endswith(("nologin", "false")):
            continue
        home = Path(info.pw_dir)
        if PASSWD_HOME_ROOT not in home.parents or not home.is_dir():
            continue
        users.append(info)
    return sorted(users, key=lambda info: info.pw_uid)


def desktop_dir(home: Path) -> Path:
    """The user's Desktop folder: XDG_DESKTOP_DIR when set to a folder in the home, else ~/Desktop."""

    try:
        lines = (home / ".config/user-dirs.dirs").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    for line in lines:
        key, _sep, value = line.strip().partition("=")
        if key != "XDG_DESKTOP_DIR":
            continue
        value = value.strip().strip('"')
        if value.startswith("$HOME/") and len(value) > len("$HOME/"):
            return home / value[len("$HOME/"):]
    return home / "Desktop"


def _safe_user_dir(directory: Path, home: Path, uid: int) -> bool:
    if directory.is_symlink() or not directory.is_dir():
        return False
    resolved = directory.resolve()
    return home.resolve() in resolved.parents and resolved.stat().st_uid == uid


def _place(context: MigrationContext, content: str, directory: Path, owner: tuple[int, int] | None) -> bool:
    target = directory / LAUNCHER_NAME
    if target.exists() or target.is_symlink():
        context.log(f"keep existing {target}")
        return False
    context.log(f"add App Center launcher {target}")
    if context.dry_run:
        return True
    # mkstemp creates a new file (O_EXCL) and os.replace renames over the name without following it,
    # so nothing the user put in their folder can redirect this root write.
    fd, staging = tempfile.mkstemp(prefix=f".{LAUNCHER_NAME}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            os.fchmod(handle.fileno(), 0o755)
            if owner is not None:
                os.fchown(handle.fileno(), *owner)
        os.replace(staging, target)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise
    return True


def run(context: MigrationContext) -> None:
    if not SYSTEM_LAUNCHER.is_file():
        context.log(f"install {PACKAGE} from its latest GitHub release")
        if not context.dry_run:
            try:
                update_app_center(install_missing=True)
            except AppCenterUpdateError as exc:
                context.log(f"could not install {PACKAGE}: {exc}; no Desktop launcher")
                return
    if not SYSTEM_LAUNCHER.is_file():
        context.log(f"{SYSTEM_LAUNCHER} is missing; no Desktop launcher")
        return
    content = SYSTEM_LAUNCHER.read_text(encoding="utf-8")

    if not context.dry_run:
        SKEL_DESKTOP.mkdir(parents=True, exist_ok=True)
    if SKEL_DESKTOP.is_dir() or context.dry_run:
        _place(context, content, SKEL_DESKTOP, None)

    for info in _desktop_users():
        home = Path(info.pw_dir)
        directory = desktop_dir(home)
        if not _safe_user_dir(directory, home, info.pw_uid):
            context.log(f"skip {info.pw_name}: no usable Desktop folder at {directory}")
            continue
        _place(context, content, directory, (info.pw_uid, info.pw_gid))
