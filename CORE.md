# KÖKEN — CORE

Authoritative specification. This file does not change during the build. If reality
contradicts it, stop and report rather than improvising.

---

## 1. IDENTITY

| Field | Value |
|---|---|
| Display name | KÖKEN |
| Package name | `koken` |
| Binary | `koken` |
| Repository | `github.com/sudo-megas/KOKEN` |
| Licence | GPL-3.0-or-later |
| Version scheme | Two numerals — `v1.0`, `v1.1` |
| Family | Megas |

The name means *origin* or *root-source*. The app is a hardware and system device
browser: it reads what the machine is made of and explains, in plain language, what
each value means. Its subtitle, used on the banner and in the About section, is
**Machine Corpus**.

All user-facing text, documentation and comments are in English. The name and the
`Ö` in it are the only non-English elements in the project.

---

## 2. THE POINT OF THE APP

Every existing Linux system-information tool reformats the same jargon that `lshw`
and `lsblk` already print. KÖKEN's reason to exist is the **explanation layer**: every
row carries a plain-language expansion that says what the value means and why it
might matter. The data gathering is the easy half. The explanations are the product.

Second reason to exist: it is **offline and read-only**. No benchmarks, no result
uploading, no network of any kind.

---

## 3. DO-NOT RULES

These are absolute. Violating any of them is a build failure.

1. **No network access.** No update check, no telemetry, no analytics, no crash
   reporting, no benchmark upload. The app must function identically with the
   interface down.
2. **No popups, no dialogs, no modal windows.** One window, always. The only
   exception is the polkit authentication prompt, which belongs to the system, not
   to KÖKEN.
3. **No writes outside `$XDG_CONFIG_HOME/koken/`, and exactly one class of system
   action: mounting and unmounting filesystems** (§12). Nothing else. The app never
   loads a module, never changes a governor, never edits a config, never formats,
   never unlocks an encrypted volume, never powers off a device. If a proposed
   feature writes anything and is not literally `Filesystem.Mount` or
   `Filesystem.Unmount`, the answer is no.
4. **No shelling out to `lspci`, `lsusb`, `lshw`, `hwinfo` or `inxi`** and scraping
   their text. Read sysfs directly. Exactly two commands are permitted, both only
   inside the privileged helper and nowhere else in the application: `dmidecode`,
   because the DMI table has no other interface; and `smartctl`, because the
   per-attribute SMART table has no other interface this Qt binding can read.
   Neither is required for the application to run, and adding a third needs an
   amendment to this document.
5. **No AI attribution anywhere.** No `Co-Authored-By` trailers, no
   `Generated with` lines, no mention in commits, README, About page or release
   notes. This is the standing rule for this repository.
6. **No clickable links.** Addresses in the About section are selectable text. The
   app opens no browser and follows no URL.

---

## 4. STACK AND DEPENDENCIES

Python 3.11+ with PySide6. Every dependency below does real work; none is
decorative.

| Dependency | What it actually does | Could we drop it? |
|---|---|---|
| `python` ≥ 3.11 | `tomllib` is stdlib from 3.11, so explanation files parse with no third-party TOML library | No |
| `python-pyside6` | The GUI, and `QtDBus` for both udisks2 and the appearance portal | No |
| `hwdata` | `/usr/share/hwdata/pci.ids` and `usb.ids` — turns `1002:747e` into "Navi 32 [Radeon RX 7800 XT]". Data files, not a library | Only by shipping a stale copy of the same data |
| `udisks2` | SMART attributes over D-Bus with its own polkit policy, for both ATA and NVMe, plus the mount and unmount calls in §12. Saves writing a privileged SMART path and a privileged mount path | No — it is now load-bearing for two features |
| `dmidecode` | DMI type 17 for per-DIMM part number, speed, rank and slot. There is no sysfs equivalent | No |
| `polkit` | Authenticates the helper | No |

Explicitly **not** dependencies:

- `python-dbus` / `dbus-python` — PySide6 ships `QtDBus`.
- `pciutils` / `usbutils` — we read sysfs, we do not scrape `lspci`.
- `smartmontools` — **optional, not required.** udisks2 covers the SMART health
  verdict, temperature, power-on hours and bad-sector count, and those rows come
  from it whether or not smartmontools is installed. It does not cover the
  per-attribute table: `SmartGetAttributes` returns `a(ysqiiixia{sv})` for ATA and
  `a{sv}` for NVMe, and both arrive as a `QDBusArgument` that PySide6 cannot read —
  its extraction operator aborts the process. The table therefore comes from
  `smartctl --json` inside the privileged helper when it is installed, and the SMART
  tab says so plainly when it is not. Declared `optdepends` on Arch and `Suggests`
  on Debian, deliberately not `Recommends`: apt installs Recommends by default and
  Debian's smartmontools brings `smartd`, a monitoring daemon that starts at boot
  and mails root. A read-only browser does not get to make that change to a machine.
- Any theming, icon or widget library — Qt's own palette handling is enough.

---

