# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Partitions, one row 3 instance each, and the one row that can act.

The first row of every instance is the mount state row defined in CORE section
12.1. It is the only place in the entire application with a control on it, and
the only place KOKEN writes to the system at all.

The udisks2 object path is carried on the section so the interface can build
the control, but it is a starting point and not an authority. Object paths are
not stable across a replug: unplug a stick and put it back and udisks2 may hand
the same path to a different device. The action layer therefore resolves the
path again from the device node at the moment the button is pressed, and this
one is only ever used to decide what the row should say now.

System-critical mounts are marked at warning severity so that the button on the
row holding ``/`` is visibly not the same object as the button on a USB stick.
The button is still shown. udisks2 decides what is permitted, not a list of
guesses written here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_bytes,
    fmt_int,
    fmt_percent,
    get_root,
    list_dir,
    or_missing,
    path_exists,
    read_first_line,
    read_int,
    read_lines,
)
from .disks import (
    IFACE_BLOCK,
    IFACE_FILESYSTEM,
    IFACE_PARTITION,
    IFACE_SWAP,
    SECTOR_BYTES,
    _decode_byte_array_list,
    _decode_bytes,
    client,
)

BLOCK_ROOT = "/sys/class/block"

# A filesystem mounted at one of these is holding the running system up.
CRITICAL_MOUNTS = ("/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home")

# Values used by the mount state row and by actions.py, so the two never
# disagree about what state a volume is in.
STATE_MOUNTED = "mounted"
STATE_UNMOUNTED = "unmounted"
STATE_NO_FILESYSTEM = "none"

NOT_MOUNTED_TEXT = "Not mounted"

# Skipped by the free-space read: statvfs on an unreachable network mount
# blocks until it times out, and this runs on the interval timer.
NETWORK_FILESYSTEMS = {
    "nfs",
    "nfs4",
    "cifs",
    "smbfs",
    "sshfs",
    "fuse.sshfs",
    "afs",
    "ceph",
    "glusterfs",
    "9p",
}


def parse_mounts(lines) -> list[dict]:
    """``/proc/mounts`` into records, with octal escapes decoded.

    A mount point containing a space is written ``\\040`` by the kernel, and a
    reader that does not decode it reports the wrong path for exactly the
    directories users are most likely to have created by hand.
    """
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        out.append(
            {
                "source": _unescape(parts[0]),
                "target": _unescape(parts[1]),
                "type": parts[2],
                "options": parts[3].split(","),
            }
        )
    return out


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text
    out = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 3 < len(text) + 1:
            chunk = text[index + 1 : index + 4]
            if len(chunk) == 3 and chunk.isdigit():
                try:
                    out.append(chr(int(chunk, 8)))
                    index += 4
                    continue
                except ValueError:
                    pass
        out.append(text[index])
        index += 1
    return "".join(out)


def parse_swaps(lines) -> list[dict]:
    """``/proc/swaps``, skipping its header line."""
    out = []
    for line in lines[1:] if lines else []:
        parts = line.split()
        if len(parts) < 5:
            continue
        out.append(
            {
                "filename": _unescape(parts[0]),
                "type": parts[1],
                "size_kb": _int(parts[2]),
                "used_kb": _int(parts[3]),
                "priority": parts[4],
            }
        )
    return out


def _int(text):
    try:
        return int(text)
    except (ValueError, TypeError):
        return None


def is_critical(mount_point: str | None, sources, swaps) -> bool:
    if mount_point and mount_point in CRITICAL_MOUNTS:
        return True
    names = {sources} if isinstance(sources, str) else set(sources or ())
    if names and any(entry["filename"] in names for entry in swaps):
        return True
    return False


# LVM escapes a hyphen inside a volume group or logical volume name by doubling
# it, so the group `vg-one` holding the volume `lv-two` becomes the single
# device-mapper name `vg--one-lv--two`. This splits on the one hyphen that is
# neither preceded nor followed by another.
_LVM_SEPARATOR = re.compile(r"(?<!-)-(?!-)")


def _lvm_pair(dm_name: str) -> tuple[str, str] | None:
    parts = _LVM_SEPARATOR.split(dm_name)
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0].replace("--", "-"), parts[1].replace("--", "-")


def device_aliases(path, name: str) -> set[str]:
    """Every ``/dev`` name that can stand for this block device.

    This exists because ``/proc/mounts`` and ``/sys/class/block`` do not agree
    on what a device-mapper volume is called. sysfs calls it ``dm-1``; the
    kernel writes into ``/proc/mounts`` whatever path was passed to mount, which
    for LVM is ``/dev/mapper/vg-root`` or ``/dev/vg/root``, and for a LUKS
    container is ``/dev/mapper/whatever-the-user-named-it``. Matching only on
    ``/dev/dm-1`` therefore finds no mount record at all, and on a machine whose
    root filesystem is on LVM - which is most installations that were not set up
    by hand - the volume holding ``/`` disappears from the Volumes list.
    """
    aliases = {f"/dev/{name}"}
    dm_name = read_first_line(path / "dm/name")
    if dm_name:
        aliases.add(f"/dev/mapper/{dm_name}")
        pair = _lvm_pair(dm_name)
        if pair is not None:
            aliases.add(f"/dev/{pair[0]}/{pair[1]}")
    return aliases


