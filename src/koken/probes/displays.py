# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Connected displays, one row 3 instance per output.

Each DRM connector directory carries the display's own EDID alongside the
connector's state, so this probe is mostly :mod:`koken.probes.edid` with a
sysfs directory listing around it.

The manufacture week and year are the fields worth having here. A display has
no other way of telling you how old it is - there is no menu for it and no
sticker that survives - and on a second-hand panel, or one replaced under
warranty, the answer is frequently not the one the owner expected.
"""

from __future__ import annotations

from . import edid as edid_module
from .base import (
    NONE_PRESENT,
    NOT_AVAILABLE,
    NOT_REPORTED,
    VOLATILE,
    Probe,
    Section,
    fmt_list,
    glob_dirs,
    or_missing,
    read_bytes,
    read_first_line,
    read_lines,
)

DRM_ROOT = "/sys/class/drm"

# How many entries a list may have before it stops being a value and becomes
# an expansion body. CORE 11: the value column is one line.
INLINE_LIMIT = 3

# Three-letter PNP ids that turn up often enough to be worth naming without a
# database. hwdata ships pnp.ids, but it is not one of this project's declared
# dependencies, so only the certain cases are named and the rest stay raw.
KNOWN_VENDORS = {
    "SAM": "Samsung",
    "GSM": "LG",
    "AUS": "ASUS",
    "ACI": "ASUS",
    "DEL": "Dell",
    "ACR": "Acer",
    "BNQ": "BenQ",
    "AOC": "AOC",
    "MSI": "MSI",
    "HPN": "HP",
    "HWP": "HP",
    "LEN": "Lenovo",
    "PHL": "Philips",
    "VSC": "ViewSonic",
    "APP": "Apple",
    "EIZ": "EIZO",
    "IVM": "Iiyama",
    "SHP": "Sharp",
    "BOE": "BOE",
    "CMN": "Chi Mei Innolux",
    "LGD": "LG Display",
    "AUO": "AU Optronics",
}


class DisplaysProbe(Probe):
    branch = "hardware"
    id = "displays"
    label = "Displays"

    def __init__(self, context=None):
        super().__init__(context)
        self._connectors: list[dict] = []

    # -- enumeration ------------------------------------------------------

    def _find_connectors(self) -> list[dict]:
        found = []
        for path in glob_dirs(f"{DRM_ROOT}/card[0-9]*-*"):
            status = read_first_line(path / "status")
            found.append(
                {
                    "path": path,
                    "name": path.name,
                    "short": path.name.split("-", 1)[1] if "-" in path.name else path.name,
                    "status": status,
                    "connected": (status or "").lower() == "connected",
                }
            )
        return found

    def sections(self) -> list[Section]:
        connectors = self._find_connectors()
        self._connectors = connectors
        connected = [item for item in connectors if item["connected"]]

        if not connected:
            if not connectors:
                return [
                    self.empty_section(
                        "overview",
                        "Overview",
                        "No display connectors were found. A machine with no graphics "
                        "driver loaded looks like this.",
                    )
                ]
            section = Section(id="overview", label="Overview")
            section.add(
                self.row(
                    "none_connected",
                    "Connected displays",
                    "None. Nothing is plugged into any output.",
                )
            )
            section.add(
                self.row(
                    "connectors",
                    "Outputs on this machine",
                    fmt_list([item["short"] for item in connectors]),
                )
            )
            return [section]

        return [self._connector_section(item) for item in connected]

    def _connector_section(self, item) -> Section:
        path = item["path"]
        parsed = edid_module.parse(read_bytes(path / "edid"))
        section = Section(
            id=item["name"],
            label=self._label(item, parsed),
            icon="display",
        )

        section.add(self.row("connector", "Connector", item["short"]))
        section.add(
            self.row("status", "Status", or_missing(item["status"], NOT_AVAILABLE), tier=VOLATILE)
        )
        enabled = read_first_line(path / "enabled")
        if enabled:
            section.add(self.row("enabled", "Enabled", enabled, tier=VOLATILE))
        dpms = read_first_line(path / "dpms")
        if dpms:
            section.add(self.row("dpms", "Power state", dpms, tier=VOLATILE))

        if not parsed.valid:
            section.add(self.row("edid", "Display details", parsed.error))
            for row in self._mode_rows(path, parsed):
                section.add(row)
            return section

        for row in self._edid_rows(parsed):
            section.add(row)
        for row in self._mode_rows(path, parsed):
            section.add(row)
        return section

    def _label(self, item, parsed) -> str:
        if parsed.valid and parsed.monitor_name:
            name = parsed.monitor_name
            return name if len(name) <= 24 else name[:23] + "…"
        return item["short"]

    # -- rows -------------------------------------------------------------

    def _edid_rows(self, parsed) -> list:
        rows = []
        vendor = KNOWN_VENDORS.get(parsed.manufacturer)
        rows.append(
            self.row(
                "name",
                "Model",
                or_missing(parsed.monitor_name, NOT_REPORTED),
            )
        )
        rows.append(
            self.row(
                "manufacturer",
                "Manufacturer",
                f"{parsed.manufacturer} ({vendor})" if vendor else
                f"{parsed.manufacturer} — three-letter PNP identifier, not in the "
                "local database",
            )
        )
        rows.append(
            self.row(
                "product_code",
                "Product code",
                f"{parsed.product_code:04x}" if parsed.product_code is not None else NOT_REPORTED,
            )
        )
        rows.append(
            self.row(
                "serial",
                "Serial number",
                parsed.monitor_serial
                or (str(parsed.serial_number) if parsed.serial_number else NOT_REPORTED),
            )
        )
        rows.append(self.row("manufactured", "Manufactured", _manufactured(parsed)))
        rows.append(self.row("edid_version", "EDID version", or_missing(parsed.version)))

        interface = parsed.interface or NOT_REPORTED
        if parsed.digital and parsed.bit_depth:
            interface = f"{interface}, {parsed.bit_depth} bits per colour"
        rows.append(self.row("interface", "Interface", interface))

        if parsed.width_cm and parsed.height_cm:
            diagonal = parsed.diagonal_inches
            rows.append(
                self.row(
                    "size",
                    "Physical size",
                    f"{parsed.width_cm} × {parsed.height_cm} cm"
                    + (f" — about {diagonal:.0f} inches diagonal" if diagonal else ""),
                )
            )
        else:
            rows.append(
                self.row(
                    "size",
                    "Physical size",
                    "Not reported. A projector, or a display that declines to say.",
                )
            )

        if parsed.gamma:
            rows.append(self.row("gamma", "Gamma", f"{parsed.gamma:.2f}"))

        native = parsed.native
        if native is not None:
            rows.append(self.row("native_mode", "Native mode", native.describe()))
            if native.pixel_clock_khz:
                rows.append(
                    self.row(
                        "pixel_clock",
                        "Pixel clock",
                        f"{native.pixel_clock_khz / 1000:.2f} MHz",
                    )
                )
            density = _density(native, parsed)
            if density:
                rows.append(self.row("density", "Pixel density", f"{density:.0f} ppi"))

        preferred = parsed.preferred
        if preferred is not None and preferred is not native:
            # The base block names its first descriptor as the preferred
            # timing, and on a display whose best mode needs more pixel clock
            # than that descriptor can carry, the preferred timing is not the
            # best mode. Both are shown rather than one standing in for the
            # other and being wrong about the panel.
            rows.append(
                self.row(
                    "preferred_mode",
                    "Preferred timing",
                    preferred.describe(),
                    gloss="the mode the base block names first",
                )
            )

        others = sorted(
            _unique(parsed.detailed_timings, skip=native),
            key=edid_module.native_rank,
            reverse=True,
        )
        if others:
            rows.append(
                self.row(
                    "detailed_modes",
                    "Other modes described",
                    _summary([timing.describe() for timing in others], "further mode"),
                    body=_body(
                        "Every other mode this display spells out in full, and the "
                        "part of its description each one was read from.",
                        _mode_lines(others),
                    ),
                )
            )
        if parsed.range_limits:
            rows.append(self.row("range_limits", "Supported range", parsed.range_limits))
        if parsed.standard:
            rows.append(
                self.row("standard_modes", "Standard timings", fmt_list(parsed.standard))
            )
        if parsed.established:
            rows.append(
                self.row(
                    "established_modes", "Established timings", fmt_list(parsed.established)
                )
            )
        if parsed.features:
            rows.append(self.row("features", "Features", fmt_list(parsed.features)))
        for text in parsed.monitor_text:
            rows.append(self.row("edid_text", "Descriptor text", text, key=f"text{text}"))
        formats = _format_lines(parsed)
        if formats:
            rows.append(
                self.row(
                    "cta_formats",
                    "Accepted video formats",
                    _summary(formats, "format"),
                    body=_body(
                        "Formats the display tells a source it will accept, listed "
                        "in the CTA-861 extension block as numbered codes rather "
                        "than as timings. Accepting a format is not the same as "
                        "having a panel that size - a television that takes 2160p "
                        "and scales it down says so here - which is why these do "
                        "not decide the native mode.",
                        formats,
                    ),
                )
            )

        if parsed.extensions or parsed.extension_notes:
            rows.append(self._extension_row(parsed))
        return rows

    def _extension_row(self, parsed):
        value = str(parsed.extensions)
        if parsed.extension_blocks:
            value = f"{parsed.extensions} — {fmt_list(parsed.extension_blocks)}"
        lines = list(parsed.extension_notes)
        if parsed.undecoded:
            lines.append(
                "Timing blocks found here and deliberately not decoded: "
                + fmt_list(parsed.undecoded)
                + ". They state their modes in layouts KOKEN does not read, so a "
                "mode described only in one of them is missing from the lists "
                "above. KOKEN names them rather than guessing at them."
            )
        return self.row(
            "extensions",
            "Extension blocks",
            value,
            body=_body(
                "The 128-byte blocks that follow the first one. The descriptor the "
                "first block uses cannot express a pixel clock above 655.35 MHz, "
                "which is not enough for 1440p much above 150 Hz, so a fast "
                "display states its best modes in these blocks instead. KOKEN "
                "reads them, and checks each one's checksum before believing any "
                "of it.",
                lines,
            ),
        )

    def _mode_rows(self, path, parsed=None) -> list:
        """The kernel's own mode list, opened out rather than elided.

        ``modes`` names every mode by resolution alone, so a display that runs
        2560x1440 at five refresh rates prints that name five times and a
        single line of them reads as a fault. The count stays on the value
        line and the list goes in the row's own expansion body, one resolution
        per line, with the refresh rates the EDID gives for it.
        """
        modes = read_lines(path / "modes")
        if not modes:
            return [
                self.row(
                    "modes",
                    "Modes offered",
                    "This connector lists no modes.",
                )
            ]
        groups = _group_modes(modes)
        rates = parsed.refresh_map() if parsed is not None and parsed.valid else {}

        lines = []
        derived = False
        for name, count in groups:
            line = f"{name} — {count} mode" + ("s" if count != 1 else "")
            known = rates.get(_resolution(name))
            if known:
                line += f"; the EDID states {_rates(known)}"
            else:
                derived = True
            lines.append(line)

        lead = (
            "The kernel names every mode by its resolution alone, so a resolution "
            "the display runs at more than one refresh rate appears once per rate."
        )
        if not rates:
            # Either the EDID could not be read at all, or it described nothing
            # this connector went on to offer. Saying the rates were derived
            # would be a claim about a description that was never read.
            lead += (
                " The display's own description gave no rates to put against "
                "these, so the resolutions are shown on their own."
            )
        else:
            lead += (
                " The rates below are the ones this display's own description "
                "states at that resolution."
            )
            if derived:
                lead += (
                    " A resolution with no rates against it is one the kernel "
                    "worked out from the display's stated frequency range rather "
                    "than one the display spells out."
                )

        largest = max(groups, key=lambda group: _area(group[0]))[0]
        plural = "s" if len(groups) != 1 else ""
        return [
            self.row(
                "mode_count",
                "Modes offered",
                str(len(modes)),
                gloss=f"across {len(groups)} resolution{plural}",
            ),
            self.row(
                "modes",
                "Mode list",
                f"{len(groups)} resolution{plural}, largest {largest} — expand for "
                "the full list",
                body=_body(lead, lines),
            ),
        ]

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for item in self._connectors or self._find_connectors():
            if not item["connected"]:
                continue
            path = item["path"]
            rows = [
                self.row(
                    "status",
                    "Status",
                    or_missing(read_first_line(path / "status"), NOT_AVAILABLE),
                    tier=VOLATILE,
                )
            ]
            enabled = read_first_line(path / "enabled")
            if enabled:
                rows.append(self.row("enabled", "Enabled", enabled, tier=VOLATILE))
            dpms = read_first_line(path / "dpms")
            if dpms:
                rows.append(self.row("dpms", "Power state", dpms, tier=VOLATILE))
            out[item["name"]] = rows
        return out


def _manufactured(parsed) -> str:
    if parsed.manufacture_year is None:
        return NOT_REPORTED
    if parsed.is_model_year:
        return f"Model year {parsed.manufacture_year}"
    if parsed.manufacture_week:
        return f"Week {parsed.manufacture_week} of {parsed.manufacture_year}"
    return str(parsed.manufacture_year)


def _density(timing, parsed=None) -> float | None:
    """Pixels per inch, from whichever physical size the display gives.

    The 18-byte descriptor carries the image size in millimetres. A DisplayID
    descriptor carries none at all, so when the best mode came from one of
    those the size is taken from a descriptor that has it, and failing that
    from the base block - which states the panel in whole centimetres, and is
    therefore the last choice rather than the first. It is the same panel
    either way.
    """
    width_mm, height_mm = timing.width_mm, timing.height_mm
    if not width_mm or not height_mm:
        for other in parsed.detailed_timings if parsed is not None else []:
            if other.width_mm and other.height_mm:
                width_mm, height_mm = other.width_mm, other.height_mm
                break
    if not width_mm or not height_mm:
        if parsed is not None and parsed.width_cm and parsed.height_cm:
            width_mm, height_mm = parsed.width_cm * 10, parsed.height_cm * 10
    if not width_mm or not height_mm:
        return None
    diagonal_mm = (width_mm**2 + height_mm**2) ** 0.5
    diagonal_px = (timing.width**2 + timing.height**2) ** 0.5
    if diagonal_mm <= 0:
        return None
    return diagonal_px / (diagonal_mm / 25.4)


def _key(timing) -> tuple:
    return (
        timing.width,
        timing.height,
        round(timing.refresh_hz or 0.0, 2),
        timing.interlaced,
    )


def _unique(timings, skip=None) -> list:
    """Modes with the duplicates dropped, first sighting winning.

    A display that describes the same mode in two of its blocks is common and
    is not interesting to look at twice. *skip* drops a mode that is already
    shown elsewhere - the native one - and drops any later copy of it too,
    which is the case that matters: a mode stated in both the base block and
    an extension would otherwise appear as native and again as "other".
    """
    out, seen = [], set()
    if skip is not None:
        seen.add(_key(skip))
    for timing in timings:
        key = _key(timing)
        if key in seen:
            continue
        seen.add(key)
        out.append(timing)
    return out


def _mode_lines(timings) -> list[str]:
    """One line per mode, naming the block that described it."""
    return [
        timing.describe() + (f" — {timing.source}" if timing.source else "")
        for timing in timings
    ]


def _format_lines(parsed) -> list[str]:
    """The CTA video codes, named where the local table has them."""
    lines = [timing.describe() for timing in _unique(parsed.cta_video)]
    lines += [
        f"Code {vic} — not in the local table, so KOKEN does not name it"
        for vic in parsed.cta_unknown_vics
    ]
    return lines


def _summary(items: list[str], noun: str) -> str:
    """The value line for a list that opens downward.

    A short list reads better in place than behind a chevron. Anything longer
    is a count and an invitation, because the value line is one line and
    elides, which is exactly the fault this avoids.
    """
    if not items:
        return NONE_PRESENT
    if len(items) <= INLINE_LIMIT:
        return fmt_list(items)
    return f"{len(items)} {noun}s — expand for the full list"


def _body(lead: str, lines: list[str]) -> str:
    """A row's own expansion body: a sentence of context, then the list."""
    if not lines:
        return lead
    return lead + "\n\n" + "\n".join(lines)


