# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Which toolkit each installed application is built on, without launching one.

An application's toolkit is not written down anywhere. The desktop entry does
not say it, the package name rarely says it, and asking the program itself
means starting it - which this application will not do, for four hundred
programs, at launch, on a machine somebody is trying to use.

So it is read out of the files instead. The dynamic linker settles this
question every time a program starts, by reading the ``DT_NEEDED`` list in the
executable's ``PT_DYNAMIC`` segment and loading the sonames it names. That list
is thirty or so bytes of offsets into a string table, sitting in a file that is
already on disk, and nothing stops a reader from looking at it directly. A
binary with ``libgtk-4.so.1`` in that list will load GTK 4 when it starts, and
that is not a guess about the program - it is the same fact the loader acts on.

Five sources, in the order they are tried:

1. **Filesystem markers beside the binary.** ``resources/app.asar``,
   ``chrome-sandbox``, ``icudtl.dat``, ``*.pak``. An Electron application also
   links GTK 3, so if the sonames were read first every Electron application on
   the machine would be filed as a GTK 3 application, which is wrong in every
   way that matters to somebody reading this page.
2. **The ELF ``DT_NEEDED`` list**, parsed here. Where a binary links no toolkit
   directly but keeps its interface in a private library - which is how most
   large KDE and Mozilla applications are built - the libraries it does name
   are opened one level down, resolved through ``DT_RUNPATH`` and the system
   library directories exactly as the loader would resolve them.
3. **The script it points at.** ``Exec=`` very often names a shell wrapper that
   execs the real binary two directories away, and following that wrapper is
   what makes a package-manager lookup unnecessary for nearly everything. Where
   the wrapper leads to an interpreter rather than an executable - a Python
   application is ``python3`` plus a file - the file is read and its imports
   are matched, because ``import tkinter`` is as load-bearing as ``DT_NEEDED``.
4. **Flatpak metadata.** A Flatpak application's files are on disk and are read
   the same way as anything else; the ``metadata`` file names its command and
   its runtime, and the runtime is used only when the files cannot be read.
5. **The package database**, last, cheaply, and only for what is left over.

Everything here reads files and nothing here runs anything. Every reader
returns nothing rather than raising, the same rule the EDID parser follows,
because a ``.desktop`` file is written by whoever installed it and an
executable may be truncated, foreign-architecture, or not an executable at all.

The scan is bounded by a wall-clock budget and stops when it expires, and it
never touches a path on a network filesystem: a hung NFS server turns a
``stat`` into an unbounded wait, and no fact on this page is worth a window
that will not open. What was skipped is reported rather than hidden.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import struct
import time
from dataclasses import dataclass, field

from .base import list_dir, resolve

# --------------------------------------------------------------------------
# The toolkits
# --------------------------------------------------------------------------
#
# `rank` decides which one wins when a binary shows evidence of more than one,
# and the order is not alphabetical or historical - it is the order in which
# one toolkit hides another:
#
#   Electron links GTK 3 for its window and its file dialogs, and Java's SWT
#   loads it too, but neither draws a single widget with it. wxWidgets on Linux
#   *is* GTK underneath, so every wx binary names libgtk in DT_NEEDED; the code
#   in the application is wx. Only after those three are excluded does a GTK or
#   Qt soname mean the application is a GTK or Qt application, and there the
#   newer major version wins, because an application linking both is mid-port
#   and the newer one is what it draws with.
#
# `note` is the lead paragraph of the row's own expansion, before the list of
# applications. It is here rather than in the explanation corpus because the
# row's body has to carry the machine's own list, and a row body supplied by a
# probe replaces the corpus entry rather than joining it.


@dataclass(frozen=True)
class Toolkit:
    key: str
    label: str
    rank: int
    note: str


