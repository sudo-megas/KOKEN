# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The probes, and the branch tree they make up.

:data:`BRANCHES` is CORE section 6 written down once. Row 1 is its four
entries in this order, row 2 is each branch's probes in this order, and row 3
comes from whatever each probe enumerates. Nothing else in the codebase decides
what the navigation contains.
"""

from .audio import AudioProbe
from .base import Context, Probe, Row, Section
from .cpu import CpuProbe
from .desktop import DesktopProbe
from .disks import DisksProbe
from .displays import DisplaysProbe
from .filesystems import FilesystemsProbe
from .graphics import GraphicsProbe
from .input import InputProbe
from .kernel import KernelProbe
from .memory import MemoryProbe
from .motherboard import MotherboardProbe
from .network import NetworkProbe
from .pci import PciProbe
from .power import PowerProbe
from .security import SecurityProbe
from .sensors import SensorsProbe
from .system import SystemProbe
from .usb import UsbProbe
from .volumes import VolumesProbe

# (branch id, branch label, probe classes in row 2 order)
BRANCHES = (
    (
        "hardware",
        "Hardware",
        (CpuProbe, MemoryProbe, GraphicsProbe, DisplaysProbe, MotherboardProbe),
    ),
    (
        "system",
        "System",
        (SystemProbe, KernelProbe, DesktopProbe, SecurityProbe),
    ),
    (
        "storage",
        "Storage",
        (DisksProbe, VolumesProbe, FilesystemsProbe),
    ),
    (
        "peripherals",
        "Peripherals",
        (UsbProbe, PciProbe, NetworkProbe, AudioProbe, InputProbe, PowerProbe, SensorsProbe),
    ),
)

BRANCH_IDS = tuple(branch_id for branch_id, _label, _probes in BRANCHES)


def build(context: Context) -> dict[str, list[Probe]]:
    """One instance of every probe, grouped by branch, built once at launch."""
    return {
        branch_id: [probe_class(context) for probe_class in probe_classes]
        for branch_id, _label, probe_classes in BRANCHES
    }


__all__ = ["BRANCHES", "BRANCH_IDS", "Context", "Probe", "Row", "Section", "build"]
