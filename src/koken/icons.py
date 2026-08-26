# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Thirty Tabler glyphs, compiled into a font subset, addressed by concept.

Icons are a font rather than SVG files for three reasons. It avoids QtSvg,
which is a separate package again on Debian. A glyph is text, so it takes the
palette's colour for free and recolours itself when the theme switches. And
thirty glyphs is fourteen kilobytes against a directory of files.

Call sites ask for a concept - ``glyph("usb_keyboard")`` - and never handle a
code point. That is what makes it possible to change which Tabler icon stands
for a concept by editing one line in the table below.

Regenerating the subset
-----------------------

The file in ``assets/`` was built from ``@tabler/icons-webfont`` 3.46.0. The
names in :data:`INCLUDE_ICONS` are the upstream ``compile-options.json``
``includeIcons`` array for this project, and every one of them was checked
against the shipped stylesheet before it went in - that stylesheet is the list
of names that actually reached the font, which is a stricter test than the icon
index. To rebuild::

    npm pack @tabler/icons-webfont
    # take the name -> code point pairs out of dist/tabler-icons.css,
    # then subset dist/fonts/tabler-icons.ttf to the code points in GLYPHS
    # with fontTools.

The subset carries name IDs 0, 13 and 14 - copyright, licence and licence URL -
which the upstream webfont build does not emit and which have to be set on the
name table explicitly. They are there so that the MIT notice travels inside the
font file itself, for anyone who ends up holding the TTF and nothing else. The
same notice is also shipped whole as ``LICENSE-tabler``, and both packages
install it to ``/usr/share/licenses/koken/``.

One concept has no icon on purpose. CORE names ``chip`` for a generic PCI
device, and there is no icon called ``chip`` in Tabler - there is
``poker-chip``, and there is ``cpu``, and neither is what was meant. CORE also
says that a concept with no clean match gets no icon rather than a vague
approximation, because a wrong icon is worse than none, so ``pci_generic``
resolves to nothing and those tabs render as text alone.
"""

from __future__ import annotations

from .config import asset_bytes

ASSET_NAME = "tabler-icons-subset.ttf"

# The upstream includeIcons array: every Tabler name in the subset, verified to
# exist in @tabler/icons-webfont 3.46.0.
INCLUDE_ICONS = (
    "alert-octagon",
    "alert-triangle",
    "arrow-loop-left",
    "battery",
    "bluetooth",
    "camera",
    "check",
    "copy",
    "device-desktop",
    "device-desktop-analytics",
    "device-sd-card",
    "device-usb",
    "disc",
    "folder",
    "folder-off",
    "headphones",
    "hierarchy",
    "keyboard",
    "mouse",
    "network",
    "plug",
    "plug-connected",
    "plug-connected-x",
    "power",
    "question-mark",
    "server",
    "temperature",
    "volume",
    "wifi",
    "wind",
)

# concept -> (Tabler name, code point). CORE section 13.5, with the one
# unmatched concept left out entirely rather than approximated.
GLYPHS: dict[str, tuple[str, int]] = {
    # Row 3 instance tabs, USB
    "usb_storage": ("device-usb", 0xFC59),
    "usb_keyboard": ("keyboard", 0xEBD6),
    "usb_pointer": ("mouse", 0xEAF9),
    "usb_audio": ("headphones", 0xEABD),
    "usb_video": ("camera", 0xEA54),
    "usb_hub": ("hierarchy", 0xEE9E),
    "bluetooth": ("bluetooth", 0xEA37),
    "usb_unknown": ("question-mark", 0xEC9D),
    # Row 3 instance tabs, PCI and graphics
    "graphics": ("device-desktop-analytics", 0xEE77),
    "pci_network": ("network", 0xF09F),
    "pci_storage": ("server", 0xEB1F),
    "pci_audio": ("volume", 0xEB51),
    "audio": ("volume", 0xEB51),
    # Row 3 instance tabs, storage
    "disk_rotational": ("disc", 0xEA90),
    "disk_solid": ("device-sd-card", 0xF384),
    "volume_mounted": ("folder", 0xEAAD),
    "volume_unmounted": ("folder-off", 0xED14),
    # Row 3 instance tabs, network and displays
    "net_ethernet": ("plug", 0xEBD9),
    "net_wireless": ("wifi", 0xEB52),
    "net_virtual": ("arrow-loop-left", 0xED9F),
    "display": ("device-desktop", 0xEA89),
    # Row 3 instance tabs, sensors and power
    "temperature": ("temperature", 0xEB38),
    "fan": ("wind", 0xEC34),
    "battery": ("battery", 0xEA34),
    "power": ("power", 0xEB0D),
    # The three non-tab places icons appear
    "copy": ("copy", 0xEA7A),
    "copy_done": ("check", 0xEA5E),
    "warning": ("alert-triangle", 0xEA06),
    "danger": ("alert-octagon", 0xECC6),
    "mount": ("plug-connected", 0xF00A),
    "unmount": ("plug-connected-x", 0xF0A0),
}

_family: str | None = None
_loaded = False


def load() -> str | None:
    """Register the subset with Qt and return its family name.

    Safe to call more than once. A failure here is not fatal: every call site
    treats an absent glyph as no glyph, so the application renders correctly
    with text alone rather than with a row of missing-character boxes.
    """
    global _family, _loaded
    if _loaded:
        return _family
    _loaded = True

    payload = asset_bytes(ASSET_NAME)
    if payload is None:
        return None

    try:
        from PySide6.QtCore import QByteArray
        from PySide6.QtGui import QFontDatabase

        font_id = QFontDatabase.addApplicationFontFromData(QByteArray(payload))
        if font_id == -1:
            return None
        families = QFontDatabase.applicationFontFamilies(font_id)
    except Exception:
        return None

    _family = families[0] if families else None
    return _family


def family() -> str | None:
    """The loaded font family, or None if the subset could not be registered."""
    return _family if _loaded else load()


def available() -> bool:
    return family() is not None


def glyph(concept: str) -> str:
    """The character for *concept*, or an empty string.

    Empty for a concept with no icon, and empty when the font did not load.
    Both cases are drawn as nothing, never as a placeholder box.
    """
    entry = GLYPHS.get(concept)
    if entry is None:
        return ""
    if family() is None:
        return ""
    return chr(entry[1])


def name_of(concept: str) -> str | None:
    """The upstream Tabler name behind a concept. For diagnostics only."""
    entry = GLYPHS.get(concept)
    return entry[0] if entry else None