TOOLKITS: tuple[Toolkit, ...] = (
    Toolkit(
        "electron",
        "Electron",
        0,
        "Electron applications are web pages. Each one carries its own copy of "
        "Chromium and its own copy of Node, so the version of the browser "
        "inside one has nothing to do with the version of the browser inside "
        "the next, or with any browser installed on this machine. That is what "
        "the memory goes on: a single Electron application starts a browser "
        "engine, a renderer process per window, a GPU process and a utility "
        "process before it has drawn anything, and two hundred megabytes "
        "resident is ordinary rather than exceptional. Nothing is shared "
        "between two of them, so ten Electron applications are ten copies. "
        "They also draw their own widgets, which is why an Electron "
        "application ignores the system widget theme, the system font "
        "settings and, usually, the system title bar, and why its text "
        "selection and scrollbars behave like a web page rather than like the "
        "rest of the desktop. KÖKEN files these first: an Electron binary "
        "links GTK 3 as well, for the file dialog and the window, but calling "
        "it a GTK 3 application would say nothing true about it.",
    ),
    Toolkit(
        "java",
        "Java",
        1,
        "A Java application is a launcher that starts a virtual machine and "
        "hands it a jar. The widgets are drawn inside that machine by Swing or "
        "by SWT, not by any library the system linker loaded, which is why a "
        "Java application looks like itself everywhere and why the GTK look "
        "and feel it may offer is an imitation drawn by Java rather than GTK "
        "doing the drawing. Two consequences show up on a modern desktop: "
        "display scaling is the JVM's own setting rather than the desktop's, "
        "so a Java application on a HiDPI screen is often the one rendering at "
        "half size until sun.java2d.uiScale is set for it, and the font "
        "rendering is Java's own, so hinting and antialiasing settings applied "
        "to everything else stop at its window edge.",
    ),
    Toolkit(
        "wx",
        "wxWidgets",
        2,
        "wxWidgets is not a widget set of its own on Linux - it is a layer over "
        "GTK, and every wx binary links GTK for that reason. The practical "
        "effect is that a wx application inherits the GTK theme and the GTK "
        "file dialog, and therefore mostly looks right, but its layout, its "
        "menus and its dialogs are wx's idea of those things rather than the "
        "desktop's. KÖKEN files these as wx rather than as GTK because the GTK "
        "soname in the binary comes from the wrapper, not from the "
        "application's own code, and because a GTK theme change reaches it "
        "only as far as wx passes it through.",
    ),
    Toolkit(
        "qt6",
        "Qt 6",
        3,
        "Qt 6 scales fractionally and does it per screen, always: the "
        "device-pixel-ratio work that was optional in Qt 5 is compiled in and "
        "on, so a Qt 6 application on a 150% display renders at 150% rather "
        "than at 100% or 200%. It also reads its own settings rather than the "
        "desktop's - a GNOME or Xfce session hands a Qt application no widget "
        "style, no icon theme and no font unless qt6ct or a platform theme "
        "plugin is installed to translate, and Qt 5's translation layer (qt5ct, "
        "the Qt 5 platform themes) does not serve Qt 6. That is why Qt 5 and "
        "Qt 6 applications on the same desktop can disagree about cursor size, "
        "icon set and scaling at the same time: they are reading two different "
        "sets of settings, and where the cursor theme is concerned Qt 6 asks "
        "the compositor while Qt 5 reads the X resource.",
    ),
    Toolkit(
        "qt5",
        "Qt 5",
        4,
        "Qt 5 scales by whole numbers unless it is told otherwise. Fractional "
        "scaling needs QT_ENABLE_HIGHDPI_SCALING or QT_SCALE_FACTOR set for it, "
        "and without them a Qt 5 application on a 150% display is the one "
        "rendering too small or too large next to its Qt 6 and GTK 4 "
        "neighbours. Its cursor size comes from the X resource rather than from "
        "the compositor, which is the usual reason a Qt 5 window shows a cursor "
        "of a different size from the desktop around it. Its style, icons and "
        "fonts come from qt5ct or a Qt 5 platform theme, neither of which "
        "affects Qt 6.",
    ),
    Toolkit(
        "gtk4",
        "GTK 4",
        5,
        "GTK 4 reads its appearance from gtk-4.0/gtk.css and from the libadwaita "
        "stylesheet, and it cannot read a GTK 3 theme: the widget names, the "
        "CSS nodes and the drawing model all changed, so a theme written for "
        "GTK 3 has nothing GTK 4 can apply and is ignored rather than "
        "half-applied. That is the whole answer to why setting a theme leaves "
        "some windows changed and some untouched - the untouched ones are on "
        "the other major version. GTK 4 also renders through GSK, which means "
        "it wants the GPU, and its dark preference comes from the desktop's "
        "colour-scheme setting rather than from the theme name.",
    ),
    Toolkit(
        "gtk3",
        "GTK 3",
        6,
        "GTK 3 reads gtk-3.0/gtk.css and the theme named in its own settings, "
        "which is a different file and a different setting from GTK 4's. A "
        "desktop running both - and nearly every desktop runs both - is "
        "theming them separately whether or not the settings panel says so, "
        "and a GTK 3 application will keep the old appearance until a GTK 3 "
        "version of the theme is installed as well. Icon themes are the "
        "exception and are shared. GTK 3 remains the most common toolkit on "
        "Linux by application count, and is in long-term maintenance rather "
        "than development, so applications still on it are not behind so much "
        "as not yet moved.",
    ),
    Toolkit(
        "gtk2",
        "GTK 2",
        7,
        "GTK 2 has had no release since 2020 and takes its appearance from a "
        "theme engine, a compiled plugin, rather than from CSS - so a GTK 2 "
        "application cannot use any theme made in the last decade and falls "
        "back to the default grey unless a matching GTK 2 engine and gtkrc are "
        "installed for it. It also has no display scaling at all, which is why "
        "a GTK 2 application on a HiDPI screen is drawn at one physical pixel "
        "per logical pixel and comes out half size. Anything still here is "
        "either very old or deliberately frozen.",
    ),
    Toolkit(
        "tk",
        "Tk",
        8,
        "Tk is the widget set that ships with Tcl and, through tkinter, with "
        "Python. It draws its own controls and reads no desktop setting at "
        "all: not the theme, not the icon set, not the font, not the cursor "
        "size and not the scaling factor. A Tk application therefore looks the "
        "same on every desktop, which is to say it looks like nothing else on "
        "any of them, and on a HiDPI screen it is drawn at native pixel size "
        "and comes out small. It is here because it costs almost nothing and "
        "starts instantly, which for a small utility is a fair trade.",
    ),
)

TOOLKIT_BY_KEY = {toolkit.key: toolkit for toolkit in TOOLKITS}
TOOLKIT_ORDER = {toolkit.key: toolkit.rank for toolkit in TOOLKITS}


def best(keys) -> str:
    """The toolkit an application is built on, out of everything found in it."""
    found = [key for key in keys if key in TOOLKIT_ORDER]
    if not found:
        return ""
    return min(found, key=lambda key: TOOLKIT_ORDER[key])


# Sonames, matched as prefixes against DT_NEEDED entries. libffmpeg.so is
# Electron's own bundled copy and no distribution ships a library by that name,
# so it is a reliable second marker for an Electron binary whose directory
# layout is unusual.
SONAME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gtk4", ("libgtk-4.so",)),
    ("gtk3", ("libgtk-3.so",)),
    ("gtk2", ("libgtk-x11-2.0.so", "libgtk-quartz-2.0.so")),
    ("qt6", ("libQt6Core.so", "libQt6Gui.so", "libQt6Widgets.so", "libQt6Quick.so")),
    ("qt5", ("libQt5Core.so", "libQt5Gui.so", "libQt5Widgets.so", "libQt5Quick.so")),
    ("wx", ("libwx_",)),
    ("tk", ("libtk8.", "libtk9.", "libtk.so")),
    ("java", ("libjli.so", "libjvm.so", "libjava.so")),
    ("electron", ("libffmpeg.so",)),
)


def toolkit_for_soname(soname: str) -> str:
    for key, prefixes in SONAME_RULES:
        for prefix in prefixes:
            if soname.startswith(prefix):
                return key
    return ""


# Files that sit beside an Electron binary and beside nothing else. The .pak
# suffix is Chromium's resource bundle and is checked by suffix because its
# stem varies with the locale set that was built in.
ELECTRON_FILES = frozenset(
    {
        "chrome-sandbox",
        "icudtl.dat",
        "libffmpeg.so",
        "snapshot_blob.bin",
        "v8_context_snapshot.bin",
        "chrome_crashpad_handler",
        "LICENSES.chromium.html",
    }
)
ELECTRON_ASAR = frozenset({"app.asar", "electron.asar", "default_app.asar"})

