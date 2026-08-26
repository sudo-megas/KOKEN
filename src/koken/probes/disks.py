# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Physical drives, and the udisks2 access layer the whole Storage branch uses.

Most of a drive is readable straight out of ``/sys/class/block``. SMART is not:
the kernel exposes no SMART interface at all, and reading it means either
talking ATA pass-through as root or asking something that already does. udisks2
already does, over D-Bus, with its own polkit policy, for both ATA and NVMe.
Using it is what lets this application avoid both a privileged SMART path of
its own and a dependency on smartmontools.

The D-Bus layer below is shared with :mod:`koken.probes.volumes`, which needs
the same object graph to find each partition's filesystem and its object path.
It is written on the assumption that udisks2 is simply not there: no service, no
system bus, or a bus that answers with an error. Every one of those cases lands
in the same place a missing sysfs file lands - a value of None and a row that
says so - with no traceback and nothing on the console.

Enumeration goes through Introspect and then per-interface ``GetAll`` calls,
rather than one ObjectManager call returning the whole nested graph. That is
more round trips, but each reply is a flat ``a{sv}`` that demarshals to an
ordinary dictionary, where the nested form arrives as a container this binding
cannot reliably unpack. Both happen only on enumeration, never on the timer.
"""

from __future__ import annotations

import re

from .base import (
    DANGER,
    NOT_AVAILABLE,
    NOT_REPORTED,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_bytes,
    fmt_duration,
    fmt_int,
    fmt_list,
    glob_dirs,
    list_dir,
    or_missing,
    path_exists,
    read_first_line,
    read_int,
    read_text,
    resolve,
)

BLOCK_ROOT = "/sys/class/block"

SERVICE = "org.freedesktop.UDisks2"
ROOT_PATH = "/org/freedesktop/UDisks2"
DRIVES_PATH = ROOT_PATH + "/drives"
BLOCK_PATH = ROOT_PATH + "/block_devices"

IFACE_DRIVE = "org.freedesktop.UDisks2.Drive"
IFACE_ATA = "org.freedesktop.UDisks2.Drive.Ata"
IFACE_NVME = "org.freedesktop.UDisks2.NVMe.Controller"
IFACE_BLOCK = "org.freedesktop.UDisks2.Block"
IFACE_FILESYSTEM = "org.freedesktop.UDisks2.Filesystem"
IFACE_PARTITION = "org.freedesktop.UDisks2.Partition"
IFACE_SWAP = "org.freedesktop.UDisks2.Swapspace"
IFACE_PROPERTIES = "org.freedesktop.DBus.Properties"
IFACE_INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"

# Sectors are 512 bytes in /sys/class/block/*/size regardless of the drive's
# real block size. This trips people up often enough to be worth naming.
SECTOR_BYTES = 512

# nvme0n1 -> controller nvme0, namespace 1.
_NVME_NAME = re.compile(r"^(?P<controller>nvme\d+)n\d+")

# What may follow a disk name to make a partition name. The kernel's rule, in
# block/partition-generic.c, is not "an optional p": a disk whose name ends in
# a digit takes a mandatory `p` before the number, and one that does not takes
# the bare number. Accepting `p?` for both is what lets nvme0n1 claim nvme0n11,
# which is not its partition but the eleventh namespace of the same controller.
_PARTITION_DIGITS = re.compile(r"^\d+$")
_PARTITION_P_DIGITS = re.compile(r"^p\d+$")


def _partition_of(disk_name: str, name: str) -> bool:
    """Whether *name* is the name the kernel would give a partition of *disk_name*."""
    if not name.startswith(disk_name) or name == disk_name:
        return False
    suffix = name[len(disk_name) :]
    if disk_name and disk_name[-1].isdigit():
        return bool(_PARTITION_P_DIGITS.match(suffix))
    return bool(_PARTITION_DIGITS.match(suffix))


def _partitions_of(disk_name: str) -> list:
    """Partition directories belonging to *disk_name*.

    Scanned as siblings under /sys/class/block rather than as children of the
    disk directory. sysfs presents partitions both ways - as a symlink beside
    the disk and as a real subdirectory beneath it - and the sibling view is
    the one that is always populated.
    """
    out = []
    for path in list_dir(BLOCK_ROOT):
        if not _partition_of(disk_name, path.name):
            continue
        if path_exists(path / "partition"):
            out.append(path)
    return out


# ==========================================================================
# udisks2
# ==========================================================================


class Udisks2:
    """A snapshot of what udisks2 knows, or a clear statement that it is absent.

    ``available`` is False for every failure mode - no PySide QtDBus, no system
    bus, no service, an error reply - and ``reason`` says which, in a sentence
    fit to put on screen.
    """

    def __init__(self, available: bool = False, reason: str = "") -> None:
        self.available = available
        self.reason = reason
        self._bus = None
        self._properties: dict[tuple[str, str], dict] = {}
        self._drive_paths: list[str] = []
        self._block_paths: list[str] = []

    # -- construction -----------------------------------------------------

    @classmethod
    def unavailable(cls, reason: str) -> "Udisks2":
        return cls(available=False, reason=reason)

    @classmethod
    def connect(cls) -> "Udisks2":
        try:
            from PySide6.QtDBus import QDBusConnection
        except ImportError:
            return cls.unavailable(
                "This build of PySide6 has no QtDBus module, so udisks2 cannot be "
                "reached. SMART data and the mount controls are unavailable."
            )

        bus = QDBusConnection.systemBus()
        if not bus.isConnected():
            return cls.unavailable(
                "There is no connection to the system message bus, so udisks2 "
                "cannot be reached. SMART data and the mount controls are unavailable."
            )

        client = cls(available=True)
        client._bus = bus
        client._drive_paths = client._children(DRIVES_PATH)
        client._block_paths = client._children(BLOCK_PATH)
        if not client._drive_paths and not client._block_paths:
            probe = client._get_all(ROOT_PATH, "org.freedesktop.UDisks2.Manager")
            if probe is None:
                return cls.unavailable(
                    "udisks2 is not answering on the system bus. It is probably not "
                    "installed or not running. SMART data and the mount controls "
                    "are unavailable."
                )
        return client

    @classmethod
    def from_snapshot(
        cls,
        properties: dict[tuple[str, str], dict],
        drive_paths: list[str],
        block_paths: list[str],
    ) -> "Udisks2":
        """Build a client from captured data, with no bus involved.

        This is the seam that makes the whole Storage branch, including the
        mount and unmount call layer, exercisable where udisks2 is not running.
        """
        client = cls(available=True)
        client._properties = dict(properties)
        client._drive_paths = list(drive_paths)
        client._block_paths = list(block_paths)
        return client

    # -- raw calls --------------------------------------------------------

    def _interface(self, path: str, interface: str):
        if self._bus is None:
            return None
        try:
            from PySide6.QtDBus import QDBusInterface

            handle = QDBusInterface(SERVICE, path, interface, self._bus)
        except Exception:
            return None
        return handle if handle.isValid() else None

    def _children(self, path: str) -> list[str]:
        """Object paths one level under *path*, via Introspect."""
        handle = self._interface(path, IFACE_INTROSPECTABLE)
        if handle is None:
            return []
        try:
            message = handle.call("Introspect")
            arguments = message.arguments()
        except Exception:
            return []
        if not arguments or not isinstance(arguments[0], str):
            return []
        return [f"{path}/{name}" for name in _introspect_nodes(arguments[0])]

    def _get_all(self, path: str, interface: str) -> dict | None:
        """``org.freedesktop.DBus.Properties.GetAll``, or None."""
        handle = self._interface(path, IFACE_PROPERTIES)
        if handle is None:
            return None
        try:
            message = handle.call("GetAll", interface)
            arguments = message.arguments()
        except Exception:
            return None
        if not arguments or not isinstance(arguments[0], dict):
            return None
        return {str(key): _unwrap(value) for key, value in arguments[0].items()}

    def properties(self, path: str, interface: str, refresh: bool = False) -> dict:
        """Cached properties of one interface on one object. Empty when absent.

        The cache is what keeps enumeration to one round trip per interface.
        The volatile pass passes ``refresh`` so that a temperature read on the
        timer is a fresh one rather than the value from launch.
        """
        key = (path, interface)
        if refresh or key not in self._properties:
            fetched = self._get_all(path, interface)
            if fetched is None and key in self._properties:
                return self._properties[key]
            self._properties[key] = fetched or {}
        return self._properties[key]

    def has_interface(self, path: str, interface: str) -> bool:
        return bool(self.properties(path, interface))

    # -- the object graph -------------------------------------------------

    @property
    def drive_paths(self) -> list[str]:
        return list(self._drive_paths)

    @property
    def block_paths(self) -> list[str]:
        return list(self._block_paths)

    def block_for_device(self, device: str) -> str | None:
        """The object path of the block device at ``/dev/sda1``, or None.

        Resolved by asking each block object what device node it is, rather
        than by guessing the object path from the name: udisks2 escapes names
        in a way that is easy to get subtly wrong, and a wrong path here would
        act on the wrong device.
        """
        for path in self._block_paths:
            block = self.properties(path, IFACE_BLOCK)
            for key in ("Device", "PreferredDevice"):
                if _decode_bytes(block.get(key)) == device:
                    return path
        return None

    def blocks_for_drive(self, drive_path: str) -> list[str]:
        out = []
        for path in self._block_paths:
            block = self.properties(path, IFACE_BLOCK)
            if _object_path(block.get("Drive")) == drive_path:
                out.append(path)
        return out

    def smart_attributes(self, drive_path: str) -> tuple[list, str]:
        """The per-attribute SMART table, and a note when there is not one.

        ATA answers with an array of structures, which this binding hands back
        as a container it cannot unpack; NVMe answers with a flat dictionary,
        which it can. Both are attempted, and neither failing is an error worth
        showing - the headline SMART values come from the interface properties
        either way.
        """
        if not self.available:
            return [], ""
        if self.has_interface(drive_path, IFACE_NVME):
            handle = self._interface(drive_path, IFACE_NVME)
            if handle is None:
                return [], ""
            try:
                message = handle.call("SmartGetAttributes", {})
                arguments = message.arguments()
            except Exception:
                return [], ""
            if arguments and isinstance(arguments[0], dict):
                return sorted(
                    (str(key), _unwrap(value)) for key, value in arguments[0].items()
                ), ""
            return [], ""

        if self.has_interface(drive_path, IFACE_ATA):
            handle = self._interface(drive_path, IFACE_ATA)
            if handle is None:
                return [], ""
            try:
                message = handle.call("SmartGetAttributes", {})
                arguments = message.arguments()
            except Exception:
                return [], ""
            if arguments and isinstance(arguments[0], list):
                return _ata_attribute_rows(arguments[0]), ""
            return (
                [],
                "The per-attribute table is not shown: udisks2 returns it in a form "
                "this Qt binding cannot unpack. The values above come from the same "
                "SMART data.",
            )
        return [], ""


# -- demarshalling helpers -------------------------------------------------


def _introspect_nodes(xml: str) -> list[str]:
    """Child node names from an Introspect reply."""
    try:
        from xml.dom import minidom

        document = minidom.parseString(xml)
    except Exception:
        return []
    names = []
    root = document.documentElement
    if root is None:
        return []
    for node in root.childNodes:
        if getattr(node, "tagName", None) != "node":
            continue
        name = node.getAttribute("name")
        if name:
            names.append(name)
    return names


def _unwrap(value):
    """Turn a D-Bus value into something ordinary Python code can use."""
    try:
        from PySide6.QtDBus import QDBusObjectPath, QDBusVariant

        if isinstance(value, QDBusVariant):
            return _unwrap(value.variant())
        if isinstance(value, QDBusObjectPath):
            return value.path()
    except ImportError:
        pass
    try:
        from PySide6.QtCore import QByteArray

        if isinstance(value, QByteArray):
            return bytes(value)
    except ImportError:
        pass
    if isinstance(value, (list, tuple)):
        return [_unwrap(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _unwrap(item) for key, item in value.items()}
    return value


def _decode_bytes(value) -> str | None:
    """udisks2 sends device nodes and labels as NUL-terminated byte arrays."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.rstrip("\x00") or None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).split(b"\x00")[0].decode("utf-8", "replace") or None
    if isinstance(value, list):
        try:
            return bytes(bytearray(int(item) & 0xFF for item in value)).split(b"\x00")[
                0
            ].decode("utf-8", "replace") or None
        except (TypeError, ValueError):
            return None
    return None


