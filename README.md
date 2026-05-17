# claude-tray

Always-on Claude Code usage dashboard for Windows.

Two side-by-side tray icons show your 5-hour session and 7-day weekly utilization. The percentage color isn't just the raw value — it's **normalized by how much of the reset window is still left** so the icon only goes red when you're actually in trouble (high % *and* lots of window remaining), not when you're at 95 % with two minutes to reset. A transparent always-on-top widget mirrors the same visual language at a larger size.

![hero](docs/hero.png)

## The urgency color model

Raw % is half the story. Being at 90 % with 4 hours left of a 5-hour window is a problem — you'll burn out before reset. Being at 90 % with 2 minutes left isn't — you'll reset to 0 in 2 minutes. Plain severity-by-% can't tell those apart, so color is driven by a single formula:

```
urgency = pct + time_remaining% − 100   (= pct − elapsed%)
```

The color is a **continuous gradient**, not a step function — it slides smoothly from green at urgency 0, through peak amber at urgency 15, to pure red at urgency 40 and above. So small overshoots tint slightly amber; big ones go red.

### The full curve

![urgency curve](docs/urgency_curve.png)

X = time remaining; Y = pct used. Each pixel is colored exactly as the tray icon / widget ring would be at that state. Dashed diagonals mark the gradient anchors (peak amber, pure red). The "danger zone" is the top-right corner — high % with a fresh window. The further from that corner, the safer.

### 3 × 3 sample map

![urgency model](docs/urgency.png)

**Low % stays green regardless of time.** **High % only goes red when there's lots of window left.** The bottom-right cell (92 % at 5 % time remaining) is green because the reset is right there — you made it.

## Visual language

The same two visual elements appear in both surfaces:

- **White ring / arc** drains as the reset window elapses — full circle just after a reset, gone right before the next one.
- **Big bold percentage number** colored by urgency — green / amber / red.

The widget adds the obvious extras the tray slot can't fit: section labels ("5h session" / "7d weekly") and reset countdowns ("resets 2h 18m").

## Features

- **In-taskbar overlay** — two large icons sized to the full taskbar height (~32 px) sit on top of the Windows taskbar just left of the tray notification area. Bold % colored by urgency, white perimeter arc draining with time. Way more readable than a 16×16 tray icon.
- **Floating always-on-top widget** with two ring gauges, percentages and reset countdowns — clicked open from the taskbar overlay. Drag to move, scroll-wheel to resize, right-click for refresh / opacity / quit.
- **Polls every 60 s** with a free OAuth endpoint + Haiku-header-probe fallback — total quota cost is ~0.05 % of your 5 h window even in the worst case.
- **Persists position, size, opacity, visibility** across restarts (`~/.claude/.usagedashboard.json`).
- **Click the taskbar overlay** to toggle the floating widget. Right-click for menu.
- **Multi-monitor aware** — places itself on whichever screen your cursor is on; recovers gracefully if a previously-saved position is on a disconnected monitor.

## Install

Requires Python 3.10+ and a Claude Pro/Max subscription you've already signed into via [Claude Code](https://docs.claude.com/en/docs/claude-code).

```powershell
git clone https://github.com/snipemanmike/claude-tray
cd claude-tray
pip install -r requirements.txt
pythonw usagedashboard.py
```

Two tray icons appear (initially under the `^` overflow chevron — see below). The widget appears on whichever monitor your cursor is on.

### Auto-start on login

Drop a shortcut to `run.bat` (or `pythonw.exe usagedashboard.py`) into your Startup folder:

```
Win+R → shell:startup → drop shortcut
```

### Pin the tray icons to the always-visible strip

Windows places new tray icons under the overflow chevron `^` by default. Pin them out once:

- **Easy:** click the `^`, drag each icon left past the chevron into the always-visible strip.
- **Settings way:** *Settings → Personalization → Taskbar → Other system tray icons* → toggle both **Claude Usage Dashboard** entries on.

## Controls

| Surface | Action | What it does |
|---|---|---|
| Taskbar overlay | Left-click | Show / hide the floating widget |
| Taskbar overlay | Right-click | Show / hide widget • Refresh now • Quit |
| Taskbar overlay | Hover | Tooltip with exact % and reset countdown |
| Widget | Left-click + drag | Move (position persisted) |
| Widget | Scroll wheel | Resize (0.6× – 2.0×) |
| Widget | Right-click | Hide widget • Refresh now • Opacity 50 / 75 / 92 / 100 % • Quit |

## What it looks like

**In your taskbar** (mock — actual icons sit on top of your real taskbar to the left of the chevron):

| Chill | Burning fast | At-the-limit |
|---|---|---|
| ![low](docs/taskbar-low.png) | ![mid](docs/taskbar-mid.png) | ![high](docs/taskbar-high.png) |

**Floating widget** (pops up when you click the overlay):

| Chill | Burning fast | Made it |
|---|---|---|
| ![chill](docs/widget-low.png) | ![burning](docs/widget-mid.png) | ![made-it](docs/widget-high.png) |

Overlay icons across four urgency scenarios (shown 8× upscaled):

![tray](docs/tray.png)

## How it works

Reads the OAuth access token Claude Code keeps at `~/.claude/.credentials.json`. Two data sources, tried in order:

**1. Primary — `/api/oauth/usage`** (free, no quota cost)
A `GET` to `https://api.anthropic.com/api/oauth/usage` with the OAuth Bearer token and `anthropic-beta: oauth-2025-04-20`:

