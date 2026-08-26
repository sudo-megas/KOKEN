# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The probe contract, and readers that never raise.

Every probe in this package answers two questions. ``sections()`` enumerates
the machine and returns the full ordered list of rows, once at launch and again
on F5. ``sample()`` returns only the rows whose values move, and is called on
the interval timer; the window matches those rows to widgets it already holds
and sets text on them, so nothing is rebuilt and no expansion collapses.

The readers here are the only way this package touches the filesystem. They
return ``None`` for anything they cannot read - absent, forbidden, or a device
that answers a read with an error - because a missing sysfs file is the normal
case on some machine somewhere, not an exceptional one. Nothing in this
application may crash because a sysfs file was absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Row tiers. Fixed at read time by whichever probe emits the row, never guessed
# later: a row is static because the value cannot change without new hardware.
STATIC = "static"
VOLATILE = "volatile"

# Row severities. `warning` and `danger` colour the value text only.
NORMAL = "normal"
WARNING = "warning"
DANGER = "danger"

# Standard value strings. Every probe uses these rather than inventing its own
# phrasing, so an absent subsystem reads the same wherever it turns up.
NOT_AVAILABLE = "Not available"
NOT_REPORTED = "Not reported"
NONE_PRESENT = "None present"
REQUIRES_ROOT = "Requires administrator access — restart KÖKEN to authenticate"


@dataclass(frozen=True)
class Row:
    """One line of content.

    ``id`` is the explanation key, ``branch.section.field``, matching the keys
    in ``explanations.en.toml`` exactly. It is deliberately not unique: every
    USB device's speed row carries the same id, because they all deserve the
    same explanation.

    ``key`` is what makes a row addressable inside its section for the volatile
    pass. It defaults to ``id``, which is right for the ordinary case of one row
    per field, and is set explicitly where a section repeats a field - one cache
    row per level, one temperature row per hwmon channel.
    """

    id: str
    label: str
    value: str
    tier: str = STATIC
    severity: str = NORMAL
    key: str = ""
    # An expansion body the row carries itself, instead of one looked up by id
    # in the explanation file. Only the About section uses this, to show the
    # licence text without putting 35 kB of GPL into a user-editable TOML.
    body: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            object.__setattr__(self, "key", self.id)

    @property
    def is_volatile(self) -> bool:
        return self.tier == VOLATILE


@dataclass
class Section:
    """One row 3 entry and the rows beneath it.

    ``icon`` is a concept name from :mod:`koken.icons`, set only on instance
    sections - one per USB device, per disk, per network interface. Fixed
    sections such as ``Overview`` carry no icon, and neither do row 1 or row 2.
    """

    id: str
    label: str
    rows: list[Row] = field(default_factory=list)
    icon: str | None = None
    # Set by volumes.py only: the udisks2 object path the mount row acts on.
    # Resolved again at press time, never trusted from here.
    object_path: str | None = None

    def add(self, row: Row) -> None:
        self.rows.append(row)


# --------------------------------------------------------------------------
# Filesystem root
# --------------------------------------------------------------------------
#
# Every reader below resolves through this. It exists so a probe can be run
# against a directory tree that is empty, or that holds a captured copy of
# another machine's sysfs, without the probe knowing the difference. That is
# the only way to test the absent-hardware path on a machine that has hardware,
# and the only way to test the present-hardware path in a container that has
# none.

_root = Path("/")


def set_root(path: str | Path) -> None:
    """Point every reader at *path* instead of ``/``."""
    global _root
    _root = Path(path)


def get_root() -> Path:
    return _root


def resolve(path: str | Path) -> Path:
    """Join *path* under the active root.

    A path that is already inside the root is returned untouched. This matters
    because the glob helpers hand back rooted paths, and probes then build on
    those - ``card["device"] / "vendor"`` is already rooted, and joining it
    under the root a second time would silently address a directory that does
    not exist.
    """
    p = Path(path)
    if _root == Path("/"):
        return p
    if p.is_absolute():
        if p == _root or _root in p.parents:
            return p
        return _root / p.relative_to("/")
    return _root / p


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------
#
# OSError is the base of FileNotFoundError, PermissionError, IsADirectoryError
# and the plain errno failures that sysfs attributes raise when the device
# cannot answer - /sys/class/net/*/speed answers EINVAL on an interface that is
# down, and on every wireless interface. Catching OSError catches all of them.
# ValueError and UnicodeDecodeError cover a file that exists but holds
# something other than what was expected.


