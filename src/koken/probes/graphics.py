# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Graphics cards, one row 3 instance per DRM card.

The row that earns this section its place is the PCIe link. A card negotiates
a width and a speed with the board at boot, and it is entirely possible for a
16-lane card to come up at eight lanes because of which slot it is in, what
else is populated, or a marginal riser. The machine works. Nothing complains.
The card is simply slower than it should be, permanently, and the only place
that fact is written down is a pair of sysfs files nobody reads.

Width and speed are treated differently, because they fail differently. A
narrow link is a wiring fact and stays wrong until someone moves the card. A
slow link is usually just power management: an idle card drops to 2.5 GT/s on
purpose and climbs back under load. Both are flagged, and the explanation for
each says which kind it is.

Several files here exist only under amdgpu - VRAM totals, the DPM state tables,
the firmware listing. The driver is detected first, and a card on another
driver gets a row saying the data is not exposed rather than a blank space.
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
    glob_dirs,
    list_dir,
    or_missing,
    read_first_line,
    read_int,
    read_link_name,
    read_lines,
    read_text,
)
from .hwids import describe_device, describe_subsystem, describe_vendor, format_pair

DRM_ROOT = "/sys/class/drm"

# GT/s per lane -> the generation people actually say out loud.
PCIE_GENERATIONS = {
    "2.5": "Gen 1",
    "5.0": "Gen 2",
    "8.0": "Gen 3",
    "16.0": "Gen 4",
    "32.0": "Gen 5",
    "64.0": "Gen 6",
}

_DPM_LINE = re.compile(r"^\s*(\d+):\s*(\S+)\s*(\*?)\s*$")


def parse_link_speed(text: str | None) -> str | None:
    """``16.0 GT/s PCIe`` -> ``16.0``."""
    if not text:
        return None
    match = re.match(r"\s*([\d.]+)\s*GT/s", text)
    return match.group(1) if match else None


def pcie_generation(speed: str | None) -> str | None:
    return PCIE_GENERATIONS.get(speed) if speed else None


def parse_dpm(lines) -> tuple[str | None, list[str]]:
    """``0: 500Mhz`` / ``1: 2124Mhz *`` -> the starred state and every state."""
    states = []
    current = None
    for line in lines:
        match = _DPM_LINE.match(line)
        if not match:
            continue
        value = match.group(2)
        states.append(value)
        if match.group(3) == "*":
            current = value
    return current, states


