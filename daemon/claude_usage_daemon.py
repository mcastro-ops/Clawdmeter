#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (BLE) — macOS port of claude-usage-daemon.sh.

Polls Claude API rate-limit headers and writes a JSON payload to the
ESP32 "Clawdmeter" peripheral over a custom GATT service. Uses
bleak (CoreBluetooth backend on macOS).
"""

import asyncio
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

DEVICE_NAME = "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
TICK = 5
SCAN_TIMEOUT = 8.0
WINDOW_7D_SECONDS = 7 * 24 * 60 * 60

# macOS: token lives in Keychain (service "Claude Code-credentials").
# Linux: token lives in ~/.claude/.credentials.json.
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_token_keychain() -> str | None:
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        log(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain access error: {e}")
        return None
    return _extract_access_token(out.stdout)


def _read_token_file() -> str | None:
    try:
        raw = CREDENTIALS_PATH.read_text()
    except OSError as e:
        log(f"Error reading credentials: {e}")
        return None
    return _extract_access_token(raw)


def read_token() -> str | None:
    if sys.platform == "darwin":
        return _read_token_keychain()
    return _read_token_file()


# ──────────────────────────────────────────────────────────────────────────
# OAuth token refresh. Anthropic's Claude Code access tokens expire after
# ~8h; Claude Code refreshes them lazily on next API call. The stock
# daemon doesn't refresh, so it 401s for hours when the user hasn't
# touched `claude` recently. This block proactively renews via the
# refresh_token grant against the Console OAuth endpoint, then writes
# the new triple (access/refresh/expiresAt) back to whichever credential
# store we read from.
#
# Endpoint and client_id are the public Claude Code defaults — same ones
# the official CLI uses.
# ──────────────────────────────────────────────────────────────────────────
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_REFRESH_MARGIN_S = 300   # refresh if access token expires within 5 min


def _read_credential_blob() -> dict | None:
    """Return the *full* parsed credentials dict (not just the access token)."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password",
                 "-s", KEYCHAIN_SERVICE,
                 "-a", getpass.getuser(), "-w"],
                check=True, capture_output=True, text=True, timeout=10,
            )
            raw = out.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError,
                subprocess.TimeoutExpired) as e:
            log(f"Keychain read failed: {e}")
            return None
    else:
        try:
            raw = CREDENTIALS_PATH.read_text().strip()
        except OSError as e:
            log(f"Credentials file read failed: {e}")
            return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _write_credential_blob(blob: dict) -> bool:
    """Persist the updated credential blob to the same store we read from."""
    payload = json.dumps(blob, separators=(",", ":"))
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["security", "add-generic-password", "-U",
                 "-s", KEYCHAIN_SERVICE,
                 "-a", getpass.getuser(),
                 "-w", payload],
                check=True, capture_output=True, text=True, timeout=10,
            )
            return True
        except subprocess.CalledProcessError as e:
            log(f"Keychain write failed (rc={e.returncode}): {e.stderr.strip()}")
            return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log(f"Keychain write error: {e}")
            return False
    try:
        CREDENTIALS_PATH.write_text(payload)
        return True
    except OSError as e:
        log(f"Credentials file write failed: {e}")
        return False


async def _refresh_oauth(refresh_token: str) -> dict | None:
    """POST to Anthropic's OAuth token endpoint. Returns the JSON response
    on 2xx, None otherwise.
    """
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLAUDE_CODE_CLIENT_ID,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                OAUTH_TOKEN_URL,
                json=body,
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        log(f"OAuth refresh failed: {e}")
        return None
    if resp.status_code >= 400:
        log(f"OAuth refresh HTTP {resp.status_code}: {resp.text[:200]}")
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        log("OAuth refresh: response was not JSON")
        return None


