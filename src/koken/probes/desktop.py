# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The desktop session, read from the environment it was started with.

This is the one probe whose source is the process environment rather than a
file, because that is where the answer genuinely lives: a Wayland session is a
Wayland session because ``WAYLAND_DISPLAY`` is set and a compositor is
listening on that socket, not because anything wrote it down.

KOKEN knows nothing about any particular desktop and must not learn. Names are
reported as the session set them and are not translated into a list of
shells this application has heard of.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import toolkits
from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    Probe,
    Section,
    fmt_list,
    natural_key,
    or_missing,
    path_exists,
    read_first_line,
)

# How many lines one expansion will carry before it says how many it left out.
# A desktop with four hundred applications lists all four hundred; the cap is
# there for the directory that holds five thousand.
MAX_LISTED = 500

# What each evidence source is called on the page, in the order it is tried.
SOURCE_NAMES = (
    ("markers", "settled by the files sitting beside the binary"),
    ("libraries", "settled by the libraries the binary links"),
    ("scripts", "settled by the script the entry points at"),
    ("flatpak", "settled by Flatpak metadata"),
    ("packages", "settled by the package database"),
    ("unresolved", "named a command that is not installed"),
    ("network", "sat on a network filesystem and were not visited"),
)

# Read for the Overview section, in this order.
SESSION_VARIABLES = (
    ("XDG_CURRENT_DESKTOP", "Desktop", "current_desktop"),
    ("XDG_SESSION_DESKTOP", "Session desktop", "session_desktop"),
    ("DESKTOP_SESSION", "Session name", "desktop_session"),
    ("XDG_SESSION_TYPE", "Session type", "session_type"),
)

# Read for the Session section.
SESSION_DETAIL = (
    ("XDG_SESSION_ID", "Session id", "session_id"),
    ("XDG_SESSION_CLASS", "Session class", "session_class"),
    ("XDG_SEAT", "Seat", "seat"),
    ("XDG_VTNR", "Virtual terminal", "vtnr"),
    ("XDG_RUNTIME_DIR", "Runtime directory", "runtime_dir"),
    ("XDG_CONFIG_HOME", "Config directory", "config_home"),
    ("XDG_DATA_HOME", "Data directory", "data_home"),
    ("SHELL", "Login shell", "shell"),
    ("TERM", "Terminal type", "term"),
    ("LANG", "Language", "lang"),
    ("QT_QPA_PLATFORM", "Qt platform override", "qt_platform"),
    ("GDK_BACKEND", "GTK backend override", "gdk_backend"),
)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _colour_scheme_reading() -> tuple[str, str]:
    """Ask the theme layer what the portal said. Never raises."""
    try:
        from ..theme import describe_colour_scheme

        return describe_colour_scheme()
    except Exception:
        return "dark", ""


