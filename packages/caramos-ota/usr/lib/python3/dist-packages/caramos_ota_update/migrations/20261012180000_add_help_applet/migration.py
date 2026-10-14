"""Feedback & Help: the question mark at the right end of the panel.

The applet (caramos-help@caramos, shipped in this package) opens Feedback & Help to ask the VNLF community
for help, report a bug or send feedback, opens Driver Check, and lists the requests sent from this
computer. This adds it to the system panel defaults (new users, the live session of ISO builds) and to the
panel of every existing user, after the applets already on the right. Everything else on a panel stays as
it is; a user who removes the applet later keeps it removed, as this runs once.

Cinnamon 6.6 crashes when the system dconf database is rebuilt (`dconf update`) under a running session:
cinnamon-launcher starts it again, but the panel blinks and the clock loses its Vietnamese format until
the next login. While someone is logged in, the database is therefore left for a later `dconf update` and
each user's panel gets the applet through gsettings, which Cinnamon applies at once.
"""

from __future__ import annotations

import ast
import os
import pwd
import subprocess
import tempfile
from pathlib import Path

from caramos_ota_update.context import MigrationContext

DESCRIPTION = "Add the Feedback & Help applet to the Cinnamon panel"

APPLET_UUID = "caramos-help@caramos"
APPLET_DIR = Path("/usr/share/cinnamon/applets") / APPLET_UUID
# The applet goes on the panel with the first of these on its right: the Control Center, else the clock.
NEIGHBOURS = ("caramos-control-center@caramos", "calendar@cinnamon.org")
PANEL_DCONF_FILE = Path("/etc/dconf/db/local.d/01-caramos-task17-panel")
CINNAMON_SECTION = "org/cinnamon"


def add_applet(enabled_applets: str) -> str | None:
    """enabled-applets with the applet added at the right end of the panel with the Control Center.

    Without the Control Center it goes on the panel with the clock, and without either on the first panel.
    The list comes back unchanged when the applet is already there, and None when its format is unknown or
    it is empty (no panel to add to): then nothing is changed. Entries are panel:zone:position:uuid, with an
    instance id as fifth field since Cinnamon 2; the new instance id is not used by any applet.
    """

    value = enabled_applets.strip()
    if value.startswith("@as "):
        value = value[4:].lstrip()
    if not value.startswith("[") or not value.endswith("]"):
        return None
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not parsed or not isinstance(parsed, list) or any(not isinstance(entry, str) for entry in parsed):
        return None
    entries = [entry.split(":") for entry in parsed]
    if any(len(parts) < 4 for parts in entries):
        return None
    if any(parts[3] == APPLET_UUID for parts in entries):
        return value

    right = [parts for parts in entries if parts[1] == "right" and parts[2].isdigit()]
    neighbour = next((parts for uuid in NEIGHBOURS for parts in right if parts[3] == uuid), None)
    if neighbour:
        panel = neighbour[0]
    else:
        panels = [parts[0] for parts in entries]
        panel = "panel1" if "panel1" in panels else panels[0]
    position = max((int(parts[2]) for parts in right if parts[0] == panel), default=-1) + 1
    ids = [int(parts[4]) for parts in entries if len(parts) >= 5 and parts[4].isdigit()]
    entry = f"{panel}:right:{position}:{APPLET_UUID}:{max(ids, default=-1) + 1}"
    return f"[{', '.join(repr(item) for item in [*parsed, entry])}]"


def updated_defaults(source: str) -> str | None:
    """The panel defaults file with the applet; None when it has no enabled-applets to add to."""

    lines = source.splitlines()
    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == f"[{CINNAMON_SECTION}]"
            continue
        if in_section and stripped.startswith("enabled-applets="):
            updated = add_applet(stripped.split("=", 1)[1])
            if updated is None:
                return None
            lines[index] = f"enabled-applets={updated}"
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
    if not PANEL_DCONF_FILE.is_file() or PANEL_DCONF_FILE.is_symlink():
        context.log(f"no panel defaults at {PANEL_DCONF_FILE}; leave them as they are")
        return False
    source = PANEL_DCONF_FILE.read_text(encoding="utf-8")
    updated = updated_defaults(source)
    if updated is None:
        context.log(f"unexpected enabled-applets in {PANEL_DCONF_FILE}; leave the panel defaults as they are")
        return False
    if updated == source:
        context.log("Feedback & Help is already in the panel defaults")
        return False
    context.log(f"add Feedback & Help to the panel defaults: {PANEL_DCONF_FILE}")
    if not context.dry_run:
        _write_atomic(PANEL_DCONF_FILE, updated, PANEL_DCONF_FILE.stat().st_mode & 0o777)
    return True


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
        # A running session sees the change at once: Cinnamon adds the applet without a new login.
        env.update({"XDG_RUNTIME_DIR": str(runtime_dir), "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus"})
    else:
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    return env


def _gsettings(username: str, env: dict[str, str], args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["runuser", "-u", username, "--"]
    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        command += ["dbus-run-session", "--"]
    return subprocess.run([*command, "gsettings", *args], check=False, capture_output=True, text=True, env=env)


def _add_for_user(context: MigrationContext, username: str, uid: int, home: Path) -> None:
    env = _user_environment(username, uid, home)
    current = _gsettings(username, env, ["get", "org.cinnamon", "enabled-applets"])
    if current.returncode != 0:
        context.log(f"warning: could not read the panel of {username}: {current.stderr.strip()}")
        return
    updated = add_applet(current.stdout)
    if updated is None:
        context.log(f"warning: unexpected panel applets for {username}; leave the panel as it is")
        return
    if updated == current.stdout.strip().removeprefix("@as ").lstrip():
        context.log(f"Feedback & Help is already on the panel of {username}")
        return
    result = _gsettings(username, env, ["set", "org.cinnamon", "enabled-applets", updated])
    if result.returncode == 0:
        context.log(f"added Feedback & Help to the panel of {username}")
    else:
        context.log(f"warning: could not add Feedback & Help for {username}: {result.stderr.strip()}")


def run(context: MigrationContext) -> None:
    if not (APPLET_DIR / "applet.js").is_file():
        context.log(f"{APPLET_DIR} is missing; the panel is left as it is")
        return
    users = _desktop_users()
    defaults_changed = _update_defaults(context)
    if context.dry_run:
        context.log("[dry-run] add Feedback & Help to the panel of every desktop user")
        return
    if defaults_changed:
        logged_in = [username for username, uid, _home in users if _session_bus(uid).exists()]
        if logged_in:
            context.log(f"{', '.join(logged_in)} logged in: dconf update waits, Cinnamon would crash under it")
        else:
            context.run_command(["dconf", "update"], allow_fail=True)
    for username, uid, home in users:
        _add_for_user(context, username, uid, home)
