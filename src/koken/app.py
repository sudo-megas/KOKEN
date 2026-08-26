# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Startup, in the order the pieces need to happen.

Fusion is pinned before any widget exists, because a style set afterwards
leaves already-built widgets on the old one. The privileged helper runs before
the window is shown, so the polkit prompt is the first and only thing between
launching and the interface - it is not a modal on top of a half-drawn window,
and there is no second prompt later. Static data is gathered next, the window
is shown with it already filled in, and only then does the timer start.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject, QTime, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from . import config, explain, icons, privileged, theme
from .actions import MountActions
from .probes import BRANCHES, Context, build
from .probes import disks as disks_module
from .probes import hwids
from .ui.mountrow import make_factory
from .ui.window import Window

APPLICATION_NAME = "koken"
# Must match the installed koken.desktop basename, or a Wayland
# compositor cannot match the window to its launcher entry.
DESKTOP_FILE = "koken"


class Application(QObject):
    """Everything that is not a widget and not a probe."""

    def __init__(self, argv: list[str] | None = None) -> None:
        super().__init__()
        self.qt = QApplication(argv if argv is not None else sys.argv)
        # Before the first widget. CORE 13.1.
        self.qt.setStyle("Fusion")
        self.qt.setApplicationName(APPLICATION_NAME)
        self.qt.setApplicationDisplayName("KÖKEN")
        self.qt.setDesktopFileName(DESKTOP_FILE)
        self.qt.setWindowIcon(QIcon.fromTheme(APPLICATION_NAME))

        self.settings = config.Settings()
        self.theme = theme.Theme(self.qt)
        self.explanations = explain.Explanations()
        self.context = Context()
        self.probes: dict[str, list] = {}
        self.privileged = privileged.PrivilegedData(refused=True)
        self.window: Window | None = None

        self.timer = QTimer(self)
        self.timer.setSingleShot(False)
        self.timer.timeout.connect(self._tick)

    # -- startup -----------------------------------------------------------

    def start(self) -> int:
        config.ensure_config_tree()
        self.settings = config.load_settings()
        self.explanations = explain.load()

        icons.load()
        self.theme.reload()
        self.theme.apply()
        self.theme.watch()

        # 1. The privileged read, once, before there is a window to obscure.
        self.privileged = privileged.run()

        # 2. Static data.
        self.context = Context(
            pci_ids=hwids.load_pci_ids(),
            usb_ids=hwids.load_usb_ids(),
            privileged=self.privileged,
        )
        self.probes = build(self.context)

        # 3. The window, built with the data already in it.
        self.window = Window(explanations=self.explanations)
        self.window.set_mount_row_factory(
            make_factory(MountActions(), self._storage_changed)
        )
        self.window.refresh_requested.connect(self.reenumerate)
        self.window.quit_requested.connect(self.quit)
        self.window.interval_changed.connect(self._set_interval)
        self.window.branch_changed.connect(self._branch_changed)
        self.window.apply_interval(self.settings.refresh_interval)
        self.window.set_privileged_status(
            self.privileged.status_text, self.privileged.available
        )

        self.reenumerate(restore_branch=self.settings.last_branch)
        self.window.installEventFilter(self)
        self.window.show()

        # 4. Only now the timer.
        self._set_interval(self.settings.refresh_interval)

        self.qt.aboutToQuit.connect(self._save)
        self.qt.aboutToQuit.connect(self._shutdown)
        return self.qt.exec()

    def _shutdown(self) -> None:
        """Take the window down while there is still a Python to take it down with.

        PySide destroys any QApplication that is still standing during Python's
        own finalisation, and Qt sends a close event to every widget it destroys
        on the way. That event goes through this object's event filter - into an
        interpreter that has already been shut down - and the process dies with a
        segmentation fault after doing everything correctly. Nothing is visibly
        wrong, which is exactly what makes it worth removing: an exit status of
        139 is what a crash reporter files a bug about.

        Closing the window here, with the event loop still running and the filter
        removed first, means there is nothing left for that pass to destroy.
        """
        window = self.window
        self.window = None
        if window is not None:
            window.removeEventFilter(self)
            window.hide()
            window.setParent(None)
            window.deleteLater()
        self.theme.unwatch()
        self.timer.stop()

    # -- enumeration -------------------------------------------------------

    def reenumerate(self, restore_branch: str | None = None) -> None:
        """The static pass. Launch, and F5, and nothing else."""
        if self.window is None:
            return
        # Hardware may have appeared or disappeared since the last pass, so the
        # udisks2 snapshot is dropped rather than reused.
        disks_module.invalidate()
        enumeration = {
            branch_id: [(probe, probe.safe_sections()) for probe in probes]
            for branch_id, probes in self.probes.items()
        }
        self.window.set_enumeration(enumeration, restore_branch=restore_branch)
        self._stamp()

    def _storage_changed(self) -> None:
        """Re-run the storage enumeration only, after a mount or unmount.

        CORE 12.4: not the whole static pass, and the row 3 selection is kept.
        """
        if self.window is None:
            return
        disks_module.invalidate()
        entries = [
            (probe, probe.safe_sections()) for probe in self.probes.get("storage", [])
        ]
        self.window.replace_branch("storage", entries)
        self._stamp()

    # -- the timer ---------------------------------------------------------

    def _set_interval(self, seconds: int) -> None:
        self.settings.refresh_interval = seconds
        if seconds <= 0:
            self.timer.stop()
            return
        self.timer.setInterval(seconds * 1000)
        if self._window_visible():
            self.timer.start()

    def _tick(self) -> None:
        if self.window is None or not self._window_visible():
            return
        self.window.sample_visible()
        self._stamp()

    def _stamp(self) -> None:
        if self.window is not None:
            self.window.set_last_refresh(
                "Last read " + QTime.currentTime().toString("HH:mm:ss")
            )

    def _window_visible(self) -> bool:
        return (
            self.window is not None
            and self.window.isVisible()
            and not self.window.isMinimized()
        )

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        """Stop the timer when the window is not on screen.

        Reading sysfs every second for a minimised window is waste, and it is
        waste on a laptop battery in particular.
        """
        kind = event.type()
        if kind in (
            QEvent.Type.WindowStateChange,
            QEvent.Type.Show,
            QEvent.Type.Hide,
        ):
            if self._window_visible():
                if self.settings.refresh_interval > 0 and not self.timer.isActive():
                    self.timer.start()
            else:
                self.timer.stop()
        return False

    # -- shutdown ----------------------------------------------------------

    def _branch_changed(self, branch_id: str) -> None:
        self.settings.last_branch = branch_id

    def _save(self) -> None:
        config.save_settings(self.settings)

    def quit(self) -> None:
        self.qt.quit()


def run(argv: list[str] | None = None) -> int:
    return Application(argv).start()
