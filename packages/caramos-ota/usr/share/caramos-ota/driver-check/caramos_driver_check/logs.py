"""Logs attached to a help request, without personal data.

- What an app logged when something failed: the App Center and the Update Center save it to a file and open
  Feedback & Help with it (caramos-help --type bug --message TEXT --log FILE).
- system.log, made when the form opens: CaramOS and kernel, live USB or not, CPU, memory and swap, and the
  warnings and errors of this boot (and the end of the previous one, after a freeze and a restart).
- driver-check.log: what each Driver Check scan did and how long it took (note()).

The form attaches them, ticked, to the request's report, which only VNLF's moderators see, never the community
(HELP_API.md, "Logs"). A log keeps its last lines up to LOG_LIMIT, after e-mail and IP addresses, MAC
addresses, passwords and tokens, the home folder, the computer's name and the user's login name are taken out.
"""

from __future__ import annotations

import getpass
import os
import platform
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from caramos_ota_audit.redaction import redact_text

LOG_LIMIT = 24 * 1024     # characters kept of each log: its end, where the error is
READ_LIMIT = 256 * 1024   # bytes read from the end of a file
MAX_LOGS = 4
MIN_USER_NAME = 3         # shorter login names are also ordinary words
DRIVER_CHECK_LOG_LIMIT = 64 * 1024
JOURNAL_LINES = 200       # warnings and errors of this boot
PREVIOUS_BOOT_LINES = 60
JOURNAL_TIMEOUT = 10


@dataclass(frozen=True)
class Log:
    name: str   # the file's name, shown on the form
    text: str


def redact(text: str, user: str | None = None) -> str:
    text = redact_text(text, long_tokens=False)
    try:
        user = getpass.getuser() if user is None else user
    except (KeyError, OSError):
        user = ""
    if len(user) >= MIN_USER_NAME and user != "root":
        text = re.sub(rf"(?<![\w.-]){re.escape(user)}(?![\w-])", "[REDACTED_USER]", text)
    return text


def tail(text: str, limit: int = LOG_LIMIT) -> str:
    """The end of text in at most limit characters, starting at a line."""

    if len(text) <= limit:
        return text
    end = text[-limit:]
    newline = end.find("\n")
    return "…\n" + (end[newline + 1:] if 0 <= newline < len(end) - 1 else end)


def read_log(path: Path) -> Log | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - READ_LIMIT))
            data = handle.read()
    except OSError:
        return None
    text = tail(redact(data.decode("utf-8", errors="replace")).strip())
    return Log(path.name, text) if text else None


def read_logs(paths: Iterable[str | Path]) -> tuple[Log, ...]:
    logs = []
    for path in list(paths)[:MAX_LOGS]:
        log = read_log(Path(path))
        if log is not None:
            logs.append(log)
    return tuple(logs)


# Driver Check's own log --------------------------------------------------------------------------

def _cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def driver_check_log_path() -> Path:
    return _cache_dir() / "caramos-driver-check" / "driver-check.log"


def note(line: str, path: Path | None = None) -> None:
    """Add a line, after the time, to Driver Check's log, which keeps its last DRIVER_CHECK_LOG_LIMIT bytes."""

    path = path or driver_check_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} {line}\n")
        if path.stat().st_size > 2 * DRIVER_CHECK_LOG_LIMIT:
            path.write_text(tail(path.read_text(encoding="utf-8", errors="replace"), DRIVER_CHECK_LOG_LIMIT),
                            encoding="utf-8")
    except OSError:
        pass


# The computer ----------------------------------------------------------------------------------

def _gib(kib: int) -> str:
    return f"{kib / 1024 / 1024:.1f} GiB"


def _meminfo(path: Path) -> dict[str, int]:
    values = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, rest = line.partition(":")
            if rest.strip().split()[:1]:
                values[name] = int(rest.split()[0])
    except (OSError, ValueError):
        pass
    return values


def system_summary(root: Path = Path("/")) -> str:
    """One line: CPU, load, memory, swap and uptime."""

    model = ""
    try:
        for line in (root / "proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("model name"):
                model = " ".join(line.split(":", 1)[1].split())
                break
    except OSError:
        pass
    parts = [f"CPU {model or '?'}, {os.cpu_count() or '?'} threads"]
    try:
        parts.append("load " + " ".join((root / "proc/loadavg").read_text(encoding="utf-8").split()[:3]))
    except OSError:
        pass
    memory = _meminfo(root / "proc/meminfo")
    if memory.get("MemTotal"):
        parts.append(f"memory {_gib(memory['MemTotal'])}, {_gib(memory.get('MemAvailable', 0))} available")
    if "SwapTotal" in memory:
        parts.append(f"swap {_gib(memory['SwapTotal'])}, {_gib(memory.get('SwapFree', 0))} free")
    try:
        minutes = int(float((root / "proc/uptime").read_text(encoding="utf-8").split()[0]) // 60)
        parts.append(f"up {minutes} min")
    except (OSError, ValueError, IndexError):
        pass
    return " · ".join(parts)


def _journal(arguments: list[str], lines: int) -> str:
    try:
        done = subprocess.run(["journalctl", *arguments, "-p", "warning", "--no-pager", "-q", "-o", "short-iso",
                               "-n", str(lines)], check=False, capture_output=True, text=True, timeout=JOURNAL_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def system_log(root: Path = Path("/")) -> str:
    try:
        command_line = (root / "proc/cmdline").read_text(encoding="utf-8")
        flags = (root / "proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        command_line = flags = ""
    live = "boot=casper" in command_line.split()
    virtual = " hypervisor" in flags
    release = ""
    try:
        for line in (root / "etc/caramos-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("VERSION="):
                release = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    head = "\n".join([f"CaramOS {release or '?'} · Linux {platform.release()} · live USB: {'yes' if live else 'no'} · "
                      f"virtual machine: {'yes' if virtual else 'no'}", system_summary(root)])
    journal = redact(_journal(["-b", "0"], JOURNAL_LINES))
    # After a freeze the computer was restarted: how the previous boot ended. A live USB has no previous boot.
    previous = "" if live else redact(_journal(["-b", "-1"], PREVIOUS_BOOT_LINES))
    # The journal gives way, from its oldest lines, so that the computer's lines always fit LOG_LIMIT.
    room = LOG_LIMIT - len(head) - 200
    if previous:
        previous = tail(previous, room // 3)
        room -= len(previous)
    lines = [head, "", f"== Warnings and errors of this boot (last {JOURNAL_LINES}) ==",
             tail(journal, max(room, 1000)) or "(none readable)"]
    if previous:
        lines += ["", f"== End of the previous boot (warnings and errors, last {PREVIOUS_BOOT_LINES}) ==", previous]
    return "\n".join(lines) + "\n"


def system_logs() -> tuple[Log, ...]:
    """What a help request carries about the computer: system.log, and driver-check.log when Driver Check ran."""

    found = [Log("system.log", system_log().strip())]
    driver_check = read_log(driver_check_log_path())
    if driver_check is not None:
        found.append(driver_check)
    return tuple(found)