# What a script says about itself. Matched against the text of an interpreted
# file, so the patterns have to be tight enough that a comment or a string
# mentioning a toolkit does not count as using one.
SCRIPT_RULES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("gtk4", re.compile(r"""require_version\s*\(\s*['"]Gtk['"]\s*,\s*['"]4""")),
    ("gtk3", re.compile(r"""require_version\s*\(\s*['"]Gtk['"]\s*,\s*['"]3""")),
    ("qt6", re.compile(r"^\s*(?:from|import)\s+(?:PySide6|PyQt6)\b", re.M)),
    ("qt5", re.compile(r"^\s*(?:from|import)\s+(?:PySide2|PyQt5)\b", re.M)),
    ("tk", re.compile(r"^\s*(?:from|import)\s+[Tt]kinter\b", re.M)),
    ("wx", re.compile(r"^\s*(?:from|import)\s+wx\b", re.M)),
    ("gtk3", re.compile(r"from\s+gi\.repository\s+import[^\n]*\bGtk\b")),
    ("java", re.compile(r"^\s*(?:exec\s+)?\S*\bjava\b[^\n]*-jar\b", re.M)),
    ("electron", re.compile(r"^\s*(?:exec\s+)?\S*\belectron[0-9]*\b", re.M)),
)

# Package names, matched as prefixes against a package's declared dependencies.
# Last resort, and deliberately short: this is the source that is wrong most
# often, because a package may depend on a toolkit for one optional plugin.
PACKAGE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("electron", ("electron",)),
    ("java", ("java-runtime", "java-environment", "default-jre", "openjdk-")),
    ("wx", ("wxwidgets", "libwxgtk", "wxgtk")),
    ("qt6", ("qt6-base", "libqt6core6", "libqt6gui6", "libqt6widgets6")),
    ("qt5", ("qt5-base", "libqt5core5", "libqt5gui5", "libqt5widgets5")),
    ("gtk4", ("gtk4", "libgtk-4-1")),
    ("gtk3", ("gtk3", "libgtk-3-0")),
    ("gtk2", ("gtk2", "libgtk2.0-0")),
    ("tk", ("tk", "tk8.6", "tk9.0")),
)

# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------
#
# Every one of these exists because the input is not ours. A .desktop file is
# written by whoever installed the application, points wherever it likes, and
# is read at launch on a machine whose window has not appeared yet.

TIME_BUDGET = 0.35  # seconds for the whole survey
MAX_ENTRIES = 2000  # .desktop files considered per directory
MAX_HEAD = 4096  # bytes read from an ELF header and its program headers
MAX_DYNAMIC = 1 << 16  # bytes of PT_DYNAMIC read
MAX_STRTAB = 1 << 18  # bytes of DT_STRTAB read
MAX_SCRIPT = 65536  # bytes of an interpreted file read
MAX_ENTRY_TEXT = 65536  # bytes of a .desktop file read
MAX_PHNUM = 512  # program headers believed
MAX_HOPS = 8  # wrapper scripts followed from one Exec line
MAX_LINKS = 16  # symlinks followed resolving one path
MAX_CANDIDATES = 6  # paths tried out of one wrapper script
MAX_TRANSITIVE = 12  # libraries opened one level below a binary

DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"

# Multilib and multiarch directories, plus the two the Filesystem Hierarchy
# Standard names. Whichever of these exist is where a soname resolves.
LIBRARY_DIRS = (
    "/usr/lib",
    "/usr/lib64",
    "/lib",
    "/lib64",
    "/usr/local/lib",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/usr/lib/i386-linux-gnu",
    "/usr/lib/riscv64-linux-gnu",
    "/lib/x86_64-linux-gnu",
    "/lib/aarch64-linux-gnu",
)

# Filesystem types whose server can be gone. A stat on one of these blocks in
# the kernel until it answers or the mount times out, which can be minutes, and
# there is no way to interrupt it from here - so these are not visited at all.
NETWORK_FILESYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "afs",
        "ceph",
        "9p",
        "fuse.sshfs",
        "fuse.davfs",
        "fuse.rclone",
        "fuse.s3fs",
        "fuse.gvfsd-fuse",
        "fuse.curlftpfs",
    }
)

INTERPRETERS = frozenset(
    {
        "sh",
        "bash",
        "dash",
        "zsh",
        "ksh",
        "env",
        "python",
        "python2",
        "python3",
        "perl",
        "ruby",
        "node",
        "nodejs",
        "wish",
        "tclsh",
    }
)

SCRIPT_SUFFIXES = (".py", ".sh", ".pl", ".rb", ".js", ".jar", ".tcl", ".bash")

# Field codes a desktop entry's Exec line may carry. They are arguments the
# launcher fills in, not part of the command.
_FIELD_CODES = re.compile(r"(?<!%)%[fFuUdDnNickvm]")
_ABSOLUTE_PATH = re.compile(r"/[A-Za-z0-9._+@\-]+(?:/[A-Za-z0-9._+@\-]+)+")
_EXEC_LINE = re.compile(r"^[ \t]*exec[ \t][^\n]*", re.M)


# --------------------------------------------------------------------------
# Guarded reading
# --------------------------------------------------------------------------
#
# base.py's readers were written for sysfs, where every path is the kernel's
# own and every file is a few bytes. These paths are named by third-party
# desktop entries and may be anything at all, so opening one needs two things
# base.py has no reason to do: O_NONBLOCK, so that opening a named pipe returns
# instead of waiting forever for a writer, and a regular-file check, so that a
# character device is never read from. Sizes are capped for the same reason.


def _open(path: str):
    """A read-only handle on *path* if it is an ordinary file, else None."""
    try:
        fd = os.open(
            str(resolve(path)),
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | getattr(os, "O_CLOEXEC", 0),
        )
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return os.fdopen(fd, "rb", closefd=True)
    except (OSError, ValueError):
        try:
            os.close(fd)
        except OSError:
            pass
        return None


def read_head(path: str, limit: int) -> bytes:
    """At most *limit* bytes from the start of *path*. Empty when unreadable."""
    handle = _open(path)
    if handle is None:
        return b""
    try:
        with handle:
            return handle.read(limit) or b""
    except (OSError, ValueError):
        return b""


def read_head_text(path: str, limit: int) -> str:
    raw = read_head(path, limit)
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


def _stat(path: str):
    try:
        return os.stat(str(resolve(path)))
    except (OSError, ValueError):
        return None


def is_regular(path: str) -> bool:
    info = _stat(path)
    return info is not None and stat.S_ISREG(info.st_mode)


