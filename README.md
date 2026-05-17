# claude-tray

Always-on Claude Code usage dashboard for Windows.

Two icons embedded in the Windows taskbar show your 5-hour session and 7-day weekly utilization. Click them to pop up a bigger floating widget with rings + reset countdowns.

![hero](docs/hero.png)

## The color model

Color isn't raw %. It's normalized by how much of the reset window is left:

```
urgency = pct + time_remaining% − 100
```

| color | urgency | meaning |
|---|---|---|
| **blue**  | ≤ −25 | under-utilizing — slack on the table |
| **green** | 0     | on pace — maximizing compute-to-cost |
| **amber** | 15    | burning faster than reset can save you |
| **red**   | ≥ 40  | will exhaust before the window resets |

Continuous gradient between anchors. Live curve over the full `(time_remaining, pct)` plane:

![urgency curve](docs/urgency_curve.png)

If you want to maximize what you paid for, aim for green.

## How it works

- Reads OAuth token from `~/.claude/.credentials.json` (where Claude Code stores it).
- Polls `https://api.anthropic.com/api/oauth/usage` every 60 s — free, no quota cost.
- On 429 (the endpoint rate-limits aggressively — see [anthropics/claude-code#31637](https://github.com/anthropics/claude-code/issues/31637)), falls back to a `max_tokens=1` Haiku ping and reads the rate-limit headers (~0.0002 % of 5 h quota per call).
- Auto-refreshes its own OAuth token when expired so cold-boot works without launching Claude Code first.
- Taskbar overlay is a `WS_CHILD` window `SetParent`'d into `Shell_TrayWnd` so the shell can't paint over it.

## Install

```powershell
git clone https://github.com/snipemanmike/claude-tray
cd claude-tray
pip install -r requirements.txt
pythonw usagedashboard.py
```

Auto-start: `Win+R` → `shell:startup` → drop a shortcut to `run.bat`.

## Acknowledgments

Endpoint shape documented by [ohugonnot/claude-code-statusline](https://github.com/ohugonnot/claude-code-statusline). Taskbar-embed technique borrowed from [CodeZeno/Claude-Code-Usage-Monitor](https://github.com/CodeZeno/Claude-Code-Usage-Monitor).
