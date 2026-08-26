# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""KOKEN, a hardware and system device browser that explains what it shows.

The name means origin, or root-source. The package, the binary and every
installed path are lowercase ``koken``; the display name is KOKEN with an O
umlaut, which lives in :data:`DISPLAY_NAME` and nowhere else in the tree.
"""

DISPLAY_NAME = "KÖKEN"
SUBTITLE = "Machine Corpus"
VERSION = "1.0"
RELEASE_DATE = "2026-08-26"
MAKER = "Megas"
SOURCE = "github.com/sudo-megas/KOKEN"
SPDX = "GPL-3.0-or-later"

__version__ = VERSION

__all__ = [
    "DISPLAY_NAME",
    "SUBTITLE",
    "VERSION",
    "RELEASE_DATE",
    "MAKER",
    "SOURCE",
    "SPDX",
    "__version__",
]
