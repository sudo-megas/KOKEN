# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Which application opens what, and where that was decided.

Double-clicking a file is one of the few things on a desktop with no visible
mechanism at all. A MIME type is matched to a *desktop entry* - the small
``.desktop`` file that describes an installed application - through a chain of
plain text files that no interface on the machine shows in one place:
``mimeapps.list`` for the choices somebody made, ``mimeinfo.cache`` for the
associations applications declared about themselves, and a precedence order
between them that decides which of several answers wins.

The failure worth catching here is the broken default: a type whose chosen
application was uninstalled and whose association outlived it. The user-visible
symptom is that double-clicking does nothing at all, with no error and nothing
in any log, because the desktop asked for an application that is not there. The
entry is still written down, and it is still first in line. This section finds
those and says which line in which file to delete.

Everything is read as files. The precedence walk - locations in order, and
inside each location the ``XDG_CURRENT_DESKTOP`` names before the plain name -
is the same one xdg-desktop-portal uses for its own configuration, so it lives
in :mod:`koken.probes.portals` and is imported from there rather than written
twice.

The two specifications differ in one way that matters and is honoured here: a
portal reads exactly one configuration file and stops, while every
``mimeapps.list`` in the search takes part, higher precedence first, and a
desktop-specific one may only set defaults - never add or remove an
association.
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
)
from .portals import (
    config_dirs,
    config_home,
    data_dirs,
    data_home,
    dedupe,
    parse_ini,
    search,
    semicolon_list,
)

MIMEAPPS = "mimeapps.list"
CACHE_NAME = "mimeinfo.cache"

DEFAULTS_GROUP = "Default Applications"
ADDED_GROUP = "Added Associations"
REMOVED_GROUP = "Removed Associations"
CACHE_GROUP = "MIME Cache"
ENTRY_GROUP = "Desktop Entry"

# A directory of desktop entries is a few hundred files on a full desktop. The
# cap is not for those: it is for the machine where an applications directory
# has been pointed at something enormous, so that enumerating it cannot become
# the reason the application takes a minute to start.
MAX_ENTRIES = 20000

# How many lines a summary expansion carries before it says how many it left
# out. Long lists belong in the row's own body, but not without a floor.
MAX_LISTED = 60

# The types people actually think in. Each row answers one everyday question,
# and asks it of the types a desktop really uses for it - the first type with a
# handler is the one the row reports, and any disagreement between them is in
# the expansion.
CATEGORIES = (
    ("web", "Web pages", ("x-scheme-handler/https", "x-scheme-handler/http", "text/html")),
    ("mail", "Mail", ("x-scheme-handler/mailto",)),
    ("pdf", "PDF documents", ("application/pdf",)),
    ("text", "Plain text", ("text/plain",)),
    ("image", "Images", ("image/png", "image/jpeg", "image/gif", "image/webp")),
    ("audio", "Audio", ("audio/mpeg", "audio/flac", "audio/x-vorbis+ogg", "audio/ogg")),
    ("video", "Video", ("video/mp4", "video/x-matroska", "video/webm")),
    ("directory", "Folders", ("inode/directory",)),
)


# --------------------------------------------------------------------------
# The files
# --------------------------------------------------------------------------


def mimeapps_directories() -> list[str]:
    """Where a ``mimeapps.list`` may be, highest precedence first.

    mime-apps: ``$XDG_CONFIG_HOME``, each ``$XDG_CONFIG_DIRS``, then - kept for
    compatibility and deprecated for new use - ``$XDG_DATA_HOME/applications``
    and each ``$XDG_DATA_DIRS/applications``.
    """
    bases = [config_home()] + config_dirs()
    apps = [data_home()] + data_dirs()
    return dedupe(
        [base for base in bases if base] + [f"{app}/applications" for app in apps if app]
    )


def application_directories() -> list[str]:
    """Where installed desktop entries are, highest precedence first."""
    return dedupe(f"{base}/applications" for base in [data_home()] + data_dirs() if base)