## 5. NAVIGATION MODEL

Three horizontal tab rows, stacked with vertical gaps between them, each visually
quieter than the row above.

- **Row 1** — four fixed branches, always the same, equal width, largest.
- **Row 2** — populated by the row 1 selection. Equal width, medium.
- **Row 3** — populated by the row 2 selection. Smallest, underline style rather
  than filled pills.
- **Content** — a scrollable list of rows below.

### Rules

1. **Row 3 is never empty.** If a row 2 section has no meaningful subdivision, row 3
   renders a single `Overview` tab. The layout must never shift vertically when
   crossing tabs.
2. **Row 3 wraps.** When entries exceed the window width, they flow onto a second
   (and third) line. No horizontal scrolling, no dropdown, no ellipsis. This needs
   a flow layout — Qt has no built-in one, so implement the standard
   `FlowLayout` (roughly 70 lines, from the Qt widget examples).
3. **Selection is remembered per branch.** Returning to `Hardware` restores
   `CPU → Clocks`, not the default. Held in memory for the session; the last row 1
   selection persists to config.
4. **Row 3 means different things in different branches** — sometimes property
   groups, sometimes device instances. This is intentional. Section 6 fixes it
   per branch.
5. **Keyboard**: `Ctrl+1..4` selects row 1. `Tab` / `Shift+Tab` moves through row 2.
   `←` / `→` moves through row 3. `F5` forces a refresh. `Ctrl+Q` quits.

---

## 6. THE BRANCH TREE

Row 3 entries marked *(instances)* are generated from the hardware present, one tab
per device, and therefore vary per machine. All others are fixed.

### 6.1 Hardware

| Row 2 | Row 3 |
|---|---|
| CPU | Overview · Cores and threads · Clocks · Cache · Instruction sets · Vulnerabilities |
| Memory | Overview · Modules |
| Graphics | *(instances — one per DRM card)* |
| Displays | *(instances — one per connected output)* |
| Motherboard | Overview · Firmware · Chassis |

### 6.2 System

| Row 2 | Row 3 |
|---|---|
| Operating system | Overview · Distribution · Init · Packages · File types |
| Kernel | Overview · Command line · Modules |
| Desktop | Overview · Session · Toolkits · Portals |
| Security | Overview |

### 6.3 Storage

| Row 2 | Row 3 |
|---|---|
| Disks | *(instances — one per physical block device)* |
| Volumes | *(instances — one per partition)* |
| SMART | *(instances — one per drive with SMART data)* |
| Filesystems | Mounts · Swap |

`Disks` keeps the headline SMART rows — the health verdict, temperature,
power-on hours, reallocated sectors — because somebody looking at a drive
should see at a glance whether it is failing. `SMART` carries the full
per-attribute table, which is long enough to bury everything else in the
section it would otherwise sit in.

### 6.4 Peripherals

| Row 2 | Row 3 |
|---|---|
| USB | *(instances)* |
| PCI | *(instances)* |
| Network | *(instances — one per interface)* |
| Audio | *(instances — one per card)* |
| Input | *(instances)* |
| Power | Overview · Battery · Supplies |
| Sensors | *(instances — one per hwmon chip)* |

On a desktop with no battery, `Power → Battery` still renders, with rows explaining
that no battery is present. Absent hardware is stated, never hidden.

---

## 7. DATA SOURCES

Unprivileged unless marked. Every path below is read directly; nothing is scraped
from another tool's output.

### 7.1 CPU

| Path | Yields |
|---|---|
| `/proc/cpuinfo` | Model name, flags, bogomips, stepping, microcode |
| `/sys/devices/system/cpu/cpu*/topology/` | `core_id`, `physical_package_id`, `die_id`, `thread_siblings_list` — real core/thread mapping and CCX layout |
| `/sys/devices/system/cpu/cpu*/cpufreq/` | `scaling_cur_freq`, `scaling_governor`, `scaling_driver`, `cpuinfo_max_freq`, `energy_performance_preference` |
| `/sys/devices/system/cpu/cpu*/cache/index*/` | `level`, `type`, `size`, `ways_of_associativity`, `shared_cpu_list` — build the real cache tree, including whether L3 is shared across the whole CCX |
| `/sys/devices/system/cpu/vulnerabilities/*` | One file per known CPU vulnerability, contents state mitigation status |
| `/sys/devices/system/cpu/{online,offline,present}` | Parked or disabled cores |

### 7.2 Memory

| Path | Yields | Privilege |
|---|---|---|
| `/proc/meminfo` | Total, available, buffers, cached, swap, hugepages | — |
| `dmidecode -t 17` | Per-DIMM: size, speed, configured speed, manufacturer, part number, serial, rank, form factor, bank locator | **root** |
| `dmidecode -t 16` | Array-level: max capacity, slot count, error correction | **root** |

Channel configuration is derived from the bank locators returned by type 17, not
read directly.

### 7.3 Graphics

