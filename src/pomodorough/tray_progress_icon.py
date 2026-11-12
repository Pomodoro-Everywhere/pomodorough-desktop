"""Tray icon whose own clock arc shows how far the current timer has run."""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
PROGRESS_TRACK_ID = "progress-track"
PROGRESS_ARC_ID = "progress-arc"
ICON_PIXELS = 64
# Trays draw the icon at roughly 16-24 px, where the artwork's own arc is
# about one pixel wide. A doubled stroke keeps the progress readable.
TRAY_STROKE_SCALE = 2.0

ElementTree.register_namespace("", SVG_NAMESPACE)


class TrayProgressIcon:
    """Render the app icon with its static arc replaced by timer progress.

    The artwork is split around the arc so the progress ring is painted
    between the clock face and the pointer and time board, keeping the
    original layering. Ring geometry and colors come from the artwork.
    """

    def __init__(self, svg_path: Path) -> None:
        root = ElementTree.parse(svg_path).getroot()
        children = list(root)
        ids = [child.get("id") for child in children]
        arc_index = ids.index(PROGRESS_ARC_ID)
        track = children[ids.index(PROGRESS_TRACK_ID)]
        arc = children[arc_index]
        self._beneath = QSvgRenderer(_artwork(root, children[:arc_index]))
        self._above = QSvgRenderer(_artwork(root, children[arc_index + 1 :]))
        radius = float(track.get("r", "0"))
        self._ring = QRectF(
            float(track.get("cx", "0")) - radius,
            float(track.get("cy", "0")) - radius,
            radius * 2,
            radius * 2,
        )
        self._track_color = QColor(track.get("stroke", ""))
        self._arc_color = QColor(arc.get("stroke", ""))
        self._stroke = float(arc.get("stroke-width", "0")) * TRAY_STROKE_SCALE

    def icon(self, progress: float) -> QIcon:
        pixmap = QPixmap(ICON_PIXELS, ICON_PIXELS)
        pixmap.fill(Qt.GlobalColor.transparent)
        bounds = QRectF(pixmap.rect())
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._beneath.render(painter, bounds)
        painter.save()
        view_box = self._beneath.viewBoxF()
        painter.scale(
            bounds.width() / view_box.width(), bounds.height() / view_box.height()
        )
        painter.translate(-view_box.topLeft())
        painter.setPen(self._pen(self._track_color))
        painter.drawEllipse(self._ring)
        painter.setPen(self._pen(self._arc_color))
        sweep = round(max(0.0, min(1.0, progress)) * 360 * 16)
        painter.drawArc(self._ring, 90 * 16, -sweep)
        painter.restore()
        self._above.render(painter, bounds)
        painter.end()
        return QIcon(pixmap)

    def _pen(self, color: QColor) -> QPen:
        return QPen(color, self._stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)


def _artwork(
    root: ElementTree.Element, children: list[ElementTree.Element]
) -> QByteArray:
    """The root element with only the given children, as SVG bytes."""
    part = ElementTree.Element(root.tag, root.attrib)
    part.extend(children)
    return QByteArray(ElementTree.tostring(part))