@dataclass
class MimeFile:
    """One ``mimeapps.list``, as read."""

    path: str
    directory: str
    desktop: str | None
    exists: bool = False
    mine: bool = False
    defaults: dict[str, list[str]] = field(default_factory=dict)
    added: dict[str, list[str]] = field(default_factory=dict)
    removed: dict[str, list[str]] = field(default_factory=dict)
    # Set when a desktop-specific file tried to add or remove associations,
    # which the specification does not allow it to do.
    ignored_groups: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.desktop is None:
            return self.path
        return f"{self.path} (only used while the desktop is {self.desktop})"


def read_mimeapps(candidate, mine: bool) -> MimeFile:
    """One candidate path as a file of associations. Absent reads as empty."""
    out = MimeFile(
        path=candidate.path,
        directory=candidate.directory,
        desktop=candidate.desktop,
        exists=candidate.exists,
        mine=mine,
    )
    if not candidate.exists:
        return out
    groups = parse_ini(candidate.path)
    out.defaults = {
        key: semicolon_list(value) for key, value in groups.get(DEFAULTS_GROUP, {}).items()
    }
    added = {key: semicolon_list(value) for key, value in groups.get(ADDED_GROUP, {}).items()}
    removed = {key: semicolon_list(value) for key, value in groups.get(REMOVED_GROUP, {}).items()}
    if candidate.desktop is not None:
        # "the desktop-specific files can only be used for specifying the
        # default application for a given type. It is not possible to add or
        # remove associations from these files." Reporting what such a file
        # tried to do is more use than silently dropping it.
        if added:
            out.ignored_groups.append(ADDED_GROUP)
        if removed:
            out.ignored_groups.append(REMOVED_GROUP)
    else:
        out.added, out.removed = added, removed
    return out


@dataclass
class Level:
    """One directory in the search, with its files and its association cache."""

    directory: str
    files: list[MimeFile] = field(default_factory=list)
    cache: dict[str, list[str]] = field(default_factory=dict)
    cache_path: str = ""
    has_cache: bool = False
    # True when there is no mimeinfo.cache and the associations were read from
    # the MimeType lines of the entries themselves.
    derived: bool = False


def read_cache(directory: str) -> tuple[dict[str, list[str]], bool]:
    """``mimeinfo.cache`` for a directory: type to desktop ids, in order.

    The cache is generated from the ``MimeType=`` lines of the desktop entries
    installed beside it, which is why it is the answer to "what can open this"
    when nobody has chosen anything.
    """
    path = f"{directory}/{CACHE_NAME}"
    groups = parse_ini(path)
    entries = groups.get(CACHE_GROUP)
    if entries is None:
        return {}, False
    return {key: semicolon_list(value) for key, value in entries.items()}, True


def load_levels() -> tuple[list[Level], list]:
    """Every level of the search, in precedence order, with its files read."""
    home_dirs = {path for path in (config_home(), data_home()) if path}
    candidates = search(mimeapps_directories(), MIMEAPPS)
    levels: list[Level] = []
    by_directory: dict[str, Level] = {}
    for candidate in candidates:
        level = by_directory.get(candidate.directory)
        if level is None:
            level = Level(directory=candidate.directory)
            level.cache, level.has_cache = read_cache(candidate.directory)
            level.cache_path = f"{candidate.directory}/{CACHE_NAME}"
            by_directory[candidate.directory] = level
            levels.append(level)
        mine = any(
            candidate.directory == home or candidate.directory.startswith(f"{home}/")
            for home in home_dirs
        )
        level.files.append(read_mimeapps(candidate, mine))
    return levels, candidates


# --------------------------------------------------------------------------
# The applications
# --------------------------------------------------------------------------