| Path | Yields | Privilege |
|---|---|---|
| `/sys/class/drm/card*/device/` | `vendor`, `device`, `subsystem_vendor`, `subsystem_device`, `revision`, driver name via the `driver` symlink | — |
| `/sys/class/drm/card*/device/` | `current_link_speed`, `current_link_width`, `max_link_speed`, `max_link_width` — the x8-when-it-should-be-x16 check | — |
| `/sys/class/drm/card*/device/mem_info_vram_total`, `mem_info_vram_used` | VRAM size and usage (amdgpu) | — |
| `/sys/class/drm/card*/device/hwmon/hwmon*/` | GPU edge, junction and memory temperature, fan RPM, power draw, power cap | — |
| `/sys/class/drm/card*/device/pp_dpm_sclk`, `pp_dpm_mclk` | Available and current DPM states (amdgpu) | — |
| `/sys/kernel/debug/dri/*/amdgpu_firmware_info` | **VBIOS version string, feature version, firmware versions per block** | **root** (debugfs) |
| `/sys/class/drm/card*/device/unique_id` | GPU die serial where the ASIC exposes it | — |

VBIOS build date and part number are embedded in the VBIOS version string reported
by `amdgpu_firmware_info`; parse rather than seek a separate file.

### 7.4 Displays

`/sys/class/drm/card*-*/edid` is world-readable and contains the full EDID blob.
Parse it in-app — roughly 60 lines of `struct` unpacking — to yield manufacturer ID,
product code, serial number, manufacture week and year, EDID version, physical size,
gamma, supported and native timings, and the monitor's descriptive name.

Also read `enabled`, `status`, `dpms` and `modes` from the same connector directory.

### 7.5 Motherboard and firmware

| Path | Yields | Privilege |
|---|---|---|
| `/sys/class/dmi/id/{board_vendor,board_name,board_version,bios_vendor,bios_version,bios_date,sys_vendor,product_name,product_family,chassis_type}` | Board and BIOS identity | — |
| `/sys/class/dmi/id/{product_serial,board_serial,product_uuid}` | Serial numbers | **root** |
| `/sys/firmware/efi/` | Presence indicates UEFI boot; `fw_platform_size` gives 32/64-bit firmware | — |
| `/sys/firmware/efi/efivars/SecureBoot-*` | Secure Boot state — read the fifth byte of the variable, not the whole blob | — |

### 7.6 System

| Path | Yields |
|---|---|
| `/etc/os-release` | Distribution name, ID, version, build ID |
| `/proc/version`, `os.uname()` | Kernel release, version, build compiler |
| `/proc/cmdline` | Kernel command line, tokenised one row per parameter |
| `/proc/modules`, `/sys/module/*/` | Loaded modules, sizes, dependency counts, parameters |
| `/proc/uptime`, `/proc/loadavg` | Uptime, load |
| `/sys/kernel/security/lockdown` | Kernel lockdown mode |
| `/sys/class/iommu/` | IOMMU presence and groups |
| Environment | `XDG_SESSION_TYPE`, `XDG_CURRENT_DESKTOP`, `WAYLAND_DISPLAY`, `DESKTOP_SESSION` |

The CachyOS kernel identifies itself in `/proc/version`; surface the flavour rather
than only the version number.

### 7.7 Storage

| Path | Yields | Privilege |
|---|---|---|
| `/sys/class/block/*/` | `size`, `queue/rotational`, `queue/scheduler`, `queue/physical_block_size`, `queue/logical_block_size`, `queue/discard_max_bytes` | — |
| `/sys/class/block/*/device/` | `model`, `vendor`, `serial`, `firmware_rev` | — |
| `/sys/class/nvme/*/` | NVMe controller model, serial, firmware, transport, `numa_node` | — |
| `/proc/mounts`, `/proc/swaps` | Mount points, filesystem types, mount options, swap devices | — |
| `statvfs()` per mount | Capacity, free, available, inode counts | — |
| udisks2 over D-Bus | **SMART attributes, power-on hours, temperature, health assessment, for both ATA and NVMe** | polkit |

### 7.8 Peripherals

| Path | Yields |
|---|---|
| `/sys/bus/usb/devices/*/` | `idVendor`, `idProduct`, `manufacturer`, `product`, `serial`, `speed`, `bMaxPower`, `bcdDevice`, `version`, `bDeviceClass`, `bNumInterfaces` |
| `/sys/bus/pci/devices/*/` | `class`, `vendor`, `device`, `subsystem_vendor`, `subsystem_device`, `revision`, `current_link_speed`, `current_link_width`, `power_state`, `d3cold_allowed`, `numa_node`, IOMMU group via symlink |
| `/sys/class/net/*/` | `address`, `speed`, `duplex`, `operstate`, `carrier`, `mtu`, `type`, driver via `device/driver` symlink |
| `/sys/class/net/*/statistics/` | Byte and packet counters, errors, drops |
| `/proc/asound/cards`, `/sys/class/sound/card*/` | Sound card identity, codec |
| `/sys/class/input/input*/` | Device name, physical path, capabilities bitmaps |
| `/sys/class/power_supply/*/` | `type`, `status`, `capacity`, `charge_full`, `charge_full_design`, `cycle_count`, `voltage_now`, `power_now`, `technology` |
| `/sys/class/hwmon/hwmon*/` | `name`, `temp*_input`, `temp*_label`, `temp*_crit`, `fan*_input`, `in*_input`, `power*_input` |

