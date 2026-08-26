# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Everything currently mounted, and everything currently swapping.

Where Volumes is a device view - one entry per partition, whether or not it is
in use - this is a mount view. The two overlap deliberately: a tmpfs has no
partition and appears only here, and an unmounted disk has no mount point and
appears only there.

Pseudo filesystems are shown, in their own group and below the real ones. A
machine has thirty of them and none of them is interesting until the moment one
of them is the answer to why something is out of space.
"""

from __future__ import annotations

from .base import (
    NOT_AVAILABLE,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_bytes,
    fmt_percent,
    read_lines,
)
from .volumes import parse_mounts, parse_swaps, statvfs_usage

# Kernel bookkeeping rather than storage. Listed second and counted separately.
PSEUDO_FILESYSTEMS = {
    "autofs",
    "bpf",
    "binfmt_misc",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fuse.gvfsd-fuse",
    "fuse.portal",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "proc",
    "pstore",
    "ramfs",
    "rpc_pipefs",
    "securityfs",
    "selinuxfs",
    "sysfs",
    "tracefs",
}

# Shown, but as a filesystem in its own right rather than a pseudo one: a
# tmpfs holds real files and can really fill up.
MEMORY_FILESYSTEMS = {"tmpfs", "zram"}


class FilesystemsProbe(Probe):
    branch = "storage"
    id = "filesystems"
    label = "Filesystems"

    def sections(self) -> list[Section]:
        return [self._mounts(), self._swap()]

    # -- mounts -----------------------------------------------------------

    def _mounts(self) -> Section:
        section = Section(id="mounts", label="Mounts")
        mounts = parse_mounts(read_lines("/proc/mounts"))
        if not mounts:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "/proc/mounts could not be read, so nothing can be said about "
                    "what is mounted.",
                )
            )
            return section

        real = [m for m in mounts if m["type"] not in PSEUDO_FILESYSTEMS]
        pseudo = [m for m in mounts if m["type"] in PSEUDO_FILESYSTEMS]

        section.add(
            self.row(
                "mount_count",
                "Mounted filesystems",
                f"{len(mounts)} in total — {len(real)} holding files, "
                f"{len(pseudo)} kernel interfaces",
            )
        )
        section.add(
            self.row(
                "mount_types",
                "Types in use",
                ", ".join(sorted({m["type"] for m in real})) or "None",
            )
        )

        for mount in sorted(real, key=lambda m: m["target"]):
            for row in self._mount_rows(mount):
                section.add(row)

        if pseudo:
            section.add(
                self.row(
                    "pseudo_heading",
                    "Kernel interfaces",
                    f"{len(pseudo)} pseudo filesystems, which hold no files on any disk",
                )
            )
            for mount in sorted(pseudo, key=lambda m: m["target"]):
                section.add(
                    self.row(
                        "pseudo_mount",
                        f"  {mount['target']}",
                        mount["type"],
                        key=f"pseudo{mount['target']}",
                    )
                )
        return section

    def _mount_rows(self, mount) -> list:
        usage = statvfs_usage(mount["target"], mount["type"])
        detail = mount["type"]
        severity = "normal"
        if usage and usage["total"]:
            used = usage["total"] - usage["free"]
            share = used * 100.0 / usage["total"]
            detail += ", {} of {} used ({})".format(
                fmt_bytes(used), fmt_bytes(usage["total"]), fmt_percent(share)
            )
            if usage["available"] * 100.0 / usage["total"] < 5:
                severity = WARNING
        elif usage is None and mount["type"] not in MEMORY_FILESYSTEMS:
            detail += ", size not read"

        rows = [
            self.row(
                "mount",
                mount["target"],
                detail,
                tier=VOLATILE,
                severity=severity,
                key=f"mount{mount['target']}",
            ),
            self.row(
                "mount_source",
                "  from",
                mount["source"],
                key=f"src{mount['target']}",
            ),
        ]
        interesting = [
            option
            for option in mount["options"]
            if option in ("ro", "noexec", "nosuid", "nodev", "noatime", "relatime",
                          "discard", "compress", "subvol", "sync")
            or option.startswith(("subvol=", "compress=", "compress-force="))
        ]
        if interesting:
            rows.append(
                self.row(
                    "mount_options",
                    "  options",
                    ", ".join(interesting),
                    severity=WARNING if "ro" in interesting else "normal",
                    key=f"opt{mount['target']}",
                )
            )
        return rows

    # -- swap -------------------------------------------------------------

    def _swap(self) -> Section:
        section = Section(id="swap", label="Swap")
        swaps = parse_swaps(read_lines("/proc/swaps"))
        if not swaps:
            section.add(
                self.row(
                    "swap_absent",
                    "Swap",
                    "None configured. When memory runs out the kernel has nowhere "
                    "to put pages, and will start killing processes instead.",
                )
            )
            return section

        total = sum(entry["size_kb"] or 0 for entry in swaps) * 1024
        used = sum(entry["used_kb"] or 0 for entry in swaps) * 1024
        section.add(self.row("swap_count", "Swap areas", str(len(swaps))))
        section.add(self.row("swap_total", "Total", fmt_bytes(total)))
        section.add(
            self.row(
                "swap_used",
                "In use",
                f"{fmt_bytes(used)} ({fmt_percent(used * 100.0 / total)})"
                if total
                else fmt_bytes(used),
                tier=VOLATILE,
                severity=WARNING if total and used * 100.0 / total > 50 else "normal",
            )
        )

        swappiness = _swappiness()
        if swappiness is not None:
            section.add(
                self.row(
                    "swappiness",
                    "Swappiness",
                    f"{swappiness} of 200 — how readily the kernel moves pages out of memory",
                )
            )

        for entry in swaps:
            size = (entry["size_kb"] or 0) * 1024
            entry_used = (entry["used_kb"] or 0) * 1024
            detail = "{}, {} of {} used, priority {}".format(
                entry["type"], fmt_bytes(entry_used), fmt_bytes(size), entry["priority"]
            )
            section.add(
                self.row(
                    "swap_area",
                    f"  {entry['filename']}",
                    detail,
                    tier=VOLATILE,
                    key=f"swap{entry['filename']}",
                )
            )
        return section

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        out: dict[str, list] = {}

        mounts = parse_mounts(read_lines("/proc/mounts"))
        rows = []
        for mount in sorted(
            (m for m in mounts if m["type"] not in PSEUDO_FILESYSTEMS),
            key=lambda m: m["target"],
        ):
            rows.extend(row for row in self._mount_rows(mount) if row.is_volatile)
        if rows:
            out["mounts"] = rows

        swaps = parse_swaps(read_lines("/proc/swaps"))
        if swaps:
            total = sum(entry["size_kb"] or 0 for entry in swaps) * 1024
            used = sum(entry["used_kb"] or 0 for entry in swaps) * 1024
            swap_rows = [
                self.row(
                    "swap_used",
                    "In use",
                    f"{fmt_bytes(used)} ({fmt_percent(used * 100.0 / total)})"
                    if total
                    else fmt_bytes(used),
                    tier=VOLATILE,
                    severity=WARNING if total and used * 100.0 / total > 50 else "normal",
                )
            ]
            for entry in swaps:
                size = (entry["size_kb"] or 0) * 1024
                entry_used = (entry["used_kb"] or 0) * 1024
                swap_rows.append(
                    self.row(
                        "swap_area",
                        f"  {entry['filename']}",
                        "{}, {} of {} used, priority {}".format(
                            entry["type"],
                            fmt_bytes(entry_used),
                            fmt_bytes(size),
                            entry["priority"],
                        ),
                        tier=VOLATILE,
                        key=f"swap{entry['filename']}",
                    )
                )
            out["swap"] = swap_rows
        return out


def _swappiness() -> int | None:
    from .base import read_int

    return read_int("/proc/sys/vm/swappiness")
