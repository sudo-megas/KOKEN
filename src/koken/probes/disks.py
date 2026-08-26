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
Using it is what lets this application avoid a privileged SMART path of its
own, and it is where every SMART row in this file comes from.

The headline values stay here - the health verdict, temperature, power-on
hours, bad sectors, the self-test result - because somebody looking at a drive
should see at a glance whether it is failing. The per-attribute table is a
different thing: thirty rows that would bury everything else in this section.
It lives in :mod:`koken.probes.smart`, which is the Storage branch's third row
2 tab, and which is the only place attributes are rendered.

The D-Bus layer below is shared with :mod:`koken.probes.volumes`, which needs
the same object graph to find each partition's filesystem and its object path.
It is written on the assumption that udisks2 is simply not there: no service, no
system bus, or a bus that answers with an error. Every one of those cases lands
in the same place a missing sysfs file lands - a value of None and a row that
says so - with no traceback and nothing on the console.

Enumeration is shaped entirely by one property of this Qt binding: it can only
hand back D-Bus values that Qt maps to an ordinary type. Anything Qt maps to
``QDBusArgument`` - every dictionary, every array of arrays, every array of
structures - arrives as an opaque object whose contents PySide6 cannot read at
all, and whose extraction operators abort the process on a type mismatch. That
rules out all three of the obvious routes: ``GetManagedObjects`` (``a{oa{sa{sv}}}``),
``Manager.GetBlockDevices`` (``ao``), and ``Properties.GetAll`` (``a{sv}``).

What is left, and what this file uses, is:

* ``Introspectable.Introspect``, which returns ``s`` - a string - and gives both
  the child objects of a path and the interfaces and property names of an
  object. This is the enumeration.
* ``Properties.Get``, which returns ``v`` and unwraps correctly for every basic
  type and for ``ay``. This is one round trip per property, so only the
  properties this application actually shows are asked for.

Both happen on enumeration, and the volatile pass asks for one property per
drive. Properties whose type this binding cannot demarshal are never requested,
which is also what keeps Qt from writing "must be registered with Qt D-Bus"
onto the console.
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
IFACE_PARTITION_TABLE = "org.freedesktop.UDisks2.PartitionTable"
IFACE_PROPERTIES = "org.freedesktop.DBus.Properties"
IFACE_INTROSPECTABLE = "org.freedesktop.DBus.Introspectable"

# A blocking read is capped well below libdbus's 25 second default. udisks2
# answers property reads out of its own cache in well under a millisecond, so
# anything approaching this is a daemon that has stopped answering, and waiting
# out the default there would freeze the interface for half a minute.
READ_TIMEOUT_MS = 5000

# The D-Bus null object path. udisks2 writes it into Drive, MDRaid,
# CryptoBackingDevice and Table to mean "there is no such object", and reading
# it back as a path would send later lookups to an object that does not exist.
NULL_OBJECT_PATH = "/"

# Type codes this binding demarshals into something ordinary Python can read.
# Everything absent from here - a{sv}, aay, ao, a(sa{sv}) - arrives as an
# opaque QDBusArgument, so those properties are never asked for.
_READABLE_SIGNATURES = frozenset(
    ("y", "b", "n", "q", "i", "u", "x", "t", "d", "s", "o", "g", "ay", "as")
)

# The two properties that say which device node a block object is.
_DEVICE_KEYS = ("Device", "PreferredDevice")

# The properties each interface is asked for, which is exactly the set this
# application displays. Anything added to a row here has to be added to this
# map as well, because a property that is not listed is never fetched.
WANTED_PROPERTIES = {
    IFACE_BLOCK: _DEVICE_KEYS + ("Drive", "IdType", "IdLabel", "IdUUID"),
    IFACE_PARTITION: ("Name", "Type", "UUID", "Offset", "Flags"),
    IFACE_PARTITION_TABLE: ("Type",),
    IFACE_FILESYSTEM: (),
    IFACE_SWAP: (),
    IFACE_DRIVE: ("Model", "Serial", "Size"),
    IFACE_ATA: (
        "SmartSupported",
        "SmartEnabled",
        "SmartUpdated",
        "SmartFailing",
        "SmartPowerOnSeconds",
        "SmartTemperature",
        "SmartNumBadSectors",
        "SmartNumAttributesFailing",
        "SmartNumAttributesFailedInThePast",
        "SmartSelftestStatus",
    ),
    IFACE_NVME: (
        "SmartUpdated",
        "SmartCriticalWarning",
        "SmartPowerOnHours",
        "SmartTemperature",
        "NVMeRevision",
        "ControllerID",
        "UnallocatedCapacity",
    ),
}