Battery health is `charge_full / charge_full_design`, expressed as a percentage with
the raw values shown alongside.

### 7.9 Name resolution

`/usr/share/hwdata/pci.ids` and `/usr/share/hwdata/usb.ids` map numeric IDs to
vendor, device and subsystem names, and PCI class codes to human categories. Parse
both once at startup into dictionaries. When a lookup misses, show the raw ID and
say plainly that the local database has no entry — never invent a name.

---

## 8. THE PRIVILEGED HELPER

### 8.1 Shape

The GUI never runs as root. A separate script runs as root for well under a second,
prints JSON to stdout, and exits.

Running the Qt GUI itself under `pkexec` is not an option: root cannot connect to
the user's Wayland compositor socket, and `XDG_RUNTIME_DIR` is not readable across
the uid boundary. It would appear to work on X11 and fail on Wayland.

| Property | Value |
|---|---|
| Installed path | `/usr/lib/koken/koken-helper` |
| Ownership | `root:root`, mode `0755` |
| Invocation | `pkexec /usr/lib/koken/koken-helper` |
| Policy action ID | `com.github.sudo_megas.koken.read-hardware` |
| Policy file | `/usr/share/polkit-1/actions/com.github.sudo_megas.koken.policy` |
| Timeout | 10 seconds, enforced by the caller |
| Output | A single JSON object on stdout, nothing else |

The policy file must set `org.freedesktop.policykit.exec.path` to the absolute
helper path and use `auth_admin_keep` so a second run within the session does not
re-prompt.

### 8.2 What it collects

```json
{
  "version": 1,
  "dmi": {
    "type16": [],
    "type17": [],
    "serials": {}
  },
  "gpu_firmware": {
    "0": { "vbios_version": "", "firmware": {} }
  },
  "errors": []
}
```

Three jobs and no more:

1. `dmidecode -t 16` and `-t 17`, parsed to structured entries.
2. `/sys/class/dmi/id/{product_serial,board_serial,product_uuid}`.
3. `/sys/kernel/debug/dri/*/amdgpu_firmware_info` for every DRM card present.

Anything it cannot read goes into `errors` as a plain string. The helper never
exits non-zero for a partial read — only for a genuine crash.

### 8.3 Behaviour when refused

The prompt fires once, at launch, before the window is shown. If the user cancels
or authentication fails, the app opens normally and every affected row renders its
value as `Requires administrator access — restart KÖKEN to authenticate`. No retry
loop, no nagging, no dialog. The status is also reflected once in a footer
indicator.

---

## 9. REFRESH MODEL

Data is split into two tiers, and the split is fixed at read time, not guessed.

| Tier | Contents | When read |
|---|---|---|
| **Static** | Device enumeration, model names, capacities, serials, cache topology, DMI, VBIOS, EDID, instruction sets | Once at launch, and on `F5` |
| **Volatile** | CPU per-core frequency, governor, hwmon temperatures, fan RPM, GPU clocks and power, VRAM usage, battery charge and status, network link state and counters, memory in use, filesystem free space | On the interval timer |

### Rules

1. **The timer never re-enumerates.** USB devices, PCI devices and disks are not
   re-scanned on the interval. Hardware that appears or disappears is picked up on
   `F5` only.
2. **Refresh mutates existing widgets.** The volatile pass sets text on rows it
   already holds references to, keyed by row ID. It must never rebuild the row list,
   because that would collapse any expanded explanation and lose scroll position.
3. **Interval is user-selectable** via a segmented control in the window footer:
   `Off · 1s · 2s · 5s · 10s`. Default `2s`. Persisted to config.
4. **The timer stops when the window is not visible.** Reading sysfs every second
   for a minimised window is waste.

---

## 10. THE EXPLANATION LAYER

### 10.1 Storage

Explanations live in a data file, never in source, and that file lives in the
user's config directory:

```
$XDG_CONFIG_HOME/koken/explanations.en.toml
```

The shipped corpus travels inside the installed Python package, under
`koken/defaults/`, read with `importlib.resources`. On startup, if the config file
is **absent**, it is copied out once. If it is present, it is used as-is and never
touched again.

That means an upgrade brings new shipped explanations only for someone who has no
file yet. Anyone who has edited theirs keeps it, and gets the newer corpus by
deleting their copy and relaunching. This is stated in the README; it is a
deliberate trade for having exactly one editable file in one predictable place.

Parsed with `tomllib`. A missing or malformed file leaves every row without an
expander rather than crashing.

### 10.2 Schema

Keys are `branch.section.field`, matching the row IDs exactly.