@dataclass
class Applications:
    """Every installed desktop entry, by the id associations refer to it by."""

    by_id: dict[str, str] = field(default_factory=dict)
    # Directory to the (id, path) pairs found in it, for the directories that
    # have no mimeinfo.cache and must be read entry by entry.
    by_directory: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    directories: list[str] = field(default_factory=list)
    truncated: bool = False

    def path(self, desktop_id: str) -> str:
        return self.by_id.get(desktop_id, "")

    def installed(self, desktop_id: str) -> bool:
        return desktop_id in self.by_id


def load_applications() -> Applications:
    """Walk the applications directories and index them by desktop file id.

    The id is the path below ``applications`` with the separators turned into
    dashes, so ``/usr/share/applications/foo/bar.desktop`` is ``foo-bar.desktop``
    - which is why an association can name a file that appears to be missing
    from the directory it names. Building the index this way answers both
    spellings without guessing. The first directory to hold an id wins, which
    is how a copy in the home directory replaces the system one.
    """
    out = Applications(directories=application_directories())
    seen = 0
    for directory in out.directories:
        try:
            base = resolve(directory)
        except (OSError, ValueError):
            continue
        try:
            walker = os.walk(base, followlinks=False)
            for root, _dirs, names in walker:
                for name in names:
                    if not name.endswith(".desktop"):
                        continue
                    seen += 1
                    if seen > MAX_ENTRIES:
                        out.truncated = True
                        break
                    full = os.path.join(root, name)
                    relative = os.path.relpath(full, str(base))
                    desktop_id = relative.replace(os.sep, "-")
                    out.by_id.setdefault(desktop_id, full)
                    out.by_directory.setdefault(directory, []).append((desktop_id, full))
                if out.truncated:
                    break
        except (OSError, ValueError):
            continue
        if out.truncated:
            break
    return out


def derive_cache(entries) -> dict[str, list[str]]:
    """The associations a directory's own entries declare, read from them.

    ``mimeinfo.cache`` is a cached copy of exactly this - the ``MimeType=``
    lines of the entries beside it, gathered by update-desktop-database. When
    the cache is absent the entries still say what they open, and a machine
    where the cache was never generated is a normal machine, not a broken one,
    so the lines are read directly rather than reporting no associations at
    all. This costs one small read per entry and only happens for a directory
    with no cache.
    """
    out: dict[str, list[str]] = {}
    # Sorted, so that the answer does not depend on the order the filesystem
    # happened to hand back the directory.
    for desktop_id, path in sorted(entries):
        groups = parse_ini(path)
        entry = groups.get(ENTRY_GROUP)
        if not entry:
            continue
        # A NoDisplay entry is kept: hiding something from a menu does not
        # stop it being what opens a type, and update-desktop-database puts it
        # in the cache too.
        for mime in semicolon_list(entry.get("MimeType")):
            out.setdefault(mime, []).append(desktop_id)
    return out


def entry_name(path: str) -> str:
    """The ``Name=`` of a desktop entry, for the gloss. Empty when unreadable.

    Only ever called for the handful of entries a row actually names, so this
    never turns into reading every desktop file on the machine.
    """
    if not path:
        return ""
    groups = parse_ini(path)
    entry = groups.get(ENTRY_GROUP, {})
    return entry.get("Name", "").strip()


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@dataclass
class Handler:
    """Which application opens a type, and how that was settled."""

    mime: str
    desktop_id: str = ""
    path: str = ""
    source: MimeFile | None = None
    # Ids named ahead of the winner that are not installed at all.
    missing: list[str] = field(default_factory=list)
    # True when the answer came from the associations rather than a default.
    from_association: bool = False
    # True when a default is written down and none of it is installed.
    dead_default: bool = False
    associated: bool = True

    @property
    def found(self) -> bool:
        return bool(self.desktop_id)


