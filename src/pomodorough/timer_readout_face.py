"""Compact timer readout for large text (UX round 3 DSK-12).

At large fonts the round dial and the controls do not fit the window
together. The Timer screen then shows this readout instead of the dial,
like the Apple clients do at accessibility text sizes (AccessibleDialFace):
a panel with the phase, the countdown on its departure board, the status
with the long-break dots, and a progress bar.

Every dimension is a share of the widget font, so the readout follows the
font scale. When the panel is smaller than the content's natural size, the
content scales down to the panel; a label longer than the panel shrinks to
its width.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPalette, QPen
from PySide6.QtWidgets import QStyle

from .railway_palette import (
    STEEL,
    TICKET,
    TRACK,
    control_border,
    progress_track,
    signal_accent,
)

# Font sizes as shares of the widget font, after the Apple readout: title2
# over body for the phase, and a countdown a few times the body size.
_PHASE_SCALE = 1.25
_COUNTDOWN_SCALE = 2.5
# The Apple countdown shrinks to this share before it is cut. The readout
# asks for at least this share of its natural size.
LEAST_SCALE = 0.6
# Shares of the widget font's line height.
_PADDING = 0.6
_ROW_GAP = 0.35
_BOARD_PADDING = 0.4
_BAR_HEIGHT = 0.4
_DOT_SIZE = 0.5
_DOT_COUNT = 4
# Shape styling: line widths in pixels.
_BORDER_WIDTH = 2


@dataclass(frozen=True)
class ReadoutState:
    time_text: str
    phase_text: str
    status_text: str
    progress: float
    dots: int


def _sized(font: QFont, scale: float) -> QFont:
    sized = QFont(font)
    if font.pointSizeF() > 0:
        sized.setPointSizeF(font.pointSizeF() * scale)
    else:
        sized.setPixelSize(max(1, round(font.pixelSize() * scale)))
    sized.setBold(True)
    return sized


class ReadoutFace:
    """Measures and paints the readout for one widget font and timer state."""

    def __init__(
        self, font: QFont, state: ReadoutState, direction: Qt.LayoutDirection
    ) -> None:
        self._state = state
        self._direction = direction
        self._line = QFontMetricsF(font).height()
        self._phase_font = _sized(font, _PHASE_SCALE)
        self._status_font = _sized(font, 1.0)
        self._countdown_font = _sized(font, _COUNTDOWN_SCALE)
        self._countdown_font.setFamily("DejaVu Sans Mono")

    def natural_size(self) -> QSizeF:
        """Size at which nothing shrinks. The width follows the countdown
        only, so a new status or phase label never moves the layout."""
        padding = self._line * _PADDING
        heights = self._row_heights()
        gaps = self._line * _ROW_GAP * (len(heights) - 1)
        return QSizeF(
            self._board_size().width() + 2 * padding,
            sum(heights) + gaps + 2 * padding,
        )

    def paint(self, painter: QPainter, panel: QRectF, palette: QPalette) -> None:
        painter.setPen(QPen(QColor(control_border(palette)), _BORDER_WIDTH))
        painter.setBrush(palette.color(QPalette.ColorRole.AlternateBase))
        inset = _BORDER_WIDTH / 2
        painter.drawRect(panel.adjusted(inset, inset, -inset, -inset))
        natural = self.natural_size()
        scale = min(
            1.0, panel.width() / natural.width(), panel.height() / natural.height()
        )
        padding = self._line * _PADDING
        painter.save()
        painter.translate(
            panel.left(), panel.top() + (panel.height() - natural.height() * scale) / 2
        )
        painter.scale(scale, scale)
        content = QRectF(
            padding,
            padding,
            panel.width() / scale - 2 * padding,
            natural.height() - 2 * padding,
        )
        self._paint_rows(painter, content, palette)
        painter.restore()

    def _board_size(self) -> QSizeF:
        metrics = QFontMetricsF(self._countdown_font)
        return QSizeF(
            metrics.horizontalAdvance(self._state.time_text)
            + 2 * self._line * _BOARD_PADDING,
            metrics.height(),
        )

    def _row_heights(self) -> list[float]:
        """Phase, countdown board, status with dots, progress bar."""
        return [
            QFontMetricsF(self._phase_font).height(),
            self._board_size().height(),
            max(QFontMetricsF(self._status_font).height(), self._line * _DOT_SIZE),
            self._line * _BAR_HEIGHT,
        ]

    def _paint_rows(self, painter: QPainter, content: QRectF, palette: QPalette) -> None:
        rows = []
        top = content.top()
        for height in self._row_heights():
            rows.append(QRectF(content.left(), top, content.width(), height))
            top += height + self._line * _ROW_GAP
        phase, board, status, bar = rows
        ink = palette.color(QPalette.ColorRole.WindowText)
        self._paint_label(painter, phase, self._phase_font, self._state.phase_text, ink)
        self._paint_board(painter, board)
        dots_width = self._paint_dots(painter, status, palette)
        label = status.adjusted(0, 0, -dots_width - self._line * _ROW_GAP, 0)
        self._paint_label(
            painter,
            self._visual(status, label),
            self._status_font,
            self._state.status_text,
            ink,
        )
        self._paint_bar(painter, bar, palette)

    def _visual(self, row: QRectF, part: QRectF) -> QRectF:
        """part of row, mirrored in a right-to-left layout."""
        return QRectF(
            QStyle.visualRect(self._direction, row.toAlignedRect(), part.toAlignedRect())
        )

    def _paint_label(
        self, painter: QPainter, room: QRectF, font: QFont, text: str, ink: QColor
    ) -> None:
        fitted = QFont(font)
        width = QFontMetricsF(font).horizontalAdvance(text)
        if width > room.width() > 0:
            # The whole label stays readable: it shrinks instead of ending
            # in an ellipsis.
            fitted = _sized(font, room.width() / width)
        painter.setFont(fitted)
        painter.setPen(ink)
        leading = (
            Qt.AlignmentFlag.AlignRight
            if self._direction == Qt.LayoutDirection.RightToLeft
            else Qt.AlignmentFlag.AlignLeft
        )
        painter.drawText(room, leading | Qt.AlignmentFlag.AlignVCenter, text)

    def _paint_board(self, painter: QPainter, row: QRectF) -> None:
        size = self._board_size()
        board = self._visual(row, QRectF(row.topLeft(), size))
        # The departure board of the round dial: track-black panel with
        # ticket-gold digits in both OS themes.
        painter.setPen(QPen(QColor(STEEL), _BORDER_WIDTH))
        painter.setBrush(QColor(TRACK))
        painter.drawRect(board)
        painter.setFont(self._countdown_font)
        painter.setPen(QColor(TICKET))
        painter.drawText(board, Qt.AlignmentFlag.AlignCenter, self._state.time_text)

    def _paint_dots(self, painter: QPainter, row: QRectF, palette: QPalette) -> float:
        """Paint the long-break dots at the trailing end; return their width."""
        size = self._line * _DOT_SIZE
        step = size * 1.5
        width = step * (_DOT_COUNT - 1) + size
        strip = self._visual(row, QRectF(row.right() - width, row.top(), width, row.height()))
        filled = palette.color(QPalette.ColorRole.Text)
        empty = palette.color(QPalette.ColorRole.Base)
        painter.setPen(QPen(palette.color(QPalette.ColorRole.WindowText), _BORDER_WIDTH))
        for index in range(_DOT_COUNT):
            position = index
            if self._direction == Qt.LayoutDirection.RightToLeft:
                position = _DOT_COUNT - 1 - index
            center = QPointF(strip.left() + size / 2 + position * step, strip.center().y())
            painter.setBrush(filled if index < self._state.dots else empty)
            painter.drawEllipse(center, size / 2, size / 2)
        return width

    def _paint_bar(self, painter: QPainter, row: QRectF, palette: QPalette) -> None:
        accent = QColor(signal_accent(palette))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(progress_track(palette, accent))
        painter.drawRect(row)
        done = QRectF(row.left(), row.top(), row.width() * self._state.progress, row.height())
        painter.setBrush(accent)
        painter.drawRect(self._visual(row, done))