# The one property the volatile pass re-reads, per interface. The timer must
# not cost a round trip for every property of every drive.
VOLATILE_PROPERTIES = {
    IFACE_ATA: ("SmartUpdated", "SmartTemperature"),
    IFACE_NVME: ("SmartUpdated", "SmartTemperature"),
}

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

    def __init__(
        self, available: bool = False, reason: str = "", detail: str = ""
    ) -> None:
        self.available = available
        self.reason = reason
        # The raw D-Bus error behind `reason`, when there was one. Kept apart
        # from it because a row value is one line that elides, and an error
        # name is the part somebody reporting a fault needs to be able to
        # read: it goes in the row's expansion, where there is room for it.
        self.detail = detail
        self._bus = None
        self._properties: dict[tuple[str, str], dict] = {}
        self._drive_paths: list[str] = []
        self._block_paths: list[str] = []
        # path -> (child names, {interface: {property: signature}}), from one
        # Introspect call per object. This is the whole object graph: which
        # objects exist, what each of them implements, and what may be read
        # from it.
        self._nodes: dict[str, tuple] = {}
        # The name and message of the last D-Bus error, so a failure can say
        # what actually went wrong instead of guessing.
        self._last_error: tuple[str, str] = ("", "")

    # -- construction -----------------------------------------------------

    @classmethod
    def unavailable(cls, reason: str, detail: str = "") -> "Udisks2":
        return cls(available=False, reason=reason, detail=detail)

    @property
    def full_reason(self) -> str:
        """Reason and detail as one paragraph, for somewhere that wraps."""
        return f"{self.reason} {self.detail}".strip()

    @classmethod
    def connect(cls, bus=None) -> "Udisks2":
        """Build a client from the bus, or say in one sentence why not.

        A client is only ever returned as available with a populated object
        graph. An empty graph used to be returned as available, and the result
        was three different rows each blaming something else: a drive with no
        SMART record, a partition with no filesystem, and a mount control that
        said udisks2 was missing - all for the one cause, and none of them
        saying it.

        *bus* is the system bus unless one is handed in, which is how this can
        be exercised against a service on a session bus.
        """
        if bus is None:
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
                    "cannot be reached. SMART data and the mount controls are "
                    "unavailable."
                )

        client = cls(available=True)
        client._bus = bus
        block_paths = client._children(BLOCK_PATH)
        if not block_paths:
            return cls.unavailable(*client._enumeration_failure())
        client._block_paths = block_paths
        # A machine can have block devices and no drives - every one of them
        # loop, device-mapper or a virtio disk udisks2 gives no drive record.
        # That is not a failure, and it is not a reason to throw away the block
        # objects the mount controls need.
        client._drive_paths = client._children(DRIVES_PATH)
        return client

    def _enumeration_failure(self) -> tuple[str, str]:
        """Why the object graph came back empty: a sentence, and the raw error."""
        name, message = self._last_error
        tail = "SMART data and the mount controls are unavailable."
        if not name:
            return (
                "udisks2 answered on the system bus but lists no block devices at "
                f"all, so there is nothing to match this machine's disks against. "
                f"{tail}",
                f"{BLOCK_PATH} has no objects under it, which on a machine with "
                "disks in it means the daemon has not finished starting or has "
                "lost its view of them.",
            )
        detail = (
            f"The system bus answered the request to list {BLOCK_PATH} with "
            f"{name}" + (f": {message}" if message else ".")
        )
        if name.endswith(("ServiceUnknown", "NameHasNoOwner")):
            return (
                f"udisks2 is not running: nothing on the system bus owns {SERVICE}. "
                f"It is probably not installed, or its service is stopped. {tail}",
                detail,
            )
        if name.endswith(("NoReply", "Timeout", "TimedOut")):
            return (
                f"udisks2 did not answer within {READ_TIMEOUT_MS // 1000} seconds, "
                f"so its list of block devices could not be read. {tail}",
                detail,
            )
        return (
            f"udisks2 would not list its block devices, so none of this machine's "
            f"disks could be matched to one. {tail}",
            detail,
        )

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

    def _call(self, path: str, interface: str, method: str, arguments=()):
        """One blocking call. The reply message, or None with the error kept.

        Built as a plain message rather than through ``QDBusInterface``, which
        introspects the object again on construction to build a metaobject this
        code never uses, and which writes a warning to the console when a
        property's type has no Qt equivalent.
        """
        if self._bus is None:
            return None
        try:
            from PySide6.QtDBus import QDBus, QDBusMessage

            message = QDBusMessage.createMethodCall(SERVICE, path, interface, method)
            if arguments:
                message.setArguments(list(arguments))
            reply = self._bus.call(message, QDBus.CallMode.Block, READ_TIMEOUT_MS)
        except Exception as error:  # a binding fault must not reach the screen
            self._last_error = (type(error).__name__, str(error))
            return None
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            self._last_error = (reply.errorName() or "", reply.errorMessage() or "")
            return None
        return reply

    def _introspect(self, path: str) -> tuple:
        """``(child names, {interface: {property: signature}})`` for one object.

        One round trip, cached, and the only thing that establishes what an
        object is: udisks2 puts Block, Partition, Filesystem and Swapspace on
        the same object, and this is what says which of them are there.
        """
        cached = self._nodes.get(path)
        if cached is not None:
            return cached
        reply = self._call(path, IFACE_INTROSPECTABLE, "Introspect")
        parsed: tuple = ([], {})
        if reply is not None:
            arguments = reply.arguments()
            if arguments and isinstance(arguments[0], str):
                parsed = _parse_introspection(arguments[0])
        self._nodes[path] = parsed
        return parsed

    def _children(self, path: str) -> list[str]:
        """Object paths one level under *path*, via Introspect."""
        return [f"{path}/{name}" for name in self._introspect(path)[0]]

    def _get(self, path: str, interface: str, name: str):
        """``org.freedesktop.DBus.Properties.Get``, unwrapped, or None."""
        reply = self._call(path, IFACE_PROPERTIES, "Get", (interface, name))
        if reply is None:
            return None
        arguments = reply.arguments()
        return _unwrap(arguments[0]) if arguments else None

    def _fetch(self, path: str, interface: str, names=None) -> dict | None:
        """Read *names* of *interface*, one property per round trip.

        None means the object does not carry the interface at all, which is a
        different thing from carrying it with nothing readable on it: a
        Filesystem interface has only ``MountPoints`` and ``Size`` on it, and
        the first of those is an ``aay`` this binding cannot demarshal.
        """
        declared = self._introspect(path)[1].get(interface)
        if declared is None:
            return None
        if names is None:
            names = WANTED_PROPERTIES.get(interface, tuple(declared))
        out = {}
        for name in names:
            signature = declared.get(name)
            if signature is None and declared:
                # This object declares the interface's properties, and this is
                # not one of them: an older udisks2 than the one this was
                # written against. Asking anyway would only earn an error.
                continue
            if signature is not None and signature not in _READABLE_SIGNATURES:
                continue
            # With no declaration to go on the property is asked for blind, and
            # an answer that comes back as an unreadable container is dropped
            # the same way a declared one would have been skipped.
            value = self._get(path, interface, name)
            if value is not None and not _is_opaque(value):
                out[name] = value
        return out

    def properties(self, path: str, interface: str, refresh: bool = False) -> dict:
        """Cached properties of one interface on one object. Empty when absent.

        The cache is what keeps enumeration to one pass over the properties
        this application shows. The volatile pass passes ``refresh`` so that a
        temperature read on the timer is a fresh one rather than the value from
        launch, and re-reads only the properties that can have changed.
        """
        key = (path, interface)
        if not refresh and key in self._properties:
            return self._properties[key]
        fetched = self._fetch(
            path, interface, VOLATILE_PROPERTIES.get(interface) if refresh else None
        )
        known = self._properties.get(key)
        if fetched is None:
            # The object does not carry the interface. A previous answer, if
            # there is one, is better than replacing it with nothing.
            self._properties[key] = known if known is not None else {}
        elif known:
            # A refresh reads a subset, so it updates what is held rather than
            # replacing it and dropping every value it did not ask for.
            self._properties[key] = {**known, **fetched}
        else:
            self._properties[key] = fetched
        return self._properties[key]

    def has_interface(self, path: str, interface: str) -> bool:
        """Whether the object at *path* carries *interface*.

        Introspection is the authority where there is a bus, because an
        interface can be present with nothing on it this binding can read. A
        client built from a snapshot has no introspection data and falls back
        to the captured properties.
        """
        if self._bus is not None:
            return interface in self._introspect(path)[1]
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
            for key in _DEVICE_KEYS:
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

    def smart_attributes(self, drive_path: str) -> list[dict]:
        """The per-attribute SMART table, if this binding can read it.

        Asked for first, and on every machine so far it comes back empty:
        ``SmartGetAttributes`` returns ``a(ysqiiixia{sv})`` for ATA and
        ``a{sv}`` for NVMe, Qt maps both to ``QDBusArgument``, and this binding
        cannot read one. The call is still made rather than assumed away,
        because it is the route with no extra dependency behind it and it costs
        one round trip to find out. :mod:`koken.probes.smart` renders whatever
        this returns and falls back to smartctl when it returns nothing.

        The entries are shaped exactly like the helper's, so there is one
        renderer rather than two.
        """
        if not self.available:
            return []
        if self.has_interface(drive_path, IFACE_NVME):
            reply = self._call(drive_path, IFACE_NVME, "SmartGetAttributes", ({},))
            arguments = reply.arguments() if reply is not None else []
            if arguments and isinstance(arguments[0], dict):
                return _nvme_attribute_entries(arguments[0])
            return []

        if self.has_interface(drive_path, IFACE_ATA):
            reply = self._call(drive_path, IFACE_ATA, "SmartGetAttributes", ({},))
            arguments = reply.arguments() if reply is not None else []
            if arguments and isinstance(arguments[0], list):
                return _ata_attribute_entries(arguments[0])
        return []


