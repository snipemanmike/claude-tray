"""
Always-on Claude Code usage dashboard.

Two surfaces, same visual language:
  - Five taskbar tiles — Claude (5h session, 7d weekly, 7d Fable) and
    OpenAI/ChatGPT (5h, weekly, read from local Codex CLI session logs) —
    with the % in the centre coloured by an urgency model
    (pct + time_remaining% − 100) and a white perimeter ring that drains
    as the reset window elapses. Providers are demarcated by tile base
    colour (slate vs teal), marker letters, and a wider gap.
  - Frameless transparent always-on-top widget mirroring the same ring
    behaviour at a larger size, with reset countdowns and labels.

Polls https://api.anthropic.com/api/oauth/usage with a max_tokens=1
Haiku ping as fallback when the OAuth endpoint 429s. OAuth credential
is read from ~/.claude/.credentials.json (refreshed by Claude Code).

Drag the widget with left mouse anywhere; right-click for menu.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from PySide6.QtCore import (
    QAbstractNativeEventFilter,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
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
TOKEN_URL = "https://claude.ai/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code public client_id
USER_AGENT = "usagedashboard/1.0"

# Visual palette
BG_COLOR = QColor(20, 22, 28, 200)
TRACK_COLOR = QColor(60, 65, 75, 180)
TEXT_COLOR = QColor(235, 235, 240)
SUB_COLOR = QColor(160, 165, 175)

# Severity colors keyed by an urgency band.
BLUE  = QColor( 95, 175, 240)   # under-utilizing — plenty of headroom
GREEN = QColor(110, 200, 140)   # on pace — maximizing compute-to-cost
AMBER = QColor(235, 175,  60)   # burning faster than reset can save you
RED   = QColor(235,  80,  80)   # will exhaust before the window resets

# Provider demarcation: gauges [:GROUP_SPLIT] are Claude, the rest OpenAI.
# OpenAI tiles get a teal-dark base (vs Claude's neutral slate) and both
# surfaces put a wider gap / divider line between the two groups.
GROUP_SPLIT = 3
CLAUDE_TILE_BASE = QColor(40, 44, 52)
OPENAI_TILE_BASE = QColor(22, 48, 42)


# Urgency-to-colour gradient anchors. The diagonals on the (time_rem, pct)
# plane are pct + time_rem = constant. urgency = pct + time_rem − 100.
URGENCY_BLUE_ANCHOR  = -25.0   # pct + time_remaining = 75 — under-utilizing
URGENCY_AMBER_ANCHOR =  15.0   # urgency value at peak amber
URGENCY_RED_ANCHOR   =  40.0   # urgency value at pure red (and beyond)


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(round(c1.red()   + (c2.red()   - c1.red())   * t)),
        int(round(c1.green() + (c2.green() - c1.green()) * t)),
        int(round(c1.blue()  + (c2.blue()  - c1.blue())  * t)),
    )


def urgency_color(pct: float | None, time_rem_pct: float | None) -> QColor:
    """Continuous-gradient severity color along the urgency axis.

      urgency = pct + time_remaining% − 100   (= pct − elapsed%)

      ≤ URGENCY_BLUE_ANCHOR ...... pure BLUE   (under-utilizing — slack on the table)
      BLUE → 0 .................. smoothly blue→green
      = 0 ........................ pure GREEN  (on pace — maximizing compute/cost)
      0 → URGENCY_AMBER_ANCHOR ... smoothly green→amber
      AMBER → URGENCY_RED_ANCHOR . smoothly amber→red
      ≥ URGENCY_RED_ANCHOR ....... pure RED    (will exhaust before reset)

    If `time_rem_pct` is unavailable, falls back to raw % bucketing.
    """
    if pct is None:
        return GREEN
    if time_rem_pct is None:
        if pct >= 90: return RED
        if pct >= 70: return AMBER
        if pct <= 10: return BLUE
        return GREEN
    urgency = pct + time_rem_pct - 100.0
    if urgency >= URGENCY_RED_ANCHOR:
        return RED
    if urgency <= URGENCY_BLUE_ANCHOR:
        return BLUE
    if urgency >= URGENCY_AMBER_ANCHOR:
        return _lerp_color(
            AMBER, RED,
            (urgency - URGENCY_AMBER_ANCHOR) /
            (URGENCY_RED_ANCHOR - URGENCY_AMBER_ANCHOR),
        )
    if urgency >= 0:
        return _lerp_color(GREEN, AMBER, urgency / URGENCY_AMBER_ANCHOR)
    # urgency in (BLUE_ANCHOR, 0): blue → green
    return _lerp_color(
        BLUE, GREEN,
        (urgency - URGENCY_BLUE_ANCHOR) / (0.0 - URGENCY_BLUE_ANCHOR),
    )


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


def _credentials_expired() -> bool:
    """True if the on-disk access token is past (or within 60s of) its expiry.

    Used to decide whether *we* should refresh, or whether the 401 is more
    likely a transient hiccup and we should leave Claude Code's tokens alone.
    """
    try:
        data = json.loads(CREDS_PATH.read_text())
        expires_at_ms = int(data["claudeAiOauth"]["expiresAt"])
        return time.time() * 1000 > (expires_at_ms - 60_000)
    except Exception:
        return False


def refresh_access_token() -> str | None:
    """Exchange the on-disk refresh_token for a new access_token.

    Writes the new credentials back to ~/.claude/.credentials.json so the
    Claude Code CLI/IDE pick up the same fresh token. Returns the new access
    token on success, None on any failure (network, malformed creds, etc.).
    """
    try:
        creds = json.loads(CREDS_PATH.read_text())
        refresh_token = creds["claudeAiOauth"]["refreshToken"]
    except Exception:
        return None
    try:
        r = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        payload = r.json()
    except Exception:
        return None
    new_access = payload.get("access_token")
    if not new_access:
        return None
    # Persist the rotated credentials. Best-effort — if the file is locked by
    # Claude Code at the same instant we just keep the new token in memory.
    try:
        creds["claudeAiOauth"]["accessToken"] = new_access
        creds["claudeAiOauth"]["refreshToken"] = (
            payload.get("refresh_token") or refresh_token
        )
        # expires_in defaults to 10h (36000s) to match observed Claude lifetimes
        creds["claudeAiOauth"]["expiresAt"] = (
            int(time.time() * 1000) + int(payload.get("expires_in", 36000)) * 1000
        )
        CREDS_PATH.write_text(json.dumps(creds, indent=2))
    except Exception:
        pass
    return new_access


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


CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def fetch_openai_usage() -> dict | None:
    """Newest ChatGPT plan rate-limit snapshot from Codex CLI session logs.

    Codex CLI embeds a `rate_limits` block in the token_count events it
    appends to ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl:

        {"primary":   {"used_percent": 8.0, "window_minutes": 300,
                       "resets_at": <epoch s>},          # 5h window
         "secondary":  {... "window_minutes": 10080 ...}, # weekly
         "plan_type": "prolite"}

    These are the same plan-level meters ChatGPT Settings -> Usage shows.
    Reading the tail of recent files is free, offline and unthrottleable;
    the trade-off is freshness — the snapshot is only as recent as the
    last Codex activity (ChatGPT app usage between Codex runs is unseen).
    """
    try:
        files = sorted(
            CODEX_SESSIONS_DIR.glob("*/*/*/rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )[:8]
    except OSError:
        return None
    for f in files:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 262144))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                rl = json.loads(line)["payload"]["rate_limits"]
            except (ValueError, KeyError, TypeError):
                continue
            if isinstance(rl, dict) and isinstance(rl.get("primary"), dict):
                return rl
    return None


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


def _fable_limit(data: dict) -> dict | None:
    """Extract the Fable weekly-scoped cap from a usage payload.

    The OAuth endpoint reports per-model weekly limits as entries in the
    `limits` array (kind 'weekly_scoped'), tagged by scope.model.display_name.
    The top-level seven_day_* keys are all null for these, so this array is the
    only reliable source. Returns the raw limit dict (carries 'percent' and
    'resets_at') or None when Fable is absent — e.g. the header-probe fallback,
    whose data has no `limits` array at all.
    """
    limits = data.get("limits")
    if not isinstance(limits, list):
        return None
    for item in limits:
        if not isinstance(item, dict):
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if isinstance(model, dict) and model.get("display_name") == "Fable":
            return item
    return None


class RingGauge:
    """Pure paint helper — no widget, just draws into a QPainter."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.pct: float | None = None
        self.time_rem_pct: float | None = None
        self.reset_label = "—"
        self.reset_iso: str | None = None   # kept so the reading can be cached

    def update(self, pct: float | None, reset_iso: str | None,
               time_rem_pct: float | None) -> None:
        self.pct = pct
        self.time_rem_pct = time_rem_pct
        self.reset_label = fmt_reset(reset_iso)
        self.reset_iso = reset_iso

    def paint(self, p: QPainter, rect: QRectF) -> None:
        # Ring drains with TIME REMAINING (matches the tray-icon behavior).
        size = min(rect.width(), rect.height() - 22)
        ring_rect = QRectF(
            rect.center().x() - size / 2,
            rect.top() + 4,
            size,
            size,
        )
        thickness = max(6, size * 0.12)
        ring_inner = ring_rect.adjusted(
            thickness / 2, thickness / 2, -thickness / 2, -thickness / 2
        )

        # Track
        p.setPen(QPen(TRACK_COLOR, thickness, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawArc(ring_inner, 0, 360 * 16)

        # Time-remaining arc (white) — matches the tray icon
        if self.time_rem_pct is not None:
            t = max(0.0, min(100.0, self.time_rem_pct))
            p.setPen(QPen(TEXT_COLOR, thickness, Qt.SolidLine, Qt.RoundCap))
            # Start at 12 o'clock, sweep clockwise as time elapses
            p.drawArc(ring_inner, 90 * 16, int(-(t / 100.0) * 360 * 16))

        # Center percentage — coloured by urgency (matches tray number)
        pct = 0.0 if self.pct is None else max(0.0, min(100.0, self.pct))
        font = QFont()
        font.setPointSizeF(max(10.0, size * 0.22))
        font.setBold(True)
        p.setFont(font)
        text = "—" if self.pct is None else f"{int(round(pct))}%"
        p.setPen(urgency_color(pct, self.time_rem_pct))
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
        # Don't steal focus when shown — otherwise the taskbar overlay gets
        # demoted in the topmost band and the Windows taskbar paints over it.
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # Min width holds each of the five gauges at ~90px.
        self.setMinimumSize(QSize(470, 130))

        self.gauge_5h = RingGauge("5h session")
        self.gauge_7d = RingGauge("7d weekly")
        self.gauge_fable = RingGauge("7d Fable")
        self.gauge_gpt5h = RingGauge("5h GPT")
        self.gauge_gptw = RingGauge("7d GPT")
        # Paint/taskbar order with per-tile marker; [:GROUP_SPLIT] = Claude.
        self._all_gauges: list[tuple[RingGauge, str]] = [
            (self.gauge_5h, "h"), (self.gauge_7d, "d"),
            (self.gauge_fable, "fd"),
            (self.gauge_gpt5h, "g"), (self.gauge_gptw, "gw"),
        ]
        self.last_error: str | None = None
        self.last_fetch_ts: float = 0.0
        self.token = read_token()

        state = load_state()

        # Seed Fable from its last cached reading. Its only source is the
        # aggressively-throttled OAuth metadata endpoint (the header-probe
        # fallback can't see it), so without this a restart during a throttle
        # window shows blank. Weekly usage barely moves, so a recent cached
        # value is accurate; skip it only if its window has already elapsed.
        cached_fpct = state.get("fable_pct")
        cached_freset = state.get("fable_resets_at")
        if cached_fpct is not None and cached_freset:
            trf = time_remaining_pct(cached_freset, 7 * 86400)
            if trf:  # non-zero/non-None => window still open
                self.gauge_fable.update(cached_fpct, cached_freset, trf)

        x = state.get("x")
        y = state.get("y")
        self.resize(state.get("w", 520), state.get("h", 150))
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
        self._consecutive_429: int = 0
        self.tray_5h: QSystemTrayIcon | None = None
        self.tray_7d: QSystemTrayIcon | None = None
        self.tray_fable: QSystemTrayIcon | None = None
        self.taskbar: TaskbarWidget | None = None
        self.setWindowTitle("Claude usage")

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_now)
        self._timer.start(POLL_SECONDS * 1000)

        # Repaint countdown labels once per second without re-polling. Also
        # re-push current gauge data into the taskbar overlay every tick so
        # the two surfaces can't drift out of sync after a lock/unlock or any
        # other event that disrupted a set_data call.
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._tick_repaint)
        self._tick.start(1000)

        QTimer.singleShot(50, self.refresh_now)

    def _taskbar_entries(self) -> list[tuple[float | None, float | None, str]]:
        return [(g.pct, g.time_rem_pct, m) for g, m in self._all_gauges]

    def _tick_repaint(self) -> None:
        self.update()
        if self.taskbar is not None:
            self.taskbar.set_data(self._taskbar_entries())

    # --- painting ----------------------------------------------------------
    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        bg = self.rect().adjusted(0, 0, -1, -1)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(BG_COLOR))
        p.drawRoundedRect(bg, 14, 14)

        # Layout: five gauges side by side (Claude trio | OpenAI pair) with
        # label area above and a divider line between the provider groups.
        inner = bg.adjusted(10, 22, -10, -10)
        gauges = [g for g, _ in self._all_gauges]
        gap, group_gap = 6, 14
        n = len(gauges)
        col_w = (inner.width() - gap * (n - 1) - group_gap) / n
        x = inner.left()
        for i, gauge in enumerate(gauges):
            if i == GROUP_SPLIT:
                div_x = x + group_gap / 2 - gap / 2
                p.setPen(QPen(TRACK_COLOR, 1))
                p.drawLine(int(div_x), int(inner.top() - 8),
                           int(div_x), int(inner.bottom()))
                x += group_gap
            gauge.paint(p, QRectF(x, inner.top(), col_w, inner.height()))
            x += col_w + gap

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
    def _update_openai(self) -> None:
        """Refresh the two GPT gauges from the local Codex session logs.

        Free and offline, so it runs every poll regardless of how the
        Anthropic endpoints are doing.
        """
        rl = fetch_openai_usage()
        if not rl:
            return
        now = time.time()
        for gauge, block in ((self.gauge_gpt5h, rl.get("primary")),
                             (self.gauge_gptw, rl.get("secondary"))):
            if not isinstance(block, dict):
                continue
            resets_epoch = block.get("resets_at")
            window_s = (block.get("window_minutes") or 0) * 60
            if resets_epoch and resets_epoch < now:
                # The snapshot predates a window reset and Codex hasn't run
                # since, so nothing has been used in the current window.
                # (ChatGPT-app usage in the meantime is invisible to us.)
                gauge.update(0.0, None, None)
                continue
            iso = None
            if resets_epoch:
                iso = datetime.fromtimestamp(
                    resets_epoch, timezone.utc).isoformat()
            gauge.update(
                block.get("used_percent"), iso,
                time_remaining_pct(iso, window_s) if window_s else None,
            )

    def refresh_now(self, manual: bool = False) -> None:
        self._update_openai()
        if not self.token:
            self.token = read_token()
        if not self.token:
            self.last_error = "no creds at ~/.claude/.credentials.json"
            self.update()
            return

        # User-initiated refresh: clear any OAuth backoff so we really do
        # try both endpoints fresh. Important when the window just reset
        # and the user wants to see new values immediately.
        if manual:
            self._backoff_steps = 0
            self._oauth_skip_until = 0.0
            self._consecutive_429 = 0

        data, status = self._fetch(allow_refresh=True)
        if data is None:
            self._handle_fetch_error(status)
            return

        # Successful fetch — clear the rate-limit streak counter
        self._consecutive_429 = 0

        # Always keep the timer at the normal cadence on success
        self._timer.start(POLL_SECONDS * 1000)
        self.last_error = (
            "using header probe (oauth throttled)"
            if self._last_method == "header probe" else None
        )
        self.last_fetch_ts = time.time()

        five = data.get("five_hour") or {}
        seven = data.get("seven_day") or {}
        pct5 = five.get("utilization")
        pct7 = seven.get("utilization")
        tr5 = time_remaining_pct(five.get("resets_at"), 5 * 3600)
        tr7 = time_remaining_pct(seven.get("resets_at"), 7 * 86400)
        self.gauge_5h.update(pct5, five.get("resets_at"), tr5)
        self.gauge_7d.update(pct7, seven.get("resets_at"), tr7)

        # Fable's weekly-scoped cap lives in the `limits` array, not a
        # top-level key, and is absent from header-probe data — leave the last
        # reading in place when this poll didn't carry it.
        fable = _fable_limit(data)
        if fable is not None:
            fable_reset = fable.get("resets_at")
            self.gauge_fable.update(
                fable.get("percent"), fable_reset,
                time_remaining_pct(fable_reset, 7 * 86400),
            )
            # Cache it so it survives OAuth throttle windows and restarts.
            self._persist()
        pctf = self.gauge_fable.pct
        trf = self.gauge_fable.time_rem_pct
        resetf = self.gauge_fable.reset_label

        tooltip = self._tooltip(data)
        self.setToolTip(tooltip)
        reset5 = fmt_reset(five.get("resets_at"))
        reset7 = fmt_reset(seven.get("resets_at"))
        if self.taskbar is not None:
            self.taskbar.set_data(self._taskbar_entries())
            tip = []
            if pct5 is not None:
                tip.append(f"5-hour session: {pct5:.0f}% — resets in {reset5}")
            if pct7 is not None:
                tip.append(f"7-day weekly: {pct7:.0f}% — resets in {reset7}")
            if pctf is not None:
                tip.append(f"7-day Fable: {pctf:.0f}% — resets in {resetf}")
            for gauge, name in ((self.gauge_gpt5h, "GPT 5-hour"),
                                (self.gauge_gptw, "GPT weekly")):
                if gauge.pct is not None:
                    tip.append(f"{name}: {gauge.pct:.0f}% — resets in "
                               f"{gauge.reset_label}")
            self.taskbar.setToolTip("\n".join(tip) or "loading…")
        if self.tray_5h is not None:
            self.tray_5h.setIcon(make_tray_icon(pct5, tr5, marker="h"))
            self.tray_5h.setToolTip(
                f"5-hour session: {pct5:.0f}% — resets in {reset5}"
                if pct5 is not None else "5-hour session: —"
            )
        if self.tray_7d is not None:
            self.tray_7d.setIcon(make_tray_icon(pct7, tr7, marker="d"))
            self.tray_7d.setToolTip(
                f"7-day weekly: {pct7:.0f}% — resets in {reset7}"
                if pct7 is not None else "7-day weekly: —"
            )
        if self.tray_fable is not None:
            self.tray_fable.setIcon(make_tray_icon(pctf, trf, marker="fd"))
            self.tray_fable.setToolTip(
                f"7-day Fable: {pctf:.0f}% — resets in {resetf}"
                if pctf is not None else "7-day Fable: —"
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
        fable = _fable_limit(data)
        if fable is not None and fable.get("percent") is not None:
            lines.append(f"fable (7d): {fable['percent']}%  "
                         f"resets {fmt_reset(fable.get('resets_at'))}")
        for gauge, name in ((self.gauge_gpt5h, "gpt (5h)"),
                            (self.gauge_gptw, "gpt (7d)")):
            if gauge.pct is not None:
                lines.append(f"{name}: {gauge.pct:.1f}%  "
                             f"resets {gauge.reset_label}")
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
        m.addAction("Hide widget", self.toggle_visible)
        m.addAction("Refresh now", lambda: self.refresh_now(manual=True))
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

    def _fetch(self, allow_refresh: bool) -> tuple[dict | None, int | None]:
        """One end-to-end attempt: OAuth metadata → header probe → refresh-and-retry.

        Caller passes allow_refresh=False on the recursive retry so we don't
        loop indefinitely on a permanently revoked token.
        """
        now = time.time()
        data: dict | None = None
        status: int | None = None
        used_oauth = False

        # Primary: free OAuth metadata endpoint (unless we know it's locked)
        if now >= self._oauth_skip_until:
            data, status = fetch_usage(self.token)
            used_oauth = True
            if data is None and status == 429:
                self._backoff_steps += 1
                delay = min(
                    POLL_SECONDS * (2 ** (self._backoff_steps - 1)),
                    MAX_BACKOFF_SECONDS,
                )
                self._oauth_skip_until = now + delay

        # Fallback: header probe via a tiny Haiku ping
        if data is None and status != 401 and status != 403:
            data, status = fetch_usage_via_headers(self.token)

        # 401/403 — try to recover without stepping on Claude Code's refresh
        if data is None and status in (401, 403) and allow_refresh:
            # Step 1: maybe Claude Code already refreshed; re-read disk first.
            disk_token = read_token()
            if disk_token and disk_token != self.token:
                self.token = disk_token
                return self._fetch(allow_refresh=False)
            # Step 2: only do our own refresh if the token is actually expired.
            # This avoids rotating the refresh_token out from under Claude Code
            # when our in-memory token is fine but the endpoint hiccuped.
            if _credentials_expired():
                new_token = refresh_access_token()
                if new_token:
                    self.token = new_token
                    return self._fetch(allow_refresh=False)

        if data is not None:
            # Source of truth for "what worked this poll"
            self._last_method = "oauth metadata" if used_oauth else "header probe"
            # Clear OAuth backoff state on a successful primary call
            if used_oauth and self._backoff_steps:
                self._backoff_steps = 0
                self._oauth_skip_until = 0.0
        return data, status

    def _handle_fetch_error(self, status: int | None) -> None:
        """Both primary + fallback failed. Stay on normal cadence; show why.
        If we keep getting 429 from both endpoints AND a recent successful
        poll showed we were already running hot, infer we've hit the limit
        and force the display to 100% — the rate-limit IS the signal."""
        if status == 429:
            self._consecutive_429 += 1
            self.last_error = "rate-limited (both endpoints) — retrying"
            if self._consecutive_429 >= 2:
                self._infer_at_limit()
        elif status in (401, 403):
            self.last_error = "auth failed — open Claude Code to refresh"
        elif status is not None:
            self.last_error = f"HTTP {status}"
        else:
            self.last_error = "network error"
        # Keep gauges showing the last good reading (or the inferred 100%).
        self.update()

    def _infer_at_limit(self) -> None:
        """If the last good poll showed >= 85 % on a limit, two consecutive
        rate-limit errors almost certainly mean we hit that limit. Bump the
        display to 100 % so the user sees the actual state."""
        pct5 = self.gauge_5h.pct
        pct7 = self.gauge_7d.pct
        pctf = self.gauge_fable.pct
        changed = False
        if pct5 is not None and pct5 >= 85.0 and pct5 < 100.0:
            self.gauge_5h.pct = 100.0
            changed = True
        if pct7 is not None and pct7 >= 85.0 and pct7 < 100.0:
            self.gauge_7d.pct = 100.0
            changed = True
        if pctf is not None and pctf >= 85.0 and pctf < 100.0:
            self.gauge_fable.pct = 100.0
            changed = True
        if changed and self.taskbar is not None:
            self.taskbar.set_data(self._taskbar_entries())

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
        state = load_state()
        state.update({
            "x": self.x(), "y": self.y(),
            "w": self.width(), "h": self.height(),
            "opacity": self._opacity,
            "visible": self._visible_pref,
        })
        # Only overwrite the cached Fable reading when we actually hold one —
        # a drag/toggle persist while the gauge is still blank (OAuth
        # throttled since launch) must not wipe the cache on disk.
        if self.gauge_fable.pct is not None:
            state["fable_pct"] = self.gauge_fable.pct
            state["fable_resets_at"] = self.gauge_fable.reset_iso
        save_state(state)


def make_tray_pixmap(pct: float | None, time_rem_pct: float | None,
                     size: int = 16, marker: str | None = None,
                     base: QColor | None = None) -> QPixmap:
    """Render a tray icon natively at the requested size.

    Design:
      - dark rounded base (`base` tints it per provider — slate for Claude,
        teal for OpenAI)
      - white perimeter arc shows `time_rem_pct` of the reset window remaining
        (100 = just reset → full circle; 0 = imminent reset → empty)
      - severity-coloured bold percentage in the center
      - optional `marker` letter in the bottom corner so the icons stay
        distinguishable even when Windows shuffles their order
    """
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    radius = max(1, size // 8)

    # Dark base
    p.setPen(Qt.NoPen)
    p.setBrush(base or CLAUDE_TILE_BASE)
    p.drawRoundedRect(0, 0, size, size, radius, radius)

    # Time-remaining arc (white)
    thickness = max(1.5, size * 0.10)
    inset = thickness / 2
    arc_rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(60, 65, 75, 220), thickness,
                  Qt.SolidLine, Qt.RoundCap))
    p.drawArc(arc_rect, 0, 360 * 16)
    if time_rem_pct is not None:
        t = max(0.0, min(100.0, time_rem_pct))
        p.setPen(QPen(QColor(245, 247, 252), thickness,
                      Qt.SolidLine, Qt.RoundCap))
        p.drawArc(arc_rect, 90 * 16, int(-(t / 100.0) * 360 * 16))

    # Big urgency-coloured number with dark outline
    if pct is None:
        text, fill = "—", QColor(180, 185, 195)
    else:
        v = max(0.0, min(100.0, pct))
        text, fill = f"{int(round(v))}", urgency_color(v, time_rem_pct)
    f = QFont()
    f.setBold(True)
    f.setPointSizeF(size * 0.55)
    p.setFont(f)
    rect = QRectF(0, -1, size, size)
    p.setPen(QColor(0, 0, 0, 220))
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        p.drawText(rect.translated(dx, dy), Qt.AlignCenter, text)
    p.setPen(fill)
    p.drawText(rect, Qt.AlignCenter, text)

    # Tiny identity marker in the bottom-right corner (e.g. 'h' for 5h, 'd'
    # for 7d, 'fd' for Fable weekly). Two-char markers get a smaller font and
    # a wider box so they fit the corner without clipping or hitting the number.
    if marker:
        mf = QFont()
        mf.setBold(True)
        mf.setPointSizeF(size * (0.30 if len(marker) == 1 else 0.24))
        p.setFont(mf)
        corner = 0.55 if len(marker) == 1 else 0.42
        marker_rect = QRectF(size * corner, size * 0.55,
                             size * (1.0 - corner), size * 0.45)
        p.setPen(QColor(0, 0, 0, 200))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            p.drawText(marker_rect.translated(dx, dy), Qt.AlignCenter, marker)
        p.setPen(QColor(220, 225, 235, 230))
        p.drawText(marker_rect, Qt.AlignCenter, marker)

    p.end()
    return pix


