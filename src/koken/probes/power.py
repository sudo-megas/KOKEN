# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Power: what is supplying it, and what is storing it.

``Power -> Battery`` renders on a desktop. It has to: CORE says absent hardware
is stated and never hidden, and a section that vanishes on machines without a
battery would mean the layout moves depending on what is plugged in. On a
machine with no battery the section says so, and says how to tell the
difference between "there is no battery" and "there is one and it is not being
reported".

Battery health is ``charge_full / charge_full_design``, and it is the number
worth having. Every battery loses capacity; the question is how much, and the
percentage the desktop shows is a percentage of what the battery can hold
*today*, not of what it could hold when it was new. A battery at 62% health
showing 100% charge is full, and holds under two thirds of what it used to.

Some batteries report charge in microampere-hours and others report energy in
microwatt-hours. Both are handled, because a laptop that reports energy would
otherwise show nothing at all.
"""

from __future__ import annotations

from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_duration,
    fmt_list,
    fmt_percent,
    list_dir,
    or_missing,
    path_exists,
    read_first_line,
    read_int,
)

SUPPLY_ROOT = "/sys/class/power_supply"

# Below this, a battery is worn enough to be worth flagging.
HEALTH_WARNING = 80.0

STATUS_TEXT = {
    "Charging": "Charging",
    "Discharging": "Discharging — running on battery",
    "Full": "Full",
    "Not charging": "Not charging — held at its current level on purpose",
    "Unknown": "Unknown",
}


class PowerProbe(Probe):
    branch = "peripherals"
    id = "power"
    label = "Power"

    def _find_supplies(self) -> list[dict]:
        supplies = []
        for path in list_dir(SUPPLY_ROOT):
            kind = read_first_line(path / "type")
            supplies.append(
                {
                    "name": path.name,
                    "path": path,
                    "type": kind,
                    "is_battery": (kind or "").lower() == "battery",
                    "is_mains": (kind or "").lower() == "mains",
                }
            )
        return supplies

    def sections(self) -> list[Section]:
        supplies = self._find_supplies()
        return [
            self._overview(supplies),
            self._battery(supplies),
            self._supplies(supplies),
        ]

    # -- overview ---------------------------------------------------------

    def _overview(self, supplies) -> Section:
        section = Section(id="overview", label="Overview")
        if not supplies:
            section.add(
                self.row(
                    "absent",
                    "Power supplies",
                    "Nothing is listed under /sys/class/power_supply. A desktop "
                    "whose firmware reports no supply at all looks like this, and "
                    "so does a machine with the ACPI battery driver unloaded.",
                )
            )
            return section

        batteries = [item for item in supplies if item["is_battery"]]
        mains = [item for item in supplies if item["is_mains"]]

        section.add(
            self.row(
                "supply_count",
                "Power supplies",
                "{} in total — {} batteries, {} mains".format(
                    len(supplies), len(batteries), len(mains)
                ),
            )
        )
        for row in self._mains_rows(mains):
            section.add(row)
        section.add(
            self.row(
                "battery_present",
                "Batteries",
                fmt_list(item["name"] for item in batteries)
                if batteries
                else "None — this machine runs on mains power only",
            )
        )

        profile = read_first_line("/sys/firmware/acpi/platform_profile")
        if profile:
            choices = read_first_line("/sys/firmware/acpi/platform_profile_choices")
            section.add(
                self.row(
                    "platform_profile",
                    "Platform profile",
                    f"{profile} — of {choices}" if choices else profile,
                    tier=VOLATILE,
                )
            )
        return section

    def _mains_rows(self, mains) -> list:
        rows = []
        for item in mains:
            online = read_int(item["path"] / "online")
            rows.append(
                self.row(
                    "mains_online",
                    f"{item['name']}",
                    {
                        1: "Connected — the machine is on mains power",
                        0: "Not connected",
                    }.get(online, NOT_REPORTED),
                    tier=VOLATILE,
                    key=f"mains{item['name']}",
                )
            )
        return rows

    # -- battery ----------------------------------------------------------

    def _battery(self, supplies) -> Section:
        section = Section(id="battery", label="Battery")
        batteries = [item for item in supplies if item["is_battery"]]

        if not batteries:
            section.add(
                self.row(
                    "battery_absent",
                    "Battery",
                    "No battery is present on this machine.",
                )
            )
            section.add(
                self.row(
                    "battery_absent_detail",
                    "How this was determined",
                    "Nothing under /sys/class/power_supply reports a type of "
                    "Battery. On a desktop that is the expected answer. On a "
                    "portable machine it means the battery driver has not loaded, "
                    "and the firmware setting or the kernel module is the place to "
                    "look.",
                )
            )
            return section

        for battery in batteries:
            for row in self._battery_rows(battery):
                section.add(row)
        return section

    def _battery_rows(self, battery) -> list:
        path = battery["path"]
        prefix = battery["name"]
        rows = []

        present = read_int(path / "present")
        if present == 0:
            return [
                self.row(
                    "battery_removed",
                    prefix,
                    "The bay is present but no battery is in it.",
                    key=f"{prefix}removed",
                )
            ]

        rows.append(
            self.row(
                "battery_name",
                prefix,
                " · ".join(
                    part
                    for part in (
                        read_first_line(path / "manufacturer"),
                        read_first_line(path / "model_name"),
                        read_first_line(path / "technology"),
                    )
                    if part
                )
                or NOT_REPORTED,
                key=f"{prefix}name",
            )
        )

        rows.extend(self._battery_volatile_rows(battery))

        # Health: what it holds now against what it was built to hold.
        full, design, unit = _capacity_pair(path)
        if full and design:
            health = full * 100.0 / design
            rows.append(
                self.row(
                    "battery_health",
                    "  Health",
                    "{} — holds {} of an original {}".format(
                        fmt_percent(health, 1),
                        _quantity(full, unit),
                        _quantity(design, unit),
                    ),
                    severity=WARNING if health < HEALTH_WARNING else "normal",
                    key=f"{prefix}health",
                )
            )
        else:
            rows.append(
                self.row(
                    "battery_health",
                    "  Health",
                    "Not reported. This battery does not publish its design "
                    "capacity, so wear cannot be worked out.",
                    key=f"{prefix}health",
                )
            )

        cycles = read_int(path / "cycle_count")
        if cycles is not None:
            rows.append(
                self.row(
                    "battery_cycles",
                    "  Charge cycles",
                    str(cycles) if cycles else "Not counted by this battery",
                    key=f"{prefix}cycles",
                )
            )

        for field, label, name, divisor, suffix in (
            ("battery_voltage_design", "  Design voltage", "voltage_min_design", 1_000_000, " V"),
            ("battery_capacity_level", "  Capacity level", "capacity_level", None, ""),
        ):
            if divisor is None:
                value = read_first_line(path / name)
                if value:
                    rows.append(
                        self.row(field, label, value, key=f"{prefix}{name}")
                    )
                continue
            raw = read_int(path / name)
            if raw is not None:
                rows.append(
                    self.row(
                        field,
                        label,
                        f"{raw / divisor:.2f}{suffix}",
                        key=f"{prefix}{name}",
                    )
                )

        serial = read_first_line(path / "serial_number")
        if serial:
            rows.append(
                self.row("battery_serial", "  Serial number", serial, key=f"{prefix}serial")
            )
        return rows

    def _battery_volatile_rows(self, battery) -> list:
        path = battery["path"]
        prefix = battery["name"]
        rows = []

        capacity = read_int(path / "capacity")
        status = read_first_line(path / "status")
        rows.append(
            self.row(
                "battery_charge",
                "  Charge",
                f"{capacity}%" if capacity is not None else NOT_REPORTED,
                tier=VOLATILE,
                severity=WARNING if capacity is not None and capacity <= 10 else "normal",
                key=f"{prefix}charge",
            )
        )
        rows.append(
            self.row(
                "battery_status",
                "  Status",
                STATUS_TEXT.get(status or "", or_missing(status, NOT_REPORTED)),
                tier=VOLATILE,
                key=f"{prefix}status",
            )
        )

        voltage = read_int(path / "voltage_now")
        if voltage is not None:
            rows.append(
                self.row(
                    "battery_voltage",
                    "  Voltage",
                    f"{voltage / 1_000_000:.2f} V",
                    tier=VOLATILE,
                    key=f"{prefix}voltage",
                )
            )

        power = read_int(path / "power_now")
        current = read_int(path / "current_now")
        if power is not None:
            rows.append(
                self.row(
                    "battery_draw",
                    "  Drawing",
                    f"{power / 1_000_000:.2f} W",
                    tier=VOLATILE,
                    key=f"{prefix}draw",
                )
            )
        elif current is not None and voltage is not None:
            watts = (current / 1_000_000) * (voltage / 1_000_000)
            rows.append(
                self.row(
                    "battery_draw",
                    "  Drawing",
                    f"{watts:.2f} W — worked out from current and voltage",
                    tier=VOLATILE,
                    key=f"{prefix}draw",
                )
            )

        now, _design, unit = _capacity_pair(path, use_now=True)
        if now:
            rows.append(
                self.row(
                    "battery_stored",
                    "  Stored",
                    _quantity(now, unit),
                    tier=VOLATILE,
                    key=f"{prefix}stored",
                )
            )
        remaining = _time_remaining(now, power, current, status)
        if remaining:
            rows.append(
                self.row(
                    "battery_remaining",
                    "  Estimated time",
                    remaining,
                    tier=VOLATILE,
                    key=f"{prefix}remaining",
                )
            )
        return rows

    # -- supplies ---------------------------------------------------------

    def _supplies(self, supplies) -> Section:
        section = Section(id="supplies", label="Supplies")
        if not supplies:
            section.add(
                self.row(
                    "absent",
                    "Supplies",
                    "Nothing is listed under /sys/class/power_supply.",
                )
            )
            return section

        for item in supplies:
            path = item["path"]
            detail = [item["type"] or "Unknown type"]
            online = read_int(path / "online")
            if online is not None:
                detail.append("connected" if online else "not connected")
            status = read_first_line(path / "status")
            if status:
                detail.append(status.lower())
            scope = read_first_line(path / "scope")
            if scope:
                detail.append(f"scope {scope.lower()}")
            section.add(
                self.row(
                    "supply",
                    item["name"],
                    ", ".join(detail),
                    tier=VOLATILE,
                    key=f"supply{item['name']}",
                )
            )
            model = read_first_line(path / "model_name")
            if model:
                section.add(
                    self.row(
                        "supply_model",
                        "  Model",
                        model,
                        key=f"supplymodel{item['name']}",
                    )
                )
        return section

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        supplies = self._find_supplies()
        out: dict[str, list] = {}

        overview = self._mains_rows([item for item in supplies if item["is_mains"]])
        profile = read_first_line("/sys/firmware/acpi/platform_profile")
        if profile:
            choices = read_first_line("/sys/firmware/acpi/platform_profile_choices")
            overview.append(
                self.row(
                    "platform_profile",
                    "Platform profile",
                    f"{profile} — of {choices}" if choices else profile,
                    tier=VOLATILE,
                )
            )
        if overview:
            out["overview"] = overview

        battery_rows = []
        for battery in supplies:
            if not battery["is_battery"]:
                continue
            if read_int(battery["path"] / "present") == 0:
                continue
            battery_rows.extend(self._battery_volatile_rows(battery))
        if battery_rows:
            out["battery"] = battery_rows

        supply_rows = []
        for item in supplies:
            path = item["path"]
            detail = [item["type"] or "Unknown type"]
            online = read_int(path / "online")
            if online is not None:
                detail.append("connected" if online else "not connected")
            status = read_first_line(path / "status")
            if status:
                detail.append(status.lower())
            scope = read_first_line(path / "scope")
            if scope:
                detail.append(f"scope {scope.lower()}")
            supply_rows.append(
                self.row(
                    "supply",
                    item["name"],
                    ", ".join(detail),
                    tier=VOLATILE,
                    key=f"supply{item['name']}",
                )
            )
        if supply_rows:
            out["supplies"] = supply_rows
        return out


def _capacity_pair(path, use_now: bool = False) -> tuple[int | None, int | None, str]:
    """Current-or-full and design capacity, in whichever unit the battery uses."""
    for now_name, full_name, design_name, unit in (
        ("charge_now", "charge_full", "charge_full_design", "Ah"),
        ("energy_now", "energy_full", "energy_full_design", "Wh"),
    ):
        design = read_int(path / design_name)
        first = read_int(path / (now_name if use_now else full_name))
        if first is not None or design is not None:
            return first, design, unit
    return None, None, ""


def _quantity(microunits: int, unit: str) -> str:
    if not unit:
        return str(microunits)
    return f"{microunits / 1_000_000:.2f} {unit}"


def _time_remaining(stored, power, current, status) -> str | None:
    """Roughly how long, at the rate right now. Deliberately not precise."""
    if not stored or not status:
        return None
    rate = power or current
    if not rate:
        return None
    hours = stored / rate
    if status == "Discharging":
        return f"About {fmt_duration(hours * 3600)} left at the present rate"
    return None