class Registry:
    """The whole chain, read once, answering both questions about a type."""

    def __init__(self) -> None:
        self.levels, self.candidates = load_levels()
        self.applications = load_applications()
        for level in self.levels:
            if level.has_cache:
                continue
            entries = self.applications.by_directory.get(level.directory)
            if entries:
                level.cache = derive_cache(entries)
                level.derived = bool(level.cache)
        self.files = [file for level in self.levels for file in level.files]
        self.present = [file for file in self.files if file.exists]

    # -- associations ----------------------------------------------------

    def associations(self, mime: str) -> list[str]:
        """Every application associated with *mime*, most preferred first.

        The walk the specification suggests: added associations, then removed
        ones as a blacklist, then whatever the directory's own entries declare,
        one level at a time from the top of the precedence order down.
        """
        results: list[str] = []
        blacklist: set[str] = set()
        for level in self.levels:
            for file in level.files:
                for desktop_id in file.added.get(mime, []):
                    if desktop_id not in blacklist and desktop_id not in results:
                        results.append(desktop_id)
                for desktop_id in file.removed.get(mime, []):
                    blacklist.add(desktop_id)
            for desktop_id in level.cache.get(mime, []):
                if desktop_id not in blacklist and desktop_id not in results:
                    results.append(desktop_id)
        return results

    # -- defaults --------------------------------------------------------

    def default(self, mime: str) -> Handler:
        """The application that opens *mime*, and what was skipped to get there.

        Entries are tried in order, highest-precedence file first and within a
        file in the order written. An entry naming an application that is not
        installed is recorded and passed over, which is exactly what the
        desktop does - and exactly what hides the fault, because the type keeps
        working through the second entry while the first stays broken.

        An entry that is installed but not associated with the type is used
        anyway, and said so. The specification asks implementations to verify
        the association, but they disagree in practice, and reporting a handler
        this machine will not actually use would be the worse error.
        """
        out = Handler(mime=mime)
        named = False
        for file in self.files:
            for desktop_id in file.defaults.get(mime, []):
                named = True
                if not self.applications.installed(desktop_id):
                    if desktop_id not in out.missing:
                        out.missing.append(desktop_id)
                    continue
                out.desktop_id = desktop_id
                out.path = self.applications.path(desktop_id)
                out.source = file
                out.associated = desktop_id in self.associations(mime)
                return out
        if named:
            out.dead_default = True
        associated = self.associations(mime)
        for desktop_id in associated:
            if self.applications.installed(desktop_id):
                out.desktop_id = desktop_id
                out.path = self.applications.path(desktop_id)
                out.from_association = True
                return out
        return out

    # -- the shape of what is written down -------------------------------

    def default_types(self) -> list[str]:
        types: list[str] = []
        for file in self.files:
            for mime in file.defaults:
                if mime not in types:
                    types.append(mime)
        return sorted(types)

    def association_types(self) -> list[str]:
        types: set[str] = set()
        for level in self.levels:
            types.update(level.cache)
            for file in level.files:
                types.update(file.added)
        return sorted(types)

    def associated_applications(self) -> set[str]:
        ids: set[str] = set()
        for level in self.levels:
            for entries in level.cache.values():
                ids.update(entries)
        return ids


# --------------------------------------------------------------------------
# The section
# --------------------------------------------------------------------------


def _body(lead: str, lines=()) -> str:
    lines = [line for line in lines if line]
    if not lines:
        return lead
    shown = lines[:MAX_LISTED]
    if len(lines) > MAX_LISTED:
        shown.append(f"... and {len(lines) - MAX_LISTED} more")
    return lead + "\n\n" + "\n".join(shown)


def _tick(candidate) -> str:
    state = "found" if candidate.exists else "not present"
    if candidate.desktop is not None:
        return f"{candidate.path} — {state}; defaults only, and only under {candidate.desktop}"
    return f"{candidate.path} — {state}"


def _describe_source(handler: Handler) -> str:
    """Where this handler was decided, in a few words for the gloss."""
    if handler.from_association:
        return "no default is set; this is the first application associated with it"
    if handler.source is None:
        return ""
    if handler.source.mine:
        return "your own choice"
    return f"inherited from {handler.source.path}"