def is_executable(path: str) -> bool:
    info = _stat(path)
    return (
        info is not None
        and stat.S_ISREG(info.st_mode)
        and bool(info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    )


def names_in(path: str) -> set[str]:
    """The names in a directory, as a set. Empty when unreadable.

    Not base.list_dir: that sorts what it reads so that cpu2 comes before
    cpu10, which is right for a sysfs enumeration and costs more than the read
    itself for /usr/bin's three thousand entries. Nothing here wants an order.
    """
    try:
        return set(os.listdir(str(resolve(path))))
    except (OSError, ValueError):
        return set()


def follow_links(path: str) -> str:
    """*path* with its symlinks resolved, in paths as seen from the real root.

    Written out rather than handed to ``Path.resolve`` because the paths here
    are logical - a link pointing at ``/usr/bin/vim.basic`` inside a captured
    tree means that path inside the tree, not on the running machine - and
    because a link loop has to end in a shrug rather than in an exception.
    """
    current = path
    for _ in range(MAX_LINKS):
        try:
            target = os.readlink(str(resolve(current)))
        except (OSError, ValueError):
            return current
        if target.startswith("/"):
            current = os.path.normpath(target)
        else:
            current = os.path.normpath(os.path.join(os.path.dirname(current), target))
    return current


# --------------------------------------------------------------------------
# ELF
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Elf:
    """What one executable says it needs.

    ``needed`` is the DT_NEEDED list in file order - the sonames the loader
    will open. ``runpath`` is DT_RUNPATH or DT_RPATH, the directories it will
    look in first, which is how an application finds the private libraries it
    keeps beside itself.
    """

    needed: tuple[str, ...] = ()
    runpath: tuple[str, ...] = ()


PT_LOAD = 1
PT_DYNAMIC = 2
DT_NULL = 0
DT_NEEDED = 1
DT_STRTAB = 5
DT_RPATH = 15
DT_RUNPATH = 29


def read_elf(path: str) -> Elf | None:
    """The dynamic entries of the ELF file at *path*, or None if it is not one.

    None means "this is not an ELF file, or not one this can read" - a script,
    a JPEG, a zero-length file, a truncated download, an object built for a
    machine word size that does not exist. An empty :class:`Elf` means a real
    ELF file that names nothing, which a static binary genuinely does.

    Nothing in here trusts a length. Every offset read out of the file is
    checked against what was actually read before it is used, because these
    numbers come from a file that anything may have written.
    """
    handle = _open(path)
    if handle is None:
        return None
    try:
        with handle:
            return _parse_elf(handle)
    except (OSError, ValueError, struct.error, IndexError, MemoryError):
        return None


def _parse_elf(handle) -> Elf | None:
    head = handle.read(MAX_HEAD)
    if len(head) < 64 or head[:4] != b"\x7fELF":
        return None

    width, endian = head[4], head[5]
    if width not in (1, 2) or endian not in (1, 2):
        return None
    order = "<" if endian == 1 else ">"
    sixty_four = width == 2

    if sixty_four:
        phoff = struct.unpack_from(order + "Q", head, 32)[0]
        phentsize, phnum = struct.unpack_from(order + "HH", head, 54)
        header_format, entry_format, entry_size = order + "IIQQQQQQ", order + "qQ", 16
        header_size = 56
    else:
        phoff = struct.unpack_from(order + "I", head, 28)[0]
        phentsize, phnum = struct.unpack_from(order + "HH", head, 42)
        header_format, entry_format, entry_size = order + "IIIIIIII", order + "iI", 8
        header_size = 32

    if phentsize < header_size or not 0 < phnum <= MAX_PHNUM or phoff <= 0:
        return None

    span = phentsize * phnum
    if phoff + span <= len(head):
        table, base = head, phoff
    else:
        handle.seek(phoff)
        table, base = handle.read(span), 0
        if len(table) < span:
            return None

    segments = []
    for index in range(phnum):
        at = base + index * phentsize
        if at + header_size > len(table):
            break
        segments.append(struct.unpack_from(header_format, table, at))
    if not segments:
        return None

    dynamic = next((item for item in segments if item[0] == PT_DYNAMIC), None)
    if dynamic is None:
        return Elf()

    if sixty_four:
        dyn_offset, dyn_size = dynamic[2], dynamic[5]
    else:
        dyn_offset, dyn_size = dynamic[1], dynamic[4]
    if dyn_offset <= 0 or dyn_size <= 0:
        return Elf()

    handle.seek(dyn_offset)
    blob = handle.read(min(dyn_size, MAX_DYNAMIC))

    needed: list[int] = []
    paths: list[int] = []
    strtab = None
    for at in range(0, len(blob) - entry_size + 1, entry_size):
        tag, value = struct.unpack_from(entry_format, blob, at)
        if tag == DT_NULL:
            break
        if tag == DT_NEEDED:
            needed.append(value)
        elif tag in (DT_RPATH, DT_RUNPATH):
            paths.append(value)
        elif tag == DT_STRTAB:
            strtab = value
    if strtab is None or not (needed or paths):
        return Elf()

    # DT_STRTAB is a virtual address. Turning it into a file offset means
    # finding the PT_LOAD segment that will be mapped over it and subtracting
    # that segment's own virtual address - which is exactly the arithmetic the
    # loader does, and the reason a section header table is not needed here.
    table_bytes = b""
    wanted = max(needed + paths) + 4096
    for item in segments:
        if item[0] != PT_LOAD:
            continue
        if sixty_four:
            vaddr, offset, filesize = item[3], item[2], item[5]
        else:
            vaddr, offset, filesize = item[2], item[1], item[4]
        if not vaddr <= strtab < vaddr + filesize:
            continue
        inside = strtab - vaddr
        handle.seek(offset + inside)
        table_bytes = handle.read(min(wanted, filesize - inside, MAX_STRTAB))
        break
    if not table_bytes:
        return Elf()

    return Elf(
        needed=tuple(_strings(table_bytes, needed)),
        runpath=tuple(
            part
            for value in paths
            for part in _split_runpath(_string_at(table_bytes, value))
        ),
    )


def _string_at(table: bytes, offset: int) -> str:
    if not 0 <= offset < len(table):
        return ""
    end = table.find(b"\0", offset)
    if end < 0:
        end = len(table)
    return table[offset:end].decode("utf-8", errors="replace")


def _strings(table: bytes, offsets) -> list[str]:
    out = []
    for offset in offsets:
        text = _string_at(table, offset)
        if text:
            out.append(text)
    return out


def _split_runpath(text: str) -> list[str]:
    return [part for part in text.split(":") if part]


# --------------------------------------------------------------------------
# Desktop entries
# --------------------------------------------------------------------------


@dataclass
class Application:
    """One visible desktop entry and what it turned out to be built with."""

    name: str
    entry: str
    entry_id: str
    exec_line: str = ""
    try_exec: str = ""
    command: str = ""
    binary: str = ""
    toolkit: str = ""
    evidence: str = ""
    also: tuple[str, ...] = ()

    def describe(self) -> str:
        """The line this application contributes to a toolkit row's body."""
        detail = self.evidence or "no evidence found"
        if self.also:
            others = ", ".join(
                TOOLKIT_BY_KEY[key].label for key in self.also if key in TOOLKIT_BY_KEY
            )
            if others:
                detail += f"; also links {others}"
        return f"{self.name} — {detail}"


def parse_entry(path: str) -> dict[str, str]:
    """The ``[Desktop Entry]`` group of a desktop file, as plain keys.

    Only that group. A modern desktop entry carries ``[Desktop Action …]``
    groups after it, each with its own ``Exec``, and a parser that reads keys
    without tracking the group it is in will happily report LibreOffice's
    Writer action as the application's command.

    Localised keys - ``Name[de]`` - are dropped, so ``Name`` is the untranslated
    string this page should show whatever the session language is. The first
    value for a key wins, as the specification requires.
    """
    text = read_head_text(path, MAX_ENTRY_TEXT)
    if not text:
        return {}
    out: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            inside = line == "[Desktop Entry]"
            continue
        if not inside or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if "[" in key or key in out:
            continue
        out[key] = value.strip()
    return out


def is_true(value: str) -> bool:
    return value.strip().lower() == "true"


def exec_tokens(line: str) -> list[str]:
    """The command an ``Exec=`` line names, with the launcher's own parts removed.

    Field codes are the launcher's arguments, not the program's, and ``%%`` is
    a literal per cent that must survive. A leading ``env`` and any
    ``VAR=value`` assignments in front of the command are stripped, because the
    program being started is the first thing that is not one of those.
    """
    line = _FIELD_CODES.sub(" ", line).replace("%%", "%")
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    while tokens:
        head = tokens[0]
        if head.rsplit("/", 1)[-1] == "env":
            tokens = tokens[1:]
            continue
        name, sep, _ = head.partition("=")
        if sep and not head.startswith("/") and name.isidentifier():
            tokens = tokens[1:]
            continue
        break
    return tokens


def flatpak_id(tokens: list[str]) -> str:
    """The application id out of ``flatpak run --branch=stable org.foo.Bar``."""
    if not tokens or os.path.basename(tokens[0]) != "flatpak":
        return ""
    rest = tokens[1:]
    if not rest or rest[0] != "run":
        return ""
    for token in rest[1:]:
        if not token.startswith("-"):
            return token
    return ""


# --------------------------------------------------------------------------
# Installed versions
# --------------------------------------------------------------------------
#
# The version that matters is the one on disk, because that is the file the
# loader will map. A package manager's opinion is a second-hand account of it,
# and on a machine where a library was replaced by hand the two disagree.
#
# GTK, like the rest of the GLib family, names its libraries by libtool's
# rules: libgtk-3.so.0.2409.32 is (current - age).(age).(revision), where
# current - age is the binary age minus the interface age and revision is the
# interface age. Adding the two back together gives the binary age, 2441, and
# the binary age is 100 * minor + micro - so that file is GTK 3.24.41. The same
# arithmetic reads GTK 2 and GTK 4. Qt states its version outright.


@dataclass
class Installed:
    """A toolkit as it exists on this machine, if it does."""

    version: str = ""
    files: tuple[str, ...] = ()
    detail: str = ""

    @property
    def present(self) -> bool:
        return bool(self.version or self.files)


def library_directories() -> list[str]:
    seen: list[str] = []
    for candidate in LIBRARY_DIRS:
        info = _stat(candidate)
        if info is None or not stat.S_ISDIR(info.st_mode):
            continue
        real = follow_links(candidate)
        if real not in seen:
            seen.append(real)
    return seen


def _libtool_version(major: str, fields: list[int]) -> str:
    """minor.micro out of a GLib-family library file name."""
    if len(fields) < 2:
        return ""
    binary_age = fields[1] + (fields[2] if len(fields) > 2 else 0)
    if binary_age <= 0:
        return ""
    return f"{major}.{binary_age // 100}.{binary_age % 100}"


def _numeric_tail(name: str, marker: str = ".so.") -> list[int]:
    at = name.find(marker)
    if at < 0:
        return []
    out = []
    for part in name[at + len(marker) :].split("."):
        if not part.isdigit():
            return out
        out.append(int(part))
    return out


def library_index() -> list[tuple[str, list[str]]]:
    """Each library directory and its contents, read once for every lookup."""
    return [(directory, sorted(names_in(directory))) for directory in library_directories()]


def _resolved_library(index, prefix: str) -> list[str]:
    """Every distinct real file a soname beginning *prefix* resolves to."""
    out: list[str] = []
    for directory, names in index:
        for name in names:
            if not name.startswith(prefix):
                continue
            real = os.path.basename(follow_links(f"{directory}/{name}"))
            if real not in out:
                out.append(real)
    return out


def installed_versions() -> dict[str, Installed]:
    """What each toolkit's own libraries say their version is, on this machine."""
    directories = library_index()
    out: dict[str, Installed] = {}

    for key, prefix, major in (
        ("gtk4", "libgtk-4.so.", "4"),
        ("gtk3", "libgtk-3.so.", "3"),
        ("gtk2", "libgtk-x11-2.0.so.", "2"),
    ):
        files = _resolved_library(directories, prefix)
        versions = [
            _libtool_version(major, _numeric_tail(name)) for name in files
        ]
        out[key] = Installed(
            version=_join(sorted({item for item in versions if item})),
            files=tuple(files),
        )

    for key, prefix in (("qt6", "libQt6Core.so."), ("qt5", "libQt5Core.so.")):
        files = _resolved_library(directories, prefix)
        versions = []
        for name in files:
            fields = _numeric_tail(name)
            if len(fields) >= 3:
                versions.append(".".join(str(part) for part in fields[:3]))
            elif fields:
                versions.append(str(fields[0]))
        out[key] = Installed(
            version=_join(sorted({item for item in versions if item})),
            files=tuple(files),
        )

    out["wx"] = _wx_version(directories)
    out["tk"] = _tk_version(directories)
    out["java"] = _java_version()
    out["electron"] = _electron_version(directories)
    return out


def _wx_version(directories) -> Installed:
    """wxWidgets names its series in the file and its patch level after it.

    libwx_baseu-3.2.so.0.2.1 is the 3.2 series; the 2 repeats the minor, and
    where it does the 1 after it is the patch level. Where it does not, only
    the series is claimed - a wrong third numeral would be worse than none.
    """
    files = _resolved_library(directories, "libwx_baseu-")
    if not files:
        files = _resolved_library(directories, "libwx_")
    versions = set()
    for name in files:
        head = name.split(".so")[0]
        series = head.rsplit("-", 1)[-1]
        parts = series.split(".")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            continue
        fields = _numeric_tail(name)
        if len(fields) >= 3 and fields[1] == int(parts[1]):
            versions.add(f"{series}.{fields[2]}")
        else:
            versions.add(series)
    return Installed(version=_join(sorted(versions)), files=tuple(files))


def _tk_version(directories) -> Installed:
    found, versions = [], set()
    for name in _resolved_library(directories, "libtk"):
        match = re.match(r"libtk(\d+\.\d+)?\.so", name)
        if not match:
            continue
        found.append(name)
        if match.group(1):
            versions.add(match.group(1))
    return Installed(version=_join(sorted(versions)), files=tuple(found))


def _java_version() -> Installed:
    """Read out of each runtime's own release file, which is plain text."""
    versions = set()
    homes = []
    for name in sorted(names_in("/usr/lib/jvm")):
        info = _stat(f"/usr/lib/jvm/{name}")
        if info is None or not stat.S_ISDIR(info.st_mode):
            continue
        homes.append(name)
        text = read_head_text(f"/usr/lib/jvm/{name}/release", 4096)
        for line in text.splitlines():
            if line.startswith("JAVA_VERSION="):
                versions.add(line.partition("=")[2].strip().strip('"'))
    return Installed(version=_join(sorted(versions)), files=tuple(homes))


def _electron_version(index) -> Installed:
    """Electron packaged by the distribution states its version in a file.

    Most Electron applications do not use it and carry their own copy, which
    is the entire complaint about Electron and is said plainly on the row.
    """
    versions = set()
    found = []
    for directory, names in index:
        for name in names:
            if not re.fullmatch(r"electron\d*", name):
                continue
            found.append(name)
            text = read_head_text(f"{directory}/{name}/version", 64).strip()
            if text:
                versions.add(text.lstrip("v"))
    return Installed(version=_join(sorted(versions)), files=tuple(sorted(set(found))))


def _join(items) -> str:
    return ", ".join(items)


# --------------------------------------------------------------------------
# The package database, last
# --------------------------------------------------------------------------


class Packages:
    """Which toolkit a package declares a dependency on.

    Both databases are plain text and are read directly. Neither is searched
    exhaustively: a full file-to-package map means opening every list file the
    package manager holds, which is two thousand files for one answer nothing
    else could produce. Instead the package is guessed from the binary's own
    name, which is right most of the time it is asked, and a miss is a shrug.
    """

    def __init__(self) -> None:
        self._dpkg: dict[str, str] | None = None

    def toolkit(self, binary: str) -> tuple[str, str]:
        name = os.path.basename(binary)
        if not name:
            return "", ""
        for lookup in (self._pacman, self._dpkg_lookup):
            key, package = lookup(name, binary)
            if key:
                return key, f"{package} depends on it"
        return "", ""

    def _pacman(self, name: str, binary: str) -> tuple[str, str]:
        for item in list_dir("/var/lib/pacman/local"):
            if not item.name.startswith(name + "-"):
                continue
            files = read_head_text(
                f"/var/lib/pacman/local/{item.name}/files", MAX_SCRIPT
            )
            if files and binary.lstrip("/") not in files:
                continue
            desc = read_head_text(f"/var/lib/pacman/local/{item.name}/desc", MAX_SCRIPT)
            key = _depends_toolkit(_section(desc, "%DEPENDS%"))
            if key:
                return key, item.name.rsplit("-", 2)[0]
        return "", ""

    def _dpkg_lookup(self, name: str, binary: str) -> tuple[str, str]:
        listing = read_head_text(f"/var/lib/dpkg/info/{name}.list", MAX_SCRIPT)
        if not listing or binary not in listing.splitlines():
            return "", ""
        if self._dpkg is None:
            self._dpkg = _dpkg_depends()
        key = _depends_toolkit(
            [part.strip() for part in self._dpkg.get(name, "").replace("|", ",").split(",")]
        )
        return (key, name) if key else ("", "")


def _section(text: str, header: str) -> list[str]:
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("%") and line.endswith("%"):
            inside = line == header
            continue
        if inside and line:
            out.append(line)
    return out


def _depends_toolkit(entries) -> str:
    found = []
    for entry in entries:
        bare = re.split(r"[<>=\s(]", entry.strip(), maxsplit=1)[0].strip()
        if not bare:
            continue
        for key, prefixes in PACKAGE_RULES:
            if any(bare == prefix or bare.startswith(prefix) for prefix in prefixes):
                found.append(key)
    return best(found)


def _dpkg_depends() -> dict[str, str]:
    """Package to Depends line, read once out of dpkg's status file."""
    text = read_head_text("/var/lib/dpkg/status", 1 << 24)
    out: dict[str, str] = {}
    package = ""
    for line in text.splitlines():
        if line.startswith("Package: "):
            package = line[9:].strip()
        elif line.startswith("Depends: ") and package:
            out[package] = out.get(package, "") + ", " + line[9:].strip()
    return out


# --------------------------------------------------------------------------
# The survey
# --------------------------------------------------------------------------


@dataclass
class Survey:
    applications: list[Application] = field(default_factory=list)
    directories: list[tuple[str, int]] = field(default_factory=list)
    hidden: int = 0
    duplicates: int = 0
    skipped: int = 0
    network_skipped: int = 0
    seconds: float = 0.0
    installed: dict[str, Installed] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)

    def by_toolkit(self, key: str) -> list[Application]:
        return [item for item in self.applications if item.toolkit == key]

    @property
    def unclassified(self) -> list[Application]:
        return [item for item in self.applications if not item.toolkit]

    @property
    def counts(self) -> dict[str, int]:
        out = {toolkit.key: 0 for toolkit in TOOLKITS}
        for item in self.applications:
            if item.toolkit in out:
                out[item.toolkit] += 1
        return out


