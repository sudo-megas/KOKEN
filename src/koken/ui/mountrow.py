# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The mount state row: the only interactive element in the application.

The two-step guard is the point of this file. A single click never acts. The
first click changes the button to ``Confirm unmount`` and starts a five second
countdown; a second click inside that window performs the call. This is not a
dialog and does not break the no-popup rule - the button changes in place, and
nothing opens.

It exists because an otherwise inert browser, where every other row does
nothing at all, is exactly the kind of interface where a mis-click unmounts a
drive mid-write. Inert rows need no friction. This one does.

Failures are reported in the row's own expansion body, in plain language, and
stay there until the next action or refresh. They do not go to the footer and
they do not open anything.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QPushButton

from .. import icons
from ..probes.base import WARNING
from ..probes.volumes import STATE_MOUNTED, STATE_NO_FILESYSTEM, STATE_UNMOUNTED
from .row import ContentRow

ARM_SECONDS = 5

IDLE = "idle"
ARMED = "armed"
WORKING = "working"

LABELS = {
    STATE_MOUNTED: ("Unmount", "Confirm unmount", "unmount"),
    STATE_UNMOUNTED: ("Mount", "Confirm mount", "mount"),
}

WORKING_LABEL = "Working…"

# Only one row may be armed at a time. Arming a second disarms the first, which
# is the concrete meaning of "reverts if focus moves elsewhere".
_armed_row: "MountStateRow | None" = None


class MountStateRow(ContentRow):
    """A content row with a button, for a volume that can be mounted."""

    def __init__(
        self,
        section,
        row,
        actions,
        on_success=None,
        odd: bool = False,
        parent=None,
    ):
        super().__init__(
            row_id=row.id,
            label=row.label,
            value=row.value,
            severity=row.severity,
            raw_value=row.value,
            body="",
            odd=odd,
            parent=parent,
        )
        self.device_node = f"/dev/{section.id}"
        self.volume_state = _state_of(row)
        self._actions = actions
        self._on_success = on_success
        self._phase = IDLE

        self.button = QPushButton(self._header)
        self.button.setObjectName("mountButton")
        self.button.setProperty("armed", False)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button.setFixedHeight(22)
        self.button.clicked.connect(self._on_click)

        # Between the value and the copy control: the rightmost thing on the
        # row that is not the row's own chevron.
        layout = self._header.layout()
        layout.insertWidget(layout.count() - 2, self.button)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(ARM_SECONDS * 1000)
        self._timer.timeout.connect(self.disarm)

        self._apply_phase()

        window = self.window()
        if window is not None:
            window.installEventFilter(self)

    # -- state -------------------------------------------------------------

    def set_volume_state(self, state: str) -> None:
        if state == self.volume_state:
            return
        self.volume_state = state
        self.disarm()
        self._apply_phase()

    def _labels(self):
        return LABELS.get(self.volume_state)

    def _apply_phase(self) -> None:
        labels = self._labels()
        if labels is None:
            # No mountable filesystem: no control at all, per CORE 12.1.
            self.button.setVisible(False)
            return
        idle_label, armed_label, concept = labels
        self.button.setVisible(True)

        if self._phase == WORKING:
            self.button.setText(WORKING_LABEL)
            self.button.setEnabled(False)
            self._set_armed_property(False)
            return

        self.button.setEnabled(True)
        if self._phase == ARMED:
            self.button.setText(armed_label)
            self._set_armed_property(True)
        else:
            glyph = icons.glyph(concept)
            self.button.setText(f"{glyph} {idle_label}" if glyph else idle_label)
            if glyph:
                font = self.button.font()
                # The glyph and the label share one button, so the button uses
                # the interface font and Qt falls back to the icon font for the
                # single character it cannot draw.
                self.button.setFont(font)
            self._set_armed_property(False)

    def _set_armed_property(self, armed: bool) -> None:
        if self.button.property("armed") == armed:
            return
        self.button.setProperty("armed", armed)
        self.button.style().unpolish(self.button)
        self.button.style().polish(self.button)

    # -- the guard ---------------------------------------------------------

    def _on_click(self) -> None:
        if self._phase == WORKING:
            return
        if self._phase == IDLE:
            self.arm()
            return
        self._perform()

    def arm(self) -> None:
        global _armed_row
        if _armed_row is not None and _armed_row is not self:
            _armed_row.disarm()
        _armed_row = self
        self._phase = ARMED
        self._apply_phase()
        self._timer.start()

    def disarm(self) -> None:
        global _armed_row
        if _armed_row is self:
            _armed_row = None
        self._timer.stop()
        if self._phase == ARMED:
            self._phase = IDLE
            self._apply_phase()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        """Disarm when the window stops being the one in front."""
        if event.type() == QEvent.Type.WindowDeactivate:
            self.disarm()
        return False

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Clicking the row itself is "somewhere else" as far as an armed
        # button is concerned.
        if self._phase == ARMED:
            self.disarm()
        super().mouseReleaseEvent(event)

    # -- the call ----------------------------------------------------------

    def _perform(self) -> None:
        global _armed_row
        if _armed_row is self:
            _armed_row = None
        self._timer.stop()
        self._phase = WORKING
        self._apply_phase()
        self.set_body("")

        method = (
            self._actions.unmount
            if self.volume_state == STATE_MOUNTED
            else self._actions.mount
        )
        method(self.device_node, self._finished)

    def _finished(self, result) -> None:
        self._phase = IDLE
        self._apply_phase()

        if result.ok or result.was_stale:
            # A stale view is refreshed the same way a success is: in both
            # cases what is on screen no longer matches the machine.
            self.set_body(result.message if result.was_stale else "")
            self.set_value(self.value.full_text(), self._current_severity())
            if self._on_success is not None:
                self._on_success()
            return

        self.set_body(result.message)
        self.set_expanded(True)
        self.set_value(self.value.full_text(), WARNING)

    def _current_severity(self) -> str:
        return self.value.property("severity") or "normal"


def _state_of(row) -> str:
    """Read the volume's state back off the row volumes.py built."""
    from ..probes.volumes import NOT_MOUNTED_TEXT

    value = (row.value or "").strip()
    if value == NOT_MOUNTED_TEXT:
        return STATE_UNMOUNTED
    if value.startswith("/") or value == "Mounted":
        return STATE_MOUNTED
    return STATE_NO_FILESYSTEM


def make_factory(actions, on_success):
    """Build the callable the window uses to construct this row.

    The window knows only that the Storage branch's Volumes sections have a
    first row somebody else builds. It has no idea what a mount is.
    """

    def factory(section, row, odd: bool = False):
        try:
            return MountStateRow(
                section=section,
                row=row,
                actions=actions,
                on_success=on_success,
                odd=odd,
            )
        except Exception:
            # An interactive row that cannot be built must not take the
            # Volumes view down with it; the window falls back to a plain row.
            return None

    return factory
