# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Reading the block of bytes a monitor tells the computer about itself.

``/sys/class/drm/card0-DP-1/edid`` is world readable and holds the display's
own description of itself: who made it, when, how big it is and what it can
show. The base block is 128 bytes of packed fields from 1994; byte 126 says how
many further 128-byte blocks follow it, and on any modern display at least one
does. Unpacking all of them is the whole job - no library, no external tool.

The extension blocks are not optional reading. The 18-byte detailed timing
descriptor that the base block uses carries its pixel clock in a 16-bit field
counting units of 10 kHz, so it cannot express anything above 655.35 MHz - and
2560x1440 at 280 Hz needs about 1.1 GHz. A display like that physically cannot
state its own best mode in the base block. It states a conservative 60 Hz mode
there instead and puts the real one in a CTA-861 or DisplayID extension, so a
parser that reads only the base block and calls its first descriptor "native"
reports 59.95 Hz on a 280 Hz monitor.

Nothing here is trusted before it is checked. The eight-byte header and the
checksum are both verified first, because a blob can legitimately be
zero-length (nothing plugged in), truncated (a marginal cable), or all zeroes
(a KVM switch that answers reads without passing them through). A blob that
fails either check produces a result that says so, and the display probe shows
that sentence instead of inventing a monitor. Every extension block is then
checked on its own terms before any of it is believed: a block that fails its
checksum is named and dropped, and the blocks either side of it are still read.

What is deliberately not decoded is listed in :data:`UNDECODED`. Those formats
are named where they are found rather than guessed at, because a wrong bit
layout that yields a plausible-looking mode is worse here than a missing one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

BLOCK_SIZE = 128
HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"

# Descriptor block tags, at byte 3 of an 18-byte descriptor whose first two
# bytes are zero.
TAG_SERIAL = 0xFF
TAG_TEXT = 0xFE
TAG_RANGE_LIMITS = 0xFD
TAG_NAME = 0xFC

# Extension block tags, byte 0 of every 128-byte block after the base one.
EXT_CTA = 0x02
EXT_DISPLAYID = 0x70

EXTENSION_NAMES = {
    0x02: "CTA-861",
    0x10: "Video timing block",
    0x20: "EDID 2.0",
    0x40: "Display information",
    0x50: "Localised string",
    0x60: "Microdisplay interface",
    0x70: "DisplayID",
    0xF0: "Block map",
    0xFF: "Manufacturer defined",
}

# CTA-861 data block collection tags, the top three bits of each block header.
CTA_TAG_AUDIO = 1
CTA_TAG_VIDEO = 2
CTA_TAG_VENDOR = 3
CTA_TAG_SPEAKER = 4
CTA_TAG_EXTENDED = 7

# CTA-861 extended tags that carry timings and are named but not decoded. See
# UNDECODED for why.
CTA_TIMING_BLOCKS = {
    0x22: "CTA-861 DisplayID Type VII timing block",
    0x23: "CTA-861 DisplayID Type VIII timing block",
    0x24: "CTA-861 DisplayID Type X timing block",
}

# DisplayID data block tags that carry timings this parser does not decode,
# per DisplayID major version.
DISPLAYID_TIMING_BLOCKS = {
    1: {
        0x04: "DisplayID Type II timing block",
        0x05: "DisplayID Type III timing block",
        0x06: "DisplayID Type IV timing block",
        0x07: "DisplayID VESA timing block",
        0x08: "DisplayID CTA timing block",
        0x11: "DisplayID Type V timing block",
        0x13: "DisplayID Type VI timing block",
    },
    # DisplayID 2.0 renumbered its data blocks into the 0x20s, and the only two
    # tags in that range confirmed here against the kernel's own header are
    # 0x03 (Type I, 1.x) and 0x22 (Type VII), both of which are decoded below.
    # The rest were guessed once and the guesses were wrong, so nothing is
    # asserted about them: an unrecognised block is reported by its tag number
    # rather than given a name this parser cannot stand behind.
    2: {},
}

# The detailed timing block. DisplayID 1.x calls it Type I and tags it 0x03;
# DisplayID 2.0 calls it Type VII and tags it 0x22. The twenty bytes are laid
# out identically and only the pixel clock unit differs, 10 kHz against 1 kHz,
# so the tag alone decides both which blocks to read and how to read them.
#
# Keyed by tag rather than by the block's declared major version, because that
# is what the kernel does - drm_edid.c dispatches on the tag and derives the
# unit from it (`type_7 = block->tag == DATA_BLOCK_2_TYPE_7_DETAILED_TIMING`),
# never consulting the version. A display that declares one version and carries
# the other's timing block is therefore read correctly rather than skipped.
DISPLAYID_DETAILED_TAGS = {0x03: 10, 0x22: 1}

# Named here rather than in a comment because the display probe shows this list
# on screen when one of these blocks turns up. Each of these encodes timings in
# a layout this parser does not implement:
#
#   - the enumerated and formula-based DisplayID timing blocks, which give a
#     code or a set of parameters rather than a timing, and would need the full
#     VESA DMT table and the CVT formulae reimplemented to resolve;
#   - the CTA-861 extended-tag timing blocks, which embed a DisplayID
#     descriptor inside a CTA data block. The descriptor itself is the same
#     twenty bytes decoded below, but whether a revision byte sits between the
#     extended tag and those twenty bytes is not something to get wrong by
#     guessing: one byte out and the parser prints a confident, invented mode.
UNDECODED = tuple(CTA_TIMING_BLOCKS.values()) + tuple(
    name for table in DISPLAYID_TIMING_BLOCKS.values() for name in table.values()
)

