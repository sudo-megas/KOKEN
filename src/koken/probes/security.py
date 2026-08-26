# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""What is switched on to keep this machine honest.

Most of these are single files holding a single number, and every one of them
is a switch somebody could have set either way. The section exists so that
"is Secure Boot actually on?" has an answer that takes two seconds to find
instead of a reboot into firmware.

Secure Boot deserves a note. The EFI variable is not a boolean file - it is
four bytes of variable attributes followed by the value, so the state is the
fifth byte. Reading the file whole and testing it for truth reports Secure Boot
as enabled on every UEFI machine ever built, including the ones where it is
switched off.
"""

from __future__ import annotations

from .base import (
    NONE_PRESENT,
    NOT_AVAILABLE,
    WARNING,
    Probe,
    Section,
    fmt_list,
    glob_paths,
    list_dir,
    or_missing,
    path_exists,
    read_bytes,
    read_first_line,
    read_int,
    read_text,
)

EFIVARS = "/sys/firmware/efi/efivars"
# The GUID every firmware uses for the global EFI variables.
GLOBAL_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"

# Kernel switches worth a row: path, label, field, and how to read the value.
SYSCTL_SWITCHES = (
    (
        "/proc/sys/kernel/yama/ptrace_scope",
        "Ptrace restriction",
        "ptrace_scope",
        {
            "0": "0 — any process may trace any other of the same user",
            "1": "1 — only a parent may trace its own children",
            "2": "2 — only the administrator may trace",
            "3": "3 — tracing is disabled entirely",
        },
    ),
    (
        "/proc/sys/kernel/kptr_restrict",
        "Kernel pointer hiding",
        "kptr_restrict",
        {
            "0": "0 — kernel addresses are shown to everyone",
            "1": "1 — kernel addresses are hidden from unprivileged users",
            "2": "2 — kernel addresses are hidden from everyone",
        },
    ),
    (
        "/proc/sys/kernel/dmesg_restrict",
        "Kernel log restriction",
        "dmesg_restrict",
        {
            "0": "0 — any user may read the kernel log",
            "1": "1 — only the administrator may read the kernel log",
        },
    ),
    (
        "/proc/sys/kernel/unprivileged_bpf_disabled",
        "Unprivileged BPF",
        "unprivileged_bpf",
        {
            "0": "0 — unprivileged programs may load BPF",
            "1": "1 — unprivileged BPF is disabled and the setting is locked",
            "2": "2 — unprivileged BPF is disabled",
        },
    ),
    (
        "/proc/sys/kernel/randomize_va_space",
        "Address space randomisation",
        "aslr",
        {
            "0": "0 — disabled",
            "1": "1 — stack and libraries are randomised",
            "2": "2 — full randomisation, including the heap",
        },
    ),
)

# A value that is not the safer one earns a warning, so the row reads as a
# finding rather than a fact.
EXPECTED = {
    "ptrace_scope": ("1", "2", "3"),
    "kptr_restrict": ("1", "2"),
    "dmesg_restrict": ("1",),
    "unprivileged_bpf": ("1", "2"),
    "aslr": ("2",),
}


def read_efi_variable(name: str, guid: str = GLOBAL_GUID) -> int | None:
    """The value byte of an EFI variable.

    An efivars file is four bytes of attribute flags followed by the data. For
    these one-byte booleans the value is therefore at offset 4, and nowhere
    else.
    """
    blob = read_bytes(f"{EFIVARS}/{name}-{guid}")
    if blob is None or len(blob) < 5:
        return None
    return blob[4]


def parse_bracketed(text: str | None) -> str | None:
    """``[none] integrity confidentiality`` -> ``none``."""
    if not text:
        return None
    for token in text.split():
        if token.startswith("[") and token.endswith("]"):
            return token[1:-1]
    return None


class SecurityProbe(Probe):
    branch = "system"
    id = "security"
    label = "Security"

    def sections(self) -> list[Section]:
        section = Section(id="overview", label="Overview")
        for row in self._boot_rows():
            section.add(row)
        for row in self._lockdown_rows():
            section.add(row)
        for row in self._lsm_rows():
            section.add(row)
        for row in self._iommu_rows():
            section.add(row)
        for row in self._switch_rows():
            section.add(row)
        for row in self._tpm_rows():
            section.add(row)
        for row in self._vulnerability_rows():
            section.add(row)
        return [section]

    # -- boot -------------------------------------------------------------

    def _boot_rows(self) -> list:
        rows = []
        efi = path_exists("/sys/firmware/efi")
        rows.append(
            self.row(
                "uefi",
                "Firmware type",
                "UEFI" if efi else "Legacy BIOS — Secure Boot does not exist on this machine",
            )
        )
        if not efi:
            return rows

        if not path_exists(EFIVARS):
            rows.append(
                self.row(
                    "secure_boot",
                    "Secure Boot",
                    "The EFI variable filesystem is not mounted at "
                    f"{EFIVARS}, so the state cannot be read.",
                )
            )
            return rows

        value = read_efi_variable("SecureBoot")
        if value is None:
            rows.append(
                self.row(
                    "secure_boot",
                    "Secure Boot",
                    "The SecureBoot variable could not be read. Some firmware "
                    "does not publish it, and some kernels restrict it.",
                )
            )
        else:
            enabled = value == 1
            rows.append(
                self.row(
                    "secure_boot",
                    "Secure Boot",
                    "Enabled" if enabled else f"Disabled (the variable reads {value})",
                    severity="normal" if enabled else WARNING,
                )
            )

        setup = read_efi_variable("SetupMode")
        if setup is not None:
            rows.append(
                self.row(
                    "setup_mode",
                    "Firmware setup mode",
                    "Active — the platform keys have been cleared and any signature "
                    "may be enrolled"
                    if setup == 1
                    else "Not active — the platform keys are enrolled",
                    severity=WARNING if setup == 1 else "normal",
                )
            )

        sig_enforce = read_first_line("/sys/module/module/parameters/sig_enforce")
        if sig_enforce is not None:
            enforcing = sig_enforce.strip().upper() in ("Y", "1")
            rows.append(
                self.row(
                    "module_signing",
                    "Module signature enforcement",
                    "Enforced — the kernel refuses unsigned modules"
                    if enforcing
                    else "Not enforced — unsigned modules may be loaded",
                )
            )
        return rows

    # -- lockdown ---------------------------------------------------------

    def _lockdown_rows(self) -> list:
        text = read_text("/sys/kernel/security/lockdown")
        if text is None:
            return [
                self.row(
                    "lockdown",
                    "Kernel lockdown",
                    "Not available. This kernel was built without lockdown, or "
                    "securityfs is not mounted at /sys/kernel/security.",
                )
            ]
        active = parse_bracketed(text) or text
        return [
            self.row(
                "lockdown",
                "Kernel lockdown",
                f"{active} — of {text.replace('[', '').replace(']', '')}",
            )
        ]

    # -- security modules -------------------------------------------------

    def _lsm_rows(self) -> list:
        rows = []
        lsm = read_first_line("/sys/kernel/security/lsm")
        rows.append(
            self.row(
                "lsm",
                "Security modules",
                or_missing(lsm, "Not listed. securityfs may not be mounted."),
            )
        )

        selinux = read_first_line("/sys/fs/selinux/enforce")
        if selinux is not None:
            rows.append(
                self.row(
                    "selinux",
                    "SELinux",
                    "Enforcing" if selinux == "1" else "Permissive — policy is loaded but not applied",
                )
            )
        apparmor = path_exists("/sys/kernel/security/apparmor")
        if apparmor:
            profiles = read_text("/sys/kernel/security/apparmor/profiles")
            count = len(profiles.splitlines()) if profiles else 0
            rows.append(
                self.row(
                    "apparmor",
                    "AppArmor",
                    f"Loaded, with {count} profile(s)" if count else "Loaded",
                )
            )
        if selinux is None and not apparmor:
            rows.append(
                self.row(
                    "mac",
                    "Mandatory access control",
                    "Neither SELinux nor AppArmor is active on this machine.",
                )
            )
        return rows

    # -- iommu ------------------------------------------------------------

    def _iommu_rows(self) -> list:
        groups = list_dir("/sys/kernel/iommu_groups")
        devices = list_dir("/sys/class/iommu")
        if not groups and not devices:
            return [
                self.row(
                    "iommu",
                    "IOMMU",
                    "Not enabled. Nothing appears under /sys/class/iommu, so device "
                    "memory access is not being isolated.",
                    severity=WARNING,
                )
            ]
        rows = [
            self.row(
                "iommu",
                "IOMMU",
                "Enabled — {} group(s) across {} unit(s)".format(
                    len(groups), len(devices) or 1
                ),
            )
        ]
        if devices:
            rows.append(
                self.row(
                    "iommu_units",
                    "IOMMU units",
                    fmt_list(path.name for path in devices),
                )
            )
        return rows

    # -- kernel switches --------------------------------------------------

    def _switch_rows(self) -> list:
        rows = []
        for path, label, field, meanings in SYSCTL_SWITCHES:
            raw = read_first_line(path)
            if raw is None:
                continue
            value = meanings.get(raw, raw)
            expected = EXPECTED.get(field, ())
            rows.append(
                self.row(
                    field,
                    label,
                    value,
                    severity=WARNING if expected and raw not in expected else "normal",
                )
            )
        if not rows:
            rows.append(
                self.row(
                    "switches",
                    "Kernel hardening switches",
                    "None of the usual switches could be read under /proc/sys/kernel.",
                )
            )
        return rows

    # -- tpm --------------------------------------------------------------

    def _tpm_rows(self) -> list:
        chips = list_dir("/sys/class/tpm")
        if not chips:
            return [
                self.row(
                    "tpm",
                    "Trusted platform module",
                    "None present. Disk encryption that binds a key to the TPM is "
                    "not possible on this machine.",
                )
            ]
        rows = []
        for chip in chips:
            version = read_first_line(chip / "tpm_version_major")
            description = read_first_line(chip / "device/description")
            detail = f"TPM {version}" if version else "Present"
            if description:
                detail += f" — {description}"
            rows.append(
                self.row(
                    "tpm",
                    f"Trusted platform module ({chip.name})",
                    detail,
                    key=f"tpm{chip.name}",
                )
            )
        return rows

    # -- cpu vulnerabilities ----------------------------------------------

    def _vulnerability_rows(self) -> list:
        entries = []
        for path in list_dir("/sys/devices/system/cpu/vulnerabilities"):
            text = read_text(path)
            if text:
                entries.append((path.name, text))
        if not entries:
            return [
                self.row(
                    "vulnerabilities",
                    "Processor vulnerabilities",
                    "This kernel does not report processor vulnerability status.",
                )
            ]
        vulnerable = [
            name
            for name, text in entries
            if text.startswith(("Vulnerable", "Unknown", "Processor vulnerable"))
        ]
        return [
            self.row(
                "vulnerabilities",
                "Processor vulnerabilities",
                f"{len(vulnerable)} of {len(entries)} not mitigated: {', '.join(vulnerable)}"
                if vulnerable
                else f"All {len(entries)} tracked issues are mitigated or not applicable",
                severity=WARNING if vulnerable else "normal",
            ),
            self.row(
                "vulnerabilities_detail",
                "Full list",
                "Hardware, CPU, Vulnerabilities has one row per issue with the "
                "kernel's exact wording",
            ),
        ]
