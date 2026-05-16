"""Render the urgency color model as a 2D heatmap of the live curve.

X axis = time remaining %, Y axis = pct used. Each cell colored by what
the tray icon / widget ring would show at that state — uses the real
`urgency_color` function from usagedashboard.py so the chart always
reflects the deployed curve.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from usagedashboard import (
    urgency_color, URGENCY_AMBER_ANCHOR, URGENCY_RED_ANCHOR,
)


DARK  = QColor(28, 30, 36)
TEXT  = QColor(235, 235, 240)
SUB   = QColor(160, 165, 180)
GRID  = QColor(255, 255, 255, 45)


def render_heatmap(amber: float = URGENCY_AMBER_ANCHOR,
                   red: float = URGENCY_RED_ANCHOR,
                   title: str = "", cell_px: int = 5) -> QPixmap:
    """Render a 100x100 grid coloured by the live urgency_color function."""
    grid = 100
    chart_w = grid * cell_px
    chart_h = grid * cell_px
    pad_l, pad_b, pad_t, pad_r = 60, 50, 40, 20
    W = pad_l + chart_w + pad_r
    H = pad_t + chart_h + pad_b

    pix = QPixmap(W, H); pix.fill(DARK)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    # Title
    f = QFont(); f.setBold(True); f.setPointSizeF(11)
    p.setFont(f); p.setPen(TEXT)
    p.drawText(QRectF(pad_l, 8, chart_w, 24), Qt.AlignLeft, title)

    # Fill each cell with the live urgency colour (gradient)
    for x_idx in range(grid):
        tr = x_idx + 0.5
        for y_idx in range(grid):
            pct = (grid - 1 - y_idx) + 0.5
            c = urgency_color(pct, tr)
            p.fillRect(
                pad_l + x_idx * cell_px,
                pad_t + y_idx * cell_px,
                cell_px, cell_px, c,
            )

    # Mark the two gradient anchor diagonals (where peak amber and pure red live).
    def chart_xy(tr_, pct_):
        return QPointF(pad_l + tr_ * cell_px,
                       pad_t + (grid - pct_) * cell_px)
    p.setPen(QPen(QColor(0, 0, 0, 160), 1.2, Qt.DashLine))
    for thr in (amber, red):
        if 0 <= thr <= 100:
            p.drawLine(chart_xy(thr, 100), chart_xy(100, thr))

    # Axes / labels
    p.setPen(QPen(QColor(255, 255, 255, 120), 1))
    p.drawRect(pad_l, pad_t, chart_w, chart_h)
    # Gridlines every 25
    p.setPen(QPen(GRID, 1, Qt.DotLine))
    for v in (25, 50, 75):
        x = pad_l + v * cell_px
        y = pad_t + (100 - v) * cell_px
        p.drawLine(x, pad_t, x, pad_t + chart_h)
        p.drawLine(pad_l, y, pad_l + chart_w, y)

    # Tick labels
    sm = QFont(); sm.setPointSizeF(8); p.setFont(sm); p.setPen(SUB)
    for v in (0, 25, 50, 75, 100):
        x = pad_l + v * cell_px
        y = pad_t + (100 - v) * cell_px
        p.drawText(QRectF(x - 18, pad_t + chart_h + 4, 36, 14),
                   Qt.AlignCenter, f"{v}")
        p.drawText(QRectF(pad_l - 38, y - 7, 32, 14),
                   Qt.AlignRight | Qt.AlignVCenter, f"{v}")
    # Axis titles
    p.setFont(QFont("", 9, QFont.Bold)); p.setPen(TEXT)
    p.drawText(QRectF(pad_l, pad_t + chart_h + 22, chart_w, 18),
               Qt.AlignCenter, "time remaining  →  fresh window")
    p.save()
    p.translate(16, pad_t + chart_h / 2)
    p.rotate(-90)
    p.drawText(QRectF(-chart_h / 2, 0, chart_h, 14),
               Qt.AlignCenter, "pct used  ↑")
    p.restore()

    # Legend dot for each color band, with the formula
    legend_y = pad_t + chart_h + 42
    legend_x = pad_l
    f2 = QFont(); f2.setPointSizeF(8); p.setFont(f2)
    p.setPen(SUB)
    p.drawText(QRectF(pad_l, legend_y, chart_w, 16), Qt.AlignLeft,
               f"urgency = pct + time_remaining − 100   |   "
               f"green ≤ 0 → smooth → peak amber at {amber:g} → "
               f"pure red at {red:g}   (dashed lines = anchors)")
    p.end()
    return pix


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    out = Path(__file__).resolve().parent
    title = (
        f"continuous gradient  (peak amber at urgency = "
        f"{URGENCY_AMBER_ANCHOR:g}, pure red at {URGENCY_RED_ANCHOR:g})"
    )
    render_heatmap(title=title).save(str(out / "urgency_curve.png"))
    print("saved urgency_curve.png")


if __name__ == "__main__":
    main()
