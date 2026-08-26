# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The full per-attribute SMART table, one row 3 instance per drive.

CORE section 6.3 splits SMART across two row 2 tabs on purpose. ``Disks``
keeps the headline values - the health verdict, the temperature, the power-on
hours - because they answer "is this drive dying" at a glance and they come out
of udisks2 properties that already work. This tab carries the table underneath
them: every attribute the drive reports, with its id, its name, the drive's own
normalised, worst and threshold values, and the raw value decoded the way the
vendor meant it.

Where the table comes from, in order:

1. ``udisks2.SmartGetAttributes``. Asked first because it needs nothing this
   application does not already depend on. It has never yet answered: ATA
   returns ``a(ysqiiixia{sv})`` and NVMe returns ``a{sv}``, Qt maps both to
   ``QDBusArgument``, and PySide6 cannot read one - ``asVariant()`` gives None
   and the extraction operator aborts the process. The call is still made,
   because it costs one round trip and it is the route with no dependency.
2. ``smartctl --json=c``, run once at launch by the privileged helper. This is
   the route that works, and it is optional: smartmontools absent costs this
   tab and nothing else in the application.

A drive with no table still gets a tab, and the tab says which of the several
quite different reasons applies. "The drive was asleep and was left that way",
"this USB enclosure does not pass SMART commands through", "smartmontools is
not installed" and "administrator access was declined" are four different
problems with four different answers, and collapsing them into one sentence
sends people looking for a fault in a drive that does not have one.

Nothing here is re-read on the interval timer except temperature. The helper
runs once, at launch, before the window exists; the table it returned is a
snapshot of that moment and is honestly presented as one. Temperature is the
exception because udisks2 does keep it current, so the temperature attribute
and the NVMe log's temperature field are refreshed from the same property the
Disks tab's temperature row uses.
"""

from __future__ import annotations

from .base import (
    DANGER,
    NORMAL,
    NOT_REPORTED,
    REQUIRES_ROOT,
    STATIC,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_bytes,
    fmt_duration,
    fmt_int,
)
from .disks import (
    IFACE_ATA,
    IFACE_BLOCK,
    IFACE_NVME,
    _object_path,
    client,
    find_disks,
)

# Attributes that are counters of things going wrong. Every one of them is
# meant to read zero for the whole life of the drive, so a non-zero value is
# worth colouring whatever the drive's own threshold says - a drive can sit at
# two hundred reallocated sectors and still be nowhere near its threshold.
#
# Matched by id and by name, because vendors disagree about both. Samsung calls
# 187 Uncorrectable_Error_Cnt and Seagate calls it Reported_Uncorrect; some
# drives report a CRC error count under no id this list would recognise.
COUNTER_IDS = frozenset((5, 187, 196, 197, 198, 199))
COUNTER_NAMES = ("reallocat", "pending", "uncorrect", "crc", "offline_unc")

# Wear attributes count down rather than up: the drive starts them at 100 and
# walks them towards its own threshold as the flash is used. Their raw value is
# a program/erase cycle count or a byte total and says nothing on its own, so
# these are judged on the normalised value instead.
WEAR_NAMES = ("wear", "life", "endurance", "lifetime", "media_wearout")

# The one number in this file that the drive did not supply. Danger is always
# the drive's own threshold and never this; this only decides which rows get
# the eye first on a drive that is wearing out but has not yet said so. Twenty
# percent of rated life left is late enough to mean something and early enough
# to be worth acting on.
WEAR_WARNING_VALUE = 20

# The NVMe health log, in the order the specification lays it out rather than
# alphabetically, because that is the order every other tool prints it in.
# Anything the log carries that is not named here is still shown, after these.
NVME_LOG_ORDER = (
    ("critical_warning", "Critical warning"),
    ("temperature", "Temperature"),
    ("available_spare", "Available spare"),
    ("available_spare_threshold", "Available spare threshold"),
    ("percentage_used", "Percentage used"),
    ("data_units_read", "Data units read"),
    ("data_units_written", "Data units written"),
    ("host_reads", "Host read commands"),
    ("host_writes", "Host write commands"),
    ("controller_busy_time", "Controller busy time"),
    ("power_cycles", "Power cycles"),
    ("power_on_hours", "Powered on for"),
    ("unsafe_shutdowns", "Unsafe shutdowns"),
    ("media_errors", "Media and data integrity errors"),
    ("num_err_log_entries", "Error log entries"),
    ("warning_temp_time", "Time above the warning temperature"),
    ("critical_comp_time", "Time above the critical temperature"),
)

# An NVMe data unit is a thousand 512-byte blocks, per the specification. It is
# not a sector and it is not a kibibyte, and reading it as either is how a
# drive ends up reported as having written a thousandth of what it has.
NVME_DATA_UNIT_BYTES = 512 * 1000

# Matches the Disks tab, so one drive does not read as hot on one tab and
# temperate on the other.
HOT_CELSIUS = 60

# The attribute udisks2's SmartTemperature is the same measurement as, and the
# only one the live reading is substituted into. A drive frequently reports a
# second temperature under 190, Airflow_Temperature_Cel, which is a different
# sensor in a different place; writing one number into both rows would be
# inventing a reading for a sensor nobody read. 190 keeps its own raw value.
TEMPERATURE_ID = 194


def _celsius(kelvin) -> float | None:
    """udisks2 reports SMART temperature in kelvin, as a float."""
    if not isinstance(kelvin, (int, float)) or not kelvin:
        return None
    return kelvin - 273.15


def _leading_int(value) -> int | None:
    """The first whole number in a raw value, or None.

    Raw SMART fields are vendor-defined and often several numbers packed into
    one, which smartctl renders as ``34 (Min/Max 20/45)`` or ``8087 (55 205)``.
    The first is the count in every layout anybody has documented.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    digits = ""
    for character in text:
        if character.isdigit():
            digits += character
        elif digits:
            break
        elif character not in "+ ":
            return None
    return int(digits) if digits else None