def read_text(path: str | Path) -> str | None:
    """File contents with surrounding whitespace stripped, or None."""
    try:
        text = resolve(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    text = text.strip()
    return text or None


def read_bytes(path: str | Path) -> bytes | None:
    """Raw file contents, or None. Used for EDID blobs and efivars."""
    try:
        return resolve(path).read_bytes()
    except (OSError, ValueError):
        return None


def read_first_line(path: str | Path) -> str | None:
    """The first line only, stripped, or None.

    Reads the whole file and takes the first line: every sysfs attribute this
    is used on is a few bytes, and streaming would buy nothing.
    """
    text = read_text(path)
    if text is None:
        return None
    line = text.splitlines()[0].strip() if text.splitlines() else ""
    return line or None


def read_int(path: str | Path, base: int = 0) -> int | None:
    """An integer, or None if absent or not a number.

    Base 0 lets Python read the ``0x`` prefix that sysfs uses for PCI vendor
    and device ids, while still reading plain decimal correctly.
    """
    text = read_first_line(path)
    if text is None:
        return None
    try:
        return int(text, base)
    except (ValueError, TypeError):
        try:
            return int(text)
        except (ValueError, TypeError):
            return None


def read_lines(path: str | Path) -> list[str]:
    """Every non-empty line, stripped. An empty list when unreadable."""
    text = read_text(path)
    if text is None:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def read_link_name(path: str | Path) -> str | None:
    """The final component of a symlink's target, or None if it is not a symlink.

    This is how a driver name is read: ``device/driver`` points at
    ``../../../bus/pci/drivers/amdgpu``, and the useful part is the last one.
    The symlink check is not decoration - without it a plain directory would
    answer with its own name, so a card with no driver bound would report a
    driver called "driver".
    """
    try:
        target = resolve(path)
        if not target.is_symlink():
            return None
        target = target.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    name = target.name
    return name or None


def glob_paths(pattern: str) -> list[Path]:
    """Every path matching an absolute glob *pattern*, naturally sorted.

    Sorted so ``cpu2`` comes before ``cpu10``, which plain lexical sorting
    gets wrong and which shows up in every enumeration in this package.
    """
    p = Path(pattern)
    if p.is_absolute():
        # Root the pattern through resolve() so a pattern that is already
        # inside the root is not rooted twice. Glob metacharacters survive
        # this untouched: resolve() only joins path components.
        rooted = resolve(p)
        base = Path(rooted.anchor)
        relative = rooted.relative_to(base)
    else:
        base, relative = Path("."), p
    try:
        matches = list(base.glob(str(relative)))
    except (OSError, ValueError, NotImplementedError):
        return []
    return sorted(matches, key=lambda item: natural_key(item.name))


def glob_dirs(pattern: str) -> list[Path]:
    """As :func:`glob_paths`, restricted to directories and symlinks to them."""
    out = []
    for item in glob_paths(pattern):
        try:
            if item.is_dir():
                out.append(item)
        except OSError:
            continue
    return out


def list_dir(path: str | Path) -> list[Path]:
    """Directory entries, naturally sorted. An empty list when unreadable."""
    try:
        entries = list(resolve(path).iterdir())
    except (OSError, ValueError):
        return []
    return sorted(entries, key=lambda item: natural_key(item.name))


def path_exists(path: str | Path) -> bool:
    try:
        return resolve(path).exists()
    except (OSError, ValueError):
        return False


_NATURAL = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """Sort key that orders embedded numbers numerically."""
    parts = _NATURAL.split(str(text))
    return tuple(int(part) if part.isdigit() else part for part in parts)


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------
#
# Shared so that two probes never disagree about what a gigabyte is.

_BINARY_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_DECIMAL_UNITS = ("B", "kB", "MB", "GB", "TB", "PB")


def fmt_bytes(value: int | float | None, binary: bool = True) -> str:
    """A byte count as a human-sized string.

    Binary by default, because memory, caches and filesystem sizes are all
    powers of two. Disk capacity is the exception and passes ``binary=False``,
    since that is the number printed on the drive.
    """
    if value is None:
        return NOT_AVAILABLE
    units = _BINARY_UNITS if binary else _DECIMAL_UNITS
    step = 1024.0 if binary else 1000.0
    size = float(value)
    negative = size < 0
    size = abs(size)
    index = 0
    while size >= step and index < len(units) - 1:
        size /= step
        index += 1
    if index == 0:
        text = f"{int(size)} {units[0]}"
    elif size >= 100:
        text = f"{size:.0f} {units[index]}"
    elif size >= 10:
        text = f"{size:.1f} {units[index]}"
    else:
        text = f"{size:.2f} {units[index]}"
    return f"-{text}" if negative else text


def fmt_khz(value: int | float | None) -> str:
    """A kHz reading - the unit cpufreq and the DRM DPM tables use - as MHz or GHz."""
    if value is None:
        return NOT_AVAILABLE
    mhz = float(value) / 1000.0
    if mhz >= 1000:
        return f"{mhz / 1000:.2f} GHz"
    return f"{mhz:.0f} MHz"


def fmt_mhz(value: int | float | None) -> str:
    if value is None:
        return NOT_AVAILABLE
    mhz = float(value)
    if mhz >= 1000:
        return f"{mhz / 1000:.2f} GHz"
    return f"{mhz:.0f} MHz"


def fmt_percent(value: float | None, digits: int = 0) -> str:
    if value is None:
        return NOT_AVAILABLE
    return f"{value:.{digits}f}%"


def fmt_int(value: int | None) -> str:
    """An integer with thousands separators."""
    if value is None:
        return NOT_AVAILABLE
    return f"{value:,}"


def fmt_duration(seconds: float | None) -> str:
    """A span in days, hours and minutes. Used for uptime and power-on hours."""
    if seconds is None:
        return NOT_AVAILABLE
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} day" + ("s" if days != 1 else ""))
    if hours or days:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    return ", ".join(parts)


