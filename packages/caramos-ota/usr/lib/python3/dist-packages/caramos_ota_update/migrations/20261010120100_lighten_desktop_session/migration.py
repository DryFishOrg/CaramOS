"""Lighten the desktop session and keep the machine responsive when memory runs out.

Measured on the test VMs (4 vCPU, 6 GiB, automatic login, 5 minutes after boot), CaramOS 1.0.21 used
about 95 MiB more memory than Linux Mint 22.3. With these changes it uses about 50 MiB less, and every
feature stays:

- GTK 3 caches the OpenGL visuals of the X screen in the GDK_VISUALS property of the root window. When it
  is missing, every GTK 3 process probes OpenGL and keeps Mesa and LLVM loaded (~13 MiB each). The login
  screen fills it before a password login, but an automatic login (and the live session) starts on a
  fresh X server, where cinnamon-session starts ~8 csd-* daemons at once. A tiny GTK call from
  Xsession.d fills the cache first.
- Fcitx 5's classic UI follows the accent colour through the XDG Settings portal, which starts
  xdg-desktop-portal and four backends (~20 MiB) at every login. Cinnamon has no accent colour.
- evolution-alarm-notify, the reminder daemon of GNOME Calendar and Evolution, starts the Evolution
  data server (~55 MiB in four processes) at every login, although most users never add an event. It
  now starts once there is an event or task in a local calendar, a calendar account or a subscription
  (checked at login, then once a minute), or right away when Evolution is installed.
  system-config-printer's print queue applet (~20 MiB) repeats the print notifications Cinnamon already
  shows. Both are overridden for Cinnamon sessions in /etc/xdg/xdg-cinnamon/autostart, which takes
  precedence over /etc/xdg/autostart; package files stay.
- Flameshot paints with QPainter, yet Qt's OpenGL integration loaded Mesa and LLVM into it (~22 MiB).
  Its autostart entry, which CaramOS puts in /etc/skel and so in each user's ~/.config/autostart, now
  starts it with QT_XCB_GL_INTEGRATION=none, wherever its Exec line is still CaramOS's.
- cups-browsed (queues for network printers, which CUPS now lists by itself) and kerneloops (sends
  kernel crash reports to Ubuntu and holds boot until the network is online) are disabled.
- earlyoom (a Recommends of caramos-ota) stops the largest process when free RAM and swap both fall
  under 10%. Without it, a machine with an HDD froze for over 30 minutes while swapping in tests; with it,
  the desktop never stalled. It never picks the processes the session needs.

Boot and login also do less work:

- caramos-fcitx5-lotus-system-enable ran `systemctl enable` for every user at every boot, and
  `systemctl enable` reloads systemd, which stalls the units starting at that moment. It now only
  enables users whose Lotus server is not enabled yet.
- The Lotus login helper copied the CaramOS input method defaults and restarted Fcitx at every login,
  although org.fcitx.Fcitx5.desktop starts it at the same time. It now restarts Fcitx only when a
  default changed. It no longer resets the custom0 keyboard shortcut (Flameshot, Super+Shift+S), a
  cleanup of an old experiment that also undid a user's change to that shortcut at every login.
"""

from __future__ import annotations

import os
import pwd
import tempfile
from pathlib import Path

from caramos_ota_update.context import MigrationContext

DESCRIPTION = "Lighten the desktop session and prevent freezes when memory runs out"

SYSTEMD_RUNTIME = Path("/run/systemd/system")
UNIT_DIRS = (Path("/etc/systemd/system"), Path("/usr/lib/systemd/system"), Path("/lib/systemd/system"))

GTK_VISUALS_PRIMER = Path("/etc/X11/Xsession.d/60caramos-prime-gtk-visuals")
GTK_VISUALS_PRIMER_SCRIPT = """\
# CaramOS (caramos-ota): fill GTK 3's cache of OpenGL visuals, the GDK_VISUALS property of the root
# window, before cinnamon-session starts its GTK daemons. Without it, after an automatic login each of
# them probes OpenGL at the same time and keeps Mesa and LLVM loaded (~13 MiB per process).
if [ -n "$DISPLAY" ] && [ -x /usr/bin/python3 ]; then
    (/usr/bin/python3 -c 'import gi; gi.require_version("Gdk", "3.0"); from gi.repository import Gdk; Gdk.Display.open(None)' >/dev/null 2>&1 &)
fi
"""

