# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The one place that knows where anything lives.

Everything KOKEN reads that is not hardware sits in a single directory::

    $XDG_CONFIG_HOME/koken/
    |- settings.toml
    |- explanations.en.toml
    +- palettes/
       |- catppuccin-latte.toml
       +- catppuccin-mocha.toml

The defaults travel inside the installed package under ``koken/defaults/`` and
are read with :mod:`importlib.resources`, which works the same whether the
package is an unpacked directory, a wheel installed into site-packages, or a
zip import. On every start the directories are created if absent and each
default file is copied out **only if no file of that name exists**. Nothing is
overwritten, nothing is merged, and nothing is ever deleted.

The consequence is deliberate and is stated in the README: an upgrade brings a
newer explanation corpus only to someone who has no file yet. Anyone who has
edited theirs keeps their edit, and takes the new corpus by deleting their copy
and relaunching. The alternative - tracking which files the user meant to
delete - is more machinery than the problem deserves.

No other module in this codebase builds a config path by hand.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

APP_DIR_NAME = "koken"
SETTINGS_NAME = "settings.toml"
EXPLANATIONS_NAME = "explanations.en.toml"
PALETTES_DIR_NAME = "palettes"

# The interval choices offered by the footer control, in seconds. Zero is Off.
REFRESH_INTERVALS = (0, 1, 2, 5, 10)
DEFAULT_REFRESH_INTERVAL = 2

BRANCHES = ("hardware", "system", "storage", "peripherals")
DEFAULT_BRANCH = "hardware"


def config_home() -> Path:
    """``$XDG_CONFIG_HOME``, honoured when set, ``~/.config`` when not.

    The specification says an empty or relative value is to be treated as
    unset, so both fall back rather than producing a path relative to whatever
    directory the application happened to start in.
    """
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if raw:
        candidate = Path(raw)
        if candidate.is_absolute():
            return candidate
    return Path.home() / ".config"


def app_dir() -> Path:
    return config_home() / APP_DIR_NAME


def palettes_dir() -> Path:
    return app_dir() / PALETTES_DIR_NAME


def settings_path() -> Path:
    return app_dir() / SETTINGS_NAME


def explanations_path() -> Path:
    return app_dir() / EXPLANATIONS_NAME


# --------------------------------------------------------------------------
# Package data
# --------------------------------------------------------------------------


def _defaults_root():
    return resources.files("koken").joinpath("defaults")


def read_default(*parts: str) -> bytes | None:
    """Read one shipped default out of the installed package, or None."""
    try:
        target = _defaults_root()
        for part in parts:
            target = target.joinpath(part)
        return target.read_bytes()
    except (OSError, ModuleNotFoundError, FileNotFoundError, AttributeError):
        return None


def default_palette_names() -> list[str]:
    """Every palette filename shipped inside the package."""
    try:
        entries = _defaults_root().joinpath(PALETTES_DIR_NAME).iterdir()
    except (OSError, ModuleNotFoundError, FileNotFoundError, AttributeError):
        return []
    names = []
    for entry in entries:
        name = getattr(entry, "name", "")
        if name.endswith(".toml"):
            names.append(name)
    return sorted(names)


def asset_bytes(name: str) -> bytes | None:
    """Read a code asset - the icon font - out of the package. Never seeded."""
    try:
        return resources.files("koken").joinpath("assets", name).read_bytes()
    except (OSError, ModuleNotFoundError, FileNotFoundError, AttributeError):
        return None


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def _copy_if_absent(destination: Path, payload: bytes | None) -> bool:
    """Write *payload* to *destination* only when nothing is there.

    Returns True when a file was written. ``x`` mode makes the check and the
    write one operation, so two copies of the application starting at once
    cannot have one truncate the other's freshly written file.
    """
    if payload is None:
        return False
    try:
        if destination.exists():
            return False
        with destination.open("xb") as handle:
            handle.write(payload)
        return True
    except OSError:
        return False


def ensure_config_tree() -> list[Path]:
    """Create the directories and seed any absent default. Returns what was written."""
    written: list[Path] = []
    directory = app_dir()
    palettes = palettes_dir()
    try:
        palettes.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A read-only or unwritable home is survivable: the shipped defaults
        # are still readable from inside the package, so the application runs
        # with them and simply persists nothing.
        return written

    if _copy_if_absent(directory / EXPLANATIONS_NAME, read_default(EXPLANATIONS_NAME)):
        written.append(directory / EXPLANATIONS_NAME)

    for name in default_palette_names():
        target = palettes / name
        if _copy_if_absent(target, read_default(PALETTES_DIR_NAME, name)):
            written.append(target)

    return written


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass
class Settings:
    """The two keys that persist. Nothing else is remembered.

    No window geometry, no cached hardware, no history. A missing or corrupt
    file falls back to these defaults silently - there is no dialog to show and
    nothing the user could usefully do about it.
    """

    refresh_interval: int = DEFAULT_REFRESH_INTERVAL
    last_branch: str = DEFAULT_BRANCH

    def normalised(self) -> "Settings":
        interval = self.refresh_interval
        if not isinstance(interval, int) or isinstance(interval, bool):
            interval = DEFAULT_REFRESH_INTERVAL
        if interval not in REFRESH_INTERVALS:
            interval = DEFAULT_REFRESH_INTERVAL
        branch = self.last_branch
        if not isinstance(branch, str) or branch not in BRANCHES:
            branch = DEFAULT_BRANCH
        return Settings(refresh_interval=interval, last_branch=branch)


def load_settings() -> Settings:
    """Read ``settings.toml``. Anything unreadable or wrong yields the defaults."""
    path = settings_path()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    return Settings(
        refresh_interval=data.get("refresh_interval", DEFAULT_REFRESH_INTERVAL),
        last_branch=data.get("last_branch", DEFAULT_BRANCH),
    ).normalised()


def save_settings(settings: Settings) -> bool:
    """Write ``settings.toml`` on clean exit. Failure is silent and harmless.

    Written by hand rather than through a library: the standard library reads
    TOML from 3.11 but does not write it, two keys do not justify a dependency,
    and both values are constrained to a small set that needs no escaping.
    """
    settings = settings.normalised()
    text = (
        "# KOKEN settings. Two keys, written on clean exit.\n"
        f"refresh_interval = {settings.refresh_interval}\n"
        f'last_branch = "{settings.last_branch}"\n'
    )
    path = settings_path()
    temporary = path.with_name(path.name + ".new")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
        return True
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False


def load_explanations_text() -> str | None:
    """The user's explanation file, falling back to the shipped corpus.

    The fallback matters when the home directory could not be written: the
    application still shows explanations, it simply reads them from inside the
    package instead.
    """
    try:
        return explanations_path().read_text(encoding="utf-8")
    except (OSError, ValueError):
        payload = read_default(EXPLANATIONS_NAME)
        if payload is None:
            return None
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
