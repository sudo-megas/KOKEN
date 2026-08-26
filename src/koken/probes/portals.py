# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Which portal backend answers which interface, and how that was decided.

xdg-desktop-portal is a front desk, not a service. An application asks it for a
file dialog, a screenshot, or the light-or-dark preference, and the portal hands
the request to a *backend* - a separate program shipped by a desktop, declaring
in a small file which interfaces it can answer. Having four backends installed
is ordinary: a Wayland compositor pulls in one for screen capture, a toolkit
pulls in another for file dialogs, a distribution ships a third by default.

What is not ordinary is the wrong one holding an interface. A backend written
for one desktop, running under another, answers from configuration that is not
there - and it answers confidently, because from the portal's side it did its
job. The interface where that bites hardest is ``Settings``, which carries
``org.freedesktop.appearance color-scheme``: every application that follows the
system light or dark setting follows whatever that one backend says.

Nothing on a running machine writes down which backend holds which interface.
This section works it out from the same files xdg-desktop-portal reads, in the
same order, and says for every interface both who is assigned and *how* that was
decided - named for that interface, inherited from the ``default`` line, or not
configured at all. Those are three different situations and a fix that suits one
does nothing for the others.

It reads files only. What the portal currently *answers* is a different question
from who is configured to answer it, and the Desktop Overview already reports
the colour scheme the running portal returned.

The precedence walk here is portals.conf(5) written out: the locations in order,
and inside each location the desktop-specific names before the plain one. The
same walk, keyed the same way by ``XDG_CURRENT_DESKTOP``, decides which
``mimeapps.list`` files apply, so :mod:`koken.probes.filetypes` imports it from
here rather than writing it a second time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .base import (
    NORMAL,
    WARNING,
    Row,
    Section,
    resolve,
    read_text,
)

# Where a portal backend declares itself, and what the interface names look
# like. The prefix is stripped for display: a row labelled "Settings" is easier
# to scan than one labelled org.freedesktop.impl.portal.Settings, and the full
# name is in the row's expansion.
PORTAL_SUBDIR = "xdg-desktop-portal"
CONFIG_NAME = "portals.conf"
IMPL_PREFIX = "org.freedesktop.impl.portal."
SETTINGS_INTERFACE = IMPL_PREFIX + "Settings"

# xdg-desktop-portal's own last resort: with nothing else selected it tries the
# GTK backend, which historically served desktops with no backend of their own.
GTK_BUS_NAME = IMPL_PREFIX + "desktop.gtk"

# The two special values a preference list may hold.
ANY = "*"
NONE = "none"


# --------------------------------------------------------------------------
# The XDG precedence walk
# --------------------------------------------------------------------------
#
# Shared with filetypes.py, which resolves mimeapps.list through exactly this
# machinery. Both specifications say the same thing in different words: a list
# of locations in precedence order, and within each location a set of
# desktop-specific filenames - built from XDG_CURRENT_DESKTOP, lower-cased,
# most specific first - ahead of the plain one.
#
# Every path here goes through base.resolve(), so a fake root reaches all of it
# and the environment variables can be set to whatever the test needs.


def _absolute(value: str | None) -> str | None:
    """A directory from the environment, or None if unset or relative.

    The base directory specification says a relative value is invalid and must
    be treated as unset, which matters: a relative path here would silently
    address whatever directory the application happened to start in.
    """
    text = (value or "").strip()
    return text if text.startswith("/") else None


