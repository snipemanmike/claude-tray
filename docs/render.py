"""Regenerate the screenshots in this folder.

Usage:  python docs/render.py

Produces clean, reproducible preview images for the README so we don't have
to capture live desktop screenshots (which leak wallpaper / tray contents).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Import siblings from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from usagedashboard import make_tray_pixmap, ring_color


# ---------------------------------------------------------------------------
# Widget mock-up
# ---------------------------------------------------------------------------

WIDGET_W, WIDGET_H = 260, 150
BG = QColor(20, 22, 28, 255)
TRACK = QColor(60, 65, 75, 255)
TEXT = QColor(235, 235, 240)
SUB = QColor(160, 165, 175)


def _draw_ring(p: QPainter, rect: QRectF, label: str, pct: float, reset: str) -> None:
    # Ring geometry — size based on rect width, leaving room for labels
    size = min(rect.width(), rect.height() - 22)
    ring = QRectF(rect.center().x() - size / 2, rect.top() + 4, size, size)
    thickness = max(6.0, size * 0.12)

    # Track
    p.setPen(QPen(TRACK, thickness, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawArc(ring.adjusted(thickness/2, thickness/2, -thickness/2, -thickness/2),
              0, 360 * 16)

    # Value arc
    v = max(0.0, min(100.0, pct))
    p.setPen(QPen(ring_color(v), thickness, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(
        ring.adjusted(thickness/2, thickness/2, -thickness/2, -thickness/2),
        90 * 16,
        int(-v / 100.0 * 360 * 16),
    )

    # Percentage centred in ring
    f = QFont()
    f.setBold(True)
    f.setPointSizeF(max(10.0, size * 0.22))
    p.setFont(f)
    p.setPen(TEXT)
    p.drawText(ring, Qt.AlignCenter, f"{int(round(v))}%")

    # Label above ring
    sub = QFont()
    sub.setPointSizeF(max(7.0, size * 0.10))
    p.setFont(sub)
    p.setPen(SUB)
    p.drawText(QRectF(rect.left(), rect.top() - 14, rect.width(), 14),
               Qt.AlignCenter, label)

    # Reset under ring
    p.drawText(QRectF(rect.left(), ring.bottom() + 2, rect.width(), 18),
               Qt.AlignCenter, f"resets {reset}")


def render_widget(pct5: float, pct7: float,
                  reset5: str = "3h 12m",
                  reset7: str = "4d 6h",
                  scale: int = 2) -> QPixmap:
    """Render the widget contents at the given utilizations."""
    pix = QPixmap(WIDGET_W * scale, WIDGET_H * scale)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    p.scale(scale, scale)

    # Rounded card background
    bg = QRectF(0, 0, WIDGET_W - 1, WIDGET_H - 1)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(BG))
    p.drawRoundedRect(bg, 14, 14)

    # Two gauges side by side, with label area at top
    inner = bg.adjusted(10, 22, -10, -10)
    half = inner.width() / 2
    left = QRectF(inner.left(), inner.top(), half - 4, inner.height())
    right = QRectF(inner.left() + half + 4, inner.top(),
                   half - 4, inner.height())
    _draw_ring(p, left, "5h session", pct5, reset5)
    _draw_ring(p, right, "7d weekly", pct7, reset7)
    p.end()
    return pix


# ---------------------------------------------------------------------------
# Tray strip mock-up
# ---------------------------------------------------------------------------

def render_tray_strip(pairs: list[tuple[float, float]],
                      scale: int = 8,
                      gap: int = 6,
                      group_gap: int = 28) -> QPixmap:
    """Show several pairs of (5H, 7D) icons in a row, scaled up so labels read."""
    base = 16
    icon = base * scale
    n = len(pairs)
    w = group_gap + n * (2 * icon + gap + group_gap)
    h = icon + 24
    pix = QPixmap(w, h)
    pix.fill(QColor(28, 30, 36))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    label_font = QFont()
    label_font.setPointSizeF(10)
    p.setFont(label_font)
    p.setPen(QColor(170, 175, 188))

    x = group_gap
    for pct5, pct7 in pairs:
        # Render the actual tray pixmaps at native 16, then scale up cleanly
        i5 = make_tray_pixmap("5H", pct5, base).scaled(
            icon, icon, Qt.KeepAspectRatio, Qt.FastTransformation
        )
        i7 = make_tray_pixmap("7D", pct7, base).scaled(
            icon, icon, Qt.KeepAspectRatio, Qt.FastTransformation
        )
        y = 8
        p.drawPixmap(x, y, i5)
        p.drawPixmap(x + icon + gap, y, i7)
        p.drawText(
            QRectF(x, y + icon + 2, 2 * icon + gap, 20),
            Qt.AlignCenter,
            f"5h={int(round(pct5))}%   7d={int(round(pct7))}%",
        )
        x += 2 * icon + gap + group_gap
    p.end()
    return pix


# ---------------------------------------------------------------------------
# Compose the hero image (widget + tray strip stacked)
# ---------------------------------------------------------------------------

def render_hero() -> QPixmap:
    widget = render_widget(72.0, 35.0, reset5="2h 18m", reset7="3d 4h", scale=2)
    tray = render_tray_strip(
        [(8.0, 4.0), (35.0, 22.0), (78.0, 55.0), (95.0, 80.0)],
        scale=6, gap=4, group_gap=22,
    )

    pad = 24
    W = max(widget.width(), tray.width()) + pad * 2
    H = widget.height() + tray.height() + pad * 3 + 28
    pix = QPixmap(W, H)
    pix.fill(QColor(28, 30, 36))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    title = QFont()
    title.setBold(True)
    title.setPointSizeF(11)
    p.setFont(title)
    p.setPen(QColor(170, 175, 188))
    p.drawText(QRectF(pad, pad, W - 2 * pad, 22),
               Qt.AlignLeft | Qt.AlignVCenter, "floating widget")

    p.drawPixmap((W - widget.width()) // 2, pad + 22, widget)

    p.setFont(title)
    p.drawText(QRectF(pad, pad + 22 + widget.height() + 12, W - 2 * pad, 22),
               Qt.AlignLeft | Qt.AlignVCenter, "tray icons (shown 6× upscaled)")

    p.drawPixmap((W - tray.width()) // 2,
                 pad + 22 + widget.height() + 12 + 22, tray)
    p.end()
    return pix


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    out = Path(__file__).resolve().parent

    # Hero / README banner
    render_hero().save(str(out / "hero.png"))

    # Individual: widget at three states
    states = [
        ("widget-low.png", 8.0, 4.0, "4h 12m", "5d 8h"),
        ("widget-mid.png", 65.0, 38.0, "2h 1m", "3d 2h"),
        ("widget-high.png", 96.0, 78.0, "12m", "1d 5h"),
    ]
    for name, p5, p7, r5, r7 in states:
        render_widget(p5, p7, r5, r7, scale=2).save(str(out / name))

    # Individual: tray strip
    render_tray_strip(
        [(8.0, 4.0), (35.0, 22.0), (78.0, 55.0), (95.0, 80.0)],
        scale=8, gap=6, group_gap=28,
    ).save(str(out / "tray.png"))

    for f in sorted(out.glob("*.png")):
        print(f"{f.name:18s}  {f.stat().st_size:>6} bytes")


if __name__ == "__main__":
    main()