FCITX_CLASSICUI = Path("/etc/xdg/fcitx5/conf/classicui.conf")
FCITX_CLASSICUI_CONFIG = """\
# CaramOS (caramos-ota): system defaults of the Fcitx 5 classic UI; a user's own
# ~/.config/fcitx5/conf/classicui.conf overrides them. Following the accent colour or the dark style
# queries the XDG Settings portal, which starts xdg-desktop-portal and its backends at every login.
UseAccentColor=False
UseDarkTheme=False
"""

CINNAMON_AUTOSTART = Path("/etc/xdg/xdg-cinnamon/autostart")
SYSTEM_AUTOSTART = Path("/etc/xdg/autostart")
ALARM_NOTIFY = "org.gnome.Evolution-alarm-notify.desktop"
ALARM_NOTIFY_WRAPPER = Path("/usr/local/libexec/caramos-evolution-alarm-notify")
ALARM_NOTIFY_WRAPPER_SCRIPT = """\
#!/bin/sh
# CaramOS (caramos-ota): Evolution's reminder daemon starts the Evolution data server (four processes,
# ~55 MiB). Start it once the user has something to be reminded of: an event or task in a local calendar
# (GNOME Calendar), or a calendar account or subscription. Until then, check once a minute.
data="${XDG_DATA_HOME:-$HOME/.local/share}/evolution"
config="${XDG_CONFIG_HOME:-$HOME/.config}/evolution"

has_reminders() {
    command -v evolution >/dev/null 2>&1 && return 0
    grep -qs -e 'BEGIN:VEVENT' -e 'BEGIN:VTODO' "$data"/calendar/*/calendar.ics "$data"/tasks/*/tasks.ics && return 0
    for source in "$config"/sources/*.source; do
        case "${source##*/}" in
            '*.source' | system-proxy.source) ;;
            *) return 0 ;;
        esac
    done
    return 1
}

until has_reminders; do
    sleep 60
done
exec /usr/libexec/evolution-data-server/evolution-alarm-notify "$@"
"""
PRINT_APPLET = "print-applet.desktop"
PRINT_APPLET_OVERRIDE = """\
[Desktop Entry]
Type=Application
Name=Print Queue Applet
# CaramOS (caramos-ota): Cinnamon already shows print job notifications (csd-print-notifications).
Hidden=true
"""

DISABLED_SERVICES = ("cups-browsed.service", "kerneloops.service")

FLAMESHOT_AUTOSTART = Path(".config/autostart/flameshot.desktop")
FLAMESHOT_EXEC = "Exec=/usr/bin/flameshot"
FLAMESHOT_EXEC_WITHOUT_GL = "Exec=env QT_XCB_GL_INTEGRATION=none /usr/bin/flameshot"
SKEL = Path("/etc/skel")
PASSWD_HOME_ROOT = Path("/home")

LOTUS_SYSTEM_HELPER = Path("/usr/local/sbin/caramos-fcitx5-lotus-system-enable")
LOTUS_SYSTEM_HELPER_SCRIPT = r'''#!/bin/sh
set -eu

TEMPLATE_UNIT="/lib/systemd/system/fcitx5-lotus-server@.service"

[ -f "$TEMPLATE_UNIT" ] || [ -f "/etc/systemd/system/fcitx5-lotus-server@.service" ] || exit 0
command -v systemctl >/dev/null 2>&1 || exit 0

while IFS=: read -r user _ uid _ _ home shell; do
    [ "$uid" -ge 1000 ] 2>/dev/null || continue
    [ "$uid" -lt 60000 ] 2>/dev/null || continue
    [ -d "$home" ] || continue
    case "$home" in
        /home/*) ;;
        *) continue ;;
    esac
    case "$shell" in
        */nologin|*/false) continue ;;
    esac

    unit="fcitx5-lotus-server@${user}.service"
    # multi-user.target starts an enabled server. `systemctl enable` also reloads systemd, which
    # stalls the rest of boot, so it only runs for users that are not enabled yet.
    systemctl is-enabled --quiet "$unit" 2>/dev/null && continue
    # --no-block: fcitx5-lotus-server@ is ordered After=multi-user.target, which waits for this
    # oneshot. Waiting for the start job here deadlocks boot.
    if ! systemctl enable --now --no-block "$unit" >/dev/null 2>&1; then
        echo "caramos-fcitx5-lotus-system-enable: failed to enable service for ${user}" >&2
    fi
done < /etc/passwd
'''