# Where a timing came from, recorded on the timing itself so the display probe
# can say which block described a mode.
FROM_BASE = "base block"
FROM_CTA = "CTA-861 extension"
FROM_CTA_VIC = "CTA-861 video format"

# A DisplayID descriptor is twenty bytes of counts, every one of them stored
# one less than its real value.
DISPLAYID_DESCRIPTOR = 20

# Sanity bounds for a timing recovered from an extension block. A block that
# passed its checksum can still be structurally odd, and a descriptor made of
# padding decodes to a 1x1 mode at an absurd rate. These are wide enough to
# admit anything a real display offers and narrow enough to reject arithmetic
# on bytes that were never a timing.
MIN_ACTIVE = 64
MIN_REFRESH = 1.0
MAX_REFRESH = 1000.0

DIGITAL_INTERFACES = {
    0: "Undefined",
    1: "DVI",
    2: "HDMI-a",
    3: "HDMI-b",
    4: "MDDI",
    5: "DisplayPort",
}

BIT_DEPTHS = {0: None, 1: 6, 2: 8, 3: 10, 4: 12, 5: 14, 6: 16}

# The fixed table of timings every display of the era advertised as a bitmap.
ESTABLISHED_TIMINGS = (
    (0, 7, "720x400 @ 70 Hz"),
    (0, 6, "720x400 @ 88 Hz"),
    (0, 5, "640x480 @ 60 Hz"),
    (0, 4, "640x480 @ 67 Hz"),
    (0, 3, "640x480 @ 72 Hz"),
    (0, 2, "640x480 @ 75 Hz"),
    (0, 1, "800x600 @ 56 Hz"),
    (0, 0, "800x600 @ 60 Hz"),
    (1, 7, "800x600 @ 72 Hz"),
    (1, 6, "800x600 @ 75 Hz"),
    (1, 5, "832x624 @ 75 Hz"),
    (1, 4, "1024x768 @ 87 Hz interlaced"),
    (1, 3, "1024x768 @ 60 Hz"),
    (1, 2, "1024x768 @ 70 Hz"),
    (1, 1, "1024x768 @ 75 Hz"),
    (1, 0, "1280x1024 @ 75 Hz"),
    (2, 7, "1152x870 @ 75 Hz"),
)

# Standard timing aspect ratios, byte 2 bits 7-6. Code 0 changed meaning: it
# is 1:1 in EDID 1.0 through 1.2 and 16:10 from 1.3 onward, and the structure
# carries no other clue which it is. Reading a 1.2 display's 1280x1280 mode as
# 1280x800 is not a rounding error, it is a different mode.
ASPECT_RATIOS = {0: (16, 10), 1: (4, 3), 2: (5, 4), 3: (16, 9)}
ASPECT_RATIOS_PRE_1_3 = {0: (1, 1), 1: (4, 3), 2: (5, 4), 3: (16, 9)}