def section(probe) -> Section:
    """The Operating system → File types section, built for *probe*."""
    out = Section(id="filetypes", label="File types")
    try:
        rows = _rows(probe)
    except Exception as exc:  # deliberately broad: a section is not worth a crash
        rows = [
            probe.row(
                "mime_error",
                "Status",
                f"File type associations could not be read on this machine "
                f"({type(exc).__name__}).",
            )
        ]
    for row in rows:
        out.add(row)
    return out


def _rows(probe) -> list[Row]:
    registry = Registry()
    rows: list[Row] = []
    caches = [level for level in registry.levels if level.has_cache or level.derived]

    if not registry.present and not caches:
        rows.append(
            probe.row(
                "mime_absent",
                "Status",
                "No mimeapps.list and no mimeinfo.cache were found, so nothing on "
                "this machine records which application opens which kind of file.",
                body=_body(
                    "This is what a machine with no desktop installed looks like, and "
                    "it is not a fault. Associations appear when applications are "
                    "installed - each desktop entry lists the types it can open, and "
                    "update-desktop-database gathers those lines into mimeinfo.cache - "
                    "and choices appear in mimeapps.list when something sets one. "
                    "These are the places that were searched:",
                    [_tick(candidate) for candidate in registry.candidates],
                ),
            )
        )

    rows.extend(_summary_rows(probe, registry, caches))
    rows.extend(_category_rows(probe, registry))
    return rows