```toml
[hardware.cpu.smt]
short = "SMT enabled"
long = """
Simultaneous multithreading lets each physical core hold two instruction
streams at once, so the second stream can use execution units the first
leaves idle while it waits on memory. It is not a second core: under a
fully saturated load, expect roughly 20-30% more throughput, not double.
Disabling it in firmware can slightly raise per-thread latency consistency,
which is why some competitive gaming guides recommend it, but for mixed
desktop workloads leaving it on is almost always correct.
"""
```

- `short` is optional. When present it becomes the inline gloss appended to the
  value: `16 — SMT enabled`.
- `long` is the expansion body. Plain prose, no Markdown, wrapped by the widget.
- A row with no entry renders normally and shows no expander. This is the mechanism
  that lets v1.0 ship with partial coverage.
- A probe may supply the expansion itself, on the row, instead of having it looked
  up here. When it does, that wins. This is for text no static corpus can hold:
  a list that depends on the machine, or a sentence explaining why a particular
  number is unavailable *on this machine* rather than in general. It is not a
  licence to move explanations into source. If a row's text would read the same on
  every machine in the world, it belongs in this file.

  There is a second reason it exists. Section 10.1's trade means an existing
  user's copy of this file is never refreshed, so an explanation added here for a
  section added later reaches only people installing for the first time. A probe
  that carries its own text reaches everybody. Weigh that when adding a section
  after 1.0.

### 10.3 Coverage target for v1.0

Ship with entries for the rows that get looked at — around 60. Every remaining row
still displays its value correctly and simply has no expander. Coverage grows by
editing one file afterwards, with no release required.

The file must be laid out branch by branch in the same order as section 6, with a
comment header per branch, so filling gaps later means finding the right block
rather than searching.

---

## 11. ROW RENDERING

Content is a `QScrollArea` containing a `QVBoxLayout` of row widgets. Not a
`QTableView`, not a `QTreeView` — the expansion behaviour and per-row control are
worth more than the model/view machinery at this scale, and no view holds more than
about 60 rows.

Each row is a widget with:

- **Label**, left, `--text-secondary` weight, fixed 38% width.
- **Value**, right-aligned, remaining width, elides only at extreme narrowness.
- **Copy glyph**, appearing on hover at the right edge of the value.
- **Expander chevron**, right edge, present only if an explanation exists.
- **Expansion body**, a word-wrapped label below, hidden by default, revealed
  in place by a click anywhere on the row.

### Click targets

Two distinct actions on one row, so they must not overlap:

| Gesture | Result |
|---|---|
| Click anywhere on the row | Toggle the explanation expansion |
| Click the copy glyph | Copy the raw value to the clipboard |

The copy glyph is hidden until the pointer enters the row, so a resting row shows
no controls at all. On copy, the glyph is replaced by a check mark for one second,
then reverts. No toast, no status message, no dialog.

What lands on the clipboard is the **raw value, not the glossed one**. A row reading
`16 — SMT enabled` copies `16`. The gloss is for reading; the value is for pasting
into a form or a search box.

There is no copy-all, no section copy, no export, and no report. One value at a
time is the whole feature.

Alternating row background at 50% subtlety. Hairline separators. Expanding a row
pushes the rows below it down; it does not overlay, float, or open anything.

Values that represent a problem — a PCIe link below its maximum, single-channel
memory on a dual-channel board, a battery below 80% health, a disk SMART assessment
that is not `OK`, a thermal throttle flag — render in the warning colour and always
carry an explanation entry. This is the one place colour is allowed to carry meaning.

---

## 12. ACTIONS — MOUNT AND UNMOUNT

The only place KÖKEN writes to the system. Everything in this section is scoped to
`Storage → Volumes`, and nothing here appears anywhere else in the app.

### 12.1 Where it lives

The first row of every `Storage → Volumes` instance view is the mount state row:

| State | Value shown | Control |
|---|---|---|
| Mounted | The mount point, e.g. `/run/media/user/ARCHIVE` | `Unmount` |
| Not mounted | `Not mounted` | `Mount` |
| No mountable filesystem | `No filesystem` or the detected type | None |

The control is a small button on the right of that row. It is the only interactive
element in the entire application.

### 12.2 The two-step guard

A single click never acts. The first click changes the button label to
`Confirm unmount` (or `Confirm mount`) and starts a five second countdown; a second
click within that window performs the call, and the button reverts if the window
expires or focus moves elsewhere.

This is not a dialog and does not violate the no-popup rule — the widget changes in
place. It exists because an inert browser where one row can unmount a drive
mid-write needs friction that inert rows do not.

Volumes that are system-critical — anything mounted at `/`, `/boot`, `/boot/efi`,
`/usr`, `/var`, or `/home`, or backing an active swap — render their mount state row
at `warning` severity, so the button is visibly a different thing from the one on a
USB stick. The button is still shown; udisks2 is the authority on what is permitted,
not KÖKEN's guesswork.

### 12.3 The calls

Over `QtDBus`, on the volume's `org.freedesktop.UDisks2.Filesystem` interface:

