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
            for row in self._mode_rows(path):
                section.add(row)
            return section

        for row in self._edid_rows(parsed):
            section.add(row)
        for row in self._mode_rows(path):
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
            rows.append(
                self.row(
                    "pixel_clock",
                    "Pixel clock",
                    f"{native.pixel_clock_khz / 1000:.2f} MHz",
                )
            )
            if native.width_mm and native.height_mm:
                density = _density(native)
                if density:
                    rows.append(self.row("density", "Pixel density", f"{density:.0f} ppi"))

        if len(parsed.detailed) > 1:
            rows.append(
                self.row(
                    "detailed_modes",
                    "Other detailed modes",
                    fmt_list(timing.describe() for timing in parsed.detailed[1:]),
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
        if parsed.extensions:
            rows.append(
                self.row(
                    "extensions",
                    "Extension blocks",
                    f"{parsed.extensions} — high refresh and HDR modes live in these, "
                    "and are not shown here",
                )
            )
        return rows

    def _mode_rows(self, path) -> list:
        modes = read_lines(path / "modes")
        if not modes:
            return [
                self.row(
                    "modes",
                    "Modes offered",
                    "This connector lists no modes.",
                )
            ]
        return [
            self.row("mode_count", "Modes offered", str(len(modes))),
            self.row("modes", "Mode list", fmt_list(modes)),
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


def _density(timing) -> float | None:
    """Pixels per inch, from the physical size in the timing descriptor."""
    if not timing.width_mm or not timing.height_mm:
        return None
    diagonal_mm = (timing.width_mm**2 + timing.height_mm**2) ** 0.5
    diagonal_px = (timing.width**2 + timing.height**2) ** 0.5
    if diagonal_mm <= 0:
        return None
    return diagonal_px / (diagonal_mm / 25.4)
