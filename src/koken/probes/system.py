# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The operating system: what is running, which distribution it is, and About.

The About section lives here rather than in a dialog, because KOKEN has no
dialogs. It is an ordinary row 3 entry with ordinary rows, and the licence text
sits in the expansion body of one of them - the same in-place expansion every
other row in the application uses.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from .. import MAKER, RELEASE_DATE, SOURCE, SPDX, SUBTITLE, VERSION
from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    VOLATILE,
    Probe,
    Section,
    fmt_duration,
    fmt_list,
    or_missing,
    path_exists,
    read_first_line,
    read_lines,
)

OS_RELEASE_PATHS = ("/etc/os-release", "/usr/lib/os-release")

# Searched in order for the verbatim GPL-3 text. The first two are where this
# project's own packages install it; the third is Debian's shared copy, which
# is byte-identical; the last covers running from a source checkout.
LICENCE_PATHS = (
    "/usr/share/licenses/koken/LICENSE",
    "/usr/share/doc/koken/LICENSE",
    "/usr/share/common-licenses/GPL-3",
)

# os-release fields worth a row, in the order they are shown.
OS_RELEASE_FIELDS = (
    ("PRETTY_NAME", "Name", "pretty_name"),
    ("NAME", "Short name", "name"),
    ("ID", "Identifier", "distro_id"),
    ("ID_LIKE", "Derived from", "id_like"),
    ("VERSION", "Version", "version"),
    ("VERSION_ID", "Version identifier", "version_id"),
    ("VERSION_CODENAME", "Codename", "codename"),
    ("BUILD_ID", "Build", "build_id"),
    ("VARIANT", "Variant", "variant"),
    ("ANSI_COLOR", "Terminal colour", "ansi_color"),
)


def parse_os_release(lines) -> dict[str, str]:
    """``ID=arch`` and ``PRETTY_NAME="Arch Linux"`` into a dict, quotes removed."""
    out: dict[str, str] = {}
    for line in lines:
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def find_licence_text() -> str | None:
    for candidate in LICENCE_PATHS:
        try:
            text = Path(candidate).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        if "GNU GENERAL PUBLIC LICENSE" in text:
            return text
    # A source checkout: src/koken/probes/system.py -> repository root.
    try:
        local = Path(__file__).resolve().parents[3] / "LICENSE"
        text = local.read_text(encoding="utf-8", errors="replace")
        if "GNU GENERAL PUBLIC LICENSE" in text:
            return text
    except (OSError, ValueError, IndexError):
        pass
    return None


