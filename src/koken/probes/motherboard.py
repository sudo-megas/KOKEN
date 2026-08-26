# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The board, its firmware and the box it all sits in.

Everything here comes from the DMI table the firmware wrote at boot, most of it
through ``/sys/class/dmi/id/`` where it is world readable. The three serial
number fields are the exception: the kernel makes them root-only, so they
arrive through the helper or not at all.

Firmware writes whatever it likes into these fields, and a great deal of it is
placeholder text - "To Be Filled By O.E.M.", "Default string", "System
Manufacturer". Those are passed through rather than hidden, because a board
that says "Default string" is telling you something true about itself.
"""

from __future__ import annotations

from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    Probe,
    Section,
    or_missing,
    path_exists,
    read_first_line,
    read_int,
)

DMI = "/sys/class/dmi/id"

# SMBIOS specification, System Enclosure or Chassis Types. The values people
# actually meet are the desktop and portable ones; the rest are here so that a
# server or an all-in-one is named rather than shown as a number.
CHASSIS_TYPES = {
    1: "Other",
    2: "Unknown",
    3: "Desktop",
    4: "Low profile desktop",
    5: "Pizza box",
    6: "Mini tower",
    7: "Tower",
    8: "Portable",
    9: "Laptop",
    10: "Notebook",
    11: "Hand held",
    12: "Docking station",
    13: "All in one",
    14: "Sub notebook",
    15: "Space saving",
    16: "Lunch box",
    17: "Main server chassis",
    18: "Expansion chassis",
    19: "Sub chassis",
    20: "Bus expansion chassis",
    21: "Peripheral chassis",
    22: "RAID chassis",
    23: "Rack mount chassis",
    24: "Sealed case PC",
    25: "Multi system chassis",
    26: "Compact PCI",
    27: "Advanced TCA",
    28: "Blade",
    29: "Blade enclosure",
    30: "Tablet",
    31: "Convertible",
    32: "Detachable",
    33: "IoT gateway",
    34: "Embedded PC",
    35: "Mini PC",
    36: "Stick PC",
}


class MotherboardProbe(Probe):
    branch = "hardware"
    id = "motherboard"
    label = "Motherboard"

    def sections(self) -> list[Section]:
        return [self._overview(), self._firmware(), self._chassis()]

    def _dmi(self, name: str) -> str | None:
        return read_first_line(f"{DMI}/{name}")

    def _privileged_serial(self, name: str) -> str:
        priv = self.context.privileged
        if priv is None:
            return NOT_AVAILABLE
        value = priv.serial(name) if getattr(priv, "available", False) else None
        # The kernel makes these root-only, so an unprivileged read finds
        # nothing even where the firmware did fill the field in.
        return priv.text(value, fallback=NOT_REPORTED)

    # -- overview ---------------------------------------------------------

    def _overview(self) -> Section:
        section = Section(id="overview", label="Overview")
        vendor = self._dmi("board_vendor")
        name = self._dmi("board_name")

        if vendor is None and name is None and not path_exists(DMI):
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "This machine exposes no DMI table. A virtual machine or an "
                    "ARM board without SMBIOS looks like this.",
                )
            )
            return section

        section.add(self.row("board_vendor", "Vendor", or_missing(vendor, NOT_REPORTED)))
        section.add(self.row("board_name", "Model", or_missing(name, NOT_REPORTED)))
        section.add(
            self.row(
                "board_version",
                "Board version",
                or_missing(self._dmi("board_version"), NOT_REPORTED),
            )
        )
        section.add(
            self.row("board_serial", "Board serial", self._privileged_serial("board_serial"))
        )
        section.add(
            self.row(
                "board_asset",
                "Asset tag",
                or_missing(self._dmi("board_asset_tag"), NOT_REPORTED),
            )
        )
        section.add(
            self.row(
                "product_name",
                "System model",
                or_missing(self._dmi("product_name"), NOT_REPORTED),
            )
        )
        section.add(
            self.row(
                "product_family",
                "System family",
                or_missing(self._dmi("product_family"), NOT_REPORTED),
            )
        )
        section.add(
            self.row(
                "sys_vendor",
                "System vendor",
                or_missing(self._dmi("sys_vendor"), NOT_REPORTED),
            )
        )
        return section

    # -- firmware ---------------------------------------------------------

    def _firmware(self) -> Section:
        section = Section(id="firmware", label="Firmware")
        section.add(
            self.row(
                "bios_vendor",
                "Firmware vendor",
                or_missing(self._dmi("bios_vendor"), NOT_REPORTED),
            )
        )
        section.add(
            self.row(
                "bios_version",
                "Firmware version",
                or_missing(self._dmi("bios_version"), NOT_REPORTED),
            )
        )
        section.add(
            self.row("bios_date", "Firmware date", or_missing(self._dmi("bios_date"), NOT_REPORTED))
        )
        release = self._dmi("bios_release")
        if release:
            section.add(self.row("bios_release", "Firmware release", release))

        efi = path_exists("/sys/firmware/efi")
        section.add(
            self.row(
                "boot_mode",
                "Boot mode",
                "UEFI" if efi else "Legacy BIOS — no /sys/firmware/efi on this machine",
            )
        )
        if efi:
            size = read_first_line("/sys/firmware/efi/fw_platform_size")
            section.add(
                self.row(
                    "fw_platform_size",
                    "Firmware word size",
                    f"{size}-bit" if size else NOT_REPORTED,
                )
            )

        for label, name in (
            ("ACPI tables", "/sys/firmware/acpi/tables"),
            ("Device tree", "/sys/firmware/devicetree"),
        ):
            if path_exists(name):
                section.add(
                    self.row(
                        "firmware_interface",
                        label,
                        "Present",
                        key=f"iface{label}",
                    )
                )
        return section

    # -- chassis ----------------------------------------------------------

    def _chassis(self) -> Section:
        section = Section(id="chassis", label="Chassis")
        chassis_type = read_int(f"{DMI}/chassis_type")
        if chassis_type is not None:
            name = CHASSIS_TYPES.get(chassis_type)
            value = f"{name} (type {chassis_type})" if name else f"Type {chassis_type}, not named in the SMBIOS table"
        else:
            value = NOT_REPORTED
        section.add(self.row("chassis_type", "Chassis type", value))
        section.add(
            self.row(
                "chassis_vendor",
                "Chassis vendor",
                or_missing(self._dmi("chassis_vendor"), NOT_REPORTED),
            )
        )
        section.add(
            self.row(
                "chassis_version",
                "Chassis version",
                or_missing(self._dmi("chassis_version"), NOT_REPORTED),
            )
        )
        section.add(
            self.row(
                "chassis_asset",
                "Chassis asset tag",
                or_missing(self._dmi("chassis_asset_tag"), NOT_REPORTED),
            )
        )
        section.add(
            self.row(
                "product_serial", "System serial", self._privileged_serial("product_serial")
            )
        )
        section.add(
            self.row("product_uuid", "System UUID", self._privileged_serial("product_uuid"))
        )
        section.add(
            self.row(
                "product_sku",
                "SKU",
                or_missing(self._dmi("product_sku"), NOT_REPORTED),
            )
        )
        section.add(
            self.row(
                "product_version",
                "System version",
                or_missing(self._dmi("product_version"), NOT_REPORTED),
            )
        )
        return section
