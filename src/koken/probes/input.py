# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Input devices, one row 3 instance each.

Every keyboard, mouse, touchpad, lid switch, power button and virtual console
the kernel knows about appears here, which is a longer list than most people
expect - a laptop typically has a dozen.

What each one *is* has to be worked out from its capability bitmaps rather than
read from a field, because the kernel does not store a device type. A device
that reports relative axes and buttons is a mouse; one that reports a great many
keys is a keyboard; one that reports absolute axes and a finger tool is a
touchpad. That is the same reasoning every input stack on the system uses, and
it is done here rather than guessed at from the device's name.
"""

from __future__ import annotations

from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    Probe,
    Section,
    fmt_list,
    list_dir,
    or_missing,
    path_exists,
    read_first_line,
    read_int,
)

INPUT_ROOT = "/sys/class/input"

# Event type bits from the kernel's input-event-codes.h.
EV_SYN, EV_KEY, EV_REL, EV_ABS, EV_MSC, EV_SW, EV_LED, EV_SND, EV_REP, EV_FF = (
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x11, 0x12, 0x14, 0x15,
)

EVENT_NAMES = {
    EV_SYN: "synchronisation",
    EV_KEY: "keys and buttons",
    EV_REL: "relative axes",
    EV_ABS: "absolute axes",
    EV_MSC: "miscellaneous",
    EV_SW: "switches",
    EV_LED: "indicator lights",
    EV_SND: "sound",
    EV_REP: "auto repeat",
    EV_FF: "force feedback",
}

BUS_TYPES = {
    0x01: "PCI",
    0x03: "USB",
    0x05: "Bluetooth",
    0x11: "ISA",
    0x12: "i8042 (PS/2)",
    0x13: "Infrared",
    0x18: "I2C",
    0x19: "Host",
    0x1B: "Virtual",
    0x06: "Virtual",
}

# Key codes that decide what a device is.
KEY_A = 30
BTN_LEFT = 0x110
BTN_TOUCH = 0x14A
BTN_TOOL_FINGER = 0x145
ABS_X = 0x00
REL_X = 0x00


def parse_bitmap(text: str | None) -> set[int]:
    """A sysfs capability bitmap into the set of bits it has set.

    The kernel writes these as space-separated hexadecimal words, most
    significant word first, each word being one machine long.
    """
    if not text:
        return set()
    words = text.split()
    bits: set[int] = set()
    for position, word in enumerate(reversed(words)):
        try:
            value = int(word, 16)
        except ValueError:
            continue
        base = position * 64
        index = 0
        while value:
            if value & 1:
                bits.add(base + index)
            value >>= 1
            index += 1
    return bits


def classify(events: set[int], keys: set[int], relative: set[int], absolute: set[int]) -> tuple[str, str]:
    """What the device is, and which glyph concept fits it."""
    has_finger = BTN_TOOL_FINGER in keys or BTN_TOUCH in keys

    if EV_ABS in events and absolute and has_finger:
        return ("Touchpad or touchscreen", "usb_pointer")
    if EV_REL in events and REL_X in relative and BTN_LEFT in keys:
        return ("Pointing device", "usb_pointer")
    if EV_KEY in events and KEY_A in keys:
        return ("Keyboard", "usb_keyboard")
    if EV_SW in events:
        return ("Switch — a lid, a dock, or a hardware toggle", "usb_unknown")
    if EV_ABS in events and absolute:
        return ("Absolute pointing device or game controller", "usb_pointer")
    if EV_KEY in events and keys:
        return ("Buttons only — a power button, or a set of media keys", "usb_unknown")
    return ("Not classifiable from its capabilities", "usb_unknown")


class InputProbe(Probe):
    branch = "peripherals"
    id = "input"
    label = "Input"

    def _find_devices(self) -> list[dict]:
        devices = []
        for path in list_dir(INPUT_ROOT):
            if not path.name.startswith("input"):
                continue
            if not path.name[5:].isdigit():
                continue
            capabilities = path / "capabilities"
            events = parse_bitmap(read_first_line(capabilities / "ev"))
            keys = parse_bitmap(read_first_line(capabilities / "key"))
            relative = parse_bitmap(read_first_line(capabilities / "rel"))
            absolute = parse_bitmap(read_first_line(capabilities / "abs"))
            kind, icon = classify(events, keys, relative, absolute)
            devices.append(
                {
                    "name": path.name,
                    "path": path,
                    "label": read_first_line(path / "name"),
                    "phys": read_first_line(path / "phys"),
                    "uniq": read_first_line(path / "uniq"),
                    "bus": read_int(path / "id/bustype", base=16),
                    "vendor": read_first_line(path / "id/vendor"),
                    "product": read_first_line(path / "id/product"),
                    "version": read_first_line(path / "id/version"),
                    "events": events,
                    "keys": keys,
                    "relative": relative,
                    "absolute": absolute,
                    "kind": kind,
                    "icon": icon,
                    "nodes": [
                        child.name
                        for child in list_dir(path)
                        if child.name.startswith(("event", "mouse", "js"))
                    ],
                }
            )
        return devices

    def sections(self) -> list[Section]:
        devices = self._find_devices()
        if not devices:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No input devices were found. A machine with no keyboard, mouse "
                    "or console attached looks like this.",
                )
            ]
        return [self._device_section(device) for device in devices]

    def _device_section(self, device) -> Section:
        section = Section(
            id=device["name"],
            label=self._label(device),
            icon=device["icon"],
        )

        section.add(
            self.row("name", "Name", or_missing(device["label"], NOT_REPORTED))
        )
        section.add(self.row("kind", "Kind", device["kind"]))
        section.add(
            self.row(
                "bus",
                "Connected by",
                BUS_TYPES.get(device["bus"], f"Bus type 0x{device['bus']:02x}")
                if device["bus"] is not None
                else NOT_REPORTED,
            )
        )
        section.add(
            self.row(
                "identity",
                "Vendor and product",
                "{}:{}".format(
                    device["vendor"] or "????", device["product"] or "????"
                ),
            )
        )
        if device["version"]:
            section.add(self.row("version", "Version", device["version"]))
        section.add(
            self.row("phys", "Physical path", or_missing(device["phys"], NOT_REPORTED))
        )
        if device["uniq"]:
            section.add(self.row("uniq", "Unique identifier", device["uniq"]))

        section.add(
            self.row(
                "event_types",
                "Reports",
                fmt_list(
                    EVENT_NAMES.get(bit, f"type 0x{bit:02x}")
                    for bit in sorted(device["events"])
                    if bit != EV_SYN
                )
                or "Nothing",
            )
        )
        if device["keys"]:
            section.add(
                self.row("key_count", "Keys and buttons", str(len(device["keys"])))
            )
        if device["relative"]:
            section.add(
                self.row("relative_axes", "Relative axes", str(len(device["relative"])))
            )
        if device["absolute"]:
            section.add(
                self.row("absolute_axes", "Absolute axes", str(len(device["absolute"])))
            )
        section.add(
            self.row(
                "nodes",
                "Device nodes",
                fmt_list(f"/dev/input/{name}" for name in device["nodes"])
                or "None — this device has no event node",
            )
        )
        section.add(self.row("sysfs", "Kernel name", device["name"]))
        return section

    def _label(self, device) -> str:
        name = device["label"] or device["name"]
        return name if len(name) <= 24 else name[:23] + "…"
