# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Reading the block of bytes a monitor tells the computer about itself.

``/sys/class/drm/card0-DP-1/edid`` is world readable and holds the display's
own description of itself: who made it, when, how big it is and what it can
show. It is 128 bytes of packed fields from 1994, extended a few times since,
and unpacking it is the whole job - no library, no external tool.

Nothing here is trusted before it is checked. The eight-byte header and the
checksum are both verified first, because a blob can legitimately be
zero-length (nothing plugged in), truncated (a marginal cable), or all zeroes
(a KVM switch that answers reads without passing them through). A blob that
fails either check produces a result that says so, and the display probe shows
that sentence instead of inventing a monitor.
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


@dataclass
class Timing:
    """One detailed timing descriptor - a mode the display actually supports."""

    width: int
    height: int
    pixel_clock_khz: int
    refresh_hz: float | None
    interlaced: bool = False
    width_mm: int | None = None
    height_mm: int | None = None

    def describe(self) -> str:
        text = f"{self.width}x{self.height}"
        if self.refresh_hz:
            text += f" @ {self.refresh_hz:.2f} Hz".replace(".00 Hz", " Hz")
        if self.interlaced:
            text += " interlaced"
        return text


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
    extensions: int = 0

    @property
    def native(self) -> Timing | None:
        """The first detailed timing, which by convention is the native mode."""
        return self.detailed[0] if self.detailed else None

    @property
    def diagonal_inches(self) -> float | None:
        if not self.width_cm or not self.height_cm:
            return None
        return ((self.width_cm**2 + self.height_cm**2) ** 0.5) / 2.54


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
    edid.extensions = blob[126]
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
            timing = _detailed_timing(block)
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


def _detailed_timing(block: bytes) -> Timing | None:
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
    )
