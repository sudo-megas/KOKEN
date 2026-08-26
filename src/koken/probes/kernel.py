# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The kernel: which one, started how, with what loaded.

The command line gets one row per parameter rather than a single long string,
because that is the form in which people actually need it. Someone checking
whether ``amd_iommu=on`` survived a firmware update wants to scan a list, not
read a paragraph and count spaces.

The flavour is surfaced separately from the version. A CachyOS kernel and a
stock Arch kernel both report a version like ``6.11.4``, and the difference
between them - scheduler, compiler, patch set - lives in the local version
suffix and in the build string, not in the numbers.
"""

from __future__ import annotations

import os
import platform
import re

from .base import (
    NONE_PRESENT,
    NOT_AVAILABLE,
    NOT_REPORTED,
    VOLATILE,
    WARNING,
    Probe,
    Section,
    fmt_bytes,
    fmt_int,
    fmt_list,
    or_missing,
    read_first_line,
    read_int,
    read_lines,
    read_text,
)

# /proc/sys/kernel/tainted, bit by bit. Only the bits worth naming are here;
# an unnamed bit still shows as its number so nothing is silently dropped.
TAINT_FLAGS = {
    0: "a proprietary module was loaded",
    1: "a module was force loaded",
    2: "the kernel is running on an out of specification system",
    3: "a module was force unloaded",
    4: "a machine check exception occurred",
    5: "a bad page was found",
    6: "a user requested the taint",
    7: "the system died from an oops",
    8: "an ACPI table was overridden",
    9: "a warning was issued",
    10: "a staging driver was loaded",
    11: "the kernel is working around firmware bugs",
    12: "a virtual machine module was loaded",
    13: "an out of tree module was loaded",
    14: "an unsigned module was loaded",
    15: "a soft lockup occurred",
    16: "a module was live patched",
    17: "an auxiliary taint was set",
    18: "a structurally randomised struct was used",
    19: "an in-kernel test was run",
}

# Distribution kernels that name themselves in the local version suffix.
FLAVOURS = {
    "cachyos": "CachyOS",
    "zen": "Zen",
    "lts": "Long term support",
    "hardened": "Hardened",
    "rt": "Real time",
    "xanmod": "XanMod",
    "liquorix": "Liquorix",
    "generic": "Generic",
    "lowlatency": "Low latency",
    "arch": "Arch",
    "mainline": "Mainline",
    "surface": "Surface",
    "amd": "AMD",
}

# The compiler field contains its own brackets - "gcc (GCC) 15.2.0, GNU ld (GNU
# Binutils) 2.46" - so it cannot be matched non-greedily up to the first close
# bracket. Anchoring the tail on the build number, which always begins with a
# hash, makes the greedy match give back exactly the right amount.
_VERSION_LINE = re.compile(
    r"^Linux version (?P<release>\S+) \((?P<builder>[^)]*)\) "
    r"\((?P<compiler>.*)\) (?P<rest>#.*)$"
)

# Written into the build string by the kernel build system. The preemption
# model is the single biggest behavioural difference between a stock kernel and
# a desktop-tuned one, and it is recorded nowhere else.
PREEMPTION_MODELS = (
    ("PREEMPT_RT", "Real time — the strongest preemption the kernel offers"),
    ("PREEMPT_DYNAMIC", "Dynamic — the model can be chosen at boot"),
    ("PREEMPT_VOLUNTARY", "Voluntary — the kernel yields at explicit points"),
    ("PREEMPT", "Preemptible — tuned for desktop responsiveness"),
)


def parse_modules(lines) -> list[dict]:
    """``/proc/modules``: name, size, use count, dependencies, state."""
    modules = []
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        deps = parts[3]
        modules.append(
            {
                "name": parts[0],
                "size": _int_or_none(parts[1]),
                "used_by_count": _int_or_none(parts[2]) or 0,
                "used_by": [] if deps == "-" else [d for d in deps.split(",") if d],
                "state": parts[4] if len(parts) > 4 else "",
            }
        )
    return modules


def _int_or_none(text: str) -> int | None:
    try:
        return int(text)
    except (ValueError, TypeError):
        return None


def decode_taint(value: int | None) -> list[str]:
    """The taint bitmap into the list of reasons it is set.

    Negative is refused rather than decoded. ``/proc/sys/kernel/tainted`` is an
    unsigned long and never negative, but a Python int is not a machine word:
    ``-1 & (1 << bit)`` is true for every bit, so a garbage read would be
    rendered as a kernel tainted in all eighteen ways at once.
    """
    if not value or value < 0:
        return []
    out = []
    for bit in range(0, 32):
        if value & (1 << bit):
            out.append(TAINT_FLAGS.get(bit, f"bit {bit} is set"))
    return out


def parse_cmdline(text: str) -> list[tuple[str, str | None]]:
    """``/proc/cmdline`` into (parameter, value) pairs, the way the kernel does.

    A plain ``text.split()`` is wrong, and wrong in a way that shows. The
    kernel's own ``next_arg`` in ``init/main.c`` honours double quotes, so
    ``dm-mod.create="root,,,ro,0 4096 linear /dev/sda2 0"`` - which is what an
    encrypted or LVM root on a dracut system boots with - is one parameter.
    Splitting on whitespace turns it into six, five of which are fragments with
    no meaning at all, and inflates the parameter count to match.
    """
    out: list[tuple[str, str | None]] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        start = index
        quoted = False
        if text[index] == '"':
            index += 1
            start = index
            quoted = True
        in_quote = quoted
        equals = 0
        offset = 0
        while index < length:
            character = text[index]
            if character.isspace() and not in_quote:
                break
            if equals == 0 and character == "=":
                equals = offset
            if character == '"':
                in_quote = not in_quote
            index += 1
            offset += 1
        token = text[start:index]
        if quoted and token.endswith('"'):
            token = token[:-1]
        if not equals:
            out.append((token, None))
        else:
            name = token[:equals]
            value = token[equals + 1 :]
            # The kernel strips the quotes from the value rather than showing
            # them, so `root="/dev/sda 1"` reads as `/dev/sda 1`.
            if value.startswith('"'):
                value = value[1:]
                if value.endswith('"'):
                    value = value[:-1]
            out.append((name, value))
        index += 1
    return out


def kernel_flavour(release: str) -> tuple[str | None, str | None]:
    """``6.11.4-2-cachyos`` -> ``("cachyos", "CachyOS")``."""
    if not release:
        return None, None
    # Everything after the first dash is the local version.
    _, _, local = release.partition("-")
    if not local:
        return None, None
    for token in reversed(re.split(r"[-.+_]", local)):
        token = token.lower()
        if token in FLAVOURS:
            return token, FLAVOURS[token]
    return local, None


class KernelProbe(Probe):
    branch = "system"
    id = "kernel"
    label = "Kernel"

    def sections(self) -> list[Section]:
        return [self._overview(), self._cmdline(), self._modules()]

    # -- overview ---------------------------------------------------------

    def _overview(self) -> Section:
        section = Section(id="overview", label="Overview")
        release = platform.release() or ""
        version_text = read_first_line("/proc/version") or ""
        match = _VERSION_LINE.match(version_text)

        section.add(self.row("release", "Release", or_missing(release, NOT_AVAILABLE)))

        token, flavour = kernel_flavour(release)
        if flavour:
            section.add(
                self.row("flavour", "Flavour", f"{flavour} — local version suffix “{token}”")
            )
        elif token:
            section.add(
                self.row(
                    "flavour",
                    "Flavour",
                    f"Local version suffix “{token}”, which is not one this build recognises",
                )
            )
        else:
            section.add(
                self.row(
                    "flavour",
                    "Flavour",
                    "No local version suffix, so this is a plain upstream version string",
                )
            )

        if match:
            section.add(
                self.row("compiler", "Built with", or_missing(match.group("compiler"), NOT_REPORTED))
            )
            section.add(
                self.row("builder", "Built by", or_missing(match.group("builder"), NOT_REPORTED))
            )
            rest = (match.group("rest") or "").strip()
            if rest:
                section.add(self.row("build", "Build", rest))
                model = _preemption(rest)
                if model:
                    section.add(self.row("preemption", "Preemption model", model))
        section.add(
            self.row("version_string", "Version string", or_missing(version_text, NOT_AVAILABLE))
        )
        section.add(
            self.row("architecture", "Architecture", platform.machine() or NOT_AVAILABLE)
        )

        taint = read_int("/proc/sys/kernel/tainted")
        flags = decode_taint(taint)
        if taint is None or taint < 0:
            taint_text = NOT_AVAILABLE
        elif not flags:
            taint_text = "Not tainted"
        else:
            taint_text = f"{taint} — {'; '.join(flags)}"
        section.add(
            self.row(
                "tainted",
                "Taint",
                taint_text,
                severity=WARNING if flags else "normal",
            )
        )

        for field, label, path, formatter in (
            ("osrelease", "Reported release", "/proc/sys/kernel/osrelease", None),
            ("hostname", "Host name", "/proc/sys/kernel/hostname", None),
            ("domainname", "Domain name", "/proc/sys/kernel/domainname", None),
        ):
            value = read_first_line(path)
            if value:
                section.add(self.row(field, label, value))

        page_size = _page_size()
        if page_size:
            section.add(self.row("page_size", "Page size", fmt_bytes(page_size)))

        entropy = read_int("/proc/sys/kernel/random/entropy_avail")
        if entropy is not None:
            section.add(
                self.row("entropy", "Entropy available", f"{entropy} bits", tier=VOLATILE)
            )

        threads_max = read_int("/proc/sys/kernel/threads-max")
        if threads_max is not None:
            section.add(self.row("threads_max", "Maximum threads", fmt_int(threads_max)))

        pid_max = read_int("/proc/sys/kernel/pid_max")
        if pid_max is not None:
            section.add(self.row("pid_max", "Highest process id", fmt_int(pid_max)))
        return section

    # -- command line -----------------------------------------------------

    def _cmdline(self) -> Section:
        section = Section(id="cmdline", label="Command line")
        text = read_first_line("/proc/cmdline")
        if not text:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "/proc/cmdline could not be read, so the boot parameters are unknown.",
                )
            )
            return section

        section.add(self.row("cmdline", "Full command line", text))
        parameters = parse_cmdline(text)
        section.add(self.row("cmdline_count", "Parameters", str(len(parameters))))
        for index, (name, value) in enumerate(parameters):
            section.add(
                self.row(
                    "cmdline_parameter",
                    f"  {name}",
                    "set, with no value" if value is None else value,
                    key=f"param{index}{name}",
                )
            )
        return section

    # -- modules ----------------------------------------------------------

    def _modules(self) -> Section:
        section = Section(id="modules", label="Modules")
        modules = parse_modules(read_lines("/proc/modules"))
        if not modules:
            section.add(
                self.row(
                    "absent",
                    "Status",
                    "No loadable modules are present. A kernel with everything built "
                    "in, or one with module loading disabled, looks like this.",
                )
            )
            return section

        total = sum(module["size"] or 0 for module in modules)
        section.add(self.row("module_count", "Loaded modules", str(len(modules))))
        section.add(self.row("module_memory", "Memory used", fmt_bytes(total)))

        unused = [module["name"] for module in modules if not module["used_by_count"]]
        section.add(
            self.row(
                "modules_unused",
                "Loaded but unused",
                f"{len(unused)} of {len(modules)}",
            )
        )

        for module in sorted(modules, key=lambda item: item["name"]):
            detail = fmt_bytes(module["size"])
            if module["used_by"]:
                detail += f", used by {', '.join(module['used_by'])}"
            elif module["used_by_count"]:
                detail += f", {module['used_by_count']} user(s)"
            else:
                detail += ", not in use"
            if module["state"] and module["state"] != "Live":
                detail += f" — {module['state']}"
            section.add(
                self.row(
                    "module",
                    f"  {module['name']}",
                    detail,
                    key=f"mod{module['name']}",
                )
            )
        return section

    # -- volatile pass ----------------------------------------------------

    def sample(self) -> dict[str, list]:
        entropy = read_int("/proc/sys/kernel/random/entropy_avail")
        if entropy is None:
            return {}
        return {
            "overview": [
                self.row("entropy", "Entropy available", f"{entropy} bits", tier=VOLATILE)
            ]
        }


def _preemption(build: str) -> str | None:
    for token, description in PREEMPTION_MODELS:
        if token in build:
            return f"{token} — {description.split(' — ', 1)[-1]}"
    return None


def _page_size() -> int | None:
    try:
        return os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