async def get_fresh_token() -> str | None:
    """Read credentials, proactively refresh if the access token is within
    TOKEN_REFRESH_MARGIN_S of expiring, write the new tokens back, and
    return the access token to use for this poll.
    """
    blob = _read_credential_blob()
    if blob is None:
        return None

    # Locate the claudeAiOauth section (top-level or nested).
    section = blob.get("claudeAiOauth") if isinstance(blob.get("claudeAiOauth"), dict) else blob
    if not isinstance(section, dict):
        return None

    access = section.get("accessToken")
    refresh = section.get("refreshToken")
    expires_at_ms = section.get("expiresAt") or 0
    expires_at_s = expires_at_ms / 1000.0 if expires_at_ms else 0
    now = time.time()

    if expires_at_s and now + TOKEN_REFRESH_MARGIN_S < expires_at_s:
        # Fresh enough; use the cached access token.
        return access

    if not refresh:
        log("Access token expired and no refresh_token present")
        return access  # try once anyway — daemon will get 401 and back off

    log(f"Access token expires in {int((expires_at_s - now)/60)} min; refreshing")
    new_tokens = await _refresh_oauth(refresh)
    if new_tokens is None:
        return access  # fall back

    new_access = new_tokens.get("access_token")
    if not isinstance(new_access, str):
        log("OAuth refresh: response missing access_token")
        return access

    # Persist. Anthropic always rotates the refresh_token; keep the old one
    # if for some reason a new one isn't returned (shouldn't happen).
    section["accessToken"] = new_access
    if isinstance(new_tokens.get("refresh_token"), str):
        section["refreshToken"] = new_tokens["refresh_token"]
    expires_in = new_tokens.get("expires_in")
    if isinstance(expires_in, (int, float)):
        section["expiresAt"] = int((now + expires_in) * 1000)

    if _write_credential_blob(blob):
        log("Keychain updated with refreshed token")
    return new_access


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Accept both Linux MAC (AA:BB:CC:DD:EE:FF) and macOS CoreBluetooth UUID
    # (E621E1F8-C36C-495A-93FC-0C247A3E6E5F).
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


def save_address(addr: str) -> None:
    SAVED_ADDR_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAVED_ADDR_FILE.write_text(addr)


async def scan_for_device() -> str | None:
    log(f"Scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
    devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
    for d in devices:
        if d.name == DEVICE_NAME:
            log(f"Found: {d.address}")
            return d.address
    return None


# --- macOS: recover a device the OS already holds as an HID keyboard --------
#
# The firmware advertises as a BLE HID keyboard so its buttons type into the
# Mac. macOS auto-connects to that HID, and CoreBluetooth then EXCLUDES the
# peripheral from BleakScanner.discover() results (already-connected devices
# never appear in scans). bleak's connect-by-address path also scans
# internally, so a cached address can't help either. The documented escape
# hatch is retrieveConnectedPeripheralsWithServices_, which returns
# peripherals the system is already connected to. We wrap the result in a
# BLEDevice carrying the live (peripheral, manager) details so BleakClient
# connects to it directly without scanning. CoreBluetooth shares the single
# physical link, so this rides the existing HID connection — the keyboard
# keeps working.
_cb_manager = None  # reused CentralManagerDelegate (CoreBluetooth)


async def _get_cb_manager():
    """Lazily create and ready a shared CoreBluetooth central manager."""
    global _cb_manager
    if _cb_manager is None:
        from bleak.backends.corebluetooth.CentralManagerDelegate import (
            CentralManagerDelegate,
        )

        mgr = CentralManagerDelegate()
        await mgr.wait_until_ready()  # raises if Bluetooth is unauthorized/off
        _cb_manager = mgr
    return _cb_manager


async def retrieve_connected_macos(skip_addr: str | None = None):
    """Return a BLEDevice for a system-connected 'Claude Controller', or None.

    Two-step lookup, strongest signal first:

    1. Peripherals connected under our CUSTOM service UUID. Membership in
       that service is unambiguous (no other device exposes it), so we accept
       by service alone — the peripheral's name can be None on macOS.
    2. Fall back to the generic HID service 0x1812, but ONLY trust a
       peripheral whose name matches DEVICE_NAME. 0x1812 also matches
       unrelated keyboards/mice, so picking blindly here could grab the
       wrong device.

    ``skip_addr`` skips a peripheral whose UUID just failed to connect, so a
    stale CoreBluetooth handle can't trap us into never trying a fresh scan.
    """
    from CoreBluetooth import CBUUID
    from bleak.backends.device import BLEDevice

    try:
        manager = await _get_cb_manager()
    except Exception as e:  # BleakBluetoothNotAvailableError etc.
        log(f"CoreBluetooth unavailable: {e}")
        return None

    cm = manager.central_manager

    def _wrap(p):
        addr = p.identifier().UUIDString()
        log(f"Found system-connected peripheral: {p.name()!r} [{addr}]")
        return BLEDevice(addr, p.name(), (p, manager))

    def _ok(p) -> bool:
        return not (skip_addr and p.identifier().UUIDString() == skip_addr)

    # 1. Custom service — accept by service membership alone.
    custom = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_(SERVICE_UUID)]
    )
    for p in custom or []:
        if _ok(p):
            return _wrap(p)

    # 2. Generic HID service — require an exact name match.
    hid = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_("1812")]
    )
    for p in hid or []:
        if _ok(p) and p.name() == DEVICE_NAME:
            return _wrap(p)

    return None


