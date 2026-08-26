# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""How the software on this machine got here.

Every other section describes something the machine is. This one describes
something that was done to it: a few hundred deliberate decisions, each of
which dragged in a handful of packages nobody chose, over however many years
the installation has been running. A roll call of 1,400 names would say
nothing, so this reports the shape of the installation instead - what was
chosen against what followed, what is left over, what is large, what is
foreign, and what landed recently.

Three things are worth stating before reading the code.

Nothing here needs root and nothing here runs a command. Both package
databases are world-readable - that is why ``pacman -Q`` and ``dpkg -l`` work
unprivileged - so this section is file reading like every other section, and
the one authentication prompt KOKEN raises stays reserved for the three things
that genuinely need it.

The orphan rule is the delicate part. A package is an orphan when it was
installed as a dependency and nothing installed still depends on it, which
means the dependency lists have to be read with provides honoured and version
constraints stripped: a package is not an orphan because the package needing
it named a virtual name it provides, and it is not an orphan because the
constraint said ``>= 1.2``. Every ambiguity here is resolved towards *not*
calling something an orphan, because the number is read as permission to
remove things.

The scan runs inside the launch enumeration and so carries a time budget. A
machine with a pathological database gets a section that says what was skipped
rather than a window that never appears. If the budget stops the scan early,
the orphan count is withheld entirely - half a dependency graph produces
orphans that are not orphans.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import time
from dataclasses import dataclass, field

from .base import (
    Probe,
    Section,
    fmt_bytes,
    fmt_int,
    fmt_list,
    list_dir,
    path_exists,
    read_bytes,
    read_text,
    resolve,
)

PACMAN_LOCAL = "/var/lib/pacman/local"
PACMAN_SYNC = "/var/lib/pacman/sync"
DPKG_STATUS = "/var/lib/dpkg/status"
DPKG_INFO = "/var/lib/dpkg/info"
APT_EXTENDED_STATES = "/var/lib/apt/extended_states"

# What the whole section may spend inside the launch enumeration, and what the
# installed-package scan may spend of it. Whatever is left over is what the
# repository databases get, and they are the part that is dropped first: the
# installation is the subject, the repositories are context for one row.
BUDGET = 2.0
LOCAL_BUDGET = 1.2
# The clock is consulted once per this many records rather than once per
# record. time.monotonic() is cheap, but not as cheap as not calling it.
CLOCK_INTERVAL = 64

# A compressed repository database is a system file, not input from anywhere,
# but a bounded decompression still beats an unbounded one: this is inside
# launch. Arch's largest repository database unpacks to about 40 MB.
MAX_DB_BYTES = 128 * 1024 * 1024

LIST_LIMIT = 25  # longest list written into an expansion body
TOP_SIZES = 10
RECENT_COUNT = 10
RECENT_WINDOW = 7 * 86400

# Timestamps outside this range are a database that was written wrong, not a
# machine that was installed in 1970 or will be in 3000.
EARLIEST = 315532800  # 1980-01-01
LATEST = 4102444800  # 2100-01-01


# --------------------------------------------------------------------------
# The time budget
# --------------------------------------------------------------------------


class _Clock:
    """A deadline the scan asks about as it goes.

    Not a timeout in another thread and not a signal: this is a synchronous
    enumeration, and the only honest way to bound it is for the loops to stop
    themselves and say how much they did not read.
    """

    def __init__(self, budget: float) -> None:
        self.started = time.monotonic()
        self.deadline = self.started + max(float(budget), 0.0)

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def remaining(self) -> float:
        return max(self.deadline - time.monotonic(), 0.0)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def share(self, seconds: float) -> "_Clock":
        """A second clock for a subordinate scan, never outliving this one."""
        return _Clock(min(float(seconds), self.remaining()))


# --------------------------------------------------------------------------
# What a package is, here
# --------------------------------------------------------------------------


@dataclass
class _Package:
    """One installed package, reduced to what this section reports.

    ``uid`` separates the two copies of a package that a multi-architecture
    Debian machine holds - ``libc6:amd64`` and ``libc6:i386`` are two
    installations of one name - so that "is anything other than me depending
    on this" cannot answer itself.
    """

    name: str
    version: str = ""
    description: str = ""
    architecture: str = ""
    size: int | None = None
    installed: int | None = None
    explicit: bool = True
    depends: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    validation: str = ""

    @property
    def uid(self) -> str:
        return f"{self.name}:{self.architecture}" if self.architecture else self.name

    def describe(self) -> str:
        """One line for an expansion body: name, version, size, purpose."""
        parts = [fmt_list([self.name, self.version], empty=self.name, separator=" ")]
        if self.size:
            parts.append(fmt_bytes(self.size))
        if self.description:
            parts.append(self.description)
        return " — ".join(parts)