class GraphicsProbe(Probe):
    branch = "hardware"
    id = "graphics"
    label = "Graphics"

    def __init__(self, context=None):
        super().__init__(context)
        self._cards: list[dict] = []

    # -- enumeration ------------------------------------------------------

    def _find_cards(self) -> list[dict]:
        cards = []
        for path in glob_dirs(f"{DRM_ROOT}/card[0-9]*"):
            # card0-DP-1 and friends are connectors, not cards; displays.py
            # owns those.
            if "-" in path.name:
                continue
            device = path / "device"
            cards.append(
                {
                    "name": path.name,
                    "index": path.name[4:],
                    "path": path,
                    "device": device,
                    "driver": read_link_name(device / "driver"),
                    "vendor": read_int(device / "vendor"),
                    "device_id": read_int(device / "device"),
                    "subsystem_vendor": read_int(device / "subsystem_vendor"),
                    "subsystem_device": read_int(device / "subsystem_device"),
                    "revision": read_first_line(device / "revision"),
                }
            )
        return cards

    def sections(self) -> list[Section]:
        cards = self._find_cards()
        self._cards = cards
        if not cards:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No DRM graphics card was found. A machine with no graphics "
                    "driver loaded, or one running entirely headless, looks like this.",
                )
            ]
        return [self._card_section(card) for card in cards]

    def _card_section(self, card) -> Section:
        section = Section(
            id=card["name"],
            label=self._card_label(card),
            icon="graphics",
        )
        pci = self.context.pci_ids

        section.add(
            self.row(
                "model",
                "Model",
                describe_device(pci, card["vendor"], card["device_id"]),
            )
        )
        section.add(
            self.row("vendor", "Vendor", describe_vendor(pci, card["vendor"]))
        )
        section.add(
            self.row(
                "pci_id", "PCI ID", format_pair(card["vendor"], card["device_id"])
            )
        )
        if card["subsystem_vendor"] is not None:
            section.add(
                self.row(
                    "board",
                    "Board",
                    describe_subsystem(
                        pci,
                        card["vendor"],
                        card["device_id"],
                        card["subsystem_vendor"],
                        card["subsystem_device"],
                    ),
                )
            )
        section.add(
            self.row("driver", "Driver", or_missing(card["driver"], "None loaded"))
        )
        section.add(
            self.row("revision", "Revision", or_missing(card["revision"], NOT_REPORTED))
        )
        section.add(
            self.row("node", "Device node", f"/dev/dri/{card['name']}")
        )

        address = read_link_name(card["device"])
        if address:
            section.add(self.row("address", "PCI address", address))

        unique = read_first_line(card["device"] / "unique_id")
        if unique:
            section.add(self.row("unique_id", "Die serial", unique))

        for row in self._link_rows(card):
            section.add(row)
        for row in self._vram_rows(card):
            section.add(row)
        for row in self._clock_rows(card):
            section.add(row)
        for row in self._hwmon_rows(card):
            section.add(row)
        for row in self._firmware_rows(card):
            section.add(row)
        return section

    def _card_label(self, card) -> str:
        name = None
        pci = self.context.pci_ids
        if pci is not None:
            name = pci.device(card["vendor"], card["device_id"])
        if not name:
            return card["name"]
        # pci.ids writes the marketing name in brackets after the codename:
        # "Navi 32 [Radeon RX 7700 XT / 7800 XT]". The bracketed half is the
        # half anyone recognises.
        match = re.search(r"\[([^\]]+)\]", name)
        if match:
            name = match.group(1)
        name = name.split("/")[0].strip()
        return name if len(name) <= 28 else name[:27] + "…"

    # -- the link ---------------------------------------------------------

    def _link_rows(self, card) -> list:
        device = card["device"]
        current_width = read_int(device / "current_link_width")
        max_width = read_int(device / "max_link_width")
        current_speed = parse_link_speed(read_first_line(device / "current_link_speed"))
        max_speed = parse_link_speed(read_first_line(device / "max_link_speed"))

        if current_width is None and max_width is None and current_speed is None:
            return [
                self.row(
                    "link",
                    "PCIe link",
                    "This device does not report its link state. An integrated "
                    "graphics processor has no PCIe link to report.",
                )
            ]

        rows = []
        generation = pcie_generation(max_speed)
        summary = []
        if current_width is not None:
            summary.append(f"x{current_width}")
        if current_speed:
            summary.append(f"at {current_speed} GT/s")
        if generation:
            summary.append(f"({generation} capable)")
        rows.append(
            self.row(
                "link",
                "PCIe link",
                " ".join(summary) if summary else NOT_AVAILABLE,
                tier=VOLATILE,
            )
        )

        narrow = (
            current_width is not None
            and max_width is not None
            and current_width < max_width
        )
        rows.append(
            self.row(
                "link_width",
                "Link width",
                _compare("x", current_width, max_width),
                severity=WARNING if narrow else "normal",
            )
        )

        slow = _speed_below(current_speed, max_speed)
        rows.append(
            self.row(
                "link_speed",
                "Link speed",
                _compare("", current_speed, max_speed, suffix=" GT/s"),
                tier=VOLATILE,
                severity=WARNING if slow else "normal",
            )
        )
        return rows

    # -- amdgpu-only data -------------------------------------------------

    def _is_amdgpu(self, card) -> bool:
        return (card["driver"] or "").lower() == "amdgpu"

    def _vram_rows(self, card) -> list:
        device = card["device"]
        total = read_int(device / "mem_info_vram_total")
        if total is None:
            if self._is_amdgpu(card):
                return [
                    self.row(
                        "vram",
                        "Video memory",
                        "This amdgpu card does not report a VRAM total.",
                    )
                ]
            return [
                self.row(
                    "vram",
                    "Video memory",
                    f"Not exposed by the {card['driver'] or 'current'} driver. "
                    "Only amdgpu publishes VRAM totals in sysfs.",
                )
            ]
        rows = [self.row("vram", "Video memory", fmt_bytes(total))]
        visible = read_int(device / "mem_info_vis_vram_total")
        if visible is not None:
            rows.append(
                self.row(
                    "vram_visible",
                    "Host-visible video memory",
                    fmt_bytes(visible),
                )
            )
        rows.append(self._vram_used_row(card, total))
        return rows

    def _vram_used_row(self, card, total=None):
        device = card["device"]
        used = read_int(device / "mem_info_vram_used")
        if total is None:
            total = read_int(device / "mem_info_vram_total")
        if used is None:
            return self.row("vram_used", "Video memory in use", NOT_AVAILABLE, tier=VOLATILE)
        text = fmt_bytes(used)
        if total:
            text += f" ({fmt_percent(used * 100.0 / total)})"
        return self.row("vram_used", "Video memory in use", text, tier=VOLATILE)

    def _clock_rows(self, card) -> list:
        rows = []
        found = False
        for attribute, field, label in (
            ("pp_dpm_sclk", "core_clock", "Core clock"),
            ("pp_dpm_mclk", "memory_clock", "Memory clock"),
        ):
            lines = read_lines(card["device"] / attribute)
            if not lines:
                continue
            found = True
            current, states = parse_dpm(lines)
            value = or_missing(current, NOT_REPORTED)
            if states:
                value += f" — states: {', '.join(states)}"
            rows.append(self.row(field, label, value, tier=VOLATILE))
        if not found and not self._is_amdgpu(card):
            rows.append(
                self.row(
                    "clocks",
                    "Clocks",
                    f"Not exposed by the {card['driver'] or 'current'} driver. "
                    "The DPM state tables are an amdgpu interface.",
                )
            )
        return rows

    def _hwmon_rows(self, card) -> list:
        rows = []
        for hwmon in glob_dirs(card["device"] / "hwmon" / "hwmon[0-9]*"):
            rows.extend(self._hwmon_entries(hwmon))
        if not rows:
            rows.append(
                self.row(
                    "sensors",
                    "Sensors",
                    "This card exposes no temperature, fan or power readings.",
                )
            )
        return rows

    def _hwmon_entries(self, hwmon) -> list:
        rows = []
        for entry in list_dir(hwmon):
            name = entry.name
            if name.startswith("temp") and name.endswith("_input"):
                stem = name[: -len("_input")]
                label = read_first_line(hwmon / f"{stem}_label") or stem
                value = read_int(hwmon / name)
                critical = read_int(hwmon / f"{stem}_crit")
                text = f"{value / 1000:.1f} °C" if value is not None else NOT_AVAILABLE
                hot = False
                if value is not None and critical:
                    text += f" (critical {critical / 1000:.0f} °C)"
                    hot = value >= critical
                rows.append(
                    self.row(
                        "temperature",
                        f"Temperature, {label}",
                        text,
                        tier=VOLATILE,
                        severity=WARNING if hot else "normal",
                        key=f"{hwmon.name}{stem}",
                    )
                )
            elif name.startswith("fan") and name.endswith("_input"):
                stem = name[: -len("_input")]
                value = read_int(hwmon / name)
                rows.append(
                    self.row(
                        "fan",
                        f"Fan {stem[3:]}",
                        f"{value} rpm" if value is not None else NOT_AVAILABLE,
                        tier=VOLATILE,
                        key=f"{hwmon.name}{stem}",
                    )
                )
            elif name.startswith("power") and name.endswith(("_average", "_input")):
                stem = name.rsplit("_", 1)[0]
                value = read_int(hwmon / name)
                cap = read_int(hwmon / f"{stem}_cap")
                text = f"{value / 1_000_000:.1f} W" if value is not None else NOT_AVAILABLE
                if cap:
                    text += f" of a {cap / 1_000_000:.0f} W limit"
                rows.append(
                    self.row(
                        "power",
                        "Power draw",
                        text,
                        tier=VOLATILE,
                        key=f"{hwmon.name}{stem}",
                    )
                )
        return rows

    def _firmware_rows(self, card) -> list:
        priv = self.context.privileged
        if priv is None or not getattr(priv, "available", False):
            if not self._is_amdgpu(card):
                return []
            return [
                self.row("vbios", "VBIOS version", REQUIRES_ROOT),
            ]

        firmware = priv.firmware_for_card(card["index"])
        if not firmware:
            if not self._is_amdgpu(card):
                return [
                    self.row(
                        "vbios",
                        "VBIOS version",
                        f"Not exposed by the {card['driver'] or 'current'} driver. "
                        "The VBIOS string is read from an amdgpu debugfs file.",
                    )
                ]
            return [
                self.row(
                    "vbios",
                    "VBIOS version",
                    "This card publishes no firmware information, which usually "
                    "means the kernel was built without debugfs.",
                )
            ]

        rows = []
        vbios = firmware.get("vbios_version") or ""
        rows.append(self.row("vbios", "VBIOS version", or_missing(vbios, NOT_REPORTED)))
        part, date = _split_vbios(vbios)
        if part:
            rows.append(self.row("vbios_part", "VBIOS part number", part))
        if date:
            rows.append(self.row("vbios_date", "VBIOS build date", date))

        blocks = firmware.get("firmware") or {}
        if isinstance(blocks, dict) and blocks:
            rows.append(
                self.row("firmware_count", "Firmware blocks", str(len(blocks)))
            )
            for name in sorted(blocks):
                entry = blocks[name]
                if not isinstance(entry, dict):
                    continue
                rows.append(
                    self.row(
                        "firmware_block",
                        f"  {name}",
                        "version {}, feature {}".format(
                            entry.get("firmware", "?"), entry.get("feature", "?")
                        ),
                        key=f"fw{name}",
                    )
                )
        return rows

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for card in self._cards or self._find_cards():
            rows = []
            for row in self._link_rows(card):
                if row.is_volatile:
                    rows.append(row)
            if read_int(card["device"] / "mem_info_vram_total") is not None:
                rows.append(self._vram_used_row(card))
            rows.extend(self._clock_rows(card))
            for hwmon in glob_dirs(card["device"] / "hwmon" / "hwmon[0-9]*"):
                rows.extend(self._hwmon_entries(hwmon))
            out[card["name"]] = [row for row in rows if row.is_volatile]
        return out