# CTA-861 video identification codes: the short video descriptors in a video
# data block are indices into this table, not timings. Only the active pixel
# count, the nominal field rate and the scan are kept, because that is all this
# application shows; a rate given here as 60 is 60 or 59.94 depending on which
# pixel clock the source drives, and the two are the same mode.
#
# The table stops at 127 on purpose. Codes 193 and above exist and describe the
# 8K and 10K formats, but this parser is not confident enough of that part of
# the table to print a resolution from it, so those codes are reported as bare
# numbers instead.
VIC_MODES = {
    1: (640, 480, 60.0, False),
    2: (720, 480, 60.0, False),
    3: (720, 480, 60.0, False),
    4: (1280, 720, 60.0, False),
    5: (1920, 1080, 60.0, True),
    6: (1440, 480, 60.0, True),
    7: (1440, 480, 60.0, True),
    8: (1440, 240, 60.0, False),
    9: (1440, 240, 60.0, False),
    10: (2880, 480, 60.0, True),
    11: (2880, 480, 60.0, True),
    12: (2880, 240, 60.0, False),
    13: (2880, 240, 60.0, False),
    14: (1440, 480, 60.0, False),
    15: (1440, 480, 60.0, False),
    16: (1920, 1080, 60.0, False),
    17: (720, 576, 50.0, False),
    18: (720, 576, 50.0, False),
    19: (1280, 720, 50.0, False),
    20: (1920, 1080, 50.0, True),
    21: (1440, 576, 50.0, True),
    22: (1440, 576, 50.0, True),
    23: (1440, 288, 50.0, False),
    24: (1440, 288, 50.0, False),
    25: (2880, 576, 50.0, True),
    26: (2880, 576, 50.0, True),
    27: (2880, 288, 50.0, False),
    28: (2880, 288, 50.0, False),
    29: (1440, 576, 50.0, False),
    30: (1440, 576, 50.0, False),
    31: (1920, 1080, 50.0, False),
    32: (1920, 1080, 24.0, False),
    33: (1920, 1080, 25.0, False),
    34: (1920, 1080, 30.0, False),
    35: (2880, 480, 60.0, False),
    36: (2880, 480, 60.0, False),
    37: (2880, 576, 50.0, False),
    38: (2880, 576, 50.0, False),
    39: (1920, 1080, 50.0, True),
    40: (1920, 1080, 100.0, True),
    41: (1280, 720, 100.0, False),
    42: (720, 576, 100.0, False),
    43: (720, 576, 100.0, False),
    44: (1440, 576, 100.0, True),
    45: (1440, 576, 100.0, True),
    46: (1920, 1080, 120.0, True),
    47: (1280, 720, 120.0, False),
    48: (720, 480, 120.0, False),
    49: (720, 480, 120.0, False),
    50: (1440, 480, 120.0, True),
    51: (1440, 480, 120.0, True),
    52: (720, 576, 200.0, False),
    53: (720, 576, 200.0, False),
    54: (1440, 576, 200.0, True),
    55: (1440, 576, 200.0, True),
    56: (720, 480, 240.0, False),
    57: (720, 480, 240.0, False),
    58: (1440, 480, 240.0, True),
    59: (1440, 480, 240.0, True),
    60: (1280, 720, 24.0, False),
    61: (1280, 720, 25.0, False),
    62: (1280, 720, 30.0, False),
    63: (1920, 1080, 120.0, False),
    64: (1920, 1080, 100.0, False),
    65: (1280, 720, 24.0, False),
    66: (1280, 720, 25.0, False),
    67: (1280, 720, 30.0, False),
    68: (1280, 720, 50.0, False),
    69: (1280, 720, 60.0, False),
    70: (1280, 720, 100.0, False),
    71: (1280, 720, 120.0, False),
    72: (1920, 1080, 24.0, False),
    73: (1920, 1080, 25.0, False),
    74: (1920, 1080, 30.0, False),
    75: (1920, 1080, 50.0, False),
    76: (1920, 1080, 60.0, False),
    77: (1920, 1080, 100.0, False),
    78: (1920, 1080, 120.0, False),
    79: (1680, 720, 24.0, False),
    80: (1680, 720, 25.0, False),
    81: (1680, 720, 30.0, False),
    82: (1680, 720, 50.0, False),
    83: (1680, 720, 60.0, False),
    84: (1680, 720, 100.0, False),
    85: (1680, 720, 120.0, False),
    86: (2560, 1080, 24.0, False),
    87: (2560, 1080, 25.0, False),
    88: (2560, 1080, 30.0, False),
    89: (2560, 1080, 50.0, False),
    90: (2560, 1080, 60.0, False),
    91: (2560, 1080, 100.0, False),
    92: (2560, 1080, 120.0, False),
    93: (3840, 2160, 24.0, False),
    94: (3840, 2160, 25.0, False),
    95: (3840, 2160, 30.0, False),
    96: (3840, 2160, 50.0, False),
    97: (3840, 2160, 60.0, False),
    98: (4096, 2160, 24.0, False),
    99: (4096, 2160, 25.0, False),
    100: (4096, 2160, 30.0, False),
    101: (4096, 2160, 50.0, False),
    102: (4096, 2160, 60.0, False),
    103: (3840, 2160, 24.0, False),
    104: (3840, 2160, 25.0, False),
    105: (3840, 2160, 30.0, False),
    106: (3840, 2160, 50.0, False),
    107: (3840, 2160, 60.0, False),
    108: (1280, 720, 48.0, False),
    109: (1280, 720, 48.0, False),
    110: (1680, 720, 48.0, False),
    111: (1920, 1080, 48.0, False),
    112: (1920, 1080, 48.0, False),
    113: (2560, 1080, 48.0, False),
    114: (3840, 2160, 48.0, False),
    115: (4096, 2160, 48.0, False),
    116: (3840, 2160, 48.0, False),
    117: (3840, 2160, 100.0, False),
    118: (3840, 2160, 120.0, False),
    119: (3840, 2160, 100.0, False),
    120: (3840, 2160, 120.0, False),
    121: (5120, 2160, 24.0, False),
    122: (5120, 2160, 25.0, False),
    123: (5120, 2160, 30.0, False),
    124: (5120, 2160, 48.0, False),
    125: (5120, 2160, 50.0, False),
    126: (5120, 2160, 60.0, False),
    127: (5120, 2160, 100.0, False),
}


@dataclass
class Timing:
    """One timing descriptor - a mode the display says it can show.

    ``source`` names the block it was read from, so a mode found only in an
    extension can be shown as such rather than appearing from nowhere.
    """

    width: int
    height: int
    pixel_clock_khz: int
    refresh_hz: float | None
    interlaced: bool = False
    width_mm: int | None = None
    height_mm: int | None = None
    source: str = ""

    def describe(self) -> str:
        text = f"{self.width}x{self.height}"
        if self.refresh_hz:
            text += f" @ {self.refresh_hz:.2f} Hz".replace(".00 Hz", " Hz")
        if self.interlaced:
            text += " interlaced"
        return text

    @property
    def mode_key(self) -> tuple[int, int, bool]:
        """What makes two entries the same mode for listing purposes.

        The scan is part of it: 1920x1080 interlaced and 1920x1080 progressive
        are different modes, the kernel gives them different names, and the
        rates one runs at are not the rates the other does.
        """
        return (self.width, self.height, self.interlaced)


