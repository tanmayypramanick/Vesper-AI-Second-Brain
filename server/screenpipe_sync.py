# ─────────────────────────────────────────
# VESPER SCREENPIPE SYNC
# Pulls Mac screen data → Chroma
# ─────────────────────────────────────────

import requests
import sys
sys.path.append('/home/tanmay/vesper')
from pipelines.memory import store_memory
from config import MAC_LOCAL_IP, SCREENPIPE_PORT

SCREENPIPE = f"http://{MAC_LOCAL_IP}:{SCREENPIPE_PORT}"

SKIP_APPS = [
    'screenpipe', 'terminal', 'finder',
    'system preferences', 'activity monitor',
    'iterm', 'iterm2'
]

def sync_screen():
    try:
        res = requests.get(
            f"{SCREENPIPE}/search",
            params={"limit": 100, "content_type": "ocr"},
            timeout=15
        )
        if res.status_code != 200:
            return
        count = 0
        for item in res.json().get("data", []):
            content = item.get("content", {})
            text = content.get("text", "").strip()
            app = content.get("app_name", "unknown").lower()
            if app in SKIP_APPS or len(text) < 50:
                continue
            store_memory(
                f"[Mac Screen - {app}]: {text[:800]}",
                category="mac_screen",
                source=app
            )
            count += 1
        print(f"✅ Screen: {count} captures synced")
    except Exception as e:
        print(f"❌ Screen sync: {e}")

def sync_audio():
    try:
        res = requests.get(
            f"{SCREENPIPE}/search",
            params={"limit": 50, "content_type": "audio"},
            timeout=15
        )
        if res.status_code != 200:
            return
        count = 0
        for item in res.json().get("data", []):
            text = item.get(
                "content", {}
            ).get("transcription", "").strip()
            if len(text) > 20:
                store_memory(
                    f"[Mac Audio]: {text[:800]}",
                    category="mac_audio",
                    source="mic"
                )
                count += 1
        print(f"✅ Audio: {count} transcriptions synced")
    except Exception as e:
        print(f"❌ Audio sync: {e}")

if __name__ == "__main__":
    sync_screen()
    sync_audio()