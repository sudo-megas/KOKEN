# KÖKEN — STEP

The single living build document. Rewrite this file as work proceeds — it is the
only document that changes. `CORE.md` is fixed; if this file ever contradicts it,
`CORE.md` wins.

Build continuously. Do not stop between phases for approval. Stop only at the
handover point in §8.

---

## 1. ENVIRONMENT

Claude Code Web, Arch container.

```bash
pacman -Syu --noconfirm python python-pyside6 hwdata udisks2 dmidecode polkit git base-devel
```

The container has no real hardware to speak of. Sysfs paths that exist on the
target machine will be missing or stubbed here. This is the single largest risk in
the build — see §7.

Target machines:

- **Desktop** — Ryzen 7 9800X3D, RX 7800 XT, 32 GB DDR5-6000, Arch + CachyOS
  kernels, Niri/Noctalia on Wayland, 2560x1440 QD-OLED.
- **Laptop** — Ryzen 5 3450U, Vega 8, 8 GB DDR4, Arch + KDE.

Write for both. The laptop is the only machine with a battery, so
`Peripherals → Power → Battery` cannot be tested on the desktop and must be written
defensively.

---

## 2. REPOSITORY LAYOUT

```
koken/
├── CORE.md
├── STEP.md
├── LICENSE
├── LICENSE-tabler
├── README.md
├── pyproject.toml
├── .github/
│   └── workflows/
│       └── release.yml
├── packaging/
│   └── PKGBUILD
├── debian/
│   ├── control
│   ├── rules
│   ├── changelog
│   ├── compat
│   └── install
├── data/
│   ├── com.github.sudo_megas.koken.policy
│   ├── koken.desktop
│   ├── banner.png          (supplied — do not modify)
│   └── icons/
│       └── koken.png       (supplied — do not modify)
├── helper/
│   └── koken-helper
└── src/
    └── koken/
        ├── __init__.py
        ├── __main__.py
        ├── app.py
        ├── config.py
        ├── theme.py
        ├── privileged.py
        ├── flowlayout.py
        ├── explain.py
        ├── actions.py
        ├── icons.py
        ├── assets/
        │   └── tabler-icons-subset.ttf
        ├── defaults/
        │   ├── explanations.en.toml
        │   └── palettes/
        │       ├── catppuccin-latte.toml
        │       └── catppuccin-mocha.toml
        ├── ui/
        │   ├── window.py
        │   ├── tabrow.py
        │   ├── row.py
        │   ├── mountrow.py
        │   └── footer.py
        └── probes/
            ├── __init__.py
            ├── base.py
            ├── hwids.py
            ├── edid.py
            ├── cpu.py
            ├── memory.py
            ├── graphics.py
            ├── displays.py
            ├── motherboard.py
            ├── system.py
            ├── kernel.py
            ├── desktop.py
            ├── security.py
            ├── disks.py
            ├── volumes.py
            ├── filesystems.py
            ├── usb.py
            ├── pci.py
            ├── network.py
            ├── audio.py
            ├── input.py
            ├── power.py
            └── sensors.py
```

---

## 3. BUILD ORDER

Work top to bottom. Commit after each numbered item. Do not push.

### 3.1 Foundation