@dataclass
class Edid:
    """A parsed EDID, or the reason it could not be parsed.

    ``valid`` is the only thing callers branch on. When it is False, ``error``
    holds a sentence written for the person reading the screen, not a
    diagnostic code.
    """

    valid: bool = False
    error: str = ""
    raw_length: int = 0

    manufacturer: str = ""
    product_code: int | None = None
    serial_number: int | None = None
    manufacture_week: int | None = None
    manufacture_year: int | None = None
    is_model_year: bool = False
    version: str = ""

    digital: bool | None = None
    bit_depth: int | None = None
    interface: str = ""
    width_cm: int | None = None
    height_cm: int | None = None
    gamma: float | None = None
    features: list[str] = field(default_factory=list)

    monitor_name: str = ""
    monitor_serial: str = ""
    monitor_text: list[str] = field(default_factory=list)
    range_limits: str = ""

    established: list[str] = field(default_factory=list)
    standard: list[str] = field(default_factory=list)
    detailed: list[Timing] = field(default_factory=list)

    # How many extension blocks byte 126 declares, which is not necessarily
    # how many arrived: see extension_notes.
    extensions: int = 0
    # One name per extension block that was read and believed.
    extension_blocks: list[str] = field(default_factory=list)
    # One sentence per block that was refused, and why.
    extension_notes: list[str] = field(default_factory=list)
    # Timing blocks that were found, named, and deliberately not decoded.
    undecoded: list[str] = field(default_factory=list)

    cta_detailed: list[Timing] = field(default_factory=list)
    cta_video: list[Timing] = field(default_factory=list)
    cta_vics: list[int] = field(default_factory=list)
    cta_unknown_vics: list[int] = field(default_factory=list)
    cta_features: list[str] = field(default_factory=list)

    displayid_detailed: list[Timing] = field(default_factory=list)

    @property
    def detailed_timings(self) -> list[Timing]:
        """Every mode any block spells out in full, base block first.

        A detailed timing is the display stating a pixel grid and the clock to
        drive it with. That is a different claim from a CTA video code, which
        says only that the display will accept a standard broadcast format -
        which is why the two are kept apart and only this list decides what is
        native.
        """
        return [*self.detailed, *self.cta_detailed, *self.displayid_detailed]

    @property
    def all_timings(self) -> list[Timing]:
        """Detailed timings plus the CTA video formats, for listing only."""
        return [*self.detailed_timings, *self.cta_video]

    @property
    def preferred(self) -> Timing | None:
        """The base block's first detailed timing.

        EDID calls this the preferred timing and for most displays it is also
        the best one. On a display whose best mode needs more than 655.35 MHz
        it cannot be, because the field it would have to be written in does not
        go that high - so this is reported as what it is, the mode named first,
        and :attr:`native` is worked out separately.
        """
        return self.detailed[0] if self.detailed else None

    @property
    def native(self) -> Timing | None:
        """The best mode the display describes, across every block it sent.

        The rule, in order of precedence:

        1. the largest pixel count, width times height;
        2. progressive before interlaced;
        3. the highest refresh rate;
        4. the highest pixel clock.

        Resolution comes first because that is what "native" means on a fixed
        pixel grid: every other mode on an LCD or an OLED is being scaled onto
        it. Refresh ranks below resolution and above pixel clock because a
        display that offers 1440p at 280 Hz and 1080p at 360 Hz is a 1440p
        panel, while two entries for the same grid differ only in how fast it
        is driven. Ties are settled by the pixel clock, and a tie all the way
        down is won by whichever block was read first, which puts the base
        block's own preferred timing ahead of a copy of it in an extension.

        Only detailed timings are candidates. A CTA video code says the display
        will accept a format, not that the format is its own; a 1080p television
        that accepts 2160p60 over HDMI and scales it down would otherwise be
        reported as a 4K panel.
        """
        candidates = self.detailed_timings
        if not candidates:
            return None
        return max(candidates, key=native_rank)

    @property
    def diagonal_inches(self) -> float | None:
        if not self.width_cm or not self.height_cm:
            return None
        return ((self.width_cm**2 + self.height_cm**2) ** 0.5) / 2.54

    def refresh_map(self) -> dict[tuple[int, int, bool], list[float]]:
        """Refresh rates this EDID states, keyed by mode, highest first.

        The DRM connector's ``modes`` file names every mode by its resolution
        alone, so a display that runs 2560x1440 at five different rates lists
        the same name five times. This is what turns that back into something
        readable.
        """
        out: dict[tuple[int, int, bool], list[float]] = {}
        for timing in self.all_timings:
            if not timing.refresh_hz:
                continue
            rates = out.setdefault(timing.mode_key, [])
            if not any(abs(rate - timing.refresh_hz) < 0.05 for rate in rates):
                rates.append(timing.refresh_hz)
        for rates in out.values():
            rates.sort(reverse=True)
        return out


