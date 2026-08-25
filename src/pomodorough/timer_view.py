from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .localization import Strings


@dataclass(frozen=True)
class _ClockGeometry:
    side: float
    dial: QRectF
    center: QPointF
    radius: float

    @classmethod
    def for_widget(cls, widget: QWidget) -> _ClockGeometry:
        side = min(widget.width(), widget.height()) - 14
        dial = QRectF(
            (widget.width() - side) / 2,
            (widget.height() - side) / 2,
            side,
            side,
        )
        return cls(side, dial, dial.center(), side / 2)

    def arc_rect(self) -> QRectF:
        inset = self.radius * 0.18
        return self.dial.adjusted(inset, inset, -inset, -inset)

    def display_board(self) -> QRectF:
        return QRectF(
            self.center.x() - self.radius * 0.79,
            self.center.y() - self.radius * 0.27,
            self.radius * 1.58,
            self.radius * 0.54,
        )


class _ClockRenderer:
    def __init__(
        self,
        painter: QPainter,
        geometry: _ClockGeometry,
        palette: QPalette,
        *,
        time_text: str,
        phase_text: str,
        status_text: str,
        progress: float,
    ) -> None:
        self.painter = painter
        self.geometry = geometry
        self.palette = palette
        self.time_text = time_text
        self.phase_text = phase_text
        self.status_text = status_text
        self.progress = progress

    def paint(self) -> None:
        self._paint_face()
        self._paint_ticks()
        self._paint_progress()
        self._paint_pointer()
        self._paint_display()
        self._paint_labels()

    def _paint_face(self) -> None:
        side = self.geometry.side
        radius = self.geometry.radius
        base = self.palette.color(QPalette.ColorRole.Base)
        alternate = self.palette.color(QPalette.ColorRole.AlternateBase)
        mid = self.palette.color(QPalette.ColorRole.Mid)
        self.painter.setPen(QPen(base, max(6, side * 0.022)))
        self.painter.setBrush(alternate)
        self.painter.drawEllipse(self.geometry.dial.adjusted(7, 7, -7, -7))
        self.painter.setPen(QPen(mid, max(10, side * 0.045)))
        self.painter.setBrush(Qt.BrushStyle.NoBrush)
        self.painter.drawEllipse(
            self.geometry.dial.adjusted(
                radius * 0.17,
                radius * 0.17,
                -radius * 0.17,
                -radius * 0.17,
            )
        )

    def _paint_ticks(self) -> None:
        radius = self.geometry.radius
        text = self.palette.color(QPalette.ColorRole.Text)
        self.painter.save()
        self.painter.translate(self.geometry.center)
        self.painter.setPen(QPen(text, max(3, self.geometry.side * 0.009)))
        for tick in range(60):
            length = radius * (0.11 if tick % 5 == 0 else 0.055)
            outer = radius * 0.81
            self.painter.drawLine(QPointF(0, -outer), QPointF(0, -outer + length))
            self.painter.rotate(6)
        self.painter.restore()

    def _paint_progress(self) -> None:
        highlight = self.palette.color(QPalette.ColorRole.Highlight)
        self.painter.setPen(
            QPen(
                highlight,
                max(10, self.geometry.side * 0.047),
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.FlatCap,
            )
        )
        self.painter.drawArc(
            self.geometry.arc_rect(),
            90 * 16,
            -round(self.progress * 360 * 16),
        )

    def _paint_pointer(self) -> None:
        angle = math.radians(-90 + self.progress * 360)
        pointer_radius = self.geometry.radius * 0.64
        center = self.geometry.center
        pointer = QPointF(
            center.x() + math.cos(angle) * pointer_radius,
            center.y() + math.sin(angle) * pointer_radius,
        )
        text = self.palette.color(QPalette.ColorRole.Text)
        highlight = self.palette.color(QPalette.ColorRole.Highlight)
        self.painter.setPen(QPen(text, max(4, self.geometry.side * 0.013)))
        self.painter.drawLine(center, pointer)
        self.painter.setBrush(highlight)
        pointer_size = max(5, self.geometry.side * 0.018)
        self.painter.drawEllipse(pointer, pointer_size, pointer_size)

    def _paint_display(self) -> None:
        side = self.geometry.side
        center = self.geometry.center
        board = self.geometry.display_board()
        base = self.palette.color(QPalette.ColorRole.Base)
        text = self.palette.color(QPalette.ColorRole.Text)
        window = self.palette.color(QPalette.ColorRole.Window)
        self.painter.setPen(QPen(base, max(4, side * 0.013)))
        self.painter.setBrush(text)
        self.painter.drawRect(board)
        self.painter.setPen(QPen(window, 3))
        self.painter.drawLine(center.x(), board.top() + 5, center.x(), board.bottom() - 5)
        display_font = QFont(
            "DejaVu Sans Mono",
            max(20, round(side * 0.105)),
            QFont.Weight.Bold,
        )
        display_font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 91)
        self.painter.setFont(display_font)
        self.painter.setPen(base)
        self.painter.drawText(board, Qt.AlignmentFlag.AlignCenter, self.time_text)

    def _paint_labels(self) -> None:
        side = self.geometry.side
        radius = self.geometry.radius
        center = self.geometry.center
        board = self.geometry.display_board()
        alternate = self.palette.color(QPalette.ColorRole.AlternateBase)
        window_text = self.palette.color(QPalette.ColorRole.WindowText)
        label_font = QFont(
            "DejaVu Sans Condensed",
            max(8, round(side * 0.032)),
            QFont.Weight.Bold,
        )
        label_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        self.painter.setFont(label_font)
        self.painter.setPen(window_text)
        phase_rect = QRectF(
            self.geometry.dial.left(),
            board.top() - radius * 0.24,
            side,
            radius * 0.18,
        )
        self.painter.drawText(
            phase_rect,
            Qt.AlignmentFlag.AlignCenter,
            self.phase_text,
        )
        status_width = min(
            radius * 1.6,
            self.painter.fontMetrics().horizontalAdvance(self.status_text)
            + radius * 0.18,
        )
        status_rect = QRectF(
            center.x() - status_width / 2,
            board.bottom() + radius * 0.06,
            status_width,
            radius * 0.18,
        )
        self.painter.fillRect(status_rect, alternate)
        self.painter.setPen(window_text)
        self.painter.drawText(
            status_rect,
            Qt.AlignmentFlag.AlignCenter,
            self.status_text,
        )


class ClockWidget(QWidget):
    def __init__(self, strings: Strings | None = None) -> None:
        super().__init__()
        self.strings = strings or Strings()
        self.time_text = "25:00"
        self.phase_text = self.strings.text("phase.focus").upper()
        self.status_text = self.strings.text("status.rail.idle")
        self.progress = 0.0
        self.setAccessibleName(self.strings.text("status.timer_accessible"))
        self.setMinimumSize(170, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(360, 360)

    def set_state(
        self, time_text: str, phase: str, status: str, progress: float
    ) -> None:
        self.time_text = time_text
        self.phase_text = phase.upper()
        self.status_text = status.upper()
        self.progress = max(0.0, min(1.0, progress))
        self.setAccessibleDescription(
            self.strings.text(
                "status.timer_description",
                phase=phase.title(),
                time=time_text,
                status=status.lower(),
            )
        )
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _ClockRenderer(
            painter,
            _ClockGeometry.for_widget(self),
            self.palette(),
            time_text=self.time_text,
            phase_text=self.phase_text,
            status_text=self.status_text,
            progress=self.progress,
        ).paint()