def _summary_rows(probe, registry, caches) -> list[Row]:
    rows: list[Row] = []

    rows.append(
        probe.row(
            "mime_files",
            "Association files",
            str(len(registry.present)),
            gloss=f"of {len(registry.candidates)} places searched",
            body=_body(
                "Every one of these files takes part, highest precedence first - "
                "unlike portal configuration, which stops at the first file it finds. "
                "A type's default comes from the first file that names it; "
                "associations accumulate down the list. A file named after the "
                "desktop is read only in that desktop, and may set defaults only: it "
                "cannot add or remove associations.",
                [_tick(candidate) for candidate in registry.candidates],
            ),
        )
    )

    ignored = [
        f"{file.path} — {', '.join(file.ignored_groups)} in a desktop-specific file, ignored"
        for file in registry.present
        if file.ignored_groups
    ]
    if ignored:
        rows.append(
            probe.row(
                "mime_ignored_groups",
                "Ignored in these files",
                f"{len(ignored)} file" + ("s" if len(ignored) != 1 else ""),
                severity=WARNING,
                gloss="add or remove associations where that is not allowed",
                body=_body(
                    "A desktop-specific mimeapps.list may only name defaults. Added "
                    "and Removed Associations written in one are ignored, so these "
                    "lines do nothing at all and belong in the plain mimeapps.list "
                    "beside them.",
                    ignored,
                ),
            )
        )

    defaults = [registry.default(mime) for mime in registry.default_types()]
    broken = [handler for handler in defaults if handler.missing]
    dead = [handler for handler in defaults if handler.dead_default]

    if broken:
        lines = []
        for handler in broken:
            names = ", ".join(handler.missing)
            if handler.dead_default:
                outcome = (
                    f"opened by {handler.desktop_id} through its associations"
                    if handler.found
                    else "nothing opens it at all"
                )
            else:
                outcome = f"opened by {handler.desktop_id} instead"
            lines.append(f"{handler.mime} — {names} is not installed; {outcome}")
        rows.append(
            probe.row(
                "mime_broken",
                "Broken defaults",
                f"{len(broken)} type" + ("s" if len(broken) != 1 else ""),
                severity=WARNING,
                gloss="name an application that is not installed",
                body=_body(
                    "Each of these types has a default pointing at a desktop entry "
                    "that is not on this machine. It is what an uninstalled "
                    "application leaves behind: removing the program does not remove "
                    "the line that names it. Where a second entry is listed the type "
                    "still opens, through that one, and the dead line simply sits "
                    "there. Where there is nothing else, double-clicking such a file "
                    "does nothing whatsoever - no error, no message, nothing in any "
                    "log - because the desktop asked for an application that does not "
                    "exist. The fix is to delete the line naming the missing entry "
                    "from the file that holds it, under [Default Applications], or to "
                    "set the type to something else, which rewrites the same line.",
                    lines,
                ),
            )
        )
    else:
        rows.append(
            probe.row(
                "mime_broken",
                "Broken defaults",
                "None",
                gloss="every default names an application that is installed"
                if defaults
                else "nothing on this machine sets a default for any type",
                body=_body(
                    "A broken default is a type whose chosen application was "
                    "uninstalled while the line naming it stayed behind. Nothing on "
                    "this machine has one."
                ),
            )
        )

    if dead:
        stranded = [handler for handler in dead if not handler.found]
        rows.append(
            probe.row(
                "mime_stranded",
                "Types with no working default",
                f"{len(dead)} type" + ("s" if len(dead) != 1 else ""),
                severity=WARNING if stranded else NORMAL,
                gloss=(
                    f"{len(stranded)} of them open with nothing at all"
                    if stranded
                    else "all of them still open through an association"
                ),
                body=_body(
                    "Every application named as the default for these types is "
                    "missing. A type with an association left can still be opened by "
                    "it; a type with none cannot be opened at all.",
                    [
                        f"{handler.mime} — "
                        + (
                            f"falls back to {handler.desktop_id}"
                            if handler.found
                            else "nothing on this machine opens it"
                        )
                        for handler in dead
                    ],
                ),
            )
        )

    mine = [handler for handler in defaults if handler.source is not None and handler.source.mine]
    inherited = [
        handler for handler in defaults if handler.source is not None and not handler.source.mine
    ]
    counted = f"{len(mine)} set by you, {len(inherited)} inherited"
    unresolved = len(defaults) - len(mine) - len(inherited)
    if unresolved:
        counted += f", {unresolved} naming nothing that is installed"
    rows.append(
        probe.row(
            "mime_defaults",
            "Types with a default",
            str(len(defaults)),
            gloss=counted,
            body=_body(
                "A default is a choice somebody made; an association is an "
                "application saying what it can open. Your own choices live in "
                f"{(config_home() or '~/.config')}/{MIMEAPPS} and are written there by "
                "whatever you used to set them - a file manager's \"open with\", a "
                "settings panel, "
                "or an editor. The inherited ones come from the distribution's files "
                "further down the search and apply until something of yours says "
                "otherwise.",
                [
                    f"{handler.mime} — {handler.desktop_id}"
                    for handler in defaults
                    if handler.source is not None and handler.source.mine
                ],
            ),
        )
    )

    disagreements = []
    for handler in defaults:
        if not handler.found or handler.from_association:
            continue
        associated = registry.associations(handler.mime)
        if associated and associated[0] != handler.desktop_id:
            disagreements.append(
                f"{handler.mime} — opens with {handler.desktop_id}, while "
                f"{associated[0]} is first in the associations"
            )
    if disagreements:
        rows.append(
            probe.row(
                "mime_disagreements",
                "Default is not first choice",
                f"{len(disagreements)} type" + ("s" if len(disagreements) != 1 else ""),
                gloss="the default and the association order differ",
                body=_body(
                    "The default wins, so these types open with the application named "
                    "below. The disagreement is worth knowing about because the two "
                    "lists are written by different things: the default by whoever "
                    "chose it, the association order by what the applications declare "
                    "and what any Added Associations line puts in front of them. An "
                    "\"open with\" menu is usually built from the second list, so it "
                    "can offer a different first entry from the one a double-click "
                    "uses.",
                    disagreements,
                ),
            )
        )

    associated_types = registry.association_types()
    rows.append(
        probe.row(
            "mime_associations",
            "Types with a handler",
            str(len(associated_types)),
            gloss=f"declared by {len(registry.associated_applications())} applications",
            body=_body(
                "How many distinct MIME types something on this machine says it can "
                "open, counted from the mimeinfo.cache files and any Added "
                "Associations. This is the pool a default is chosen from, and the "
                "reason a type with no default still opens with something.",
                [
                    f"{level.cache_path} — {len(level.cache)} types"
                    if level.has_cache
                    else f"{level.directory} — {len(level.cache)} types, read from the "
                    "MimeType lines of the entries themselves, since this directory "
                    "has no mimeinfo.cache"
                    for level in caches
                ],
            ),
        )
    )

    applications = registry.applications
    rows.append(
        probe.row(
            "mime_applications",
            "Desktop entries installed",
            str(len(applications.by_id)),
            gloss=f"{len(registry.associated_applications())} of them claim a file type",
            body=_body(
                "Every installed application that offers itself to the desktop has a "
                ".desktop file in one of these directories. An association names one "
                "of them by its id, which is its path below the directory with the "
                "separators turned into dashes.",
                applications.directories
                + (
                    [f"stopped counting at {MAX_ENTRIES} entries"]
                    if applications.truncated
                    else []
                ),
            ),
        )
    )
    return rows