def native_rank(timing: Timing) -> tuple:
    """The sort key behind :attr:`Edid.native`, documented there.

    Public because the display probe orders the rest of the mode list by the
    same rule, so that "next best" on screen means what "best" means here.
    """
    return (
        timing.width * timing.height,
        0 if timing.interlaced else 1,
        timing.refresh_hz or 0.0,
        timing.pixel_clock_khz,
    )


def _invalid(reason: str, length: int) -> Edid:
    return Edid(valid=False, error=reason, raw_length=length)


def checksum_ok(block: bytes) -> bool:
    """Every EDID block sums to zero modulo 256. That is the whole check."""
    return len(block) >= BLOCK_SIZE and sum(block[:BLOCK_SIZE]) % 256 == 0


def parse(blob: bytes | None) -> Edid:
    """Parse an EDID blob. Never raises; an unusable blob comes back invalid."""
    if blob is None:
        return _invalid("This connector exposes no EDID.", 0)
    length = len(blob)
    if length == 0:
        return _invalid(
            "The EDID is empty. Nothing is connected, or the display did not answer.",
            0,
        )
    if length < BLOCK_SIZE:
        return _invalid(
            f"The EDID is only {length} bytes, where at least {BLOCK_SIZE} are needed. "
            "A marginal cable or adapter usually causes this.",
            length,
        )
    if blob[:8] != HEADER:
        return _invalid(
            "The EDID does not begin with the expected header, so nothing in it "
            "can be trusted. A KVM switch or a display adapter that answers reads "
            "without passing them through looks like this.",
            length,
        )
    if not checksum_ok(blob):
        return _invalid(
            "The EDID checksum does not match, so the data arrived corrupted and "
            "is not being shown.",
            length,
        )

    try:
        return _parse_checked(blob, length)
    except (struct.error, ValueError, IndexError):
        # The header and checksum passed, so this is a firmware that wrote
        # something structurally odd rather than a transmission fault.
        return _invalid(
            "The EDID passed its checksum but could not be unpacked, which means "
            "the display wrote something the standard does not describe.",
            length,
        )


def _parse_checked(blob: bytes, length: int) -> Edid:
    edid = Edid(valid=True, raw_length=length)

    # Bytes 8-9: three five-bit letters packed big-endian, A=1.
    packed = struct.unpack_from(">H", blob, 8)[0]
    letters = [
        (packed >> 10) & 0x1F,
        (packed >> 5) & 0x1F,
        packed & 0x1F,
    ]
    edid.manufacturer = "".join(
        chr(ord("A") + value - 1) if 1 <= value <= 26 else "?" for value in letters
    )

    edid.product_code = struct.unpack_from("<H", blob, 10)[0]
    serial = struct.unpack_from("<I", blob, 12)[0]
    edid.serial_number = serial if serial else None

    week = blob[16]
    year = blob[17]
    if week == 0xFF:
        # The standard reuses the week byte as a flag: the year is the model
        # year rather than a manufacture date.
        edid.is_model_year = True
        edid.manufacture_week = None
    elif 1 <= week <= 54:
        edid.manufacture_week = week
    if year:
        edid.manufacture_year = year + 1990

    edid.version = f"{blob[18]}.{blob[19]}"

    video_input = blob[20]
    edid.digital = bool(video_input & 0x80)
    if edid.digital:
        edid.bit_depth = BIT_DEPTHS.get((video_input >> 4) & 0x07)
        edid.interface = DIGITAL_INTERFACES.get(video_input & 0x0F, "Unknown")
    else:
        edid.interface = "Analogue"

    edid.width_cm = blob[21] or None
    edid.height_cm = blob[22] or None
    if blob[23] != 0xFF:
        edid.gamma = (blob[23] + 100) / 100.0

    features = blob[24]
    names = []
    if features & 0x80:
        names.append("standby")
    if features & 0x40:
        names.append("suspend")
    if features & 0x20:
        names.append("active off")
    if features & 0x04:
        names.append("sRGB default colour space")
    if features & 0x02:
        names.append("preferred timing in first descriptor")
    if features & 0x01:
        names.append("continuous frequency")
    edid.features = names

    edid.established = _established(blob)
    edid.standard = _standard(blob, (blob[18], blob[19]))
    _descriptors(blob, edid)
    _extensions(blob, edid)
    return edid


def _established(blob: bytes) -> list[str]:
    out = []
    for offset, bit, name in ESTABLISHED_TIMINGS:
        if blob[35 + offset] & (1 << bit):
            out.append(name)
    return out


def _standard(blob: bytes, version: tuple[int, int] = (1, 3)) -> list[str]:
    ratios = ASPECT_RATIOS if version >= (1, 3) else ASPECT_RATIOS_PRE_1_3
    out = []
    for index in range(8):
        first = blob[38 + index * 2]
        second = blob[39 + index * 2]
        # 0x01 0x01 is the documented "unused" marker.
        if first == 0x01 and second == 0x01:
            continue
        if first == 0x00:
            continue
        width = (first + 31) * 8
        ratio = ratios.get((second >> 6) & 0x03)
        refresh = (second & 0x3F) + 60
        if ratio:
            height = width * ratio[1] // ratio[0]
            out.append(f"{width}x{height} @ {refresh} Hz")
        else:
            out.append(f"{width} wide @ {refresh} Hz")
    return out