```json
{
  "five_hour":  {"utilization": 12.0, "resets_at": "2026-05-16T11:40:00Z"},
  "seven_day":  {"utilization":  7.0, "resets_at": "2026-05-20T21:00:00Z"},
  ...
}
```

`time_remaining_pct` is computed locally: `(resets_at − now) / window_length × 100`, with `window_length` of 5 h for the session limit and 7 d for the weekly limit.

**2. Fallback — `max_tokens=1` Haiku ping** (~0.0002 % of 5 h quota per call)
On a 429 from the primary, sends a minimum-cost message to `claude-haiku-4-5` and reads the response headers — `anthropic-ratelimit-unified-5h-utilization` and `-7d-utilization` give the same numbers. The widget remembers when OAuth was throttled and skips it during the backoff window so we don't waste round-trips.

> **Why two paths?** `/api/oauth/usage` is undocumented and currently has an aggressive rate limit (see [anthropics/claude-code#31637](https://github.com/anthropics/claude-code/issues/31637) — polls as slow as 5 min can trip 429, and recovery can take 30+ min with no `Retry-After`). The header probe is bullet-proof but costs a tiny fraction of quota per call. The hybrid gets you free polling when the endpoint works and reliable polling when it doesn't. At 60 s polling, even worst-case "header probe every time" burns about **0.05 % of your 5 h quota over the full 5 h window** — basically noise.

The OAuth access token expires every ~8–10 hours. The widget recovers without manual intervention:

1. On `401` / `403`, first **re-read the file** in case Claude Code already refreshed (it usually has) — retry with that token.
2. Only if the on-disk token is *actually* past its `expiresAt`, **do our own refresh** via `POST https://claude.ai/v1/oauth/token` (Claude Code's public OAuth client_id), and write the rotated credentials back to `~/.claude/.credentials.json` so Claude Code stays in sync.

This minimizes the OAuth refresh race — whichever side calls refresh first invalidates the other's refresh_token, so we only refresh when nobody else has and the token is genuinely expired.

## Why a taskbar overlay instead of tray icons

`QSystemTrayIcon` is the standard Qt API for "thing in the tray" but it's hard-capped at 16×16 pixels per slot (`SM_CXSMICON`). That's barely enough room for two stacked characters. The clock, ink workspace, and "real" CodeZeno-style widgets aren't tray icons — they're regular Win32 windows attached *to* the taskbar.

We do the same:

1. `FindWindow("Shell_TrayWnd")` to get the taskbar HWND.
2. `FindWindowEx(..., "TrayNotifyWnd")` to find the tray notification area, so we know where to position ourselves.
3. Create a frameless Qt window, then modify its style to `WS_CHILD` and call `SetParent(our_hwnd, taskbar_hwnd)`. This makes our window a **child of the taskbar's window hierarchy** — child windows are rendered in the parent's z-context, *after* the parent's own content. So the taskbar literally cannot paint over us. No amount of clicking on the speaker icon, the chevron, or empty taskbar space can bury us.
4. Re-poll the taskbar geometry every ~1 s to follow autohide / DPI changes / monitor disconnects. Detect `Shell_TrayWnd` HWND changing (explorer.exe restart) and re-embed if needed.

Result: each icon is ~32 px tall instead of 16, the percentage number is readable, and the overlay can't be obscured by taskbar interaction.

**Why we initially tried other approaches and failed:** standard `QSystemTrayIcon` capped us at 16×16. Switching to a floating top-most window worked visually but Shell_TrayWnd has special z-order treatment that beat plain `HWND_TOPMOST` — clicking the taskbar would let the shell repaint over us. Even 20 Hz polling of `SetWindowPos(HWND_TOPMOST)` couldn't keep up. The `SetParent`+`WS_CHILD` approach is the actual solution and is what CodeZeno's Rust implementation uses.

## Configuration

There's no config file — sensible defaults. To change behavior, edit constants at the top of `usagedashboard.py`:

```python
POLL_SECONDS        = 60        # how often to refetch usage
MAX_BACKOFF_SECONDS = 1800      # 30 min ceiling on OAuth-endpoint 429 backoff
USAGE_URL           = "https://api.anthropic.com/api/oauth/usage"
HEADER_PROBE_MODEL  = "claude-haiku-4-5-20251001"
```

State (window position, size, opacity, visibility) lives in `~/.claude/.usagedashboard.json`. Delete it to reset.

## Regenerating the doc images

```powershell
python docs/render.py
```

Produces `hero.png`, `taskbar-low/mid/high.png`, `tray.png`, `widget-low/mid/high.png`, `urgency.png`, and `urgency_curve.png` — all from synthetic data, so the repo never leaks personal taskbar / wallpaper content.

## Uninstall

```powershell
# stop running instance
Get-Process pythonw | Stop-Process -Force

# remove autostart (if you created the shortcut)
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Claude Usage Dashboard.lnk"

# remove saved state
Remove-Item "$env:USERPROFILE\.claude\.usagedashboard.json"

# remove the repo
Remove-Item -Recurse -Force claude-tray
```

## Acknowledgments

The undocumented `/api/oauth/usage` endpoint and its response shape was documented by [ohugonnot/claude-code-statusline](https://github.com/ohugonnot/claude-code-statusline). Several similar tools served as design references: [bozdemir/claude-usage-widget](https://github.com/bozdemir/claude-usage-widget), [CodeZeno/Claude-Code-Usage-Monitor](https://github.com/CodeZeno/Claude-Code-Usage-Monitor), [SlavomirDurej/claude-usage-widget](https://github.com/SlavomirDurej/claude-usage-widget).
