# Clawdmeter — Mateo's fork setup

Personal AMOLED dashboard on the Waveshare ESP32-C6-Touch-AMOLED-2.16
that shows Claude Code's live rate-limit usage and how this week compares
to the previous one. Built on top of [HermannBjorgvin/Clawdmeter](https://github.com/HermannBjorgvin/Clawdmeter)
with three local patches:

- **Weekly delta** — `▲ X%` / `▼ X%` next to the weekly reset, computed
  from your local `~/.claude/projects/**/*.jsonl` logs (the rate-limit
  headers don't expose history).
- **OAuth auto-refresh** — daemon detects access-token expiry, exchanges
  the refresh-token at `console.anthropic.com/v1/oauth/token`, writes the
  fresh triple back to the Keychain. Stock daemon 401s for hours when
  you don't touch `claude` recently; this one doesn't.
- **Font merge** — `font_styrene_24` regenerated with DejaVu fallback for
  ▲/▼ since Styrene Regular doesn't ship those glyphs.

All three patches live on the `mateo/weekly-delta` branch. `main` mirrors
upstream verbatim for clean rollback.

## What you need

| Tool | Install | Why |
| --- | --- | --- |
| Homebrew | <https://brew.sh> | Mac package manager |
| PlatformIO | `brew install platformio` | ESP32 build/flash |
| Python 3.10+ | `brew install python@3.14` if you don't already have ≥3.10 | Daemon uses PEP 604 `str \| None` |
| Node 18+ | (only if you need to regen fonts/icons — not for normal use) | `lv_font_conv` |
| Claude Code | <https://claude.com/code> | the OAuth token lives in Keychain after first login |
| **Hardware** | [Waveshare ESP32-C6-Touch-AMOLED-2.16](https://www.waveshare.com/esp32-c6-touch-amoled-2.16.htm) (~$40 from Amazon) | The display itself |

First-time PlatformIO build downloads the RISC-V toolchain (~500 MB) and
takes 10-15 minutes. Subsequent builds are 20-30 seconds.

## Day-1 flash

```bash
# 1. Clone the fork
git clone https://github.com/mcastro-ops/Clawdmeter ~/Desktop/clawdmeter
cd ~/Desktop/clawdmeter
git checkout mateo/weekly-delta

# 2. Plug the Waveshare into USB-C, then build + flash
./flash-mac.sh waveshare_amoled_216_c6
# (auto-detects /dev/cu.usbmodem*; pass an explicit port as $2 if needed)

# 3. Pair via System Settings → Bluetooth → "Clawdmeter"

# 4. Install the daemon (Python venv + LaunchAgent)
./install-mac.sh
```

After step 4, within ~60 seconds the device should show real numbers.

## "I see the splash but no numbers" troubleshooting

### Daemon not running
```bash
launchctl list | grep claude-usage    # expect "PID 0 com.user.claude-usage-daemon"
tail -F ~/Library/Logs/claude-usage-daemon.out.log
```

If the line is missing, the LaunchAgent didn't load:
```bash
launchctl load -w ~/Library/LaunchAgents/com.user.claude-usage-daemon.plist
```

### Daemon crashes immediately
Check `~/Library/Logs/claude-usage-daemon.err.log`. The two known causes:

1. **Python 3.9** — the venv was built with the wrong Python. Rebuild:
   ```bash
   rm -rf daemon/.venv
   /opt/homebrew/bin/python3.14 -m venv daemon/.venv
   daemon/.venv/bin/pip install bleak httpx
   ```
2. **macOS TCC permission denying access to `~/Desktop/.../pyvenv.cfg`** —
   move the repo outside `~/Desktop` (e.g. `~/code/clawdmeter`) and
   re-run `./install-mac.sh`, or grant Terminal/Python Full Disk Access.

### Daemon logs HTTP 401 forever
Means the OAuth token is expired AND the refresh failed. Force a refresh:
```bash
claude -p "ping"   # any real prompt; Claude Code refreshes the token
```
Then wait one poll cycle (≤60s). The next-cycle daemon will pick up the
fresh token from the Keychain automatically. If that doesn't fix it,
sign out / sign in:
```bash
claude logout && claude login
```

### Device shows numbers but no ▲/▼ glyph (renders as a rectangle)
Means your firmware was built before the font-merge commit. Rebuild
from the current `mateo/weekly-delta`:
```bash
git pull origin mateo/weekly-delta
./flash-mac.sh waveshare_amoled_216_c6
```

## Rolling back to stock

```bash
git checkout main
./flash-mac.sh waveshare_amoled_216_c6
launchctl unload ~/Library/LaunchAgents/com.user.claude-usage-daemon.plist
launchctl load -w ~/Library/LaunchAgents/com.user.claude-usage-daemon.plist
```

The OAuth refresh and delta features go away; the rest still works.

## What the layout looks like

```
┌─────────────────────────────┐
│ Logo            🔋          │  ← Anthropic logo + battery icon
│                             │
│ Current               5h    │  ← Current 5h rolling window
│ 17%                         │  ← big number
│ ████░░░░░░░░░░              │  ← green→amber→red bar
│ Resets in 3h 20m            │
│                             │
│ Weekly                      │  ← Weekly 7d rolling window
│ 5%                          │
│ ██░░░░░░░░░░░░              │
│ Resets in 3d 6h    ▼ 14%    │  ← delta vs previous 7d
│                             │
│       * Musing...           │  ← whimsical status line
└─────────────────────────────┘
```

▲ green for more usage than last week (productive!), ▼ amber for less
(idler week). Hidden during your first week of Claude Code use (no
prev-7d data yet).

## Upstream sync

When HermannBjorgvin pushes new commits:
```bash
git fetch upstream
git checkout main && git merge --ff-only upstream/main && git push origin main
git checkout mateo/weekly-delta && git rebase main && git push --force-with-lease
```

If `ui.cpp` or `main.cpp` conflict (they probably will if upstream touches
the Usage screen), resolve by re-applying the delta-render hunk on top of
the new layout. Search the branch's three patch commits for context:

```bash
git log --oneline main..mateo/weekly-delta
```
