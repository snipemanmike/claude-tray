# claude-tray

Always-on Claude Code usage dashboard for Windows.

Two side-by-side tray icons show your 5-hour session and 7-day weekly utilization. Color isn't just the raw percentage — it's **normalized by how much of the reset window is still left** so the icon only goes red when you're actually in trouble (high % *and* lots of window remaining), not when you're at 95 % with two minutes to reset. A transparent always-on-top widget with ring gauges and reset countdowns sits on the desktop using the same color logic.

![hero](docs/hero.png)

## The urgency color model

Raw % is half the story. Being at 90 % with 4 hours left of a 5-hour window is a problem (you'll burn out before reset). Being at 90 % with 2 minutes left isn't (you'll reset to 0 in 2 minutes). Plain severity-by-% can't tell those apart, so the color is driven by a simple formula:

```
urgency = pct − elapsed%  =  pct + time_remaining% − 100
```

| urgency | meaning | color |
|---|---|---|
| < 0 | ahead of pace, you'll finish under quota | **green** |
| ≈ 0 | on pace | **green** |
| 30–60 | burning faster than the reset can save you | **amber** |
| ≥ 60 | catastrophic burn rate | **red** |

Same model is used by both surfaces. The visualization across the 9 quadrants of `pct × time_remaining`:

![urgency model](docs/urgency.png)

Notice that **low % stays green regardless of time**, and **high % only goes red when there's lots of window left**. The bottom-right cell (92 % at 5 % time remaining) is green because the reset is right there.

## Features

- **Two tray icons** rendered natively at 16×16 — big bold percentage colored by urgency, white perimeter arc that drains as the reset window runs out.
- **Transparent always-on-top widget** with two ring gauges, percentages, and reset countdowns. Drag to move, scroll-wheel to resize, right-click for opacity / quit.
- **Polls every 60 s** with an OAuth + header-probe fallback strategy — primary path is free; the fallback costs ~9 tokens per call (negligible).
- **Persists position, size, opacity, visibility** across restarts (`~/.claude/.usagedashboard.json`).
- **Tray click toggles** the floating widget. Tray right-click for refresh / quit.
- **Multi-monitor aware** — places itself on whichever screen your cursor is on; recovers gracefully if you save a position on a monitor you later disconnect.

## Install

Requires Python 3.10+ and a Claude Pro/Max subscription that you've already signed into via [Claude Code](https://docs.claude.com/en/docs/claude-code).

```powershell
git clone https://github.com/snipemanmike/claude-tray
cd claude-tray
pip install -r requirements.txt
pythonw usagedashboard.py
```

Two tray icons appear (initially under the `^` overflow chevron — see below). The floating widget appears on whichever monitor your cursor is on.

### Auto-start on login

Drop a shortcut to `run.bat` (or `pythonw.exe usagedashboard.py`) into your Startup folder:

```
Win+R → shell:startup → drop shortcut
```

### Pin the tray icons to the always-visible strip

Windows places new tray icons under the overflow chevron `^` by default. Pin them out once:

- **Easy:** click the `^`, drag each icon left past the chevron into the always-visible strip.
- **Settings way:** *Settings → Personalization → Taskbar → Other system tray icons* → toggle **Claude Usage Dashboard** entries on.

## Controls

| Surface | Action | What it does |
|---|---|---|
| Tray icon | Left-click | Show / hide the floating widget |
| Tray icon | Right-click | Refresh now / show-hide / quit |
| Tray icon | Hover | Tooltip with exact % and reset countdown |
| Widget | Left-click + drag | Move (position is persisted) |
| Widget | Scroll wheel | Resize (0.6× – 2.0×) |
| Widget | Right-click | Opacity 50 / 75 / 92 / 100% / quit |

## States at a glance

| | Chill | Burning fast | Made it |
|---|---|---|---|
| Scenario | low % regardless of time | mid % with lots of window left | high % but reset is right there |
| Widget | ![chill](docs/widget-low.png) | ![burning](docs/widget-mid.png) | ![made-it](docs/widget-high.png) |

Tray icons across four scenarios spanning the urgency range (shown 8× upscaled):

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

`time_remaining_pct` is computed locally: `(resets_at − now) / window_length × 100`, with `window_length` of 5 h for session and 7 d for weekly.

**2. Fallback — `max_tokens=1` Haiku ping** (~0.0002% of 5h quota per call)
On a 429 from the primary, sends a minimum-cost message to `claude-haiku-4-5` and reads the response headers — `anthropic-ratelimit-unified-5h-utilization` and `-7d-utilization` give the same numbers. The widget remembers when OAuth was throttled and skips it during the backoff window to avoid wasting round-trips.

> **Why two paths?** `/api/oauth/usage` is undocumented and currently has an aggressive rate limit (see [anthropics/claude-code#31637](https://github.com/anthropics/claude-code/issues/31637) — polls as slow as 5 min can trip 429, and recovery can take 30+ min with no `Retry-After`). The header probe is bullet-proof but costs a tiny fraction of quota per call. The hybrid gets you free polling when the endpoint works and reliable polling when it doesn't. At 60 s polling, even worst-case "header probe every time" burns about **0.05 % of your 5 h quota over the full 5 h window** — basically noise.

The OAuth token expires periodically; Claude Code itself refreshes it in `~/.claude/.credentials.json` whenever you use the CLI/IDE. As long as you keep using Claude Code, the widget stays authenticated.

## The 16×16 tray-icon ceiling

Windows tray icons are limited to `SM_CXSMICON` (16×16 px at 100 % DPI, larger at higher DPI). The clock and Ink Workspace aren't tray icons — they're special shell widgets. We render natively at 16×16 (no downsample blur) and use two side-by-side slots — one for 5h, one for 7d — so each gets the full pixel budget.

## Configuration

There's no config file — sensible defaults. To change behavior, edit constants at the top of `usagedashboard.py`:

```python
POLL_SECONDS        = 60        # how often to refetch usage
MAX_BACKOFF_SECONDS = 1800      # ceiling on OAuth-endpoint 429 backoff
USAGE_URL           = "https://api.anthropic.com/api/oauth/usage"
HEADER_PROBE_MODEL  = "claude-haiku-4-5-20251001"
```

State (window position, size, opacity, visibility) lives in `~/.claude/.usagedashboard.json`. Delete it to reset.

## Regenerating the doc images

```powershell
python docs/render.py
```

Produces `hero.png`, `tray.png`, `widget-low/mid/high.png`, and `urgency.png` from synthetic data so no live desktop screenshots leak.

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
