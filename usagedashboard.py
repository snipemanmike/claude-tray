"""
Always-on Claude Code usage dashboard.

Frameless transparent always-on-top window showing two ring gauges:
  - 5-hour session utilization
  - 7-day weekly utilization

Polls https://api.anthropic.com/api/oauth/usage using the OAuth token
stored locally by Claude Code at ~/.claude/.credentials.json.

Drag with left mouse anywhere on the widget. Right-click for menu.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QSystemTrayIcon,
    QWidget,
)


CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
STATE_PATH = Path.home() / ".claude" / ".usagedashboard.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
POLL_SECONDS = 60        # 1 min — feasible because we fall back to header probe on 429
MAX_BACKOFF_SECONDS = 1800   # 30 min ceiling on OAuth-endpoint backoff
HEADER_PROBE_MODEL = "claude-haiku-4-5-20251001"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
USER_AGENT = "usagedashboard/1.0"

# Visual palette
BG_COLOR = QColor(20, 22, 28, 200)
TRACK_COLOR = QColor(60, 65, 75, 180)
TEXT_COLOR = QColor(235, 235, 240)
SUB_COLOR = QColor(160, 165, 175)

# Ring colors keyed by utilization band
def ring_color(pct: float) -> QColor:
    if pct >= 90:
        return QColor(235, 80, 80)      # red
    if pct >= 70:
        return QColor(235, 175, 60)     # amber
    return QColor(110, 200, 140)        # green


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def read_token() -> str | None:
    try:
        data = json.loads(CREDS_PATH.read_text())
        return data["claudeAiOauth"]["accessToken"]
    except Exception:
        return None


def fetch_usage(token: str) -> tuple[dict | None, int | None]:
    """Hit the (free) OAuth metadata endpoint. Returns (data, status_code)."""
    try:
        r = requests.get(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": USER_AGENT,
            },
            timeout=10,
        )
        if r.status_code == 200:
            return r.json(), 200
        return None, r.status_code
    except Exception:
        return None, None


def fetch_usage_via_headers(token: str) -> tuple[dict | None, int | None]:
    """Fallback: send a max_tokens=1 ping to Haiku, read rate-limit headers.

    Costs ~9 tokens of your 5h quota per call (a tiny fraction of a percent).
    Used when the OAuth metadata endpoint is throttled.
    """
    body = json.dumps({
        "model": HEADER_PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    })
    try:
        r = requests.post(
            MESSAGES_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=15,
        )
        if r.status_code != 200:
            return None, r.status_code
        h = r.headers
        five = h.get("anthropic-ratelimit-unified-5h-utilization")
        seven = h.get("anthropic-ratelimit-unified-7d-utilization")
        if five is None and seven is None:
            return None, 200
        def _iso(epoch: str | None) -> str | None:
            if not epoch:
                return None
            try:
                return datetime.fromtimestamp(
                    int(epoch), timezone.utc).isoformat()
            except Exception:
                return None
        data = {
            "five_hour": {
                "utilization": float(five) * 100 if five else None,
                "resets_at": _iso(h.get("anthropic-ratelimit-unified-5h-reset")),
            },
            "seven_day": {
                "utilization": float(seven) * 100 if seven else None,
                "resets_at": _iso(h.get("anthropic-ratelimit-unified-7d-reset")),
            },
        }
        return data, 200
    except Exception:
        return None, None


def fmt_reset(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        # python's fromisoformat handles "+00:00" in 3.11+
        dt = datetime.fromisoformat(iso)
        delta = dt - datetime.now(timezone.utc)
        secs = int(delta.total_seconds())
        if secs <= 0:
            return "now"
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        if d:
            return f"{d}d {h}h"
        if h:
            return f"{h}h {m}m"
        return f"{m}m"
    except Exception:
        return "—"


class RingGauge:
    """Pure paint helper — no widget, just draws into a QPainter."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.pct: float | None = None
        self.reset_label = "—"

    def update(self, pct: float | None, reset_iso: str | None) -> None:
        self.pct = pct
        self.reset_label = fmt_reset(reset_iso)

    def paint(self, p: QPainter, rect: QRectF) -> None:
        # Ring
        size = min(rect.width(), rect.height() - 22)
        ring_rect = QRectF(
            rect.center().x() - size / 2,
            rect.top() + 4,
            size,
            size,
        )
        thickness = max(6, size * 0.12)
        pen_track = QPen(TRACK_COLOR, thickness, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen_track)
        p.setBrush(Qt.NoBrush)
        p.drawArc(ring_rect.adjusted(thickness/2, thickness/2, -thickness/2, -thickness/2),
                  0, 360 * 16)

        pct = 0.0 if self.pct is None else max(0.0, min(100.0, self.pct))
        pen_val = QPen(ring_color(pct), thickness, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen_val)
        # Qt arcs: start at 3 o'clock (0°), positive = counter-clockwise.
        # Start at 12 (90°), sweep clockwise (negative angle).
        span = int(-pct / 100.0 * 360 * 16)
        p.drawArc(
            ring_rect.adjusted(thickness/2, thickness/2, -thickness/2, -thickness/2),
            90 * 16,
            span,
        )

        # Center percentage
        font = QFont()
        font.setPointSizeF(max(10.0, size * 0.22))
        font.setBold(True)
        p.setFont(font)
        p.setPen(TEXT_COLOR)
        text = "—" if self.pct is None else f"{int(round(pct))}%"
        p.drawText(ring_rect, Qt.AlignCenter, text)

        # Label above ring (small)
        sub = QFont()
        sub.setPointSizeF(max(7.0, size * 0.10))
        p.setFont(sub)
        p.setPen(SUB_COLOR)
        label_rect = QRectF(rect.left(), rect.top() - 14, rect.width(), 14)
        p.drawText(label_rect, Qt.AlignCenter, self.label)

        # Reset countdown below ring
        reset_rect = QRectF(rect.left(), ring_rect.bottom() + 2, rect.width(), 18)
        p.drawText(reset_rect, Qt.AlignCenter, f"resets {self.reset_label}")


