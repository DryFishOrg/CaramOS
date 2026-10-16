"""Stop CopyQ, the clipboard manager, from starting at login.

CaramOS put CopyQ's autostart entry in /etc/skel, so it started for every user and sat in the system tray:
three processes, about 45 MiB of memory (PSS on the 1.0.20 test VM). Few people need a clipboard history,
and new installs no longer have CopyQ. Here CopyQ stays installed, with its history, and no longer starts at
login:

- Each user's ~/.config/autostart/copyq.desktop that is still the one CaramOS shipped gets
  X-GNOME-Autostart-enabled=false. Turning it off in System Settings > Startup Applications writes the same
  thing, so it can be turned on again there.
- The copy in /etc/skel goes, so users added later do not get it.

An entry the user changed stays as it is. A CopyQ that is running now stops with the session.
"""

from __future__ import annotations

import os
import pwd
import tempfile
from pathlib import Path

from caramos_ota_update.context import MigrationContext

DESCRIPTION = "Stop CopyQ from starting at login"

AUTOSTART = Path(".config/autostart/copyq.desktop")
SKEL = Path("/etc/skel")
PASSWD_HOME_ROOT = Path("/home")
ENABLED = "X-GNOME-Autostart-enabled=true"
DISABLED = "X-GNOME-Autostart-enabled=false"
# config/includes.chroot/etc/skel/.config/autostart/copyq.desktop of the CaramOS ISOs before 1.3.0.
SHIPPED = """\
[Desktop Entry]
Name=CopyQ
Icon=copyq
GenericName=Clipboard Manager
# Workaround / fix for issue #1526 that prevents a proper autostart of the tray icon in GNOME
X-GNOME-Autostart-Delay=3
# The rest is taken from Klipper application.
Type=Application
Terminal=false
X-KDE-autostart-after=panel
X-KDE-StartupNotify=false
X-KDE-UniqueApplet=true
Categories=Qt;KDE;Utility;

GenericName[en_GB]=Clipboard Tool
GenericName[vi]=Công cụ bảng nhớ tạm
GenericName[x-test]=xxClipboard Toolxx
Comment=A cut & paste history utility
Comment[en_GB]=A cut & paste history utility
Comment[vi]=Công cụ chứa lịch sử các cắt dán

Exec="/usr/bin/copyq"
Hidden=false
X-GNOME-Autostart-enabled=true
"""


def _shipped(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").splitlines() == SHIPPED.splitlines()
    except (OSError, UnicodeDecodeError):
        return False


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


def _turn_off(context: MigrationContext, home: Path, owner: tuple[int, int]) -> None:
    path = home / AUTOSTART
    if path.is_symlink() or not path.is_file():
        return
    directory = path.parent
    # This runs as root in the user's home: write only inside it, into their own folder and file.
    resolved = directory.resolve()
    if (directory.is_symlink() or home.resolve() not in resolved.parents
            or resolved.stat().st_uid != owner[0] or path.stat().st_uid != owner[0]):
        context.log(f"skip {path}: not a regular file in a folder of its owner")
        return
    if not _shipped(path):
        context.log(f"keep {path}: not the entry CaramOS shipped")
        return
    context.log(f"stop CopyQ starting at login: {path}")
    if context.dry_run:
        return
    content = "\n".join(DISABLED if line == ENABLED else line for line in SHIPPED.splitlines()) + "\n"
    mode = path.stat().st_mode & 0o777
    # mkstemp creates a new file and os.replace renames over the name without following it.
    fd, staging = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(staging, mode)
        os.chown(staging, *owner)
        os.replace(staging, path)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise


def _remove_from_skel(context: MigrationContext) -> None:
    path = SKEL / AUTOSTART
    if path.is_symlink() or not path.is_file() or not _shipped(path):
        return
    context.log(f"remove {path}")
    if not context.dry_run:
        path.unlink()


def run(context: MigrationContext) -> None:
    _remove_from_skel(context)
    for info in _desktop_users():
        _turn_off(context, Path(info.pw_dir), (info.pw_uid, info.pw_gid))
