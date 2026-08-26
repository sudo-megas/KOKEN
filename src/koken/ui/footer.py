# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The footer: how often to re-read, whether root answered, and when it last ran.

Three pieces of state that belong nowhere else. The privileged indicator is
here because CORE says the refusal is reflected once, in the footer, and not in
a dialog, a banner, or a repeated prompt.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStyle,
    QStyleOption,
    QWidget,
)

from ..config import REFRESH_INTERVALS


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
        self.setFixedHeight(22)
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


class Footer(QWidget):
    """The interval control, the privileged indicator, and the last refresh time."""

    interval_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("footer")
        self.setFixedHeight(34)
        self._segments: list[Segment] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(10)

        caption = QLabel("Refresh")
        caption.setObjectName("footerCaption")
        layout.addWidget(caption)

        for seconds in REFRESH_INTERVALS:
            segment = Segment(seconds)
            segment.clicked.connect(
                lambda _checked=False, value=seconds: self._choose(value)
            )
            self._segments.append(segment)
            layout.addWidget(segment)

        layout.addStretch(1)

        self.privileged_label = QLabel("")
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

    # -- indicators -------------------------------------------------------

    def set_privileged_status(self, text: str, granted: bool) -> None:
        self.privileged_label.setText(text)
        self.privileged_label.setProperty("severity", "normal" if granted else "warning")
        self.privileged_label.style().unpolish(self.privileged_label)
        self.privileged_label.style().polish(self.privileged_label)

    def set_last_refresh(self, text: str) -> None:
        self.refreshed_label.setText(text)