def _descriptors(blob: bytes, edid: Edid) -> None:
    for index in range(4):
        start = 54 + index * 18
        block = blob[start : start + 18]
        if len(block) < 18:
            continue
        if block[0] == 0 and block[1] == 0:
            _monitor_descriptor(block, edid)
        else:
            timing = _detailed_timing(block, FROM_BASE)
            if timing is not None:
                edid.detailed.append(timing)


def _monitor_descriptor(block: bytes, edid: Edid) -> None:
    tag = block[3]
    payload = block[5:18]
    if tag in (TAG_NAME, TAG_SERIAL, TAG_TEXT):
        text = _ascii(payload)
        if not text:
            return
        if tag == TAG_NAME:
            edid.monitor_name = text
        elif tag == TAG_SERIAL:
            edid.monitor_serial = text
        else:
            edid.monitor_text.append(text)
    elif tag == TAG_RANGE_LIMITS:
        # Offsets in byte 4 extend the ranges past 255; ignoring them would
        # under-report a high refresh display by exactly 255 Hz.
        offsets = block[4]
        v_offset = 255 if offsets & 0x02 else 0
        h_offset = 255 if offsets & 0x08 else 0
        v_min, v_max = block[5], block[6] + v_offset
        h_min, h_max = block[7], block[8] + h_offset
        parts = []
        if v_min and v_max:
            parts.append(f"{v_min}-{v_max} Hz vertical")
        if h_min and h_max:
            parts.append(f"{h_min}-{h_max} kHz horizontal")
        if block[9] and block[9] != 0xFF:
            parts.append(f"up to {block[9] * 10} MHz pixel clock")
        edid.range_limits = ", ".join(parts)


def _ascii(payload: bytes) -> str:
    """Descriptor text is space padded and terminated by 0x0A."""
    text = payload.split(b"\x0a")[0]
    return "".join(
        chr(byte) for byte in text if 32 <= byte < 127
    ).strip()


def _detailed_timing(block: bytes, source: str = FROM_BASE) -> Timing | None:
    """One 18-byte detailed timing descriptor, base block or CTA extension.

    The pixel clock is sixteen bits of 10 kHz units, which stops at 655.35 MHz.
    Every mode above that - 1440p over about 150 Hz, 4K over about 75 Hz - has
    to be described somewhere else, which is the whole reason the DisplayID
    descriptor below exists.
    """
    clock = struct.unpack_from("<H", block, 0)[0]
    if clock == 0:
        return None
    pixel_clock_khz = clock * 10

    h_active = block[2] | ((block[4] & 0xF0) << 4)
    h_blank = block[3] | ((block[4] & 0x0F) << 8)
    v_active = block[5] | ((block[7] & 0xF0) << 4)
    v_blank = block[6] | ((block[7] & 0x0F) << 8)

    if not h_active or not v_active:
        return None

    h_total = h_active + h_blank
    v_total = v_active + v_blank
    refresh = None
    if h_total and v_total:
        refresh = (pixel_clock_khz * 1000.0) / (h_total * v_total)

    width_mm = block[12] | ((block[14] & 0xF0) << 4)
    height_mm = block[13] | ((block[14] & 0x0F) << 8)

    return Timing(
        width=h_active,
        height=v_active,
        pixel_clock_khz=pixel_clock_khz,
        refresh_hz=refresh,
        interlaced=bool(block[17] & 0x80),
        width_mm=width_mm or None,
        height_mm=height_mm or None,
        source=source,
    )


# --------------------------------------------------------------------------
# Extension blocks
# --------------------------------------------------------------------------


