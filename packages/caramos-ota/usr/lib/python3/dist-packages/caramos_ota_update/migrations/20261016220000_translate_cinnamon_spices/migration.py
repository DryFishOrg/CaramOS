"""Compile the translations of the Cinnamon spices CaramOS installs, so the Start Menu speaks Vietnamese.

Hook 0170 copies Cinnamenu (the Start Menu), the weather applet and two extensions from the Cinnamon spices
repositories into /usr/share/cinnamon. Their po/ folders carry translations (Cinnamenu's vi.po has
"Tất cả ứng dụng" for "All applications"), but nothing compiled them, and Cinnamon only looks for spice
translations in ~/.local/share/locale (appletManager.js, cinnamon-settings). System Settings > Applets puts
them there when it installs a spice. So the Start Menu showed English words in a Vietnamese session.

This does what System Settings does: each po/<language>.po goes through `msgfmt -c` into
<language>/LC_MESSAGES/<uuid>.mo, under ~/.local/share/locale of every desktop user and of /etc/skel for
accounts created later, including the live session of ISOs built with this migration. A translation that is
already there is kept. A spice the user installed again in ~/.local/share/cinnamon is skipped for that user:
System Settings gave that copy its own translations. The Start Menu shows them from the next login.
"""

from __future__ import annotations

import os
import pwd
import re
import subprocess
import tempfile
from pathlib import Path

from caramos_ota_update.context import MigrationContext

DESCRIPTION = "Compile the translations of the Cinnamon spices CaramOS installs"

SPICES = Path("/usr/share/cinnamon")
KINDS = ("applets", "desklets", "extensions")
LOCALE = Path(".local/share/locale")
USER_SPICES = Path(".local/share/cinnamon")
SKEL = Path("/etc/skel")
SKEL_OWNER = (0, 0)
PASSWD_HOME_ROOT = Path("/home")
MSGFMT = "/usr/bin/msgfmt"
# po/<language>.po names, such as vi, pt_BR or sr@latin; anything else is not a folder name to create.
LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?(@[a-z]+)?$")
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

Spice = tuple[str, str, list[Path]]   # kind, uuid, po files


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


def _spices() -> list[Spice]:
    """Each spice in /usr/share/cinnamon that ships translations."""

    found = []
    for kind in KINDS:
        base = SPICES / kind
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            po_files = sorted((folder / "po").glob("*.po"))
            if folder.is_symlink() or not (folder / "metadata.json").is_file() or not po_files:
                continue
            found.append((kind, folder.name, po_files))
    return found


def _compile(context: MigrationContext, spices: list[Spice]) -> dict[str, dict[str, bytes]]:
    """{language: {uuid: .mo bytes}}; a po file msgfmt rejects is left out, as is an odd language name."""

    compiled: dict[str, dict[str, bytes]] = {}
    with tempfile.TemporaryDirectory(prefix="caramos-spice-translations-") as tmp:
        for _kind, uuid, po_files in spices:
            for po in po_files:
                language = po.name.split(".")[0]
                if not LANGUAGE_RE.fullmatch(language):
                    context.log(f"skip {po}: not a language name")
                    continue
                target = Path(tmp) / f"{uuid}.{language}.mo"
                result = subprocess.run(
                    [MSGFMT, "-c", "-o", str(target), str(po)],
                    check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                if result.returncode != 0:
                    context.log(f"skip {po}: msgfmt failed: {result.stderr.strip()[:200]}")
                    continue
                compiled.setdefault(language, {})[uuid] = target.read_bytes()
    return compiled


def _open_folder(parent: int, name: str, owner: tuple[int, int]) -> int | None:
    """Open the folder name in parent, creating it for owner; None for a symlink, a file or a folder of
    someone else. This runs as root in the user's home: nothing is followed or written outside it."""

    try:
        fd = os.open(name, DIRECTORY, dir_fd=parent)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o755, dir_fd=parent)
            fd = os.open(name, DIRECTORY, dir_fd=parent)
            os.fchown(fd, *owner)
        except OSError:
            return None
    except OSError:
        return None
    if os.fstat(fd).st_uid != owner[0]:
        os.close(fd)
        return None
    return fd


def _add_file(folder: int, name: str, data: bytes, owner: tuple[int, int]) -> bool:
    """Create name in folder with data for owner; False when it is already there (it stays as it is)."""

    staging = f".{name}.caramos-ota"
    try:
        os.unlink(staging, dir_fd=folder)   # left by an interrupted run
    except FileNotFoundError:
        pass
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            os.fchown(handle.fileno(), *owner)
        # A hard link never replaces an existing file: a translation already there wins.
        os.link(staging, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        return True
    except FileExistsError:
        return False
    finally:
        os.unlink(staging, dir_fd=folder)


def _install(root: Path, owner: tuple[int, int], translations: dict[str, dict[str, bytes]]) -> tuple[int, int] | None:
    """Add the translations under root/.local/share/locale; returns (files added, files that failed), or None
    when that folder cannot be used: missing home, a symlink, or a folder of someone else."""

    try:
        top = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    opened = [top]
    added = failed = 0
    try:
        if os.fstat(top).st_uid != owner[0]:
            return None
        locale = top
        for part in LOCALE.parts:
            locale = _open_folder(locale, part, owner)
            if locale is None:
                return None
            opened.append(locale)
        for language, files in sorted(translations.items()):
            language_folder = _open_folder(locale, language, owner)
            messages = None if language_folder is None else _open_folder(language_folder, "LC_MESSAGES", owner)
            try:
                if messages is None:
                    failed += len(files)
                    continue
                for uuid, data in sorted(files.items()):
                    try:
                        added += _add_file(messages, f"{uuid}.mo", data, owner)
                    except OSError:
                        failed += 1
            finally:
                for fd in (messages, language_folder):
                    if fd is not None:
                        os.close(fd)
    finally:
        for fd in opened:
            os.close(fd)
    return added, failed


def _own_copies(home: Path, spices: list[Spice]) -> set[str]:
    """Spices the user installed again from System Settings, which brought their own translations."""

    return {uuid for kind, uuid, _po in spices if (home / USER_SPICES / kind / uuid).exists()}


def run(context: MigrationContext) -> None:
    spices = _spices()
    if not spices:
        context.log(f"no spice with translations in {SPICES}")
        return
    names = ", ".join(uuid for _kind, uuid, _po in spices)
    if context.dry_run:
        context.log(f"compile the translations of {names} into {SKEL / LOCALE} and each user's ~/{LOCALE}")
        return
    if not Path(MSGFMT).is_file():
        context.log(f"{MSGFMT} is missing (package gettext); the spices stay untranslated")
        return
    translations = _compile(context, spices)
    targets = [(SKEL, SKEL_OWNER, set())]
    targets += [(Path(info.pw_dir), (info.pw_uid, info.pw_gid), _own_copies(Path(info.pw_dir), spices))
                for info in _desktop_users()]
    for root, owner, own in targets:
        for uuid in sorted(own):
            context.log(f"skip {uuid} in {root}: installed again in ~/{USER_SPICES}, with its own translations")
        wanted = {}
        for language, files in translations.items():
            kept = {uuid: data for uuid, data in files.items() if uuid not in own}
            if kept:
                wanted[language] = kept
        result = _install(root, owner, wanted)
        if result is None:
            context.log(f"skip {root / LOCALE}: not a folder of its owner")
            continue
        added, failed = result
        context.log(f"add {added} translations of {names} to {root / LOCALE}"
                    + (f"; {failed} could not be written" if failed else ""))
