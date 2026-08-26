# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The processor, in six sections.

Two parts of this are worth more than the rest. The cache tree is built from
the real per-cpu cache directories rather than from ``/proc/cpuinfo``'s single
"cache size" line, which reports only the last level and reports it per
package, so it tells you almost nothing. And the CCX layout is derived from
which cores share an L3 instance, which is the honest way to find it: AMD does
not publish a "cores per CCX" file, but every core's L3 already says who it is
shared with, and on a chiplet part that grouping *is* the CCX.
"""

from __future__ import annotations

import sys

from .base import (
    NONE_PRESENT,
    NOT_AVAILABLE,
    NOT_REPORTED,
    STATIC,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_bytes,
    fmt_khz,
    fmt_list,
    glob_dirs,
    list_dir,
    or_missing,
    read_first_line,
    read_int,
    read_lines,
    read_text,
)

CPU_ROOT = "/sys/devices/system/cpu"

# Grouped for reading, not for completeness. The point of this section is to
# let someone see at a glance whether the machine has AVX-512 or hardware
# virtualisation, not to reprint /proc/cpuinfo with headings.
FLAG_GROUPS = (
    ("64-bit", ("lm",)),
    ("SSE", ("sse", "sse2", "pni", "ssse3", "sse4_1", "sse4_2", "sse4a")),
    ("AVX", ("avx", "avx2", "fma", "f16c", "avx_vnni")),
    ("AVX-512", None),  # every flag beginning avx512, collected below
    ("AMX", ("amx_tile", "amx_bf16", "amx_int8", "amx_fp16")),
    ("Cryptography", ("aes", "vaes", "pclmulqdq", "vpclmulqdq", "sha_ni", "sha512")),
    ("Random numbers", ("rdrand", "rdseed")),
    ("Bit manipulation", ("bmi1", "bmi2", "abm", "adx", "popcnt", "movbe", "lzcnt")),
    ("Virtualisation", ("vmx", "svm", "ept", "npt", "vnmi", "tpr_shadow", "flexpriority")),
    (
        "Memory protection",
        ("smep", "smap", "umip", "pku", "ospke", "nx", "la57", "user_shstk", "ibt"),
    ),
    (
        "Speculation control",
        ("ibrs", "ibpb", "stibp", "ssbd", "md_clear", "ibrs_enhanced", "flush_l1d"),
    ),
    ("Power and timing", ("constant_tsc", "nonstop_tsc", "tsc_deadline_timer", "aperfmperf")),
)

# The glibc x86-64 microarchitecture levels. A distribution that builds for
# v3 will refuse to run on a machine that does not reach v3, which makes this
# one of the more consequential facts about a processor and one that nothing
# else on the system prints.
MICROARCH_LEVELS = (
    ("x86-64-v2", ("cx16", "lahf_lm", "popcnt", "pni", "sse4_1", "sse4_2", "ssse3")),
    ("x86-64-v3", ("avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave")),
    ("x86-64-v4", ("avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl")),
)

# A vulnerability file whose text starts with one of these is not mitigated.
UNMITIGATED_PREFIXES = ("Vulnerable", "Unknown", "Processor vulnerable")


def parse_cpu_list(text: str | None) -> list[int]:
    """``0-3,8,12-13`` -> ``[0, 1, 2, 3, 8, 12, 13]``.

    This format turns up in ``thread_siblings_list``, ``shared_cpu_list``,
    ``online`` and half a dozen other places, and every one of them can be
    empty or absent.
    """
    if not text:
        return []
    out: list[int] = []
    for chunk in text.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            try:
                low, high = int(start), int(end)
            except ValueError:
                continue
            if low <= high and high - low < 65536:
                out.extend(range(low, high + 1))
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                continue
    return sorted(set(out))


def parse_cpuinfo(text: str | None) -> list[dict[str, str]]:
    """``/proc/cpuinfo`` split into one dict per logical processor."""
    if not text:
        return []
    blocks = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        current[key.strip()] = value.strip()
    if current:
        blocks.append(current)
    return blocks


class CpuProbe(Probe):
    branch = "hardware"
    id = "cpu"
    label = "CPU"

    def __init__(self, context=None):
        super().__init__(context)
        self._cpus: list[int] = []

    # -- gathering --------------------------------------------------------

    def _online_cpus(self) -> list[int]:
        online = parse_cpu_list(read_first_line(f"{CPU_ROOT}/online"))
        if online:
            return online
        # No `online` file at all: fall back to what is present, and then to
        # the directories themselves.
        present = parse_cpu_list(read_first_line(f"{CPU_ROOT}/present"))
        if present:
            return present
        found = []
        for path in glob_dirs(f"{CPU_ROOT}/cpu[0-9]*"):
            try:
                found.append(int(path.name[3:]))
            except ValueError:
                continue
        return sorted(found)

    def _topology(self, cpus: list[int]) -> list[dict]:
        entries = []
        for cpu in cpus:
            base = f"{CPU_ROOT}/cpu{cpu}/topology"
            entries.append(
                {
                    "cpu": cpu,
                    "core_id": read_int(f"{base}/core_id"),
                    "package": read_int(f"{base}/physical_package_id"),
                    "die": read_int(f"{base}/die_id"),
                    "cluster": read_int(f"{base}/cluster_id"),
                    "threads": parse_cpu_list(
                        read_first_line(f"{base}/thread_siblings_list")
                    ),
                }
            )
        return entries

    def _caches(self, cpus: list[int]) -> list[dict]:
        """Every distinct cache instance on the machine.

        Each cpu lists the caches it can see, so the same L3 appears once per
        core that shares it. Deduplicated on the cache ``id`` where the kernel
        exposes one, and on the set of cpus sharing it where it does not.
        """
        seen: dict[tuple, dict] = {}
        for cpu in cpus:
            for index in glob_dirs(f"{CPU_ROOT}/cpu{cpu}/cache/index[0-9]*"):
                level = read_int(index / "level")
                kind = read_text(index / "type")
                if level is None:
                    continue
                shared = parse_cpu_list(read_first_line(index / "shared_cpu_list"))
                cache_id = read_int(index / "id")
                key = (level, kind, cache_id if cache_id is not None else tuple(shared))
                if key in seen:
                    continue
                seen[key] = {
                    "level": level,
                    "type": kind or "Unified",
                    "id": cache_id,
                    "size": _parse_cache_size(read_text(index / "size")),
                    "ways": read_int(index / "ways_of_associativity"),
                    "sets": read_int(index / "number_of_sets"),
                    "line": read_int(index / "coherency_line_size"),
                    "shared": shared,
                }
        order = {"Data": 0, "Instruction": 1, "Unified": 2}
        return sorted(
            seen.values(),
            key=lambda c: (c["level"], order.get(c["type"], 3), c["id"] if c["id"] is not None else 0),
        )

    def _flags(self) -> list[str]:
        blocks = parse_cpuinfo(read_text("/proc/cpuinfo"))
        if not blocks:
            return []
        return blocks[0].get("flags", "").split()

    # -- sections ---------------------------------------------------------

    def sections(self) -> list[Section]:
        cpus = self._online_cpus()
        self._cpus = cpus
        blocks = parse_cpuinfo(read_text("/proc/cpuinfo"))
        first = blocks[0] if blocks else {}
        topology = self._topology(cpus)
        caches = self._caches(cpus)
        flags = first.get("flags", "").split()

        return [
            self._overview(first, blocks, topology),
            self._cores(first, topology, cpus),
            self._clocks(cpus),
            self._cache(caches, topology),
            self._instructions(flags),
            self._vulnerabilities(),
        ]

    # -- overview ---------------------------------------------------------

    def _overview(self, first, blocks, topology) -> Section:
        section = Section(id="overview", label="Overview")
        if not first:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "/proc/cpuinfo could not be read on this machine.",
                )
            )
            return section

        packages = {entry["package"] for entry in topology if entry["package"] is not None}

        section.add(
            self.row("model", "Model", or_missing(first.get("model name")))
        )
        section.add(self.row("vendor", "Vendor", or_missing(first.get("vendor_id"))))
        section.add(
            self.row(
                "family",
                "Family, model, stepping",
                "{} / {} / {}".format(
                    or_missing(first.get("cpu family"), "?"),
                    or_missing(first.get("model"), "?"),
                    or_missing(first.get("stepping"), "?"),
                ),
            )
        )
        section.add(
            self.row("microcode", "Microcode", or_missing(first.get("microcode"), NOT_REPORTED))
        )
        section.add(
            self.row(
                "sockets",
                "Physical processors",
                str(len(packages)) if packages else NOT_AVAILABLE,
            )
        )
        section.add(
            self.row("logical", "Logical processors", str(len(blocks)) if blocks else NOT_AVAILABLE)
        )
        section.add(
            self.row(
                "address_sizes",
                "Address sizes",
                or_missing(first.get("address sizes"), NOT_REPORTED),
            )
        )
        section.add(
            self.row("bogomips", "BogoMIPS", or_missing(first.get("bogomips"), NOT_REPORTED))
        )
        section.add(
            self.row(
                "byte_order",
                "Byte order",
                "Little endian" if _is_little_endian() else "Big endian",
            )
        )
        return section

    # -- cores and threads ------------------------------------------------

    def _cores(self, first, topology, cpus) -> Section:
        section = Section(id="cores", label="Cores and threads")
        if not topology:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "This kernel exposes no CPU topology, so cores and threads cannot be told apart.",
                )
            )
            return section

        physical = {
            (entry["package"], entry["core_id"])
            for entry in topology
            if entry["core_id"] is not None
        }
        core_count = len(physical) if physical else 0
        thread_count = len(topology)
        smt_on = bool(core_count) and thread_count > core_count
        per_core = (thread_count // core_count) if core_count else 0

        section.add(
            self.row(
                "cores",
                "Physical cores",
                str(core_count) if core_count else NOT_AVAILABLE,
            )
        )
        # The value is the bare count so that copying the row yields a number
        # that can be pasted somewhere; the SMT state rides alongside it as a
        # gloss, which is what makes the row read "16 - SMT enabled".
        section.add(
            self.row(
                "smt",
                "Logical threads",
                str(thread_count),
                gloss=("SMT enabled" if smt_on else "SMT disabled") if core_count else "",
            )
        )
        if per_core:
            section.add(
                self.row("threads_per_core", "Threads per core", str(per_core))
            )

        smt_control = read_first_line(f"{CPU_ROOT}/smt/control")
        if smt_control:
            section.add(self.row("smt_control", "SMT control", smt_control))

        dies = {
            (entry["package"], entry["die"])
            for entry in topology
            if entry["die"] is not None
        }
        if dies:
            section.add(self.row("dies", "Dies", str(len(dies))))

        # The CCX derivation: cores that share one L3 instance form one
        # complex. On a monolithic part this comes out as a single group, which
        # is the correct answer for that machine rather than a missing row.
        groups = self._l3_groups(topology)
        if groups:
            sizes = sorted({len(cores) for cores in groups})
            if len(sizes) == 1:
                shape = f"{len(groups)} × {sizes[0]} core" + ("s" if sizes[0] != 1 else "")
            else:
                shape = ", ".join(str(len(cores)) for cores in groups) + " cores"
            section.add(
                self.row(
                    "ccx",
                    "Core complexes",
                    f"{len(groups)} sharing one L3 each — {shape}",
                )
            )

        online = read_first_line(f"{CPU_ROOT}/online")
        offline = read_first_line(f"{CPU_ROOT}/offline")
        present = read_first_line(f"{CPU_ROOT}/present")
        section.add(self.row("online", "Online", or_missing(online, NOT_AVAILABLE)))
        section.add(
            self.row(
                "offline",
                "Offline",
                offline if offline else "None — every processor is online",
                severity=WARNING if offline else "normal",
            )
        )
        if present and present != online:
            section.add(self.row("present", "Present", present))

        for entry in topology:
            siblings = [t for t in entry["threads"] if t != entry["cpu"]]
            detail = (
                f"package {_or_q(entry['package'])}, core {_or_q(entry['core_id'])}"
            )
            if siblings:
                detail += ", paired with cpu " + fmt_list(siblings, empty="none")
            section.add(
                self.row(
                    "thread_map",
                    f"cpu{entry['cpu']}",
                    detail,
                    key=f"thread{entry['cpu']}",
                )
            )
        return section

    def _l3_groups(self, topology) -> list[list[int]]:
        """Cores grouped by the L3 instance they share."""
        groups: dict[tuple, set] = {}
        for entry in topology:
            cpu = entry["cpu"]
            for index in glob_dirs(f"{CPU_ROOT}/cpu{cpu}/cache/index[0-9]*"):
                if read_int(index / "level") != 3:
                    continue
                shared = parse_cpu_list(read_first_line(index / "shared_cpu_list"))
                cache_id = read_int(index / "id")
                key = (cache_id,) if cache_id is not None else tuple(shared)
                groups.setdefault(key, set())
                if entry["core_id"] is not None:
                    groups[key].add((entry["package"], entry["core_id"]))
                else:
                    groups[key].add(cpu)
        return [sorted(cores, key=lambda c: (str(c))) for cores in groups.values() if cores]

    # -- clocks -----------------------------------------------------------

    def _clocks(self, cpus) -> Section:
        section = Section(id="clocks", label="Clocks")
        base = f"{CPU_ROOT}/cpu{cpus[0]}/cpufreq" if cpus else None
        if base is None or read_first_line(f"{base}/scaling_driver") is None:
            section.add(
                self.row(
                    "governor",
                    "Frequency scaling",
                    "This kernel exposes no cpufreq interface, so clock speeds and "
                    "the governor cannot be read. A virtual machine or a firmware-"
                    "managed processor will look like this.",
                )
            )
            self._add_cpuinfo_mhz(section, cpus)
            return section

        section.add(
            self.row("driver", "Scaling driver", or_missing(read_first_line(f"{base}/scaling_driver")))
        )
        section.add(
            self.row(
                "governor",
                "Governor",
                or_missing(read_first_line(f"{base}/scaling_governor")),
                tier=VOLATILE,
            )
        )
        epp = read_first_line(f"{base}/energy_performance_preference")
        if epp:
            section.add(
                self.row("epp", "Energy performance preference", epp, tier=VOLATILE)
            )
        section.add(
            self.row("max_freq", "Maximum frequency", fmt_khz(read_int(f"{base}/cpuinfo_max_freq")))
        )
        section.add(
            self.row("min_freq", "Minimum frequency", fmt_khz(read_int(f"{base}/cpuinfo_min_freq")))
        )
        base_freq = read_int(f"{base}/base_frequency")
        if base_freq is not None:
            section.add(self.row("base_freq", "Base frequency", fmt_khz(base_freq)))

        boost = read_first_line(f"{CPU_ROOT}/cpufreq/boost")
        if boost is not None:
            section.add(
                self.row(
                    "boost",
                    "Boost",
                    "Enabled" if boost == "1" else "Disabled",
                    tier=VOLATILE,
                )
            )

        for row in self._clock_rows(cpus):
            section.add(row)
        return section

    def _add_cpuinfo_mhz(self, section: Section, cpus) -> None:
        """The one clock reading available without cpufreq."""
        blocks = parse_cpuinfo(read_text("/proc/cpuinfo"))
        for block in blocks:
            mhz = block.get("cpu MHz")
            number = block.get("processor")
            if mhz is None or number is None:
                continue
            section.add(
                self.row(
                    "core_clock",
                    f"cpu{number}",
                    f"{mhz} MHz (as reported by /proc/cpuinfo)",
                    tier=VOLATILE,
                    key=f"clock{number}",
                )
            )

    def _clock_rows(self, cpus) -> list:
        """Per-core current frequency, plus the spread across all of them."""
        readings = []
        rows = []
        for cpu in cpus:
            value = read_int(f"{CPU_ROOT}/cpu{cpu}/cpufreq/scaling_cur_freq")
            if value is not None:
                readings.append(value)
            rows.append(
                self.row(
                    "core_clock",
                    f"cpu{cpu}",
                    fmt_khz(value),
                    tier=VOLATILE,
                    key=f"clock{cpu}",
                )
            )
        summary = []
        if readings:
            summary.append(
                self.row(
                    "clock_spread",
                    "Lowest, average, highest",
                    "{} · {} · {}".format(
                        fmt_khz(min(readings)),
                        fmt_khz(sum(readings) / len(readings)),
                        fmt_khz(max(readings)),
                    ),
                    tier=VOLATILE,
                )
            )
        return summary + rows

    # -- cache ------------------------------------------------------------

    def _cache(self, caches, topology) -> Section:
        section = Section(id="cache", label="Cache")
        if not caches:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "This kernel exposes no cache topology, so the cache tree cannot be read.",
                )
            )
            return section

        grouped: dict[tuple, list] = {}
        for cache in caches:
            grouped.setdefault((cache["level"], cache["type"]), []).append(cache)

        for (level, kind), members in grouped.items():
            sizes = [c["size"] for c in members if c["size"]]
            total = sum(sizes) if sizes else None
            each = sizes[0] if sizes and len(set(sizes)) == 1 else None
            shared_width = len(members[0]["shared"])

            if each is not None and len(members) > 1:
                value = f"{len(members)} × {fmt_bytes(each)} = {fmt_bytes(total)}"
            elif total is not None:
                value = fmt_bytes(total)
            else:
                value = NOT_AVAILABLE

            detail = []
            if members[0]["ways"]:
                detail.append(f"{members[0]['ways']}-way")
            if members[0]["line"]:
                detail.append(f"{members[0]['line']} B line")
            if shared_width > 1:
                detail.append(f"shared by {shared_width} threads")
            elif shared_width == 1:
                detail.append("private per thread")
            if detail:
                value = f"{value}, {', '.join(detail)}"

            section.add(
                self.row(
                    f"cache_l{level}",
                    _cache_label(level, kind),
                    value,
                    key=f"cache{level}{kind}",
                )
            )

        total_all = sum(c["size"] for c in caches if c["size"])
        if total_all:
            section.add(self.row("cache_total", "Total cache", fmt_bytes(total_all)))
        return section

    # -- instruction sets -------------------------------------------------

    def _instructions(self, flags) -> Section:
        section = Section(id="instructions", label="Instruction sets")
        if not flags:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "/proc/cpuinfo reported no feature flags on this machine.",
                )
            )
            return section

        available = set(flags)
        level = _microarchitecture_level(available)
        section.add(self.row("microarch_level", "Microarchitecture level", level))

        for title, names in FLAG_GROUPS:
            if names is None:
                present = sorted(f for f in available if f.startswith("avx512"))
            else:
                present = [name for name in names if name in available]
            section.add(
                self.row(
                    "flag_group",
                    title,
                    fmt_list(present, empty="Not supported"),
                    key=f"group{title}",
                )
            )

        section.add(
            self.row("flag_count", "Total flags reported", str(len(flags)))
        )
        section.add(self.row("flags", "All flags", " ".join(flags)))
        return section

    # -- vulnerabilities --------------------------------------------------

    def _vulnerabilities(self) -> Section:
        section = Section(id="vulnerabilities", label="Vulnerabilities")
        entries = []
        for path in list_dir(f"{CPU_ROOT}/vulnerabilities"):
            text = read_text(path)
            if text is None:
                continue
            entries.append((path.name, text))

        if not entries:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "This kernel reports no CPU vulnerability status, which means it "
                    "predates the interface rather than that the processor is unaffected.",
                )
            )
            return section

        vulnerable = [name for name, text in entries if _is_vulnerable(text)]
        section.add(
            self.row(
                "vuln_summary",
                "Summary",
                f"{len(entries)} tracked, {len(vulnerable)} not mitigated"
                if vulnerable
                else f"{len(entries)} tracked, all mitigated or not affected",
                severity=WARNING if vulnerable else "normal",
            )
        )
        for name, text in entries:
            section.add(
                self.row(
                    "vuln",
                    name.replace("_", " ").capitalize(),
                    text,
                    severity=WARNING if _is_vulnerable(text) else "normal",
                    key=f"vuln{name}",
                )
            )
        return section

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        cpus = self._cpus or self._online_cpus()
        rows = []
        base = f"{CPU_ROOT}/cpu{cpus[0]}/cpufreq" if cpus else None
        if base is not None and read_first_line(f"{base}/scaling_driver") is not None:
            governor = read_first_line(f"{base}/scaling_governor")
            if governor:
                rows.append(self.row("governor", "Governor", governor, tier=VOLATILE))
            epp = read_first_line(f"{base}/energy_performance_preference")
            if epp:
                rows.append(
                    self.row("epp", "Energy performance preference", epp, tier=VOLATILE)
                )
            boost = read_first_line(f"{CPU_ROOT}/cpufreq/boost")
            if boost is not None:
                rows.append(
                    self.row(
                        "boost",
                        "Boost",
                        "Enabled" if boost == "1" else "Disabled",
                        tier=VOLATILE,
                    )
                )
            rows.extend(self._clock_rows(cpus))
        else:
            section = Section(id="clocks", label="Clocks")
            self._add_cpuinfo_mhz(section, cpus)
            rows.extend(section.rows)
        return {"clocks": rows}


# -- helpers --------------------------------------------------------------


def _parse_cache_size(text: str | None) -> int | None:
    """``32K`` or ``1024K`` or ``8M`` -> bytes."""
    if not text:
        return None
    text = text.strip()
    multiplier = 1
    if text[-1:] in ("K", "k"):
        multiplier, text = 1024, text[:-1]
    elif text[-1:] in ("M", "m"):
        multiplier, text = 1024 * 1024, text[:-1]
    elif text[-1:] in ("G", "g"):
        multiplier, text = 1024 * 1024 * 1024, text[:-1]
    try:
        return int(text) * multiplier
    except ValueError:
        return None


def _cache_label(level: int, kind: str) -> str:
    if kind == "Data":
        return f"L{level} data"
    if kind == "Instruction":
        return f"L{level} instruction"
    return f"L{level}"


def _is_vulnerable(text: str) -> bool:
    return any(text.startswith(prefix) for prefix in UNMITIGATED_PREFIXES)


def _microarchitecture_level(flags: set) -> str:
    reached = "x86-64 (baseline)"
    for name, required in MICROARCH_LEVELS:
        if all(flag in flags for flag in required):
            reached = name
        else:
            break
    return reached


def _or_q(value) -> str:
    return "?" if value is None else str(value)


def _is_little_endian() -> bool:
    return sys.byteorder == "little"
