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

from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    Probe,
    Section,
    fmt_list,
    or_missing,
    path_exists,
    read_first_line,
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
        return [self._overview(), self._session()]

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
