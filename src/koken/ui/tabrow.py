# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""One tab row widget, three visual weights, chosen by ``level``.

Rows 1 and 2 lay their tabs out with equal width and fill the selected one.
Row 3 uses the flow layout and marks the selected tab with an underline. That
is the whole difference, and it is a parameter rather than three classes
because the behaviour - selection, keyboard movement, rebuilding - is identical
at every level and would otherwise be written three times.

A tab is a small widget rather than a QPushButton because row 3 tabs carry a
glyph in a different font from their label, and a button draws its text in one
font. Two child labels solve that and cost nothing: both are styled by the
stylesheet, so both take their colour from the palette and both change when the
theme changes.
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

from .. import icons
from ..flowlayout import FlowLayout

# CORE 13.6. Heights are fixed so that crossing tabs never moves the content.
LEVEL_HEIGHTS = {1: 44, 2: 34, 3: 26}
LEVEL_PADDING = {1: 18, 2: 14, 3: 10}


class Tab(QAbstractButton):
    """One tab. Checkable, with an optional glyph before its label."""

    def __init__(self, entry_id: str, label: str, level: int, icon: str | None = None):
        super().__init__()
        self.entry_id = entry_id
        self.level = level
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName(f"tab{level}")
        self.setProperty("selected", False)
        self.setFixedHeight(LEVEL_HEIGHTS.get(level, 26))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QHBoxLayout(self)
        padding = LEVEL_PADDING.get(level, 10)
        layout.setContentsMargins(padding, 0, padding, 0)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.glyph_label: QLabel | None = None
        glyph = icons.glyph(icon) if icon else ""
        if glyph:
            self.glyph_label = QLabel(glyph)
            self.glyph_label.setObjectName("tabGlyph")
            family = icons.family()
            if family:
                font = self.glyph_label.font()
                font.setFamily(family)
                self.glyph_label.setFont(font)
            # The labels must not swallow the clicks meant for the tab.
            self.glyph_label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
            )
            layout.addWidget(self.glyph_label)

        self.text_label = QLabel(label)
        self.text_label.setObjectName(f"tabText{level}")
        self.text_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        layout.addWidget(self.text_label)

        if level == 3:
            self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Let the stylesheet paint the background.

        A plain QWidget ignores stylesheet backgrounds unless it asks the style
        to draw PE_Widget, which is what this does. Without it the selected tab
        would have no fill and no underline.
        """
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_Widget, option, painter, self
        )

    def set_selected(self, selected: bool) -> None:
        self.setChecked(selected)
        if self.property("selected") == selected:
            return
        self.setProperty("selected", selected)
        # A dynamic property change does not restyle on its own.
        self.style().unpolish(self)
        self.style().polish(self)
        for child in (self.glyph_label, self.text_label):
            if child is not None:
                child.style().unpolish(child)
                child.style().polish(child)
        self.update()

    def sizeHint(self):  # noqa: N802 - Qt naming
        hint = super().sizeHint()
        return self.layout().sizeHint() if self.layout() else hint

    def minimumSizeHint(self):  # noqa: N802 - Qt naming
        return self.layout().minimumSize() if self.layout() else super().minimumSizeHint()


class TabRow(QWidget):
    """A row of tabs at one level, with exactly one of them selected."""

    selected = Signal(str)

    def __init__(self, level: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.level = level
        self.setObjectName(f"tabRow{level}")
        self._tabs: list[Tab] = []
        self._current: str | None = None

        if level == 3:
            # Row 3 wraps. No horizontal scrolling, no dropdown, no ellipsis.
            self._layout = FlowLayout(self, margin=0, horizontal_spacing=6, vertical_spacing=6)
        else:
            self._layout = QHBoxLayout(self)
            self._layout.setContentsMargins(0, 0, 0, 0)
            self._layout.setSpacing(6)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    # -- contents ---------------------------------------------------------

    def set_entries(self, entries, current: str | None = None) -> None:
        """Replace every tab. *entries* is ``(id, label, icon concept or None)``."""
        self.clear()
        for entry in entries:
            entry_id, label = entry[0], entry[1]
            icon = entry[2] if len(entry) > 2 else None
            tab = Tab(entry_id, label, self.level, icon)
            tab.clicked.connect(lambda _checked=False, name=entry_id: self._on_click(name))
            self._tabs.append(tab)
            if self.level == 3:
                self._layout.addWidget(tab)
            else:
                self._layout.addWidget(tab, 1)

        chosen = current if current in self.ids() else (self.ids()[0] if self._tabs else None)
        self._current = None
        if chosen is not None:
            self.set_current(chosen, notify=False)
        self.updateGeometry()

    def clear(self) -> None:
        for tab in self._tabs:
            self._layout.removeWidget(tab)
            tab.setParent(None)
            tab.deleteLater()
        self._tabs = []
        self._current = None

    def ids(self) -> list[str]:
        return [tab.entry_id for tab in self._tabs]

    def current(self) -> str | None:
        return self._current

    def count(self) -> int:
        return len(self._tabs)

    # -- selection --------------------------------------------------------

    def _on_click(self, entry_id: str) -> None:
        self.set_current(entry_id, notify=True)

    def set_current(self, entry_id: str | None, notify: bool = True) -> None:
        if entry_id is not None and entry_id not in self.ids():
            return
        changed = entry_id != self._current
        self._current = entry_id
        for tab in self._tabs:
            tab.set_selected(tab.entry_id == entry_id)
        if changed and notify and entry_id is not None:
            self.selected.emit(entry_id)

    def move(self, delta: int) -> None:
        """Step the selection, wrapping at both ends. Drives the arrow keys."""
        ids = self.ids()
        if not ids:
            return
        if self._current in ids:
            index = ids.index(self._current)
        else:
            index = 0
        self.set_current(ids[(index + delta) % len(ids)], notify=True)

    def select_index(self, index: int) -> None:
        ids = self.ids()
        if 0 <= index < len(ids):
            self.set_current(ids[index], notify=True)
