# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Calling the root helper once, and taking no for an answer.

The prompt fires once, at launch, before the window is shown. If the user
cancels, if authentication fails, if the helper is missing, if it times out or
if what comes back is not JSON, the result is an empty one carrying
``refused``. There is no retry loop, no second prompt, no dialog and no nagging.
Every row that wanted privileged data renders
``Requires administrator access — restart KÖKEN to authenticate`` instead, and
the footer says so once.

The subprocess is deliberately behind one function. :func:`from_json` and
:func:`from_file` produce exactly the same object from captured output, which
is the only way to exercise this path in a container where pkexec cannot run
at all.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .probes.base import REQUIRES_ROOT

# Where the packages install it. CORE fixes this path and the polkit policy
# repeats it verbatim, because pkexec matches the two against each other.
INSTALLED_HELPER = "/usr/lib/koken/koken-helper"

# Tried in order. The second is where a Debian-flavoured layout might put it;
# the third lets the helper be exercised straight from a source checkout,
# where pkexec falls back to asking for the root password because no policy
# names that path. That is a correct outcome, not a bug.
HELPER_CANDIDATES = (
    INSTALLED_HELPER,
    "/usr/libexec/koken/koken-helper",
)

TIMEOUT_MS = 10_000

# pkexec's own exit codes, distinct from anything the helper returns.
EXIT_DISMISSED = 126
EXIT_NOT_AUTHORISED = 127


class PrivilegedData:
    """What the helper returned, or a refusal.

    ``available`` is the only thing probes need to branch on. Everything else
    answers empty when the read did not happen, so a probe that forgets to
    check still renders sensibly rather than raising.
    """

    def __init__(
        self,
        payload: dict | None = None,
        refused: bool = False,
        reason: str = "",
    ) -> None:
        self._payload = payload if isinstance(payload, dict) else {}
        self.refused = bool(refused)
        self.reason = reason

    # -- state ------------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._payload) and not self.refused

    @property
    def version(self) -> int | None:
        value = self._payload.get("version")
        return value if isinstance(value, int) else None

    # -- contents ---------------------------------------------------------

    def _dmi(self) -> dict:
        value = self._payload.get("dmi")
        return value if isinstance(value, dict) else {}

    @property
    def type16(self) -> list[dict]:
        """Physical memory array records: capacity ceiling, slot count, ECC."""
        value = self._dmi().get("type16")
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @property
    def type17(self) -> list[dict]:
        """Memory device records, one per slot, populated or not."""
        value = self._dmi().get("type17")
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @property
    def serials(self) -> dict[str, str]:
        value = self._dmi().get("serials")
        if not isinstance(value, dict):
            return {}
        return {k: str(v) for k, v in value.items() if isinstance(k, str) and v}

    @property
    def gpu_firmware(self) -> dict[str, dict]:
        value = self._payload.get("gpu_firmware")
        if not isinstance(value, dict):
            return {}
        return {str(k): v for k, v in value.items() if isinstance(v, dict)}

    @property
    def helper_errors(self) -> list[str]:
        """What the helper could not read. A partial read is a normal outcome."""
        value = self._payload.get("errors")
        return [str(item) for item in value] if isinstance(value, list) else []

    # -- convenience for probes -------------------------------------------

    def serial(self, name: str) -> str | None:
        return self.serials.get(name)

    def firmware_for_card(self, index: str | int) -> dict:
        return self.gpu_firmware.get(str(index), {})

    def text(self, value, fallback: str = "") -> str:
        """A privileged value as text, or the standard refusal line.

        Probes call this instead of testing ``available`` themselves, so the
        wording is identical on every row that wanted root and did not get it.
        """
        if not self.available:
            return REQUIRES_ROOT
        if value is None or str(value).strip() == "":
            from .probes.base import NOT_REPORTED

            return fallback or NOT_REPORTED
        return str(value).strip()

    @property
    def status_text(self) -> str:
        """One line for the footer indicator."""
        if self.available:
            errors = self.helper_errors
            if errors:
                return f"Administrator access granted, {len(errors)} item(s) unreadable"
            return "Administrator access granted"
        return self.reason or "Administrator access declined"


def _empty(reason: str) -> PrivilegedData:
    return PrivilegedData(payload=None, refused=True, reason=reason)


def parse_payload(text: str) -> dict | None:
    """Validate the helper's output. Anything unexpected returns None."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != 1:
        return None
    return payload


def from_json(text: str) -> PrivilegedData:
    """Build a result from helper output held in a string."""
    payload = parse_payload(text)
    if payload is None:
        return _empty("The helper returned something that was not the expected JSON")
    return PrivilegedData(payload=payload)


def from_file(path: str | Path) -> PrivilegedData:
    """Build a result from a captured helper run.

    This is the seam that makes the privileged path testable where pkexec
    cannot run: capture ``sudo ./helper/koken-helper > capture.json`` on a real
    machine, then feed the file in here.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return _empty(f"The captured helper output could not be read: {exc}")
    return from_json(text)


def find_helper() -> str | None:
    """The installed helper, or the one in a source checkout beside this file."""
    for candidate in HELPER_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # src/koken/privileged.py -> src/koken -> src -> repository root
    source_tree = Path(__file__).resolve().parents[2] / "helper" / "koken-helper"
    if source_tree.is_file() and os.access(source_tree, os.X_OK):
        return str(source_tree)
    return None


def run(helper_path: str | None = None, timeout_ms: int = TIMEOUT_MS) -> PrivilegedData:
    """Ask once, wait at most ten seconds, and never ask again.

    Blocking is correct here and only here: this runs before the window is
    shown, so there is no interface to keep responsive, and the polkit agent
    owns the screen until the user answers it.
    """
    from PySide6.QtCore import QProcess

    helper = helper_path or find_helper()
    if helper is None:
        return _empty("The privileged helper is not installed")

    pkexec = shutil.which("pkexec")
    if pkexec is None:
        return _empty("pkexec is not installed, so privileged details cannot be read")

    process = QProcess()
    process.setProgram(pkexec)
    process.setArguments([helper])
    process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
    process.start()

    if not process.waitForStarted(5_000):
        return _empty("The privileged helper could not be started")

    if not process.waitForFinished(timeout_ms):
        process.kill()
        process.waitForFinished(1_000)
        return _empty("The privileged helper did not finish within ten seconds")

    if process.exitStatus() != QProcess.ExitStatus.NormalExit:
        return _empty("The privileged helper stopped unexpectedly")

    code = process.exitCode()
    if code == EXIT_DISMISSED:
        return _empty("Administrator access declined")
    if code == EXIT_NOT_AUTHORISED:
        return _empty("Administrator access could not be obtained")
    if code != 0:
        return _empty(f"The privileged helper exited with status {code}")

    raw = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
    return from_json(raw)
