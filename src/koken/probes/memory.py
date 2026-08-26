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
    fmt_percent,
    or_missing,
    read_lines,
)

MEMINFO = "/proc/meminfo"

# Tried in order against the bank locator, then the locator. Firmware vendors
# do not agree on a format, so each of these covers a real family of boards.
_CHANNEL_PATTERNS = (
    re.compile(r"CHANNEL\s*[-_]?\s*([A-Z0-9])", re.IGNORECASE),
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


def parse_dmi_size(text: str | None) -> int | None:
    """``16 GB``, ``8192 MB``, ``No Module Installed`` -> bytes or None.

    DMI sizes are binary despite the unit spelling, which is why a "16 GB"
    module shows as 16 GiB here and everywhere else on the system.
    """
    if not text:
        return None
    match = re.match(r"^\s*(\d+)\s*([kKmMgGtT])?B\b", text)
    if not match:
        return None
    value = int(match.group(1))
    unit = (match.group(2) or "").lower()
    factor = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}.get(unit, 1)
    return value * factor


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


def _populated(record: dict) -> bool:
    fields = record.get("fields", {})
    size = fields.get("Size")
    if not size:
        return False
    return parse_dmi_size(size) is not None


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
        populated = [record for record in modules if _populated(record)]

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
                    "{} total, {} populated".format(
                        or_missing(fields.get("Number Of Devices"), str(len(modules))),
                        len(populated),
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
                    f"{len(modules)} total, {len(populated)} populated",
                )
            )

        installed = sum(
            parse_dmi_size(record["fields"].get("Size")) or 0 for record in populated
        )
        if installed:
            rows.append(self.row("installed", "Installed", fmt_bytes(installed)))

        rows.append(self._channel_row(populated, modules))
        return rows

    def _channel_row(self, populated, modules):
        """The derived channel configuration, and the warning when it is wrong."""
        channels = []
        for record in populated:
            fields = record.get("fields", {})
            channel = channel_of(fields.get("Locator"), fields.get("Bank Locator"))
            if channel is not None:
                channels.append(channel)

        if not channels:
            return self.row(
                "channels",
                "Channel configuration",
                "This firmware does not name a channel for any slot, so the "
                "configuration cannot be derived.",
            )

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
        size = parse_dmi_size(fields.get("Size"))

        if size is None:
            return [
                self.row(
                    "dimm_empty",
                    locator,
                    "Empty" + (f" ({bank})" if bank else ""),
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


def _parse_speed(text: str | None) -> int | None:
    """``6000 MT/s`` -> ``6000``."""
    if not text:
        return None
    match = re.match(r"^\s*(\d+)", text)
    return int(match.group(1)) if match else None
