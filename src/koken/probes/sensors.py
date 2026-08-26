# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""hwmon chips, one row 3 instance each.

Every temperature, fan, voltage and power reading on the machine comes through
hwmon, and it is all the same shape: a channel prefix, a number, and a suffix.
``temp1_input`` is a reading, ``temp1_label`` names it, ``temp1_crit`` is where
the hardware considers it an emergency. Enumerating by pattern rather than by a
list of known chips means a sensor this application has never heard of still
shows up correctly.

Units are fixed and unhelpful: millidegrees, millivolts, microwatts,
microamps. They are converted here so that nobody has to divide 63000 by a
thousand in their head.
"""

from __future__ import annotations

import re

from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_list,
    glob_dirs,
    list_dir,
    or_missing,
    read_first_line,
    read_int,
    read_link_name,
)

HWMON_ROOT = "/sys/class/hwmon"

# prefix -> (divisor, unit, label). The divisor turns the kernel's fixed-point
# integer into the unit a person expects.
CHANNELS = (
    ("temp", 1000.0, "°C", "Temperature"),
    ("fan", 1.0, "rpm", "Fan"),
    ("in", 1000.0, "V", "Voltage"),
    ("power", 1_000_000.0, "W", "Power"),
    ("curr", 1000.0, "A", "Current"),
    ("energy", 1_000_000.0, "J", "Energy"),
    ("humidity", 1000.0, "%", "Humidity"),
)

CHANNEL_BY_PREFIX = {prefix: (divisor, unit, label) for prefix, divisor, unit, label in CHANNELS}

_INPUT_FILE = re.compile(r"^(?P<prefix>[a-z]+)(?P<index>\d+)_input$")

# Thresholds worth showing alongside a reading, most severe first.
LIMIT_SUFFIXES = (("crit", "critical"), ("emergency", "emergency"), ("max", "maximum"))


class SensorsProbe(Probe):
    branch = "peripherals"
    id = "sensors"
    label = "Sensors"

    def __init__(self, context=None):
        super().__init__(context)
        self._chips: list[dict] = []

    def _find_chips(self) -> list[dict]:
        chips = []
        for path in glob_dirs(f"{HWMON_ROOT}/hwmon[0-9]*"):
            name = read_first_line(path / "name")
            chips.append(
                {
                    "id": path.name,
                    "path": path,
                    "name": name,
                    "label": name or path.name,
                    "driver": read_link_name(path / "device/driver"),
                }
            )
        return chips

    def sections(self) -> list[Section]:
        chips = self._find_chips()
        self._chips = chips
        if not chips:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No hwmon chips were found. This machine exposes no temperature, "
                    "fan or voltage sensors to the kernel, which is normal in a "
                    "virtual machine and unusual on real hardware.",
                )
            ]
        return [self._chip_section(chip) for chip in chips]

    def _chip_section(self, chip) -> Section:
        readings = self._readings(chip)
        icon = "fan" if any(item["prefix"] == "fan" for item in readings) else "temperature"
        section = Section(id=chip["id"], label=self._label(chip), icon=icon)

        section.add(
            self.row("chip", "Chip", or_missing(chip["name"], chip["id"]))
        )
        if chip["driver"]:
            section.add(self.row("driver", "Driver", chip["driver"]))
        section.add(self.row("sysfs", "Kernel name", chip["id"]))

        if not readings:
            section.add(
                self.row(
                    "channels",
                    "Readings",
                    "This chip is registered but publishes no readings.",
                )
            )
            return section

        counts: dict[str, int] = {}
        for reading in readings:
            counts[reading["prefix"]] = counts.get(reading["prefix"], 0) + 1
        section.add(
            self.row(
                "channels",
                "Readings",
                fmt_list(
                    f"{count} {CHANNEL_BY_PREFIX.get(prefix, (0, '', prefix))[2].lower()}"
                    for prefix, count in sorted(counts.items())
                ),
            )
        )
        for reading in readings:
            section.add(self._reading_row(chip, reading))
        return section

    def _label(self, chip) -> str:
        name = chip["label"]
        return name if len(name) <= 24 else name[:23] + "…"

    def _readings(self, chip) -> list[dict]:
        found = []
        for entry in list_dir(chip["path"]):
            match = _INPUT_FILE.match(entry.name)
            if not match:
                continue
            prefix = match.group("prefix")
            if prefix not in CHANNEL_BY_PREFIX:
                continue
            stem = entry.name[: -len("_input")]
            found.append(
                {
                    "prefix": prefix,
                    "index": int(match.group("index")),
                    "stem": stem,
                    "path": entry,
                    "label": read_first_line(chip["path"] / f"{stem}_label"),
                }
            )
        return sorted(found, key=lambda item: (item["prefix"], item["index"]))

    def _reading_row(self, chip, reading):
        divisor, unit, kind = CHANNEL_BY_PREFIX[reading["prefix"]]
        raw = read_int(reading["path"])
        label = reading["label"] or f"{kind} {reading['index']}"

        if raw is None:
            return self.row(
                "reading",
                label,
                NOT_AVAILABLE,
                tier=VOLATILE,
                key=f"{chip['id']}{reading['stem']}",
            )

        value = raw / divisor
        text = f"{value:.0f} {unit}" if unit in ("rpm", "%") else f"{value:.2f} {unit}"
        if unit == "°C":
            text = f"{value:.1f} {unit}"

        hot = False
        for suffix, word in LIMIT_SUFFIXES:
            limit = read_int(chip["path"] / f"{reading['stem']}_{suffix}")
            if limit is None:
                continue
            limit_value = limit / divisor
            text += f" ({word} {limit_value:.0f} {unit})"
            if reading["prefix"] == "temp" and value >= limit_value:
                hot = True
            break

        alarm = read_int(chip["path"] / f"{reading['stem']}_alarm")
        if alarm:
            text += " — alarm raised"
            hot = True

        return self.row(
            "reading",
            label,
            text,
            tier=VOLATILE,
            severity=WARNING if hot else "normal",
            key=f"{chip['id']}{reading['stem']}",
        )

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for chip in self._chips or self._find_chips():
            rows = [self._reading_row(chip, reading) for reading in self._readings(chip)]
            if rows:
                out[chip["id"]] = rows
        return out
