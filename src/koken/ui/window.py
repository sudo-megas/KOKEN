# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The window: three tab rows, a list of rows, and a footer. One window, always.

Two rules shape everything here.

Selection is remembered per branch. Leaving Hardware on ``CPU -> Clocks`` and
coming back to it later lands on ``CPU -> Clocks``, not on the default. That is
one dictionary, kept for the session, with the row 1 choice persisted to
config.

The timer never rebuilds. Crossing a tab rebuilds rows 2 and 3 and the content
list, because the content genuinely changed. A refresh tick does not: it calls
``set_value`` on rows it already holds, keyed by row id. Rebuilding on a tick
would collapse every open explanation and throw the scroll position back to the
top twice a second, which is the single most obvious way an application like
this can be unpleasant to use.

Only the section on screen is sampled. Reading every network counter and every
PCI power state on the interval, for sections nobody is looking at, is the same
waste as leaving the timer running for a minimised window - and crossing to a
section samples it at once, so what is on screen is never stale.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QMainWindow,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..probes import BRANCHES
from ..probes.base import Section
from .footer import Footer
from .row import LABEL_SHARE, ContentRow
from .tabrow import TabRow

DEFAULT_SIZE = (1100, 760)
MINIMUM_SIZE = (720, 520)
TAB_ROW_GAP = 12