def fmt_list(items, empty: str = NONE_PRESENT, separator: str = ", ") -> str:
    items = [str(item) for item in items if item]
    return separator.join(items) if items else empty


def fmt_hex_id(value: int | None, width: int = 4) -> str:
    if value is None:
        return NOT_AVAILABLE
    return f"{value:0{width}x}"


def or_missing(value, fallback: str = NOT_AVAILABLE) -> str:
    """A value as text, or the standard absent string when there is none."""
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def yes_no(value: bool | None, yes: str = "Yes", no: str = "No") -> str:
    if value is None:
        return NOT_AVAILABLE
    return yes if value else no


def parse_key_values(lines, separator: str = ":") -> dict[str, str]:
    """Split ``key: value`` lines into a dict. Later keys win."""
    out: dict[str, str] = {}
    for line in lines:
        if separator not in line:
            continue
        key, _, value = line.partition(separator)
        out[key.strip()] = value.strip()
    return out


# --------------------------------------------------------------------------
# The probe itself
# --------------------------------------------------------------------------


@dataclass
class Context:
    """What every probe is handed: the id databases and the privileged read.

    Assembled once at launch and passed down. Probes never reach for a global.
    """

    pci_ids: object | None = None
    usb_ids: object | None = None
    privileged: object | None = None


class Probe:
    """One row 2 section.

    Subclasses set ``branch``, ``id`` and ``label``, and implement
    :meth:`sections`. :meth:`sample` is optional and defaults to nothing, which
    is correct for a probe whose every row is static.
    """

    branch = ""
    id = ""
    label = ""

    def __init__(self, context: Context | None = None) -> None:
        self.context = context if context is not None else Context()

    # -- to implement -----------------------------------------------------

    def sections(self) -> list[Section]:
        """Enumerate. Called at launch and on F5, never on the timer."""
        raise NotImplementedError

    def sample(self) -> dict[str, list[Row]]:
        """The volatile rows only, as ``{section id: rows}``.

        Called on the interval timer. Returning a row whose section or key the
        window does not already hold is harmless; it is ignored.
        """
        return {}

    # -- shared -----------------------------------------------------------

    def key(self, field_name: str) -> str:
        """The explanation key for a field of this probe."""
        return f"{self.branch}.{self.id}.{field_name}"

    def row(
        self,
        field_name: str,
        label: str,
        value: str,
        tier: str = STATIC,
        severity: str = NORMAL,
        key: str = "",
        body: str = "",
    ) -> Row:
        return Row(
            id=self.key(field_name),
            label=label,
            value=value,
            tier=tier,
            severity=severity,
            key=key,
            body=body,
        )

    def empty_section(
        self,
        section_id: str,
        label: str,
        message: str,
        field_name: str = "absent",
    ) -> Section:
        """A section that states plainly that nothing was found.

        Row 3 is never empty and absent hardware is stated, never hidden, so
        every enumeration that finds nothing ends here rather than returning an
        empty list.
        """
        section = Section(id=section_id, label=label)
        section.add(self.row(field_name, "Status", message))
        return section

    def safe_sections(self) -> list[Section]:
        """:meth:`sections` with a floor under it.

        The readers do not raise, but arithmetic on a value some firmware
        reports as nonsense still can. A probe that fails outright renders as a
        section saying so, which is a great deal more useful than a traceback
        and an application that will not start.
        """
        try:
            sections = self.sections()
        except Exception as exc:  # deliberately broad: see the docstring
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    f"This section could not be read on this machine ({type(exc).__name__}).",
                    field_name="error",
                )
            ]
        if not sections:
            return [
                self.empty_section(
                    "overview", "Overview", "Nothing of this kind was found on this machine."
                )
            ]
        return sections

    def safe_sample(self) -> dict[str, list[Row]]:
        """:meth:`sample` with the same floor. A failed sample updates nothing."""
        try:
            return self.sample() or {}
        except Exception:
            return {}
