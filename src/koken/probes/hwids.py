# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Turning numbers into names, out of the system's own id files.

``hwdata`` ships ``pci.ids`` and ``usb.ids``: two plain text files that the
whole Linux world agrees on, mapping ``1002:747e`` to "Navi 32 [Radeon RX 7800
XT]". Reading them directly is the reason this application does not shell out
to ``lspci``, and the reason it does not carry its own stale copy of the same
data.

Both files use the same shape. A line with no indentation opens a vendor, one
tab in names a device under it, two tabs in names a subsystem of that device.
``pci.ids`` then closes with a separate section, introduced by lines beginning
``C``, that maps device class codes to human categories.

When a lookup misses, the caller is given ``None`` and says so plainly. A name
is never invented, and a partial match is never dressed up as a full one.
"""

from __future__ import annotations

from pathlib import Path

# Searched in order. CORE names the hwdata paths; Debian installs the same
# files under /usr/share/misc and symlinks them into place, but the symlinks
# come from a package that can be absent, so both are tried.
PCI_IDS_PATHS = (
    "/usr/share/hwdata/pci.ids",
    "/usr/share/misc/pci.ids",
    "/usr/share/pci.ids",
)

USB_IDS_PATHS = (
    "/usr/share/hwdata/usb.ids",
    "/usr/share/misc/usb.ids",
    "/usr/share/usb.ids",
)

# usb.ids continues past the vendor list with sections that are not vendors:
# device classes, audio terminals, HID descriptors, and more. They are all
# introduced by a short uppercase token and a space, and none of them is a
# four-digit hex id, so a section line is recognised and the section skipped.
_SECTION_TOKENS = {
    "C",
    "AT",
    "HID",
    "R",
    "BIAS",
    "PHY",
    "HUT",
    "L",
    "HCC",
    "VT",
}


class IdDatabase:
    """A parsed ids file.

    Empty is a valid state: a machine without ``hwdata`` installed gets a
    database that misses every lookup, and every caller already handles a miss.
    """

    def __init__(self, source: Path | None = None) -> None:
        self.source = source
        self.vendors: dict[str, str] = {}
        self.devices: dict[tuple[str, str], str] = {}
        self.subsystems: dict[tuple[str, str, str, str], str] = {}
        # Keyed by code length: "03" a class, "0380" a subclass, "038000" the
        # programming interface. Only pci.ids populates this.
        self.classes: dict[str, str] = {}

    @property
    def loaded(self) -> bool:
        return bool(self.vendors) or bool(self.classes)

    # -- lookups ----------------------------------------------------------

    def vendor(self, vendor_id: str | int | None) -> str | None:
        key = _hex4(vendor_id)
        return self.vendors.get(key) if key else None

    def device(
        self, vendor_id: str | int | None, device_id: str | int | None
    ) -> str | None:
        vendor_key, device_key = _hex4(vendor_id), _hex4(device_id)
        if not vendor_key or not device_key:
            return None
        return self.devices.get((vendor_key, device_key))

    def subsystem(
        self,
        vendor_id: str | int | None,
        device_id: str | int | None,
        subsystem_vendor: str | int | None,
        subsystem_device: str | int | None,
    ) -> str | None:
        keys = (
            _hex4(vendor_id),
            _hex4(device_id),
            _hex4(subsystem_vendor),
            _hex4(subsystem_device),
        )
        if not all(keys):
            return None
        return self.subsystems.get(keys)  # type: ignore[arg-type]

    def device_class(self, code: str | int | None) -> str | None:
        """The subclass name for a class code - what lspci prints.

        sysfs reports six hex digits: class, subclass and programming
        interface. The subclass is the level people recognise - "VGA
        compatible controller", "Ethernet controller", "USB controller" - so
        that is what this returns, falling back to the bare class name when
        pci.ids names no subclass.
        """
        text = _hex_code(code)
        if not text:
            return None
        return self.classes.get(text[:4]) or self.classes.get(text[:2])

    def programming_interface(self, code: str | int | None) -> str | None:
        """The prog-if name - "XHCI", "NVM Express" - or None.

        pci.ids names this for only a minority of devices, so a miss here is
        ordinary and the caller simply omits the detail.
        """
        text = _hex_code(code)
        if not text:
            return None
        return self.classes.get(text)

    def class_category(self, code: str | int | None) -> str | None:
        """The top-level class name only - "Display controller", "Network controller"."""
        text = _hex_code(code)
        if not text:
            return None
        return self.classes.get(text[:2])


def _hex4(value: str | int | None) -> str | None:
    """Normalise an id to four lowercase hex digits."""
    if value is None:
        return None
    if isinstance(value, int):
        return f"{value:04x}"
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text.rjust(4, "0")[-4:] if len(text) >= 4 else text.rjust(4, "0")


def _hex_code(value: str | int | None) -> str | None:
    """Normalise a class code to six lowercase hex digits."""
    if value is None:
        return None
    if isinstance(value, int):
        return f"{value:06x}"
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    # Padded on the right, not the left: a class code reads class, subclass,
    # programming interface from the top, so "0300" is 03/00/00 and not
    # 00/03/00. Getting this backwards silently mislabels every device.
    return text.ljust(6, "0")[:6]


def parse(path: Path, with_classes: bool = False) -> IdDatabase:
    """Parse one ids file. An unreadable file yields an empty database."""
    db = IdDatabase(source=path)
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return db

    vendor_key: str | None = None
    device_key: str | None = None
    class_prefix: str | None = None
    subclass_prefix: str | None = None
    # Set while inside a section of usb.ids that is not the vendor list, or
    # inside the pci.ids class list when the caller does not want it.
    skipping = False

    with handle:
        for raw in handle:
            line = raw.rstrip("\n").rstrip()
            if not line or line.lstrip().startswith("#"):
                continue

            depth = len(line) - len(line.lstrip("\t"))
            body = line.lstrip("\t")

            if depth == 0:
                token = body.split(" ", 1)[0]
                if token in _SECTION_TOKENS:
                    vendor_key = device_key = None
                    if token == "C" and with_classes:
                        class_prefix, class_name = _parse_class_open(body)
                        subclass_prefix = None
                        skipping = class_prefix is None
                        if class_prefix is not None:
                            db.classes.setdefault(class_prefix, class_name)
                    else:
                        class_prefix = subclass_prefix = None
                        skipping = True
                    continue
                # A vendor line: four hex digits, two spaces, the name.
                skipping = False
                class_prefix = subclass_prefix = None
                key, name = _split_id(body)
                if key is None:
                    vendor_key = device_key = None
                    continue
                vendor_key = key
                device_key = None
                db.vendors.setdefault(vendor_key, name)
                continue

            if skipping:
                continue

            if class_prefix is not None:
                # Inside the pci.ids class section.
                key, name = _split_id(body, width=2)
                if key is None:
                    continue
                if depth == 1:
                    subclass_prefix = class_prefix + key
                    db.classes.setdefault(subclass_prefix, name)
                elif depth == 2 and subclass_prefix is not None:
                    db.classes.setdefault(subclass_prefix + key, name)
                continue

            if vendor_key is None:
                continue

            if depth == 1:
                key, name = _split_id(body)
                if key is None:
                    device_key = None
                    continue
                device_key = key
                db.devices.setdefault((vendor_key, device_key), name)
                continue

            if depth >= 2 and device_key is not None:
                pair, name = _split_subsystem(body)
                if pair is None:
                    continue
                db.subsystems.setdefault(
                    (vendor_key, device_key, pair[0], pair[1]), name
                )

    return db


def _parse_class_open(body: str) -> tuple[str | None, str]:
    """``C 03  Display controller`` -> the prefix ``03`` and the class name."""
    parts = body.split(None, 1)
    if len(parts) < 2:
        return None, ""
    return _split_id(parts[1], width=2)


def _split_id(body: str, width: int = 4) -> tuple[str | None, str]:
    """Split ``747e  Navi 32`` into its id and its name."""
    parts = body.split("  ", 1)
    if len(parts) != 2:
        parts = body.split(None, 1)
        if len(parts) != 2:
            return None, ""
    key, name = parts[0].strip().lower(), parts[1].strip()
    if len(key) != width:
        return None, ""
    try:
        int(key, 16)
    except ValueError:
        return None, ""
    return key, name


def _split_subsystem(body: str) -> tuple[tuple[str, str] | None, str]:
    """Split ``1849 5313  RX 7600 Challenger OC`` into its id pair and its name."""
    parts = body.split("  ", 1)
    if len(parts) != 2:
        return None, ""
    ids, name = parts[0].split(), parts[1].strip()
    if len(ids) != 2:
        return None, ""
    first, second = ids[0].strip().lower(), ids[1].strip().lower()
    for value in (first, second):
        if len(value) != 4:
            return None, ""
        try:
            int(value, 16)
        except ValueError:
            return None, ""
    return (first, second), name


def _load_first(paths, with_classes: bool) -> IdDatabase:
    for candidate in paths:
        path = Path(candidate)
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        db = parse(path, with_classes=with_classes)
        if db.loaded:
            return db
    return IdDatabase()


def load_pci_ids() -> IdDatabase:
    """Parse ``pci.ids``, including its device class section."""
    return _load_first(PCI_IDS_PATHS, with_classes=True)


def load_usb_ids() -> IdDatabase:
    """Parse ``usb.ids``. Its class section is not used and is skipped."""
    return _load_first(USB_IDS_PATHS, with_classes=False)


# -- presentation ---------------------------------------------------------
#
# These build the strings the probes show. They live here so that "no entry"
# is worded identically everywhere it appears.

NO_ENTRY = "no entry in the local device database"


def describe_device(
    db: IdDatabase | None,
    vendor_id: str | int | None,
    device_id: str | int | None,
) -> str:
    """A device as ``Name`` or, on a miss, ``vvvv:dddd (no entry ...)``."""
    pair = format_pair(vendor_id, device_id)
    if db is None:
        return f"{pair} ({NO_ENTRY})"
    name = db.device(vendor_id, device_id)
    if name:
        return name
    return f"{pair} ({NO_ENTRY})"


def describe_vendor(db: IdDatabase | None, vendor_id: str | int | None) -> str:
    key = _hex4(vendor_id)
    if db is not None:
        name = db.vendor(vendor_id)
        if name:
            return name
    if key is None:
        from .base import NOT_AVAILABLE

        return NOT_AVAILABLE
    return f"{key} ({NO_ENTRY})"


def describe_subsystem(
    db: IdDatabase | None,
    vendor_id: str | int | None,
    device_id: str | int | None,
    subsystem_vendor: str | int | None,
    subsystem_device: str | int | None,
) -> str:
    """The subsystem name, which is the board partner on a graphics card."""
    pair = format_pair(subsystem_vendor, subsystem_device)
    if db is None:
        return f"{pair} ({NO_ENTRY})"
    name = db.subsystem(vendor_id, device_id, subsystem_vendor, subsystem_device)
    if name:
        vendor_name = db.vendor(subsystem_vendor)
        return f"{name} ({vendor_name})" if vendor_name else name
    vendor_name = db.vendor(subsystem_vendor)
    if vendor_name:
        return f"{vendor_name} — {pair} ({NO_ENTRY})"
    return f"{pair} ({NO_ENTRY})"


def format_pair(vendor_id: str | int | None, device_id: str | int | None) -> str:
    """``1002:747e``, the form people paste into a search box."""
    from .base import NOT_AVAILABLE

    vendor_key, device_key = _hex4(vendor_id), _hex4(device_id)
    if not vendor_key and not device_key:
        return NOT_AVAILABLE
    return f"{vendor_key or '????'}:{device_key or '????'}"