def _device_number(path) -> str | None:
    """The ``major:minor`` this block device answers to, as sysfs writes it."""
    line = read_first_line(path / "dev")
    return line if line and ":" in line else None


def _stat_device_number(node: str) -> str | None:
    """``major:minor`` behind a ``/dev`` path, or None if it is not a device.

    Only consulted against the real filesystem. A test root holds a fabricated
    sysfs whose device numbers are invented, and asking the host's ``/dev``
    about them would match the wrong hardware rather than nothing.
    """
    if get_root() != Path("/"):
        return None
    try:
        info = os.stat(node)
    except (OSError, ValueError):
        return None
    if not (info.st_mode & 0o170000) == 0o060000:  # S_IFBLK
        return None
    return f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"


def statvfs_usage(mount_point: str, filesystem_type: str = "") -> dict | None:
    """Capacity and free space at *mount_point*, or None."""
    if filesystem_type in NETWORK_FILESYSTEMS:
        return None
    try:
        stats = os.statvfs(mount_point)
    except (OSError, ValueError):
        return None
    block = stats.f_frsize or stats.f_bsize
    return {
        "total": stats.f_blocks * block,
        "free": stats.f_bfree * block,
        "available": stats.f_bavail * block,
        "inodes": stats.f_files,
        "inodes_free": stats.f_ffree,
    }