1. `git init -b main` — the branch **must** be `main`, not `master`, because the
   remote will be an empty GitHub repository expecting `main`. Then `.gitignore`,
   `LICENSE` (verbatim GPL-3 text; the *or-later* choice lives in each
   file's header notice, per `CORE.md` §16.5), `pyproject.toml`
   with `hatchling` and a `[project.scripts]` entry point `koken = "koken.__main__:main"`.

   `data/icons/koken.png` and `data/banner.png` are supplied and already in
   place. Commit them untouched in the first commit. Do not modify, re-render,
   re-mask or replace either — see `CORE.md` §13.7.

   The repository is named `KOKEN` in uppercase; the package, binary, module and
   every installed path are lowercase `koken`. Do not let the repository name leak
   into any of them.

2. `probes/base.py` — the probe contract. A probe returns an ordered list of
   `Row(id, label, value, tier, severity)` where `tier` is `static` or `volatile`
   and `severity` is `normal` or `warning`. Plus safe readers:
   `read_text(path)`, `read_int(path)`, `read_first_line(path)`, `glob_dirs(pattern)`.
   Every reader returns `None` on `FileNotFoundError`, `PermissionError` or
   `OSError` rather than raising. **Nothing in this app may crash because a sysfs
   file was absent.**
3. `probes/hwids.py` — parse `pci.ids` and `usb.ids` into lookup dicts. Handle the
   vendor / device / subsystem three-level indentation and the PCI device-class
   section at the end of `pci.ids`.
4. `config.py` — the whole of `CORE.md` §15. Resolve `$XDG_CONFIG_HOME` properly
   rather than assuming `~/.config`. Create the directory tree, seed the defaults
   out of `koken/defaults/` with `importlib.resources` copying only absent files,
   and read and write the two-key `settings.toml`. Nothing else in the codebase
   builds a config path by hand.

### 3.2 Privileged path

5. `helper/koken-helper` — standalone Python script, no imports from `koken`.
   Collects the three items in `CORE.md` §8.2, prints one JSON object, exits 0.
   Must be runnable directly as root for testing: `sudo ./helper/koken-helper`.
6. `data/com.github.sudo_megas.koken.policy` — polkit action, `auth_admin_keep`,
   `org.freedesktop.policykit.exec.path` pointing at `/usr/lib/koken/koken-helper`.
7. `privileged.py` — run `pkexec` via `QProcess` with a 10 second timeout, parse
   the JSON, expose it as a dict. On cancellation, non-zero exit, timeout or
   unparseable output, return an empty result and set a `refused` flag. Never
   raise, never retry, never prompt twice.

### 3.3 Probes

8. `probes/cpu.py` — all six row 3 sections. The cache tree and the CCX topology
   derivation are the fiddly parts; take the time.
9. `probes/memory.py` — `/proc/meminfo` for the overview, helper DMI data for
   modules. Derive channel configuration from bank locators.
10. `probes/graphics.py` — per-card instance rows, including the PCIe
    current-versus-max link comparison, which is a `warning` severity when current
    is below max. VBIOS fields come from the helper.
11. `probes/edid.py` then `probes/displays.py` — EDID blob parsing (header check,
    manufacturer ID from the packed five-bit letters, product code, serial,
    manufacture week and year, EDID version, basic display parameters, detailed
    timing descriptors, monitor name and serial strings from descriptor blocks).
    Validate the 8-byte header and the checksum before trusting any of it.
12. `probes/motherboard.py`, `system.py`, `kernel.py`, `desktop.py`, `security.py`.
13. `probes/disks.py`, `volumes.py`, `filesystems.py`. udisks2 SMART access goes in
    `disks.py` via `QtDBus` — enumerate `org.freedesktop.UDisks2` objects, match
    drives to block devices, call `SmartGetAttributes` for ATA and the NVMe
    equivalent. Treat a D-Bus failure exactly like a missing file.

    `volumes.py` also builds the mount state row defined in `CORE.md` §12.1 and
    must expose the volume's D-Bus object path so the action layer can call
    `Filesystem.Mount` and `Filesystem.Unmount` on it.
14. `probes/usb.py`, `pci.py`, `network.py`, `audio.py`, `input.py`, `power.py`,
    `sensors.py`.

### 3.4 Interface

15. `icons.py` and `assets/tabler-icons-subset.ttf` — implement `CORE.md` §13.5.
    Verify every candidate glyph name against the upstream Tabler icon list first,
    drop any that do not exist, then compile the subset via the upstream
    `compile-options.json` `includeIcons` array. Load with
    `QFontDatabase.addApplicationFont` and expose a single
    `glyph(concept) -> str` lookup so no call site handles code points directly.
    Add `LICENSE-tabler` to the repository root.
16. `flowlayout.py` — the standard Qt flow layout. Row 3 needs it.
17. `ui/tabrow.py` — one reusable widget, three visual weights, driven by a
    `level` parameter. Row 3 uses the flow layout; rows 1 and 2 use equal-width
    stretch.
18. `ui/row.py` — the content row per `CORE.md` §11: label, value, hover copy
    glyph, chevron, hidden expansion body. Click the row toggles the expansion;
    click the copy glyph copies the **raw** value, not the glossed one, and swaps
    to a check mark for one second. The two click targets must not overlap.
    Exposes `set_value(text, severity)` so the volatile pass can update text
    without rebuilding.
19. `ui/window.py` — the cascade. Selection state per branch, held in a dict.
    Row 3 always has at least one entry. Rebuild rows 2 and 3 on selection change;
    never rebuild on refresh.
20. `ui/footer.py` — refresh interval segmented control, privileged-access
    indicator, last-refresh time.
21. `theme.py` and `koken/defaults/palettes/` — implement `CORE.md` §13 in full.
    Load every palette TOML from `$XDG_CONFIG_HOME/koken/palettes/`, resolve light or dark from the Settings portal over
    `QtDBus`, generate the stylesheet from a template, apply it, and regenerate live
    on the portal's change signal. Ship `catppuccin-latte.toml` and
    `catppuccin-mocha.toml`.

    No colour literal may appear anywhere outside a palette file. Pin
    `setStyle("Fusion")` before the first widget is constructed.
22. `explain.py` — load the TOML, attach `short` and `long` to rows by ID.
23. `actions.py` and `ui/mountrow.py` — the mount and unmount feature, exactly as
    specified in `CORE.md` §12 and no further. Asynchronous `QtDBus` calls, the
    five second two-step confirmation, the error translation table, and the
    storage-only re-enumeration on success. Never pass `force`. This is the only
    code in the project that writes to the system; keep it in these two files so
    it stays auditable.
24. `app.py` and `__main__.py` — wire it together. Order at startup: run the
    privileged helper, then build static data, then show the window, then start
    the timer.

### 3.5 Content

25. `src/koken/defaults/explanations.en.toml` — write roughly 60 entries, ordered by branch to
    match `CORE.md` §6. Prioritise, in this order: the PCIe link width rows, memory
    channel configuration, SMT, CPU governor, cache levels, SMART health and
    power-on hours, battery health, CPU vulnerability statuses, Secure Boot,
    IOMMU, kernel lockdown, and the EDID manufacture-date fields.

    These are the rows people actually stop and look at, and each one is a place
    where the raw value is genuinely opaque.

26. `README.md` — written for **users, not developers**. This is a family
    convention and it matters more than the structure below.

    The reader is someone who wants to know what the program does and how to get
    it running. They are not reading your code and are not going to. Plain second
    person, short sentences, no jargon that is not immediately explained, no
    architecture talk, no "leverages" or "seamlessly". If a sentence would only
    make sense to someone who writes software, rewrite it or cut it.

    Where something is genuinely technical and unavoidable — the polkit prompt,
    the two PySide6 package names on Debian — explain the *consequence* to the
    person, not the mechanism. "You will be asked for your password once when the
    program starts, so it can read your memory module details" beats any accurate
    description of `pkexec`.

    Pre-warn every gotcha so it does not look like a fault. State plainly when
    something is unavailable rather than giving a command that would fail.

    Structure: banner, title, badges (version, release
    date, licence, per-platform package size with distro logos), subtitle
    "Machine Corpus" (English only — no Turkish anywhere in this repository),
    then numbered ALL-CAPS sections —
    1. DESCRIPTION, 2. DEPENDENCIES, 3. INSTALLATION (3.A source, 3.B distro,
    3.C other), 4. HOW TO USE? WHAT IS THE APPLICATION SECTIONS? as a two-column
    table with one row per branch, "What it does with your data", 5. LICENCE
    SUMMARY. Close with the copyright line and *Built with Reason and Passion.*

    Section 2 is the ideological centre. Argue each of the six dependencies: what it
    does, why nothing lighter would do, and why the list stops there. Name the ones
    deliberately avoided — `python-dbus`, `pciutils`, `usbutils`, `smartmontools` —
    and say what replaced them.

    Pre-warn the gotchas so they do not look like failures: the password prompt at
    launch and what happens if you decline; VBIOS data being amdgpu-only; DIMM
    detail being unavailable without authentication; Debian naming four PySide6
    packages; and the **second** polkit prompt that appears when mounting or
    unmounting an internal filesystem.

    Section 2 must also name the bundled Tabler subset and its MIT licence,
    since it is the one asset shipped inside the package rather than depended on.

    The "What it does with your data" section must state plainly that KÖKEN reads
    and never writes, with one named exception: it can mount and unmount
    filesystems through udisks2, on explicit two-step confirmation, and it never
    forces an unmount.

---

## 4. LOCAL COMMITS AND TAGS

Commit after every numbered item in §3. Present tense, imperative, lowercase
subject, no scope prefixes, no emoji.

```bash
git add -A
git commit -m "add cpu cache topology probe"
```

**No AI attribution.** No `Co-Authored-By`, no `Generated with`, nothing in the
body. Configure this before the first commit and do not deviate.

When §3 is complete and the app runs, tag locally:

```bash
git tag -a v1.0 -m "KÖKEN v1.0"
```

Do not push. Do not create a remote. The tag travels in the archive.

---

## 5. VERIFICATION BEFORE HANDOVER

Run every check. Report the result of each in the handover summary.

```bash
python -m koken
```

1. Window opens. No dialog appears other than the polkit prompt.
2. All four row 1 branches render. Every row 2 entry renders. Every row 3 entry
   renders, and none is empty.
3. Declining the polkit prompt leaves the app running with the affected rows
   showing the "requires administrator access" text.
4. Expanding a row pushes content down; nothing floats or overlays.
5. An expanded row stays expanded across at least three refresh ticks.
6. Scroll position survives a refresh tick.
7. Changing the interval to `Off` stops updates; changing to `1s` resumes them.
8. `F5` re-enumerates. `Ctrl+1..4`, `Tab`, `←`, `→`, `Ctrl+Q` all behave.
9. Row 3 wraps to a second line when narrowed, with no horizontal scrollbar.
10. Resize to 800x600 and to full screen: no clipping, no overlap.
11. Toggle the system colour scheme: the palette switches live without a restart.
    Editing a palette file in `$XDG_CONFIG_HOME/koken/palettes/` and relaunching
    shows the edit. A malformed palette file is ignored rather than crashing.
12. First run against an empty `HOME` creates `~/.config/koken/`, `palettes/`,
    `explanations.en.toml`, `catppuccin-latte.toml` and `catppuccin-mocha.toml`.
13. Second run does **not** overwrite them: edit a value in
    `catppuccin-mocha.toml`, relaunch, confirm the edit survived.
14. The application appears in the launcher with its icon, at every size, and
    `data/icons/koken.png` is byte-identical to the supplied file.
15. Hovering a content row reveals a copy glyph; clicking it puts the raw value on
    the clipboard and shows a check mark for one second. Clicking elsewhere on the
    row expands instead of copying. A row reading `16 — SMT enabled` copies `16`.
16. Every row 3 instance tab shows a device-class glyph, and no glyph renders as a
    missing-character box. Row 1 and row 2 tabs have no icons at all.
17. `Storage → Volumes` shows a mount state row on every volume, with the mount
    point when mounted and `Not mounted` when not.
18. A single click on `Unmount` does **not** unmount — it arms, and reverts after
    five seconds if left alone.
19. Unmounting a busy filesystem produces the plain-language busy message inline
    under the row, and the filesystem stays mounted.
20. A successful mount or unmount updates the mount point, free space and
    filesystem rows without changing the selected row 3 tab.
21. Volumes on `/`, `/boot` and `/home` render their mount state row at warning
    severity.
22. `grep -rn "force" src/koken/actions.py` returns nothing that passes `force` to
    a D-Bus call.
23. `grep -rnE "#[0-9a-fA-F]{6}" src/koken/*.py src/koken/ui src/koken/probes` returns nothing. Every colour lives in a
    palette file.

```bash
grep -rn "requests\|urllib\|http\|socket\|urlopen" src/ helper/
```

24. Returns nothing. No network code exists.

```bash
python -c "import ast,pathlib; [ast.parse(p.read_text()) for p in pathlib.Path('src').rglob('*.py')]"
```

25. Everything parses.

26. Corrupt `explanations.en.toml` with invalid TOML: the app starts and shows no
    expanders rather than crashing.
27. Delete `~/.config/koken/explanations.en.toml` and `palettes/`, run twice:
    the first run re-seeds them, the second leaves them alone.

---

## 6. RELEASE INFRASTRUCTURE

Written during the build, not after. Nothing here runs in the container — it runs
on GitHub after the tag is pushed.

### 6.1 `packaging/PKGBUILD`

Lives in `packaging/`, not the repository root. `makepkg` sets `$startdir` to the
directory containing the `PKGBUILD`, so the project root is `$startdir/..`. Leave
`source=()` empty and build from the checkout directly rather than fetching a
tarball — this is a repository-local PKGBUILD, not an AUR one.

`debian/` stays at the repository root. `dpkg-buildpackage` requires it there and
provides no way to relocate it. The asymmetry is deliberate.

```
pkgname=koken
pkgver=1.0
pkgrel=1
arch=('any')
license=('GPL-3.0-or-later')
depends=('python' 'python-pyside6' 'hwdata' 'udisks2' 'dmidecode' 'polkit')
makedepends=('python-build' 'python-installer' 'python-hatchling' 'python-wheel')
source=()
```

Install the helper `0755 root:root` to `/usr/lib/koken/`, the policy file to
`/usr/share/polkit-1/actions/`, the desktop entry to `/usr/share/applications/`,
and the licence to `/usr/share/licenses/koken/`. Scale `data/icons/koken.png` to
512, 256, 128, 64, 48 and 32 and install each into
`/usr/share/icons/hicolor/<size>x<size>/apps/koken.png`.

The explanation corpus and the palettes are **not** installed to a system path.
They are package data inside the Python module, declared in `pyproject.toml` so
`hatchling` includes `koken/defaults/**` in the wheel, and the app seeds them into
the user's config directory on first run.

### 6.2 `debian/`

`control` names the four PySide6 module packages separately. `rules` uses
`dh $@ --with python3 --buildsystem=pybuild`. `compat` is 13. `install` places the
helper, policy, explanations and desktop entry at the same paths as the Arch
package, so both distributions behave identically.

### 6.3 `.github/workflows/release.yml`

Triggered on tag push matching `v*`. Three jobs.

**Job `arch`** — runs in an `archlinux:base-devel` container. `makepkg` refuses to
run as root, so create a build user, `chown` the workspace, and run `makepkg -s`
under it **from inside `packaging/`**, since that is where the `PKGBUILD` lives.
The built package lands in `packaging/`; upload `koken-1.0-1-any.pkg.tar.zst`
from there.

**Job `debian`** — runs in a `debian:trixie` container. Install `devscripts`,
`debhelper`, `dh-python`, `python3-all`. Build with `dpkg-buildpackage -us -uc -b`.
Upload `koken_1.0-1_all.deb`.

**Job `release`** — needs both. Downloads the artifacts and publishes them to a
GitHub release for the tag using `softprops/action-gh-release`, with
`generate_release_notes: false` and a body written from the tag message.

Pin container images by tag, not by digest, so the workflow keeps working when the
images move.

---

## 7. KNOWN RISKS

Address each one in code; do not discover them at release time.

1. **The container has no hardware.** Every probe must be written so that a
   completely absent sysfs subtree produces a section with rows explaining that
   nothing was found, rather than an empty view or an exception. Test each probe by
   pointing the path root at an empty directory.
2. **`pkexec` cannot be exercised in the container.** The helper must be testable
   standalone as root, and `privileged.py` must be testable by feeding it a
   pre-captured JSON file instead of a subprocess. Build that seam in from the
   start.
3. **udisks2 will not be running in the container.** The D-Bus call must fail into
   the same "unavailable" path as a missing file, silently and without a stack
   trace.
4. **amdgpu-specific paths.** `mem_info_vram_total`, `pp_dpm_sclk` and
   `amdgpu_firmware_info` do not exist on Intel or NVIDIA. Detect the driver first
   and render an explanatory row rather than a blank one.
5. **`/sys/class/net/*/speed` raises `EINVAL`** on interfaces that are down, and on
   wireless. Reading it must not produce a traceback.
6. **EDID blobs can be zero-length or corrupt.** Validate header and checksum
   before parsing; a bad blob yields a row saying so.
7. **Qt on Wayland under Niri.** The window must not rely on setting its own
   position or size hints that Wayland ignores. Set a sensible default size and let
   the compositor place it.
8. **The mount feature cannot be tested in the container.** udisks2 will not be
   running, so `actions.py` must be written against the D-Bus interface as
   documented and structured so the call layer can be exercised with a stub. Verify
   checks 12 to 17 on the real machine, not in the container, and say so in the
   handover summary rather than claiming they passed.
9. **udisks2 object paths are not stable across replug.** Resolve the volume's
   object path at the moment the button is pressed, not at enumeration time, or a
   stale path will act on the wrong device after a hotplug.

---

## 8. HANDOVER

When §3 is complete, §5 fully passes, and the local `v1.0` tag exists — stop.

Produce `koken-v1.0-source.zip` containing the entire working tree **including the
`.git` directory** so that commit history and the local tag survive the transfer.

```bash
cd /home/claude
zip -r koken-v1.0-source.zip koken/ -x '*/__pycache__/*' '*.pyc'
```

Confirm the archive contains `.git`:

```bash
unzip -l koken-v1.0-source.zip | grep -c "koken/.git/"
```

Then hand over with a summary stating: every §5 check and its result, the commit
count, the tag name, the explanation entry count, and anything in §7 that turned
out differently than expected.

### What happens next, on the user's machine

Not automated. The user runs these.

The GitHub repository is created **empty** — no README, no `.gitignore`, no
licence, no template of any kind. An initialised repository would carry a commit
that shares no history with the archive, and the push would be rejected as
unrelated histories. Nothing in the archive needs merging with anything.

```bash
unzip koken-v1.0-source.zip
cd koken
git remote add origin git@github.com:sudo-megas/KOKEN.git
git push -u origin main
git push origin v1.0
```

Confirm the local branch is actually `main` before pushing:

```bash
git branch --show-current
```

If it says `master`, rename it rather than pushing a second branch:

```bash
git branch -m master main
```

The tag push triggers `release.yml`, which builds both packages and publishes the
release. If the workflow fails, the fix is a commit to `main` followed by deleting
and re-pushing the tag:

```bash
git tag -d v1.0
git push origin :refs/tags/v1.0
git tag -a v1.0 -m "KÖKEN v1.0"
git push origin v1.0
```

---

## 9. LOG

Rewrite this section as work proceeds. Keep it short — what is done, what changed
from the plan, and what is next.

**Done.** All of §3, items 1 to 26. 22 probe modules, the three tab rows, the
content row, the theme, the explanation corpus (72 entries), both packages and
the release workflow. §5 checks 1 to 27 all pass; the five mount checks were run
against the stub call layer §7.8 asks for, not against a live udisks2.

**Changed from the plan.**

- `src/koken/ui/__init__.py` was added. §2 lists it for `probes/` but not for
  `ui/`; without it `koken.ui` is a namespace package, which is fragile in a
  wheel.
- §6.2 says `debian/install` places the explanations; §6.1 and `CORE.md` §16 say
  the corpus is package data and is not installed to a system path. CORE wins,
  so it is not installed separately.
- `Row` gained two fields. `body` lets the About section carry the GPL text as
  its own expansion rather than putting 35 kB of licence into a user-editable
  TOML. `gloss` lets a probe supply an inline gloss that depends on the machine,
  which is what makes a row read `16 — SMT enabled` while still copying `16`; a
  static `short` in the corpus could only ever say one of the two.
- `chip` does not exist in Tabler 3.46.0. `CORE.md` §13.5 names it for the
  generic PCI concept with no fallback, and also says an unmatched concept gets
  no icon rather than an approximation, so generic PCI tabs carry none. Every
  other candidate name verified and is in the subset: 30 glyphs, 14 kB.
- `license-files` was removed from `pyproject.toml`. The PEP 639 list form is
  rejected by the hatchling in Debian trixie, which is what the `.deb` is built
  with. Hatchling finds both licence files on its own; the built wheel carries
  them.
- Package sizes for the README badges were measured by building the `.deb`
  here (445 KiB) and compressing the same payload the Arch way (480 KiB).

**Next.** Nothing in §3. The remaining work is §8: the user creates the empty
GitHub repository, pushes `main` and the `v1.0` tag, and the workflow builds and
publishes both packages.