| Action | Method | Options |
|---|---|---|
| Mount | `Mount(a{sv} options)` → returns the mount path | Empty dict |
| Unmount | `Unmount(a{sv} options)` | Empty dict |

**Never pass `force`.** A forced unmount on a filesystem with dirty pages loses
data. If udisks2 says the device is busy, that answer stands and is reported; it is
not something to retry harder.

Both calls are asynchronous. During the call the button reads `Working…` and is
disabled. Never block the UI thread on a D-Bus round trip — mounting a large
filesystem can take seconds.

### 12.4 Results

On success, re-run the storage enumeration only — not the whole static pass — and
rebuild the Volumes branch so the mount point, free space and filesystem rows are
correct. The row 3 selection is preserved.

On failure, the D-Bus error is translated to plain language and shown as an
expansion body under the mount state row, styled as `warning`. It stays until the
next action or a refresh. It does not go in the footer, and it does not open
anything.

| D-Bus error | Shown as |
|---|---|
| `UDisks2.Error.DeviceBusy` | Something is still using this filesystem. Close any program with files open on it, then try again. |
| `UDisks2.Error.NotAuthorized*` | Authentication was declined or failed. |
| `UDisks2.Error.AlreadyMounted` | Already mounted — the view was stale, and has now been refreshed. |
| `UDisks2.Error.NotMounted` | Not mounted — the view was stale, and has now been refreshed. |
| `UDisks2.Error.Failed` | The message text from udisks2, verbatim. |
| Anything else | The D-Bus error name and message, unmodified. |

Never invent a friendlier message for an error not in this table. An unrecognised
failure is shown raw.

### 12.5 Authentication

Mounting or unmounting an internal fixed filesystem requires
`org.freedesktop.udisks2.filesystem-mount-system`, so polkit may prompt a second
time, mid-session, after the launch prompt. This is expected and is the system's
prompt, not KÖKEN's. Removable media usually needs no prompt for an active local
session.

The README must pre-warn about this second prompt so it does not look like a bug.

### 12.6 Explicitly not included

Unlocking or locking LUKS volumes, ejecting, powering off a drive, formatting,
editing `/etc/fstab`, changing mount options, and creating mount points. `Mount` and
`Unmount` with default options. Nothing else.

---

## 13. APPEARANCE

### 13.1 Style

Pin `QApplication.setStyle("Fusion")` at startup. Without it the app inherits
Breeze under KDE and default Fusion under Niri, and looks like two different
applications on the two target machines. Fusion is the predictable base that the
stylesheet is written against.

Everything visual on top of that is a Qt stylesheet generated at runtime from the
active palette. No hand-written colour literals anywhere in the QSS — the sheet is
a template, the palette fills it.

### 13.2 Palettes are data files

Palettes are not compiled in. They are TOML files in one directory:

```
$XDG_CONFIG_HOME/koken/palettes/
```

Seeded the same way as the explanation file. On startup, `catppuccin-latte.toml`
and `catppuccin-mocha.toml` are copied out of the installed package's
`koken/defaults/palettes/` **only if that filename is not already present**. An
existing file of the same name is never overwritten.

Every `.toml` in the directory is loaded. Adding a palette is dropping a file in;
removing one is deleting it. There is nothing else to configure.

Two ship with v1.0:

| File | Variant |
|---|---|
| `catppuccin-latte.toml` | light |
| `catppuccin-mocha.toml` | dark |

Schema — twelve roles, nothing more:

```toml
name = "Catppuccin Latte"
variant = "light"

[colors]
base    = "#eff1f5"
surface = "#e6e9ef"
overlay = "#ccd0da"
border  = "#dce0e8"
text    = "#4c4f69"
subtext = "#6c6f85"
muted   = "#9ca0b0"
accent  = "#1e66f5"
success = "#40a02b"
warning = "#df8e1d"
danger  = "#d20f39"
selection = "#dce0e8"
```

A palette file missing a role falls back to the shipped palette of the same
variant for that role only. A malformed file is ignored with no crash and no
dialog.

### 13.3 Which palette is active

Read `org.freedesktop.appearance color-scheme` from the Settings portal over
`QtDBus`. Light selects the first `variant = "light"` palette, dark the first
`variant = "dark"`, with user-supplied files winning over shipped ones. Subscribe
to the change signal and regenerate the stylesheet live.

If the portal is unavailable, default to dark.

This is deliberately the only theming mechanism. KÖKEN contains no knowledge of
any specific desktop shell, and must not gain any. A user whose shell can write a
palette file — Noctalia's user templates, matugen, anything else — points it at
`$XDG_CONFIG_HOME/koken/palettes/` and it works, with no code here aware that
shell exists.

There is no theme picker in the interface.

### 13.4 Typography

| Element | Font | Size |
|---|---|---|
| Row label | System UI sans, `QFont()` default | 100% |
| Row value | System fixed font, `QFontDatabase.systemFont(QFontDatabase.FixedFont)` | 95% |
| Row 1 tabs | System UI sans, medium weight | 115% |
| Row 2 tabs | System UI sans | 100% |
| Row 3 tabs | System UI sans | 90% |
| Expansion body | System UI sans | 95% |