class DesktopProbe(Probe):
    branch = "system"
    id = "desktop"
    label = "Desktop"

    def sections(self) -> list[Section]:
        return [self._overview(), self._session(), self._toolkits()]

    # -- overview ---------------------------------------------------------

    def _overview(self) -> Section:
        section = Section(id="overview", label="Overview")

        wayland = _env("WAYLAND_DISPLAY")
        x11 = _env("DISPLAY")
        session_type = _env("XDG_SESSION_TYPE")

        if not any((wayland, x11, session_type, _env("XDG_CURRENT_DESKTOP"))):
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "No desktop session is described in this process's environment. "
                    "KÖKEN was started from a plain terminal, a service, or over SSH.",
                )
            )

        for name, label, field in SESSION_VARIABLES:
            value = _env(name)
            section.add(
                self.row(
                    field,
                    label,
                    or_missing(value, f"{name} is not set"),
                )
            )

        if wayland:
            socket_path = _wayland_socket(wayland)
            detail = wayland
            if socket_path is not None:
                detail += f" — socket at {socket_path}"
            section.add(self.row("wayland_display", "Wayland display", detail))
        else:
            section.add(
                self.row(
                    "wayland_display",
                    "Wayland display",
                    "Not set, so this is not a Wayland session",
                )
            )

        section.add(
            self.row(
                "x11_display",
                "X display",
                or_missing(x11, "Not set, so no X server is reachable from here"),
            )
        )
        if x11 and wayland:
            section.add(
                self.row(
                    "xwayland",
                    "Xwayland",
                    "Both a Wayland and an X display are set, so X applications are "
                    "being served through Xwayland",
                )
            )
        return section

    # -- session ----------------------------------------------------------

    def _session(self) -> Section:
        section = Section(id="session", label="Session")

        for name, label, field in SESSION_DETAIL:
            value = _env(name)
            if value is None:
                continue
            section.add(self.row(field, label, value))

        section.add(
            self.row("user", "User", or_missing(_current_user(), NOT_AVAILABLE))
        )
        section.add(self.row("home", "Home directory", or_missing(_env("HOME"), NOT_AVAILABLE)))

        portal = _portal_state()
        section.add(self.row("portal", "Desktop portal", portal))

        # What the portal answered for the appearance setting, not merely
        # whether a portal exists. A shell switched to light while KOKEN stays
        # dark is either a portal that says dark or a portal that was never
        # asked, and those need different fixes.
        variant, reason = _colour_scheme_reading()
        section.add(
            self.row(
                "colour_scheme",
                "Colour scheme",
                {"light": "Light", "dark": "Dark"}.get(variant, variant),
                body=reason,
            )
        )

        section.add(
            self.row(
                "dbus_session",
                "Session bus",
                or_missing(_env("DBUS_SESSION_BUS_ADDRESS"), "Not advertised in the environment"),
            )
        )
        section.add(
            self.row(
                "dbus_system",
                "System bus",
                "/run/dbus/system_bus_socket is present"
                if path_exists("/run/dbus/system_bus_socket")
                else "No system bus socket found at /run/dbus/system_bus_socket",
            )
        )

        flatpak = path_exists("/.flatpak-info")
        section.add(
            self.row(
                "flatpak",
                "Flatpak sandbox",
                "Running inside a Flatpak sandbox"
                if flatpak
                else "Not running inside a Flatpak sandbox",
            )
        )

        locales = [
            f"{name}={value}"
            for name, value in sorted(os.environ.items())
            if name.startswith("LC_") and value
        ]
        section.add(
            self.row(
                "locale",
                "Locale overrides",
                fmt_list(locales, empty="None set beyond LANG"),
            )
        )
        return section

    # -- toolkits ---------------------------------------------------------

    def _toolkits(self) -> Section:
        """What every installed application is built on, and what is installed.

        One row per toolkit, not one row per application. A desktop holds
        anywhere between five and four hundred desktop entries, and this list
        stops being readable at about sixty rows, so the roll call lives inside
        one row's expansion and the section itself carries the distribution -
        which is the more useful fact in any case. Nobody wants to read four
        hundred names; the question is what this desktop is made of. The shape
        is the same fourteen rows on a machine with four hundred applications
        and on one with none.

        Every toolkit is listed whether or not it is installed and whether or
        not anything uses it, the same way an absent battery is stated rather
        than hidden. "GTK 2 is not installed" is an answer.
        """
        section = Section(id="toolkits", label="Toolkits")
        try:
            survey = toolkits.survey()
        except Exception as exc:  # deliberately broad: a section, not a crash
            section.add(
                self.row(
                    "toolkit_scan",
                    "Applications scanned",
                    "The installed applications could not be read on this "
                    f"machine ({type(exc).__name__}).",
                )
            )
            return section

        counts = survey.counts
        total = len(survey.applications)
        order = sorted(toolkits.TOOLKITS, key=lambda item: (-counts[item.key], item.rank))

        section.add(self._scan_row(survey, total))
        section.add(self._mix_row(survey, counts, total))
        for toolkit in order:
            section.add(self._toolkit_row(toolkit, survey, counts[toolkit.key]))
        section.add(self._list_row(survey, order, total))
        section.add(self._unclassified_row(survey))
        section.add(
            self.row(
                "toolkit_method",
                "Detection",
                "Read from the files on disk — nothing was started",
            )
        )
        return section

    def _scan_row(self, survey, total: int):
        """Where the entries came from, how they were settled, what was missed."""
        lines = [
            f"{directory} — {_plural(count, 'entry', 'entries')}"
            for directory, count in survey.directories
        ]
        if not lines:
            lines.append("No applications directory exists on this machine.")

        notes = []
        if survey.hidden:
            notes.append(
                f"{_plural(survey.hidden, 'entry', 'entries')} marked NoDisplay or "
                "Hidden, which a desktop does not show in its menu, counted and "
                "not classified"
            )
        if survey.duplicates:
            notes.append(
                f"{_plural(survey.duplicates, 'entry', 'entries')} shadowed by an "
                "entry of the same name in an earlier directory"
            )
        if survey.skipped:
            notes.append(
                f"{_plural(survey.skipped, 'entry', 'entries')} left unread when "
                f"the scan's {toolkits.TIME_BUDGET:.2f} second budget ran out"
            )
        if survey.network_skipped:
            notes.append(
                f"{survey.network_skipped} path(s) on a network filesystem, which "
                "are never visited: a server that has stopped answering turns "
                "reading a file into an unbounded wait"
            )
        sources = [
            f"{count} {phrase}"
            for key, phrase in SOURCE_NAMES
            if (count := survey.sources.get(key, 0))
        ]
        body = _body(
            "Every desktop entry found, and where. A desktop entry is a text "
            "file naming a program, an icon and the command that starts it. "
            "KÖKEN reads the command, follows it to the file it really names - "
            "usually through a wrapper script or two - and reads that file. "
            "Nothing on this page was started to produce it.",
            lines + _titled("How each was settled", sources) + _titled("Not counted", notes),
        )
        return self.row(
            "toolkit_scan",
            "Applications scanned",
            str(total),
            gloss=(
                f"from {_plural(len(survey.directories), 'directory', 'directories')}"
                if survey.directories
                else "no applications directory exists here"
            ),
            body=body,
        )

    def _mix_row(self, survey, counts: dict, total: int):
        used = [key for key, count in counts.items() if count]
        if not total:
            return self.row("toolkit_mix", "Toolkit mix", "No applications found")
        leader = max(used, key=lambda key: counts[key]) if used else ""
        gloss = ""
        if leader:
            label = toolkits.TOOLKIT_BY_KEY[leader].label
            gloss = f"{label} leads, with {counts[leader]} of {total} applications"
        return self.row(
            "toolkit_mix",
            "Toolkit mix",
            f"{_plural(len(used), 'toolkit', 'toolkits')} in use",
            gloss=gloss,
        )

    def _toolkit_row(self, toolkit, survey, count: int):
        """One toolkit: the version on disk, and how much of the machine uses it.

        The value is the version rather than the count because the version is
        what somebody would paste into a search, and the copy control hands
        over the value. The count is the gloss beside it.
        """
        installed = survey.installed.get(toolkit.key) or toolkits.Installed()
        if installed.version:
            value = installed.version
        elif installed.present:
            value = "Installed, version not stated by the file name"
        elif toolkit.key == "electron":
            value = "No system-wide copy"
        else:
            value = "Not installed"
        if count:
            gloss = _plural(count, "application", "applications")
        elif installed.present:
            gloss = "installed, nothing here uses it"
        else:
            gloss = "and nothing here uses it"
        return self.row(f"toolkit_{toolkit.key}", toolkit.label, value, gloss=gloss)

    def _list_row(self, survey, order, total: int):
        """The roll call, grouped, in one expansion instead of four hundred rows."""
        lines: list[str] = []
        listed = 0
        remaining = 0
        for toolkit in order:
            apps = survey.by_toolkit(toolkit.key)
            if not apps:
                continue
            if listed >= MAX_LISTED:
                remaining += len(apps)
                continue
            if lines:
                lines.append("")
            lines.append(
                f"{toolkit.label} — {_plural(len(apps), 'application', 'applications')}"
            )
            for app in sorted(apps, key=lambda item: natural_key(item.name.lower())):
                if listed >= MAX_LISTED:
                    remaining += 1
                    continue
                lines.append(f"  {app.describe()}")
                listed += 1
        if remaining:
            lines.append("")
            lines.append(f"{remaining} further application(s) not listed here.")

        classified = total - len(survey.unclassified)
        if not classified:
            return self.row(
                "toolkit_applications",
                "Application list",
                "Nothing was identified on this machine",
            )
        return self.row(
            "toolkit_applications",
            "Application list",
            f"{classified} identified — expand for the full list",
            body=_body(
                "Every application that was placed, grouped by what it is built "
                "on, each with the evidence it was placed by. An application "
                "linking a second toolkit as well is marked; that is ordinary, "
                "and says which one draws the window rather than which one is "
                "loaded.",
                lines,
            ),
        )

    def _unclassified_row(self, survey):
        rest = survey.unclassified
        if not rest:
            return self.row(
                "toolkit_unclassified",
                "Not classified",
                "0",
                gloss="every application was placed",
            )
        return self.row(
            "toolkit_unclassified",
            "Not classified",
            str(len(rest)),
            gloss=_plural(len(rest), "application", "applications"),
            body=_body(
                "Applications whose toolkit the files do not state. There are "
                "four ordinary reasons and none of them is a fault: the program "
                "loads its toolkit with dlopen at runtime rather than linking it, "
                "which is how LibreOffice and anything with a plugin-based "
                "interface layer works; its wrapper computes the path it starts "
                "rather than writing it down; it is written in a language whose "
                "own runtime carries the widgets; or the command the entry names "
                "is not installed. KÖKEN names these rather than guessing at "
                "them - a wrong answer here would be worse than none.",
                [f"  {app.describe()}" for app in sorted(
                    rest, key=lambda item: natural_key(item.name.lower())
                )][:MAX_LISTED],
            ),
        )