def _matches(name: str, needles) -> bool:
    lowered = name.lower()
    return any(needle in lowered for needle in needles)


def _is_temperature(identifier, name: str) -> bool:
    """Whether this attribute is the drive temperature udisks2 also reports.

    A numbered attribute is judged on its number, because the number is the
    thing vendors agree on and the name is not. Only a table that arrived
    without ids at all falls back to reading the name.
    """
    if identifier == TEMPERATURE_ID:
        return True
    if isinstance(identifier, int):
        return False
    return "temperature" in name.lower() and "airflow" not in name.lower()


class SmartProbe(Probe):
    branch = "storage"
    id = "smart"
    label = "SMART"

    def __init__(self, context=None):
        super().__init__(context)
        self._disks: list[dict] = []

    # -- enumeration ------------------------------------------------------

    def sections(self) -> list[Section]:
        disks = find_disks()
        self._disks = disks
        if not disks:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No physical block devices were found under /sys/class/block, "
                    "so there is nothing to ask for SMART data.",
                )
            ]
        return [self._drive_section(disk) for disk in disks]

    def _drive_section(self, disk) -> Section:
        section = Section(
            id=disk["name"],
            label=disk["name"],
            icon="disk_rotational" if disk.get("rotational") == 1 else "disk_solid",
        )
        for row in self._drive_rows(disk):
            section.add(row)
        return section

    # -- one drive --------------------------------------------------------

    def _drive_rows(self, disk) -> list:
        """The table, or the one sentence explaining why there is not one."""
        drive_path = self._drive_path(disk)

        # udisks2 first. On every binding tested this returns nothing, but it
        # is the route with no extra dependency and it costs one round trip.
        if drive_path is not None and self._supports_smart(drive_path):
            udisks = client()
            entries = udisks.smart_attributes(drive_path)
            if entries:
                if udisks.has_interface(drive_path, IFACE_NVME):
                    return self._nvme_rows(
                        {str(entry.get("name")): entry.get("raw") for entry in entries},
                        drive_path,
                        "udisks2",
                    )
                return self._ata_rows(entries, drive_path, "udisks2")

        report = self._helper_report(disk)
        state = str(report.get("state") or "") if report else ""

        if state == "ok":
            attributes = report.get("attributes")
            log = report.get("nvme_log")
            if isinstance(log, dict) and log:
                return self._nvme_rows(log, drive_path, "smartctl")
            if isinstance(attributes, list) and attributes:
                return self._ata_rows(attributes, drive_path, "smartctl")

        value, severity = self._why_not(disk, report)
        rows = [self.row("status", "Attribute table", value, severity=severity)]
        if value == REQUIRES_ROOT:
            # A bare refusal on a tab whose entire content is missing is the
            # one place it is worth saying what the password would buy.
            rows.append(
                self.row(
                    "why",
                    "Why",
                    "Reading the attribute table means sending the drive a "
                    "command, which only the administrator may do. The health "
                    "verdict, temperature and power-on hours on the Disks tab "
                    "come from udisks2, which asks on its own behalf, and are "
                    "there whether this prompt was answered or not.",
                )
            )
        return rows

    def _why_not(self, disk, report) -> tuple:
        """The value and severity for a drive with no table, and why.

        Every branch here is a different problem with a different answer, so
        each gets its own sentence. Collapsing them into "SMART data is not
        available" is what sends somebody looking for a fault in a healthy
        drive.
        """
        node = disk.get("node", "this device")
        state = str(report.get("state") or "") if report else ""
        detail = _first_message(report)

        if state == "standby":
            return (
                f"{node} was asleep when KÖKEN started, and reading its SMART "
                "table would have spun it up. It was left alone. Open this tab "
                "again after the drive has been used and the table will be "
                "there.",
                NORMAL,
            )
        if state == "unreadable":
            return (
                "smartctl could not read this device: "
                + (detail or "it gave no reason.")
                + " A USB enclosure is the usual cause — most bridge chips do "
                "not pass SMART commands through to the drive behind them, and "
                "the ones that do need to be told which protocol to translate.",
                NORMAL,
            )
        if state == "timeout":
            return (
                f"smartctl did not answer for {node} within the time the "
                "privileged helper allows itself, so its table was not read.",
                WARNING,
            )
        if state == "skipped":
            return (
                "The privileged helper ran out of time before it reached "
                f"{node}. Drives are read in order, so this one is behind "
                "several that were slow to answer.",
                WARNING,
            )
        if state == "empty":
            return (
                "smartctl opened this device and it reported no attribute "
                "table and no health log. Some USB enclosures and most "
                "memory-card readers answer this way.",
                NORMAL,
            )

        # Nothing came back for this device at all. Either the read never
        # happened or it happened and did not cover this drive.
        privileged = self._privileged()
        if privileged is None or not getattr(privileged, "available", False):
            return (REQUIRES_ROOT, NORMAL)
        if not getattr(privileged, "smart", {}):
            return (
                "The per-attribute table is not shown. udisks2 returns it in a "
                "form this Qt binding cannot read, and smartmontools — the "
                "other way to reach it — is not installed. Everything on the "
                "Disks tab comes from the same SMART data and is unaffected.",
                NORMAL,
            )
        return (
            f"The privileged helper did not report on {node}. It reads whole "
            "disks under /sys/class/block and skips loop, RAM, device-mapper "
            "and optical devices.",
            NORMAL,
        )

    # -- ATA --------------------------------------------------------------

    def _ata_rows(self, entries, drive_path, source: str) -> list:
        kelvin = self._live_kelvin(drive_path)
        rows = [
            self.row(
                "status",
                "Attribute table",
                f"{len(entries)} attributes, read with {source}",
            )
        ]
        for entry in entries:
            rows.append(self._ata_row(entry, kelvin))
        return rows

    def _ata_row(self, entry, kelvin):
        identifier = entry.get("id")
        name = str(entry.get("name") or "").strip()
        value = entry.get("value")
        worst = entry.get("worst")
        thresh = entry.get("thresh")
        raw = entry.get("raw")

        label_id = str(identifier) if isinstance(identifier, int) else "?"
        label = f"  {label_id} {name}" if name else f"  Attribute {label_id}"

        columns = []
        for caption, number in (
            ("value", value),
            ("worst", worst),
            ("threshold", thresh),
        ):
            if isinstance(number, int) and not isinstance(number, bool):
                columns.append(f"{caption} {number}")
        detail = ", ".join(columns)

        # A temperature attribute is volatile whether or not udisks2 happens to
        # have a live reading for it right now. The row is the kind that moves;
        # whether anything is currently able to move it is a separate question,
        # and deciding the tier on that would leave the row static forever on a
        # drive udisks2 had simply not got round to reading at launch.
        temperature = _is_temperature(identifier, name)
        head = ""
        if temperature and kelvin is not None:
            head = f"{kelvin:.0f} °C"
        if raw not in (None, ""):
            head = f"{head} — {raw}" if head else str(raw)
        text = f"{head} — {detail}" if head and detail else (head or detail or NOT_REPORTED)

        return self.row(
            "attribute",
            label,
            text,
            tier=VOLATILE if temperature else STATIC,
            severity=self._ata_severity(entry, name, value, thresh, raw),
            key=f"attr{label_id} {name}",
        )

    def _ata_severity(self, entry, name, value, thresh, raw) -> str:
        when_failed = str(entry.get("when_failed") or "").strip().lower()
        crossed = (
            isinstance(value, int)
            and isinstance(thresh, int)
            and thresh > 0
            and value <= thresh
        )
        if when_failed == "now" or crossed:
            return DANGER
        if when_failed == "past":
            return WARNING
        identifier = entry.get("id")
        if (identifier in COUNTER_IDS or _matches(name, COUNTER_NAMES)) and not _matches(
            name, WEAR_NAMES
        ):
            count = _leading_int(raw)
            return WARNING if count else NORMAL
        if _matches(name, WEAR_NAMES):
            if isinstance(value, int) and value <= WEAR_WARNING_VALUE:
                return WARNING
        return NORMAL

    # -- NVMe -------------------------------------------------------------

    def _nvme_rows(self, log, drive_path, source: str) -> list:
        kelvin = self._live_kelvin(drive_path)
        rows = [
            self.row(
                "status",
                "Health log",
                f"{len(log)} entries, read with {source}. NVMe has no ATA "
                "attribute table; this is the log that replaces it.",
            )
        ]
        named = [key for key, _label in NVME_LOG_ORDER if key in log]
        rest = sorted(key for key in log if key not in named)
        labels = dict(NVME_LOG_ORDER)
        spare_threshold = log.get("available_spare_threshold")
        for key in named + rest:
            rows.append(
                self._nvme_row(key, log[key], labels.get(key), kelvin, spare_threshold)
            )
        return rows

    def _nvme_row(self, key, value, label, kelvin, spare_threshold):
        caption = label or key.replace("_", " ").capitalize()
        text, severity, volatile = _nvme_value(key, value, kelvin, spare_threshold)
        return self.row(
            "nvme_log",
            f"  {caption}",
            text,
            tier=VOLATILE if volatile else STATIC,
            severity=severity,
            key=f"attr{key}",
        )

    # -- shared -----------------------------------------------------------

    def _privileged(self):
        return getattr(self.context, "privileged", None)

    def _helper_report(self, disk) -> dict:
        """What the helper's smartctl pass returned for this device node."""
        privileged = self._privileged()
        if privileged is None or not getattr(privileged, "available", False):
            return {}
        getter = getattr(privileged, "smart_for_device", None)
        if not callable(getter):
            # An older PrivilegedData, from a capture taken before this
            # existed. Empty is the right answer and a crash is not.
            return {}
        report = getter(disk.get("node") or "")
        return report if isinstance(report, dict) else {}

    def _supports_smart(self, drive_path) -> bool:
        """Whether asking udisks2 for a table on this drive is worth a call.

        A drive that has told udisks2 it does not support SMART will answer the
        call with an error, and there is no reason to make it. The properties
        this reads are already in the client's cache by the time anything on
        this tab runs, so the check costs nothing.
        """
        udisks = client()
        if udisks.has_interface(drive_path, IFACE_NVME):
            return True
        if not udisks.has_interface(drive_path, IFACE_ATA):
            return False
        return udisks.properties(drive_path, IFACE_ATA).get("SmartSupported") is not False

    def _drive_path(self, disk) -> str | None:
        """The udisks2 drive object behind this device node, if there is one."""
        udisks = client()
        if not udisks.available:
            return None
        block_path = udisks.block_for_device(disk.get("node") or "")
        if block_path is None:
            return None
        block = udisks.properties(block_path, IFACE_BLOCK)
        return _object_path(block.get("Drive"))

    def _live_kelvin(self, drive_path, refresh: bool = False):
        """The drive's current temperature from udisks2, in kelvin.

        The helper's snapshot is from launch. This is not, which is why the
        temperature rows are the only volatile ones on this tab.
        """
        if drive_path is None:
            return None
        udisks = client()
        if not udisks.available:
            return None
        for interface in (IFACE_NVME, IFACE_ATA):
            if not udisks.has_interface(drive_path, interface):
                continue
            properties = udisks.properties(drive_path, interface, refresh=refresh)
            updated = properties.get("SmartUpdated")
            if not (isinstance(updated, (int, float)) and updated > 0):
                return None
            return _celsius(properties.get("SmartTemperature"))
        return None

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        """Only the rows that genuinely move: the two temperatures.

        Every other row on this tab came from a helper that ran once before the
        window existed. Re-emitting them from the same snapshot every few
        seconds would cost work and change nothing, and re-running the helper
        would mean a second password prompt, which CORE section 8.3 rules out.
        """
        out: dict[str, list] = {}
        for disk in self._disks or find_disks():
            drive_path = self._drive_path(disk)
            if drive_path is None:
                continue
            kelvin = self._live_kelvin(drive_path, refresh=True)
            if kelvin is None:
                continue
            report = self._helper_report(disk)
            rows = []

            log = report.get("nvme_log") if isinstance(report, dict) else None
            if isinstance(log, dict) and "temperature" in log:
                rows.append(
                    self._nvme_row(
                        "temperature",
                        log["temperature"],
                        dict(NVME_LOG_ORDER).get("temperature"),
                        kelvin,
                        log.get("available_spare_threshold"),
                    )
                )
            attributes = report.get("attributes") if isinstance(report, dict) else None
            for entry in attributes or []:
                if _is_temperature(entry.get("id"), str(entry.get("name") or "")):
                    rows.append(self._ata_row(entry, kelvin))
            if rows:
                out[disk["name"]] = rows
        return out