class VolumesProbe(Probe):
    branch = "storage"
    id = "volumes"
    label = "Volumes"

    def __init__(self, context=None):
        super().__init__(context)
        self._volumes: list[dict] = []

    # -- enumeration ------------------------------------------------------

    def _find_volumes(self) -> list[dict]:
        """Every partition, plus whole devices that hold a filesystem directly."""
        volumes = []
        mounts = parse_mounts(read_lines("/proc/mounts"))
        swaps = parse_swaps(read_lines("/proc/swaps"))

        for path in list_dir(BLOCK_ROOT):
            name = path.name
            if name.startswith(("ram", "zram")):
                continue
            is_partition = path_exists(path / "partition")
            node = f"/dev/{name}"
            aliases = device_aliases(path, name)
            mount = _mount_for(path, aliases, mounts)
            if not is_partition:
                # A whole device with no partition table can still carry a
                # filesystem, and an unpartitioned USB stick usually does.
                if not self._is_volume(path, node, aliases, mount, swaps):
                    continue
            sectors = read_int(path / "size")
            volumes.append(
                {
                    "name": name,
                    "path": path,
                    "node": node,
                    "aliases": aliases,
                    "size": sectors * SECTOR_BYTES if sectors is not None else None,
                    "partition": is_partition,
                    "number": read_int(path / "partition"),
                    "mount": mount,
                    "swaps": swaps,
                    "readonly": read_int(path / "ro"),
                }
            )
        return volumes

    def _is_volume(self, path, node, aliases, mount, swaps) -> bool:
        """Whether this whole device is something to list as a volume."""
        if mount is not None:
            return True
        if any(entry["filename"] in aliases for entry in swaps):
            # Swap is not in /proc/mounts, so a swap volume has no mount record
            # to be found by. An encrypted swap on device-mapper would vanish.
            return True
        if read_first_line(path / "dm/name"):
            # A device-mapper volume - LVM, LUKS, an integrity or RAID target -
            # exists because somebody made it. It is a volume whether or not it
            # is mounted right now, and an unmounted logical volume is exactly
            # the thing somebody opens this list to look for.
            return True
        udisks = client()
        if not udisks.available:
            return False
        block_path = udisks.block_for_device(node)
        if block_path is None:
            return False
        return udisks.has_interface(block_path, IFACE_FILESYSTEM)

    def sections(self) -> list[Section]:
        volumes = self._find_volumes()
        self._volumes = volumes
        if not volumes:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No partitions or mountable volumes were found on this machine.",
                )
            ]
        return [self._volume_section(volume) for volume in volumes]

    # -- one volume -------------------------------------------------------

    def _volume_section(self, volume) -> Section:
        state = self.volume_state(volume)
        section = Section(
            id=volume["name"],
            label=volume["name"],
            icon="volume_mounted" if state["state"] == STATE_MOUNTED else "volume_unmounted",
            object_path=state["object_path"],
        )

        # CORE 12.1: the mount state row is the first row of every instance.
        section.add(self.mount_state_row(state))

        section.add(self.row("node", "Device node", volume["node"]))
        section.add(
            self.row(
                "size",
                "Size",
                fmt_bytes(volume["size"], binary=False) + f" ({fmt_bytes(volume['size'])})"
                if volume["size"]
                else NOT_AVAILABLE,
            )
        )
        section.add(
            self.row(
                "filesystem",
                "Filesystem",
                state["fstype"] or "None detected",
            )
        )
        if state["label"]:
            section.add(self.row("label", "Label", state["label"]))
        if state["uuid"]:
            section.add(self.row("uuid", "UUID", state["uuid"]))

        for row in self._partition_rows(volume):
            section.add(row)

        if volume["readonly"] == 1:
            section.add(
                self.row(
                    "readonly",
                    "Write protection",
                    "The kernel has this device marked read only",
                    severity=WARNING,
                )
            )

        for row in self.usage_rows(state):
            section.add(row)

        if state["mount_options"]:
            section.add(
                self.row(
                    "options",
                    "Mount options",
                    ", ".join(state["mount_options"]),
                )
            )
        if state["is_swap"]:
            section.add(
                self.row(
                    "swap",
                    "Swap",
                    "This volume is in use as swap space",
                    severity=WARNING,
                )
            )
        return section

    def _partition_rows(self, volume) -> list:
        rows = []
        if volume["number"] is not None:
            rows.append(self.row("partition_number", "Partition number", str(volume["number"])))
        udisks = client()
        if not udisks.available:
            return rows
        block_path = udisks.block_for_device(volume["node"])
        if block_path is None:
            return rows
        partition = udisks.properties(block_path, IFACE_PARTITION)
        for key, field, label in (
            ("Name", "partition_name", "Partition name"),
            ("Type", "partition_type", "Partition type"),
            ("UUID", "partition_uuid", "Partition UUID"),
        ):
            value = partition.get(key)
            if value not in (None, ""):
                rows.append(self.row(field, label, str(value)))
        offset = partition.get("Offset")
        if isinstance(offset, (int, float)) and offset:
            rows.append(self.row("partition_offset", "Offset", fmt_bytes(offset)))
        flags = partition.get("Flags")
        if isinstance(flags, (int, float)) and flags:
            rows.append(self.row("partition_flags", "Partition flags", hex(int(flags))))
        return rows

    # -- mount state ------------------------------------------------------

    def volume_state(self, volume) -> dict:
        """Everything the mount state row and the action layer need.

        udisks2 is preferred where it is running, because it is the same source
        the mount and unmount calls act through and so cannot disagree with
        them. ``/proc/mounts`` is the fallback, and is enough to render the row
        correctly even with no udisks2 at all - only the control needs it.
        """
        mount = volume["mount"]
        state = {
            "name": volume["name"],
            "node": volume["node"],
            "object_path": None,
            "state": STATE_NO_FILESYSTEM,
            "mount_point": None,
            "mount_options": mount["options"] if mount else [],
            "fstype": mount["type"] if mount else None,
            "label": None,
            "uuid": None,
            "critical": False,
            "is_swap": any(
                entry["filename"] in _aliases_of(volume) for entry in volume["swaps"]
            ),
            "actionable": False,
        }

        udisks = client()
        if udisks.available:
            block_path = udisks.block_for_device(volume["node"])
            if block_path is not None:
                block = udisks.properties(block_path, IFACE_BLOCK)
                state["object_path"] = block_path
                state["fstype"] = _text(block.get("IdType")) or state["fstype"]
                state["label"] = _text(block.get("IdLabel"))
                state["uuid"] = _text(block.get("IdUUID"))
                if udisks.has_interface(block_path, IFACE_SWAP):
                    state["is_swap"] = True
                if udisks.has_interface(block_path, IFACE_FILESYSTEM):
                    # The interface being there is the whole of what makes a
                    # volume mountable, and it is there for a filesystem that
                    # is not mounted - which is exactly the case a device node
                    # and /proc/mounts between them cannot see at all.
                    state["actionable"] = True
                    filesystem = udisks.properties(block_path, IFACE_FILESYSTEM)
                    points = _decode_byte_array_list(filesystem.get("MountPoints"))
                    if points:
                        state["state"] = STATE_MOUNTED
                        state["mount_point"] = points[0]
                    elif mount is not None:
                        # udisks2 writes MountPoints as an aay, which this Qt
                        # binding hands back as an opaque container, so on a
                        # live bus the list above is empty whatever the truth
                        # is. /proc/mounts is the kernel's own answer to the
                        # same question and is what udisks2 reads it from.
                        state["state"] = STATE_MOUNTED
                        state["mount_point"] = mount["target"]
                    else:
                        state["state"] = STATE_UNMOUNTED

        if state["state"] == STATE_NO_FILESYSTEM and mount is not None:
            state["state"] = STATE_MOUNTED
            state["mount_point"] = mount["target"]
        elif state["state"] == STATE_NO_FILESYSTEM and state["fstype"]:
            state["state"] = STATE_UNMOUNTED

        if state["mount_point"] and not state["mount_options"]:
            for entry in parse_mounts(read_lines("/proc/mounts")):
                if entry["target"] == state["mount_point"]:
                    state["mount_options"] = entry["options"]
                    state["fstype"] = state["fstype"] or entry["type"]
                    break

        if state["uuid"] is None:
            state["uuid"] = read_first_line(volume["path"] / "dm/uuid")

        state["critical"] = is_critical(
            state["mount_point"], _aliases_of(volume), volume["swaps"]
        )
        return state

    def mount_state_row(self, state):
        """CORE 12.1, exactly: the mount point, ``Not mounted``, or the type."""
        if state["state"] == STATE_MOUNTED:
            value = state["mount_point"] or "Mounted"
        elif state["state"] == STATE_UNMOUNTED:
            value = NOT_MOUNTED_TEXT
        else:
            value = state["fstype"] or "No filesystem"
        return self.row(
            "mount_state",
            "Mount state",
            value,
            tier=VOLATILE,
            severity=WARNING if state["critical"] else "normal",
        )

    def usage_rows(self, state) -> list:
        if state["state"] != STATE_MOUNTED or not state["mount_point"]:
            return []
        fstype = state["fstype"] or ""
        if fstype in NETWORK_FILESYSTEMS:
            return [
                self.row(
                    "free",
                    "Free space",
                    "Not read. This is a network filesystem, and asking it would "
                    "stall the interface if the server were unreachable.",
                    tier=VOLATILE,
                )
            ]
        usage = statvfs_usage(state["mount_point"], fstype)
        if usage is None:
            return [
                self.row(
                    "free",
                    "Free space",
                    f"Could not be read at {state['mount_point']}.",
                    tier=VOLATILE,
                )
            ]
        total = usage["total"]
        used = total - usage["free"]
        rows = [
            self.row("capacity", "Capacity", fmt_bytes(total), tier=VOLATILE),
            self.row(
                "used",
                "Used",
                f"{fmt_bytes(used)} ({fmt_percent(used * 100.0 / total)})" if total else fmt_bytes(used),
                tier=VOLATILE,
            ),
            self.row(
                "free",
                "Free",
                f"{fmt_bytes(usage['available'])} available to you"
                + (f", {fmt_bytes(usage['free'])} in total" if usage["free"] != usage["available"] else ""),
                tier=VOLATILE,
                severity=WARNING
                if total and usage["available"] * 100.0 / total < 5
                else "normal",
            ),
        ]
        if usage["inodes"]:
            used_inodes = usage["inodes"] - usage["inodes_free"]
            rows.append(
                self.row(
                    "inodes",
                    "Files",
                    "{} of {} slots used".format(
                        fmt_int(used_inodes), fmt_int(usage["inodes"])
                    ),
                    tier=VOLATILE,
                )
            )
        return rows

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        out: dict[str, list] = {}
        # Mount points move without any hardware appearing or disappearing, so
        # the mount records are re-read here. The device list is not.
        mounts = parse_mounts(read_lines("/proc/mounts"))
        swaps = parse_swaps(read_lines("/proc/swaps"))
        for volume in self._volumes or self._find_volumes():
            volume = dict(volume)
            volume["mount"] = _mount_for(
                volume["path"], _aliases_of(volume), mounts
            )
            volume["swaps"] = swaps
            state = self.volume_state(volume)
            rows = [self.mount_state_row(state)]
            rows.extend(self.usage_rows(state))
            out[volume["name"]] = rows
        return out


def _aliases_of(volume) -> set[str]:
    """The alias set, tolerating a volume record built before they existed."""
    aliases = volume.get("aliases")
    return set(aliases) if aliases else {volume["node"]}


def _mount_for(path, aliases, mounts):
    """The ``/proc/mounts`` record for this block device, by any of its names.

    Names are tried first, and the device number only for the leftovers: on a
    machine with no device-mapper at all the names always match, and the stat
    is never reached.
    """
    for entry in mounts:
        if entry["source"] in aliases:
            return entry
    number = _device_number(path)
    if number is None:
        return None
    for entry in mounts:
        source = entry["source"]
        if not source.startswith("/dev/") or source in aliases:
            continue
        if _stat_device_number(source) == number:
            return entry
    return None


def _text(value) -> str | None:
    text = _decode_bytes(value)
    if text is None and isinstance(value, str):
        text = value
    return text or None
