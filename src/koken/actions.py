# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""The only code in KOKEN that writes to the system.

Two methods on one D-Bus interface, with default options, and nothing else.
This file and :mod:`koken.ui.mountrow` are kept small and separate so that the
whole of what this application can do to a machine can be read in one sitting.

``force`` is never passed. A forced unmount on a filesystem with dirty pages
loses data. If udisks2 says the device is busy then that answer stands, is
reported in plain language, and is not retried harder.

Object paths are resolved at the moment the button is pressed, never at
enumeration time. udisks2 hands out paths like
``/org/freedesktop/UDisks2/block_devices/sdb1`` and reuses them: unplug a stick,
plug a different one into the same port, and a path captured a minute ago now
addresses somebody else's disk. Resolving from the device node at press time
costs one round trip and removes the entire class of problem.

The calls are asynchronous. Mounting a large filesystem takes seconds, and a
blocked interface during those seconds would look like a crash.
"""

from __future__ import annotations

from .probes.base import read_lines
from .probes.disks import IFACE_FILESYSTEM, SERVICE, _decode_bytes, client
from .probes.volumes import parse_mounts

MOUNT = "Mount"
UNMOUNT = "Unmount"

# CORE 12.4, verbatim. Nothing outside this table gets a friendlier wording:
# an unrecognised failure is shown raw, because inventing a reassuring
# sentence for a fault nobody has seen before is how people lose data.
ERROR_MESSAGES = {
    "org.freedesktop.UDisks2.Error.DeviceBusy": (
        "Something is still using this filesystem. Close any program with "
        "files open on it, then try again."
    ),
    "org.freedesktop.UDisks2.Error.AlreadyMounted": (
        "Already mounted — the view was stale, and has now been refreshed."
    ),
    "org.freedesktop.UDisks2.Error.NotMounted": (
        "Not mounted — the view was stale, and has now been refreshed."
    ),
}

# Every NotAuthorized variant udisks2 can raise - plain, CanObtain, Dismissed -
# means the same thing to the person at the keyboard.
NOT_AUTHORIZED_PREFIX = "org.freedesktop.UDisks2.Error.NotAuthorized"
NOT_AUTHORIZED_MESSAGE = "Authentication was declined or failed."

# Failed carries a useful message from udisks2 itself, so it is passed through
# rather than replaced.
FAILED = "org.freedesktop.UDisks2.Error.Failed"

# The two errors that mean the view was wrong rather than the action was.
STALE_ERRORS = (
    "org.freedesktop.UDisks2.Error.AlreadyMounted",
    "org.freedesktop.UDisks2.Error.NotMounted",
)


def translate_error(name: str, message: str) -> str:
    """A D-Bus error as a sentence. CORE 12.4's table and nothing beyond it."""
    if not name:
        return message or "The action failed, and udisks2 gave no reason."
    if name.startswith(NOT_AUTHORIZED_PREFIX):
        return NOT_AUTHORIZED_MESSAGE
    known = ERROR_MESSAGES.get(name)
    if known:
        return known
    if name == FAILED:
        return message or name
    return f"{name}: {message}" if message else name


class Result:
    """What came back from one call."""

    def __init__(
        self,
        ok: bool,
        message: str = "",
        error_name: str = "",
        mount_point: str = "",
    ) -> None:
        self.ok = ok
        self.message = message
        self.error_name = error_name
        self.mount_point = mount_point

    @property
    def was_stale(self) -> bool:
        """The action was unnecessary because the view was out of date."""
        return self.error_name in STALE_ERRORS


