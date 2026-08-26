# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The explanation layer: the reason this application exists.

Every other system information tool reformats the jargon that lshw and lsblk
already print. What KOKEN adds is a sentence saying what the number means and
why anyone would care, and those sentences live in one editable file rather
than in this source tree.

A row with no entry renders normally and shows no expander. That is not a
degraded state, it is the mechanism: v1.0 ships with the sixty or so rows people
actually stop and look at, and coverage grows afterwards by editing one file,
with no release required.

A missing or malformed file leaves every row without an expander rather than
crashing. Somebody halfway through editing their own corpus has a broken TOML
file for a few seconds, and an application that refuses to start during those
seconds would be the worse of the two failures.
"""

from __future__ import annotations

import tomllib

from . import config

# The two keys an entry may carry. Anything else in a table is ignored.
SHORT = "short"
LONG = "long"


class Explanations:
    """Short glosses and long bodies, keyed by row id."""

    def __init__(self, entries: dict[str, dict] | None = None, error: str = ""):
        self._entries = entries or {}
        self.error = error

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def count(self) -> int:
        return len(self._entries)

    @property
    def long_count(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.get(LONG))

    @property
    def short_count(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.get(SHORT))

    def short(self, row_id: str) -> str | None:
        """The inline gloss, appended to the value as ``16 — SMT enabled``."""
        entry = self._entries.get(row_id)
        if not entry:
            return None
        value = entry.get(SHORT)
        return value or None

    def long(self, row_id: str) -> str | None:
        """The expansion body. None means the row shows no expander at all."""
        entry = self._entries.get(row_id)
        if not entry:
            return None
        value = entry.get(LONG)
        return value or None

    def has(self, row_id: str) -> bool:
        return row_id in self._entries


def flatten(data: dict, prefix: str = "") -> dict[str, dict]:
    """Turn nested TOML tables back into the dotted keys they were written as.

    ``[hardware.cpu.smt]`` parses into three levels of dictionary, but the key
    that matters is the dotted string, because that is what a row id is. A
    table is an entry when it carries a short or a long; anything else is a
    level on the way down.
    """
    out: dict[str, dict] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        path = f"{prefix}.{key}" if prefix else key
        short = value.get(SHORT)
        body = value.get(LONG)
        if isinstance(short, str) or isinstance(body, str):
            entry = {}
            if isinstance(short, str) and short.strip():
                entry[SHORT] = short.strip()
            if isinstance(body, str) and body.strip():
                entry[LONG] = normalise(body)
            if entry:
                out[path] = entry
        # A table can be both an entry and a parent, so recurse regardless.
        out.update(flatten(value, path))
    return out


def normalise(text: str) -> str:
    """Reflow a TOML multi-line string into paragraphs the widget can wrap.

    The file is written with hard line breaks so it stays readable in an
    editor at eighty columns. Those breaks are not meant to survive into the
    interface, where the label wraps to whatever width the window is - but the
    blank lines between paragraphs are, so they are kept and nothing else is.
    """
    paragraphs = []
    for block in text.strip().split("\n\n"):
        joined = " ".join(line.strip() for line in block.splitlines() if line.strip())
        if joined:
            paragraphs.append(joined)
    return "\n\n".join(paragraphs)


def parse(text: str) -> Explanations:
    """Parse a corpus. Malformed input yields an empty, working object."""
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        return Explanations(error=f"The explanation file could not be read: {exc}")
    if not isinstance(data, dict):
        return Explanations(error="The explanation file is not a table.")
    return Explanations(entries=flatten(data))


def load() -> Explanations:
    """Read the user's corpus, falling back to the shipped one."""
    text = config.load_explanations_text()
    if text is None:
        return Explanations(error="No explanation file was found.")
    return parse(text)