# -- demarshalling helpers -------------------------------------------------


def _parse_introspection(xml: str) -> tuple:
    """An Introspect reply into ``(child names, {interface: {property: type}})``.

    Both halves come out of the one document because both are needed and the
    reply carries both. A container path such as ``/org/freedesktop/UDisks2/
    block_devices`` is not an object at all - the daemon answers for it with a
    bare list of children and no interfaces whatsoever - so an empty interface
    map is an ordinary result and not a failure.
    """
    try:
        from xml.dom import minidom

        document = minidom.parseString(xml)
    except Exception:
        return [], {}
    root = document.documentElement
    if root is None:
        return [], {}

    names = []
    interfaces: dict[str, dict[str, str]] = {}
    for node in root.childNodes:
        tag = getattr(node, "tagName", None)
        if tag == "node":
            name = node.getAttribute("name")
            if name:
                names.append(name)
        elif tag == "interface":
            name = node.getAttribute("name")
            if not name:
                continue
            declared: dict[str, str] = {}
            for child in node.childNodes:
                if getattr(child, "tagName", None) != "property":
                    continue
                if "read" not in (child.getAttribute("access") or "read"):
                    continue
                property_name = child.getAttribute("name")
                if property_name:
                    declared[property_name] = child.getAttribute("type")
            interfaces[name] = declared
    return names, interfaces



