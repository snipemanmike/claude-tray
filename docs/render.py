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

from usagedashboard import make_tray_pixmap, urgency_color


# ---------------------------------------------------------------------------
# Widget mock-up
# ---------------------------------------------------------------------------

WIDGET_W, WIDGET_H = 260, 150
BG = QColor(20, 22, 28, 255)
TRACK = QColor(60, 65, 75, 255)
TEXT = QColor(235, 235, 240)
SUB = QColor(160, 165, 175)


def _draw_ring(p: QPainter, rect: QRectF, label: str,
               pct: float, time_rem_pct: float, reset: str) -> None:
    # Same behavior as the tray: ring drains with time, number coloured by urgency.
    size = min(rect.width(), rect.height() - 22)
    ring = QRectF(rect.center().x() - size / 2, rect.top() + 4, size, size)
    thickness = max(6.0, size * 0.12)
    inner = ring.adjusted(thickness/2, thickness/2, -thickness/2, -thickness/2)

    # Track
    p.setPen(QPen(TRACK, thickness, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawArc(inner, 0, 360 * 16)

    # Time-remaining arc (white)
    t = max(0.0, min(100.0, time_rem_pct))
    p.setPen(QPen(TEXT, thickness, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(inner, 90 * 16, int(-(t / 100.0) * 360 * 16))

    # % in centre, urgency-coloured
    v = max(0.0, min(100.0, pct))
    f = QFont(); f.setBold(True); f.setPointSizeF(max(10.0, size * 0.22))
    p.setFont(f); p.setPen(urgency_color(v, time_rem_pct))
    p.drawText(ring, Qt.AlignCenter, f"{int(round(v))}%")

    sub = QFont(); sub.setPointSizeF(max(7.0, size * 0.10))
    p.setFont(sub); p.setPen(SUB)
    p.drawText(QRectF(rect.left(), rect.top() - 14, rect.width(), 14),
               Qt.AlignCenter, label)
    p.drawText(QRectF(rect.left(), ring.bottom() + 2, rect.width(), 18),
               Qt.AlignCenter, f"resets {reset}")


def render_widget(pct5: float, pct7: float,
                  tr5: float = 60, tr7: float = 70,
                  reset5: str = "3h 12m",
                  reset7: str = "4d 6h",
                  scale: int = 2) -> QPixmap:
    """Render the widget contents at the given utilizations + time-remaining %."""
    pix = QPixmap(WIDGET_W * scale, WIDGET_H * scale)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    p.scale(scale, scale)

    bg = QRectF(0, 0, WIDGET_W - 1, WIDGET_H - 1)
    p.setPen(Qt.NoPen); p.setBrush(QBrush(BG))
    p.drawRoundedRect(bg, 14, 14)

    inner = bg.adjusted(10, 22, -10, -10)
    half = inner.width() / 2
    left = QRectF(inner.left(), inner.top(), half - 4, inner.height())
    right = QRectF(inner.left() + half + 4, inner.top(),
                   half - 4, inner.height())
    _draw_ring(p, left, "5h session", pct5, tr5, reset5)
    _draw_ring(p, right, "7d weekly", pct7, tr7, reset7)
    p.end()
    return pix


# ---------------------------------------------------------------------------
# Tray strip mock-up
# ---------------------------------------------------------------------------

def render_tray_strip(pairs: list[tuple[float, float, float, float]],
                      scale: int = 8,
                      gap: int = 6,
                      group_gap: int = 28) -> QPixmap:
    """Show several (pct5, tr5, pct7, tr7) tuples as side-by-side icon pairs
    (left = 5h session, right = 7d weekly)."""
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
    for pct5, tr5, pct7, tr7 in pairs:
        i5 = make_tray_pixmap(pct5, tr5, base, marker="h").scaled(
            icon, icon, Qt.KeepAspectRatio, Qt.FastTransformation
        )
        i7 = make_tray_pixmap(pct7, tr7, base, marker="d").scaled(
            icon, icon, Qt.KeepAspectRatio, Qt.FastTransformation
        )
        y = 8
        p.drawPixmap(x, y, i5)
        p.drawPixmap(x + icon + gap, y, i7)
        p.drawText(
            QRectF(x, y + icon + 2, 2 * icon + gap, 20),
            Qt.AlignCenter,
            f"5h {int(round(pct5))}%  7d {int(round(pct7))}%",
        )
        x += 2 * icon + gap + group_gap
    p.end()
    return pix


# ---------------------------------------------------------------------------
# Mock Windows taskbar — shows the overlay icons in their actual context
# ---------------------------------------------------------------------------

TASKBAR_BG = QColor(32, 32, 32)
TASKBAR_TEXT = QColor(220, 220, 225)
TASKBAR_DIM = QColor(160, 165, 175)


def render_taskbar_mockup(pct5: float, tr5: float,
                          pct7: float, tr7: float,
                          width: int = 880, height: int = 56) -> QPixmap:
    """A representative slice of the Windows taskbar with our overlay icons
    embedded on the left, then fake chevron, fake tray icons, and clock.
    Icons are drawn at actual taskbar height for visual fidelity."""
    pix = QPixmap(width, height)
    pix.fill(QColor(20, 22, 26))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    # Taskbar strip across the full width
    bar_y = 8
    bar_h = height - 16
    p.setPen(Qt.NoPen)
    p.setBrush(TASKBAR_BG)
    p.drawRect(0, bar_y, width, bar_h)

    icon = bar_h - 6
    gap = 4
    # Our overlay icons — positioned just left of where the "tray" starts
    tray_x = width - 320
    overlay_w = icon * 2 + gap
    overlay_x = tray_x - overlay_w - 12
    overlay_y = bar_y + 3
    i5 = make_tray_pixmap(pct5, tr5, icon, marker="h")
    i7 = make_tray_pixmap(pct7, tr7, icon, marker="d")
    p.drawPixmap(overlay_x, overlay_y, i5)
    p.drawPixmap(overlay_x + icon + gap, overlay_y, i7)

    # Fake chevron
    p.setFont(QFont("Segoe UI", int(icon * 0.5)))
    p.setPen(TASKBAR_DIM)
    p.drawText(QRectF(tray_x, bar_y, 24, bar_h), Qt.AlignCenter, "˄")

    # Fake tray icons (just simple shapes so the layout reads)
    tx = tray_x + 30
    for col in [QColor(120, 170, 220), QColor(180, 180, 190),
                QColor(180, 180, 190), QColor(140, 200, 180)]:
        p.setBrush(col); p.setPen(Qt.NoPen)
        p.drawRoundedRect(tx, bar_y + bar_h / 2 - 8, 16, 16, 2, 2)
        tx += 28

    # Fake clock
    p.setFont(QFont("Segoe UI", 9))
    p.setPen(TASKBAR_TEXT)
    p.drawText(QRectF(width - 110, bar_y, 96, bar_h / 2),
               Qt.AlignVCenter | Qt.AlignRight, "5:33 PM")
    p.drawText(QRectF(width - 110, bar_y + bar_h / 2, 96, bar_h / 2),
               Qt.AlignVCenter | Qt.AlignRight, "5/16/2026")
    p.end()
    return pix


# ---------------------------------------------------------------------------
# Compose the hero image: taskbar mockup on top + floating widget below
# ---------------------------------------------------------------------------

def render_hero() -> QPixmap:
    # Two paired scenarios so the hero conveys both surfaces at once
    taskbar = render_taskbar_mockup(72.0, 80.0, 35.0, 78.0,
                                    width=940, height=56)
    widget = render_widget(72.0, 35.0, tr5=80, tr7=78,
                           reset5="4h 1m", reset7="5d 12h", scale=2)

    pad = 24
    W = max(taskbar.width(), widget.width()) + pad * 2
    H = taskbar.height() + widget.height() + pad * 3 + 56
    pix = QPixmap(W, H)
    pix.fill(QColor(18, 20, 24))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    title = QFont(); title.setBold(True); title.setPointSizeF(11)
    p.setFont(title); p.setPen(QColor(170, 175, 188))

    # Section A: taskbar overlay
    y = pad
    p.drawText(QRectF(pad, y, W - 2*pad, 22), Qt.AlignLeft | Qt.AlignVCenter,
               "in-taskbar overlay (lives next to your tray)")
    y += 22
    p.drawPixmap((W - taskbar.width()) // 2, y, taskbar)
    y += taskbar.height() + 20

    # Section B: floating widget that pops up on click
    p.drawText(QRectF(pad, y, W - 2*pad, 22), Qt.AlignLeft | Qt.AlignVCenter,
               "floating widget (click the overlay to toggle)")
    y += 22
    p.drawPixmap((W - widget.width()) // 2, y, widget)

    p.end()
    return pix


def render_urgency_explainer() -> QPixmap:
    """3×3 grid of (low/mid/high %) × (fresh/half/imminent time) tray icons,
    showing how the urgency model colors each cell."""
    pcts =  [(" 8%",  8.0), ("50%", 50.0), ("92%", 92.0)]
    times = [("fresh window\n(95% time)", 95.0),
             ("half window\n(50% time)", 50.0),
             ("imminent reset\n(5% time)",  5.0)]

    icon_native = 16
    upscale = 6
    icon = icon_native * upscale
    cell_w = icon + 40
    cell_h = icon + 28
    head_h = 110
    side_w = 110

    W = side_w + 3 * cell_w + 30
    H = head_h + 3 * cell_h + 40
    pix = QPixmap(W, H); pix.fill(QColor(28, 30, 36))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    title = QFont(); title.setBold(True); title.setPointSizeF(11)
    p.setFont(title); p.setPen(QColor(235, 235, 245))
    p.drawText(QRectF(20, 14, W - 40, 22), Qt.AlignLeft,
               "urgency = pct + time_remaining% − 100  (4-stop color gradient)")
    sm = QFont(); sm.setPointSizeF(8)
    p.setFont(sm); p.setPen(QColor(160, 165, 180))
    p.drawText(QRectF(20, 32, W - 40, 18), Qt.AlignLeft,
               "blue = under-utilizing, green = on pace, amber = burning fast, red = will exhaust")

    # Column headers
    p.setFont(title); p.setPen(QColor(200, 205, 215))
    for ci, (tlabel, _) in enumerate(times):
        x = side_w + ci * cell_w
        p.drawText(QRectF(x, 55, cell_w, head_h - 55),
                   Qt.AlignCenter | Qt.TextWordWrap, tlabel)

    # Row labels + cells
    for ri, (plabel, pct) in enumerate(pcts):
        y = head_h + ri * cell_h
        p.setFont(title); p.setPen(QColor(200, 205, 215))
        p.drawText(QRectF(0, y + (icon - 14)//2, side_w - 8, 22),
                   Qt.AlignRight | Qt.AlignVCenter, f"pct = {plabel}")
        for ci, (_, trp) in enumerate(times):
            x = side_w + ci * cell_w + (cell_w - icon) // 2
            ic = make_tray_pixmap(pct, trp, icon_native, marker="h").scaled(
                icon, icon, Qt.KeepAspectRatio, Qt.FastTransformation)
            p.drawPixmap(x, y, ic)
    p.end()
    return pix


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    out = Path(__file__).resolve().parent

    # Hero / README banner
    render_hero().save(str(out / "hero.png"))

    # Three states that span the urgency color range
    states = [
        # chill: low % regardless of time → green
        ("widget-low.png",  8.0,  4.0,  85, 95, "4h 12m", "5d 8h"),
        # burning fast: mid % but window mostly fresh → amber/red
        ("widget-mid.png",  65.0, 38.0, 85, 90, "4h 15m", "6d 8h"),
        # the made-it state: high % but reset is right there → green
        ("widget-high.png", 96.0, 78.0, 8,  18, "12m",    "1d 5h"),
    ]
    for name, p5, p7, tr5, tr7, r5, r7 in states:
        render_widget(p5, p7, tr5, tr7, r5, r7, scale=2).save(str(out / name))

    # Overlay-icon strip across the urgency color range
    render_tray_strip(
        [(5.0,  95.0, 3.0,  98.0),
         (55.0, 90.0, 30.0, 92.0),
         (88.0, 65.0, 60.0, 55.0),
         (95.0, 10.0, 80.0, 22.0)],
        scale=8, gap=6, group_gap=28,
    ).save(str(out / "tray.png"))

    # Mock taskbar at three urgency states — actual context users see
    render_taskbar_mockup( 8.0, 95.0,  4.0, 98.0).save(str(out / "taskbar-low.png"))
    render_taskbar_mockup(72.0, 80.0, 35.0, 78.0).save(str(out / "taskbar-mid.png"))
    render_taskbar_mockup(95.0, 65.0, 60.0, 55.0).save(str(out / "taskbar-high.png"))

    render_urgency_explainer().save(str(out / "urgency.png"))

    for f in sorted(out.glob("*.png")):
        print(f"{f.name:18s}  {f.stat().st_size:>6} bytes")


if __name__ == "__main__":
    main()