class Window(QMainWindow):
    """The one window."""

    refresh_requested = Signal()
    quit_requested = Signal()
    interval_changed = Signal(int)
    branch_changed = Signal(str)

    def __init__(self, explanations=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KÖKEN")
        self.resize(*DEFAULT_SIZE)
        self.setMinimumSize(*MINIMUM_SIZE)

        self._explanations = explanations
        # {branch id: [(probe, [Section, ...]), ...]}
        self._enumeration: dict[str, list] = {}
        # {branch id: (probe id, section id)} - the whole of "remembered per branch".
        self._selection: dict[str, tuple[str, str]] = {}
        # Every content row currently on screen, in order. A list rather than
        # a dictionary because a row key may legitimately repeat within a
        # section, and a dictionary would silently drop all but the last -
        # leaving an orphan in the layout that is never resized and never
        # sampled.
        self._row_widgets: list[ContentRow] = []
        self._current_branch: str | None = None
        self._mount_row_factory = None

        central = QWidget(self)
        central.setObjectName("central")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 16, 16, 0)
        outer.setSpacing(TAB_ROW_GAP)

        self.row1 = TabRow(1)
        self.row2 = TabRow(2)
        self.row3 = TabRow(3)
        outer.addWidget(self.row1)
        outer.addWidget(self.row2)
        outer.addWidget(self.row3)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("contentScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.content = QWidget()
        self.content.setObjectName("content")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        self.content_layout.addStretch(1)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)

        self.footer = Footer()
        outer.addWidget(self.footer)

        self.setCentralWidget(central)

        self.row1.selected.connect(self._on_branch_selected)
        self.row2.selected.connect(self._on_probe_selected)
        self.row3.selected.connect(self._on_section_selected)
        self.footer.interval_changed.connect(self.interval_changed)

        self._install_shortcuts()

        self.row1.set_entries(
            [(branch_id, label, None) for branch_id, label, _probes in BRANCHES]
        )

    # -- keyboard ----------------------------------------------------------

    def _install_shortcuts(self) -> None:
        for index, (branch_id, _label, _probes) in enumerate(BRANCHES, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(
                lambda name=branch_id: self.row1.set_current(name, notify=True)
            )

        for sequence, delta in (("Left", -1), ("Right", 1)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(lambda step=delta: self.row3.move(step))

        refresh = QShortcut(QKeySequence("F5"), self)
        refresh.activated.connect(self.refresh_requested)

        quit_shortcut = QShortcut(QKeySequence("Ctrl+Q"), self)
        quit_shortcut.activated.connect(self.quit_requested)

    def event(self, event) -> bool:
        """Take Tab and Shift+Tab before Qt turns them into focus moves.

        A shortcut cannot have them: focus navigation consumes both before any
        shortcut is considered. Intercepting here is the documented way to
        claim them, and it is worth claiming them because CORE assigns them to
        row 2 and nothing in this window wants keyboard focus.
        """
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            # Plain Tab and Shift+Tab only. Ctrl+Tab and friends belong to
            # whatever the desktop has bound them to, not to row 2.
            modifiers = event.modifiers() & ~Qt.KeyboardModifier.ShiftModifier
            if modifiers == Qt.KeyboardModifier.NoModifier:
                if key == Qt.Key.Key_Tab:
                    self.row2.move(1)
                    return True
                if key == Qt.Key.Key_Backtab:
                    self.row2.move(-1)
                    return True
        return super().event(event)

    # -- data --------------------------------------------------------------

    def set_mount_row_factory(self, factory) -> None:
        """Install the builder for the one interactive row. See actions.py."""
        self._mount_row_factory = factory

    def set_explanations(self, explanations) -> None:
        self._explanations = explanations

    def set_enumeration(self, enumeration: dict, restore_branch: str | None = None) -> None:
        """Take a fresh enumeration and rebuild what is on screen.

        Called at launch and on F5, and by the action layer for the Storage
        branch alone after a successful mount or unmount.
        """
        self._enumeration = enumeration
        branch = restore_branch or self._current_branch or self.row1.ids()[0]
        if branch not in self._enumeration:
            branch = self.row1.ids()[0]
        self._current_branch = None
        self.row1.set_current(branch, notify=False)
        self._show_branch(branch)

    def replace_branch(self, branch_id: str, entries: list) -> None:
        """Swap one branch's sections, keeping the row 3 selection.

        This is what a successful mount or unmount uses: the Volumes rows are
        rebuilt from a fresh storage enumeration, and the tab the user was
        looking at stays selected.
        """
        self._enumeration[branch_id] = entries
        if branch_id == self._current_branch:
            self._show_branch(branch_id, keep_selection=True)

    # -- the cascade -------------------------------------------------------

    def _on_branch_selected(self, branch_id: str) -> None:
        self._show_branch(branch_id)
        self.branch_changed.emit(branch_id)

    def _show_branch(self, branch_id: str, keep_selection: bool = True) -> None:
        self._current_branch = branch_id
        entries = self._enumeration.get(branch_id, [])
        remembered = self._selection.get(branch_id)
        wanted_probe = remembered[0] if (remembered and keep_selection) else None

        self.row2.blockSignals(True)
        self.row2.set_entries(
            [(probe.id, probe.label, None) for probe, _sections in entries],
            current=wanted_probe,
        )
        self.row2.blockSignals(False)

        current = self.row2.current()
        if current is not None:
            self._show_probe(current)
        else:
            self.row3.set_entries([])
            self._build_rows([])

    def _on_probe_selected(self, probe_id: str) -> None:
        self._show_probe(probe_id)

    def _show_probe(self, probe_id: str) -> None:
        branch_id = self._current_branch
        if branch_id is None:
            return
        sections = self._sections_for(branch_id, probe_id)
        remembered = self._selection.get(branch_id)
        wanted_section = (
            remembered[1] if remembered and remembered[0] == probe_id else None
        )

        self.row3.blockSignals(True)
        self.row3.set_entries(
            [(section.id, section.label, section.icon) for section in sections],
            current=wanted_section,
        )
        self.row3.blockSignals(False)

        current = self.row3.current()
        if current is not None:
            self._show_section(current)
        else:
            self._remember(probe_id, "")
            self._build_rows([])

    def _on_section_selected(self, section_id: str) -> None:
        self._show_section(section_id)

    def _show_section(self, section_id: str) -> None:
        branch_id = self._current_branch
        probe_id = self.row2.current()
        if branch_id is None or probe_id is None:
            return
        self._remember(probe_id, section_id)
        section = self._find_section(branch_id, probe_id, section_id)
        self._build_rows(section.rows if section else [], section=section)
        self.sample_visible()

    def _remember(self, probe_id: str, section_id: str) -> None:
        if self._current_branch is not None:
            self._selection[self._current_branch] = (probe_id, section_id)

    def _sections_for(self, branch_id: str, probe_id: str) -> list[Section]:
        for probe, sections in self._enumeration.get(branch_id, []):
            if probe.id == probe_id:
                return sections
        return []

    def _find_section(self, branch_id, probe_id, section_id) -> Section | None:
        for section in self._sections_for(branch_id, probe_id):
            if section.id == section_id:
                return section
        return None

    def current_probe(self):
        branch_id = self._current_branch
        probe_id = self.row2.current()
        if branch_id is None or probe_id is None:
            return None
        for probe, _sections in self._enumeration.get(branch_id, []):
            if probe.id == probe_id:
                return probe
        return None

    def current_section_id(self) -> str | None:
        return self.row3.current()

    def current_branch(self) -> str | None:
        return self._current_branch

    # -- content -----------------------------------------------------------

    def _build_rows(self, rows, section: Section | None = None) -> None:
        """Replace the content list. Only ever called on a selection change."""
        while self.content_layout.count() > 1:
            item = self.content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._row_widgets = []

        label_width = self._label_width()

        # The mount state row is the first row of a Volumes instance and is the
        # only interactive thing in the application. It is built by the action
        # layer, not here, so that everything that can write to the system
        # stays in one place.
        if (
            section is not None
            and self._mount_row_factory is not None
            and self._current_branch == "storage"
            and self.row2.current() == "volumes"
            and rows
        ):
            first, rest = rows[0], rows[1:]
            widget = self._mount_row_factory(section, first, odd=False)
            if widget is not None:
                widget.set_label_width(label_width)
                widget.toggled_expansion.connect(self._apply_label_widths)
                self.content_layout.insertWidget(self.content_layout.count() - 1, widget)
                self._row_widgets.append(widget)
                rows = rest
                offset = 1
            else:
                offset = 0
        else:
            offset = 0

        for index, row in enumerate(rows):
            body = self._body_for(row)
            value = self._glossed(row)
            widget = ContentRow(
                row_id=row.id,
                label=row.label,
                value=value,
                severity=row.severity,
                raw_value=row.value,
                body=body,
                odd=bool((index + offset) % 2),
                row_key=row.key,
            )
            widget.set_label_width(label_width)
            # Expanding a row can bring the scrollbar in, which narrows the
            # viewport - and the label column is a share of that width.
            widget.toggled_expansion.connect(self._apply_label_widths)
            self.content_layout.insertWidget(self.content_layout.count() - 1, widget)
            self._row_widgets.append(widget)

        self.scroll.verticalScrollBar().setValue(0)

    def _glossed(self, row) -> str:
        """``16`` becomes ``16 — SMT enabled``.

        The gloss is for reading and the value is for pasting, so the two are
        kept apart right up to this point: the row widget is handed the joined
        text to display and the bare value to copy.
        """
        gloss = getattr(row, "gloss", "")
        if not gloss and self._explanations is not None:
            gloss = self._explanations.short(row.id) or ""
        return f"{row.value} — {gloss}" if gloss else row.value

    def _body_for(self, row) -> str:
        if getattr(row, "body", ""):
            return row.body
        if self._explanations is None:
            return ""
        return self._explanations.long(row.id) or ""

    def _label_width(self) -> int:
        width = self.scroll.viewport().width() or self.width()
        return int(width * LABEL_SHARE)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply_label_widths()

    def content_rows(self) -> list[ContentRow]:
        """Every content row currently on screen, in the order shown."""
        return list(self._row_widgets)

    def row_for(self, key: str) -> ContentRow | None:
        """The first row carrying *key*, or None."""
        for widget in self._row_widgets:
            if widget.row_key == key:
                return widget
        return None

    def _apply_label_widths(self) -> None:
        width = self._label_width()
        for widget in self._row_widgets:
            widget.set_label_width(width)

    # -- the volatile pass -------------------------------------------------

    def sample_visible(self) -> None:
        """Update the rows on screen in place. Never rebuilds anything."""
        probe = self.current_probe()
        section_id = self.current_section_id()
        if probe is None or section_id is None or not self._row_widgets:
            return
        sample = probe.safe_sample().get(section_id, [])
        if not sample:
            return
        by_key: dict[str, list[ContentRow]] = {}
        for widget in self._row_widgets:
            by_key.setdefault(widget.row_key, []).append(widget)
        for row in sample:
            for widget in by_key.get(row.key, ()):
                widget.set_value(
                    self._glossed(row), row.severity, raw_value=row.value
                )

    def apply_interval(self, seconds: int) -> None:
        self.footer.set_interval(seconds)

    def set_privileged_status(self, text: str, granted: bool) -> None:
        self.footer.set_privileged_status(text, granted)

    def set_last_refresh(self, text: str) -> None:
        self.footer.set_last_refresh(text)