def _decode_byte_array_list(value) -> list[str]:
    """``aay`` - the mount points list - into plain strings."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _decode_bytes(item)
        if text:
            out.append(text)
    return out


def _object_path(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    path = getattr(value, "path", None)
    return path() if callable(path) else None


def _smart_was_read(properties: dict) -> bool:
    """Whether udisks2 has ever actually read this drive's SMART data.

    ``SmartUpdated`` is seconds since the epoch, or 0 for never. It is 0 on any
    machine where the daemon has not been asked - which is the ordinary case
    for a drive nothing has touched since boot - and every other Smart* property
    is then its type's default rather than a reading.
    """
    updated = properties.get("SmartUpdated")
    return isinstance(updated, (int, float)) and updated > 0


def _ata_attribute_rows(entries) -> list:
    """``a(ysqiiixia{sv})`` when the binding does unpack it."""
    out = []
    for entry in entries:
        try:
            identifier, name, _flags, value, worst, threshold, pretty, unit = entry[:8]
        except (TypeError, ValueError, IndexError):
            continue
        out.append(
            (
                f"{int(identifier)} {name}",
                f"value {value}, worst {worst}, threshold {threshold}, raw {pretty}",
            )
        )
    return out


# A single client per enumeration pass. Both disks.py and volumes.py want the
# same object graph, and asking the bus twice for it would double the work for
# no benefit. invalidate() is called before each full enumeration.
_client: Udisks2 | None = None
# Set when a client was installed by hand rather than built from the bus. A
# pinned client survives invalidate(), because otherwise the first
# re-enumeration would throw away the captured data and fall straight back to
# "no system bus" - which would make the seam useless for exactly the thing it
# exists for.
_pinned = False


def client() -> Udisks2:
    global _client
    if _client is None:
        _client = Udisks2.connect()
    return _client


def invalidate() -> None:
    """Drop the cached client, so the next enumeration asks the bus again.

    Hardware can appear and disappear between enumerations, so the snapshot is
    rebuilt rather than reused. A pinned client is left alone.
    """
    global _client
    if _pinned:
        return
    _client = None


def set_client(replacement: Udisks2 | None) -> None:
    """Install a client built from captured data. Used for testing only."""
    global _client, _pinned
    _client = replacement
    _pinned = replacement is not None


# ==========================================================================
# The probe
# ==========================================================================


class DisksProbe(Probe):
    branch = "storage"
    id = "disks"
    label = "Disks"

    def __init__(self, context=None):
        super().__init__(context)
        self._disks: list[dict] = []

    # -- enumeration ------------------------------------------------------

    def _find_disks(self) -> list[dict]:
        disks = []
        for path in list_dir(BLOCK_ROOT):
            # A partition carries a `partition` file; volumes.py owns those.
            if path_exists(path / "partition"):
                continue
            name = path.name
            if name.startswith(("loop", "ram", "zram")):
                continue
            device = path / "device"
            disks.append(
                {
                    "name": name,
                    "path": path,
                    "device": device,
                    "node": f"/dev/{name}",
                    "sectors": read_int(path / "size"),
                    "rotational": read_int(path / "queue/rotational"),
                    "removable": read_int(path / "removable"),
                }
            )
        return disks

    def sections(self) -> list[Section]:
        disks = self._find_disks()
        self._disks = disks
        if not disks:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No physical block devices were found under /sys/class/block. "
                    "A machine booted entirely from network or memory looks like this.",
                )
            ]
        return [self._disk_section(disk) for disk in disks]

    def _disk_section(self, disk) -> Section:
        rotational = disk["rotational"] == 1
        section = Section(
            id=disk["name"],
            label=disk["name"],
            icon="disk_rotational" if rotational else "disk_solid",
        )

        for row in self._identity_rows(disk):
            section.add(row)
        for row in self._geometry_rows(disk):
            section.add(row)
        for row in self._queue_rows(disk):
            section.add(row)
        for row in self._smart_rows(disk):
            section.add(row)
        for row in self._partition_rows(disk):
            section.add(row)
        return section

    def _identity_rows(self, disk) -> list:
        rows = []
        device = disk["device"]
        model = read_first_line(device / "model")
        vendor = read_first_line(device / "vendor")
        nvme = self._nvme_root(disk)

        if model is None and nvme is not None:
            model = read_first_line(nvme / "model")
        if model is None:
            model = read_first_line(disk["path"] / "device/model")

        rows.append(self.row("model", "Model", or_missing(model, NOT_REPORTED)))
        if vendor:
            rows.append(self.row("vendor", "Vendor", vendor))

        serial = read_first_line(device / "serial")
        if serial is None and nvme is not None:
            serial = read_first_line(nvme / "serial")
        rows.append(self.row("serial", "Serial number", or_missing(serial, NOT_REPORTED)))

        firmware = read_first_line(device / "firmware_rev")
        if firmware is None and nvme is not None:
            firmware = read_first_line(nvme / "firmware_rev")
        rows.append(
            self.row("firmware", "Firmware revision", or_missing(firmware, NOT_REPORTED))
        )
        rows.append(self.row("node", "Device node", disk["node"]))

        if nvme is not None:
            for field, label, name in (
                ("nvme_transport", "Transport", "transport"),
                ("nvme_state", "Controller state", "state"),
                ("nvme_numa", "NUMA node", "numa_node"),
                ("nvme_address", "Controller address", "address"),
            ):
                value = read_first_line(nvme / name)
                if value:
                    rows.append(self.row(field, label, value))
        return rows

    def _nvme_root(self, disk):
        """The NVMe controller directory behind this block device, if any.

        A namespace is named ``nvme0n1``: controller ``nvme0``, namespace 1.
        The controller is everything up to the ``n`` that starts the namespace.
        """
        name = disk["name"]
        if not name.startswith("nvme"):
            return None
        match = _NVME_NAME.match(name)
        if match is None:
            return None
        candidate = f"/sys/class/nvme/{match.group('controller')}"
        return resolve(candidate) if path_exists(candidate) else None

    def _geometry_rows(self, disk) -> list:
        rows = []
        sectors = disk["sectors"]
        size = sectors * SECTOR_BYTES if sectors is not None else None
        rows.append(
            self.row(
                "capacity",
                "Capacity",
                f"{fmt_bytes(size, binary=False)} ({fmt_bytes(size)})"
                if size
                else NOT_AVAILABLE,
            )
        )
        rows.append(
            self.row(
                "kind",
                "Kind",
                {1: "Rotational — a mechanical hard disk", 0: "Solid state"}.get(
                    disk["rotational"], NOT_REPORTED
                ),
            )
        )
        rows.append(
            self.row(
                "removable",
                "Removable",
                {1: "Yes", 0: "No"}.get(disk["removable"], NOT_REPORTED),
            )
        )
        logical = read_int(disk["path"] / "queue/logical_block_size")
        physical = read_int(disk["path"] / "queue/physical_block_size")
        rows.append(
            self.row(
                "block_size",
                "Block size",
                f"{logical} B logical, {physical} B physical"
                if logical and physical
                else NOT_AVAILABLE,
                severity=WARNING
                if logical and physical and physical > logical and physical >= 4096 and logical < 4096
                else "normal",
            )
        )
        if sectors is not None:
            rows.append(
                self.row(
                    "sectors",
                    "Sectors",
                    f"{fmt_int(sectors)} of {SECTOR_BYTES} bytes as the kernel counts them",
                )
            )
        return rows

    def _queue_rows(self, disk) -> list:
        rows = []
        scheduler = read_text(disk["path"] / "queue/scheduler")
        if scheduler:
            active = None
            for token in scheduler.split():
                if token.startswith("[") and token.endswith("]"):
                    active = token[1:-1]
            rows.append(
                self.row(
                    "scheduler",
                    "I/O scheduler",
                    f"{active} — of {scheduler.replace('[', '').replace(']', '')}"
                    if active
                    else scheduler,
                )
            )
        discard = read_int(disk["path"] / "queue/discard_max_bytes")
        if discard is not None:
            rows.append(
                self.row(
                    "discard",
                    "Discard support",
                    f"Up to {fmt_bytes(discard)} per request"
                    if discard
                    else "Not supported — this device cannot be told which blocks are unused",
                )
            )
        write_cache = read_first_line(disk["path"] / "queue/write_cache")
        if write_cache:
            rows.append(self.row("write_cache", "Write cache", write_cache))
        depth = read_int(disk["path"] / "device/queue_depth")
        if depth is not None:
            rows.append(self.row("queue_depth", "Queue depth", str(depth)))
        return rows

    # -- SMART ------------------------------------------------------------

    def _drive_path(self, disk) -> str | None:
        udisks = client()
        if not udisks.available:
            return None
        block_path = udisks.block_for_device(disk["node"])
        if block_path is None:
            return None
        block = udisks.properties(block_path, IFACE_BLOCK)
        return _object_path(block.get("Drive"))

    def _smart_rows(self, disk) -> list:
        udisks = client()
        if not udisks.available:
            return [self.row("smart", "SMART health", udisks.reason)]

        drive_path = self._drive_path(disk)
        if drive_path is None:
            return [
                self.row(
                    "smart",
                    "SMART health",
                    "udisks2 does not have a drive record for this device, so no "
                    "SMART data is available for it.",
                )
            ]

        if udisks.has_interface(drive_path, IFACE_NVME):
            return self._nvme_smart_rows(udisks, drive_path)
        if udisks.has_interface(drive_path, IFACE_ATA):
            return self._ata_smart_rows(udisks, drive_path)
        return [
            self.row(
                "smart",
                "SMART health",
                "This drive reports neither an ATA nor an NVMe interface to udisks2, "
                "so it has no SMART data to give.",
            )
        ]

    def _ata_smart_rows(self, udisks, drive_path) -> list:
        ata = udisks.properties(drive_path, IFACE_ATA)
        rows = []
        supported = ata.get("SmartSupported")
        enabled = ata.get("SmartEnabled")
        if supported is False:
            return [
                self.row(
                    "smart",
                    "SMART health",
                    "This drive does not support SMART.",
                )
            ]
        if enabled is False:
            rows.append(
                self.row(
                    "smart",
                    "SMART health",
                    "SMART is supported but switched off on this drive, so nothing "
                    "is being recorded.",
                    severity=WARNING,
                )
            )
        elif not _smart_was_read(ata):
            # SmartUpdated is 0 until udisks2 has actually read the drive's
            # SMART data. SmartFailing is then a default False, and printing
            # "OK" from it would be an assurance about a drive nobody asked.
            rows.append(
                self.row(
                    "smart",
                    "SMART health",
                    "SMART is supported and switched on, but udisks2 has not read "
                    "this drive's SMART data, so there is no health result to show.",
                )
            )
        else:
            failing = ata.get("SmartFailing")
            rows.append(
                self.row(
                    "smart",
                    "SMART health",
                    "Failing — this drive expects to fail"
                    if failing
                    else "OK — the drive reports no impending failure",
                    severity=DANGER if failing else "normal",
                )
            )

        if not _smart_was_read(ata):
            # Every remaining value below is a zero the daemon has never
            # replaced. "0 reallocated sectors" reads as a measurement, so
            # none of them are shown.
            return rows

        seconds = ata.get("SmartPowerOnSeconds")
        if isinstance(seconds, (int, float)) and seconds:
            rows.append(
                self.row(
                    "power_on",
                    "Powered on for",
                    f"{fmt_duration(seconds)} — {int(seconds / 3600):,} hours",
                )
            )
        rows.append(self._temperature_row(ata.get("SmartTemperature")))

        bad = ata.get("SmartNumBadSectors")
        if isinstance(bad, (int, float)):
            rows.append(
                self.row(
                    "bad_sectors",
                    "Reallocated or pending sectors",
                    str(int(bad)),
                    severity=WARNING if bad else "normal",
                    tier=VOLATILE,
                )
            )
        failing_now = ata.get("SmartNumAttributesFailing")
        past = ata.get("SmartNumAttributesFailedInThePast")
        if isinstance(failing_now, (int, float)) or isinstance(past, (int, float)):
            rows.append(
                self.row(
                    "smart_attributes_failing",
                    "Attributes past their threshold",
                    f"{int(failing_now or 0)} now, {int(past or 0)} at some point in the past",
                    severity=WARNING if (failing_now or past) else "normal",
                )
            )
        selftest = ata.get("SmartSelftestStatus")
        if selftest:
            rows.append(self.row("selftest", "Last self test", str(selftest)))

        rows.extend(self._attribute_rows(udisks, drive_path))
        return rows

    def _nvme_smart_rows(self, udisks, drive_path) -> list:
        nvme = udisks.properties(drive_path, IFACE_NVME)
        rows = []
        warning = nvme.get("SmartCriticalWarning")
        warnings = [str(item) for item in warning] if isinstance(warning, list) else []
        if not _smart_was_read(nvme):
            # As above: an empty warning list on a controller that was never
            # read is silence, not a clean bill of health.
            rows.append(
                self.row(
                    "smart",
                    "SMART health",
                    "udisks2 has not read this controller's SMART log, so there is "
                    "no health result to show.",
                )
            )
        else:
            rows.append(
                self.row(
                    "smart",
                    "SMART health",
                    "OK — the controller reports no critical warning"
                    if not warnings
                    else "Critical warning: " + ", ".join(warnings),
                    severity=DANGER if warnings else "normal",
                )
            )
        hours = nvme.get("SmartPowerOnHours") if _smart_was_read(nvme) else None
        if isinstance(hours, (int, float)) and hours:
            rows.append(
                self.row(
                    "power_on",
                    "Powered on for",
                    f"{fmt_duration(hours * 3600)} — {int(hours):,} hours",
                )
            )
        rows.append(
            self._temperature_row(
                nvme.get("SmartTemperature") if _smart_was_read(nvme) else None
            )
        )

        # `State` is deliberately not here. The identity block above already
        # emits a "Controller state" row read straight from sysfs, under the
        # same field id, and two rows sharing an id inside one section collide
        # in the volatile pass as well as reading as a duplicate on screen.
        for key, field, label in (
            ("NVMeRevision", "nvme_revision", "NVMe revision"),
            ("ControllerID", "nvme_controller_id", "Controller id"),
        ):
            value = nvme.get(key)
            if value not in (None, ""):
                rows.append(self.row(field, label, str(value)))

        unallocated = nvme.get("UnallocatedCapacity")
        if isinstance(unallocated, (int, float)) and unallocated:
            rows.append(
                self.row(
                    "nvme_unallocated", "Unallocated capacity", fmt_bytes(unallocated)
                )
            )
        rows.extend(self._attribute_rows(udisks, drive_path))
        return rows

    def _temperature_row(self, kelvin):
        """udisks2 reports SMART temperature in kelvin, as a float."""
        if not isinstance(kelvin, (int, float)) or not kelvin:
            return self.row("temperature", "Temperature", NOT_REPORTED, tier=VOLATILE)
        celsius = kelvin - 273.15
        return self.row(
            "temperature",
            "Temperature",
            f"{celsius:.0f} °C",
            tier=VOLATILE,
            severity=WARNING if celsius >= 60 else "normal",
        )

    def _attribute_rows(self, udisks, drive_path) -> list:
        attributes, note = udisks.smart_attributes(drive_path)
        rows = []
        if note:
            rows.append(self.row("smart_table", "Attribute table", note))
        for name, value in attributes:
            rows.append(
                self.row(
                    "smart_attribute",
                    f"  {name}",
                    str(value),
                    key=f"attr{name}",
                )
            )
        return rows

    # -- partitions -------------------------------------------------------

    def _partition_rows(self, disk) -> list:
        partitions = _partitions_of(disk["name"])
        rows = [
            self.row(
                "partition_count",
                "Partitions",
                str(len(partitions)) if partitions else "None — this device is not partitioned",
            )
        ]
        udisks = client()
        if udisks.available:
            block_path = udisks.block_for_device(disk["node"])
            if block_path:
                partition_table = udisks.properties(
                    block_path, "org.freedesktop.UDisks2.PartitionTable"
                )
                kind = partition_table.get("Type")
                if kind:
                    rows.append(
                        self.row(
                            "partition_table",
                            "Partition table",
                            {"gpt": "GPT", "dos": "MBR (msdos)"}.get(str(kind), str(kind)),
                        )
                    )
        for path in partitions:
            sectors = read_int(path / "size")
            size = sectors * SECTOR_BYTES if sectors is not None else None
            rows.append(
                self.row(
                    "partition",
                    f"  /dev/{path.name}",
                    fmt_bytes(size, binary=False) if size else NOT_AVAILABLE,
                    key=f"part{path.name}",
                )
            )
        return rows

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        out: dict[str, list] = {}
        udisks = client()
        if not udisks.available:
            return out
        for disk in self._disks or self._find_disks():
            drive_path = self._drive_path(disk)
            if drive_path is None:
                continue
            rows = []
            if udisks.has_interface(drive_path, IFACE_NVME):
                nvme = udisks.properties(drive_path, IFACE_NVME, refresh=True)
                if _smart_was_read(nvme):
                    rows.append(self._temperature_row(nvme.get("SmartTemperature")))
            elif udisks.has_interface(drive_path, IFACE_ATA):
                ata = udisks.properties(drive_path, IFACE_ATA, refresh=True)
                if _smart_was_read(ata):
                    rows.append(self._temperature_row(ata.get("SmartTemperature")))
            if rows:
                out[disk["name"]] = rows
        return out
