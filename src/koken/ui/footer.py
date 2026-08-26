# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The footer: refresh now, how often to re-read, About, and what root answered.

The pieces of state and control that belong nowhere else. The privileged
indicator is here because CORE says the refusal is reflected once, in the
footer, and not in a dialog, a banner, or a repeated prompt.

Two of the controls are here because a keyboard shortcut that nothing on screen
mentions cannot be found by anyone who was not told about it. ``F5`` has always
re-enumerated and About has always existed; the Refresh button and the About
toggle put both where they can be seen. Neither is a second implementation of
anything - the button emits what F5 emits, and the toggle switches the content
area rather than opening the popup CORE forbids.

The interval caption reads ``Auto`` rather than ``Refresh``. Two controls a
centimetre apart both labelled Refresh, one a verb and one a setting, is the
kind of thing a reader has to stop and work out. The segments themselves are
untouched: ``Off - 1s - 2s - 5s - 10s``, default 2s, persisted.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOption,
    QWidget,
)

from ..config import REFRESH_INTERVALS
from .row import ElidingLabel

# The interval caption, and the two labels the refresh button alternates
# between. The busy label is not decoration: a synchronous re-enumeration on a
# machine with several disks takes long enough that a control which still reads
# "Refresh" and still looks pressable is a bug report waiting to be filed.
INTERVAL_CAPTION = "Auto"
REFRESH_TEXT = "Refresh"
REFRESH_BUSY_TEXT = "Refreshing…"
ABOUT_TEXT = "About"

# The height of every control in the footer, so the row reads as one strip.
CONTROL_HEIGHT = 22


def interval_label(seconds: int) -> str:
    return "Off" if seconds == 0 else f"{seconds}s"