class DBusCaller:
    """Makes the real asynchronous call. The only part that touches the bus.

    *bus* is the system bus unless one is handed in, which is how the two
    calls this application is allowed to make can be exercised against a
    service on a session bus.
    """

    def __init__(self, bus=None) -> None:
        self._bus = bus

    def call(self, object_path: str, method: str, on_done) -> bool:
        """Invoke *method* with empty options. ``on_done(Result)`` when it lands.

        Returns False when the call could not even be started, in which case
        ``on_done`` is not invoked and the caller reports the failure itself.
        """
        try:
            from PySide6.QtDBus import (
                QDBusConnection,
                QDBusMessage,
                QDBusPendingCallWatcher,
            )
        except ImportError:
            return False

        bus = self._bus if self._bus is not None else QDBusConnection.systemBus()
        if not bus.isConnected():
            return False

        message = QDBusMessage.createMethodCall(
            SERVICE, object_path, IFACE_FILESYSTEM, method
        )
        # The options dictionary, empty. Never a `force` key, for either method.
        message.setArguments([{}])

        try:
            pending = bus.asyncCall(message)
        except Exception:
            return False

        watcher = QDBusPendingCallWatcher(pending)
        # Kept alive by the closure until the reply lands.
        self._watcher = watcher

        def finished(_watcher=watcher):
            on_done(self._read(_watcher, method))
            _watcher.deleteLater()

        watcher.finished.connect(finished)
        return True

    @staticmethod
    def _read(watcher, method: str) -> Result:
        from PySide6.QtDBus import QDBusMessage

        reply = watcher.reply()
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            name = reply.errorName() or ""
            detail = reply.errorMessage() or ""
            return Result(False, translate_error(name, detail), error_name=name)

        mount_point = ""
        if method == MOUNT:
            arguments = reply.arguments()
            if arguments and isinstance(arguments[0], str):
                mount_point = arguments[0]
        return Result(True, mount_point=mount_point)


class MountActions:
    """Mount and unmount, and nothing else this application is allowed to do.

    The caller is injectable so that this layer can be exercised without a
    system bus: hand it something with the same ``call`` signature and the
    two-step guard, the error translation and the refresh all run exactly as
    they do against udisks2.
    """

    def __init__(self, caller=None) -> None:
        self.caller = caller if caller is not None else DBusCaller()

    def resolve_object_path(self, device_node: str) -> str | None:
        """Ask udisks2 which object is this device node, right now."""
        udisks = client()
        if not udisks.available:
            return None
        return udisks.block_for_device(device_node)

    def current_mount_point(self, device_node: str) -> str | None:
        """Where the device is mounted at this moment, or None.

        udisks2 is asked first, and answers with an ``aay`` that this Qt
        binding cannot demarshal, so in practice the answer comes from
        ``/proc/mounts`` - which is where udisks2 reads it from as well.
        """
        udisks = client()
        if udisks.available:
            path = udisks.block_for_device(device_node)
            if path is not None:
                filesystem = udisks.properties(path, IFACE_FILESYSTEM, refresh=True)
                points = filesystem.get("MountPoints")
                if isinstance(points, list):
                    for item in points:
                        text = _decode_bytes(item)
                        if text:
                            return text
        for entry in parse_mounts(read_lines("/proc/mounts")):
            if entry["source"] == device_node:
                return entry["target"]
        return None

    def perform(self, device_node: str, method: str, on_done) -> None:
        """Run *method* against whatever object holds *device_node* now."""
        udisks = client()
        if not udisks.available:
            # The client's own reason, which names what failed. A flat "udisks2
            # is not available" here once sat under a row that was showing a
            # live mount point read from /proc/mounts, which reads as the
            # button being broken rather than as udisks2 being unreachable.
            on_done(Result(False, udisks.full_reason))
            return

        object_path = self.resolve_object_path(device_node)
        if object_path is None:
            on_done(
                Result(
                    False,
                    f"udisks2 is running but has no object for {device_node}, so "
                    "there is nothing to send this to. The device may have been "
                    "removed since this view was built.",
                )
            )
            return

        started = self.caller.call(object_path, method, on_done)
        if not started:
            on_done(
                Result(
                    False,
                    "The request could not be sent to udisks2 over the system bus.",
                )
            )

    def mount(self, device_node: str, on_done) -> None:
        self.perform(device_node, MOUNT, on_done)

    def unmount(self, device_node: str, on_done) -> None:
        self.perform(device_node, UNMOUNT, on_done)
