# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""USB devices, one row 3 instance each.

``/sys/bus/usb/devices`` holds two kinds of entry. ``1-4`` is a device;
``1-4:1.0`` is one interface of that device. Only devices become instances, but
the interfaces are read anyway, because the interface is where a device says
what it actually is: a great many devices declare class 0 at the top level,
meaning "look at my interfaces", and stopping at the device level would leave
half the machine's peripherals labelled "Unknown".

That interface class is also what picks the glyph on the tab. Fifteen wrapped
USB entries reading "USB2.0 Hub" and "USB Receiver" are hard to tell apart at a
glance; the same fifteen with a hub, a keyboard and a camera icon are not.
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
    read_link_name,
)
from .hwids import describe_device, describe_vendor, format_pair

USB_ROOT = "/sys/bus/usb/devices"

# bDeviceClass and bInterfaceClass share this table.
USB_CLASSES = {
    0x00: "Defined at interface level",
    0x01: "Audio",
    0x02: "Communications",
    0x03: "Human interface",
    0x05: "Physical",
    0x06: "Still imaging",
    0x07: "Printer",
    0x08: "Mass storage",
    0x09: "Hub",
    0x0A: "Communications data",
    0x0B: "Smart card",
    0x0D: "Content security",
    0x0E: "Video",
    0x0F: "Personal healthcare",
    0x10: "Audio and video",
    0x11: "Billboard",
    0x12: "USB-C bridge",
    0x3C: "I3C",
    0xDC: "Diagnostic",
    0xE0: "Wireless controller",
    0xEF: "Miscellaneous",
    0xFE: "Application specific",
    0xFF: "Vendor specific",
}

# HID interface protocols, which is where keyboard and mouse are distinguished.
HID_PROTOCOLS = {0: "no boot protocol", 1: "keyboard", 2: "mouse"}

# Speeds as sysfs reports them, in Mbit/s, with the marketing name people know.
SPEED_NAMES = {
    "1.5": "USB 1.0 low speed",
    "12": "USB 1.1 full speed",
    "480": "USB 2.0 high speed",
    "5000": "USB 3.0 SuperSpeed, 5 Gbit/s",
    "10000": "USB 3.1 SuperSpeed+, 10 Gbit/s",
    "20000": "USB 3.2 SuperSpeed+, 20 Gbit/s",
    "40000": "USB4, 40 Gbit/s",
}


# class -> glyph concept, for the classes that have an unambiguous one.
_CLASS_ICONS = {
    0x09: "usb_hub",
    0x08: "usb_storage",
    0x01: "usb_audio",
    0x0E: "usb_video",
}


def icon_for(device) -> str:
    """The glyph concept for a device, from the first class that names one.

    The device-level class is tried first, then each interface in turn. That
    ordering matters for two common shapes: a device declaring class 0 says
    "read my interfaces" and has nothing useful at the top level, and a
    composite device declaring class 0xEF (miscellaneous) is telling you only
    that it does more than one thing. In both cases the interface is where the
    answer is. A device that matches nothing gets the question mark rather than
    a vague approximation.
    """
    candidates = []
    device_class = device.get("device_class")
    if device_class not in (None, 0x00, 0xEF, 0xFF):
        candidates.append(
            (device_class, device.get("device_subclass"), device.get("device_protocol"))
        )
    for item in device.get("interfaces") or []:
        candidates.append((item.get("class"), item.get("subclass"), item.get("protocol")))
    if device_class in (0xEF, 0xFF):
        candidates.append(
            (device_class, device.get("device_subclass"), device.get("device_protocol"))
        )

    for klass, subclass, protocol in candidates:
        if klass in _CLASS_ICONS:
            return _CLASS_ICONS[klass]
        if klass == 0x03:
            if protocol == 1:
                return "usb_keyboard"
            if protocol == 2:
                return "usb_pointer"
        if klass == 0xE0 and subclass == 0x01 and protocol in (0x01, 0x02):
            # Wireless controller, radio frequency, Bluetooth programming
            # interface - the triple every Bluetooth radio reports.
            return "bluetooth"
    return "usb_unknown"