LOTUS_LOGIN_HELPER = Path("/usr/local/bin/caramos-fcitx5-lotus-enable")
LOTUS_LOGIN_HELPER_SCRIPT = r'''#!/bin/sh
set -eu

# Avoid competing input method daemons.
killall ibus-daemon >/dev/null 2>&1 || ibus exit >/dev/null 2>&1 || true

mkdir -p "${HOME}/.config/fcitx5/conf" "${HOME}/.config/environment.d"

# The CaramOS defaults apply at every login. org.fcitx.Fcitx5.desktop starts Fcitx at the same time,
# so it is restarted only when one of them changed.
changed=0
install_default() {
    [ -f "$1" ] || return 0
    cmp -s "$1" "$2" 2>/dev/null && return 0
    cp -f "$1" "$2"
    changed=1
}
install_default /etc/skel/.config/fcitx5/config "${HOME}/.config/fcitx5/config"
install_default /etc/skel/.config/fcitx5/profile "${HOME}/.config/fcitx5/profile"
install_default /etc/skel/.config/fcitx5/conf/lotus.conf "${HOME}/.config/fcitx5/conf/lotus.conf"
install_default /etc/skel/.config/environment.d/90-fcitx5-lotus.conf "${HOME}/.config/environment.d/90-fcitx5-lotus.conf"

if [ "$changed" = 1 ] && command -v fcitx5 >/dev/null 2>&1; then
    fcitx5 -d --replace >/dev/null 2>&1 || true
fi
'''

EARLYOOM_DROPIN = Path("/etc/systemd/system/earlyoom.service.d/caramos.conf")
EARLYOOM_DROPIN_CONFIG = """\
# CaramOS (caramos-ota): earlyoom's thresholds (free RAM and free swap both under 10%), an hourly memory
# report, and never the processes the desktop session needs (matched against the process name).
[Service]
ExecStart=
ExecStart=/usr/bin/earlyoom -r 3600 --avoid ^(init|systemd|Xorg|lightdm|cinnamon-sessio|dbus-)
"""


def _systemd_running() -> bool:
    return SYSTEMD_RUNTIME.is_dir()


def _write(context: MigrationContext, path: Path, content: str, mode: int = 0o644) -> bool:
    if (path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8") == content
            and path.stat().st_mode & 0o777 == mode):
        context.log(f"file unchanged: {path}")
        return False
    context.log(f"write {path}")
    if context.dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(staging, mode)
        os.replace(staging, path)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise
    return True


def alarm_notify_override(original: str) -> str:
    """The package's entry, run through the wrapper that checks for a calendar app."""

    lines = original.splitlines()
    exec_lines = [index for index, line in enumerate(lines) if line.startswith("Exec=")]
    if "[Desktop Entry]" not in lines or len(exec_lines) != 1:
        return ""
    command = lines[exec_lines[0]][len("Exec="):].split()
    if not command or command[0] != "/usr/libexec/evolution-data-server/evolution-alarm-notify":
        return ""
    lines[exec_lines[0]] = "Exec=" + " ".join([str(ALARM_NOTIFY_WRAPPER), *command[1:]])
    index = lines.index("[Desktop Entry]") + 1
    lines[index:index] = [
        f"# CaramOS (caramos-ota): copy of /etc/xdg/autostart/{ALARM_NOTIFY} started through",
        f"# {ALARM_NOTIFY_WRAPPER}, which runs it once the user has calendar events or accounts.",
    ]
    return "\n".join(lines) + "\n"