def _extensions(blob: bytes, edid: Edid) -> None:
    """Read every 128-byte block after the base one.

    Each block is checksum-verified before any of it is believed, exactly as
    the base block was, and each is parsed into a scratch result that is only
    merged once it has come back whole. A corrupt or unparseable block is named
    and dropped; it cannot half-fill the timing lists and it cannot stop the
    blocks after it being read.
    """
    declared = blob[126]
    edid.extensions = declared
    if not declared:
        return
    available = max(0, len(blob) // BLOCK_SIZE - 1)
    if available < declared:
        missing = declared - available
        edid.extension_notes.append(
            f"The base block says {declared} further block"
            f"{'s' if declared != 1 else ''} follow it, but {missing} of them did "
            "not arrive, so part of this display's description was not read. A "
            "short read, or an adapter that passes only the first block through, "
            "looks like this."
        )

    for index in range(min(declared, available)):
        start = BLOCK_SIZE * (index + 1)
        block = blob[start : start + BLOCK_SIZE]
        if len(block) < BLOCK_SIZE:
            break
        tag = block[0]
        name = EXTENSION_NAMES.get(tag, f"Unrecognised block, tag 0x{tag:02x}")
        if not checksum_ok(block):
            edid.extension_notes.append(
                f"Block {index + 1} ({name}) failed its own checksum, so nothing "
                "from it is shown."
            )
            continue
        scratch = Edid()
        try:
            if tag == EXT_CTA:
                name = _parse_cta(block, scratch)
            elif tag == EXT_DISPLAYID:
                name = _parse_displayid(block, scratch)
        except Exception:  # noqa: BLE001 - deliberately broad, see below
            # The bytes were intact, so this is a firmware that laid its own
            # block out in a way the standard does not describe. The scratch
            # copy is discarded whatever state it reached, and the block is
            # named as unread rather than being half-believed. Broad because
            # this module's contract is that it never raises, and an extension
            # block is arbitrary vendor data.
            edid.extension_notes.append(
                f"Block {index + 1} ({name}) passed its checksum but could not be "
                "unpacked, so nothing from it is shown."
            )
            continue
        edid.extension_blocks.append(name)
        _merge(edid, scratch)


def _merge(edid: Edid, scratch: Edid) -> None:
    """Fold a successfully parsed extension block into the result."""
    edid.cta_detailed.extend(scratch.cta_detailed)
    edid.cta_video.extend(scratch.cta_video)
    edid.cta_vics.extend(scratch.cta_vics)
    edid.cta_unknown_vics.extend(scratch.cta_unknown_vics)
    edid.displayid_detailed.extend(scratch.displayid_detailed)
    edid.extension_notes.extend(scratch.extension_notes)
    for name in scratch.cta_features:
        if name not in edid.cta_features:
            edid.cta_features.append(name)
    for name in scratch.undecoded:
        if name not in edid.undecoded:
            edid.undecoded.append(name)


# -- CTA-861, extension tag 0x02 -------------------------------------------


def _parse_cta(block: bytes, edid: Edid) -> str:
    """A CTA-861 extension block.

    Byte 1 is the revision, byte 2 is the offset at which the detailed timing
    descriptors start, and byte 3 carries four capability bits and, in its low
    nibble, a count of native formats. Everything between byte 4 and the DTD
    offset is the data block collection, which from revision 3 holds the video,
    audio and vendor blocks.
    """
    revision = block[1]
    dtd_offset = block[2]
    flags = block[3]
    name = f"CTA-861 revision {revision}"

    if revision >= 2:
        for bit, text in (
            (0x80, "underscan by default"),
            (0x40, "basic audio"),
            (0x20, "YCbCr 4:4:4"),
            (0x10, "YCbCr 4:2:2"),
        ):
            if flags & bit:
                edid.cta_features.append(text)

    if revision >= 3 and 5 <= dtd_offset <= BLOCK_SIZE - 1:
        _cta_data_blocks(block, 4, dtd_offset, edid)

    if revision >= 2:
        # A zero offset is the documented way of saying there are none.
        if dtd_offset == 0:
            return name
        start = dtd_offset if dtd_offset >= 4 else 4
    else:
        start = 4

    offset = start
    # Byte 127 is the block's checksum, so a descriptor has to end by 126.
    while offset + 18 <= BLOCK_SIZE - 1:
        descriptor = block[offset : offset + 18]
        # In a CTA block there are no monitor descriptors: a zero pixel clock
        # is the end of the list, and the rest is padding.
        if descriptor[0] == 0 and descriptor[1] == 0:
            break
        timing = _detailed_timing(descriptor, FROM_CTA)
        if timing is not None:
            edid.cta_detailed.append(timing)
        offset += 18
    return name


def _cta_data_blocks(block: bytes, start: int, end: int, edid: Edid) -> None:
    """Walk the data block collection between *start* and the DTD offset."""
    offset = start
    while offset < end:
        header = block[offset]
        if header == 0:
            break  # padding, not a data block
        tag = (header >> 5) & 0x07
        length = header & 0x1F
        if offset + 1 + length > end:
            break  # the collection claims to run past its own end
        payload = block[offset + 1 : offset + 1 + length]
        if tag == CTA_TAG_VIDEO:
            _cta_video_block(payload, edid)
        elif tag == CTA_TAG_EXTENDED and payload:
            extended = CTA_TIMING_BLOCKS.get(payload[0])
            if extended is not None and extended not in edid.undecoded:
                edid.undecoded.append(extended)
        offset += 1 + length


def _cta_video_block(payload: bytes, edid: Edid) -> None:
    """Short video descriptors: one byte per video identification code.

    Values 129 to 192 are codes 1 to 64 with the top bit set to mark the
    display's own preferred format; every other value is the code itself.
    """
    for byte in payload:
        if byte == 0:
            continue
        vic = byte - 128 if 129 <= byte <= 192 else byte
        if vic in edid.cta_vics:
            continue
        edid.cta_vics.append(vic)
        mode = VIC_MODES.get(vic)
        if mode is None:
            edid.cta_unknown_vics.append(vic)
            continue
        width, height, refresh, interlaced = mode
        edid.cta_video.append(
            Timing(
                width=width,
                height=height,
                pixel_clock_khz=0,
                refresh_hz=refresh,
                interlaced=interlaced,
                source=FROM_CTA_VIC,
            )
        )


# -- DisplayID, extension tag 0x70 -----------------------------------------


def _parse_displayid(block: bytes, edid: Edid) -> str:
    """A DisplayID structure carried inside an EDID extension block.

    Byte 0 is the 0x70 extension tag. The DisplayID structure itself begins at
    byte 1 with its version, then the length of its data block payload, the
    primary use case and an extension count; the payload runs from byte 5 and
    is followed by the DisplayID structure's own checksum, which covers the
    structure and not the surrounding EDID block. Both checksums have to pass.
    """
    version = block[1]
    size = block[2]
    major = (version >> 4) & 0x0F
    name = f"DisplayID {major}.{version & 0x0F}"

    # Five header bytes, the payload, then one checksum byte, all of which have
    # to fit before the EDID block's own checksum at byte 127.
    if size > BLOCK_SIZE - 7:
        edid.extension_notes.append(
            f"{name}: the block states a length that does not fit inside it, so "
            "nothing from it is shown."
        )
        return name
    if sum(block[1 : 6 + size]) % 256 != 0:
        edid.extension_notes.append(
            f"{name}: the block's own checksum does not match, so nothing from it "
            "is shown."
        )
        return name

    undecodable = DISPLAYID_TIMING_BLOCKS.get(major, {})
    if major not in DISPLAYID_TIMING_BLOCKS:
        # Still parsed, not abandoned: the timing block tags are what decide how
        # to read a descriptor, and the kernel dispatches on them without
        # consulting the version at all. But a version this parser has never
        # seen is worth saying out loud, because anything it numbers differently
        # will pass by unread.
        edid.extension_notes.append(
            f"{name}: this parser knows DisplayID 1.x and 2.0, and this block "
            f"declares version {major}. Its detailed timings were read if they "
            "use a tag this parser recognises; anything else in it was not."
        )

    payload = block[5 : 5 + size]
    offset = 0
    # Every DisplayID data block is a three-byte header - tag, revision,
    # payload length - and then that many bytes.
    while offset + 3 <= len(payload):
        tag = payload[offset]
        length = payload[offset + 2]
        if tag == 0 and length == 0:
            break  # padding to the end of the section
        data = payload[offset + 3 : offset + 3 + length]
        if len(data) < length:
            break  # a block that runs past the section it lives in
        if tag in DISPLAYID_DETAILED_TAGS:
            khz_per_unit = DISPLAYID_DETAILED_TAGS[tag]
            for index in range(length // DISPLAYID_DESCRIPTOR):
                base = index * DISPLAYID_DESCRIPTOR
                timing = _displayid_timing(
                    data[base : base + DISPLAYID_DESCRIPTOR], khz_per_unit, name
                )
                if timing is not None:
                    edid.displayid_detailed.append(timing)
        elif tag in undecodable:
            text = undecodable[tag]
            if text not in edid.undecoded:
                edid.undecoded.append(text)
        elif 0x20 <= tag <= 0x2F:
            # DisplayID 2.0 numbers its own data blocks in this range - the one
            # tag confirmed here against the kernel, Type VII's 0x22, sits in
            # it. Anything else in the range is reported by number, because
            # naming it would mean asserting a mapping this parser has not
            # verified, and a wrong name is worse than an honest tag.
            text = f"DisplayID 2.0 data block, tag 0x{tag:02x}, not decoded"
            if text not in edid.undecoded:
                edid.undecoded.append(text)
        offset += 3 + length
    return name


def _displayid_timing(desc: bytes, khz_per_unit: int, source: str) -> Timing | None:
    """One twenty-byte DisplayID detailed timing descriptor.

    DisplayID 1.x calls this Type I and DisplayID 2.0 calls it Type VII. The
    twenty bytes are laid out identically and only the pixel clock unit
    differs: 10 kHz in Type I, 1 kHz in Type VII. Every count in the descriptor
    is stored one less than its real value, which is what every ``1 +`` below
    is undoing.

    Byte 3 carries the aspect ratio in its low nibble, the interlace flag at
    bit 4 and the stereo mode at bits 5 and 6; only the interlace flag is
    wanted here. The three-byte clock is why this descriptor matters: it counts
    single kilohertz, so it reaches far past the 655.35 MHz ceiling of the
    18-byte descriptor and can state 2560x1440 at 280 Hz, which that one
    cannot.
    """
    if len(desc) < DISPLAYID_DESCRIPTOR or not any(desc):
        return None
    clock_units = desc[0] | (desc[1] << 8) | (desc[2] << 16)
    pixel_clock_khz = (clock_units + 1) * khz_per_unit
    interlaced = bool(desc[3] & 0x10)

    h_active = 1 + (desc[4] | (desc[5] << 8))
    h_blank = 1 + (desc[6] | (desc[7] << 8))
    v_active = 1 + (desc[12] | (desc[13] << 8))
    v_blank = 1 + (desc[14] | (desc[15] << 8))

    h_total = h_active + h_blank
    v_total = v_active + v_blank
    if h_total <= 0 or v_total <= 0:
        return None
    refresh = (pixel_clock_khz * 1000.0) / (h_total * v_total)

    # A descriptor made of padding decodes to a 1x1 mode at an implausible
    # rate. Refusing that is the difference between saying nothing and saying
    # something untrue.
    if h_active < MIN_ACTIVE or v_active < MIN_ACTIVE:
        return None
    if not MIN_REFRESH <= refresh <= MAX_REFRESH:
        return None

    return Timing(
        width=h_active,
        height=v_active,
        pixel_clock_khz=pixel_clock_khz,
        refresh_hz=refresh,
        interlaced=interlaced,
        source=source,
    )