class Widget(QWidget):
    refreshed = Signal()

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_AlwaysShowToolTips, True)
        self.setMinimumSize(QSize(220, 130))

        self.gauge_5h = RingGauge("5h session")
        self.gauge_7d = RingGauge("7d weekly")
        self.last_error: str | None = None
        self.last_fetch_ts: float = 0.0
        self.token = read_token()

        state = load_state()
        x = state.get("x")
        y = state.get("y")
        self.resize(state.get("w", 260), state.get("h", 150))
        self._opacity = state.get("opacity", 0.92)
        self.setWindowOpacity(self._opacity)
        if x is not None and y is not None and self._point_on_some_screen(x, y):
            self.move(x, y)
        else:
            # Place near bottom-right of whichever screen the cursor is on
            cursor_pos = QCursor.pos()
            screen = QGuiApplication.screenAt(cursor_pos) or QGuiApplication.primaryScreen()
            geo = screen.availableGeometry()
            self.move(geo.right() - self.width() - 16,
                      geo.bottom() - self.height() - 16)

        self._drag_offset: QPoint | None = None
        self._visible_pref: bool = bool(state.get("visible", True))
        self._backoff_steps: int = 0
        self._oauth_skip_until: float = 0.0
        self._last_method: str = ""
        self.tray_5h: QSystemTrayIcon | None = None
        self.tray_7d: QSystemTrayIcon | None = None
        self.setWindowTitle("Claude usage")

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_now)
        self._timer.start(POLL_SECONDS * 1000)

        # Repaint countdown labels once per second without re-polling
        self._tick = QTimer(self)
        self._tick.timeout.connect(self.update)
        self._tick.start(1000)

        QTimer.singleShot(50, self.refresh_now)

    # --- painting ----------------------------------------------------------
    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        bg = self.rect().adjusted(0, 0, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(BG_COLOR))
        p.drawRoundedRect(bg, 14, 14)

        # Layout: two gauges side by side, with label area above
        inner = bg.adjusted(10, 22, -10, -10)
        half_w = inner.width() / 2
        left = QRectF(inner.left(), inner.top(), half_w - 4, inner.height())
        right = QRectF(inner.left() + half_w + 4, inner.top(),
                       half_w - 4, inner.height())
        self.gauge_5h.paint(p, left)
        self.gauge_7d.paint(p, right)

        if self.last_error:
            err_font = QFont()
            err_font.setPointSizeF(7.5)
            p.setFont(err_font)
            p.setPen(QColor(235, 120, 120))
            err_rect = QRectF(bg.left() + 6, bg.bottom() - 12,
                              bg.width() - 12, 10)
            p.drawText(err_rect, Qt.AlignLeft | Qt.AlignVCenter, self.last_error)

        p.end()

    # --- polling -----------------------------------------------------------
    def refresh_now(self) -> None:
        if not self.token:
            self.token = read_token()
        if not self.token:
            self.last_error = "no creds at ~/.claude/.credentials.json"
            self.update()
            return

        now = time.time()
        data: dict | None = None
        status: int | None = None
        used_oauth = False

        # Primary: free OAuth metadata endpoint (unless we know it's locked)
        if now >= self._oauth_skip_until:
            data, status = fetch_usage(self.token)
            used_oauth = True
            if data is None and status == 429:
                # Endpoint is throttled — skip it for the backoff window
                self._backoff_steps += 1
                delay = min(
                    POLL_SECONDS * (2 ** (self._backoff_steps - 1)),
                    MAX_BACKOFF_SECONDS,
                )
                self._oauth_skip_until = now + delay

        # Fallback: header probe via a tiny Haiku ping (costs ~9 tokens)
        if data is None:
            data, status = fetch_usage_via_headers(self.token)
            if data is not None:
                self._last_method = "header probe"
        elif used_oauth:
            self._last_method = "oauth metadata"

        if data is None:
            self._handle_fetch_error(status)
            return

        # Success — if OAuth was the source, clear its backoff
        if used_oauth and self._backoff_steps:
            self._backoff_steps = 0
            self._oauth_skip_until = 0.0
        # Always keep the timer at the normal cadence on success
        self._timer.start(POLL_SECONDS * 1000)
        self.last_error = (
            "using header probe (oauth throttled)"
            if self._last_method == "header probe" else None
        )
        self.last_fetch_ts = time.time()

        five = data.get("five_hour") or {}
        seven = data.get("seven_day") or {}
        self.gauge_5h.update(five.get("utilization"), five.get("resets_at"))
        self.gauge_7d.update(seven.get("utilization"), seven.get("resets_at"))

        tooltip = self._tooltip(data)
        self.setToolTip(tooltip)
        pct5 = five.get("utilization")
        pct7 = seven.get("utilization")
        if self.tray_5h is not None:
            self.tray_5h.setIcon(make_tray_icon("5H", pct5))
            reset5 = fmt_reset(five.get("resets_at"))
            self.tray_5h.setToolTip(
                f"5-hour session: {pct5:.0f}% — resets in {reset5}"
                if pct5 is not None else "5-hour session: —"
            )
        if self.tray_7d is not None:
            self.tray_7d.setIcon(make_tray_icon("7D", pct7))
            reset7 = fmt_reset(seven.get("resets_at"))
            self.tray_7d.setToolTip(
                f"7-day weekly: {pct7:.0f}% — resets in {reset7}"
                if pct7 is not None else "7-day weekly: —"
            )
        self.update()

    def _tooltip(self, data: dict) -> str:
        lines = []
        for key in ("five_hour", "seven_day", "seven_day_opus",
                    "seven_day_sonnet"):
            block = data.get(key)
            if isinstance(block, dict) and block.get("utilization") is not None:
                lines.append(f"{key}: {block['utilization']:.1f}%  "
                             f"resets {fmt_reset(block.get('resets_at'))}")
        extra = data.get("extra_usage")
        if isinstance(extra, dict) and extra.get("is_enabled"):
            lines.append(f"extra: {extra.get('utilization')}% of "
                         f"{extra.get('monthly_limit')} {extra.get('currency')}")
        return "\n".join(lines) or "no usage data"

    # --- mouse: drag + context menu ----------------------------------------
    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()
        elif e.button() == Qt.RightButton:
            self._show_menu(e.globalPosition().toPoint())
            e.accept()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        if self._drag_offset is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)
            e.accept()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if e.button() == Qt.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            self._persist()
            e.accept()

    def wheelEvent(self, e) -> None:
        # Scroll to resize
        delta = e.angleDelta().y() / 120
        scale = 1.0 + (0.07 * delta)
        new_w = max(180, min(520, int(self.width() * scale)))
        new_h = max(110, min(320, int(self.height() * scale)))
        self.resize(new_w, new_h)
        self._persist()
        e.accept()

    def _show_menu(self, global_pos: QPoint) -> None:
        m = QMenu(self)
        m.addAction("Refresh now", self.refresh_now)
        m.addSeparator()
        for label, val in [("Opacity 50%", 0.5), ("Opacity 75%", 0.75),
                           ("Opacity 92%", 0.92), ("Opacity 100%", 1.0)]:
            act = QAction(label, m)
            act.triggered.connect(lambda _=False, v=val: self._set_opacity(v))
            m.addAction(act)
        m.addSeparator()
        m.addAction("Quit", QApplication.instance().quit)
        m.exec(global_pos)

    def _set_opacity(self, v: float) -> None:
        self._opacity = v
        self.setWindowOpacity(v)
        self._persist()

    def _handle_fetch_error(self, status: int | None) -> None:
        """Both primary + fallback failed. Stay on normal cadence; show why."""
        if status == 429:
            self.last_error = "rate-limited (both endpoints) — retrying"
        elif status in (401, 403):
            self.last_error = "auth failed — open Claude Code to refresh"
        elif status is not None:
            self.last_error = f"HTTP {status}"
        else:
            self.last_error = "network error"
        # Keep gauges showing the last good reading.
        self.update()

    def toggle_visible(self) -> None:
        self._visible_pref = not self.isVisible()
        self.setVisible(self._visible_pref)
        self._persist()

    @staticmethod
    def _point_on_some_screen(x: int, y: int) -> bool:
        # Anchor by the centre of the widget rather than the top-left corner
        # so a window that's mostly on-screen still counts.
        return QGuiApplication.screenAt(QPoint(x + 20, y + 20)) is not None

    def _persist(self) -> None:
        save_state({
            "x": self.x(), "y": self.y(),
            "w": self.width(), "h": self.height(),
            "opacity": self._opacity,
            "visible": self._visible_pref,
        })