def application_directories() -> list[str]:
    """Every directory a desktop entry may be installed in, in search order.

    XDG_DATA_DIRS is honoured because that is where a Nix profile, a Snap
    install or a second prefix puts its entries, and the Flatpak export
    directories are added whether or not the session listed them, because a
    session started before Flatpak was installed will not have.
    """
    home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.environ.get("HOME", "/root"), ".local/share"
    )
    system = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    roots = [home] + [part for part in system.split(":") if part]
    roots += [
        "/var/lib/flatpak/exports/share",
        os.path.join(home, "flatpak/exports/share"),
        "/var/lib/snapd/desktop",
    ]
    out: list[str] = []
    for root in roots:
        directory = os.path.normpath(os.path.join(root, "applications"))
        if directory not in out:
            out.append(directory)
    return out


def network_prefixes() -> list[str]:
    """Mount points whose filesystem can stop answering.

    Read from mountinfo, which is a file, and used to keep the scan off paths
    where a stat may never return.
    """
    out: list[str] = []
    text = read_head_text("/proc/self/mountinfo", 1 << 20)
    for line in text.splitlines():
        head, _, tail = line.partition(" - ")
        if not tail:
            continue
        fields = head.split()
        kind = tail.split()[0] if tail.split() else ""
        if len(fields) > 4 and kind in NETWORK_FILESYSTEMS:
            point = fields[4].replace("\\040", " ")
            if point != "/":
                out.append(point)
    return out


