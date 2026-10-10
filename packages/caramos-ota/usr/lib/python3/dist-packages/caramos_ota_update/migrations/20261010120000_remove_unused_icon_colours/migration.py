"""Remove the colours of the Tela circle icon theme that CaramOS does not use.

ISOs up to 1.0.21 installed every colour of the theme (`install.sh -a`): 15 colours, each as a base, a
-light and a -dark folder. CaramOS uses Tela-circle-light, and Tela-circle-dark in dark mode, both on top
of Tela-circle. The 14 other colours are about 600,000 small files and symlinks, 1.1 GiB on disk, which
also slow down installing and anything that walks /usr (backups, search indexes). New ISOs install the
standard colour only.

A colour is kept, with its three folders, when a user's dconf database, the login screen or the system
dconf defaults name it (a user may have picked it as their icon theme), or when a package owns it.
"""

from __future__ import annotations

import os
import pwd
import shutil
from pathlib import Path

from caramos_ota_update.context import MigrationContext

DESCRIPTION = "Remove the unused colours of the Tela circle icon theme"

ICONS_DIR = Path("/usr/share/icons")
THEME = "Tela-circle"
UNUSED_COLOURS = ("black", "blue", "brown", "green", "grey", "orange", "pink", "purple", "red", "yellow",
                  "manjaro", "ubuntu", "dracula", "nord")
VARIANTS = ("", "-light", "-dark")
SYSTEM_SETTINGS = (Path("/etc/lightdm/slick-greeter.conf"), Path("/etc/dconf/db/local.d"))
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


def _read_bytes(path: Path) -> bytes:
    # O_NOFOLLOW: this runs as root and the user's dconf file could be a symlink to anything.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return b""
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read(64 * 1024 * 1024)
    except OSError:
        return b""


def _settings_files() -> list[Path]:
    files = []
    for path in SYSTEM_SETTINGS:
        files.extend(sorted(p for p in path.iterdir() if p.is_file()) if path.is_dir() else [path])
    files.extend(Path(info.pw_dir) / ".config/dconf/user" for info in _desktop_users())
    return files


def colours_in_use() -> set[str]:
    """Colours that some settings file names, e.g. icon-theme='Tela-circle-red-dark'."""

    # dconf stores string values as plain text in its binary database, so a byte search finds them.
    found: set[str] = set()
    for path in _settings_files():
        content = _read_bytes(path)
        found.update(colour for colour in UNUSED_COLOURS if f"{THEME}-{colour}".encode() in content)
    return found


def _owned_by_a_package(context: MigrationContext, path: Path) -> bool:
    result = context.run_command(["dpkg-query", "--search", str(path)], allow_fail=True)
    return result is not None and result.returncode == 0


def run(context: MigrationContext) -> None:
    in_use = colours_in_use()
    for colour in UNUSED_COLOURS:
        folders = [ICONS_DIR / f"{THEME}-{colour}{variant}" for variant in VARIANTS]
        present = [folder for folder in folders if folder.is_dir() and not folder.is_symlink()]
        if not present:
            continue
        if colour in in_use:
            context.log(f"keep {THEME}-{colour}: selected in the settings of a user or the system")
            continue
        if any(_owned_by_a_package(context, folder) for folder in present):
            context.log(f"keep {THEME}-{colour}: installed by a package")
            continue
        for folder in present:
            context.log(f"remove {folder}")
            if not context.dry_run:
                shutil.rmtree(folder)