def _home() -> str | None:
    """The home directory, for the defaults that are built from it."""
    home = _absolute(os.environ.get("HOME"))
    if home:
        return home
    try:
        import pwd

        return _absolute(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return None


def config_home() -> str | None:
    """``$XDG_CONFIG_HOME``, or ``~/.config``."""
    home = _absolute(os.environ.get("XDG_CONFIG_HOME"))
    if home:
        return home
    base = _home()
    return f"{base}/.config" if base else None


def config_dirs() -> list[str]:
    """``$XDG_CONFIG_DIRS``, or ``/etc/xdg``, in order."""
    raw = (os.environ.get("XDG_CONFIG_DIRS") or "").strip() or "/etc/xdg"
    return [part for part in (p.strip() for p in raw.split(":")) if part.startswith("/")]


def data_home() -> str | None:
    """``$XDG_DATA_HOME``, or ``~/.local/share``."""
    home = _absolute(os.environ.get("XDG_DATA_HOME"))
    if home:
        return home
    base = _home()
    return f"{base}/.local/share" if base else None


def data_dirs() -> list[str]:
    """``$XDG_DATA_DIRS``, or ``/usr/local/share:/usr/share``, in order."""
    raw = (os.environ.get("XDG_DATA_DIRS") or "").strip() or "/usr/local/share:/usr/share"
    return [part for part in (p.strip() for p in raw.split(":")) if part.startswith("/")]


def current_desktops() -> list[str]:
    """The names in ``XDG_CURRENT_DESKTOP``, lower-cased, most specific first.

    ``XDG_CURRENT_DESKTOP=Budgie:GNOME`` searches for ``budgie-`` files before
    ``gnome-`` files before the plain one. Duplicates are dropped so a session
    that names itself twice does not double every search.
    """
    raw = os.environ.get("XDG_CURRENT_DESKTOP") or ""
    out: list[str] = []
    for part in raw.split(":"):
        name = part.strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def dedupe(paths) -> list[str]:
    """Paths in order with repeats dropped, keeping the first of each.

    ``/usr/share`` is both the last entry of the default ``XDG_DATA_DIRS`` and
    the build-time data directory searched after it, so without this the same
    file would be searched, and reported, twice.
    """
    out: list[str] = []
    for path in paths:
        if path and path not in out:
            out.append(path)
    return out


def is_file(path: str) -> bool:
    """Whether *path* is a readable file under the active root. Never raises."""
    try:
        return resolve(path).is_file()
    except (OSError, ValueError):
        return False


@dataclass(frozen=True)
class Candidate:
    """One place a configuration file may be, and whether it is there.

    ``desktop`` is the desktop name whose file this is, or None for the
    desktop-independent name. Keeping the whole list, present or not, is what
    lets the section print the search itself rather than only its answer.
    """

    path: str
    directory: str
    desktop: str | None
    exists: bool

    @property
    def kind(self) -> str:
        if self.desktop is None:
            return "the desktop-independent file"
        return f"the file for the {self.desktop} desktop"


def search(directories, basename: str, desktops=None) -> list[Candidate]:
    """Every candidate path for *basename*, highest precedence first.

    Within each directory the desktop-specific names come first, in
    ``XDG_CURRENT_DESKTOP`` order, then the plain name. That inner order is the
    part people misremember: a desktop-specific file beats a plain one *in the
    same directory*, and never reaches across directories to beat a plain file
    in a higher-precedence one.
    """
    names = current_desktops() if desktops is None else list(desktops)
    out: list[Candidate] = []
    for directory in dedupe(directories):
        for desktop in names:
            path = f"{directory}/{desktop}-{basename}"
            out.append(Candidate(path, directory, desktop, is_file(path)))
        path = f"{directory}/{basename}"
        out.append(Candidate(path, directory, None, is_file(path)))
    return out


# --------------------------------------------------------------------------
# The file format
# --------------------------------------------------------------------------
#
# Both file formats in this pair of sections are the desktop-entry ini dialect:
# groups in square brackets, key=value lines, '#' comments. Parsed here rather
# than with configparser, which lower-cases keys by default - which would turn
# org.freedesktop.impl.portal.Settings into something that matches nothing - and
# which raises on the duplicate keys a hand-edited file collects.


def parse_ini(path: str) -> dict[str, dict[str, str]]:
    """``{group: {key: value}}``. An absent or unreadable file reads as empty.

    Later keys win within a group, which is what GLib's key file reader does,
    so a file with the same interface named twice reports the value that takes
    effect rather than the one written first.
    """
    text = read_text(path)
    if text is None:
        return {}
    groups: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            current = groups.setdefault(name, {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    return groups


def semicolon_list(value: str | None) -> list[str]:
    """``a;b;c;`` into ``[a, b, c]``. Empty entries and spacing are dropped."""
    if not value:
        return []
    return [item.strip() for item in value.split(";") if item.strip()]


def short_interface(name: str) -> str:
    """``org.freedesktop.impl.portal.Settings`` shown as ``Settings``."""
    return name[len(IMPL_PREFIX):] if name.startswith(IMPL_PREFIX) else name


# --------------------------------------------------------------------------
# Installed backends
# --------------------------------------------------------------------------


@dataclass
class Backend:
    """One ``.portal`` file: a backend, and what it says it can answer."""

    name: str
    path: str
    bus_name: str = ""
    interfaces: list[str] = field(default_factory=list)
    use_in: list[str] = field(default_factory=list)
    problem: str = ""

    @property
    def usable(self) -> bool:
        return not self.problem

    def implements(self, interface: str) -> bool:
        return interface in self.interfaces

    def suits(self, desktops) -> bool:
        """Whether ``UseIn`` names one of the current desktops.

        A backend with no ``UseIn`` at all makes no claim either way and is not
        reported as unsuited; the key is deprecated and plenty of current
        backends have dropped it.
        """
        if not self.use_in:
            return True
        lowered = {name.lower() for name in self.use_in}
        return any(desktop in lowered for desktop in desktops)


def backend_directories() -> list[str]:
    """Where ``.portal`` files live, highest precedence first."""
    bases = [data_home()] + data_dirs() + ["/usr/share"]
    return dedupe(f"{base}/{PORTAL_SUBDIR}/portals" for base in bases if base)


def _portal_files(directory: str) -> list[str]:
    """Every ``*.portal`` in *directory*, in name order. Never raises."""
    try:
        entries = sorted(
            entry.name for entry in resolve(directory).iterdir() if entry.name.endswith(".portal")
        )
    except (OSError, ValueError):
        return []
    return [f"{directory}/{name}" for name in entries]


def read_backend(path: str) -> Backend:
    """One ``.portal`` file, whether or not it is well formed."""
    name = path.rsplit("/", 1)[-1]
    name = name[: -len(".portal")] if name.endswith(".portal") else name
    backend = Backend(name=name, path=path)
    groups = parse_ini(path)
    portal = groups.get("portal")
    if portal is None:
        backend.problem = "the file has no [portal] group"
        return backend
    backend.bus_name = portal.get("DBusName", "").strip()
    backend.interfaces = semicolon_list(portal.get("Interfaces"))
    backend.use_in = semicolon_list(portal.get("UseIn"))
    if not backend.bus_name:
        backend.problem = "the file names no DBusName"
    elif not backend.interfaces:
        backend.problem = "the file lists no Interfaces"
    else:
        stray = [item for item in backend.interfaces if not item.startswith(IMPL_PREFIX)]
        if stray:
            backend.problem = f"it lists {stray[0]}, which is not a portal backend interface"
    return backend


def load_backends() -> list[Backend]:
    """Every installed backend, in the order xdg-desktop-portal considers them.

    Two rules, both taken from the portal's own loader. A name found in a
    higher-precedence directory wins, so a backend dropped into the home
    directory replaces the system copy rather than joining it. And the order is
    by ``UseIn`` first - a backend that names the current desktop is considered
    before one that does not - then by file name, which is the lexicographic
    order the ``*`` preference value refers to.
    """
    desktops = current_desktops()
    found: dict[str, Backend] = {}
    for directory in backend_directories():
        for path in _portal_files(directory):
            backend = read_backend(path)
            found.setdefault(backend.name, backend)

    def order(backend: Backend) -> tuple:
        lowered = {name.lower() for name in backend.use_in}
        rank = len(desktops)
        for index, desktop in enumerate(desktops):
            if desktop in lowered:
                rank = index
                break
        return (rank, backend.name)

    return sorted(found.values(), key=order)


# --------------------------------------------------------------------------
# The configuration
# --------------------------------------------------------------------------


@dataclass
class PortalConfig:
    """One ``portals.conf``: the ``[preferred]`` group, as written."""

    path: str
    desktop: str | None
    default: list[str] = field(default_factory=list)
    interfaces: dict[str, list[str]] = field(default_factory=dict)


def read_config(path: str, desktop: str | None) -> PortalConfig | None:
    """A configuration file, or None if it has nothing to say.

    A file with no ``[preferred]`` group is not a configuration file as far as
    xdg-desktop-portal is concerned: it is skipped, and the search carries on
    to the next candidate. Returning None here reproduces that, so a
    ``portals.conf`` with a typo in its one group header is reported as not
    taking effect rather than as taking effect emptily.
    """
    groups = parse_ini(path)
    preferred = groups.get("preferred")
    if not preferred:
        return None
    config = PortalConfig(path=path, desktop=desktop)
    for key, value in preferred.items():
        if key == "default":
            config.default = semicolon_list(value)
        else:
            config.interfaces[key] = semicolon_list(value)
    return config


@dataclass
class Resolution:
    """Which configuration file is in effect, and what lost to it."""

    candidates: list[Candidate] = field(default_factory=list)
    chosen: PortalConfig | None = None
    shadowed: list[PortalConfig] = field(default_factory=list)
    # Files that are present but carry no [preferred] group, so are passed over.
    ignored: list[Candidate] = field(default_factory=list)


def config_directories() -> list[str]:
    """The portal configuration search path, highest precedence first.

    portals.conf(5): ``$XDG_CONFIG_HOME``, each ``$XDG_CONFIG_DIRS``, the
    build-time sysconfdir (``/etc``), ``$XDG_DATA_HOME``, each
    ``$XDG_DATA_DIRS``, then the build-time datadir (``/usr/share``).
    """
    bases = (
        [config_home()]
        + config_dirs()
        + ["/etc"]
        + [data_home()]
        + data_dirs()
        + ["/usr/share"]
    )
    return dedupe(f"{base}/{PORTAL_SUBDIR}" for base in bases if base)


def resolve_config() -> Resolution:
    """Walk the search path and take the first file that carries preferences."""
    out = Resolution(candidates=search(config_directories(), CONFIG_NAME))
    for candidate in out.candidates:
        if not candidate.exists:
            continue
        config = read_config(candidate.path, candidate.desktop)
        if config is None:
            out.ignored.append(candidate)
            continue
        if out.chosen is None:
            out.chosen = config
        else:
            out.shadowed.append(config)
    return out


# --------------------------------------------------------------------------
# Who answers what
# --------------------------------------------------------------------------
#
# The order the portal itself uses: an explicit key for the interface, then the
# `default` line, then - with no configuration to go on - the deprecated UseIn
# key, then the GTK backend as a last resort. Within a preference list, entries
# that name a backend which is not installed, or which does not implement the
# interface asked for, are skipped and the next entry is tried.

NAMED = "named"
INHERITED = "inherited"
UNSET = "unset"

DECISIONS = {
    NAMED: "named for this interface",
    INHERITED: "inherited from the default line",
    UNSET: "not configured",
    ANY: "any backend, taken in name order",
}


@dataclass
class Assignment:
    """One interface, its backend, and the reasoning that got there."""

    interface: str
    backend: Backend | None = None
    decision: str = UNSET
    # How it was decided, in a few words, for the value line.
    gloss: str = ""
    # The per-entry reasoning, for the expansion.
    trace: list[str] = field(default_factory=list)
    # Set when the configuration itself is wrong, not merely absent.
    problem: str = ""
    # Entries in this interface's own list that name an installed backend which
    # cannot answer this interface. Collected only from the interface's own key:
    # a default line is *meant* to be filtered per interface - that is what the
    # specification says it is for - so a skip there is not a mistake, while a
    # skip in a line written about this one interface is.
    mistaken: list[str] = field(default_factory=list)
    # True when the configuration deliberately asks for no implementation.
    suppressed: bool = False
    # What the portal would fall back to with no configuration for this
    # interface: named after the mechanism that would choose it.
    fallback: Backend | None = None
    fallback_reason: str = ""

    @property
    def short(self) -> str:
        return short_interface(self.interface)


def _first_usable(names, interface, backends, trace, mistaken=None) -> Backend | None:
    """Walk a preference list the way the portal does, recording each step."""
    by_name = {backend.name: backend for backend in backends}
    for entry in names:
        if entry == NONE:
            # Handled by the caller before this point; listing it here would
            # claim a backend called "none" was looked for.
            continue
        if entry == ANY:
            for backend in backends:
                if backend.usable and backend.implements(interface):
                    trace.append(
                        f"{ANY} — any backend: {backend.name} is the first that implements it"
                    )
                    return backend
            trace.append(f"{ANY} — any backend, but none installed implements this interface")
            continue
        backend = by_name.get(entry)
        if backend is None:
            trace.append(f"{entry} — not installed, skipped")
            continue
        if not backend.usable:
            trace.append(f"{entry} — installed but unusable: {backend.problem}")
            continue
        if not backend.implements(interface):
            trace.append(f"{entry} — installed, but does not implement this interface, skipped")
            if mistaken is not None:
                mistaken.append(entry)
            continue
        trace.append(f"{entry} — installed and implements this interface: chosen")
        return backend
    return None


def _fallback(interface: str, backends, desktops) -> tuple[Backend | None, str]:
    """What the portal picks when no configuration decides the interface."""
    for desktop in desktops:
        for backend in backends:
            if not backend.usable or not backend.implements(interface):
                continue
            if any(name.lower() == desktop for name in backend.use_in):
                return backend, f"its UseIn key names {desktop}"
    for backend in backends:
        if backend.usable and backend.bus_name == GTK_BUS_NAME and backend.implements(interface):
            return backend, "the GTK backend is the portal's last resort"
    return None, ""


def assign(interface: str, config, backends, desktops) -> Assignment:
    """Resolve one interface exactly as xdg-desktop-portal would."""
    out = Assignment(interface=interface)
    out.fallback, out.fallback_reason = _fallback(interface, backends, desktops)
    implementers = [b.name for b in backends if b.usable and b.implements(interface)]

    named = config.interfaces.get(interface) if config is not None else None
    default = list(config.default) if config is not None else []

    # "none" means provide no implementation. An explicit key saying none wins
    # over the default line; with no explicit key, a default of none applies.
    if named is not None and NONE in named:
        out.decision = NAMED
        out.suppressed = True
        out.gloss = "the configuration asks for no implementation"
        out.trace.append("none — the configuration asks for no implementation of this interface")
        return out
    if named is None and NONE in default:
        out.decision = INHERITED
        out.suppressed = True
        out.gloss = "the default line asks for no implementation"
        out.trace.append("none — the default line asks for no implementation")
        return out

    if named is not None:
        backend = _first_usable(named, interface, backends, out.trace, out.mistaken)
        if backend is not None:
            out.backend = backend
            out.decision = NAMED
            # A backend that is not literally in the list can only have come
            # from the * entry, which is a different answer to "how was this
            # decided" and reads as one.
            out.gloss = DECISIONS[NAMED] if backend.name in named else DECISIONS[ANY]
            if out.mistaken:
                out.problem = (
                    f"the line for this interface names {out.mistaken[0]}, which does "
                    f"not implement it, so {backend.name} answers instead"
                )
                out.gloss = f"named here, after {out.mistaken[0]} was passed over"
            return out
        if not default:
            out.decision = NAMED
            out.problem = (
                "this interface is named in the configuration, but nothing in its "
                "preference list implements it"
            )
            out.gloss = "named, but nothing in the list implements it"
            return out
        out.trace.append("the list decided nothing, so the default line is tried")

    if default:
        before = len(out.trace)
        backend = _first_usable(default, interface, backends, out.trace)
        if backend is not None:
            out.backend = backend
            out.decision = INHERITED
            out.gloss = (
                DECISIONS[INHERITED] if backend.name in default else DECISIONS[ANY]
            )
            if out.mistaken:
                # The interface has a line of its own, and that line did nothing.
                out.problem = (
                    f"the line for this interface names {out.mistaken[0]}, which does "
                    "not implement it, so the default line answered instead"
                )
                out.gloss = f"the default line took over; {out.mistaken[0]} cannot answer it"
            elif named is not None:
                out.gloss = "the default line took over; nothing named here is installed"
            return out
        if named is not None:
            out.decision = NAMED
            out.problem = (
                "neither the entry for this interface nor the default line names a "
                "backend that implements it"
            )
            out.gloss = "named, but nothing in the list implements it"
            return out
        if len(out.trace) > before and implementers:
            out.decision = INHERITED
            out.problem = (
                "the default line names no backend that implements this interface, "
                "though "
                + ("one is installed" if len(implementers) == 1 else "some are installed")
            )
            out.gloss = "the default line names nothing that implements it"
            return out

    out.decision = UNSET
    out.gloss = DECISIONS[UNSET]
    return out


# --------------------------------------------------------------------------
# The section
# --------------------------------------------------------------------------


def _body(lead: str, lines=()) -> str:
    """A row's own expansion: a paragraph of context, then a list."""
    lines = [line for line in lines if line]
    return lead + "\n\n" + "\n".join(lines) if lines else lead


def _tick(candidate: Candidate) -> str:
    state = "found" if candidate.exists else "not present"
    return f"{candidate.path} — {state}"


def section(probe) -> Section:
    """The Desktop → Portals section, built for *probe*.

    One entry point returning a finished section, so the probe that owns row 3
    calls this and adds nothing of its own.
    """
    out = Section(id="portals", label="Portals")
    try:
        rows = _rows(probe)
    except Exception as exc:  # deliberately broad: a section is not worth a crash
        rows = [
            probe.row(
                "portal_error",
                "Status",
                f"The portal configuration could not be read on this machine "
                f"({type(exc).__name__}).",
            )
        ]
    for row in rows:
        out.add(row)
    return out


def _rows(probe) -> list[Row]:
    desktops = current_desktops()
    backends = load_backends()
    resolution = resolve_config()
    config = resolution.chosen
    rows: list[Row] = []

    if not backends and config is None:
        rows.append(
            probe.row(
                "portal_absent",
                "Status",
                "No portal backend is installed and no portal configuration file "
                "was found, so nothing on this machine answers portal requests.",
                body=_body(
                    "xdg-desktop-portal is the service applications ask for a file "
                    "dialog, a screenshot, or the system light or dark preference. It "
                    "answers none of those itself: it hands each request to a backend, "
                    "and a machine with no backend installed has nothing to hand them "
                    "to. Applications that ask fall back to their own dialogs, and "
                    "anything that follows the system colour scheme keeps its own "
                    "default. Nothing here is broken; there is simply no portal.",
                    [_tick(candidate) for candidate in resolution.candidates],
                ),
            )
        )

    rows.extend(_config_rows(probe, resolution, desktops, backends))
    rows.extend(_backend_rows(probe, backends, desktops))
    rows.extend(_interface_rows(probe, config, backends, desktops))
    return rows


def _config_rows(probe, resolution, desktops, backends) -> list[Row]:
    rows: list[Row] = []
    config = resolution.chosen

    rows.append(
        probe.row(
            "portal_desktops",
            "Desktop names",
            ", ".join(desktops) if desktops else "XDG_CURRENT_DESKTOP is not set",
            gloss="searched for a desktop-specific configuration file"
            if desktops
            else "so only the plain portals.conf is looked for",
            body=_body(
                "Portal configuration is keyed by these names. In every directory "
                "searched, xdg-desktop-portal looks first for a file named after each "
                "of them - niri-portals.conf for a session calling itself niri - and "
                "only when none of those exist does it read the plain portals.conf in "
                "that same directory. The names come from XDG_CURRENT_DESKTOP, "
                "lower-cased, in the order the session set them, most specific first."
            ),
        )
    )

    if config is None:
        rows.append(
            probe.row(
                "portal_config",
                "Configuration in effect",
                "No configuration file found",
                gloss=f"{len(resolution.candidates)} places searched",
                body=_body(
                    "With no configuration file, xdg-desktop-portal has nothing "
                    "telling it which backend to prefer, and falls back: first to any "
                    "backend whose deprecated UseIn key names this desktop, then to "
                    "the GTK backend if it is installed. That is why a machine with "
                    "several backends can behave differently after installing one "
                    "more. The full search, highest precedence first:",
                    [_tick(candidate) for candidate in resolution.candidates],
                ),
            )
        )
    else:
        chosen = next(
            (c for c in resolution.candidates if c.path == config.path),
            None,
        )
        rows.append(
            probe.row(
                "portal_config",
                "Configuration in effect",
                config.path,
                gloss=chosen.kind if chosen is not None else "",
                body=_body(
                    "This is the file xdg-desktop-portal reads on this machine. The "
                    "search stops at the first file that carries a [preferred] group: "
                    "the files below it are not merged in, and a preference written in "
                    "one of them has no effect while this file exists. The full "
                    "search, highest precedence first:",
                    [_tick(candidate) for candidate in resolution.candidates],
                ),
            )
        )

    rows.extend(_shadow_rows(probe, resolution))

    if config is not None:
        rows.append(
            probe.row(
                "portal_default",
                "Default backend",
                ", ".join(config.default) if config.default else "Not set",
                gloss="used for every interface with no entry of its own"
                if config.default
                else "so an interface with no entry of its own is not configured",
                body=_body(
                    "The default line in [preferred] is the catch-all. Any interface "
                    "the file does not name explicitly is answered by the first "
                    "backend in this list that implements it. An interface that is "
                    "named explicitly ignores this line entirely - which is the "
                    "difference between an assignment that reads \"named for this "
                    "interface\" and one that reads \"inherited from the default "
                    "line\" below."
                ),
            )
        )
        rows.append(
            probe.row(
                "portal_interfaces_named",
                "Interfaces named",
                str(len(config.interfaces)),
                gloss="entries in [preferred] besides the default line",
                body=_body(
                    "How many interfaces this file assigns by name.",
                    [
                        f"{short_interface(name)} — {', '.join(values) or 'nothing'}"
                        for name, values in sorted(config.interfaces.items())
                    ],
                )
                if config.interfaces
                else "",
            )
        )

    rows.append(_settings_row(probe, config, backends, desktops))
    return rows


def _shadow_rows(probe, resolution) -> list[Row]:
    """The row that says a file the user probably expected to win did not."""
    rows: list[Row] = []
    home = config_home()
    losers = list(resolution.shadowed)
    ignored = list(resolution.ignored)
    if not losers and not ignored:
        return rows

    chosen_path = resolution.chosen.path if resolution.chosen is not None else ""
    own = [config for config in losers if home and config.path.startswith(f"{home}/")]
    own_ignored = [c for c in ignored if home and c.path.startswith(f"{home}/")]

    lines = [f"{config.path} — ranked lower, not read" for config in losers]
    lines += [f"{c.path} — present, but has no [preferred] group, so it is passed over"
              for c in ignored]

    if own:
        value = own[0].path
        severity = WARNING
        gloss = "your own file, and it is not the one in effect"
        same_directory = chosen_path.rsplit("/", 1)[0] == value.rsplit("/", 1)[0]
        if same_directory:
            why = (
                "Portal configuration is resolved per directory, and inside each "
                "directory a desktop-specific file wins outright: with "
                "XDG_CURRENT_DESKTOP naming a desktop, a file named after it beats "
                f"{CONFIG_NAME} sitting beside it, and nothing in the losing file is "
                "merged in."
            )
        else:
            why = (
                "The file that won sits in a directory searched before this one, and "
                "the search stops at the first file it finds: nothing in this file is "
                "merged into it."
            )
        lead = (
            "This file is yours and it is being ignored. "
            + why
            + " This is the commonest portal mistake, and it is silent - the portal "
            "logs which file it read only in its debug output, and the file you "
            "edited stays where you left it looking authoritative. Either move your "
            f"preferences into the file in effect, {chosen_path}, or rename yours to "
            "match the name that beat it."
        )
    elif own_ignored:
        value = own_ignored[0].path
        severity = WARNING
        gloss = "your own file, and it carries no [preferred] group"
        lead = (
            "This file is yours and the portal passes over it, because a portal "
            "configuration file must carry a group header reading exactly "
            "[preferred] before any of its keys. A file whose header is missing, "
            "misspelt, or written after the keys it is meant to govern is skipped "
            "entirely and the search continues past it."
        )
    else:
        value = f"{len(losers) + len(ignored)} further file"
        value += "s" if len(losers) + len(ignored) != 1 else ""
        severity = NORMAL
        gloss = "found, but ranked below the file in effect"
        lead = (
            "These files exist and are not read. The search takes the first file "
            "that carries a [preferred] group and stops there; lower-ranked files "
            "are not merged in."
        )

    rows.append(
        probe.row(
            "portal_config_shadowed",
            "Shadowed configuration",
            value,
            severity=severity,
            gloss=gloss,
            body=_body(lead, lines),
        )
    )
    return rows


def _settings_row(probe, config, backends, desktops) -> Row:
    """The interface that decides whether every application looks light or dark."""
    assignment = assign(SETTINGS_INTERFACE, config, backends, desktops)
    shown = _present(assignment, desktops, critical=True)
    lead = (
        "org.freedesktop.impl.portal.Settings is the interface that carries "
        "org.freedesktop.appearance color-scheme, the one system-wide setting that "
        "says whether applications should look light or dark. Exactly one backend "
        "holds it, and every application that follows the system setting follows "
        "that backend - including this one. A backend that cannot read the "
        "preference it is supposed to publish still answers: it answers with its "
        "own default, usually dark, and there is no way to argue with it from the "
        "application side. If everything on the machine is stuck in one scheme "
        "while the shell's own setting says otherwise, this row names the program "
        "responsible.\n\n"
        "This is who is configured to answer, which is not the same as what was "
        "answered. What the running portal actually returned for the colour scheme "
        "is the Colour scheme row in Overview. If those two disagree, the portal was "
        "started before this configuration was last changed, and restarting the "
        "session is what applies it."
    )
    if shown.extra:
        lead += "\n\n" + shown.extra
    return probe.row(
        "portal_settings",
        "Appearance backend",
        shown.value,
        severity=shown.severity,
        gloss=shown.gloss,
        body=_body(lead, assignment.trace),
    )


@dataclass
class Presentation:
    """One assignment as a row reads it."""

    value: str
    severity: str = NORMAL
    gloss: str = ""
    # Extra prose for the expansion, above the per-entry trace.
    extra: str = ""
    # One line for the summary row, set only when something is actually wrong.
    problem: str = ""


def _present(assignment, desktops, critical: bool = False) -> Presentation:
    """The value line, severity and gloss for one interface.

    A row is warning-coloured when the configuration does not do what it looks
    like it does: a backend named for an interface it cannot answer, an
    interface whose preference list nothing implements, or - and this is the
    one the appearance interface gets checked for - a backend that belongs to a
    desktop which is not the one running.

    That last check is not applied to every interface on purpose. ``UseIn`` is
    deprecated, it names a family rather than a session (``wlroots`` covers a
    dozen compositors), and a screen-capture backend outside its nominal
    desktop is ordinary and works. A *settings* backend outside its own desktop
    is not ordinary: it is asked to publish a preference that lives in that
    desktop's own configuration, which it cannot read, so it answers with a
    built-in default and every application on the machine follows it.
    """
    if assignment.suppressed:
        return Presentation("No implementation", NORMAL, assignment.gloss)

    if assignment.backend is not None:
        backend = assignment.backend
        out = Presentation(backend.name, NORMAL, assignment.gloss)
        if assignment.problem:
            out.severity = WARNING
            out.problem = f"{assignment.short} — {assignment.problem}"
            out.extra = assignment.problem[0].upper() + assignment.problem[1:] + "."
        if not backend.suits(desktops):
            session = ", ".join(desktops) if desktops else "not named by XDG_CURRENT_DESKTOP"
            mismatch = (
                f"{backend.name}.portal declares UseIn={';'.join(backend.use_in)}, and "
                f"this session is {session}. The backend still answers, because the "
                "configuration names it, but it is reading settings from a desktop "
                "that is not running - which is how an interface comes to be answered "
                "confidently and wrongly. A backend outside its own desktop usually "
                "cannot find the configuration it was written against, and falls back "
                "to a built-in default that nothing on this machine can change."
            )
            out.extra = f"{out.extra}\n\n{mismatch}" if out.extra else mismatch
            if critical:
                out.severity = WARNING
                out.gloss = f"{out.gloss}, and built for another desktop"
                out.problem = (
                    f"{assignment.short} — {backend.name} is built for "
                    f"{';'.join(backend.use_in)}, not for this session"
                )
            else:
                out.gloss = f"{out.gloss}; built for {';'.join(backend.use_in)}"
        return out

    if assignment.problem:
        return Presentation(
            "Nothing implements it",
            WARNING,
            assignment.gloss,
            assignment.problem[0].upper() + assignment.problem[1:] + ".",
            f"{assignment.short} — {assignment.problem}",
        )

    if assignment.fallback is not None:
        return Presentation(
            "Not configured",
            NORMAL,
            f"{assignment.fallback.name} would answer, because {assignment.fallback_reason}",
        )

    return Presentation("Not configured", NORMAL, "and nothing installed would answer it")


def _backend_rows(probe, backends, desktops) -> list[Row]:
    rows: list[Row] = []
    if not backends:
        rows.append(
            probe.row(
                "portal_backend_count",
                "Backends installed",
                "None",
                gloss="no .portal file was found",
                body=_body(
                    "A backend declares itself in a small file naming its bus name "
                    "and the interfaces it can answer. These directories were "
                    "searched, highest precedence first:",
                    backend_directories(),
                ),
            )
        )
        return rows

    usable = [backend for backend in backends if backend.usable]
    rows.append(
        probe.row(
            "portal_backend_count",
            "Backends installed",
            str(len(backends)),
            gloss=", ".join(backend.name for backend in backends),
            body=_body(
                "Several backends installed at once is normal, not a fault: a "
                "compositor brings one for screen capture, a toolkit brings another "
                "for file dialogs, a distribution ships a third. They do not compete "
                "at run time - the configuration assigns each interface to exactly one "
                "of them, and the rest sit idle for that interface. The order below is "
                "the order xdg-desktop-portal considers them: a backend whose UseIn "
                "names this desktop first, then by file name, which is the order the "
                "* preference value walks.",
                [
                    f"{backend.name} — {len(backend.interfaces)} interface"
                    + ("s" if len(backend.interfaces) != 1 else "")
                    + (f"; UseIn={';'.join(backend.use_in)}" if backend.use_in else "; no UseIn")
                    for backend in backends
                ],
            ),
        )
    )

    for backend in backends:
        if not backend.usable:
            rows.append(
                probe.row(
                    "portal_backend",
                    backend.name,
                    "Not usable",
                    severity=WARNING,
                    key=f"backend:{backend.name}",
                    gloss=backend.problem,
                    body=_body(
                        "xdg-desktop-portal refuses a .portal file it cannot read, so "
                        "this backend takes part in nothing, whatever the "
                        "configuration says about it.",
                        [backend.path],
                    ),
                )
            )
            continue
        gloss = f"{len(backend.interfaces)} interface"
        gloss += "s" if len(backend.interfaces) != 1 else ""
        if backend.use_in:
            gloss += f"; UseIn={';'.join(backend.use_in)}"
            if not backend.suits(desktops):
                gloss += " — not this desktop"
        rows.append(
            probe.row(
                "portal_backend",
                backend.name,
                backend.bus_name,
                key=f"backend:{backend.name}",
                gloss=gloss,
                body=_body(
                    f"Declared in {backend.path}. It can answer these interfaces, "
                    "which is not the same as being assigned any of them:",
                    [short_interface(name) for name in sorted(backend.interfaces)],
                ),
            )
        )
    return rows


def _interface_rows(probe, config, backends, desktops) -> list[Row]:
    """One row per interface, saying who holds it and how that was decided."""
    universe: list[str] = []
    for backend in backends:
        for interface in backend.interfaces:
            if interface not in universe:
                universe.append(interface)
    if config is not None:
        for interface in config.interfaces:
            if interface not in universe:
                universe.append(interface)
    if not universe:
        return []

    rows: list[Row] = []
    problems: list[str] = []
    for interface in sorted(universe, key=lambda name: short_interface(name).lower()):
        assignment = assign(interface, config, backends, desktops)
        shown = _present(assignment, desktops, critical=interface == SETTINGS_INTERFACE)
        if shown.problem:
            problems.append(shown.problem)
        lead = f"The full interface name is {interface}."
        if shown.extra:
            lead += "\n\n" + shown.extra
        if assignment.decision == UNSET and assignment.fallback is not None:
            lead += (
                "\n\nNothing in the configuration decides this interface. "
                f"xdg-desktop-portal falls back to {assignment.fallback.name} because "
                f"{assignment.fallback_reason}. A fallback is not a setting: install "
                "another backend and it can change without anything being edited."
            )
        rows.append(
            probe.row(
                "portal_interface",
                assignment.short,
                shown.value,
                severity=shown.severity,
                key=f"interface:{assignment.short}",
                gloss=shown.gloss,
                body=_body(lead, assignment.trace),
            )
        )

    if problems:
        rows.insert(
            0,
            probe.row(
                "portal_problems",
                "Configuration problems",
                f"{len(problems)} interface" + ("s" if len(problems) != 1 else ""),
                severity=WARNING,
                gloss="assigned in a way that cannot work as written",
                body=_body(
                    "Each of these is a line in the configuration that does not do "
                    "what it looks like it does - a backend named for an interface it "
                    "cannot answer, a preference list where nothing installed "
                    "implements the interface, or a backend written for a desktop that "
                    "is not the one running. The interface rows below carry the "
                    "detail.",
                    problems,
                ),
            ),
        )
    return rows