class UsbProbe(Probe):
    branch = "peripherals"
    id = "usb"
    label = "USB"

    def __init__(self, context=None):
        super().__init__(context)
        self._devices: list[dict] = []

    # -- enumeration ------------------------------------------------------

    def _find_devices(self) -> list[dict]:
        devices = []
        for path in list_dir(USB_ROOT):
            # ":" marks an interface, not a device.
            if ":" in path.name:
                continue
            if not path_exists(path / "idVendor"):
                continue
            interfaces = self._interfaces(path)
            devices.append(
                {
                    "name": path.name,
                    "path": path,
                    "vendor": read_first_line(path / "idVendor"),
                    "product": read_first_line(path / "idProduct"),
                    "manufacturer": read_first_line(path / "manufacturer"),
                    "product_name": read_first_line(path / "product"),
                    "serial": read_first_line(path / "serial"),
                    "device_class": read_int(path / "bDeviceClass", base=16),
                    "device_subclass": read_int(path / "bDeviceSubClass", base=16),
                    "device_protocol": read_int(path / "bDeviceProtocol", base=16),
                    "interfaces": interfaces,
                    "is_root": path.name.startswith("usb"),
                }
            )
        return devices

    def _interfaces(self, path) -> list[dict]:
        """The interfaces of one device.

        Scanned as siblings under the bus directory rather than as children of
        the device. sysfs presents them both ways - 1-4:1.0 sits beside 1-4 as
        a symlink and beneath it as a real directory - and the sibling view is
        the one that is always populated.
        """
        out = []
        prefix = path.name + ":"
        for child in list_dir(USB_ROOT):
            if not child.name.startswith(prefix):
                continue
            out.append(
                {
                    "name": child.name,
                    "class": read_int(child / "bInterfaceClass", base=16),
                    "subclass": read_int(child / "bInterfaceSubClass", base=16),
                    "protocol": read_int(child / "bInterfaceProtocol", base=16),
                    "driver": read_link_name(child / "driver"),
                }
            )
        return out

    def sections(self) -> list[Section]:
        devices = self._find_devices()
        self._devices = devices
        if not devices:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No USB devices were found. This machine has no USB controller, "
                    "or none is bound to a driver.",
                )
            ]
        return [self._device_section(device) for device in devices]

    def _device_section(self, device) -> Section:
        section = Section(
            id=device["name"],
            label=self._label(device),
            icon=icon_for(device),
        )
        usb = self.context.usb_ids

        section.add(
            self.row(
                "name",
                "Device",
                device["product_name"]
                or describe_device(usb, device["vendor"], device["product"]),
            )
        )
        section.add(
            self.row(
                "vendor",
                "Vendor",
                device["manufacturer"] or describe_vendor(usb, device["vendor"]),
            )
        )
        section.add(
            self.row("usb_id", "USB ID", format_pair(device["vendor"], device["product"]))
        )
        section.add(
            self.row(
                "database_name",
                "Name in the database",
                describe_device(usb, device["vendor"], device["product"]),
            )
        )
        section.add(
            self.row("serial", "Serial number", or_missing(device["serial"], NOT_REPORTED))
        )
        section.add(self.row("port", "Port path", device["name"]))

        section.add(
            self.row(
                "device_class",
                "Class",
                _class_text(device["device_class"], device["device_subclass"], device["device_protocol"]),
            )
        )

        for row in self._speed_rows(device):
            section.add(row)
        for row in self._power_rows(device):
            section.add(row)
        for row in self._version_rows(device):
            section.add(row)
        for row in self._interface_rows(device):
            section.add(row)
        return section

    def _label(self, device) -> str:
        name = device["product_name"]
        if not name:
            usb = self.context.usb_ids
            if usb is not None:
                name = usb.device(device["vendor"], device["product"])
        if not name:
            name = device["name"]
        return name if len(name) <= 24 else name[:23] + "…"

    def _speed_rows(self, device) -> list:
        speed = read_first_line(device["path"] / "speed")
        rows = []
        if speed is None:
            rows.append(self.row("speed", "Speed", NOT_REPORTED))
        else:
            named = SPEED_NAMES.get(speed)
            rows.append(
                self.row(
                    "speed",
                    "Speed",
                    f"{speed} Mbit/s — {named}" if named else f"{speed} Mbit/s",
                )
            )
        version = read_first_line(device["path"] / "version")
        if version:
            rows.append(self.row("version", "USB version", version.strip()))
        return rows

    def _power_rows(self, device) -> list:
        rows = []
        max_power = read_first_line(device["path"] / "bMaxPower")
        if max_power:
            rows.append(self.row("max_power", "Maximum draw", max_power))
        control = read_first_line(device["path"] / "power/control")
        if control:
            rows.append(self.row("power_control", "Power management", control))
        return rows

    def _version_rows(self, device) -> list:
        rows = []
        revision = read_first_line(device["path"] / "bcdDevice")
        if revision:
            rows.append(self.row("revision", "Device revision", revision))
        for field, label, name in (
            ("bus", "Bus number", "busnum"),
            ("address", "Device address", "devnum"),
            ("configurations", "Configurations", "bNumConfigurations"),
        ):
            value = read_first_line(device["path"] / name)
            if value:
                rows.append(self.row(field, label, value))
        return rows

    def _interface_rows(self, device) -> list:
        interfaces = device["interfaces"]
        if not interfaces:
            return [
                self.row(
                    "interfaces",
                    "Interfaces",
                    "None claimed. Nothing has bound a driver to this device.",
                )
            ]
        rows = [self.row("interfaces", "Interfaces", str(len(interfaces)))]
        for item in interfaces:
            detail = _class_text(item["class"], item["subclass"], item["protocol"])
            if item["class"] == 0x03 and item["protocol"] in HID_PROTOCOLS:
                detail += f" — {HID_PROTOCOLS[item['protocol']]}"
            detail += f", driver {item['driver']}" if item["driver"] else ", no driver bound"
            rows.append(
                self.row(
                    "interface",
                    f"  {item['name']}",
                    detail,
                    key=f"iface{item['name']}",
                )
            )
        return rows


def _class_text(value: int | None, subclass: int | None, protocol: int | None) -> str:
    if value is None:
        return NOT_REPORTED
    name = USB_CLASSES.get(value)
    text = f"{name} (0x{value:02x})" if name else f"0x{value:02x}, not named in the USB class table"
    parts = []
    if subclass is not None:
        parts.append(f"subclass 0x{subclass:02x}")
    if protocol is not None:
        parts.append(f"protocol 0x{protocol:02x}")
    return f"{text}, {', '.join(parts)}" if parts else text
