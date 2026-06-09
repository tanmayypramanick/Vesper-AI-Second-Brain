#!/usr/bin/env python3
"""
VESPER SCREENPIPE SYNC
Reads OCR + audio from screenpipe API and syncs to Vesper memory.
Run every 15 minutes via launchd.

Screenpipe API: http://localhost:3030
Vesper receiver: http://100.123.15.32:5000 (Tailscale)
"""

import json, os, requests, time
from datetime import datetime, timedelta
from pathlib import Path

SCREENPIPE_URL  = "http://localhost:3030"
VESPER_URL      = "http://10.0.0.120:5000"      # LAN (port 5000 only reachable via LAN, not Tailscale)
VESPER_URL_BK   = "http://100.123.15.32:5000"   # Tailscale fallback (may be blocked)
STATE_FILE      = Path.home() / "vesper_agent" / "screenpipe_state.json"
SYNC_WINDOW_MIN = 20   # fetch last N minutes on each run
MIN_TEXT_LEN    = 30   # skip very short snippets
MAX_SEND        = 100  # max items per run (don't flood)

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            pass
    # Default: start from 20 min ago
    default_ts = (datetime.utcnow() - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S")
    return {"last_synced": default_ts, "total_sent": 0}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def screenpipe_search(start_time, end_time, content_type="all", limit=200):
    try:
        params = {
            "start_time": start_time,
            "end_time": end_time,
            "content_type": content_type,
            "limit": limit,
            "offset": 0
        }
        r = requests.get(f"{SCREENPIPE_URL}/search", params=params, timeout=15)
        if r.status_code == 200:
            return r.json().get("data", [])
    except Exception as e:
        print(f"Screenpipe API error: {e}")
    return []

def vesper_store(text, category, source):
    try:
        r = requests.post(
            f"{VESPER_URL}/store_memory",
            json={"text": text, "category": category, "source": source},
            timeout=60
        )
        return r.status_code in (200, 202)
    except Exception as e:
        print(f"Vesper store error: {e}")
        return False

def vesper_health():
    for url in [VESPER_URL, VESPER_URL_BK]:
        try:
            r = requests.get(f"{url}/health", timeout=5)
            if r.status_code == 200:
                return True
        except:
            continue
    return False

def main():
    print(f"Screenpipe Sync — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Check if screenpipe is running
    try:
        r = requests.get(f"{SCREENPIPE_URL}/health", timeout=3)
        if r.status_code != 200:
            print("Screenpipe not running or unhealthy. Skipping.")
            return
    except:
        print("Screenpipe API unreachable. Is screenpipe running?")
        return

    # Check if Vesper is reachable
    if not vesper_health():
        print("Vesper server unreachable. Skipping.")
        return

    state = load_state()
    last_synced = state["last_synced"]
    now_utc = datetime.utcnow()
    end_time = now_utc.strftime("%Y-%m-%dT%H:%M:%S")

    print(f"Fetching from {last_synced} to {end_time}")

    items = screenpipe_search(start_time=last_synced, end_time=end_time, content_type="all")
    print(f"Found {len(items)} items from screenpipe")

    sent = 0
    skipped = 0

    for item in items[:MAX_SEND]:
        content_type = item.get("type", "").upper()
        content = item.get("content", {})

        if content_type == "OCR":
            text = content.get("text", "").strip()
            if not text or len(text) < MIN_TEXT_LEN:
                skipped += 1
                continue
            app = content.get("app_name", "unknown")
            window = content.get("window_name", "")
            ts = content.get("timestamp", "")

            # Skip sensitive apps
            skip_apps = {"1Password", "Bitwarden", "Keychain", "Banking", "Terminal"}
            if any(s.lower() in app.lower() for s in skip_apps):
                skipped += 1
                continue

            mem = f"[Screen OCR | {app}] {window}\n{text[:500]}"
            if vesper_store(mem, "screen_ocr", f"screen:{app}"):
                sent += 1
            else:
                skipped += 1

        elif content_type == "AUDIO":
            text = content.get("transcription", "").strip()
            if not text or len(text) < MIN_TEXT_LEN:
                skipped += 1
                continue
            device = content.get("device_name", "mic")
            ts = content.get("timestamp", "")

            mem = f"[Audio Transcription | {device}] {ts}\n{text[:800]}"
            if vesper_store(mem, "screen_audio", f"audio:{device}"):
                sent += 1
            else:
                skipped += 1

    state["last_synced"] = end_time
    state["total_sent"] = state.get("total_sent", 0) + sent
    save_state(state)

    print(f"Sent: {sent}, Skipped: {skipped}, Total ever: {state['total_sent']}")

if __name__ == "__main__":
    main()
