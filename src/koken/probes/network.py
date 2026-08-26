# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Network interfaces, one row 3 instance each.

No addresses are looked up, nothing is resolved, and nothing is contacted. This
reads the interface as a piece of hardware: what it is, whether the cable is in,
how fast the link came up, and how many bytes have crossed it since boot.

``speed`` deserves its own note. Reading it on an interface that is down, or on
any wireless interface, does not return an empty file - it returns an error, at
the read, from the driver. Code that opens it and expects text gets a traceback
on a perfectly ordinary laptop. The readers in this package answer None to that
exactly as they answer None to a file that is not there.
"""

from __future__ import annotations

from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_bytes,
    fmt_int,
    fmt_list,
    list_dir,
    or_missing,
    path_exists,
    read_first_line,
    read_int,
    read_link_name,
)

NET_ROOT = "/sys/class/net"

# ARPHRD constants from the kernel's if_arp.h. Only the ones a desktop meets.
ARP_TYPES = {
    1: "Ethernet",
    24: "IEEE 1394",
    512: "Serial line IP",
    768: "IPIP tunnel",
    769: "IPv6 in IPv4 tunnel",
    772: "Loopback",
    776: "IPv6 in IPv4 (sit)",
    778: "GRE tunnel",
    801: "IEEE 802.11",
    802: "IEEE 802.11 with Prism header",
    803: "IEEE 802.11 radiotap",
    823: "IEEE 802.15.4",
    65534: "None — a tunnel or virtual interface",
}

# operstate values, and what each means to somebody who is not a network
# engineer.
OPERSTATES = {
    "up": "Up — carrying traffic",
    "down": "Down",
    "dormant": "Dormant — waiting for something, usually authentication",
    "lowerlayerdown": "The interface beneath this one is down",
    "notpresent": "The hardware is not present",
    "testing": "Testing",
    "unknown": "Unknown — this driver does not report a state",
}

# Counters shown in the statistics block, in this order.
STATISTICS = (
    ("rx_bytes", "Received"),
    ("tx_bytes", "Sent"),
    ("rx_packets", "Packets received"),
    ("tx_packets", "Packets sent"),
    ("rx_errors", "Receive errors"),
    ("tx_errors", "Send errors"),
    ("rx_dropped", "Receive drops"),
    ("tx_dropped", "Send drops"),
    ("collisions", "Collisions"),
)

BYTE_COUNTERS = {"rx_bytes", "tx_bytes"}
ERROR_COUNTERS = {"rx_errors", "tx_errors", "rx_dropped", "tx_dropped", "collisions"}


class NetworkProbe(Probe):
    branch = "peripherals"
    id = "network"
    label = "Network"

    def __init__(self, context=None):
        super().__init__(context)
        self._interfaces: list[dict] = []

    # -- enumeration ------------------------------------------------------

    def _find_interfaces(self) -> list[dict]:
        interfaces = []
        for path in list_dir(NET_ROOT):
            # /sys/class/net holds plain files as well as interfaces: the
            # bonding module drops `bonding_masters` in there, and without this
            # check it becomes a phantom device with eight rows of nothing.
            try:
                if not path.is_dir():
                    continue
            except OSError:
                continue
            wireless = path_exists(path / "wireless") or path_exists(path / "phy80211")
            arp_type = read_int(path / "type")
            interfaces.append(
                {
                    "name": path.name,
                    "path": path,
                    "type": arp_type,
                    "wireless": wireless,
                    "loopback": arp_type == 772,
                    "driver": read_link_name(path / "device/driver"),
                    "virtual": not path_exists(path / "device"),
                }
            )
        return interfaces

    def sections(self) -> list[Section]:
        interfaces = self._find_interfaces()
        self._interfaces = interfaces
        if not interfaces:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No network interfaces were found, which should not be possible "
                    "on a running kernel - even a machine with no hardware has a "
                    "loopback interface.",
                )
            ]
        return [self._interface_section(item) for item in interfaces]

    def _interface_section(self, item) -> Section:
        section = Section(
            id=item["name"],
            label=item["name"],
            icon=self._icon(item),
        )
        path = item["path"]

        section.add(
            self.row(
                "kind",
                "Kind",
                self._kind_text(item),
            )
        )
        section.add(
            self.row(
                "address",
                "Hardware address",
                or_missing(read_first_line(path / "address"), NOT_REPORTED),
            )
        )
        permanent = read_first_line(path / "address_assign_type")
        if permanent is not None:
            section.add(
                self.row(
                    "address_type",
                    "Address origin",
                    {
                        "0": "Permanent — burned into the hardware",
                        "1": "Randomly generated",
                        "2": "Stolen from another device",
                        "3": "Set by software",
                    }.get(permanent, permanent),
                )
            )

        for row in self._link_rows(item):
            section.add(row)

        section.add(
            self.row("mtu", "MTU", or_missing(read_first_line(path / "mtu"), NOT_REPORTED))
        )
        section.add(
            self.row(
                "driver",
                "Driver",
                or_missing(
                    item["driver"],
                    "None — this is a virtual interface with no hardware behind it"
                    if item["virtual"]
                    else "None bound",
                ),
            )
        )
        index = read_first_line(path / "ifindex")
        if index:
            section.add(self.row("ifindex", "Interface index", index))

        queues = read_first_line(path / "tx_queue_len")
        if queues:
            section.add(self.row("queue_length", "Transmit queue length", queues))

        for row in self._statistics_rows(item):
            section.add(row)
        return section

    def _icon(self, item) -> str:
        if item["loopback"] or item["virtual"]:
            return "net_virtual"
        if item["wireless"]:
            return "net_wireless"
        return "net_ethernet"

    def _kind_text(self, item) -> str:
        base = ARP_TYPES.get(item["type"])
        if item["wireless"]:
            return f"Wireless ({base or 'type ' + str(item['type'])})"
        if base is None:
            return f"Type {item['type']}" if item["type"] is not None else NOT_REPORTED
        if item["virtual"] and not item["loopback"]:
            return f"{base}, virtual"
        return base

    def _link_rows(self, item) -> list:
        path = item["path"]
        rows = []

        operstate = read_first_line(path / "operstate")
        rows.append(
            self.row(
                "operstate",
                "State",
                OPERSTATES.get(operstate or "", or_missing(operstate, NOT_REPORTED)),
                tier=VOLATILE,
            )
        )

        carrier = read_int(path / "carrier")
        rows.append(
            self.row(
                "carrier",
                "Carrier",
                {
                    1: "Present — something is connected at the other end",
                    0: "Absent — nothing is connected",
                }.get(carrier, "Not readable while the interface is down"),
                tier=VOLATILE,
            )
        )

        # This is the read that raises EINVAL on a down or wireless interface.
        speed = read_int(path / "speed")
        if speed is None:
            if item["wireless"]:
                detail = (
                    "Not reported. A wireless link has no single fixed rate, so the "
                    "driver declines to answer."
                )
            elif operstate == "down":
                detail = "Not reported while the interface is down."
            else:
                # operstate "unknown" is what virtio_net, tun/tap, WireGuard and
                # loopback report while fully operational, so "down" would
                # contradict the state row directly above this one.
                detail = "Not reported by this driver."
        elif speed < 0:
            detail = "Not negotiated."
        else:
            detail = _speed_text(speed)
        rows.append(self.row("speed", "Link speed", detail, tier=VOLATILE))

        duplex = read_first_line(path / "duplex")
        if duplex:
            rows.append(
                self.row(
                    "duplex",
                    "Duplex",
                    {
                        "full": "Full — sending and receiving at the same time",
                        "half": "Half — one direction at a time",
                    }.get(duplex, duplex),
                    tier=VOLATILE,
                    severity=WARNING if duplex == "half" else "normal",
                )
            )
        return rows

    def _statistics_rows(self, item) -> list:
        rows = []
        base = item["path"] / "statistics"
        if not path_exists(base):
            return [
                self.row(
                    "statistics",
                    "Counters",
                    "This interface publishes no traffic counters.",
                )
            ]
        for name, label in STATISTICS:
            value = read_int(base / name)
            if value is None:
                continue
            if name in BYTE_COUNTERS:
                text = f"{fmt_bytes(value)} ({fmt_int(value)} bytes)"
            else:
                text = fmt_int(value)
            rows.append(
                self.row(
                    "counter",
                    label,
                    text,
                    tier=VOLATILE,
                    severity=WARNING if name in ERROR_COUNTERS and value else "normal",
                    key=f"stat{name}",
                )
            )
        return rows

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for item in self._interfaces or self._find_interfaces():
            rows = [row for row in self._link_rows(item) if row.is_volatile]
            rows.extend(row for row in self._statistics_rows(item) if row.is_volatile)
            out[item["name"]] = rows
        return out


def _speed_text(speed: int) -> str:
    """Megabits per second, with the name people use for that rate."""
    names = {
        10: "10BASE-T",
        100: "Fast Ethernet",
        1000: "Gigabit Ethernet",
        2500: "2.5 Gigabit Ethernet",
        5000: "5 Gigabit Ethernet",
        10000: "10 Gigabit Ethernet",
        25000: "25 Gigabit Ethernet",
        40000: "40 Gigabit Ethernet",
    }
    name = names.get(speed)
    if speed >= 1000:
        # Not integer division: 2.5 GbE reports 2500, and //1000 renders it as
        # "2 Gbit/s" right next to the words "2.5 Gigabit Ethernet".
        base = f"{speed / 1000:g} Gbit/s"
    else:
        base = f"{speed} Mbit/s"
    return f"{base} — {name}" if name else base
