# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Memory: what the kernel is using, and what is actually in the slots.

The overview comes from ``/proc/meminfo`` and needs no privilege. The module
detail comes from DMI type 17 by way of the helper, because there is no sysfs
equivalent - the kernel does not publish per-slot part numbers, ranks or
configured speeds anywhere an ordinary user can reach.

Channel configuration is derived rather than read. No firmware reports "this
machine is running dual channel"; what it reports is a bank locator per slot,
and the number of distinct channels named by the *populated* slots is the
answer. Getting this wrong in the pessimistic direction is the interesting
case: two sticks in the same channel run at half the bandwidth of two sticks
in different channels, the machine boots either way, and nothing on the desktop
ever mentions it. That is exactly the sort of finding this application exists
to surface, so it renders at warning severity with an explanation attached.
"""

from __future__ import annotations

import re

from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    REQUIRES_ROOT,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_bytes,
    fmt_list,
    fmt_percent,
    or_missing,
    read_lines,
)

MEMINFO = "/proc/meminfo"

# Tried in order against the bank locator, then the locator. Firmware vendors
# do not agree on a format, so each of these covers a real family of boards.
# `CHANNEL`, `CHAN` and `CH` are all in use and all mean the same thing: AMD
# boards write `P0 CHANNEL A`, several write `CHAN A DIMM 0`, and the short
# form turns up on Intel laptops. Only the short one needs a word boundary
# after the letter, to keep it from reading the `I` of `CHIP A` as a channel.
_CHANNEL_PATTERNS = (
    re.compile(r"\bCHAN(?:NEL)?\s*[-_]?\s*([A-Z0-9])", re.IGNORECASE),
    re.compile(r"\bCH\s*[-_]?\s*([A-Z0-9])\b", re.IGNORECASE),
    re.compile(r"DIMM\s*[-_]?\s*([A-Z])\s*\d", re.IGNORECASE),
    re.compile(r"^\s*([A-Z])\s*\d\s*$", re.IGNORECASE),
)

CHANNEL_WORDS = {
    1: "Single channel",
    2: "Dual channel",
    3: "Triple channel",
    4: "Quad channel",
    6: "Six channel",
    8: "Eight channel",
}

# meminfo keys worth a row, in the order they are shown.
OVERVIEW_KEYS = (
    ("MemTotal", "Total", "total"),
    ("MemAvailable", "Available", "available"),
    ("MemFree", "Free", "free"),
    ("Buffers", "Buffers", "buffers"),
    ("Cached", "Cached", "cached"),
    ("Dirty", "Dirty", "dirty"),
    ("Shmem", "Shared", "shared"),
    ("SwapTotal", "Swap total", "swap_total"),
    ("SwapFree", "Swap free", "swap_free"),
)


def parse_meminfo(lines) -> dict[str, int]:
    """``MemTotal:  32690404 kB`` -> ``{"MemTotal": 33474973696}``, in bytes."""
    out: dict[str, int] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        # Every value but the hugepage counts carries a kB suffix.
        if len(parts) > 1 and parts[1].lower() == "kb":
            value *= 1024
        out[key.strip()] = value
    return out


# Both spellings of every unit, because dmidecode changed its mind. Up to and
# including 3.6 it printed `Size: 16 GB`; 3.7 switched to binary prefixes and
# prints `Size: 16 GiB` for the identical module. Both mean 2**30 bytes - the
# quantity never changed, only the label on it - so both are read the same way
# and a machine reads the same whichever version of dmidecode is installed.
# A pattern that insists on `GB` sees no size at all on a current Arch box,
# and every populated slot then renders as an empty one.
_DMI_SIZE = re.compile(r"^\s*(\d+)\s*(bytes|B|[KMGTPE]i?B)\b", re.IGNORECASE)

_DMI_SIZE_FACTORS = {
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
    "p": 1024**5,
    "e": 1024**6,
}


def parse_dmi_size(text: str | None) -> int | None:
    """``16 GiB``, ``16 GB``, ``8192 MB``, ``No Module Installed`` -> bytes or None.

    DMI sizes are binary whichever way the unit is spelt, which is why a
    "16 GB" module shows as 16 GiB here and everywhere else on the system.
    """
    if not isinstance(text, str):
        return None
    match = _DMI_SIZE.match(text)
    if not match:
        return None
    unit = match.group(2).lower()
    factor = 1 if unit in ("b", "bytes") else _DMI_SIZE_FACTORS.get(unit[0], 1)
    return int(match.group(1)) * factor


def channel_of(locator: str | None, bank_locator: str | None) -> str | None:
    """The channel a slot belongs to, or None if the firmware does not say."""
    for text in (bank_locator, locator):
        if not text:
            continue
        for pattern in _CHANNEL_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(1).upper()
    return None


# What a slot is. Three states rather than two, because "the firmware did not
# say" is not the same claim as "there is nothing in it", and only one of them
# is safe to print next to a slot that may well be full.
SLOT_FILLED = "filled"
SLOT_EMPTY = "empty"
SLOT_UNKNOWN = "unknown"

# How firmware writes an empty slot. `0 MB` parses to a perfectly valid zero,
# which is why the size is tested before the wording: counting it as a module
# gives a machine with two sticks in four slots a slot count of four, all of
# them "populated", next to a total that adds up to the two.
_EMPTY_SIZE_WORDS = frozenset(
    {"no module installed", "not installed", "none", "not specified", "0"}
)


def slot_state(record: dict) -> tuple[str, int | None]:
    """A slot's state and, when it has one, the size of the module in it."""
    size = record.get("fields", {}).get("Size")
    parsed = parse_dmi_size(size)
    if parsed:
        return SLOT_FILLED, parsed
    if parsed == 0:
        return SLOT_EMPTY, None
    text = size.strip().lower() if isinstance(size, str) else ""
    if text in _EMPTY_SIZE_WORDS:
        return SLOT_EMPTY, None
    return SLOT_UNKNOWN, None