async def discover_target(skip_addr: str | None = None):
    """Return a connectable target, or None.

    macOS: prefer the system-connected peripheral (HID-grabbed devices are
    invisible to scans); fall back to a normal scan that yields a BLEDevice
    so the subsequent connect doesn't have to re-scan. ``skip_addr`` is
    forwarded so a just-failed peripheral is skipped, making the scan
    fallback reachable.

    Other platforms: keep the original cached-address / scan-by-name flow.
    A freshly scanned address is cached here (the only place it's saved).
    """
    if sys.platform == "darwin":
        dev = await retrieve_connected_macos(skip_addr=skip_addr)
        if dev is not None:
            return dev
        log(f"Not held by OS; scanning for '{DEVICE_NAME}' ({SCAN_TIMEOUT}s)...")
        dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=SCAN_TIMEOUT)
        if dev:
            log(f"Found: {dev.address}")
        return dev

    address = load_cached_address()
    if not address:
        address = await scan_for_device()
        if address:
            save_address(address)  # cache only freshly-scanned addresses
    return address


# ──────────────────────────────────────────────────────────────────────────
# mateo/weekly-delta: JSONL-based delta vs previous 7d. Anthropic's rate-
# limit headers expose the current weekly utilization but not the previous
# week, so the "▲ X%" delta is computed from Claude Code's local session
# logs at ~/.claude/projects/**/*.jsonl. Files are cached by mtime+size so
# warm calls cost ~1 ms even with dozens of project dirs.
# ──────────────────────────────────────────────────────────────────────────
# Approximate Anthropic public API pricing per million tokens (USD).
# These are reference rates; users on Pro/Team/Max plans pay a flat fee,
# so cost shown on the device is "what this would cost on the API".
# Cache derivatives per docs: cache_read = input × 0.10,
# cache_creation = input × 1.25 (we don't distinguish 5min vs 1h).
_PRICES_PER_M = {
    "opus":   (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku":  (0.80, 4.0),
}


def _prices_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    if "opus" in m:   return _PRICES_PER_M["opus"]
    if "haiku" in m:  return _PRICES_PER_M["haiku"]
    return _PRICES_PER_M["sonnet"]   # default; covers sonnet + unknowns


def _record_cost_cents(usage: dict, input_price: float, output_price: float) -> int:
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cr  = int(usage.get("cache_read_input_tokens") or 0)
    cw  = int(usage.get("cache_creation_input_tokens") or 0)
    usd = (
        inp * input_price        / 1_000_000.0 +
        out * output_price       / 1_000_000.0 +
        cr  * input_price * 0.10 / 1_000_000.0 +
        cw  * input_price * 1.25 / 1_000_000.0
    )
    return int(round(usd * 100))


class JsonlAggregator:
    """Per-file mtime-cached aggregator. Each cached record is
    `(ts_seconds, total_tokens, cost_cents)` — the cost is computed
    once at parse time using `_PRICES_PER_M`.
    """

    def __init__(self) -> None:
        # path -> (mtime_ns, size, [(ts, tokens, cents), ...])
        self._cache: dict[Path, tuple[int, int, list[tuple[float, int, int]]]] = {}

    def _list_files(self) -> list[Path]:
        if not PROJECTS_DIR.exists():
            return []
        return list(PROJECTS_DIR.rglob("*.jsonl"))

    def _parse_file(self, path: Path) -> list[tuple[float, int, int]]:
        records: list[tuple[float, int, int]] = []
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            return records
        for line in text.splitlines():
            if not line or '"usage"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            ts_str = obj.get("timestamp")
            if not isinstance(usage, dict) or not ts_str:
                continue
            try:
                ts = time.mktime(time.strptime(ts_str[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                continue
            total = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
            )
            if total <= 0:
                continue
            ip, op = _prices_for(msg.get("model", ""))
            cents = _record_cost_cents(usage, ip, op)
            records.append((ts, total, cents))
        return records

    def _records(self) -> list[tuple[float, int, int]]:
        files = self._list_files()
        live = set(files)
        for stale in list(self._cache.keys()):
            if stale not in live:
                self._cache.pop(stale, None)
        all_records: list[tuple[float, int, int]] = []
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            cached = self._cache.get(f)
            if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                all_records.extend(cached[2])
                continue
            parsed = self._parse_file(f)
            self._cache[f] = (st.st_mtime_ns, st.st_size, parsed)
            all_records.extend(parsed)
        return all_records

    # ── derived metrics ─────────────────────────────────────────────────

    def delta_pct(self, window_end: float | None = None) -> float | None:
        """Percent change current-7d vs previous-7d, or None if prev is 0."""
        end = window_end if (window_end and window_end > 0) else time.time()
        cutoff_cur = end - WINDOW_7D_SECONDS
        cutoff_prev = cutoff_cur - WINDOW_7D_SECONDS
        cur_sum = 0
        prev_sum = 0
        for ts, total, _cents in self._records():
            if ts >= cutoff_cur:
                cur_sum += total
            elif ts >= cutoff_prev:
                prev_sum += total
        if prev_sum <= 0:
            return None
        return (cur_sum - prev_sum) / prev_sum * 100.0

    def weekly_tokens(self, window_end: float | None = None) -> int:
        """Total tokens in the same anchored 7d window used by delta_pct."""
        end = window_end if (window_end and window_end > 0) else time.time()
        cutoff = end - WINDOW_7D_SECONDS
        return sum(t for ts, t, _ in self._records() if ts >= cutoff)

    def weekly_cost_cents(self, window_end: float | None = None) -> int:
        end = window_end if (window_end and window_end > 0) else time.time()
        cutoff = end - WINDOW_7D_SECONDS
        return sum(c for ts, _, c in self._records() if ts >= cutoff)

    def daily_cost_cents(self) -> int:
        """Today's cost, local-time midnight as the lower boundary."""
        now = time.time()
        lt = time.localtime(now)
        midnight_struct = (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, lt.tm_isdst)
        cutoff = time.mktime(midnight_struct)
        return sum(c for ts, _, c in self._records() if ts >= cutoff)

    def streak_days(self) -> int:
        """Consecutive local-time days, ending today, with ≥1 record.
        Returns 0 if today has no activity yet (so the streak hasn't started
        for this day) — we still count today as "in progress" by giving
        credit if any record exists at all today.
        """
        active_days: set[tuple[int, int, int]] = set()
        for ts, _, _ in self._records():
            d = time.localtime(ts)
            active_days.add((d.tm_year, d.tm_mon, d.tm_mday))
        if not active_days:
            return 0
        # Walk backward from today; stop on the first inactive day.
        today = time.localtime(time.time())
        cursor = time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                              0, 0, 0, 0, 0, today.tm_isdst))
        streak = 0
        while True:
            d = time.localtime(cursor)
            key = (d.tm_year, d.tm_mon, d.tm_mday)
            if key in active_days:
                streak += 1
                cursor -= 86400.0
            else:
                # If today is empty, walk one day back and try again — but
                # only once, so a still-empty "yesterday" breaks the streak.
                if streak == 0:
                    cursor -= 86400.0
                    d = time.localtime(cursor)
                    if (d.tm_year, d.tm_mon, d.tm_mday) in active_days:
                        continue
                break
        return streak


async def poll_api(token: str, aggregator: "JsonlAggregator | None" = None) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        log(f"API call failed: {e}")
        return None
    if resp.status_code >= 400:
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    payload = {
        "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
        "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
        "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        "ok": True,
    }
    # mateo/weekly-delta: augment with JSONL-derived stats. Window is
    # anchored to Anthropic's actual weekly reset (matches the "Resets in
    # Xd Yh" countdown), with rolling-from-now fallback if the header is
    # missing (transient API failure).
    payload["t"] = int(time.time())   # epoch seconds, for the idle-clock screen
    # Local UTC offset in minutes — needed because the device clock is UTC.
    payload["tz"] = int(time.localtime().tm_gmtoff // 60)
    if aggregator is not None:
        try:
            window_end = float(hdr("anthropic-ratelimit-unified-7d-reset", "0"))
        except (TypeError, ValueError):
            window_end = 0.0
        we = window_end if window_end > 0 else None
        try:
            dp = aggregator.delta_pct(window_end=we)
            wt = aggregator.weekly_tokens(window_end=we)
            cd = aggregator.daily_cost_cents()
            cw = aggregator.weekly_cost_cents(window_end=we)
            ss = aggregator.streak_days()
        except Exception as e:
            log(f"Delta aggregation failed: {e}")
            dp = wt = cd = cw = ss = None
        if dp is not None:
            payload["dp"] = round(dp, 1)
            payload["hd"] = True
        if wt is not None: payload["wt"] = wt          # weekly tokens (int)
        if cd is not None: payload["cd"] = cd          # cost today, cents
        if cw is not None: payload["cw"] = cw          # cost week, cents
        if ss is not None: payload["ss"] = ss          # streak days
    return payload


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except BleakError as e:
            log(f"Write failed: {e}")
            return False


async def connect_and_run(
    target,
    stop_event: asyncio.Event,
    aggregator: JsonlAggregator,
) -> bool:
    """Connect to a target and poll until disconnected or stopped.

    ``target`` is either an address string (Linux) or a BLEDevice carrying
    live CoreBluetooth details (macOS). Returns True if the connection was
    used successfully (so the caller keeps the cached address), False if the
    connection failed and the cache should be invalidated.
    """
    display = target if isinstance(target, str) else target.address
    log(f"Connecting to {display}...")
    client = BleakClient(target)
    try:
        await client.connect()
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0
    used_successfully = False
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                token = await get_fresh_token()
                if not token:
                    log("No token; skipping poll")
                else:
                    payload = await poll_api(token, aggregator)
                    if payload is not None:
                        if await session.write_payload(payload):
                            last_poll = time.time()
                            used_successfully = True

            try:
                await asyncio.wait_for(session.refresh_requested.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)

    log("=== Claude Usage Tracker Daemon (BLE, macOS) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    # mateo/weekly-delta: persistent JSONL aggregator (mtime-cached).
    aggregator = JsonlAggregator()

    backoff = 1
    skip_addr: str | None = None  # macOS: a peripheral to skip for one cycle
    while not stop_event.is_set():
        # Apply any pending skip exactly once, then clear it so the next
        # cycle re-tries retrieveConnected (the device may have recovered).
        target = await discover_target(skip_addr=skip_addr)
        skip_addr = None
        if not target:
            log(f"Device not found, retrying in {backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
            continue

        addr = target if isinstance(target, str) else target.address
        ok = await connect_and_run(target, stop_event, aggregator)
        if not ok:
            if sys.platform == "darwin":
                # No string cache to drop; instead skip this stale handle on
                # the next retrieveConnected so the scan fallback is reachable.
                skip_addr = addr
            else:
                log("Invalidating cached address")
                SAVED_ADDR_FILE.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