class Segment(QAbstractButton):
    """One choice in the interval control."""

    def __init__(self, seconds: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.seconds = seconds
        self.setText(interval_label(seconds))
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("segment")
        self.setProperty("selected", False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedHeight(CONTROL_HEIGHT)
        self.setMinimumWidth(38)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_Widget, option, painter, self
        )
        painter.setPen(self.palette().windowText().color())
        painter.setFont(self.font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())

    def set_selected(self, selected: bool) -> None:
        self.setChecked(selected)
        if self.property("selected") == selected:
            return
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class FooterButton(QPushButton):
    """A footer control with a text label, the same height as a segment.

    A real button rather than the hand-painted :class:`Segment`, because these
    two carry ordinary labels with no state to draw beyond what the stylesheet
    already expresses. No glyph on either: CORE 13.5 fixes the four places an
    icon may appear, and the footer is not one of them.
    """

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("footerButton")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedHeight(CONTROL_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_selected(self, selected: bool) -> None:
        """Fill the button, the way a chosen segment or tab is filled."""
        if self.property("selected") == selected:
            return
        self.setProperty("selected", selected)
        # A dynamic property change does not restyle on its own.
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class RefreshButton(FooterButton):
    """The Refresh control, wide enough for either of the two things it says.

    Sized from the longer label at all times. Letting the width follow the
    current text would slide the interval segments sideways for as long as a
    refresh takes and slide them back afterwards, which reads as the footer
    twitching every time the button is pressed.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(REFRESH_TEXT, parent)

    def _widest(self) -> int:
        metrics = QFontMetrics(self.font())
        return max(
            metrics.horizontalAdvance(REFRESH_TEXT),
            metrics.horizontalAdvance(REFRESH_BUSY_TEXT),
        )

    def sizeHint(self):  # noqa: N802 - Qt naming
        """The style's own hint, with the text swapped for the longer one.

        Taken from the style rather than computed, so whatever padding and
        border the stylesheet gives the button is included without this class
        knowing what they are.
        """
        hint = super().sizeHint()
        metrics = QFontMetrics(self.font())
        current = metrics.horizontalAdvance(self.text())
        hint.setWidth(hint.width() + max(0, self._widest() - current))
        return hint

    def minimumSizeHint(self):  # noqa: N802 - Qt naming
        return self.sizeHint()


class Footer(QWidget):
    """Refresh, the interval control, About, and the two indicators."""

    interval_changed = Signal(int)
    refresh_pressed = Signal()
    about_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("footer")
        self.setFixedHeight(34)
        self._segments: list[Segment] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(10)

        self.refresh_button = RefreshButton()
        self.refresh_button.clicked.connect(self.refresh_pressed)
        layout.addWidget(self.refresh_button)

        caption = QLabel(INTERVAL_CAPTION)
        caption.setObjectName("footerCaption")
        layout.addWidget(caption)

        for seconds in REFRESH_INTERVALS:
            segment = Segment(seconds)
            segment.clicked.connect(
                lambda _checked=False, value=seconds: self._choose(value)
            )
            self._segments.append(segment)
            layout.addWidget(segment)

        self.about_button = FooterButton(ABOUT_TEXT)
        self.about_button.setCheckable(True)
        self.about_button.clicked.connect(self._about_clicked)
        layout.addWidget(self.about_button)

        layout.addStretch(1)

        # The privileged line elides rather than clips. It is a sentence, and
        # at the minimum window width there is no longer room for all of it -
        # an ellipsis at least says that it continues, where the plain label it
        # used to be was cut through whichever letter happened to be at the
        # edge. It is also the only thing in the strip that can give up room,
        # which is why the timestamp beside it is an ordinary label: a fixed
        # nineteen characters that would otherwise be shortened to nothing.
        self.privileged_label = ElidingLabel()
        self.privileged_label.setObjectName("footerPrivileged")
        layout.addWidget(self.privileged_label)

        divider = QLabel("·")
        divider.setObjectName("footerCaption")
        layout.addWidget(divider)

        self.refreshed_label = QLabel("")
        self.refreshed_label.setObjectName("footerCaption")
        layout.addWidget(self.refreshed_label)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_Widget, option, painter, self
        )

    # -- interval ---------------------------------------------------------

    def _choose(self, seconds: int) -> None:
        self.set_interval(seconds, notify=True)

    def set_interval(self, seconds: int, notify: bool = False) -> None:
        if seconds not in REFRESH_INTERVALS:
            return
        for segment in self._segments:
            segment.set_selected(segment.seconds == seconds)
        if notify:
            self.interval_changed.emit(seconds)

    def interval(self) -> int:
        for segment in self._segments:
            if segment.isChecked():
                return segment.seconds
        return 0

    # -- refresh ----------------------------------------------------------

    def set_refreshing(self, busy: bool) -> None:
        """Say that a re-enumeration is running, and refuse a second one.

        The static pass runs on this thread, so the event loop is not going to
        come back and paint this for us: the repaint is taken here, before the
        caller starts the work. Without it the button would say Refresh, look
        pressable, and sit there frozen for as long as the pass takes.
        """
        self.refresh_button.setText(REFRESH_BUSY_TEXT if busy else REFRESH_TEXT)
        self.refresh_button.setEnabled(not busy)
        if busy:
            self.refresh_button.repaint()

    def is_refreshing(self) -> bool:
        return not self.refresh_button.isEnabled()

    # -- about ------------------------------------------------------------

    def _about_clicked(self) -> None:
        active = self.about_button.isChecked()
        self.about_button.set_selected(active)
        self.about_toggled.emit(active)

    def set_about_active(self, active: bool) -> None:
        """Match the button to the content area, without emitting anything.

        Used when something other than the button leaves the About view - a
        branch tab, or a keyboard selection.
        """
        self.about_button.setChecked(active)
        self.about_button.set_selected(active)

    def about_active(self) -> bool:
        return self.about_button.isChecked()

    # -- indicators -------------------------------------------------------

    def set_privileged_status(self, text: str, granted: bool) -> None:
        self.privileged_label.set_full_text(text)
        self.privileged_label.setProperty("severity", "normal" if granted else "warning")
        self.privileged_label.style().unpolish(self.privileged_label)
        self.privileged_label.style().polish(self.privileged_label)

    def set_last_refresh(self, text: str) -> None:
        self.refreshed_label.setText(text)