# -- helpers --------------------------------------------------------------


def _compare(prefix: str, current, maximum, suffix: str = "") -> str:
    """``x8 of a maximum x16``, or just the current value when there is no maximum."""
    if current is None and maximum is None:
        return NOT_AVAILABLE
    if maximum is None:
        return f"{prefix}{current}{suffix}"
    if current is None:
        return f"Maximum {prefix}{maximum}{suffix}"
    if str(current) == str(maximum):
        return f"{prefix}{current}{suffix} — the maximum this device supports"
    return f"{prefix}{current}{suffix} of a maximum {prefix}{maximum}{suffix}"


def _speed_below(current: str | None, maximum: str | None) -> bool:
    if not current or not maximum:
        return False
    try:
        return float(current) < float(maximum)
    except ValueError:
        return False


_VBIOS_DATE = re.compile(r"\b(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}/\d{2}/\d{2,4})\b")


def _split_vbios(text: str) -> tuple[str | None, str | None]:
    """Pull a part number and a build date out of the VBIOS version string.

    The string is whatever the board vendor wrote into the ROM. Most read like
    ``113-EXT97136-001``, some carry a date as well. Anything that is not
    recognised is left alone rather than guessed at.
    """
    if not text:
        return None, None
    date = None
    match = _VBIOS_DATE.search(text)
    if match:
        date = match.group(1)
        text = text.replace(date, " ")
    part = None
    for token in text.replace(",", " ").split():
        if re.match(r"^\d{3}-[A-Za-z0-9]+-\d+$", token):
            part = token
            break
    return part, date