def make_tray_pixmap(label: str, pct: float | None, size: int = 16) -> QPixmap:
    """Render a tray icon natively at the requested size.

    Design:
      - dark rounded base
      - severity-coloured bar fills the icon from the bottom by `pct`
      - bold label on top with a dark outline so it stays readable
        whether it lands over the dark base or the colored fill
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    radius = max(1, size // 8)

    # Dark base
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(40, 44, 52))
    p.drawRoundedRect(0, 0, size, size, radius, radius)

    # Severity-colored fill from bottom (uses full vertical space)
    if pct is not None:
        v = max(0.0, min(100.0, pct))
        fill_h = max(1 if v > 0 else 0, int(round(size * v / 100)))
        if fill_h > 0:
            p.setBrush(ring_color(v))
            # Clip to the rounded rect so fill respects the rounding
            p.save()
            clip = QPainterPath()
            clip.addRoundedRect(0, 0, size, size, radius, radius)
            p.setClipPath(clip)
            p.drawRect(0, size - fill_h, size, fill_h)
            p.restore()

    # Label with manual outline for readability over any fill color
    f = QFont()
    f.setBold(True)
    f.setPointSizeF(size * 0.62)
    p.setFont(f)
    rect = QRectF(0, -1, size, size)
    outline = QColor(0, 0, 0, 220)
    p.setPen(outline)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        p.drawText(rect.translated(dx, dy), Qt.AlignCenter, label)
    p.setPen(QColor(245, 247, 252))
    p.drawText(rect, Qt.AlignCenter, label)

    p.end()
    return pix


def make_tray_icon(label: str, pct: float | None) -> QIcon:
    """Multi-resolution icon so Windows can pick the closest match without scaling."""
    icon = QIcon()
    for sz in (16, 20, 24, 32, 40, 48):
        icon.addPixmap(make_tray_pixmap(label, pct, sz))
    return icon


def _wire_tray(tray: QSystemTrayIcon, widget: "Widget", app: QApplication) -> None:
    menu = QMenu()
    show_hide = QAction("Show / hide widget", menu)
    show_hide.triggered.connect(widget.toggle_visible)
    menu.addAction(show_hide)
    menu.addAction("Refresh now", widget.refresh_now)
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: widget.toggle_visible()
        if reason == QSystemTrayIcon.Trigger else None
    )
    tray.show()


def make_tray_icons(
    app: QApplication, widget: "Widget"
) -> tuple[QSystemTrayIcon, QSystemTrayIcon]:
    """Two side-by-side tray icons: 5h session on the left, 7d weekly on the right."""
    tray_5h = QSystemTrayIcon(make_tray_icon("5H", None), app)
    tray_7d = QSystemTrayIcon(make_tray_icon("7D", None), app)
    tray_5h.setToolTip("Claude Code — 5-hour session (loading…)")
    tray_7d.setToolTip("Claude Code — 7-day weekly (loading…)")
    _wire_tray(tray_5h, widget, app)
    _wire_tray(tray_7d, widget, app)
    return tray_5h, tray_7d


def _set_aumid() -> None:
    """Tell Windows this is a distinct app — improves tray icon identity."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ClaudeUsageDashboard.App.1"
        )
    except Exception:
        pass


def main() -> int:
    _set_aumid()
    app = QApplication(sys.argv)
    app.setApplicationName("Claude Usage Dashboard")
    app.setOrganizationName("local")
    app.setQuitOnLastWindowClosed(False)
    w = Widget()
    tray_5h, tray_7d = make_tray_icons(app, w)
    w.tray_5h = tray_5h
    w.tray_7d = tray_7d
    if w._visible_pref:
        w.show()
    # Keep references so the tray icons aren't garbage collected
    app._tray_5h = tray_5h  # type: ignore[attr-defined]
    app._tray_7d = tray_7d  # type: ignore[attr-defined]
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
