# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Palettes as data files, and the stylesheet generated from whichever is active.

Not one colour is written down in this file, or in any other source file in the
project. The stylesheet below is a template full of named roles; a palette TOML
fills them in. That is what makes a palette something a person can add by
dropping a file into a directory, with nothing to register and nothing to
recompile.

Which palette is active comes from the desktop's own light-or-dark setting, read
from the Settings portal over D-Bus, and it changes live when that setting
changes. There is no theme picker, and KOKEN contains no knowledge of any
particular desktop shell - a shell that can write a palette file into the
palettes directory has themed this application, and nothing here needs to know
that shell exists.

Fusion is pinned before the first widget is built. Without that, this looks
like a Breeze application under KDE and a Fusion one under Niri, and the
stylesheet is written against exactly one of those.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Slot
from PySide6.QtDBus import QDBusVariant

from . import config

# org.freedesktop.appearance color-scheme, as the portal defines it.
SCHEME_NO_PREFERENCE = 0
SCHEME_DARK = 1
SCHEME_LIGHT = 2

PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_INTERFACE = "org.freedesktop.portal.Settings"
APPEARANCE_NAMESPACE = "org.freedesktop.appearance"
COLOR_SCHEME_KEY = "color-scheme"

ROLES = (
    "base",
    "surface",
    "overlay",
    "border",
    "text",
    "subtext",
    "muted",
    "accent",
    "success",
    "warning",
    "danger",
    "selection",
)

VARIANTS = ("light", "dark")
DEFAULT_VARIANT = "dark"

# Shipped filenames, used only as the per-role fallback source.
SHIPPED = {"light": "catppuccin-latte.toml", "dark": "catppuccin-mocha.toml"}


@dataclass
class Palette:
    name: str
    variant: str
    colors: dict[str, str] = field(default_factory=dict)
    source: Path | None = None
    shipped: bool = False

    def role(self, name: str) -> str:
        return self.colors.get(name, "")


def parse_palette(text: str, source: Path | None = None) -> Palette | None:
    """Parse one palette file. Anything malformed returns None, silently.

    A palette a user is midway through editing is a broken TOML file for a
    few seconds, and that is not a reason to refuse to start.
    """
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    variant = data.get("variant")
    if variant not in VARIANTS:
        return None
    colors = data.get("colors")
    if not isinstance(colors, dict):
        return None

    cleaned = {}
    for role in ROLES:
        value = colors.get(role)
        if isinstance(value, str) and _is_colour(value):
            cleaned[role] = value.strip().lower()
    if not cleaned:
        return None

    name = data.get("name")
    return Palette(
        name=name if isinstance(name, str) and name else (source.stem if source else "Unnamed"),
        variant=variant,
        colors=cleaned,
        source=source,
    )


def _is_colour(value: str) -> bool:
    """``#rrggbb`` or ``#rrggbbaa``. Nothing else is accepted."""
    text = value.strip()
    if not text.startswith("#"):
        return False
    body = text[1:]
    if len(body) not in (6, 8):
        return False
    try:
        int(body, 16)
    except ValueError:
        return False
    return True