class SystemProbe(Probe):
    branch = "system"
    id = "os"
    label = "Operating system"

    def sections(self) -> list[Section]:
        return [self._overview(), self._distribution(), self._init(), self._about()]

    # -- overview ---------------------------------------------------------

    def _overview(self) -> Section:
        section = Section(id="overview", label="Overview")
        release = parse_os_release(self._os_release_lines())

        section.add(
            self.row(
                "pretty_name",
                "Distribution",
                or_missing(release.get("PRETTY_NAME") or release.get("NAME"), NOT_REPORTED),
            )
        )
        section.add(self.row("hostname", "Host name", _hostname()))
        section.add(self.row("architecture", "Architecture", platform.machine() or NOT_AVAILABLE))
        section.add(
            self.row("kernel_release", "Kernel", or_missing(platform.release(), NOT_AVAILABLE))
        )
        for row in self._uptime_rows():
            section.add(row)
        section.add(
            self.row(
                "python",
                "Python",
                f"{platform.python_version()} — the version KÖKEN itself is running on",
            )
        )
        section.add(
            self.row(
                "libc",
                "C library",
                fmt_list(part for part in platform.libc_ver() if part) or NOT_REPORTED,
            )
        )
        return section

    def _os_release_lines(self):
        for candidate in OS_RELEASE_PATHS:
            lines = read_lines(candidate)
            if lines:
                return lines
        return []

    def _uptime_rows(self) -> list:
        rows = []
        uptime = read_first_line("/proc/uptime")
        seconds = None
        if uptime:
            parts = uptime.split()
            try:
                seconds = float(parts[0])
            except (ValueError, IndexError):
                seconds = None
        rows.append(
            self.row(
                "uptime",
                "Uptime",
                fmt_duration(seconds) if seconds is not None else NOT_AVAILABLE,
                tier=VOLATILE,
            )
        )
        load = read_first_line("/proc/loadavg")
        if load:
            parts = load.split()
            if len(parts) >= 4:
                rows.append(
                    self.row(
                        "loadavg",
                        "Load average",
                        f"{parts[0]} · {parts[1]} · {parts[2]} over 1, 5 and 15 minutes",
                        tier=VOLATILE,
                    )
                )
                # parts[3] is "runnable/total", as in "2/1147".
                runnable, _, total = parts[3].partition("/")
                rows.append(
                    self.row(
                        "processes",
                        "Processes",
                        f"{runnable} runnable of {total} total" if total else parts[3],
                        tier=VOLATILE,
                    )
                )
        return rows

    # -- distribution -----------------------------------------------------

    def _distribution(self) -> Section:
        section = Section(id="distribution", label="Distribution")
        lines = self._os_release_lines()
        release = parse_os_release(lines)
        if not release:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "Neither /etc/os-release nor /usr/lib/os-release could be read, "
                    "so the distribution cannot be identified.",
                )
            )
            return section

        for key, label, field in OS_RELEASE_FIELDS:
            value = release.get(key)
            if not value:
                continue
            section.add(self.row(field, label, value))

        # Addresses are shown as plain selectable text. KOKEN opens no browser.
        for key, label in (
            ("HOME_URL", "Home address"),
            ("SUPPORT_URL", "Support address"),
            ("BUG_REPORT_URL", "Bug report address"),
            ("DOCUMENTATION_URL", "Documentation address"),
        ):
            value = release.get(key)
            if value:
                section.add(self.row("distro_address", label, value, key=f"addr{key}"))

        section.add(
            self.row(
                "os_release_source",
                "Read from",
                OS_RELEASE_PATHS[0] if read_lines(OS_RELEASE_PATHS[0]) else OS_RELEASE_PATHS[1],
            )
        )
        return section

    # -- init -------------------------------------------------------------

    def _init(self) -> Section:
        section = Section(id="init", label="Init")
        comm = read_first_line("/proc/1/comm")
        section.add(
            self.row("init_process", "Process 1", or_missing(comm, NOT_AVAILABLE))
        )

        systemd = path_exists("/run/systemd/system")
        section.add(
            self.row(
                "init_system",
                "Init system",
                "systemd" if systemd else _guess_init(comm),
            )
        )
        if systemd:
            section.add(
                self.row(
                    "systemd_units",
                    "Unit directory",
                    "/run/systemd/system is present, so systemd is managing this session",
                )
            )

        cgroup = read_first_line("/proc/self/cgroup")
        if cgroup:
            section.add(
                self.row(
                    "cgroup",
                    "Control group",
                    cgroup.split(":", 2)[-1] if ":" in cgroup else cgroup,
                )
            )
        section.add(
            self.row(
                "cgroup_version",
                "Control group version",
                "Version 2 (unified)"
                if path_exists("/sys/fs/cgroup/cgroup.controllers")
                else "Version 1, or not mounted",
            )
        )

        container = _container_hint()
        section.add(
            self.row(
                "container",
                "Container",
                container or "Not running inside a container that announces itself",
            )
        )
        return section

    # -- about ------------------------------------------------------------

    def _about(self) -> Section:
        """CORE section 14. Not a dialog, not a popup - a row 3 entry."""
        section = Section(id="about", label="About")
        section.add(self.row("about_name", "Name", f"KÖKEN — {SUBTITLE}"))
        section.add(self.row("about_maker", "Maker", MAKER))
        section.add(self.row("about_version", "Version", f"v{VERSION}"))
        section.add(self.row("about_released", "Released", RELEASE_DATE))
        section.add(self.row("about_source", "Source", SOURCE))
        section.add(self.row("about_licence", "Licence", SPDX))

        licence = find_licence_text()
        if licence:
            section.add(
                self.row(
                    "about_licence_text",
                    "Licence text",
                    "GNU General Public License, version 3 — expand to read in full",
                    body=licence,
                )
            )
        else:
            section.add(
                self.row(
                    "about_licence_text",
                    "Licence text",
                    "The full text ships with the package and could not be found on "
                    "this machine. It is in the LICENSE file alongside the source.",
                )
            )
        section.add(
            self.row(
                "about_privacy",
                "What it does",
                "Reads this machine and explains what it finds. It opens no network "
                "connection of any kind, and the only thing it ever changes is "
                "mounting or unmounting a filesystem when you ask it to.",
            )
        )
        return section

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        return {"overview": self._uptime_rows()}


def _hostname() -> str:
    """The machine's name, from the kernel and never from a resolver.

    os.uname() is a plain syscall. socket.gethostname() would answer the same
    question, but importing the socket module at all is the one thing in this
    package a reader would have to stop and check, and there is no reason to
    make them.
    """
    name = read_first_line("/proc/sys/kernel/hostname")
    if name:
        return name
    try:
        return os.uname().nodename or NOT_AVAILABLE
    except (OSError, AttributeError):
        return NOT_AVAILABLE


def _guess_init(comm: str | None) -> str:
    if not comm:
        return NOT_AVAILABLE
    known = {
        "systemd": "systemd",
        "init": "SysV init or a compatible replacement",
        "openrc-init": "OpenRC",
        "runit": "runit",
        "s6-svscan": "s6",
        "dinit": "dinit",
    }
    return known.get(comm, comm)


def _container_hint() -> str | None:
    """Whether something is announcing that this is not the bare machine."""
    for path, label in (
        ("/run/.containerenv", "Podman"),
        ("/.dockerenv", "Docker"),
        ("/run/host/container-manager", "Flatpak host container"),
    ):
        if path_exists(path):
            return label
    value = os.environ.get("container")
    if value:
        return value
    return None