Values are monospaced without exception, including short ones. Half the content of
this app is hex IDs, serials, MAC addresses and firmware strings, where `1002:747e`
and `10O2:747c` must not look alike, and where a column of values should align. A
mixed sans/mono pairing also gives the value column a visible edge without needing
a separator rule.

No font is bundled. A shipped typeface would be a dependency doing work the system
font already does.

### 13.5 Icons

Tabler Icons, MIT licensed, shipped as a **compiled font subset** — not the full
set, not SVG files.

| Property | Value |
|---|---|
| Source | `@tabler/icons`, MIT |
| Form | TTF subset, roughly 30 glyphs, built with the upstream `compile-options.json` `includeIcons` list |
| Installed path | `koken/assets/tabler-icons-subset.ttf`, package data, loaded with `importlib.resources` |
| Loading | `QFontDatabase.addApplicationFont` at startup, glyphs rendered as text |
| Licence file | `LICENSE-tabler` in the repository root, and named in README section 2 |

A font subset rather than SVG for three reasons: it avoids `QtSvg`, which is another
separate PySide6 package on Debian; glyphs inherit the palette colour as text, so
they recolour for free when the theme switches; and thirty glyphs is a few kilobytes
against a full icon directory.

The usual objection to icon fonts — render-blocking, no multi-colour, awkward for
screen readers — is a web objection and does not apply to a Qt desktop application
drawing monochrome glyphs.

The subset is **not** seeded into the config directory. It is a code asset, not
something the user edits.

#### Where icons appear

Exactly four places. Nowhere else.

| Location | Purpose |
|---|---|
| Row 3 instance tabs | A device-class glyph, so fifteen wrapped USB entries are distinguishable at a glance rather than fifteen truncated strings |
| Warning and danger rows | A single leading glyph on the value, marking the row as a finding rather than a fact |
| The mount and unmount button | State, paired with the text label — never replacing it |
| The per-row copy control | Only while the pointer is inside that row |

**Not on row 1 or row 2.** Four branches and a handful of sections have unambiguous
text labels at 44px and 34px. An icon there is decoration, and decoration in a dense
reference app is noise.

**Not on content rows.** A glyph per property row would put hundreds of icons on
screen across the app and inform nothing.

#### Glyph set

Map by concept. Every Tabler name below must be **verified to exist** against the
upstream icon list before it goes in `includeIcons` — a name that does not exist
silently produces a missing-glyph box.

| Concept | Candidate name |
|---|---|
| USB storage | `device-usb`, else `usb` |
| USB keyboard | `keyboard` |
| USB pointer | `mouse` |
| USB audio | `headphones` |
| USB video | `camera` |
| USB hub | `hierarchy` |
| Bluetooth | `bluetooth` |
| USB unknown class | `question-mark` |
| PCI generic | `chip` |
| Graphics card | `device-desktop-analytics`, else `chip` |
| Network controller | `network` |
| Storage controller | `server` |
| Audio controller | `volume` |
| Disk, rotational | `disc` |
| Disk, solid state | `device-sd-card` |
| Volume, mounted | `folder` |
| Volume, unmounted | `folder-off` |
| Ethernet interface | `plug` |
| Wireless interface | `wifi` |
| Loopback or virtual interface | `arrow-loop-left` |
| Display | `device-desktop` |
| Temperature sensor | `temperature` |
| Fan | `wind` |
| Battery | `battery` |
| Mains supply | `power` |
| Copy value | `copy` |
| Copy confirmed | `check` |
| Warning severity | `alert-triangle` |
| Danger severity | `alert-octagon` |
| Mount action | `plug-connected` |
| Unmount action | `plug-connected-x`, else `plug-off` |

If a concept has no clean Tabler match, use no icon for it rather than a vague
approximation. A wrong icon is worse than none.

### 13.6 Metrics

| Property | Value |
|---|---|
| Content row height | 32px |
| Expansion body padding | 12px top, 16px left, matching the value column |
| Label column width | 38% of content width, fixed |
| Row separator | 1px, `border` role |
| Alternating row background | `surface` role, on odd rows |
| Row 1 tab height | 44px, equal width, `accent` fill when selected |
| Row 2 tab height | 34px, equal width, `accent` fill when selected |
| Row 3 tab height | 26px, flow layout, 2px `accent` underline when selected |
| Gap between tab rows | 12px |
| Window default size | 1100x760 |
| Window minimum size | 720x520 |

`warning` and `danger` roles colour the value text only, never the row background.
This is the one place colour carries meaning, and it must stay legible against both
the base and the alternating surface.

---

## 13.7 Application icon and banner

Both assets are **supplied** and present in the repository from the first commit.
Neither is the agent's to invent, redraw, recolour, crop, re-render or regenerate.
If either is missing, ship without it and say so in the handover.