def make_tray_icon(pct: float | None, time_rem_pct: float | None,
                   marker: str | None = None) -> QIcon:
    """Multi-resolution icon so Windows can pick the closest match without scaling."""
    icon = QIcon()
    for sz in (16, 20, 24, 32, 40, 48):
        icon.addPixmap(make_tray_pixmap(pct, time_rem_pct, sz, marker))
    return icon


def time_remaining_pct(resets_at: str | None, window_seconds: int) -> float | None:
    """Convert an ISO 8601 reset timestamp into "% of window still remaining"."""
    if not resets_at:
        return None
    try:
        dt = datetime.fromisoformat(resets_at)
        rem = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, min(100.0, rem / window_seconds * 100.0))
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Taskbar overlay widget — a frameless window positioned over the shell taskbar
# so we get the full taskbar height (~32-40px) instead of the 16x16 tray slot.
# ----------------------------------------------------------------------------

def _taskbar_geometry() -> tuple[int, int, int, int, int, int] | None:
    """Return (taskbar_left, top, right, bottom, tray_left, tray_top) on
    Windows, or None on other platforms / failure.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.FindWindowW.restype = wintypes.HWND
        u.FindWindowExW.restype = wintypes.HWND
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL

        taskbar = u.FindWindowW("Shell_TrayWnd", None)
        if not taskbar:
            return None
        tb = wintypes.RECT()
        if not u.GetWindowRect(taskbar, ctypes.byref(tb)):
            return None
        tray = u.FindWindowExW(taskbar, None, "TrayNotifyWnd", None)
        tr = wintypes.RECT()
        if tray and u.GetWindowRect(tray, ctypes.byref(tr)):
            tray_left, tray_top = tr.left, tr.top
        else:
            tray_left, tray_top = tb.right, tb.top
        return tb.left, tb.top, tb.right, tb.bottom, tray_left, tray_top
    except Exception:
        return None


class TaskbarWidget(QWidget):
    """Frameless transparent always-on-top window that sits on top of the
    Windows taskbar, just to the left of the tray notification area. Shows
    the same two ring-icons we render for the tray, but at full taskbar
    height so they're actually readable.
    """

    def __init__(self, on_click) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Claude usage (taskbar)")

        self._on_click = on_click
        # (pct, time_rem_pct, marker) per tile; [:GROUP_SPLIT] = Claude
        self._entries: list[tuple[float | None, float | None, str]] = [
            (None, None, m) for m in ("h", "d", "fd", "g", "gw")
        ]
        self._icon_size = 28
        self._gap = 4
        self._group_gap = 10   # extra space between the provider groups
        self._embedded = False        # True once SetParent into Shell_TrayWnd succeeded
        self._taskbar_hwnd: int = 0   # cached parent HWND so we can detect explorer restarts
        self._recovery_requested_at: float = 0.0

        # Re-position over the taskbar — handles autohide / DPI / monitor /
        # explorer-restart. Once we're a child of Shell_TrayWnd we can't be
        # demoted, so the cadence can be relaxed.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.reposition)
        self._timer.start(1000)
        # Slower repaint for live reset countdowns. Both Qt.update() and
        # the Win32 force-redraw — see _force_redraw for why.
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._tick_repaint)
        self._tick.start(1000)
        # Embed into the taskbar shortly after creation. Needs winId() to be
        # valid, which it isn't until show() runs.
        QTimer.singleShot(100, self._embed_and_position)

    def _tick_repaint(self) -> None:
        self.repaint()
        self._force_redraw()

    def _embed_and_position(self) -> None:
        self.show()
        self._embed_in_taskbar()
        self.reposition()

    def set_data(
        self, entries: list[tuple[float | None, float | None, str]]
    ) -> None:
        self._entries = list(entries)
        self.repaint()
        self._force_redraw()

    def _row_width(self, icon: int) -> int:
        n = len(self._entries)
        return icon * n + self._gap * (n - 1) + self._group_gap

    def _force_redraw(self, include_parent: bool = False) -> None:
        """After SetParent into Shell_TrayWnd, Qt's QWidget.update() doesn't
        always result in a WM_PAINT — the parent owns invalidation. Force
        it via Win32 so the displayed pixels actually match self._pct5/etc.
        """
        if sys.platform != "win32" or not self._embedded:
            return
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.GetParent.restype = wintypes.HWND
            u.GetParent.argtypes = [wintypes.HWND]
            u.RedrawWindow.restype = wintypes.BOOL
            u.RedrawWindow.argtypes = [
                wintypes.HWND, ctypes.c_void_p, wintypes.HRGN, wintypes.UINT
            ]
            RDW_INVALIDATE = 0x0001
            RDW_UPDATENOW  = 0x0100
            RDW_ALLCHILDREN = 0x0080
            hwnd = int(self.winId())
            u.RedrawWindow(
                hwnd, None, None,
                RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN,
            )
            if include_parent:
                parent = int(u.GetParent(hwnd) or 0)
                if parent:
                    u.RedrawWindow(
                        parent, None, None,
                        RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN,
                    )
        except Exception:
            pass

    def recover_shell_surface(self, reason: str = "") -> None:
        """Refresh the HWND/taskbar relationship after lock, sleep, or explorer reset."""
        if sys.platform != "win32":
            return
        now = time.monotonic()
        if now - self._recovery_requested_at < 0.75:
            return
        self._recovery_requested_at = now
        for delay in (0, 250, 1000, 3000):
            QTimer.singleShot(delay, self._recover_shell_surface_now)

    def _recover_shell_surface_now(self) -> None:
        self._detach_from_taskbar()
        self._embed_and_position()
        self.repaint()
        self._force_redraw(include_parent=True)

    def reposition(self) -> None:
        geo = _taskbar_geometry()
        if not geo:
            return
        tb_l, tb_t, tb_r, tb_b, tray_l, tray_t = geo
        tb_h = tb_b - tb_t
        icon = max(20, tb_h - 6)
        w = self._row_width(icon)
        h = icon
        # Detect explorer.exe restart OR a session change that severed our
        # SetParent relationship — either way we need to re-embed.
        if self._embedded and sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                u = ctypes.windll.user32
                u.FindWindowW.restype = wintypes.HWND
                u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
                u.GetParent.restype = wintypes.HWND
                u.GetParent.argtypes = [wintypes.HWND]
                current_tb = int(u.FindWindowW("Shell_TrayWnd", None) or 0)
                our_parent = int(u.GetParent(int(self.winId())) or 0)
                if (not current_tb
                        or current_tb != self._taskbar_hwnd
                        or our_parent != current_tb):
                    self._embedded = False
            except Exception:
                pass
        if not self._embedded:
            self._embed_in_taskbar()
        # Right edge anchored just left of the tray, then dodge any small
        # topmost pill floating over that spot (e.g. dictation bubbles) —
        # they'd otherwise paint over our leftmost tiles.
        sx = tray_l - w - 6
        for _ in range(3):
            block = self._find_obstruction(sx, tb_t, sx + w, tb_b)
            if block is None or block - w - 4 < tb_l:
                break
            sx = block - w - 4
        if self._embedded:
            # Position relative to taskbar's client area
            x = sx - tb_l
            y = (tb_h - h) // 2
        else:
            # Fallback: absolute screen position + topmost ranking
            x = sx
            y = tb_t + (tb_h - h) // 2
        self._icon_size = icon
        self.resize(w, h)
        self.move(x, y)
        self.show()
        if not self._embedded:
            self._force_topmost()

    def _find_obstruction(self, left: int, top: int,
                          right: int, bottom: int) -> int | None:
        """Left edge of a small always-on-top window overlapping the given
        screen rect, or None. Catches utility pills that dock over the
        taskbar near the tray (dictation bubbles, recorders, …) — as
        topmost siblings they'd paint over our embedded row, so we slide
        left of them instead. Filters to small (≤600×200) topmost windows
        from other processes so ordinary app windows never trigger a dodge.
        """
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_TOPMOST = 0x0008
            my_pid = os.getpid()
            hits: list[int] = []

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd, _lp):
                if not u.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == my_pid:
                    return True
                if not (u.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST):
                    return True
                r = wintypes.RECT()
                if not u.GetWindowRect(hwnd, ctypes.byref(r)):
                    return True
                w_, h_ = r.right - r.left, r.bottom - r.top
                if not (0 < w_ <= 600 and 0 < h_ <= 200):
                    return True
                if (r.right <= left or r.left >= right
                        or r.bottom <= top or r.top >= bottom):
                    return True
                hits.append(r.left)
                return True

            u.EnumWindows(_cb, 0)
            return min(hits) if hits else None
        except Exception:
            return None

    def _detach_from_taskbar(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.SetParent.restype = wintypes.HWND
            u.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
            u.GetWindowLongW.restype = ctypes.c_long
            u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetWindowLongW.restype = ctypes.c_long
            u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            u.SetWindowPos.restype = wintypes.BOOL
            u.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.UINT,
            ]

            hwnd = int(self.winId())
            if not hwnd:
                return

            GWL_STYLE = -16
            WS_POPUP = 0x80000000
            WS_CHILD = 0x40000000
            style = u.GetWindowLongW(hwnd, GWL_STYLE)
            u.SetWindowLongW(hwnd, GWL_STYLE, (style & ~WS_CHILD) | WS_POPUP)
            u.SetParent(hwnd, 0)

            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020
            SWP_HIDEWINDOW = 0x0080
            u.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE |
                SWP_FRAMECHANGED | SWP_HIDEWINDOW,
            )
        except Exception:
            pass
        self._embedded = False
        self._taskbar_hwnd = 0

    def _embed_in_taskbar(self) -> bool:
        """Reparent our window into Shell_TrayWnd so the taskbar can never
        paint over us. Returns True on success.

        Child windows live in the parent's z-context — they're painted after
        the parent, so the taskbar simply *can't* draw on top of us. This is
        what CodeZeno does (WS_CHILD + SetParent on the taskbar HWND).
        """
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.FindWindowW.restype = wintypes.HWND
            u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            u.SetParent.restype = wintypes.HWND
            u.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
            u.GetWindowLongW.restype = ctypes.c_long
            u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetWindowLongW.restype = ctypes.c_long
            u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            u.SetWindowPos.restype = wintypes.BOOL
            u.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.UINT,
            ]

            taskbar = int(u.FindWindowW("Shell_TrayWnd", None) or 0)
            my_hwnd = int(self.winId())
            if not taskbar or not my_hwnd:
                return False

            GWL_STYLE = -16
            WS_POPUP        = 0x80000000
            WS_CHILD        = 0x40000000
            WS_VISIBLE      = 0x10000000
            WS_CLIPSIBLINGS = 0x04000000
            style = u.GetWindowLongW(my_hwnd, GWL_STYLE)
            new_style = (style & ~WS_POPUP) | WS_CHILD | WS_CLIPSIBLINGS | WS_VISIBLE
            u.SetWindowLongW(my_hwnd, GWL_STYLE, new_style)
            ctypes.windll.kernel32.SetLastError(0)
            result = u.SetParent(my_hwnd, taskbar)
            # SetParent returns the previous parent. For a top-level window,
            # previous parent is NULL even when the call succeeds.
            if not result and ctypes.windll.kernel32.GetLastError():
                return False
            HWND_TOP = 0
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            SWP_FRAMECHANGED = 0x0020
            SWP_SHOWWINDOW = 0x0040
            u.SetWindowPos(
                my_hwnd, HWND_TOP, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE |
                SWP_FRAMECHANGED | SWP_SHOWWINDOW,
            )
            self._embedded = True
            self._taskbar_hwnd = taskbar
            return True
        except Exception:
            return False

    def _force_topmost(self) -> None:
        """Re-rank above the taskbar (Shell_TrayWnd is HWND_TOPMOST too —
        Qt's WindowStaysOnTopHint isn't always enough on its own)."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.SetWindowPos.restype = wintypes.BOOL
            u.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.UINT,
            ]
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            u.SetWindowPos(
                int(self.winId()), HWND_TOPMOST,
                0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception:
            pass

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        s = self._icon_size
        x = 0
        for i, (pct, tr, marker) in enumerate(self._entries):
            if i == GROUP_SPLIT:
                x += self._group_gap
            base = OPENAI_TILE_BASE if i >= GROUP_SPLIT else CLAUDE_TILE_BASE
            p.drawPixmap(x, 0, make_tray_pixmap(pct, tr, s, marker, base))
            x += s + self._gap
        p.end()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._on_click()
            # The click activated whatever on_click toggled; reclaim topmost
            # immediately + once more after Qt finishes processing so we don't
            # get buried by the taskbar.
            self._force_topmost()
            QTimer.singleShot(50, self._force_topmost)
            QTimer.singleShot(250, self._force_topmost)
            e.accept()
        elif e.button() == Qt.RightButton:
            self._show_menu(e.globalPosition().toPoint())
            self._force_topmost()
            QTimer.singleShot(50, self._force_topmost)
            e.accept()

    def _show_menu(self, gp: QPoint) -> None:
        m = QMenu()
        m.addAction("Show / hide widget", self._on_click)
        m.addAction("Refresh now",
                    lambda: QApplication.instance()._refresh_now(manual=True))
        m.addSeparator()
        m.addAction("Quit", QApplication.instance().quit)
        m.exec(gp)


class WindowsShellEventFilter(QAbstractNativeEventFilter):
    """Watch shell/session messages that can stale a SetParent taskbar child."""

    WM_POWERBROADCAST = 0x0218
    WM_WTSSESSION_CHANGE = 0x02B1
    PBT_APMRESUMECRITICAL = 0x0006
    PBT_APMRESUMESUSPEND = 0x0007
    PBT_APMRESUMEAUTOMATIC = 0x0012
    WTS_CONSOLE_CONNECT = 0x1
    WTS_REMOTE_CONNECT = 0x3
    WTS_SESSION_LOGON = 0x5
    WTS_SESSION_UNLOCK = 0x8

    def __init__(self, taskbar: TaskbarWidget, widget: Widget) -> None:
        super().__init__()
        self._taskbar = taskbar
        self._widget = widget
        self._registered_hwnd = 0
        self._last_refresh_request_at = 0.0
        self._taskbar_created_msg = 0
        if sys.platform == "win32":
            try:
                import ctypes
                self._taskbar_created_msg = int(
                    ctypes.windll.user32.RegisterWindowMessageW("TaskbarCreated")
                    or 0
                )
            except Exception:
                self._taskbar_created_msg = 0

    def register_session_notifications(self, hwnd: int) -> None:
        if sys.platform != "win32" or not hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes
            wts = ctypes.windll.wtsapi32
            wts.WTSRegisterSessionNotification.argtypes = [
                wintypes.HWND, wintypes.DWORD
            ]
            wts.WTSRegisterSessionNotification.restype = wintypes.BOOL
            if wts.WTSRegisterSessionNotification(hwnd, 0):
                self._registered_hwnd = hwnd
        except Exception:
            self._registered_hwnd = 0

    def unregister_session_notifications(self) -> None:
        if sys.platform != "win32" or not self._registered_hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes
            wts = ctypes.windll.wtsapi32
            wts.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
            wts.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
            wts.WTSUnRegisterSessionNotification(self._registered_hwnd)
        except Exception:
            pass
        self._registered_hwnd = 0

    def nativeEventFilter(self, event_type, message):
        if sys.platform != "win32":
            return False, 0
        try:
            from ctypes import wintypes
            ptr = int(message)
            msg = wintypes.MSG.from_address(ptr)
            msg_id = int(msg.message)
            wparam = int(msg.wParam)
        except Exception:
            return False, 0

        if self._taskbar_created_msg and msg_id == self._taskbar_created_msg:
            self._recover("taskbar-created")
        elif msg_id == self.WM_POWERBROADCAST and wparam in (
            self.PBT_APMRESUMECRITICAL,
            self.PBT_APMRESUMESUSPEND,
            self.PBT_APMRESUMEAUTOMATIC,
        ):
            self._recover("power-resume")
        elif msg_id == self.WM_WTSSESSION_CHANGE and wparam in (
            self.WTS_CONSOLE_CONNECT,
            self.WTS_REMOTE_CONNECT,
            self.WTS_SESSION_LOGON,
            self.WTS_SESSION_UNLOCK,
        ):
            self._recover("session-change")
        return False, 0

    def _recover(self, reason: str) -> None:
        self._taskbar.recover_shell_surface(reason)
        QTimer.singleShot(0, self._widget._tick_repaint)
        QTimer.singleShot(750, self._widget._tick_repaint)

        now = time.monotonic()
        if now - self._last_refresh_request_at < 10:
            return
        self._last_refresh_request_at = now
        QTimer.singleShot(2000, lambda: self._widget.refresh_now(manual=True))


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
) -> tuple[QSystemTrayIcon, QSystemTrayIcon, QSystemTrayIcon]:
    """Three side-by-side tray icons: 5h session, 7d weekly, 7d Fable."""
    tray_5h = QSystemTrayIcon(make_tray_icon(None, None, marker="h"), app)
    tray_7d = QSystemTrayIcon(make_tray_icon(None, None, marker="d"), app)
    tray_fable = QSystemTrayIcon(make_tray_icon(None, None, marker="fd"), app)
    tray_5h.setToolTip("Claude Code — 5-hour session (loading…)")
    tray_7d.setToolTip("Claude Code — 7-day weekly (loading…)")
    tray_fable.setToolTip("Claude Code — 7-day Fable (loading…)")
    _wire_tray(tray_5h, widget, app)
    _wire_tray(tray_7d, widget, app)
    _wire_tray(tray_fable, widget, app)
    return tray_5h, tray_7d, tray_fable


