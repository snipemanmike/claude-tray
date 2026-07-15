"""Regenerate the screenshots + timelapse GIF in this folder.

Usage:  python docs/render.py

Produces clean, reproducible preview images for the README so we don't have
to capture live desktop screenshots (which leak wallpaper / tray contents).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

# Import siblings from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QBuffer, QRectF
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from usagedashboard import (
    CLAUDE_TILE_BASE,
    GROUP_SPLIT,
    OPENAI_TILE_BASE,
    make_tray_pixmap,
    urgency_color,
)


# ---------------------------------------------------------------------------
# Shared palette (mirrors usagedashboard.py)
# ---------------------------------------------------------------------------

WIDGET_W, WIDGET_H = 470, 158
BG = QColor(20, 22, 28, 255)
TRACK = QColor(60, 65, 75, 255)
TEXT = QColor(235, 235, 240)
SUB = QColor(160, 165, 175)

# (label, marker) in display order — OpenAI first, Claude trio after,
# same as the live app. ChatGPT plans expose only a weekly cap now.
GAUGES = [
    ("GPT weekly", "gw"),
    ("5h session", "h"), ("7d weekly", "d"), ("7d Fable", "fd"),
]


def _fmt_reset(tr_pct: float, window_h: float) -> str:
    """Human countdown for `tr_pct`% remaining of a `window_h`-hour window."""
    secs = int(tr_pct / 100.0 * window_h * 3600)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _draw_ring(p: QPainter, rect: QRectF, label: str,
               pct: float, time_rem_pct: float, reset: str) -> None:
    # Same behavior as the app: ring drains with time, number coloured by urgency.
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


def render_widget(values: list[tuple[float, float, str]],
                  scale: float = 2.0) -> QPixmap:
    """Render the 5-ring widget. `values` = [(pct, time_rem_pct, reset), …]
    in GAUGES order (OpenAI pair, then Claude trio)."""
    pix = QPixmap(int(WIDGET_W * scale), int(WIDGET_H * scale))
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    p.scale(scale, scale)

    bg = QRectF(0, 0, WIDGET_W - 1, WIDGET_H - 1)
    p.setPen(Qt.NoPen); p.setBrush(QBrush(BG))
    p.drawRoundedRect(bg, 14, 14)

    # Five columns with a wider gap + divider between the provider groups —
    # mirrors Widget.paintEvent.
    inner = bg.adjusted(10, 22, -10, -10)
    gap, group_gap = 6, 14
    n = len(GAUGES)
    col_w = (inner.width() - gap * (n - 1) - group_gap) / n
    x = inner.left()
    for i, ((label, _), (pct, tr, reset)) in enumerate(zip(GAUGES, values)):
        if i == GROUP_SPLIT:
            div_x = x + group_gap / 2 - gap / 2
            p.setPen(QPen(TRACK, 1))
            p.drawLine(int(div_x), int(inner.top() - 8),
                       int(div_x), int(inner.bottom()))
            x += group_gap
        _draw_ring(p, QRectF(x, inner.top(), col_w, inner.height()),
                   label, pct, tr, reset)
        x += col_w + gap
    p.end()
    return pix


# ---------------------------------------------------------------------------
# Mock Windows taskbar — shows the overlay tiles in their actual context
# ---------------------------------------------------------------------------

TASKBAR_BG = QColor(32, 32, 32)
TASKBAR_TEXT = QColor(220, 220, 225)
TASKBAR_DIM = QColor(160, 165, 175)


def _draw_tile_row(p: QPainter, values: list[tuple[float, float]],
                   x: int, y: int, icon: int, gap: int = 4,
                   group_gap: int = 10) -> int:
    """Draw the 5-tile overlay row exactly like TaskbarWidget.paintEvent.
    Returns the row width."""
    x0 = x
    for i, ((_, marker), (pct, tr)) in enumerate(zip(GAUGES, values)):
        if i == GROUP_SPLIT:
            x += group_gap
        base = OPENAI_TILE_BASE if i < GROUP_SPLIT else CLAUDE_TILE_BASE
        p.drawPixmap(x, y, make_tray_pixmap(pct, tr, icon, marker, base))
        x += icon + gap
    return x - gap - x0


def render_taskbar_mockup(values: list[tuple[float, float]],
                          width: int = 940, height: int = 56) -> QPixmap:
    """A representative slice of the Windows taskbar with the five overlay
    tiles embedded on the left of a fake chevron, tray, and clock."""
    pix = QPixmap(width, height)
    pix.fill(QColor(20, 22, 26))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    bar_y = 8
    bar_h = height - 16
    p.setPen(Qt.NoPen)
    p.setBrush(TASKBAR_BG)
    p.drawRect(0, bar_y, width, bar_h)

    icon = bar_h - 6
    gap, group_gap = 4, 10
    tray_x = width - 320
    n = len(GAUGES)
    row_w = icon * n + gap * (n - 1) + group_gap
    _draw_tile_row(p, values, tray_x - row_w - 12, bar_y + 3, icon,
                   gap, group_gap)

    # Fake chevron
    p.setFont(QFont("Segoe UI", int(icon * 0.5)))
    p.setPen(TASKBAR_DIM)
    p.drawText(QRectF(tray_x, bar_y, 24, bar_h), Qt.AlignCenter, "˄")

    # Fake tray icons (just simple shapes so the layout reads)
    tx = tray_x + 30
    for col in [QColor(120, 170, 220), QColor(180, 180, 190),
                QColor(180, 180, 190), QColor(140, 200, 180)]:
        p.setBrush(col); p.setPen(Qt.NoPen)
        p.drawRoundedRect(tx, int(bar_y + bar_h / 2 - 8), 16, 16, 2, 2)
        tx += 28

    # Fake clock
    p.setFont(QFont("Segoe UI", 9))
    p.setPen(TASKBAR_TEXT)
    p.drawText(QRectF(width - 110, bar_y, 96, bar_h / 2),
               Qt.AlignVCenter | Qt.AlignRight, "8:58 PM")
    p.drawText(QRectF(width - 110, bar_y + bar_h / 2, 96, bar_h / 2),
               Qt.AlignVCenter | Qt.AlignRight, "7/9/2026")
    p.end()
    return pix


# ---------------------------------------------------------------------------
# Hero: taskbar mockup on top + floating widget below, with group captions
# ---------------------------------------------------------------------------

# One value set for the whole hero so both surfaces agree. Chosen to show
# the full urgency gradient: blue / red / amber / red.
HERO = [
    ( 6.0, 62.0, "5d 8h"),    # GPT weekly  — under-utilizing (blue)
    (92.0, 40.0, "2h 0m"),    # 5h session  — will exhaust (red)
    (55.0, 60.0, "4d 4h"),    # 7d weekly   — burning fast (amber)
    (95.0, 39.0, "2d 17h"),   # 7d Fable    — pinned (red)
]


def render_hero() -> QPixmap:
    taskbar = render_taskbar_mockup([(v[0], v[1]) for v in HERO])
    widget = render_widget(HERO, scale=1.6)

    pad = 24
    W = max(taskbar.width(), widget.width()) + pad * 2
    H = taskbar.height() + widget.height() + pad * 3 + 66
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
               "in-taskbar overlay — ChatGPT (teal) | Claude (slate), "
               "right next to your tray")
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


# ---------------------------------------------------------------------------
# Timelapse GIF — a simulated day: session windows fill and reset, weekly
# meters creep upward, colors follow the urgency model, white rings drain.
# ---------------------------------------------------------------------------

def _session_cycle(t: float, phase: float, peak: float) -> tuple[float, float]:
    """(pct, time_remaining%) of a 5h window at normalized day-time t∈[0,1).
    The window resets `1/CYCLES` apart; usage climbs to ~peak within each."""
    CYCLES = 3
    local = ((t + phase) * CYCLES) % 1.0
    pct = min(100.0, peak * min(1.0, local * 1.35))
    return pct, (1.0 - local) * 100.0


def render_timelapse_frames(n_frames: int = 64) -> list[QPixmap]:
    frames = []
    for f in range(n_frames):
        t = f / n_frames
        c5, c5tr = _session_cycle(t, 0.55, 96.0)
        gw = 4.0 + 32.0 * t         # GPT weekly creeps 4 → 36
        cw = 20.0 + 38.0 * t        # Claude weekly 20 → 58
        fb = 40.0 + 56.0 * t        # Fable sprints 40 → 96 (green → red)
        wk_tr = 62.0 - 14.0 * t     # weekly windows drain a little
        values = [
            (gw, wk_tr, _fmt_reset(wk_tr, 168)),
            (c5, c5tr, _fmt_reset(c5tr, 5)),
            (cw, wk_tr, _fmt_reset(wk_tr, 168)),
            (fb, wk_tr, _fmt_reset(wk_tr, 168)),
        ]
        frames.append(render_widget(values, scale=1.3))
    return frames


def save_gif(frames: list[QPixmap], path: Path,
             ms_per_frame: int = 110) -> None:
    from PIL import Image
    imgs = []
    for pix in frames:
        buf = QBuffer()
        buf.open(QBuffer.ReadWrite)
        pix.save(buf, "PNG")
        img = Image.open(io.BytesIO(bytes(buf.data()))).convert("RGB")
        imgs.append(img.quantize(colors=128, dither=Image.Dither.NONE))
    imgs[0].save(
        path, save_all=True, append_images=imgs[1:],
        duration=ms_per_frame, loop=0, optimize=True,
    )


def main() -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    out = Path(__file__).resolve().parent
    render_hero().save(str(out / "hero.png"))
    save_gif(render_timelapse_frames(), out / "timelapse.gif")
    for f in sorted(list(out.glob("*.png")) + list(out.glob("*.gif"))):
        print(f"{f.name:24s}  {f.stat().st_size:>7} bytes")


if __name__ == "__main__":
    main()
