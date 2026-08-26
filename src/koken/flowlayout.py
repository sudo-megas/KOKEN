# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""A layout that wraps its children onto a second line.

Qt has no flow layout, so this is the standard one from the Qt widget
examples, translated and commented. Row 3 needs it: a machine with fifteen USB
devices has fifteen tabs, and CORE says they wrap rather than scroll, ellipsise
or collapse into a menu.

The part that makes wrapping work is ``heightForWidth``. A widget that wraps
cannot answer "how tall are you?" without first being told how wide it will be,
so the layout advertises that its height depends on its width and does the same
placement arithmetic twice - once to measure, once to place.
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QStyle, QWidget


class FlowLayout(QLayout):
    """Left to right, top to bottom, wrapping at the right edge."""

    def __init__(
        self,
        parent: QWidget | None = None,
        margin: int = 0,
        horizontal_spacing: int = -1,
        vertical_spacing: int = -1,
    ) -> None:
        super().__init__(parent)
        self._items: list = []
        self._horizontal_spacing = horizontal_spacing
        self._vertical_spacing = vertical_spacing
        self.setContentsMargins(QMargins(margin, margin, margin, margin))

    def __del__(self) -> None:
        while self.count():
            self.takeAt(0)

    # -- QLayout ----------------------------------------------------------

    def addItem(self, item) -> None:  # noqa: N802 - Qt naming
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802 - Qt naming
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # noqa: N802 - Qt naming
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:  # noqa: N802 - Qt naming
        # Neither: the layout takes the width it is given and grows downward.
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt naming
        super().setGeometry(rect)
        self._layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt naming
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    # -- spacing ----------------------------------------------------------

    def horizontalSpacing(self) -> int:  # noqa: N802 - Qt naming
        if self._horizontal_spacing >= 0:
            return self._horizontal_spacing
        return self._style_spacing(QStyle.PixelMetric.PM_LayoutHorizontalSpacing)

    def verticalSpacing(self) -> int:  # noqa: N802 - Qt naming
        if self._vertical_spacing >= 0:
            return self._vertical_spacing
        return self._style_spacing(QStyle.PixelMetric.PM_LayoutVerticalSpacing)

    def _style_spacing(self, metric: QStyle.PixelMetric) -> int:
        parent = self.parent()
        if parent is None:
            return 0
        if isinstance(parent, QWidget):
            return parent.style().pixelMetric(metric, None, parent)
        return parent.spacing()

    # -- placement --------------------------------------------------------

    def _layout(self, rect: QRect, test_only: bool) -> int:
        """Place every item, or just measure how tall doing so would be.

        Returns the total height. Run with ``test_only`` from heightForWidth,
        where nothing may actually move, and without it from setGeometry.
        """
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        line_height = 0
        horizontal = self.horizontalSpacing()
        vertical = self.verticalSpacing()

        for item in self._items:
            widget = item.widget()
            space_x = horizontal
            space_y = vertical
            if widget is not None:
                style = widget.style()
                if horizontal < 0:
                    space_x = style.layoutSpacing(
                        QSizePolicy.ControlType.PushButton,
                        QSizePolicy.ControlType.PushButton,
                        Qt.Orientation.Horizontal,
                    )
                if vertical < 0:
                    space_y = style.layoutSpacing(
                        QSizePolicy.ControlType.PushButton,
                        QSizePolicy.ControlType.PushButton,
                        Qt.Orientation.Vertical,
                    )

            hint = item.sizeHint()
            next_x = x + hint.width() + space_x
            if next_x - space_x > effective.right() and line_height > 0:
                # Does not fit on this line: start another.
                x = effective.x()
                y = y + line_height + space_y
                next_x = x + hint.width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))

            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()