def _populated(record: dict) -> bool:
    """Whether this slot has a module in it."""
    return slot_state(record)[0] == SLOT_FILLED


class MemoryProbe(Probe):
    branch = "hardware"
    id = "memory"
    label = "Memory"

    def sections(self) -> list[Section]:
        return [self._overview(), self._modules()]

    # -- overview ---------------------------------------------------------

    def _overview(self) -> Section:
        section = Section(id="overview", label="Overview")
        info = parse_meminfo(read_lines(MEMINFO))
        if not info:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "/proc/meminfo could not be read on this machine.",
                )
            )
            return section

        for row in self._overview_rows(info):
            section.add(row)

        total = info.get("MemTotal")
        hugepage_size = info.get("Hugepagesize")
        hugepages = info.get("HugePages_Total")
        if hugepages is not None:
            detail = str(hugepages)
            if hugepages and hugepage_size:
                detail += f" × {fmt_bytes(hugepage_size)} = {fmt_bytes(hugepages * hugepage_size)}"
            elif hugepage_size:
                detail += f" (page size {fmt_bytes(hugepage_size)})"
            section.add(self.row("hugepages", "Huge pages", detail, tier=VOLATILE))

        if total:
            section.add(
                self.row(
                    "reported_total",
                    "Reported by the kernel",
                    f"{fmt_bytes(total)} — firmware and devices reserve the rest",
                )
            )
        return section

    def _overview_rows(self, info: dict[str, int]) -> list:
        """The meminfo rows. Shared by the enumeration and the volatile pass."""
        rows = []
        total = info.get("MemTotal")
        used = None
        if total is not None and info.get("MemAvailable") is not None:
            used = total - info["MemAvailable"]

        for key, label, field in OVERVIEW_KEYS:
            value = info.get(key)
            if value is None:
                continue
            text = fmt_bytes(value)
            if total and key in ("MemAvailable", "MemFree"):
                text += f" ({fmt_percent(value * 100.0 / total)})"
            if key == "SwapFree":
                swap_total = info.get("SwapTotal") or 0
                if swap_total:
                    text += f" ({fmt_percent(value * 100.0 / swap_total)})"
            rows.append(
                self.row(
                    field,
                    label,
                    text,
                    tier=VOLATILE if key != "MemTotal" and key != "SwapTotal" else "static",
                )
            )

        if used is not None and total:
            rows.append(
                self.row(
                    "in_use",
                    "In use",
                    f"{fmt_bytes(used)} ({fmt_percent(used * 100.0 / total)})",
                    tier=VOLATILE,
                )
            )
        return rows

    # -- modules ----------------------------------------------------------

    def _modules(self) -> Section:
        section = Section(id="modules", label="Modules")
        priv = self.context.privileged

        if priv is None or not getattr(priv, "available", False):
            section.add(self.row("dimm_detail", "Module detail", REQUIRES_ROOT))
            section.add(
                self.row(
                    "dimm_why",
                    "Why",
                    "Per-slot part numbers, ranks and configured speeds live in the "
                    "DMI table, which only the administrator may read. Everything in "
                    "the Overview section is available without it.",
                )
            )
            return section

        arrays = priv.type16
        modules = priv.type17
        for row in self._array_rows(arrays, modules):
            section.add(row)

        if not modules:
            section.add(
                self.row(
                    "dimm_absent",
                    "Modules",
                    "The DMI table reports no memory devices on this machine.",
                )
            )
            return section

        for index, record in enumerate(modules):
            for row in self._module_rows(index, record):
                section.add(row)
        return section

    def _array_rows(self, arrays, modules) -> list:
        rows = []
        states = [slot_state(record) for record in modules]
        populated = [
            record
            for record, (state, _size) in zip(modules, states)
            if state == SLOT_FILLED
        ]
        unreported = sum(1 for state, _size in states if state == SLOT_UNKNOWN)

        if arrays:
            fields = arrays[0].get("fields", {})
            rows.append(
                self.row(
                    "max_capacity",
                    "Maximum capacity",
                    or_missing(fields.get("Maximum Capacity"), NOT_REPORTED),
                )
            )
            rows.append(
                self.row(
                    "slots",
                    "Slots",
                    "{} total, {} populated{}".format(
                        or_missing(fields.get("Number Of Devices"), str(len(modules))),
                        len(populated),
                        f", {unreported} not reported" if unreported else "",
                    ),
                )
            )
            rows.append(
                self.row(
                    "ecc",
                    "Error correction",
                    or_missing(fields.get("Error Correction Type"), NOT_REPORTED),
                )
            )
        else:
            rows.append(
                self.row(
                    "slots",
                    "Slots",
                    "{} total, {} populated{}".format(
                        len(modules),
                        len(populated),
                        f", {unreported} not reported" if unreported else "",
                    ),
                )
            )

        installed = sum(size or 0 for _state, size in states)
        if installed:
            rows.append(self.row("installed", "Installed", fmt_bytes(installed)))

        rows.append(self._channel_row(populated, modules))
        return rows

    def _channel_row(self, populated, modules):
        """The derived channel configuration, and the warning when it is wrong."""
        channels = _channels_named_by(populated)

        if not channels:
            # Whatever this row says, it is read directly above a list of slots
            # that print their bank locators. Saying no slot names a channel
            # while every row underneath reads `P0 CHANNEL A` is a
            # contradiction the reader can see, so the row says which of the
            # two things is actually true instead.
            named = sorted(set(_channels_named_by(modules)))
            if not modules:
                value = (
                    "No memory devices are reported, so there is no "
                    "configuration to derive."
                )
            elif named and not populated:
                value = (
                    "No slot reports a module, so there is no configuration to "
                    "derive. The slots themselves name "
                    f"{fmt_list(named)}."
                )
            elif named:
                value = (
                    "No populated slot names a channel, so the configuration "
                    f"cannot be derived. The slots name {fmt_list(named)}."
                )
            else:
                value = (
                    "This firmware does not name a channel for any slot, so the "
                    "configuration cannot be derived."
                )
            return self.row("channels", "Channel configuration", value)

        distinct = sorted(set(channels))
        count = len(distinct)
        word = CHANNEL_WORDS.get(count, f"{count} channels")
        value = f"{word} — populated: {', '.join(distinct)}"

        # More than one stick, all in one channel: the board is running at
        # half the memory bandwidth it could, and nothing else will say so.
        severity = WARNING if (count == 1 and len(populated) > 1) else "normal"
        if severity == WARNING:
            value = f"{word} with {len(populated)} modules — {', '.join(distinct)}"
        return self.row("channels", "Channel configuration", value, severity=severity)

    def _module_rows(self, index: int, record: dict) -> list:
        fields = record.get("fields", {})
        locator = fields.get("Locator") or f"Slot {index}"
        bank = fields.get("Bank Locator")
        state, size = slot_state(record)
        suffix = f" ({bank})" if bank else ""

        if state == SLOT_EMPTY:
            return [
                self.row(
                    "dimm_empty",
                    locator,
                    "Empty" + suffix,
                    key=f"slot{index}",
                )
            ]
        if state != SLOT_FILLED:
            # The firmware wrote something in the size field that is not a
            # size - `Unknown` is the usual one. The slot may well be full, so
            # this says what happened rather than calling it empty.
            written = fields.get("Size")
            written = written.strip() if isinstance(written, str) else ""
            return [
                self.row(
                    "dimm_unknown",
                    locator,
                    "Size not reported by the firmware"
                    + (f", which wrote “{written}”" if written else "")
                    + suffix,
                    key=f"slot{index}",
                )
            ]

        summary_parts = [fmt_bytes(size)]
        for key in ("Type", "Configured Memory Speed", "Speed"):
            value = fields.get(key)
            if value and value not in summary_parts:
                summary_parts.append(value)
                if key == "Configured Memory Speed":
                    break

        rows = [
            self.row(
                "dimm",
                locator,
                ", ".join(summary_parts),
                key=f"slot{index}",
            )
        ]

        detail = [
            ("dimm_manufacturer", "  Manufacturer", fields.get("Manufacturer")),
            ("dimm_part", "  Part number", fields.get("Part Number")),
            ("dimm_serial", "  Serial number", fields.get("Serial Number")),
            ("dimm_rank", "  Rank", fields.get("Rank")),
            ("dimm_form", "  Form factor", fields.get("Form Factor")),
            ("dimm_bank", "  Bank locator", bank),
            ("dimm_rated", "  Rated speed", fields.get("Speed")),
            ("dimm_configured", "  Running at", fields.get("Configured Memory Speed")),
            ("dimm_voltage", "  Configured voltage", fields.get("Configured Voltage")),
        ]
        for field_name, label, value in detail:
            if not value or value in ("Not Specified", "Unknown", "Not Provided"):
                continue
            rows.append(
                self.row(
                    field_name,
                    label,
                    str(value),
                    key=f"slot{index}{field_name}",
                )
            )

        # A module running below the speed printed on it means XMP or EXPO is
        # off, which is one of the most common and least visible ways a machine
        # ends up slower than it was bought to be.
        rated = _parse_speed(fields.get("Speed"))
        configured = _parse_speed(fields.get("Configured Memory Speed"))
        if rated and configured and configured < rated:
            rows.append(
                self.row(
                    "dimm_underclocked",
                    "  Running below rated speed",
                    f"{configured} MT/s of a rated {rated} MT/s",
                    severity=WARNING,
                    key=f"slot{index}underclocked",
                )
            )
        return rows

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        info = parse_meminfo(read_lines(MEMINFO))
        if not info:
            return {}
        rows = [row for row in self._overview_rows(info) if row.is_volatile]
        hugepages = info.get("HugePages_Total")
        hugepage_size = info.get("Hugepagesize")
        if hugepages is not None:
            detail = str(hugepages)
            if hugepages and hugepage_size:
                detail += f" × {fmt_bytes(hugepage_size)} = {fmt_bytes(hugepages * hugepage_size)}"
            elif hugepage_size:
                detail += f" (page size {fmt_bytes(hugepage_size)})"
            rows.append(self.row("hugepages", "Huge pages", detail, tier=VOLATILE))
        return {"overview": rows}


def _channels_named_by(records) -> list[str]:
    """Every channel named by these slots, one entry per slot that names one."""
    out = []
    for record in records:
        fields = record.get("fields", {})
        channel = channel_of(fields.get("Locator"), fields.get("Bank Locator"))
        if channel is not None:
            out.append(channel)
    return out


def _parse_speed(text: str | None) -> int | None:
    """``6000 MT/s`` -> ``6000``."""
    if not text:
        return None
    match = re.match(r"^\s*(\d+)", text)
    return int(match.group(1)) if match else None