@dataclass
class _Inventory:
    """Everything one package database had to say, and what was missed."""

    manager: str
    source: str
    packages: list[_Package] = field(default_factory=list)
    # Records that were present but could not be understood, and records the
    # budget stopped the scan from reaching. Both are reported, never hidden:
    # a count drawn from part of a database is a different number.
    unreadable: int = 0
    skipped: int = 0

    @property
    def complete(self) -> bool:
        return self.skipped == 0


# --------------------------------------------------------------------------
# pacman: /var/lib/pacman/local/<name>-<version>/desc
# --------------------------------------------------------------------------


def parse_desc(text: str) -> dict[str, list[str]]:
    """A pacman ``desc`` file into ``{field: values}``.

    The format is a field name wrapped in percent signs on a line of its own,
    its values on the lines below it, and a blank line before the next one.
    Anything before the first field name, and any file that holds no field
    name at all, yields nothing rather than raising.
    """
    fields: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            current = None
            continue
        if len(line) > 2 and line[0] == "%" and line[-1] == "%":
            current = fields.setdefault(line[1:-1], [])
            continue
        if current is not None:
            current.append(line)
    return fields


def _first(fields: dict[str, list[str]], name: str) -> str | None:
    values = fields.get(name)
    return values[0] if values else None


def _number(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def strip_constraint(entry: str) -> str:
    """A dependency name with its version constraint and description removed.

    ``readline>=8.2`` is a dependency on readline. ``libgl: for hardware
    acceleration`` is an optional dependency on libgl. ``libfoo.so=1-64`` is a
    dependency on a shared object by name. All three are the same package as
    far as "does anything still need this" is concerned, and matching without
    stripping would report every one of them as needed by nobody.
    """
    text = entry.split(":", 1)[0].strip()
    cut = len(text)
    for mark in "<>=":
        found = text.find(mark)
        if 0 <= found < cut:
            cut = found
    return text[:cut].strip()


def _names(values, limit: int = 512) -> tuple[str, ...]:
    out = []
    for value in values[:limit]:
        name = strip_constraint(value)
        if name:
            out.append(name)
    return tuple(out)


def pacman_package(text: str) -> _Package | None:
    """One ``desc`` file as a package, or None if it names no package."""
    fields = parse_desc(text)
    name = _first(fields, "NAME")
    if not name:
        return None
    return _Package(
        name=name,
        version=_first(fields, "VERSION") or "",
        description=_first(fields, "DESC") or "",
        size=_number(_first(fields, "SIZE")),
        installed=_number(_first(fields, "INSTALLDATE")),
        # The field is absent on a package somebody asked for by name, and
        # holds 1 on a package that arrived to satisfy something else. Any
        # other value is not a reason this application knows, and a package
        # whose reason cannot be read is treated as explicit - the reading
        # that never invents an orphan.
        explicit=_first(fields, "REASON") != "1",
        depends=_names(fields.get("DEPENDS", [])),
        optional=_names(fields.get("OPTDEPENDS", [])),
        provides=_names(fields.get("PROVIDES", [])),
        validation=(_first(fields, "VALIDATION") or "").lower(),
    )


def read_pacman(clock: _Clock) -> _Inventory | None:
    """Every installed package pacman records, or None if pacman is not here.

    Presence decides, not contents: a database that is there and could not be
    read in time is a different answer from no database at all, and only one of
    those two is "this distribution does not work that way".
    """
    if not path_exists(PACMAN_LOCAL):
        return None
    entries = list_dir(PACMAN_LOCAL)
    inventory = _Inventory(manager="pacman", source=PACMAN_LOCAL)
    for index, entry in enumerate(entries):
        if index % CLOCK_INTERVAL == 0 and clock.expired():
            inventory.skipped = len(entries) - index
            break
        text = read_text(entry / "desc")
        if text is None:
            # ALPM_DB_VERSION is a file beside the package directories, not a
            # package. A directory with no readable desc is a damaged record
            # and is counted, because a silently smaller total is a lie.
            if _is_directory(entry):
                inventory.unreadable += 1
            continue
        package = pacman_package(text)
        if package is None:
            inventory.unreadable += 1
            continue
        inventory.packages.append(package)
    return inventory


def _is_directory(path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


# --------------------------------------------------------------------------
# dpkg: /var/lib/dpkg/status, with /var/lib/apt/extended_states beside it
# --------------------------------------------------------------------------


def parse_stanzas(text: str):
    """RFC822-ish stanzas as ``{lowercase field: value}`` dicts.

    Folded continuation lines - the ones a Description or a Conffiles list
    runs onto - start with a space and are skipped: nothing this section reads
    is ever folded, and skipping them costs one comparison per line where
    joining them would cost a list allocation per stanza.
    """
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if not line or line[0] in " \t":
                continue
            name, separator, value = line.partition(":")
            if separator:
                fields[name.strip().lower()] = value.strip()
        if fields:
            yield fields


def debian_names(value: str, limit: int = 512) -> tuple[str, ...]:
    """Every package name a Depends, Recommends or Provides field mentions.

    Alternatives are all counted. ``exim4 | mail-transport-agent`` means the
    depending package is satisfied by either, and counting both is the reading
    that never invents an orphan: a package named as an alternative is a
    package something was willing to use.
    """
    out = []
    for clause in value.split(",")[:limit]:
        for alternative in clause.split("|"):
            text = alternative.strip()
            cut = text.find("(")
            if cut >= 0:
                text = text[:cut]
            cut = text.find("[")
            if cut >= 0:
                text = text[:cut]
            # A multi-architecture qualifier - foo:any, foo:amd64 - names the
            # same package. Architecture is not part of the question here.
            text = text.split(":", 1)[0].strip()
            if text:
                out.append(text)
    return tuple(out)


def read_auto_installed() -> set[str]:
    """The packages apt marked as dragged in, by ``name`` and by ``name:arch``."""
    text = read_text(APT_EXTENDED_STATES)
    if text is None:
        return set()
    marked: set[str] = set()
    for fields in parse_stanzas(text):
        if fields.get("auto-installed") != "1":
            continue
        name = fields.get("package")
        if not name:
            continue
        marked.add(name)
        architecture = fields.get("architecture")
        if architecture:
            marked.add(f"{name}:{architecture}")
    return marked


def read_dpkg(clock: _Clock) -> _Inventory | None:
    """Every installed package dpkg records, or None if dpkg is not here.

    As with pacman: the file being there is what says this machine is managed
    this way, whether or not the scan got to the end of it.
    """
    if not path_exists(DPKG_STATUS):
        return None
    text = read_text(DPKG_STATUS) or ""
    automatic = read_auto_installed()
    inventory = _Inventory(manager="dpkg", source=DPKG_STATUS)
    for index, fields in enumerate(parse_stanzas(text)):
        if index % CLOCK_INTERVAL == 0 and clock.expired():
            # Unlike the pacman scan there is no count of what is left: the
            # stanzas are being generated as they are read. Any non-zero mark
            # is enough to withhold the numbers that need the whole file.
            inventory.skipped = 1
            break
        name = fields.get("package")
        if not name:
            continue
        # "install ok installed" is installed. "deinstall ok config-files" is
        # a package that was removed and left its configuration behind, and
        # counting it would inflate every number in this section.
        state = fields.get("status", "").split()
        if len(state) < 3 or state[2] != "installed":
            continue
        architecture = fields.get("architecture", "")
        size = _number(fields.get("installed-size"))
        package = _Package(
            name=name,
            version=fields.get("version", ""),
            description=fields.get("description", ""),
            architecture=architecture,
            # Debian records this field in kibibytes, pacman records its own
            # in bytes. One unit reaches the rest of this module.
            size=size * 1024 if size is not None else None,
            installed=_install_time(name, architecture),
            explicit=not (
                name in automatic
                or (architecture and f"{name}:{architecture}" in automatic)
            ),
            depends=debian_names(
                ", ".join(
                    part
                    for part in (fields.get("depends"), fields.get("pre-depends"))
                    if part
                )
            ),
            # Recommends is Debian's optional dependency: installed by default,
            # removable without breaking the package. Suggests is weaker than
            # anything pacman records and is not counted.
            optional=debian_names(fields.get("recommends", "")),
            provides=debian_names(fields.get("provides", "")),
        )
        inventory.packages.append(package)
    return inventory


def _install_time(name: str, architecture: str) -> int | None:
    """When dpkg last wrote this package's file list.

    dpkg records no install date anywhere, so the honest substitute is the
    modification time of the list of files the package owns, which dpkg
    rewrites on install and on upgrade. This is the one place in this module
    that asks the filesystem for something other than content, and it goes
    through the same root indirection as every reader.
    """
    for candidate in (f"{name}.list", f"{name}:{architecture}.list" if architecture else ""):
        if not candidate:
            continue
        try:
            return int(resolve(DPKG_INFO).joinpath(candidate).stat().st_mtime)
        except (OSError, ValueError, OverflowError):
            continue
    return None


# --------------------------------------------------------------------------
# The shape of the installation
# --------------------------------------------------------------------------


@dataclass
class _Shape:
    total: int = 0
    explicit: int = 0
    dependency: int = 0
    orphans: list[_Package] = field(default_factory=list)
    optional_only: list[_Package] = field(default_factory=list)
    size: int = 0
    sized: int = 0
    largest: list[_Package] = field(default_factory=list)
    recent: list[_Package] = field(default_factory=list)
    recent_window: int = 0


def orphans(packages: list[_Package]) -> tuple[list[_Package], list[_Package]]:
    """Dependency-installed packages nothing still needs.

    Returns the orphans, and separately those kept alive only by being an
    optional dependency of something. The rule, in one sentence: a package
    installed as a dependency is an orphan when no *other* installed package
    names it, or any name it provides, in a dependency list.

    Two details carry the whole thing. Provides are followed, so a package
    pulled in because something needed ``java-runtime`` is not reported as an
    orphan merely because no dependency list contains its real name. And
    version constraints are stripped on both sides, so ``foo>=1.2`` is a need
    for foo; comparing versions would need a full version comparator, and
    getting it wrong invents orphans, which is the one failure mode this
    number cannot have.
    """
    required: dict[str, set[str]] = {}
    optional: dict[str, set[str]] = {}
    for package in packages:
        for name in package.depends:
            required.setdefault(name, set()).add(package.uid)
        for name in package.optional:
            optional.setdefault(name, set()).add(package.uid)

    unneeded: list[_Package] = []
    optional_only: list[_Package] = []
    for package in packages:
        if package.explicit:
            continue
        satisfies = {package.name, *package.provides}
        if _wanted_by_another(required, satisfies, package.uid):
            continue
        if _wanted_by_another(optional, satisfies, package.uid):
            optional_only.append(package)
            continue
        unneeded.append(package)
    return unneeded, optional_only


def _wanted_by_another(index: dict[str, set[str]], satisfies: set[str], uid: str) -> bool:
    """Whether anything but *uid* itself asked for one of these names."""
    for name in satisfies:
        holders = index.get(name)
        if holders and holders != {uid}:
            return True
    return False


def shape(inventory: _Inventory) -> _Shape:
    packages = inventory.packages
    result = _Shape(total=len(packages))
    result.explicit = sum(1 for package in packages if package.explicit)
    result.dependency = result.total - result.explicit

    # A partial scan cannot answer this: a package whose record was never read
    # is a package whose dependencies were never read, and every one of them
    # would show up here as an orphan that is not one.
    if inventory.complete:
        result.orphans, result.optional_only = orphans(packages)

    sized = [package for package in packages if package.size]
    result.sized = len(sized)
    result.size = sum(package.size or 0 for package in sized)
    result.largest = sorted(sized, key=lambda package: package.size or 0, reverse=True)[
        :TOP_SIZES
    ]

    dated = [package for package in packages if _sane_time(package.installed)]
    dated.sort(key=lambda package: package.installed or 0, reverse=True)
    result.recent = dated[:RECENT_COUNT]
    if dated:
        newest = dated[0].installed or 0
        result.recent_window = sum(
            1 for package in dated if (package.installed or 0) >= newest - RECENT_WINDOW
        )
    return result


def _sane_time(value: int | None) -> bool:
    return value is not None and EARLIEST <= value <= LATEST


def _when(value: int | None) -> str:
    if not _sane_time(value):
        return "date not recorded"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    except (ValueError, OSError, OverflowError):
        return "date not recorded"


# --------------------------------------------------------------------------
# The sync repositories, for the one question the local database cannot answer
# --------------------------------------------------------------------------


@dataclass
class _Repositories:
    names: set[str] = field(default_factory=set)
    read: list[tuple[str, int]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    present: bool = False

    @property
    def usable(self) -> bool:
        """Whether the comparison this exists for can honestly be made.

        One unread repository makes every package that came from it look
        foreign, so a partial read is worse than no read: it produces a
        confident number that is wrong by hundreds.
        """
        return bool(self.read) and not self.failed


def read_repositories(clock: _Clock) -> _Repositories:
    """Which packages the configured repositories offer, by name.

    A repository database is a tar archive of one directory per package. The
    directory names are the whole answer, so the archive is walked at the
    header level and no member is ever extracted.
    """
    result = _Repositories()
    entries = [entry for entry in list_dir(PACMAN_SYNC) if entry.name.endswith(".db")]
    result.present = bool(entries)
    for entry in entries:
        label = entry.name[: -len(".db")]
        if clock.expired():
            result.failed.append(f"{label} (not read: the scan ran out of time)")
            continue
        raw = read_bytes(entry)
        if raw is None:
            result.failed.append(f"{label} (unreadable)")
            continue
        plain, reason = decompress(raw)
        if plain is None:
            result.failed.append(f"{label} ({reason})")
            continue
        names = tar_package_names(plain, clock)
        if names is None:
            result.failed.append(f"{label} (not in the layout this reads)")
            continue
        result.names |= names
        result.read.append((label, len(names)))
    return result


def decompress(raw: bytes) -> tuple[bytes | None, str]:
    """A repository database's bytes, uncompressed, by magic number.

    gzip is what ``repo-add`` writes and what the official repositories ship.
    xz and bzip2 turn up on third-party repositories and cost nothing to
    support. zstd does too, and Python 3.11 cannot read it without another
    library, so that case is named rather than guessed at.
    """
    try:
        if raw[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                return stream.read(MAX_DB_BYTES), ""
        if raw[:6] == b"\xfd7zXZ\x00":
            return lzma.LZMADecompressor().decompress(raw, max_length=MAX_DB_BYTES), ""
        if raw[:3] == b"BZh":
            return bz2.BZ2Decompressor().decompress(raw, max_length=MAX_DB_BYTES), ""
        if raw[:4] == b"\x28\xb5\x2f\xfd":
            return None, "zstd compressed, which needs a library KOKEN does not carry"
        if raw[257:262] == b"ustar":
            return raw, ""
    except (OSError, ValueError, EOFError, MemoryError, lzma.LZMAError) as exc:
        return None, f"unreadable: {type(exc).__name__}"
    return None, "not a format this reads"


def tar_package_names(raw: bytes, clock: _Clock) -> set[str] | None:
    """Package names from a tar archive's member headers.

    Walking headers rather than handing the archive to ``tarfile`` is worth
    it here and nowhere else in this application: a repository database holds
    28,000 members, and building an object for each of them costs half a
    second of launch to answer a question the 100-byte name field already
    answers. Every member's path begins with ``<name>-<version>-<release>``,
    so the first component, with two fields taken off the end, is the name.
    """
    if raw[257:262] != b"ustar":
        return None
    names: set[str] = set()
    offset = 0
    total = len(raw)
    seen = 0
    while offset + 512 <= total:
        header = raw[offset : offset + 512]
        if header[0:1] == b"\x00":
            break  # the run of zero blocks that ends an archive
        try:
            size = int(header[124:136].split(b"\x00", 1)[0].strip() or b"0", 8)
        except ValueError:
            break  # a header this does not understand ends the walk honestly
        if size < 0:
            break
        path = header[:100].split(b"\x00", 1)[0].decode("utf-8", "replace")
        head = path.split("/", 1)[0]
        if head:
            names.add(head.rsplit("-", 2)[0])
        offset += 512 + ((size + 511) // 512) * 512
        seen += 1
        if seen % 4096 == 0 and clock.expired():
            break
    return names


# --------------------------------------------------------------------------
# The section
# --------------------------------------------------------------------------


def packages_section(probe: Probe, budget: float = BUDGET) -> Section:
    """The Packages section of ``System -> Operating system``.

    Takes the probe whose row ids the rows should carry - ``SystemProbe``, so
    that every id reads ``system.os.pkg_*`` - and returns a finished section.
    It never raises: a failure in here would otherwise take Overview,
    Distribution and Init down with it.
    """
    section = Section(id="packages", label="Packages")
    try:
        _fill(probe, section, _Clock(budget))
    except Exception as exc:  # deliberately broad: this runs inside launch
        section.rows.clear()
        section.add(
            probe.row(
                "pkg_error",
                "Status",
                f"The package database could not be read on this machine "
                f"({type(exc).__name__}).",
            )
        )
    return section


def _read_inventory(clock: _Clock) -> _Inventory | None:
    """Whichever database this machine actually keeps.

    Both are looked for, because a machine can hold the other one as a tool -
    an Arch box with dpkg installed to unpack a .deb, a Debian box with pacman
    for a chroot - and the one that has packages in it is the one that manages
    this machine. dpkg is only read when pacman has nothing to say, so the
    common case parses one database and not two.
    """
    pacman = read_pacman(clock)
    if pacman is not None and pacman.packages:
        return pacman
    dpkg = read_dpkg(clock)
    if dpkg is not None and dpkg.packages:
        return dpkg
    return pacman if pacman is not None else dpkg


def _fill(probe: Probe, section: Section, clock: _Clock) -> None:
    inventory = _read_inventory(clock.share(LOCAL_BUDGET))
    if inventory is None:
        _absent(probe, section)
        return

    result = shape(inventory)
    _source_rows(probe, section, inventory, clock)
    _count_rows(probe, section, inventory, result)
    _orphan_row(probe, section, inventory, result)
    _size_rows(probe, section, result)
    _foreign_rows(probe, section, inventory, clock)
    _recent_row(probe, section, inventory, result)


def _absent(probe: Probe, section: Section) -> None:
    section.add(
        probe.row(
            "pkg_absent",
            "Status",
            "No package database was found on this machine.",
            body=(
                "KÖKEN reads two of them: pacman's, one directory per package under "
                f"{PACMAN_LOCAL}, and dpkg's, one stanza per package in {DPKG_STATUS}. "
                "Neither is here.\n\n"
                "That is not a fault. A source-based distribution builds its software "
                "from recipes and records it in its own tree; a functional one keeps "
                "every version of everything in a store and describes the system as a "
                "single expression; an image-based one ships the whole filesystem as a "
                "unit and has no per-package record to keep. All three are ordinary "
                "ways to run a Linux machine, and none of them writes either of the two "
                "databases this section knows how to read.\n\n"
                "Everything else KÖKEN reports about this machine is unaffected. Only "
                "this one section has nothing to say."
            ),
        )
    )


def _source_rows(probe: Probe, section: Section, inventory: _Inventory, clock: _Clock) -> None:
    other = DPKG_STATUS if inventory.manager == "pacman" else PACMAN_LOCAL
    body = (
        f"Read from {inventory.source}, which is where {inventory.manager} keeps its "
        "own record of what is installed. No command was run and no privilege was "
        "asked for: both package databases are world-readable, which is why "
        "'pacman -Q' and 'dpkg -l' work without sudo, and this section is file "
        "reading like every other section here.\n\n"
        "These are the records as the last install or upgrade left them. Nothing in "
        "this section contacts a repository, refreshes an index or resolves anything; "
        "a package the machine has not been told about yet does not appear."
    )
    if path_exists(other):
        body += (
            f"\n\n{other} is present on this machine as well. The larger database is "
            "the one reported here; a machine carrying both usually has the second "
            "one installed as a tool rather than as the thing that manages it."
        )
    section.add(
        probe.row(
            "pkg_source",
            "Package database",
            inventory.manager,
            gloss=inventory.source,
            body=body,
        )
    )

    notes = []
    if inventory.skipped:
        notes.append(
            f"The scan stopped after {round(clock.elapsed(), 1)} seconds, its budget for "
            "this section, with "
            + (
                f"{fmt_int(inventory.skipped)} records still unread"
                if inventory.manager == "pacman"
                else "part of the file still unread"
            )
            + ". Every count below describes what was read, not the whole installation, "
            "and the orphan row is withheld: an orphan worked out from half a "
            "dependency graph is not an orphan, and this one is not going to guess."
        )
    if inventory.unreadable:
        notes.append(
            f"{fmt_int(inventory.unreadable)} record"
            + ("s" if inventory.unreadable != 1 else "")
            + " could not be understood and "
            + ("are" if inventory.unreadable != 1 else "is")
            + " left out of every count below. A damaged record is normally the result "
            "of an install that was interrupted part way."
        )
    if notes:
        section.add(
            probe.row(
                "pkg_scan",
                "Scan",
                "Incomplete — expand for what was missed"
                if inventory.skipped
                else f"{fmt_int(inventory.unreadable)} record"
                + ("s" if inventory.unreadable != 1 else "")
                + " could not be read",
                body="\n\n".join(notes),
            )
        )


def _count_rows(
    probe: Probe, section: Section, inventory: _Inventory, result: _Shape
) -> None:
    section.add(
        probe.row(
            "pkg_total",
            "Installed",
            fmt_int(result.total),
            gloss="packages"
            if inventory.complete
            else "packages read before the scan ran out of time",
            body=(
                "Every package the database records as fully installed. On the dpkg "
                "side that means the ones whose status ends in 'installed'; a package "
                "that was removed but left its configuration files behind is not "
                "counted, which is the difference between this number and the longer "
                "one 'dpkg -l' prints.\n\n"
                "A package count does not compare between distributions. Debian splits "
                "a library, its headers and its documentation into three packages where "
                "Arch usually ships one, so 2,900 packages on one machine and 1,400 on "
                "the other can be the same software. The number is worth comparing "
                "against this machine last year, not against somebody else's machine."
            ),
        )
    )
    section.add(
        probe.row(
            "pkg_explicit",
            "Chosen deliberately",
            fmt_int(result.explicit),
            gloss=_portion(result.explicit, result.total),
            body=(
                "The packages somebody asked for by name. Everything else on this "
                "machine followed from these.\n\n"
                "This is the honest measure of what was installed here, because it is "
                "the only count that moves when a person decides something. The "
                "dependency count grows on its own as upstream projects take on more "
                "libraries; this one grows when someone types an install command. It is "
                "also the list worth keeping before a rebuild: reinstall these by name "
                "and the rest comes back on its own.\n\n"
                "The flag records intent, not need. It survives upgrades and "
                "reinstalls, and it changes only when a package is installed by name "
                "again or when it is set by hand — 'pacman -D --asdeps' and "
                "'--asexplicit' on Arch, 'apt-mark auto' and 'apt-mark manual' on "
                "Debian. A package first pulled in as a dependency years ago and used "
                "directly ever since still counts as a dependency here."
            ),
        )
    )
    section.add(
        probe.row(
            "pkg_dependency",
            "Pulled in as dependencies",
            fmt_int(result.dependency),
            gloss=_portion(result.dependency, result.total),
            body=(
                "Packages nobody asked for. Each one is here because something else "
                "needed it, and the ratio against the row above is the real cost of the "
                "choices made on this machine: a few hundred decisions, a few thousand "
                "packages.\n\n"
                "They are not lesser packages — a dependency is as installed as "
                "anything else, and the flag says nothing about what the software does. "
                "It says only who asked for it, which is what makes it possible to work "
                "out, further down, which of them nothing needs any more."
            ),
        )
    )


def _portion(part: int, total: int) -> str:
    if not total:
        return ""
    return f"{round(100.0 * part / total)}% of the installation"


def _orphan_row(
    probe: Probe, section: Section, inventory: _Inventory, result: _Shape
) -> None:
    if not inventory.complete:
        return
    rule = (
        "An orphan is a package that arrived as somebody else's dependency and that "
        "nothing installed depends on any more. Whatever pulled it in is gone; the "
        "package stayed. This is leftovers, not damage: an orphan costs disk and "
        "nothing else, and a machine that has been upgraded for years accumulates "
        "them quietly.\n\n"
        "How this count is worked out. Every installed package's dependency list is "
        "read, and any package installed as a dependency that no other installed "
        "package names is counted here. Names a package provides count as its own: "
        "something pulled in to satisfy a virtual name is not an orphan because no "
        "list mentions its real name. Version constraints are stripped before "
        "matching, so a package needed as 'foo>=1.2' is a package that is needed. "
        "Packages that something still lists as an optional dependency are left out, "
        "which is the same rule 'pacman -Qtdq' uses.\n\n"
        "Why removing one is usually safe. Nothing declares a need for it, so nothing "
        "the package manager knows about can break. Both package managers will do it "
        "in one command, and both will refuse if the reasoning was wrong.\n\n"
        "Why it is not always safe. The flag records intent, not use: something "
        "installed as a dependency years ago may be a program that is used directly "
        "every day, and nothing records that. Nothing declares a runtime plugin "
        "either — a library opened by dlopen, a codec, a theme engine, an interpreter "
        "module imported by name — so a real dependency can appear in no dependency "
        "list at all. And anything built by hand outside the package manager, linked "
        "against a library that is on this list, is invisible to all of this.\n\n"
        "The list is one layer. Remove these and whatever only they depended on "
        "becomes orphaned in turn, which is why the command gets run again until it "
        "prints nothing."
    )
    if result.optional_only:
        rule += (
            f"\n\nA further {fmt_int(len(result.optional_only))} package"
            + ("s are" if len(result.optional_only) != 1 else " is")
            + " unneeded except that something still lists "
            + ("them" if len(result.optional_only) != 1 else "it")
            + " as an optional dependency. They are not counted above."
        )
    count = len(result.orphans)
    if count:
        body = rule + "\n\n" + "\n".join(_listing(result.orphans))
        gloss = "nothing installed depends on them"
    else:
        body = rule
        gloss = "every dependency here is still needed by something"
    section.add(
        probe.row(
            "pkg_orphans",
            "Orphans",
            fmt_int(count) + (" — expand for the list" if count else ""),
            gloss=gloss,
            body=body,
        )
    )


def _size_rows(probe: Probe, section: Section, result: _Shape) -> None:
    section.add(
        probe.row(
            "pkg_size",
            "Installed size",
            fmt_bytes(result.size) if result.sized else "Not recorded",
            gloss=f"across {fmt_int(result.sized)} packages that record one"
            if result.sized != result.total
            else "",
            body=(
                "The sum of what each package says its own files come to, as recorded "
                "when it was built.\n\n"
                "This is not disk usage and will not match 'df'. Filesystem block "
                "padding makes the real figure larger; files shared between packages, "
                "hard links, and any compressing filesystem underneath make it smaller. "
                "Nothing here was measured by walking the filesystem — that is a "
                "several-second scan for a number the packages already state."
            ),
        )
    )
    if not result.largest:
        return
    total = sum(package.size or 0 for package in result.largest)
    section.add(
        probe.row(
            "pkg_largest",
            "Largest packages",
            f"{fmt_bytes(total)} in the {_count_word(len(result.largest))} largest "
            "— expand for the list",
            gloss=_portion(total, result.size),
            body=(
                "The biggest packages installed, by the size they record.\n\n"
                "Large is not the same as wasteful. The names expected near the top are "
                "firmware collections, which carry blobs for hardware this machine does "
                "not have but might; compilers and their standard libraries; browsers; "
                "language runtimes; and anything shipping fonts, icons or sample data. "
                "A name here that means nothing to you is the one worth looking up.\n\n"
                + "\n".join(_listing(result.largest))
            ),
        )
    )


def _count_word(value: int) -> str:
    words = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
    }
    return words.get(value, fmt_int(value))


def _foreign_rows(
    probe: Probe, section: Section, inventory: _Inventory, clock: _Clock
) -> None:
    meaning = (
        "A foreign package is one that is installed but that no configured repository "
        "offers: built from the AUR, built by hand, or installed from a file that was "
        "downloaded once.\n\n"
        "What it means for updates is the whole point of knowing. Every update on this "
        "machine comes from a repository, so these get none — a full system upgrade "
        "will not touch them, however old they are. They are yours to rebuild, and the "
        "moment that matters is when a library they were built against changes its "
        "soname: the package manager has no idea these exist, upgrades the library, "
        "and the hand-built binary stops starting with a complaint about a missing "
        "shared object. They are also the packages whose contents no distribution "
        "reviewed."
    )
    if inventory.manager != "pacman":
        section.add(
            probe.row(
                "pkg_foreign",
                "Foreign packages",
                "Not determined on this machine",
                body=(
                    meaning + "\n\n"
                    "This machine's database is dpkg's, and dpkg does not record where "
                    "a package came from — only apt knows, from the repository indexes "
                    "it downloads. Those indexes are tens of megabytes, and on many "
                    "systems they are compressed in a format Python cannot read without "
                    "another library. Reading them is not something KÖKEN will do "
                    "inside the moment the window takes to appear, so this row states "
                    "that the number is not known rather than offering a guess as if it "
                    "were."
                ),
            )
        )
        return

    repositories = read_repositories(clock.share(clock.remaining()))
    if not repositories.present:
        section.add(
            probe.row(
                "pkg_foreign",
                "Foreign packages",
                "Not determined — no repository databases are present",
                body=(
                    meaning + "\n\n"
                    f"Working the number out means comparing what is installed against "
                    f"what the repositories offer, and {PACMAN_SYNC} holds no databases "
                    "on this machine. They arrive with the first repository refresh."
                ),
            )
        )
        return
    if not repositories.usable:
        section.add(
            probe.row(
                "pkg_foreign",
                "Foreign packages",
                "Not determined — a repository database could not be read",
                body=(
                    meaning + "\n\n"
                    "The comparison was not made, because it would have been wrong "
                    "rather than incomplete: a repository that cannot be read makes "
                    "every package that came from it look foreign, and the number would "
                    "be too large by hundreds with nothing to show which ones.\n\n"
                    "Read: " + fmt_list([name for name, _count in repositories.read],
                                        empty="none")
                    + ".\nNot read: "
                    + fmt_list(repositories.failed, empty="none")
                    + "."
                ),
            )
        )
        return

    foreign = [
        package for package in inventory.packages if package.name not in repositories.names
    ]
    count = len(foreign)
    section.add(
        probe.row(
            "pkg_foreign",
            "Foreign packages",
            fmt_int(count) + (" — expand for the list" if count else ""),
            gloss="installed from outside every configured repository"
            if count
            else "every installed package is offered by a repository",
            body=meaning + ("\n\n" + "\n".join(_listing(foreign)) if count else ""),
        )
    )
    offered = sum(number for _name, number in repositories.read)
    section.add(
        probe.row(
            "pkg_repositories",
            "Repositories",
            fmt_list([name for name, _count in repositories.read]),
            gloss=f"{fmt_int(offered)} packages offered",
            body=(
                "The repository databases this machine has downloaded, and how many "
                "packages they list between them. A package installed here that appears "
                "in none of them is counted as foreign above.\n\n"
                "These files describe what is available, not what is installed. They "
                "are refreshed by a sync, and a stale set of them is the reason an "
                "upgrade sometimes has nothing to do.\n\n"
                + "\n".join(
                    f"{name} — {fmt_int(number)} packages"
                    for name, number in repositories.read
                )
            ),
        )
    )


def _recent_row(
    probe: Probe, section: Section, inventory: _Inventory, result: _Shape
) -> None:
    if inventory.manager == "pacman":
        source = (
            "The date comes from the package's own record and is set when that version "
            "was written into the database, so an upgrade sets it exactly as a first "
            "install does."
        )
    else:
        source = (
            "dpkg records no install date, so the date here is when dpkg last rewrote "
            f"the package's list of files under {DPKG_INFO} — which it does on install "
            "and on upgrade. It is the closest thing this database has to the answer, "
            "and it is worth knowing that is what it is."
        )
    body = (
        "The packages most recently installed or upgraded, newest first.\n\n"
        + source
        + "\n\nThis is the row to read when something worked yesterday and does not "
        "today. An entry dated the day it broke is the first thing to look at, and "
        "both package managers keep the previous version in their cache, so it is "
        "usually a recoverable mistake rather than a permanent one."
    )
    if not result.recent:
        section.add(
            probe.row(
                "pkg_recent",
                "Recently installed",
                "No dates are recorded on this machine",
                body=body,
            )
        )
        return
    newest = result.recent[0]
    body += "\n\n" + "\n".join(
        f"{_when(package.installed)} — {package.describe()}" for package in result.recent
    )
    section.add(
        probe.row(
            "pkg_recent",
            "Recently installed",
            f"{fmt_int(result.recent_window)} in the seven days to "
            f"{_when(newest.installed).split(' ')[0]} — expand for the last "
            f"{_count_word(len(result.recent))}",
            gloss=f"newest {newest.name} {newest.version}".strip(),
            body=body,
        )
    )


def _listing(packages: list[_Package]) -> list[str]:
    """A capped list of package lines for an expansion body."""
    lines = [package.describe() for package in packages[:LIST_LIMIT]]
    remaining = len(packages) - LIST_LIMIT
    if remaining > 0:
        lines.append(f"and {fmt_int(remaining)} more")
    return lines
