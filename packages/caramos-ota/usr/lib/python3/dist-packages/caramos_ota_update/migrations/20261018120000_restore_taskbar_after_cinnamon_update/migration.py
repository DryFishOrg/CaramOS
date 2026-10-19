"""Keep the CaramOS taskbar when Mint updates Cinnamon, so the icons in the middle fit their dock again.

The dock in the middle of the panel is 40 px high and holds 32 px icons. Cinnamon draws them at the icon size
of the panel zone, and the CaramOS panel defaults give the middle zone none, so Cinnamon takes the panel
height, 48 px. Migration 20260803120000 changed Cinnamon's window list to draw 32 px icons anyway, but an
update of Cinnamon (6.6.4 -> 6.6.9 in October 2026) put the original file back, and the icons spilled out of
the dock (ticket H-1046).

This migration
- makes the CaramOS changes to Cinnamon's files again (caramos_ota_update.cinnamon_patches); the package now
  also does that after every Cinnamon update, through its dpkg trigger;
- gives the middle zone its own size, 32 px, in the panel defaults and for each user who has the CaramOS
  sizes, so the icons fit even when a later Cinnamon no longer takes the change. Cinnamon applies the size at
  once; the dot under running apps comes back from the next login. Sizes a user picked stay as they are.

Cinnamon 6.6 crashes when the system dconf database is rebuilt under a running session (see migration
20261012180000), so while someone is logged in the database waits for a later `dconf update`; each user's
own setting carries the new size meanwhile.
"""

from __future__ import annotations

import ast
import json
import os
import pwd
import subprocess
import tempfile
from pathlib import Path

from caramos_ota_update import cinnamon_patches
from caramos_ota_update.context import MigrationContext

DESCRIPTION = "Restore the CaramOS taskbar after a Cinnamon update and size the middle icons at 32 px"

KEY = "panel-zone-icon-sizes"
ICON_SIZES = '[{"panelId": 1, "left": 32, "center": 32, "right": 18}]'
# The sizes CaramOS set until now: in the panel defaults since 1.0.14, and for each user logged in when
# migration 20260803120000 ran.
CARAMOS_ICON_SIZES = ([{"panelId": 1, "left": 32, "right": 18}],)
DCONF_DEFAULT_FILES = (
    Path("/etc/dconf/db/local.d/00-caramos-theme"),
    Path("/etc/dconf/db/local.d/01-caramos-task17-panel"),
)
CINNAMON_SECTION = "[org/cinnamon]"


def parse_icon_sizes(value: str) -> object:
    """The JSON inside a GVariant string such as gsettings prints it; None when it is something else."""

    try:
        text = ast.literal_eval(value.strip())
        return json.loads(text) if isinstance(text, str) else None
    except (SyntaxError, ValueError):
        return None


def is_caramos_value(value: str) -> bool:
    return parse_icon_sizes(value) in CARAMOS_ICON_SIZES


def updated_defaults(source: str) -> str | None:
    """The defaults file with the new sizes (unchanged when it has them); None when its sizes are not ours."""

    lines = source.splitlines()
    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == CINNAMON_SECTION
            continue
        if in_section and stripped.startswith(f"{KEY}="):
            value = stripped.split("=", 1)[1]
            if parse_icon_sizes(value) == json.loads(ICON_SIZES):
                return source
            if not is_caramos_value(value):
                return None
            lines[index] = f"{KEY}={ICON_SIZES!r}"
            return "\n".join(lines) + "\n"
    return None


def _write_atomic(path: Path, content: str, mode: int) -> None:
    descriptor, staging_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staging = Path(staging_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        staging.chmod(mode)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def _update_defaults(context: MigrationContext) -> bool:
    changed = False
    for path in DCONF_DEFAULT_FILES:
        if not path.is_file() or path.is_symlink():
            continue
        source = path.read_text(encoding="utf-8")
        updated = updated_defaults(source)
        if updated is None:
            context.log(f"{path} has no CaramOS panel icon sizes; leave it as it is")
            continue
        if updated == source:
            context.log(f"the middle panel icons are already 32 px in {path}")
            continue
        context.log(f"size the middle panel icons at 32 px in {path}")
        changed = True
        if not context.dry_run:
            _write_atomic(path, updated, path.stat().st_mode & 0o777)
    return changed


def _desktop_users() -> list[tuple[str, int, Path]]:
    """People who log in: not system users, nor the dynamic users of services such as earlyoom (uid 61184+)."""

    users = []
    for entry in pwd.getpwall():
        if not 1000 <= entry.pw_uid < 60000 or entry.pw_shell.endswith(("/nologin", "/false")):
            continue
        if entry.pw_dir not in ("", "/", "/nonexistent") and Path(entry.pw_dir).is_dir():
            users.append((entry.pw_name, entry.pw_uid, Path(entry.pw_dir)))
    return sorted(users, key=lambda item: item[1])


def _session_bus(uid: int) -> Path:
    return Path(f"/run/user/{uid}/bus")


def _user_environment(username: str, uid: int, home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({"HOME": str(home), "USER": username, "LOGNAME": username})
    runtime_dir = _session_bus(uid).parent
    if _session_bus(uid).exists():
        # A running session sees the change at once: Cinnamon redraws the icons without a new login.
        env.update({"XDG_RUNTIME_DIR": str(runtime_dir), "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus"})
    else:
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    return env


def _gsettings(username: str, env: dict[str, str], args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["runuser", "-u", username, "--"]
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        command += ["dbus-run-session", "--"]
    return subprocess.run([*command, "gsettings", *args], check=False, capture_output=True, text=True, env=env)


def _update_user(context: MigrationContext, username: str, uid: int, home: Path) -> None:
    env = _user_environment(username, uid, home)
    current = _gsettings(username, env, ["get", "org.cinnamon", KEY])
    if current.returncode != 0:
        context.log(f"warning: could not read the panel icon sizes of {username}: {current.stderr.strip()}")
        return
    if parse_icon_sizes(current.stdout) == json.loads(ICON_SIZES):
        context.log(f"the middle panel icons of {username} are already 32 px")
        return
    if not is_caramos_value(current.stdout):
        context.log(f"{username} picked their own panel icon sizes; leave them as they are")
        return
    result = _gsettings(username, env, ["set", "org.cinnamon", KEY, repr(ICON_SIZES)])
    if result.returncode == 0:
        context.log(f"sized the middle panel icons of {username} at 32 px")
    else:
        context.log(f"warning: could not set the panel icon sizes of {username}: {result.stderr.strip()}")


def run(context: MigrationContext) -> None:
    try:
        cinnamon_patches.restore(context)
    except Exception as exc:  # the icon size below keeps the dock tidy even without the changes
        context.log(f"warning: could not restore the CaramOS changes to Cinnamon: {exc}")
    users = _desktop_users()
    defaults_changed = _update_defaults(context)
    if context.dry_run:
        context.log("[dry-run] size the middle panel icons at 32 px for every desktop user with the CaramOS sizes")
        return
    if defaults_changed:
        logged_in = [username for username, uid, _home in users if _session_bus(uid).exists()]
        if logged_in:
            context.log(f"{', '.join(logged_in)} logged in: dconf update waits, Cinnamon would crash under it")
        else:
            context.run_command(["dconf", "update"], allow_fail=True)
    for username, uid, home in users:
        _update_user(context, username, uid, home)
