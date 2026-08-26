# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""One content row: a label, a value, and the two things you can do to it.

The row has two click targets and they must never overlap, so they are two
different objects. The copy control is a real button and swallows its own
clicks; everything else in the row belongs to the row, which toggles the
explanation. There is no coordinate arithmetic deciding which one you hit.

What lands on the clipboard is the raw value, not the glossed one. A row
reading ``16 — SMT enabled`` copies ``16``. The gloss is there to be read; the
value is there to be pasted into a search box, and a search for "16 — SMT
enabled" finds nothing.

The row grows downward when it expands. It does not overlay, float, or open
anything - the rows below simply move down, which is what a scrollable list of
widgets does for free and what any popup would have had to fake.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStyle,
    QStyleOption,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..probes.base import DANGER, WARNING

ROW_HEIGHT = 32
LABEL_SHARE = 0.38
COPY_WIDTH = 22
CHEVRON_WIDTH = 18
BODY_TOP_PADDING = 12
BODY_LEFT_PADDING = 16
COPY_CONFIRM_MS = 1000

COLLAPSED_CHEVRON = "▸"
EXPANDED_CHEVRON = "▾"

SEVERITY_ICONS = {WARNING: "warning", DANGER: "danger"}


class ElidingLabel(QLabel):
    """A label that shortens its text rather than forcing the row wider.

    Values elide only at extreme narrowness: the label reports a minimum width
    of zero, so the layout gives it whatever is left after the label column and
    the controls, and it trims to fit only when that is genuinely too little.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._full = ""
        self._applying = False
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def set_full_text(self, text: str) -> None:
        self._full = text or ""
        self._apply()

    def full_text(self) -> str:
        return self._full

    def _apply(self) -> None:
        if self._applying:
            return
        self._applying = True
        try:
            width = max(0, self.width())
            metrics = QFontMetrics(self.font())
            if width <= 0 or metrics.horizontalAdvance(self._full) <= width:
                self.setText(self._full)
            else:
                self.setText(
                    metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, width)
                )
        finally:
            self._applying = False

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply()

    def minimumSizeHint(self):  # noqa: N802 - Qt naming
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint


class CopyButton(QAbstractButton):
    """The per-row copy control, shown only while the pointer is in the row.

    It keeps its place in the layout at all times and simply draws nothing when
    the row is not hovered. Adding and removing it would shift the value column
    sideways every time the pointer crossed a row.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("copyButton")
        self.setFixedWidth(COPY_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._visible = False
        self._confirmed = False
        family = icons.family()
        if family:
            font = self.font()
            font.setFamily(family)
            self.setFont(font)

    def set_hovered(self, hovered: bool) -> None:
        if self._visible == hovered:
            return
        self._visible = hovered
        if not hovered:
            self._confirmed = False
        self.update()

    def set_confirmed(self, confirmed: bool) -> None:
        self._confirmed = confirmed
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._visible:
            return
        glyph = icons.glyph("copy_done" if self._confirmed else "copy")
        if not glyph:
            # No icon font: fall back to text so the control still exists.
            glyph = "OK" if self._confirmed else "Copy"
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().windowText().color())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, glyph)


