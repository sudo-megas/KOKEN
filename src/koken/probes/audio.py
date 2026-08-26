# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Sound cards, one row 3 instance each.

ALSA describes its cards in two places that do not quite agree: the sysfs class
directory has the device relationships, and ``/proc/asound`` has the long names
and the codec detail. Both are read, and the codec is the part worth having -
"Realtek ALC4080" is the answer to why a headphone jack behaves the way it does,
and the PCI id alone will never tell you that.

Nothing here touches the mixer, the volume, or any playback state. This is a
description of the hardware, not a control panel for it.
"""

from __future__ import annotations

import re

from .base import (
    NOT_AVAILABLE,
    NOT_REPORTED,
    Probe,
    Section,
    fmt_list,
    glob_paths,
    list_dir,
    or_missing,
    path_exists,
    read_first_line,
    read_lines,
    read_text,
)

SOUND_ROOT = "/sys/class/sound"
PROC_ASOUND = "/proc/asound"

# " 0 [Generic        ]: HDA-Intel - HD-Audio Generic"
_CARD_LINE = re.compile(r"^\s*(?P<index>\d+)\s*\[(?P<id>[^\]]*)\]:\s*(?P<driver>\S+)\s*-\s*(?P<name>.*)$")


def parse_asound_cards(lines) -> dict[int, dict]:
    """``/proc/asound/cards`` into ``{index: {id, driver, name, detail}}``.

    Each card takes two lines: an indexed header and an indented long name.
    """
    cards: dict[int, dict] = {}
    last: int | None = None
    for line in lines:
        match = _CARD_LINE.match(line)
        if match:
            index = int(match.group("index"))
            cards[index] = {
                "id": match.group("id").strip(),
                "driver": match.group("driver").strip(),
                "name": match.group("name").strip(),
                "detail": "",
            }
            last = index
            continue
        if last is not None and line.strip():
            cards[last]["detail"] = line.strip()
            last = None
    return cards


class AudioProbe(Probe):
    branch = "peripherals"
    id = "audio"
    label = "Audio"

    def _find_cards(self) -> list[dict]:
        cards = []
        described = parse_asound_cards(read_lines(f"{PROC_ASOUND}/cards"))
        for path in list_dir(SOUND_ROOT):
            if not path.name.startswith("card"):
                continue
            suffix = path.name[4:]
            if not suffix.isdigit():
                continue
            index = int(suffix)
            cards.append(
                {
                    "index": index,
                    "name": path.name,
                    "path": path,
                    "id": read_first_line(path / "id"),
                    "described": described.get(index, {}),
                }
            )
        return cards

    def sections(self) -> list[Section]:
        cards = self._find_cards()
        if not cards:
            return [
                self.empty_section(
                    "overview",
                    "Overview",
                    "No sound cards were found. No ALSA driver is loaded on this "
                    "machine, or the kernel was built without sound support.",
                )
            ]
        return [self._card_section(card) for card in cards]

    def _card_section(self, card) -> Section:
        described = card["described"]
        section = Section(
            id=card["name"],
            label=self._label(card),
            icon="audio",
        )

        section.add(
            self.row(
                "name",
                "Card",
                or_missing(described.get("name") or card["id"], NOT_REPORTED),
            )
        )
        section.add(
            self.row("id", "ALSA identifier", or_missing(card["id"], NOT_REPORTED))
        )
        section.add(self.row("index", "Card number", str(card["index"])))
        section.add(
            self.row("driver", "Driver", or_missing(described.get("driver"), NOT_REPORTED))
        )
        if described.get("detail"):
            section.add(self.row("detail", "Reported as", described["detail"]))

        for row in self._codec_rows(card):
            section.add(row)
        for row in self._device_rows(card):
            section.add(row)
        return section

    def _label(self, card) -> str:
        name = card["described"].get("name") or card["id"] or card["name"]
        return name if len(name) <= 24 else name[:23] + "…"

    def _codec_rows(self, card) -> list:
        rows = []
        codecs = glob_paths(f"{PROC_ASOUND}/card{card['index']}/codec#*")
        for codec in codecs:
            text = read_text(codec)
            if not text:
                continue
            fields = {}
            for line in text.splitlines()[:12]:
                if ":" in line and not line.startswith(" "):
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip()
            name = fields.get("Codec")
            if name:
                rows.append(
                    self.row(
                        "codec",
                        f"Codec {codec.name.split('#')[-1]}",
                        name,
                        key=f"codec{codec.name}",
                    )
                )
            for key, field, label in (
                ("Vendor Id", "codec_vendor_id", "  Vendor id"),
                ("Subsystem Id", "codec_subsystem_id", "  Subsystem id"),
                ("Revision Id", "codec_revision", "  Revision"),
            ):
                if fields.get(key):
                    rows.append(
                        self.row(
                            field,
                            label,
                            fields[key],
                            key=f"{codec.name}{field}",
                        )
                    )
        if not codecs:
            rows.append(
                self.row(
                    "codec",
                    "Codec",
                    "Not reported. USB and Bluetooth audio devices have no HD Audio "
                    "codec to describe.",
                )
            )
        return rows

    def _device_rows(self, card) -> list:
        playback = []
        capture = []
        for path in list_dir(card["path"]):
            name = path.name
            if name.startswith(f"pcm{card['index']}") or re.match(r"^pcmC\d+D\d+[pc]$", name):
                (playback if name.endswith("p") else capture).append(name)
        rows = [
            self.row(
                "playback_devices",
                "Playback devices",
                fmt_list(sorted(playback), empty="None"),
            ),
            self.row(
                "capture_devices",
                "Capture devices",
                fmt_list(sorted(capture), empty="None"),
            ),
        ]
        controls = [
            path.name for path in list_dir(card["path"]) if path.name.startswith("controlC")
        ]
        if controls:
            rows.append(
                self.row("control_devices", "Control devices", fmt_list(sorted(controls)))
            )
        hwdeps = [
            path.name for path in list_dir(card["path"]) if path.name.startswith("hwC")
        ]
        if hwdeps:
            rows.append(
                self.row("hwdep_devices", "Hardware dependent devices", fmt_list(sorted(hwdeps)))
            )
        return rows