class Scanner:
    """One pass over the installed applications, under a wall-clock budget."""

    def __init__(self, budget: float = TIME_BUDGET) -> None:
        self.budget = budget
        self.started = time.monotonic()
        self.libraries = library_directories()
        self.network = network_prefixes()
        self.packages = Packages()
        self.sources: dict[str, int] = {}
        self._elf: dict[str, Elf | None] = {}
        self._soname: dict[str, str] = {}
        self._names: dict[str, set[str]] = {}
        self._search = [
            part
            for part in (os.environ.get("PATH") or DEFAULT_PATH).split(":")
            if part.startswith("/")
        ]

    # -- bounds -----------------------------------------------------------

    def spent(self) -> float:
        return time.monotonic() - self.started

    def out_of_time(self) -> bool:
        return self.spent() >= self.budget

    def on_network(self, path: str) -> bool:
        return any(
            path == point or path.startswith(point.rstrip("/") + "/")
            for point in self.network
        )

    # -- cached reads -----------------------------------------------------

    def elf(self, path: str) -> Elf | None:
        if path not in self._elf:
            self._elf[path] = read_elf(path)
        return self._elf[path]

    def directory_names(self, path: str) -> set[str]:
        if path not in self._names:
            self._names[path] = names_in(path)
        return self._names[path]

    # -- resolution -------------------------------------------------------

    def which(self, command: str) -> str:
        """*command* as an absolute path, resolved against PATH here in Python.

        The desktop entry may name an absolute path already, in which case this
        only has to check it exists.
        """
        if not command:
            return ""
        if command.startswith("/"):
            return command if is_regular(command) else ""
        if "/" in command:
            return ""
        for directory in self._search:
            candidate = f"{directory.rstrip('/')}/{command}"
            if self.on_network(candidate):
                continue
            if is_regular(candidate):
                return candidate
        return ""

    def resolve_soname(self, soname: str, origin: str, runpath) -> str:
        """Where the loader would find *soname*, near enough for reading it."""
        if "/" in soname:
            return soname
        for directory in list(runpath) + self.libraries:
            directory = directory.replace("$ORIGIN", origin).replace("${ORIGIN}", origin)
            if not directory.startswith("/"):
                continue
            candidate = f"{directory.rstrip('/')}/{soname}"
            if self.on_network(candidate):
                continue
            if is_regular(candidate):
                return candidate
        return ""

    # -- the sources ------------------------------------------------------

    def electron_markers(self, binary: str) -> str:
        """Chromium's own files, sitting beside the binary. Nothing else has them."""
        directory = os.path.dirname(binary)
        if not directory:
            return ""
        names = self.directory_names(directory)
        if not names:
            return ""
        hit = sorted(names & ELECTRON_FILES)
        if hit:
            return hit[0]
        pak = sorted(name for name in names if name.endswith(".pak"))
        if pak:
            return pak[0]
        if "resources" in names:
            inner = self.directory_names(f"{directory}/resources")
            asar = sorted(inner & ELECTRON_ASAR)
            if asar:
                return f"resources/{asar[0]}"
        return ""

    def from_sonames(self, elf: Elf, binary: str) -> tuple[str, str, list[str]]:
        """The toolkit named directly, then the one named one level down."""
        found = []
        for soname in elf.needed:
            key = toolkit_for_soname(soname)
            if key:
                found.append((key, soname))
        if found:
            chosen = best(key for key, _ in found)
            soname = next(name for key, name in found if key == chosen)
            return chosen, f"links {soname}", sorted({key for key, _ in found} - {chosen})

        # Nothing directly. A large application keeps its interface in its own
        # library and links that, so the libraries it does name are opened -
        # once each, cached by soname, and only for a binary that got this far.
        origin = os.path.dirname(binary)
        for soname in elf.needed[:MAX_TRANSITIVE]:
            if self.out_of_time():
                break
            if soname in self._soname:
                key = self._soname[soname]
            else:
                key = ""
                path = self.resolve_soname(soname, origin, elf.runpath)
                inner = self.elf(path) if path else None
                if inner is not None:
                    key = best(
                        toolkit_for_soname(name) for name in inner.needed
                    )
                self._soname[soname] = key
            if key:
                return key, f"links {soname}, which links {TOOLKIT_BY_KEY[key].label}", []
        return "", "", []

    def from_script(self, path: str) -> tuple[str, str, list[str]]:
        """What an interpreted file imports, and where its wrapper points."""
        text = read_head_text(path, MAX_SCRIPT)
        if not text:
            return "", "", []
        found = []
        for key, pattern in SCRIPT_RULES:
            if pattern.search(text):
                found.append(key)
        if found:
            chosen = best(found)
            return chosen, f"{os.path.basename(path)} uses {TOOLKIT_BY_KEY[chosen].label}", []
        return "", "", []

    def script_targets(self, path: str) -> list[str]:
        """The paths a wrapper script hands control to, best first.

        A wrapper is nearly always one exec line naming an absolute path, so
        those are read first; anything else absolute in the file is tried after
        them. Paths that are not ordinary files, and paths that are neither
        executable nor obviously a script, are dropped - which removes the
        icons, the configuration files and /dev/null that every wrapper also
        mentions.
        """
        text = read_head_text(path, MAX_SCRIPT)
        if not text:
            return []
        ordered: list[str] = []
        for line in _EXEC_LINE.findall(text) + [text]:
            for candidate in _ABSOLUTE_PATH.findall(line):
                if candidate in ordered or candidate == path:
                    continue
                if os.path.basename(candidate) in INTERPRETERS:
                    continue
                if self.on_network(candidate) or not is_regular(candidate):
                    continue
                if not (
                    is_executable(candidate) or candidate.endswith(SCRIPT_SUFFIXES)
                ):
                    continue
                ordered.append(candidate)
                if len(ordered) >= MAX_CANDIDATES:
                    return ordered
        return ordered

    # -- one application --------------------------------------------------

    def classify(self, app: Application) -> None:
        tokens = exec_tokens(app.exec_line)
        app.command = tokens[0] if tokens else ""

        identifier = flatpak_id(tokens)
        if identifier and self.classify_flatpak(app, identifier):
            return

        binary = self.which(app.command)
        if not binary and app.try_exec:
            # TryExec names the file whose presence gates the entry. Where Exec
            # names something that is not installed, it is the one that
            # resolves, and it points at the same program.
            binary = self.which(exec_tokens(app.try_exec)[:1] and exec_tokens(app.try_exec)[0] or "")
        if not binary:
            app.evidence = (
                f"{app.command or 'the Exec line'} could not be found in PATH"
            )
            self.count("unresolved")
            return
        self.inspect(app, binary)

    def classify_flatpak(self, app: Application, identifier: str) -> bool:
        """A Flatpak application, read out of its deployment rather than run.

        The deployed files are on disk and are read exactly like anything else,
        which is worth doing: the runtime name alone says GNOME or KDE but not
        which major version of the toolkit the application actually links.
        """
        for base in (
            f"/var/lib/flatpak/app/{identifier}/current/active",
            os.path.join(
                os.environ.get("XDG_DATA_HOME")
                or os.path.join(os.environ.get("HOME", "/root"), ".local/share"),
                f"flatpak/app/{identifier}/current/active",
            ),
        ):
            metadata = read_head_text(f"{base}/metadata", MAX_ENTRY_TEXT)
            if not metadata:
                continue
            fields = _ini_group(metadata, "[Application]")
            command = fields.get("command", "")
            runtime = fields.get("runtime", "")
            if command:
                binary = f"{base}/files/bin/{os.path.basename(command)}"
                if is_regular(binary):
                    self.inspect(app, binary, flatpak=identifier)
                    if app.toolkit:
                        return True
            key = _runtime_toolkit(runtime)
            app.binary = app.binary or f"{base}/files"
            if key:
                app.toolkit = key
                app.evidence = f"Flatpak on the {runtime.split('/')[0]} runtime"
                self.count("flatpak")
                return True
            if runtime:
                app.evidence = (
                    f"Flatpak on {runtime.split('/')[0]}, which does not name a toolkit"
                )
                self.count("flatpak")
                return True
        return False

    def inspect(self, app: Application, binary: str, flatpak: str = "", hops: int = 0) -> None:
        """Follow *binary* until something says what it is built with."""
        if self.on_network(binary):
            app.evidence = "on a network filesystem, not visited"
            self.count("network")
            return
        binary = follow_links(binary)
        app.binary = app.binary or binary

        marker = self.electron_markers(binary)
        if marker:
            app.toolkit = "electron"
            app.evidence = f"{marker} beside {os.path.basename(binary)}"
            self.count("markers")
            return

        elf = self.elf(binary)
        if elf is not None:
            key, evidence, also = self.from_sonames(elf, binary)
            if key:
                app.toolkit, app.evidence, app.also = key, evidence, tuple(also)
                self.count("libraries")
                return
            app.evidence = f"{os.path.basename(binary)} links no toolkit"
        else:
            key, evidence, _ = self.from_script(binary)
            if key:
                app.toolkit, app.evidence = key, evidence
                self.count("scripts")
                return
            app.evidence = f"{os.path.basename(binary)} is not an executable image"

        if hops < MAX_HOPS and not self.out_of_time():
            for candidate in self.script_targets(binary):
                self.inspect(app, candidate, flatpak=flatpak, hops=hops + 1)
                if app.toolkit:
                    return

        if not app.toolkit and not self.out_of_time():
            key, evidence = self.packages.toolkit(app.binary or binary)
            if key:
                app.toolkit, app.evidence = key, evidence
                self.count("packages")

    def count(self, source: str) -> None:
        self.sources[source] = self.sources.get(source, 0) + 1


