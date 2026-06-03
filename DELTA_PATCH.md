# Patch reference (`mateo/weekly-delta`)

For setup/install/troubleshooting see [SETUP.md](SETUP.md). This file is the
technical reference for what the three patch commits actually change.

## 1. Weekly delta indicator

Adds a small "▲ X%" / "▼ X%" next to the Weekly reset line showing how this
week's token usage compares to the previous 7 days. Anthropic's rate-limit
headers only expose current-window utilization, so the delta is computed on
the daemon side by aggregating Claude Code's local JSONL session logs at
`~/.claude/projects/**/*.jsonl`. Hidden until prev-7d data exists.

```
daemon/claude_usage_daemon.py     +110 lines  (JsonlAggregator + payload fields)
firmware/src/data.h                 +5 lines  (delta_pct, has_delta)
firmware/src/main.cpp               +4 lines  (parse "dp"/"hd" from JSON)
firmware/src/ui.cpp                +28 lines  (lbl_weekly_delta + render)
```

## 2. OAuth auto-refresh

Claude Code's access tokens expire ~8h after issuance and the CLI only
refreshes them lazily on the next API call — so the stock daemon 401s for
hours when you haven't touched `claude` recently. This patch detects expiry
(5-min margin), exchanges the refreshToken at
`https://console.anthropic.com/v1/oauth/token` (client_id
`9d1c250a-e61b-44d9-88ed-5944d1962f5e`, the public Claude Code default),
and writes the new triple back to the same credential store we read from.

```
daemon/claude_usage_daemon.py     +156 lines  (get_fresh_token + helpers)
```

## 3. Font merge for ▲/▼

Styrene Regular doesn't include U+25B2/U+25BC, so the delta arrows rendered
as tofu rectangles. `font_styrene_24.c` was regenerated with two source
fonts merged: Styrene for ASCII, DejaVu Sans Mono for just the two triangle
codepoints. Regen recipe in the commit message.

```
firmware/src/font_styrene_24.c     +77 lines
```

Layout-aware: the delta label is positioned at `L.usage_reset_y + 3` so it
follows the per-board geometry computed by `compute_layout()` in `ui.cpp`.
Works on every board the upstream HAL supports (S3-2.16, S3-1.8, C6-2.16).

Wire protocol additions (same short-key style as the existing `s`/`sr`/`w`/`wr`):

| Key  | Type  | Meaning                                                |
| ---- | ----- | ------------------------------------------------------ |
| `dp` | float | Percent change vs previous 7d (e.g. `43.5`, `-12.3`)   |
| `hd` | bool  | `true` when `dp` is present; `false` first week of use |

## Flashing on the ESP32-C6-Touch-AMOLED-2.16

The fork's `main` branch is already aligned to upstream HEAD, which includes
the HAL refactor and the C6 board support (env `waveshare_amoled_216_c6`).

Stay on `main` until you've confirmed the stock firmware boots and the
daemon talks to the device. Then move to this branch:

```bash
cd ~/Desktop/clawdmeter

# 1. Stock first — confirm board boots and pairs.
git checkout main
./flash-mac.sh waveshare_amoled_216_c6        # auto-detects /dev/cu.usbmodem*
# pair via System Settings → Bluetooth ("Clawdmeter")
./install-mac.sh                                  # installs daemon + LaunchAgent

# 2. Apply the patch.
git checkout mateo/weekly-delta
./flash-mac.sh waveshare_amoled_216_c6
launchctl unload ~/Library/LaunchAgents/com.user.claude-usage-daemon.plist
launchctl load -w ~/Library/LaunchAgents/com.user.claude-usage-daemon.plist
```

The daemon LaunchAgent's `ExecStart` is an absolute path to this checkout,
so a `git checkout` is enough — no symlink shuffling.

## Rollback

```bash
git checkout main
./flash-mac.sh waveshare_amoled_216_c6
launchctl unload ~/Library/LaunchAgents/com.user.claude-usage-daemon.plist
launchctl load -w ~/Library/LaunchAgents/com.user.claude-usage-daemon.plist
```

## Gotchas

- **First week of use:** the delta label stays hidden until the JSONL logs
  span 7+ days. `JsonlAggregator.delta_pct()` returns `None` if `prev_sum == 0`.
- **C6 has no PSRAM.** Upstream `main.cpp` already handles this with
  conditional buffer sizing (`BUF_LINES=20` on C6 vs `40` on S3), and
  `send_screenshot()` is disabled on PSRAM-free boards. The delta patch
  doesn't change framebuffer usage so this stays fine.
- **Python 3.10+ required** for the daemon (PEP 604 union types — was already
  the case in stock). User Mac's system `python3` is 3.9 but brew installed
  `/opt/homebrew/bin/python3.14`. If the venv `python3 -m venv` picks 3.9,
  edit `install-mac.sh` to use the explicit 3.14 path.
- **JSONL parse cost:** ~80-700 ms cold (varies with project count), ~1-2 ms
  warm (mtime-cached). Negligible inside a 60 s poll.