def _category_rows(probe, registry) -> list[Row]:
    """One row per everyday question: what opens a web page, a PDF, a folder."""
    rows: list[Row] = []
    for field_name, label, types in CATEGORIES:
        handlers = [(mime, registry.default(mime)) for mime in types]
        chosen = next((handler for _mime, handler in handlers if handler.found), None)
        lines = []
        for mime, handler in handlers:
            if handler.found:
                where = _describe_source(handler)
                line = f"{mime} — {handler.desktop_id}"
                if where:
                    line += f" ({where})"
                if handler.missing:
                    line += f"; {', '.join(handler.missing)} named first, not installed"
                if not handler.associated and not handler.from_association:
                    line += (
                        "; set as the default without being associated with the type, "
                        "which some desktops honour and some ignore"
                    )
            else:
                line = f"{mime} — nothing opens it"
                if handler.missing:
                    line += (
                        f"; {', '.join(handler.missing)} is set as the default and is "
                        "not installed"
                    )
            lines.append(line)

        if chosen is None:
            # A category with nothing installed is ordinary. A category whose
            # default names an application that was removed is not: the choice
            # is still written down, and the double-click does nothing.
            gone = [
                name for _mime, handler in handlers for name in handler.missing
            ]
            rows.append(
                probe.row(
                    "mime_category",
                    label,
                    "No handler",
                    severity=WARNING if gone else NORMAL,
                    key=f"category:{field_name}",
                    gloss=f"{gone[0]} is set as the default and is not installed"
                    if gone
                    else "nothing on this machine claims these types",
                    body=_body(
                        "Nothing installed says it can open any of these types, so a "
                        "double-click has nowhere to go."
                        + (
                            " Something was chosen for this once and is no longer "
                            "installed, which is why the choice is still written down "
                            "and nothing happens when it is used."
                            if gone
                            else ""
                        ),
                        lines,
                    ),
                )
            )
            continue

        name = entry_name(chosen.path)
        gloss = _describe_source(chosen)
        if name:
            gloss = f"{name} — {gloss}" if gloss else name
        severity = WARNING if chosen.missing or chosen.dead_default else NORMAL
        if chosen.missing:
            gloss = f"{gloss}; {chosen.missing[0]} is named first and is not installed"
        rows.append(
            probe.row(
                "mime_category",
                label,
                chosen.desktop_id,
                severity=severity,
                key=f"category:{field_name}",
                gloss=gloss,
                body=_body(
                    f"What opens {label.lower()} on this machine, type by type. "
                    "Where two types in the same row answer differently, the first "
                    "one with an answer is the one on the value line."
                    + (
                        "\n\nThe entry named first is not installed, which is what a "
                        "removed application leaves behind."
                        if chosen.missing
                        else ""
                    ),
                    lines,
                ),
            )
        )
    return rows
