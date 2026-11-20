"""Tray icon with a progress ring around the clock for the current timer."""

from __future__ import annotations

import math
from pathlib import Path
from xml.etree import ElementTree

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .railway_palette import SIGNAL, TRACK

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
# The artwork's own decorative arc and track. The tray leaves both out so
# an idle tray never shows a partial arc that reads as timer progress.
ARTWORK_RING_IDS = frozenset({"progress-track", "progress-arc"})
# Native sizes for common tray bars plus a larger master. Painting each
# size directly keeps the ring edges sharper than downscaling one pixmap.
ICON_SIZES = (16, 22, 24, 32, 64)
# The ring sits in the navy margin between the clock face (outer edge at
# 193 view units) and the icon edge (256). Nothing in the artwork covers
# it, so the arc end stays visible for every progress value. Units are
# relative to the artwork's 512-unit view box.
RING_RADIUS = 225.0
RING_WIDTH = 56.0
# A running timer always shows at least a stub of arc, and an unfinished
# one always shows a gap. The stub is one ring width long, so a timer that
# just started differs from an idle tray and 99% differs from 100%.
VISIBLE_STUB = RING_WIDTH / (2 * math.pi * RING_RADIUS)
# SIGNAL on TRACK is 5.9:1 (WCAG non-text minimum 3:1). The old steel track
# under the coral arc was 1.2:1, which left the arc readable by hue only.
ARC_COLOR = SIGNAL
TRACK_COLOR = TRACK

ElementTree.register_namespace("", SVG_NAMESPACE)


def visible_sweep(progress: float | None) -> float:
    """The share of the ring the arc covers, or 0.0 for an idle tray.

    Running progress maps linearly onto the stub-to-gap range, so the arc
    still grows with every percent while keeping both ends distinct.
    """
    if progress is None:
        return 0.0
    if progress >= 1.0:
        return 1.0
    clamped = max(0.0, progress)
    return VISIBLE_STUB + clamped * (1.0 - 2 * VISIBLE_STUB)


class TrayProgressIcon:
    """Render the app clock with a progress ring around its face."""

    def __init__(self, svg_path: Path) -> None:
        root = ElementTree.parse(svg_path).getroot()
        clock = [child for child in root if child.get("id") not in ARTWORK_RING_IDS]
        self._clock = QSvgRenderer(_artwork(root, clock))

    def icon(self, progress: float | None) -> QIcon:
        """The tray icon for a running timer's progress, or None when idle."""
        sweep = visible_sweep(progress)
        icon = QIcon()
        for size in ICON_SIZES:
            icon.addPixmap(self._pixmap(size, sweep))
        return icon

    def _pixmap(self, size: int, sweep: float) -> QPixmap:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        bounds = QRectF(pixmap.rect())
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._clock.render(painter, bounds)
        view_box = self._clock.viewBoxF()
        painter.scale(
            bounds.width() / view_box.width(), bounds.height() / view_box.height()
        )
        painter.translate(-view_box.topLeft())
        ring = QRectF(
            view_box.center() - QPointF(RING_RADIUS, RING_RADIUS),
            view_box.center() + QPointF(RING_RADIUS, RING_RADIUS),
        )
        painter.setPen(_ring_pen(TRACK_COLOR))
        painter.drawEllipse(ring)
        if sweep > 0.0:
            painter.setPen(_ring_pen(ARC_COLOR))
            painter.drawArc(ring, 90 * 16, -round(sweep * 360 * 16))
        painter.end()
        return pixmap


def _ring_pen(color: str) -> QPen:
    return QPen(QColor(color), RING_WIDTH, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)


def _artwork(
    root: ElementTree.Element, children: list[ElementTree.Element]
) -> QByteArray:
    """The root element with only the given children, as SVG bytes."""
    part = ElementTree.Element(root.tag, root.attrib)
    part.extend(children)
    return QByteArray(ElementTree.tostring(part))