def _is_opaque(value) -> bool:
    """Whether this is a container the binding hands back without its contents.

    A ``QDBusArgument`` is what arrives for every D-Bus type Qt has no ordinary
    equivalent for. There is no way to read one from Python - its extraction
    operators are bound in a form that returns nothing, and handing one the
    wrong type aborts the process - so one that reaches here is dropped.
    """
    try:
        from PySide6.QtDBus import QDBusArgument
    except ImportError:
        return False
    return isinstance(value, QDBusArgument)


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
    """An object path property as a string, with the null path read as None.

    udisks2 writes ``/`` into Drive, MDRaid and CryptoBackingDevice to mean
    there is no such object. It is a valid path and a truthy string, so taking
    it at face value would send every later lookup to an object that does not
    exist and get an empty answer back with nothing saying why.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        path = getattr(value, "path", None)
        value = path() if callable(path) else None
    if not value or value == NULL_OBJECT_PATH:
        return None
    return value


def _smart_was_read(properties: dict) -> bool:
    """Whether udisks2 has ever actually read this drive's SMART data.

    ``SmartUpdated`` is seconds since the epoch, or 0 for never. It is 0 on any
    machine where the daemon has not been asked - which is the ordinary case
    for a drive nothing has touched since boot - and every other Smart* property
    is then its type's default rather than a reading.
    """
    updated = properties.get("SmartUpdated")
    return isinstance(updated, (int, float)) and updated > 0


def _ata_attribute_entries(entries) -> list[dict]:
    """``a(ysqiiixia{sv})`` when the binding does unpack it.

    The tuple is id, name, flags, value, worst, threshold, pretty, pretty_unit,
    expansion. udisks2's `pretty` is the raw value already decoded per vendor,
    which is the same thing smartctl's ``raw.string`` carries, so both sources
    fill the same field here.
    """
    out = []
    for entry in entries:
        try:
            identifier, name, _flags, value, worst, threshold, pretty, _unit = entry[:8]
        except (TypeError, ValueError, IndexError):
            continue
        out.append(
            {
                "id": int(identifier),
                "name": str(name),
                "value": value,
                "worst": worst,
                "thresh": threshold,
                "when_failed": "",
                "raw": pretty,
                "prefail": False,
            }
        )
    return out


def _nvme_attribute_entries(mapping) -> list[dict]:
    """``a{sv}`` when the binding does unpack it.

    NVMe has no attribute table. What udisks2 answers with here is the health
    log as a flat dictionary, so it is carried as one rather than being forced
    into columns it does not have.
    """
    return [
        {"name": str(key), "raw": _unwrap(value)}
        for key, value in sorted(mapping.items())
    ]


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
# Enumeration
# ==========================================================================


def find_disks() -> list[dict]:
    """Every whole physical block device, one record each.

    Module level rather than a method because :mod:`koken.probes.smart` builds
    its row 3 from the same list, and two tabs in the same branch disagreeing
    about which drives exist would be a worse fault than either of them being
    wrong.
    """
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
        return find_disks()

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
            return [
                self.row("smart", "SMART health", udisks.reason, body=udisks.detail)
            ]

        # Told apart deliberately. "udisks2 has never heard of this device" and
        # "udisks2 knows the device but ties no drive to it" have different
        # causes and different answers, and one sentence covering both sent the
        # reporter of this looking for a fault in their disk.
        block_path = udisks.block_for_device(disk["node"])
        if block_path is None:
            return [
                self.row(
                    "smart",
                    "SMART health",
                    "udisks2 is running, but none of the "
                    f"{len(udisks.block_paths)} block devices it lists is "
                    f"{disk['node']}, so no SMART data is available for it.",
                )
            ]
        drive_path = _object_path(
            udisks.properties(block_path, IFACE_BLOCK).get("Drive")
        )
        if drive_path is None:
            return [
                self.row(
                    "smart",
                    "SMART health",
                    f"udisks2 lists {disk['node']} but ties no physical drive to "
                    "it, which is what it does for loop, device-mapper and some "
                    "virtual disks. SMART is a property of the drive, so there is "
                    "none to read.",
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
                    block_path, IFACE_PARTITION_TABLE
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
