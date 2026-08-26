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

Not every battery in ``/sys/class/power_supply`` belongs to the machine. A
wireless mouse, keyboard, headset or controller registers its own, and the
kernel marks the difference with ``scope``: ``System`` for the pack in the
machine, ``Device`` for the one in the thing on the desk. Both are shown and
they are never mixed together, because they answer different questions - a
peripheral publishes a percentage and a charging state and nothing else, so
asking it for wear or for a time estimate produces either an empty row or a
confident number worked out from nothing.
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

# `scope` as the kernel writes it. `Device` means the battery belongs to a
# peripheral - hid-logitech-hidpp and the generic HID battery both set it -
# and `System`, or nothing at all, means the machine's own pack.
DEVICE_SCOPE = "device"

# Consulted only when `type` is missing or reads Unknown. An entry that
# publishes any of these is a battery whatever its type file says, and a
# battery that is listed but not recognised is the one failure mode this
# section cannot afford: the answer would be "no battery" on a machine that
# has one.
BATTERY_MARKERS = (
    "capacity",
    "capacity_level",
    "charge_now",
    "energy_now",
    "charge_full",
    "energy_full",
)

# What a draw figure, a stored figure and a time estimate are worked out from.
# A battery that publishes none of them gets none of those rows rather than a
# row of dashes for each.
RATE_FILES = ("power_now", "current_now")
STORE_FILES = ("energy_now", "charge_now")

# Levels, for the batteries that report a word instead of a percentage.
LOW_LEVELS = ("low", "critical")

STATUS_TEXT = {
    "Charging": "Charging",
    "Discharging": "Discharging — running on battery",
    "Full": "Full",
    "Not charging": "Not charging — held at its current level on purpose",
    "Unknown": "Unknown",
}

# The same states, worded for something that is not the machine.
DEVICE_STATUS_TEXT = {
    "Charging": "Charging",
    "Discharging": "Discharging — the device is running on its own battery",
    "Full": "Full",
    "Not charging": "Not charging — held at its current level on purpose",
    "Unknown": "Unknown — the device did not say",
}