def _group_modes(modes: list[str]) -> list[tuple[str, int]]:
    """Mode names and how many times each appears, in the order offered."""
    order: list[str] = []
    counts: dict[str, int] = {}
    for name in modes:
        if name not in counts:
            counts[name] = 0
            order.append(name)
        counts[name] += 1
    return [(name, counts[name]) for name in order]


def _resolution(name: str) -> tuple[int, int, bool] | None:
    """A DRM mode name is WIDTHxHEIGHT, with an i appended when interlaced.

    The i is kept, as the third element of the key, because the rates an
    interlaced mode runs at are not the rates its progressive namesake does.
    """
    interlaced = name.endswith("i")
    text = name[:-1] if interlaced else name
    width, _, height = text.partition("x")
    try:
        return (int(width), int(height), interlaced)
    except ValueError:
        return None


def _area(name: str) -> int:
    resolution = _resolution(name)
    return resolution[0] * resolution[1] if resolution else 0


def _rates(values) -> str:
    """Refresh rates as a readable series: 280, 240, 165 and 59.95 Hz."""
    texts = [f"{value:.2f}".rstrip("0").rstrip(".") for value in values]
    if len(texts) == 1:
        return f"{texts[0]} Hz"
    return ", ".join(texts[:-1]) + f" and {texts[-1]} Hz"
