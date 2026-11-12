"""Railway identity colors shared by the window chrome and the dial painter.

The hex values match the web and Apple clients so every platform reads as
the same product. Qt palette roles still drive everything that must follow
the OS theme; these constants only paint the brand surfaces (header band,
navigation, primary action, departure board, progress arc).
"""

from PySide6.QtGui import QColor, QPalette

PLATFORM = "#142c5c"
PLATFORM_DEEP = "#0c1b39"
SIGNAL = "#ff604f"
# Coral for light palette surfaces, where SIGNAL stays below 3:1 (2.6:1 on
# a light window). 4.8:1 on a light window, 5.5:1 on a white base, 3.7:1
# against TICKET and 3.2:1 against TRACK.
SIGNAL_DEEP = "#c23324"
TICKET = "#f5d05b"
PORCELAIN = "#f7f8f2"
TRACK = "#111923"
STEEL = "#8fa8b8"
DANGER = "#c33d38"
# WCAG 1.4.11 minimum for graphical objects next to each other.
NON_TEXT_CONTRAST = 3.0
# WCAG 1.4.3 minimum for normal-size text.
TEXT_CONTRAST = 4.5
_TRACK_STEPS = 10
_DANGER_STEPS = 20


def signal_accent(palette: QPalette) -> str:
    """The coral with the stronger contrast against the palette's window.

    Focus rings and coral accents sit on palette surfaces, so a light OS
    theme gets SIGNAL_DEEP and a dark one keeps SIGNAL.
    """
    window = palette.color(QPalette.ColorRole.Window)
    return max(
        (SIGNAL, SIGNAL_DEEP), key=lambda coral: contrast_ratio(QColor(coral), window)
    )


def danger_text(palette: QPalette) -> str:
    """The danger red for a label on the palette's button, at least 4.5:1.

    DANGER reads at 4.5:1 on a light button but at 2.1:1 on a dark one. The
    red moves toward the button text colour in twentieths until it clears
    the WCAG text limit, so it stays as red as the palette allows. The last
    step is the button text colour itself.
    """
    button = palette.color(QPalette.ColorRole.Button)
    text = palette.color(QPalette.ColorRole.ButtonText)
    danger = QColor(DANGER)
    for step in range(_DANGER_STEPS + 1):
        candidate = _blend(danger, text, step / _DANGER_STEPS)
        if contrast_ratio(candidate, button) >= TEXT_CONTRAST:
            return candidate.name()
    return text.name()


def progress_track(palette: QPalette, arc: QColor) -> QColor:
    """The dial's track ring, kept at least 3:1 from the progress arc on it.

    The palette Mid tone sits too close to the coral arc (2.8:1 in a light
    palette, 2.3:1 in a dark one), so the ring is lightened toward the dial
    face in tenths until the arc clears the WCAG non-text limit. The arc and
    the focus accent keep their coral; only the unfilled ring gives way.
    """
    mid = palette.color(QPalette.ColorRole.Mid)
    face = palette.color(QPalette.ColorRole.AlternateBase)
    for step in range(_TRACK_STEPS + 1):
        track = _blend(mid, face, step / _TRACK_STEPS)
        if contrast_ratio(arc, track) >= NON_TEXT_CONTRAST:
            return track
    return face


def contrast_ratio(first: QColor, second: QColor) -> float:
    """WCAG contrast ratio of two opaque colors."""
    darker, lighter = sorted(
        (_relative_luminance(first), _relative_luminance(second))
    )
    return (lighter + 0.05) / (darker + 0.05)


def _blend(start: QColor, end: QColor, share: float) -> QColor:
    return QColor.fromRgbF(
        start.redF() + (end.redF() - start.redF()) * share,
        start.greenF() + (end.greenF() - start.greenF()) * share,
        start.blueF() + (end.blueF() - start.blueF()) * share,
    )


def _relative_luminance(color: QColor) -> float:
    """WCAG relative luminance of an sRGB color."""

    def linear(channel: float) -> float:
        if channel <= 0.04045:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * linear(color.redF())
        + 0.7152 * linear(color.greenF())
        + 0.0722 * linear(color.blueF())
    )