def _first_message(report) -> str:
    """smartctl's own first word on why it could not read a device."""
    messages = report.get("messages") if isinstance(report, dict) else None
    for text in messages or []:
        cleaned = str(text).strip()
        if cleaned:
            return cleaned if cleaned.endswith(".") else cleaned + "."
    return ""


def _nvme_value(key, value, kelvin, spare_threshold) -> tuple:
    """One health log field as text, severity, and whether it moves.

    Every judgement here is the drive's own. Available spare is compared with
    the threshold the drive itself publishes beside it; percentage used passes
    100 when the drive has written what the manufacturer rated it for, which is
    the specification's definition and not a number chosen here.
    """
    numeric = value if isinstance(value, int) and not isinstance(value, bool) else None

    if key == "temperature":
        celsius = kelvin if kelvin is not None else numeric
        if celsius is None:
            return (str(value), NORMAL, True)
        return (
            f"{celsius:.0f} °C",
            WARNING if celsius >= HOT_CELSIUS else NORMAL,
            True,
        )

    if numeric is None:
        if isinstance(value, (list, tuple)):
            return (", ".join(str(item) for item in value), NORMAL, False)
        return (str(value), NORMAL, False)

    if key == "critical_warning":
        return (
            "None" if numeric == 0 else f"0x{numeric:02x} — the drive is reporting one",
            DANGER if numeric else NORMAL,
            False,
        )
    if key == "available_spare":
        crossed = (
            isinstance(spare_threshold, int) and numeric <= spare_threshold
        )
        return (f"{numeric}%", DANGER if crossed else NORMAL, False)
    if key == "available_spare_threshold":
        return (f"{numeric}% — the drive's own limit", NORMAL, False)
    if key == "percentage_used":
        return (
            f"{numeric}% of the rated write endurance",
            WARNING if numeric >= 100 else NORMAL,
            False,
        )
    if key in ("data_units_read", "data_units_written"):
        return (
            f"{fmt_int(numeric)} — {fmt_bytes(numeric * NVME_DATA_UNIT_BYTES, binary=False)}",
            NORMAL,
            False,
        )
    if key == "power_on_hours":
        return (f"{fmt_duration(numeric * 3600)} — {fmt_int(numeric)} hours", NORMAL, False)
    if key in ("controller_busy_time", "warning_temp_time", "critical_comp_time"):
        return (
            "0 minutes"
            if numeric == 0
            else f"{fmt_int(numeric)} minutes — {fmt_duration(numeric * 60)}",
            WARNING if key != "controller_busy_time" and numeric else NORMAL,
            False,
        )
    if key == "media_errors":
        return (fmt_int(numeric), WARNING if numeric else NORMAL, False)
    return (fmt_int(numeric), NORMAL, False)