class ContentRow(QWidget):
    """A label, a value, and an explanation that opens in place."""

    toggled_expansion = Signal()

    def __init__(
        self,
        row_id: str,
        label: str,
        value: str,
        severity: str = "normal",
        raw_value: str | None = None,
        body: str = "",
        odd: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.row_id = row_id
        self._raw_value = raw_value if raw_value is not None else value
        self._body_text = body or ""
        self._expanded = False

        self.setObjectName("contentRow")
        self.setProperty("odd", odd)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._header = QWidget(self)
        self._header.setObjectName("rowHeader")
        self._header.setFixedHeight(ROW_HEIGHT)
        self._header.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(12, 0, 8, 0)
        header_layout.setSpacing(8)

        self.label = QLabel(label, self._header)
        self.label.setObjectName("rowLabel")
        self.label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        header_layout.addWidget(self.label)

        # The value area: a stretch, then the severity glyph, then the value.
        # The stretch keeps everything hard against the right until the value
        # is too long to fit, at which point it collapses and the value elides.
        value_area = QWidget(self._header)
        value_area.setObjectName("rowValueArea")
        value_layout = QHBoxLayout(value_area)
        value_layout.setContentsMargins(0, 0, 0, 0)
        value_layout.setSpacing(6)
        value_layout.addStretch(1)

        self.severity_label = QLabel(value_area)
        self.severity_label.setObjectName("rowSeverity")
        family = icons.family()
        if family:
            font = self.severity_label.font()
            font.setFamily(family)
            self.severity_label.setFont(font)
        value_layout.addWidget(self.severity_label)

        self.value = ElidingLabel(value_area)
        self.value.setObjectName("rowValue")
        self.value.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        value_layout.addWidget(self.value)
        header_layout.addWidget(value_area, 1)

        self.copy_button = CopyButton(self._header)
        header_layout.addWidget(self.copy_button)

        self.chevron = QLabel(self._header)
        self.chevron.setObjectName("rowChevron")
        self.chevron.setFixedWidth(CHEVRON_WIDTH)
        self.chevron.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(self.chevron)

        for passive in (self.label, self.severity_label, self.value, self.chevron):
            passive.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
            )

        outer.addWidget(self._header)

        self.body = QLabel(self)
        self.body.setObjectName("rowBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.body.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.body.setVisible(False)
        outer.addWidget(self.body)

        # The copy control is a button and keeps its own clicks; the rest of
        # the row belongs to the row. That is the whole of the separation.
        self.copy_button.clicked.connect(self._copy)

        self._confirm_timer = QTimer(self)
        self._confirm_timer.setSingleShot(True)
        self._confirm_timer.setInterval(COPY_CONFIRM_MS)
        self._confirm_timer.timeout.connect(lambda: self.copy_button.set_confirmed(False))

        self.setMouseTracking(True)
        self.set_value(value, severity, raw_value=self._raw_value)
        self.set_body(self._body_text)

    # -- painting and hover ------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_Widget, option, painter, self
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self._expanded:
            self._apply_body_margins()

    def _apply_body_margins(self) -> None:
        """Indent the explanation so it lines up with the value column."""
        self.body.setContentsMargins(
            self.label.width() + BODY_LEFT_PADDING,
            BODY_TOP_PADDING,
            16,
            BODY_TOP_PADDING,
        )

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.copy_button.set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.copy_button.set_hovered(False)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self.has_body():
            self.set_expanded(not self._expanded)
        super().mouseReleaseEvent(event)

    # -- content -----------------------------------------------------------

    def set_label_width(self, width: int) -> None:
        """Fix the label column. CORE 13.6: 38% of the content width."""
        self.label.setFixedWidth(max(0, width))
        if self._expanded:
            self._apply_body_margins()

    def set_value(self, text: str, severity: str = "normal", raw_value: str | None = None) -> None:
        """Update the row in place. This is what the volatile pass calls.

        Nothing is rebuilt and no widget is replaced, so an open explanation
        stays open and the scroll position does not move.
        """
        if raw_value is not None:
            self._raw_value = raw_value
        self.value.set_full_text(text)

        glyph = icons.glyph(SEVERITY_ICONS.get(severity, ""))
        self.severity_label.setText(glyph)
        self.severity_label.setVisible(bool(glyph))

        if self.value.property("severity") != severity:
            self.value.setProperty("severity", severity)
            self.severity_label.setProperty("severity", severity)
            for widget in (self.value, self.severity_label):
                widget.style().unpolish(widget)
                widget.style().polish(widget)
        self.update()

    def raw_value(self) -> str:
        return self._raw_value

    def displayed_value(self) -> str:
        return self.value.full_text()

    def set_body(self, text: str) -> None:
        """Attach or remove the explanation. No entry means no chevron."""
        self._body_text = text or ""
        self.body.setText(self._body_text)
        if not self._body_text:
            self.set_expanded(False)
            self.chevron.setText("")
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chevron.setText(
            EXPANDED_CHEVRON if self._expanded else COLLAPSED_CHEVRON
        )

    def has_body(self) -> bool:
        return bool(self._body_text)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded) and self.has_body()
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.body.setVisible(expanded)
        self.chevron.setText(EXPANDED_CHEVRON if expanded else COLLAPSED_CHEVRON)
        if expanded:
            self._apply_body_margins()
        self.updateGeometry()
        self.toggled_expansion.emit()

    # -- copying -----------------------------------------------------------

    def _copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(self._raw_value)
        self.copy_button.set_confirmed(True)
        self._confirm_timer.start()