class PowerProbe(Probe):
    branch = "peripherals"
    id = "power"
    label = "Power"

    def _find_supplies(self) -> list[dict]:
        supplies = []
        for path in list_dir(SUPPLY_ROOT):
            reported = read_first_line(path / "type")
            kind = (reported or "").lower()
            scope = read_first_line(path / "scope")
            is_battery = kind == "battery" or (
                kind in ("", "unknown")
                and any(path_exists(path / name) for name in BATTERY_MARKERS)
            )
            supplies.append(
                {
                    "name": path.name,
                    "path": path,
                    "type": reported,
                    "scope": scope,
                    "is_battery": is_battery,
                    "is_mains": kind == "mains",
                    "is_peripheral": (scope or "").lower() == DEVICE_SCOPE,
                }
            )
        return supplies

    @staticmethod
    def _sort(supplies) -> tuple[list, list, list]:
        """The machine's own batteries, the peripherals', and the mains inputs."""
        system = [
            item
            for item in supplies
            if item["is_battery"] and not item["is_peripheral"]
        ]
        devices = [
            item for item in supplies if item["is_battery"] and item["is_peripheral"]
        ]
        mains = [item for item in supplies if item["is_mains"]]
        return system, devices, mains

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

        batteries, devices, mains = self._sort(supplies)

        counted = [
            _plural(len(batteries), "battery in the machine", "batteries in the machine"),
            _plural(len(devices), "on a peripheral", "on peripherals"),
            _plural(len(mains), "mains input", "mains inputs"),
        ]
        counted = [text for text in counted if text]
        section.add(
            self.row(
                "supply_count",
                "Power supplies",
                "{} in total{}".format(
                    len(supplies), " — " + ", ".join(counted) if counted else ""
                ),
            )
        )
        for row in self._mains_rows(mains):
            section.add(row)
        if batteries:
            present = fmt_list(item["name"] for item in batteries)
        elif devices:
            present = (
                "None in the machine itself — it runs on mains power only. The "
                "batteries below belong to peripherals."
            )
        else:
            present = "None — this machine runs on mains power only"
        section.add(self.row("battery_present", "Batteries", present))
        if devices:
            section.add(
                self.row(
                    "battery_peripheral_present",
                    "Peripheral batteries",
                    fmt_list(_device_name(item) for item in devices),
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
        batteries, devices, _mains = self._sort(supplies)

        if not batteries:
            section.add(
                self.row(
                    "battery_absent",
                    "Battery",
                    "No battery is present in the machine itself."
                    if devices
                    else "No battery is present on this machine.",
                )
            )
            section.add(
                self.row(
                    "battery_absent_detail",
                    "How this was determined",
                    "Nothing under /sys/class/power_supply reports a battery "
                    "belonging to the machine. On a desktop that is the expected "
                    "answer. On a portable machine it means the battery driver "
                    "has not loaded, and the firmware setting or the kernel "
                    "module is the place to look."
                    + (
                        " The peripherals below report batteries of their own."
                        if devices
                        else ""
                    ),
                )
            )

        for battery in batteries:
            for row in self._battery_rows(battery):
                section.add(row)
        for battery in devices:
            for row in self._device_rows(battery):
                section.add(row)
        return section

    # -- peripheral batteries ---------------------------------------------

    def _device_rows(self, battery) -> list:
        """One wireless mouse, keyboard, headset or controller.

        Kept apart from the machine's own pack on purpose. The name of the
        thing goes in the label, because `hidpp_battery_0` names a driver and
        not anything a person owns, and the rows below it are only the ones the
        device actually answers.
        """
        path = battery["path"]
        prefix = battery["name"]
        rows = [
            self.row(
                "battery_peripheral",
                _device_name(battery),
                fmt_list(
                    (
                        "Peripheral battery",
                        read_first_line(path / "manufacturer"),
                        f"reported as {prefix}",
                    ),
                    separator=" · ",
                ),
                key=f"{prefix}peripheral",
            )
        ]
        rows.extend(self._device_volatile_rows(battery))

        serial = read_first_line(path / "serial_number")
        if serial:
            rows.append(
                self.row(
                    "battery_serial",
                    "  Serial number",
                    serial,
                    key=f"{prefix}serial",
                )
            )

        # A pack in a laptop answers for its wear and for how long it has left.
        # A device battery answers neither question, and the honest thing is to
        # say so once rather than to print two rows of dashes or a time
        # estimate divided out of numbers that are not there.
        if not _reports_rate(path):
            rows.append(
                self.row(
                    "battery_peripheral_limits",
                    "  What this device reports",
                    "Its charge and its state, and nothing else. It publishes no "
                    "design capacity, so there is no health figure for it, and no "
                    "discharge rate, so there is no time estimate.",
                    key=f"{prefix}limits",
                )
            )
        return rows

    def _device_volatile_rows(self, battery) -> list:
        """Charge and state: the two things every device battery answers."""
        path = battery["path"]
        prefix = battery["name"]
        capacity = read_int(path / "capacity")
        level = read_first_line(path / "capacity_level")
        status = read_first_line(path / "status")

        if capacity is not None:
            charge = f"{capacity}%"
            low = capacity <= 10
        elif level:
            charge = f"{level} — this device reports a level, not a percentage"
            low = level.lower() in LOW_LEVELS
        else:
            charge = NOT_REPORTED
            low = False

        rows = [
            self.row(
                "battery_charge",
                "  Charge",
                charge,
                tier=VOLATILE,
                severity=WARNING if low else "normal",
                key=f"{prefix}charge",
            ),
            self.row(
                "battery_status",
                "  Status",
                DEVICE_STATUS_TEXT.get(status or "", or_missing(status, NOT_REPORTED)),
                tier=VOLATILE,
                key=f"{prefix}status",
            ),
        ]
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
        # The rare device that does publish a rate gets the same three rows a
        # laptop pack gets, because for that one the numbers are real.
        if _reports_rate(path):
            rows.extend(self._rate_rows(battery, voltage, status))
        return rows

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

        rows.extend(self._rate_rows(battery, voltage, status))
        return rows

    def _rate_rows(self, battery, voltage, status) -> list:
        """Draw, stored and time remaining: the three rows a rate feeds.

        Every one of them is emitted on every pass, including when it has
        nothing to say. A row that disappears from sample() is simply not
        updated, and the widget keeps whatever it last said - so plugging the
        machine in would leave "About 2 hours left" on screen indefinitely.
        """
        path = battery["path"]
        prefix = battery["name"]
        power = read_int(path / "power_now")
        current = read_int(path / "current_now")
        if power is not None:
            draw = f"{abs(power) / 1_000_000:.2f} W"
        elif current is not None and voltage is not None:
            watts = (abs(current) / 1_000_000) * (abs(voltage) / 1_000_000)
            draw = f"{watts:.2f} W — worked out from current and voltage"
        else:
            draw = NOT_REPORTED
        rows = [
            self.row(
                "battery_draw",
                "  Drawing",
                draw,
                tier=VOLATILE,
                key=f"{prefix}draw",
            )
        ]

        now, _design, unit = _capacity_pair(path, use_now=True)
        rows.append(
            self.row(
                "battery_stored",
                "  Stored",
                _quantity(now, unit) if now else NOT_REPORTED,
                tier=VOLATILE,
                key=f"{prefix}stored",
            )
        )
        remaining = _time_remaining(now, unit, power, current, voltage, status)
        if remaining is None:
            if status == "Discharging":
                remaining = "Not enough information to estimate"
            else:
                remaining = "Only estimated while running on battery"
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
            section.add(self._supply_row(item))
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

        batteries, devices, _mains = self._sort(supplies)
        battery_rows = []
        for battery in batteries:
            if read_int(battery["path"] / "present") == 0:
                continue
            battery_rows.extend(self._battery_volatile_rows(battery))
        for battery in devices:
            battery_rows.extend(self._device_volatile_rows(battery))
        if battery_rows:
            out["battery"] = battery_rows

        supply_rows = [self._supply_row(item) for item in supplies]
        if supply_rows:
            out["supplies"] = supply_rows
        return out

    def _supply_row(self, item):
        """One line per entry under /sys/class/power_supply, whatever it is."""
        path = item["path"]
        detail = [item["type"] or "Unknown type"]
        online = read_int(path / "online")
        if online is not None:
            detail.append("connected" if online else "not connected")
        status = read_first_line(path / "status")
        if status:
            detail.append(status.lower())
        if item["is_peripheral"]:
            detail.append("belongs to a device, not to the machine")
        elif item["scope"]:
            detail.append(f"scope {item['scope'].lower()}")
        return self.row(
            "supply",
            item["name"],
            ", ".join(detail),
            tier=VOLATILE,
            key=f"supply{item['name']}",
        )


_FAMILIES = (
    ("charge_now", "charge_full", "charge_full_design", "Ah"),
    ("energy_now", "energy_full", "energy_full_design", "Wh"),
)


def _plural(count: int, singular: str, plural: str) -> str:
    """``2, "mains input", "mains inputs"`` -> ``2 mains inputs``. Empty at zero.

    Zero of something is left out rather than counted: the rows underneath say
    what is absent in words, and "0 batteries, 1 on a peripheral, 0 mains
    inputs" makes a reader work to find the one number that is not zero.
    """
    if not count:
        return ""
    return "{} {}".format(count, singular if count == 1 else plural)


def _device_name(battery) -> str:
    """What to call a peripheral battery.

    `hidpp_battery_0` names a driver and an index. `MX Master 3S` names the
    thing on the desk, and is what somebody opened this section to look for,
    so the model name wins wherever the driver publishes one.
    """
    path = battery["path"]
    return (
        read_first_line(path / "model_name")
        or read_first_line(path / "manufacturer")
        or battery["name"]
    )


def _reports_rate(path) -> bool:
    """Whether this battery publishes enough to work a rate or a total out of."""
    return any(path_exists(path / name) for name in RATE_FILES) and any(
        path_exists(path / name) for name in STORE_FILES
    )


def _capacity_pair(path, use_now: bool = False) -> tuple[int | None, int | None, str]:
    """Current-or-full and design capacity, in whichever unit the battery uses.

    A battery reports either a charge in microampere-hours or an energy in
    microwatt-hours, and a few report parts of both. The family that has the
    value being asked for wins; taking the first family where *anything* was
    readable can return a design capacity from one family while the matching
    current value sits unread in the other, and health then reads "Not
    reported" for a battery that could have answered.
    """
    for now_name, full_name, design_name, unit in _FAMILIES:
        first = read_int(path / (now_name if use_now else full_name))
        if first is not None:
            return first, read_int(path / design_name), unit
    for _now_name, _full_name, design_name, unit in _FAMILIES:
        design = read_int(path / design_name)
        if design is not None:
            return None, design, unit
    return None, None, ""


def _quantity(microunits: int, unit: str) -> str:
    if not unit:
        return str(microunits)
    return f"{microunits / 1_000_000:.2f} {unit}"


def _time_remaining(stored, unit, power, current, voltage, status) -> str | None:
    """Roughly how long, at the rate right now. Deliberately not precise.

    Both sides are converted to the same units before dividing. A battery may
    report what it holds as a charge (Ah) or an energy (Wh), and its rate as a
    power (W) or a current (A), and the two choices are independent - so a
    straight division can be amp-hours over watts, which is not a time and is
    out by whatever the pack voltage happens to be.

    Magnitudes are taken, because several drivers sign the discharge rate
    negative and a negative duration is not a thing.
    """
    if not stored or status != "Discharging":
        return None

    volts = (abs(voltage) / 1_000_000) if voltage else None
    if unit == "Wh":
        watt_hours = abs(stored) / 1_000_000
    elif unit == "Ah" and volts:
        watt_hours = (abs(stored) / 1_000_000) * volts
    else:
        return None

    if power:
        watts = abs(power) / 1_000_000
    elif current and volts:
        watts = (abs(current) / 1_000_000) * volts
    else:
        return None

    if watts <= 0:
        return None
    return f"About {fmt_duration((watt_hours / watts) * 3600)} left at the present rate"