def _override_autostart(context: MigrationContext) -> None:
    original = SYSTEM_AUTOSTART / ALARM_NOTIFY
    if original.is_file():
        override = alarm_notify_override(original.read_text(encoding="utf-8"))
        if override:
            _write(context, ALARM_NOTIFY_WRAPPER, ALARM_NOTIFY_WRAPPER_SCRIPT, 0o755)
            _write(context, CINNAMON_AUTOSTART / ALARM_NOTIFY, override)
        else:
            context.log(f"{original} has an unexpected Exec line; left alone")
    if (SYSTEM_AUTOSTART / PRINT_APPLET).is_file():
        _write(context, CINNAMON_AUTOSTART / PRINT_APPLET, PRINT_APPLET_OVERRIDE)


def _unit_installed(unit: str) -> bool:
    return any((directory / unit).exists() for directory in UNIT_DIRS)


def _disable_services(context: MigrationContext, running: bool) -> None:
    for unit in DISABLED_SERVICES:
        if not _unit_installed(unit):
            context.log(f"{unit} is not installed")
            continue
        context.ensure_service_disabled(unit)
        if running:
            context.run_command(["systemctl", "stop", "--no-block", unit], allow_fail=True)


def _configure_earlyoom(context: MigrationContext, running: bool) -> None:
    if not _unit_installed("earlyoom.service"):
        context.log("earlyoom is not installed")
        return
    changed = _write(context, EARLYOOM_DROPIN, EARLYOOM_DROPIN_CONFIG)
    context.ensure_service_enabled("earlyoom.service")
    if running and changed:
        context.run_command(["systemctl", "daemon-reload"], allow_fail=True)
        context.run_command(["systemctl", "restart", "--no-block", "earlyoom.service"], allow_fail=True)


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


def _flameshot_without_gl(context: MigrationContext, base: Path, owner: tuple[int, int] | None) -> None:
    path = base / FLAMESHOT_AUTOSTART
    if path.is_symlink() or not path.is_file():
        return
    directory = path.parent
    if owner is not None:
        # This runs as root in the user's home: write only inside it, into their own folder and file.
        resolved = directory.resolve()
        if (directory.is_symlink() or base.resolve() not in resolved.parents
                or resolved.stat().st_uid != owner[0] or path.stat().st_uid != owner[0]):
            context.log(f"skip {path}: not a regular file in a folder of its owner")
            return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    if FLAMESHOT_EXEC not in lines:
        return
    context.log(f"start Flameshot without OpenGL: {path}")
    if context.dry_run:
        return
    content = "\n".join(FLAMESHOT_EXEC_WITHOUT_GL if line == FLAMESHOT_EXEC else line for line in lines) + "\n"
    mode = path.stat().st_mode & 0o777
    # mkstemp creates a new file and os.replace renames over the name without following it.
    fd, staging = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(staging, mode)
        if owner is not None:
            os.chown(staging, *owner)
        os.replace(staging, path)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise


def _update_flameshot_autostart(context: MigrationContext) -> None:
    _flameshot_without_gl(context, SKEL, None)
    for info in _desktop_users():
        _flameshot_without_gl(context, Path(info.pw_dir), (info.pw_uid, info.pw_gid))


def _update_lotus_helpers(context: MigrationContext) -> None:
    # Only replace helpers CaramOS installed (migration v1_0_12 and the ISO hook).
    for path, content in ((LOTUS_SYSTEM_HELPER, LOTUS_SYSTEM_HELPER_SCRIPT),
                          (LOTUS_LOGIN_HELPER, LOTUS_LOGIN_HELPER_SCRIPT)):
        if path.is_file() and not path.is_symlink():
            _write(context, path, content, 0o755)
        else:
            context.log(f"{path} is not installed")


def run(context: MigrationContext) -> None:
    running = _systemd_running()
    _write(context, GTK_VISUALS_PRIMER, GTK_VISUALS_PRIMER_SCRIPT)
    _write(context, FCITX_CLASSICUI, FCITX_CLASSICUI_CONFIG)
    _override_autostart(context)
    _update_flameshot_autostart(context)
    _update_lotus_helpers(context)
    _disable_services(context, running)
    _configure_earlyoom(context, running)