def load_shipped() -> dict[str, Palette]:
    """The palettes inside the package, by variant. The per-role fallback."""
    out: dict[str, Palette] = {}
    for variant, filename in SHIPPED.items():
        payload = config.read_default(config.PALETTES_DIR_NAME, filename)
        if payload is None:
            continue
        try:
            parsed = parse_palette(payload.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        if parsed is not None:
            parsed.shipped = True
            out[parsed.variant] = parsed
    return out


def load_palettes() -> list[Palette]:
    """Every readable palette in the user's palettes directory.

    Every ``.toml`` in the directory is loaded. Adding a palette is dropping a
    file in; removing one is deleting it.
    """
    palettes: list[Palette] = []
    directory = config.palettes_dir()
    try:
        entries = sorted(directory.glob("*.toml"))
    except OSError:
        return palettes
    for path in entries:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        parsed = parse_palette(text, source=path)
        if parsed is not None:
            parsed.shipped = path.name in SHIPPED.values()
            palettes.append(parsed)
    return palettes


def choose(palettes: list[Palette], variant: str, shipped: dict[str, Palette]) -> Palette:
    """The first palette of *variant*, with a user's file beating a shipped one."""
    candidates = [p for p in palettes if p.variant == variant]
    for palette in candidates:
        if not palette.shipped:
            return palette
    if candidates:
        return candidates[0]
    fallback = shipped.get(variant)
    if fallback is not None:
        return fallback
    other = shipped.get(DEFAULT_VARIANT)
    if other is not None:
        return other
    # Nothing readable anywhere. An empty palette resolves every role through
    # the fallback chain below and still produces a usable sheet.
    return Palette(name="Empty", variant=variant)


def resolve(palette: Palette, shipped: dict[str, Palette]) -> dict[str, str]:
    """Fill in any role the chosen palette omits, from the shipped one.

    Per role, not per file: a palette that names eleven of the twelve is used
    for those eleven, and only the missing one is borrowed.
    """
    fallback = shipped.get(palette.variant) or shipped.get(DEFAULT_VARIANT)
    resolved = {}
    for role in ROLES:
        value = palette.role(role)
        if not value and fallback is not None:
            value = fallback.role(role)
        if not value:
            # Last resort, and only reachable when the package data itself is
            # missing: borrow another role that is present rather than write a
            # colour into this file.
            value = next(
                (palette.role(other) for other in ROLES if palette.role(other)), ""
            )
        resolved[role] = value
    return resolved


# --------------------------------------------------------------------------
# Colour arithmetic
# --------------------------------------------------------------------------


def _components(value: str) -> tuple[int, int, int]:
    body = value.lstrip("#")
    return int(body[0:2], 16), int(body[2:4], 16), int(body[4:6], 16)


def blend(first: str, second: str, amount: float = 0.5) -> str:
    """Mix two palette colours. Used for the alternating row background.

    CORE asks for the surface role at half its subtlety on odd rows, which is
    surface mixed halfway back towards base. Computing it keeps the result a
    function of the palette rather than a second colour to maintain.
    """
    try:
        red1, green1, blue1 = _components(first)
        red2, green2, blue2 = _components(second)
    except (ValueError, IndexError):
        return first
    mix = lambda a, b: max(0, min(255, round(a + (b - a) * amount)))  # noqa: E731
    return "#{:02x}{:02x}{:02x}".format(
        mix(red1, red2), mix(green1, green2), mix(blue1, blue2)
    )


# --------------------------------------------------------------------------
# The stylesheet
# --------------------------------------------------------------------------

# Every colour here is a placeholder. Nothing in this template is a literal,
# which is what STEP's check 23 is looking for.
STYLESHEET = """
QWidget {{
    background: transparent;
    color: {text};
}}

QMainWindow, #central {{
    background: {base};
}}

QToolTip {{
    background: {surface};
    color: {text};
    border: 1px solid {border};
}}

/* ---- tab rows ------------------------------------------------------ */

#tabRow1, #tabRow2, #tabRow3 {{
    background: transparent;
}}

#tab1, #tab2 {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 6px;
}}

#tab1:hover, #tab2:hover {{
    background: {overlay};
}}

#tab1[selected="true"], #tab2[selected="true"] {{
    background: {accent};
    border: 1px solid {accent};
}}

#tab3 {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
}}

#tab3:hover {{
    border-bottom: 2px solid {border};
}}

#tab3[selected="true"] {{
    border-bottom: 2px solid {accent};
}}

#tabText1 {{
    color: {text};
    font-size: {size_tab1}pt;
    font-weight: 600;
}}

#tabText2 {{
    color: {subtext};
    font-size: {size_tab2}pt;
}}

#tabText3 {{
    color: {muted};
    font-size: {size_tab3}pt;
}}

#tab1[selected="true"] #tabText1, #tab2[selected="true"] #tabText2 {{
    color: {on_accent};
}}

#tab3[selected="true"] #tabText3 {{
    color: {text};
}}

#tabGlyph {{
    color: {muted};
    font-size: {size_tab3}pt;
}}

#tab3[selected="true"] #tabGlyph {{
    color: {accent};
}}

#tab1[selected="true"] #tabGlyph, #tab2[selected="true"] #tabGlyph {{
    color: {on_accent};
}}

/* ---- content ------------------------------------------------------- */

#contentScroll, #content {{
    background: {base};
}}

#contentRow {{
    background: {base};
    border-bottom: 1px solid {border};
}}

#contentRow[odd="true"] {{
    background: {alternate};
}}

#contentRow:hover {{
    background: {hover};
}}

#rowHeader, #rowValueArea {{
    background: transparent;
}}

#rowLabel {{
    color: {subtext};
    font-size: {size_body}pt;
}}

#rowValue {{
    color: {text};
    font-size: {size_value}pt;
}}

#rowValue[severity="warning"], #rowSeverity[severity="warning"] {{
    color: {warning};
}}

#rowValue[severity="danger"], #rowSeverity[severity="danger"] {{
    color: {danger};
}}

#rowSeverity {{
    color: {muted};
    font-size: {size_value}pt;
}}

#rowChevron {{
    color: {muted};
    font-size: {size_body}pt;
}}

#copyButton {{
    background: transparent;
    border: none;
    color: {muted};
    font-size: {size_value}pt;
}}

#copyButton:hover {{
    color: {accent};
}}

#rowBody {{
    background: transparent;
    color: {subtext};
    font-size: {size_body_text}pt;
}}

#rowBody:selected {{
    background: {selection};
    color: {text};
}}

/* ---- the one control ----------------------------------------------- */

#mountButton {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 5px;
    color: {text};
    padding: 2px 10px;
    font-size: {size_body}pt;
}}

#mountButton:hover {{
    background: {overlay};
}}

#mountButton[armed="true"] {{
    background: {warning};
    border: 1px solid {warning};
    color: {on_warning};
}}

#mountButton:disabled {{
    color: {muted};
    border: 1px solid {border};
    background: {surface};
}}

/* CORE 12.4: a mount or unmount failure is shown as an expansion body under
   the row it came from, styled as a warning. */
#rowBody[severity="warning"] {{
    color: {warning};
    font-size: {size_body_text}pt;
}}

/* ---- footer -------------------------------------------------------- */

#footer {{
    background: {surface};
    border-top: 1px solid {border};
}}

#footerCaption {{
    color: {muted};
    font-size: {size_footer}pt;
}}

#footerPrivileged {{
    color: {subtext};
    font-size: {size_footer}pt;
}}

#footerPrivileged[severity="warning"] {{
    color: {warning};
}}

#segment {{
    background: {base};
    border: 1px solid {border};
    border-radius: 4px;
    color: {subtext};
    font-size: {size_footer}pt;
}}

#segment:hover {{
    background: {overlay};
}}

#segment[selected="true"] {{
    background: {accent};
    border: 1px solid {accent};
    color: {on_accent};
}}

/* ---- scrollbars ---------------------------------------------------- */

QScrollBar:vertical {{
    background: {base};
    width: 10px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background: {overlay};
    border-radius: 5px;
    min-height: 32px;
}}

QScrollBar::handle:vertical:hover {{
    background: {muted};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
"""


def _relative_luminance(value: str) -> float:
    try:
        red, green, blue = _components(value)
    except (ValueError, IndexError):
        return 0.0
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0


def _readable_on(background: str, colors: dict[str, str]) -> str:
    """Whichever of base and text stands furthest from *background*.

    Needed because accent and warning are palette colours, and the text sitting
    on them has to stay legible in both a light and a dark palette without
    either colour being written down here. Comparing luminance against a fixed
    threshold does not work: in a dark palette both the light accent and the
    light text are above any threshold you pick, and the result is light text
    on a light fill. Comparing the two candidates against the background
    instead always picks the one that can actually be read.
    """
    background_luminance = _relative_luminance(background)
    return max(
        (colors["base"], colors["text"]),
        key=lambda candidate: abs(
            _relative_luminance(candidate) - background_luminance
        ),
    )


def build_stylesheet(colors: dict[str, str], base_point_size: float) -> str:
    """Fill the template. The only place a palette becomes a stylesheet."""
    size = base_point_size if base_point_size > 0 else 10.0
    values = dict(colors)
    values.update(
        {
            # CORE 11: the alternating background is the surface role at half
            # its subtlety, which is surface mixed halfway back toward base.
            "alternate": blend(colors["surface"], colors["base"], 0.5),
            "hover": blend(colors["surface"], colors["overlay"], 0.35),
            "on_accent": _readable_on(colors["accent"], colors),
            "on_warning": _readable_on(colors["warning"], colors),
            # CORE 13.4, as point sizes relative to the system font.
            "size_tab1": round(size * 1.15, 1),
            "size_tab2": round(size * 1.00, 1),
            "size_tab3": round(size * 0.90, 1),
            "size_body": round(size * 1.00, 1),
            "size_value": round(size * 0.95, 1),
            "size_body_text": round(size * 0.95, 1),
            "size_footer": round(size * 0.90, 1),
        }
    )
    return STYLESHEET.format(**values)


# --------------------------------------------------------------------------
# The portal
# --------------------------------------------------------------------------


class Theme:
    """Chooses the palette, builds the sheet, and keeps both current."""

    def __init__(self, application=None) -> None:
        self.application = application
        self.shipped = load_shipped()
        self.palettes: list[Palette] = []
        self.variant = DEFAULT_VARIANT
        self.palette: Palette | None = None
        self._watch = None

    # -- palette selection ------------------------------------------------

    def reload(self) -> None:
        """Re-read the palettes directory. Called at startup and on a change."""
        self.palettes = load_palettes()

    def apply(self) -> None:
        """Choose, build and install the stylesheet."""
        if not self.palettes:
            self.reload()
        self.variant = read_colour_scheme()
        self.palette = choose(self.palettes, self.variant, self.shipped)
        colors = resolve(self.palette, self.shipped)
        if self.application is None:
            return
        base_size = self.application.font().pointSizeF()
        self.application.setStyleSheet(build_stylesheet(colors, base_size))

    def describe(self) -> str:
        if self.palette is None:
            return "No palette loaded"
        return f"{self.palette.name} ({self.palette.variant})"

    # -- live updates -----------------------------------------------------

    def watch(self) -> bool:
        """Subscribe to the portal's change signal. False if there is none.

        Every failure here is swallowed. Live theme switching is a convenience,
        and an application that refuses to start because an optional D-Bus
        subscription could not be set up would be trading something essential
        for something pleasant.
        """
        try:
            from PySide6.QtCore import SLOT
            from PySide6.QtDBus import QDBusConnection

            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                return False
            watcher = _PortalWatcher(self)
            # The receiver must be a QObject and the slot must be given in the
            # SLOT() form with its full signature. A plain object, or the bare
            # method name as bytes, is rejected at the binding layer.
            connected = bus.connect(
                PORTAL_SERVICE,
                PORTAL_PATH,
                PORTAL_INTERFACE,
                "SettingChanged",
                watcher,
                SLOT("onSettingChanged(QString,QString,QDBusVariant)"),
            )
        except Exception:
            return False
        if connected:
            # Held on the Theme so it outlives this call; the bus keeps only a
            # borrowed pointer to the receiver.
            self._watch = watcher
        return bool(connected)

    def unwatch(self) -> None:
        """Disconnect the portal watcher. Called once, on the way out.

        Same reasoning as Application._shutdown: a live D-Bus connection into a
        Python slot is one more thing for Qt to unwind after Python has stopped
        being able to answer.
        """
        watcher = self._watch
        self._watch = None
        if watcher is None:
            return
        try:
            from PySide6.QtCore import SLOT
            from PySide6.QtDBus import QDBusConnection

            QDBusConnection.sessionBus().disconnect(
                PORTAL_SERVICE,
                PORTAL_PATH,
                PORTAL_INTERFACE,
                "SettingChanged",
                watcher,
                SLOT("onSettingChanged(QString,QString,QDBusVariant)"),
            )
        except Exception:
            pass


class _PortalWatcher(QObject):
    """Receives SettingChanged and re-applies when the colour scheme moves."""

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self._theme = theme

    @Slot(str, str, QDBusVariant)
    def onSettingChanged(self, namespace, key, value):  # noqa: N802 - D-Bus slot
        if str(namespace) != APPEARANCE_NAMESPACE or str(key) != COLOR_SCHEME_KEY:
            return
        try:
            # Re-read the directory as well: a shell that rewrites the palette
            # file when the scheme changes has done so by now.
            self._theme.reload()
            self._theme.apply()
        except Exception:
            # A signal handler that raises crosses back into Qt's C++ stack.
            return


def read_colour_scheme() -> str:
    """``light`` or ``dark``, from the portal. Dark when it cannot be asked."""
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
    except ImportError:
        return DEFAULT_VARIANT

    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return DEFAULT_VARIANT
        interface = QDBusInterface(PORTAL_SERVICE, PORTAL_PATH, PORTAL_INTERFACE, bus)
        if not interface.isValid():
            return DEFAULT_VARIANT
        # ReadOne is the current call; Read is the older one and is still what
        # some portal builds answer.
        for method in ("ReadOne", "Read"):
            message = interface.call(method, APPEARANCE_NAMESPACE, COLOR_SCHEME_KEY)
            arguments = message.arguments()
            if not arguments:
                continue
            value = _unwrap_variant(arguments[0])
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                if value == SCHEME_DARK:
                    return "dark"
                if value == SCHEME_LIGHT:
                    return "light"
                return DEFAULT_VARIANT
    except Exception:
        return DEFAULT_VARIANT
    return DEFAULT_VARIANT


def _unwrap_variant(value, depth: int = 0):
    """The portal wraps the answer in a variant, sometimes twice."""
    if depth > 4:
        return value
    try:
        from PySide6.QtDBus import QDBusVariant

        if isinstance(value, QDBusVariant):
            return _unwrap_variant(value.variant(), depth + 1)
    except ImportError:
        pass
    inner = getattr(value, "variant", None)
    if callable(inner):
        return _unwrap_variant(inner(), depth + 1)
    return value
