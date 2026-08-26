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

Three things here are shaped by the fact that a successful call destroys the
very row that made it. The reply can land after the row is gone, so it is
checked for liveness before any widget is touched and the storage refresh is
run either way. The message that a call produced has to outlive the rebuild, so
it is left in :data:`_PENDING_NOTICE` for the replacement row to pick up. And
the armed-row marker is validated before use, because a rebuild can leave it
pointing at a widget whose C++ half is already deleted.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import QPushButton

from .. import icons
from ..probes.volumes import (
    NOT_MOUNTED_TEXT,
    STATE_MOUNTED,
    STATE_NO_FILESYSTEM,
    STATE_UNMOUNTED,
)
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

# A message produced by a call that then rebuilt the view, keyed by device node.
# CORE 12.4 requires the stale-view sentences to be shown, and the row that
# would have shown them no longer exists by the time the refresh is done, so the
# replacement row collects the message instead.
_PENDING_NOTICE: dict[str, str] = {}


def _is_alive(widget) -> bool:
    """Whether the C++ half of *widget* still exists.

    An asynchronous reply can arrive after the user has crossed to another tab,
    which destroys every row in the old view. Touching one then raises out of a
    D-Bus callback, which is both a crash and a silently skipped refresh.
    """
    try:
        from shiboken6 import isValid

        return isValid(widget)
    except Exception:
        return True


def _current_armed() -> "MountStateRow | None":
    """The armed row, if there still is one."""
    global _armed_row
    if _armed_row is None:
        return None
    if not _is_alive(_armed_row):
        _armed_row = None
    return _armed_row


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
        self.volume_state = _state_of(row.value)
        # From here on set_value may re-derive the state from the row's text.
        self._ready = True
        self._actions = actions
        self._on_success = on_success
        self._phase = IDLE
        self._filter_installed = False

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

        # A message left behind by the call that caused this row to be rebuilt.
        notice = _PENDING_NOTICE.pop(self.device_node, "")
        if notice:
            self._show_notice(notice)

    # -- state -------------------------------------------------------------

    def set_value(self, text: str, severity: str = "normal", raw_value=None) -> None:
        """Keep the button in step with the row's own text.

        The volatile pass reaches this row the same way it reaches every other
        one, so a volume mounted or unmounted from outside KOKEN updates the
        text here. Without re-deriving the state from it, the button would go on
        offering the action it was built with and send Mount to something that
        is already mounted.
        """
        super().set_value(text, severity, raw_value=raw_value)
        # ContentRow's constructor calls set_value before this subclass has
        # finished initialising, so there is one call with no state to sync to.
        if getattr(self, "_ready", False):
            self._sync_state(_state_of(self.displayed_value()))

    def _sync_state(self, state: str) -> None:
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
        previous = _current_armed()
        if previous is not None and previous is not self:
            previous.disarm()
        _armed_row = self
        self._phase = ARMED
        self._apply_phase()
        self._timer.start()

    def disarm(self) -> None:
        global _armed_row
        if _armed_row is self:
            _armed_row = None
        try:
            self._timer.stop()
        except RuntimeError:
            # The timer's C++ half went with the widget; nothing left to stop.
            return
        if self._phase == ARMED:
            self._phase = IDLE
            self._apply_phase()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Install the focus guard on the real window, once it exists.

        At construction the row has no parent, so ``window()`` answers with the
        row itself. Waiting until the row is shown is the first moment there is
        a window to watch.
        """
        super().showEvent(event)
        if self._filter_installed:
            return
        window = self.window()
        if window is not None and window is not self:
            window.installEventFilter(self)
            self._filter_installed = True

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
        alive = _is_alive(self)

        if result.ok or result.was_stale:
            # A stale view is refreshed the same way a success is: in both
            # cases what is on screen no longer matches the machine. The
            # message, if there is one, is left for the row that replaces this.
            if result.was_stale and result.message:
                _PENDING_NOTICE[self.device_node] = result.message
            if alive:
                self._phase = IDLE
                self._apply_phase()
            # Run regardless of whether this row survived: the view is wrong
            # either way, and refreshing it is the whole point of a success.
            if self._on_success is not None:
                self._on_success()
            return

        if not alive:
            return
        self._phase = IDLE
        self._apply_phase()
        self._show_notice(result.message)

    def _show_notice(self, message: str) -> None:
        """Put a message under the row, styled as a warning, expanded.

        CORE 12.4 puts failures here rather than in the footer, and styles them
        as warnings. The row's own severity is left alone: on this row warning
        already means "this filesystem is holding the running system up", and
        overloading it would make a USB stick that failed to unmount look like
        the root filesystem.
        """
        self.set_body(message)
        self.body.setProperty("severity", "warning")
        self.body.style().unpolish(self.body)
        self.body.style().polish(self.body)
        self.set_expanded(True)


def _state_of(value: str) -> str:
    """Read the volume's state back off the text volumes.py produced."""
    value = (value or "").strip()
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