| File | What it is | Used for |
|---|---|---|
| `data/icons/koken.png` | 2048x2048 RGBA, rounded corners already applied, transparent outside the corner radius | The application icon |
| `data/banner.png` | 1280x320 RGB, dark, contains the wordmark and both subtitles | The README banner, first element in the file |

#### Icon handling

The source is a single 2048px PNG. Scale it down to the standard hicolor sizes —
512, 256, 128, 64, 48, 32 — with a high-quality filter, and install each to
`/usr/share/icons/hicolor/<size>x<size>/apps/koken.png`. The `.desktop` entry
carries `Icon=koken`, with no path and no extension.

The corner rounding is **already baked into the artwork**. Do not apply a mask, a
frame, a shadow, or any further shaping. Some icon themes will apply their own
shaping on top; that is the theme's business, not something to compensate for.

#### Banner handling

`data/banner.png` goes at the very top of the README, above the title, as its own
line. It is not resized, not converted, not regenerated at a different aspect ratio,
and no text is added over it — the wordmark and subtitles are already in the image.

---

## 14. ABOUT

Not a dialog. A fifth row 3 entry under `System → Operating system`, or a footer
control that switches the content area — never a popup.

Contents: maker, version, release date, source address, the SPDX identifier
`GPL-3.0-or-later`, and the full GPL-3 licence text.
Addresses are selectable text and are not clickable. No update check.

---

## 15. CONFIGURATION

Everything KÖKEN reads that is not hardware lives in one directory:

```
$XDG_CONFIG_HOME/koken/
├── settings.toml
├── explanations.en.toml
└── palettes/
    ├── catppuccin-latte.toml
    └── catppuccin-mocha.toml
```

`$XDG_CONFIG_HOME` defaults to `~/.config` and must be honoured when set rather
than assumed. Nothing is installed to `/usr/share/koken` or `/etc/xdg/koken` — those
paths do not exist for this application.

### Seeding

The defaults ship inside the installed package at `koken/defaults/` and are read
with `importlib.resources`. On every startup:

1. Create `$XDG_CONFIG_HOME/koken/` and `palettes/` if absent.
2. For each default file, copy it out **only if no file of that name exists**.
3. Never overwrite, never merge, never delete.

A file the user has deleted on purpose comes back on the next launch. This is
accepted: the alternative is tracking deletions in a state file, which is more
machinery than the problem deserves.

### Settings

```toml
refresh_interval = 2
last_branch = "hardware"
```

Two keys, written on clean exit. A missing or corrupt file falls back to defaults
silently. Nothing else is persisted — no window geometry, no cached hardware data,
no history.

### What the app writes

`settings.toml` on exit, and the seed copies on first run. That is the complete
list. It never edits an explanation or palette file after seeding it, and never
writes anywhere outside this directory.

---

## 16. PACKAGING

Two artifacts, both produced by CI on tag.

### Arch

`packaging/PKGBUILD`. Not the repository root — `makepkg` runs from `packaging/`
and treats `$startdir/..` as the project root.

```
depends=('python' 'python-pyside6' 'hwdata' 'udisks2' 'dmidecode' 'polkit')
```

Built with `python-build` and `python-installer`. Ships the helper to
`/usr/lib/koken/`, the policy file to `/usr/share/polkit-1/actions/`, and a
`.desktop` entry to `/usr/share/applications/`. The explanation corpus and the
palettes are package data inside the Python module and are not installed
separately.

### Debian

`debian/` directory at the repository root — `dpkg-buildpackage` requires it there
and offers no way to relocate it, so it does not join `PKGBUILD` under
`packaging/`. Built into a `.deb`.

```
Depends: python3 (>= 3.11), python3-pyside6.qtcore, python3-pyside6.qtgui,
         python3-pyside6.qtwidgets, python3-pyside6.qtdbus,
         hwdata, udisks2, dmidecode, policykit-1
```

Debian splits PySide6 into per-module binary packages, so four are named rather
than one. This is expected, not a mistake.

---

## 16.5 LICENCE NOTICES

`LICENSE` holds the verbatim GPL-3 text. The *or-later* choice is expressed in the
per-file header notice, not in that file, so every source file — Python modules, the
helper, the PKGBUILD, the workflow — carries:

```text
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
```

The SPDX identifier is `GPL-3.0-or-later` everywhere it appears: `pyproject.toml`,
`PKGBUILD`, `debian/copyright` and the About section. Bare `GPL-3.0` is a
deprecated identifier and must not be used.

---

## 17. NON-GOALS

Stated so they are never accidentally built:

- No benchmarking of any kind.
- No report generation, export, or HTML output. Copying a single value to the
  clipboard (§11) is the entire extent of getting data out of the app.
- No editing, tuning, overclocking, or changing any system value, with the single
  exception of mounting and unmounting filesystems (§12).
- No monitoring history, graphs, or logging over time.
- No remote or SSH inspection of other machines.
- No Windows, no macOS, ever.
- No plugin system.