def _kill_other_instances() -> None:
    """Terminate any other process already running this script.

    Launching the dashboard means 'replace whatever is running with this
    code'. Stale instances are worse than useless: an old build keeps
    rewriting the state file in its own format (clobbering newer fields)
    and fights the new instance for the taskbar embed slot.
    """
    if sys.platform != "win32":
        return
    me = os.getpid()
    try:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter "
                "\"Name like 'python%'\" | "
                "Select-Object ProcessId,CommandLine,"
                "@{n='Created';e={$_.CreationDate.ToFileTimeUtc()}} | "
                "ConvertTo-Json",
            ],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        procs = json.loads(out.stdout or "[]")
        if isinstance(procs, dict):   # ConvertTo-Json unwraps single results
            procs = [procs]
    except Exception:
        return
    mine = next(
        (pr for pr in procs
         if isinstance(pr, dict) and pr.get("ProcessId") == me), None)
    my_created = (mine or {}).get("Created") or 0
    import ctypes
    PROCESS_TERMINATE = 0x0001
    k = ctypes.windll.kernel32
    for pr in procs:
        try:
            pid = int(pr.get("ProcessId") or 0)
            cmd = (pr.get("CommandLine") or "").lower()
            created = int(pr.get("Created") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if not pid or pid == me or "usagedashboard.py" not in cmd:
            continue
        # Newest launch wins: never kill an instance started after us, so
        # two near-simultaneous launches can't terminate each other. (The
        # newer one will sweep us instead. PID breaks exact-tick ties.)
        if created > my_created or (created == my_created and pid > me):
            continue
        try:
            h = k.OpenProcess(PROCESS_TERMINATE, False, pid)
            if h:
                k.TerminateProcess(h, 0)
                k.CloseHandle(h)
        except Exception:
            pass


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
    _kill_other_instances()
    _set_aumid()
    app = QApplication(sys.argv)
    app.setApplicationName("Claude Usage Dashboard")
    app.setOrganizationName("local")
    app.setQuitOnLastWindowClosed(False)
    w = Widget()

    # In-taskbar overlay (Windows only). Left-click toggles the floating widget.
    taskbar = TaskbarWidget(on_click=w.toggle_visible)
    w.taskbar = taskbar
    app._taskbar = taskbar  # keep alive  # type: ignore[attr-defined]
    # Expose refresh on the QApplication so the taskbar right-click menu can hit it
    app._refresh_now = w.refresh_now  # type: ignore[attr-defined]
    shell_events = WindowsShellEventFilter(taskbar, w)
    app.installNativeEventFilter(shell_events)
    shell_events.register_session_notifications(int(w.winId()))
    app._shell_events = shell_events  # keep alive  # type: ignore[attr-defined]

    if w._visible_pref:
        w.show()

    def _cleanup() -> None:
        shell_events.unregister_session_notifications()
        taskbar.hide()
    app.aboutToQuit.connect(_cleanup)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