def _current_user() -> str | None:
    for name in ("USER", "LOGNAME"):
        value = _env(name)
        if value:
            return value
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return None


def _wayland_socket(display: str) -> str | None:
    """Where the compositor is listening, when the path can be worked out."""
    if display.startswith("/"):
        return display if path_exists(display) else None
    runtime = _env("XDG_RUNTIME_DIR")
    if not runtime:
        return None
    candidate = str(Path(runtime) / display)
    return candidate if path_exists(candidate) else None


def _portal_state() -> str:
    """Whether xdg-desktop-portal is around to answer the appearance query.

    This matters to KOKEN specifically: the portal is how the active palette is
    chosen, and a session without one falls back to the dark palette.
    """
    for candidate in (
        "/usr/libexec/xdg-desktop-portal",
        "/usr/lib/xdg-desktop-portal",
        "/usr/lib/xdg-desktop-portal/xdg-desktop-portal",
    ):
        if path_exists(candidate):
            return (
                "xdg-desktop-portal is installed. KÖKEN asks it whether the system "
                "is set to light or dark."
            )
    return (
        "xdg-desktop-portal was not found. KÖKEN cannot ask whether the system is "
        "set to light or dark, and uses its dark palette."
    )


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _titled(title: str, lines: list[str]) -> list[str]:
    """A titled block inside a row body, or nothing when it would be empty."""
    if not lines:
        return []
    return ["", f"{title}:"] + [f"  {line}" for line in lines]


def _body(lead: str, lines: list[str]) -> str:
    """A row's own expansion: a paragraph of context, then the machine's list."""
    if not lines:
        return lead
    return lead + "\n\n" + "\n".join(lines)