def _ini_group(text: str, header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            inside = line == header
            continue
        if inside and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out.setdefault(key.strip(), value.strip())
    return out


def _runtime_toolkit(runtime: str) -> str:
    """org.kde.Platform//6.7 is Qt 6. The GNOME runtime carries both GTKs."""
    name = runtime.split("/")[0]
    branch = runtime.rsplit("/", 1)[-1] if "/" in runtime else ""
    if name == "org.kde.Platform":
        if branch.startswith("5"):
            return "qt5"
        if branch.startswith("6"):
            return "qt6"
    return ""


def survey(budget: float = TIME_BUDGET) -> Survey:
    """Every visible application on the machine, and what each is built with."""
    scanner = Scanner(budget)
    result = Survey(installed=installed_versions())

    seen: set[str] = set()
    pending: list[Application] = []
    for directory in application_directories():
        if scanner.on_network(directory):
            result.network_skipped += 1
            continue
        entries = [item for item in list_dir(directory) if item.name.endswith(".desktop")]
        if not entries:
            continue
        taken = 0
        for item in entries[:MAX_ENTRIES]:
            path = f"{directory.rstrip('/')}/{item.name}"
            fields = parse_entry(path)
            if not fields or fields.get("Type", "Application") != "Application":
                continue
            if is_true(fields.get("NoDisplay", "")) or is_true(fields.get("Hidden", "")):
                result.hidden += 1
                continue
            entry_id = item.name[: -len(".desktop")]
            if entry_id in seen:
                result.duplicates += 1
                continue
            seen.add(entry_id)
            taken += 1
            pending.append(
                Application(
                    name=fields.get("Name") or entry_id,
                    entry=path,
                    entry_id=entry_id,
                    exec_line=fields.get("Exec") or fields.get("TryExec", ""),
                    try_exec=fields.get("TryExec", ""),
                )
            )
        result.directories.append((directory, taken))

    for app in pending:
        if scanner.out_of_time():
            result.skipped += 1
            continue
        try:
            scanner.classify(app)
        except (OSError, ValueError, RecursionError):
            app.evidence = "could not be read"
    result.applications = pending
    result.sources = scanner.sources
    result.network_skipped += scanner.sources.get("network", 0)
    result.seconds = scanner.spent()
    return result
