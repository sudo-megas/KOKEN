# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Every PCI device, one row 3 instance each.

This is the section that replaces reading ``lspci`` output, and it replaces it
by reading the same files ``lspci`` reads. The only thing ``pciutils`` adds is
the formatting, and formatting is what this application is for.

The link rows repeat what the Graphics branch shows for the card, deliberately.
A storage controller negotiating two lanes instead of four is the same class of
silent problem as a graphics card at x8, and this is the only place it would
ever be visible.
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
    path_exists,
    read_first_line,
    read_int,
    read_link_name,
)
from .graphics import parse_link_speed, pcie_generation
from .hwids import describe_device, describe_subsystem, describe_vendor, format_pair

PCI_ROOT = "/sys/bus/pci/devices"

# Top-level class codes to glyph concepts, per CORE 13.5. Anything not here
# gets the generic chip glyph rather than a vague approximation.
CLASS_ICONS = {
    0x01: "pci_storage",
    0x02: "pci_network",
    0x03: "graphics",
    0x04: "pci_audio",
}


class PciProbe(Probe):
    branch = "peripherals"
    id = "pci"
    label = "PCI"

    def __init__(self, context=None):
        super().__init__(context)
        self._devices: list[dict] = []

    # -- enumeration ------------------------------------------------------

    def _find_devices(self) -> list[dict]:
        devices = []
        for path in list_dir(PCI_ROOT):
            if not path_exists(path / "vendor"):
                continue
            devices.append(
                {
                    "address": path.name,
                    "path": path,
                    "vendor": read_int(path / "vendor"),
                    "device": read_int(path / "device"),
                    "subsystem_vendor": read_int(path / "subsystem_vendor"),
                    "subsystem_device": read_int(path / "subsystem_device"),
                    "class": read_int(path / "class"),
                    "revision": read_first_line(path / "revision"),
                    "driver": read_link_name(path / "driver"),
                }
            )
        # PCI addresses are fixed-width hexadecimal, so they sort correctly as
        # plain text. The natural sort the readers use is right for cpu2 before
        # cpu10 and wrong here, where it would put 00:0a.0 before 00:01.0.
        return sorted(devices, key=lambda item: item["address"])

    def sections(self) -> list[Section]:
        devices = self._find_devices()
        self._devices = devices
        if not devices:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No PCI devices were found. A machine with no PCI bus at all "
                    "looks like this.",
                )
            ]
        return [self._device_section(device) for device in devices]

    def _device_section(self, device) -> Section:
        pci = self.context.pci_ids
        top = (device["class"] >> 16) if device["class"] is not None else None
        section = Section(
            id=device["address"],
            label=self._label(device),
            icon=CLASS_ICONS.get(top, "pci_generic"),
        )

        section.add(
            self.row("model", "Device", describe_device(pci, device["vendor"], device["device"]))
        )
        section.add(self.row("vendor", "Vendor", describe_vendor(pci, device["vendor"])))
        section.add(
            self.row("pci_id", "PCI ID", format_pair(device["vendor"], device["device"]))
        )
        section.add(self.row("address", "Address", device["address"]))

        klass = None
        if pci is not None and device["class"] is not None:
            klass = pci.device_class(device["class"])
        section.add(
            self.row(
                "device_class",
                "Class",
                "{} — class code {:06x}".format(klass, device["class"])
                if klass and device["class"] is not None
                else (f"{device['class']:06x}" if device["class"] is not None else NOT_REPORTED),
            )
        )
        if pci is not None and device["class"] is not None:
            prog_if = pci.programming_interface(device["class"])
            if prog_if:
                section.add(
                    self.row("prog_if", "Programming interface", prog_if)
                )

        if device["subsystem_vendor"] is not None:
            section.add(
                self.row(
                    "subsystem",
                    "Subsystem",
                    describe_subsystem(
                        pci,
                        device["vendor"],
                        device["device"],
                        device["subsystem_vendor"],
                        device["subsystem_device"],
                    ),
                )
            )
        section.add(
            self.row("revision", "Revision", or_missing(device["revision"], NOT_REPORTED))
        )
        section.add(
            self.row(
                "driver",
                "Driver",
                or_missing(device["driver"], "None bound — this device is not in use"),
            )
        )

        for row in self._link_rows(device):
            section.add(row)
        for row in self._power_rows(device):
            section.add(row)
        for row in self._placement_rows(device):
            section.add(row)
        return section

    def _label(self, device) -> str:
        pci = self.context.pci_ids
        name = pci.device(device["vendor"], device["device"]) if pci is not None else None
        if not name:
            name = device["address"]
        else:
            match = re.search(r"\[([^\]]+)\]", name)
            if match:
                name = match.group(1)
            name = name.split("/")[0].strip()
        return name if len(name) <= 24 else name[:23] + "…"

    def _link_rows(self, device) -> list:
        path = device["path"]
        current_width = read_int(path / "current_link_width")
        max_width = read_int(path / "max_link_width")
        current_speed = parse_link_speed(read_first_line(path / "current_link_speed"))
        max_speed = parse_link_speed(read_first_line(path / "max_link_speed"))

        if current_width is None and max_width is None and current_speed is None:
            return [
                self.row(
                    "link",
                    "PCIe link",
                    "This device reports no link state. Devices on the chipset's "
                    "internal buses often do not.",
                )
            ]

        rows = []
        narrow = (
            current_width is not None
            and max_width is not None
            and current_width < max_width
        )
        rows.append(
            self.row(
                "link_width",
                "Link width",
                _compare(current_width, max_width, prefix="x"),
                severity=WARNING if narrow else "normal",
            )
        )
        slow = _below(current_speed, max_speed)
        generation = pcie_generation(current_speed)
        speed_text = _compare(current_speed, max_speed, suffix=" GT/s")
        if generation:
            speed_text += f" ({generation})"
        rows.append(
            self.row(
                "link_speed",
                "Link speed",
                speed_text,
                tier=VOLATILE,
                severity=WARNING if slow else "normal",
            )
        )
        return rows

    def _power_rows(self, device) -> list:
        rows = []
        state = read_first_line(device["path"] / "power_state")
        if state:
            rows.append(
                self.row(
                    "power_state",
                    "Power state",
                    f"{state} — D0 is fully on, D3 is the deepest sleep this device offers",
                    tier=VOLATILE,
                )
            )
        d3cold = read_first_line(device["path"] / "d3cold_allowed")
        if d3cold is not None:
            rows.append(
                self.row(
                    "d3cold",
                    "Deepest sleep allowed",
                    "D3cold — the device may be powered off entirely"
                    if d3cold == "1"
                    else "D3hot at most — the device keeps some power",
                )
            )
        control = read_first_line(device["path"] / "power/control")
        if control:
            rows.append(self.row("power_control", "Runtime power management", control))
        return rows

    def _placement_rows(self, device) -> list:
        rows = []
        numa = read_int(device["path"] / "numa_node")
        if numa is not None:
            rows.append(
                self.row(
                    "numa_node",
                    "NUMA node",
                    str(numa) if numa >= 0 else "-1 — this machine has no NUMA topology",
                )
            )
        group = read_link_name(device["path"] / "iommu_group")
        if group:
            members = [
                item.name
                for item in list_dir(f"/sys/kernel/iommu_groups/{group}/devices")
            ]
            detail = group
            if members:
                detail += " — shared with " + fmt_list(
                    [name for name in members if name != device["address"]],
                    empty="nothing else",
                )
            rows.append(self.row("iommu_group", "IOMMU group", detail))

        irq = read_int(device["path"] / "irq")
        if irq:
            rows.append(self.row("irq", "Interrupt line", str(irq)))

        modalias = read_first_line(device["path"] / "modalias")
        if modalias:
            rows.append(self.row("modalias", "Module alias", modalias))
        return rows

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for device in self._devices or self._find_devices():
            rows = [row for row in self._link_rows(device) if row.is_volatile]
            rows.extend(row for row in self._power_rows(device) if row.is_volatile)
            if rows:
                out[device["address"]] = rows
        return out


def _compare(current, maximum, prefix: str = "", suffix: str = "") -> str:
    if current is None and maximum is None:
        return NOT_AVAILABLE
    if maximum is None:
        return f"{prefix}{current}{suffix}"
    if current is None:
        return f"Maximum {prefix}{maximum}{suffix}"
    if str(current) == str(maximum):
        return f"{prefix}{current}{suffix} — the maximum this device supports"
    return f"{prefix}{current}{suffix} of a maximum {prefix}{maximum}{suffix}"


def _below(current: str | None, maximum: str | None) -> bool:
    if not current or not maximum:
        return False
    try:
        return float(current) < float(maximum)
    except ValueError:
        return False
