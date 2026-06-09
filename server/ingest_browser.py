# ─────────────────────────────────────────
# VESPER BROWSER HISTORY INGESTION
# ─────────────────────────────────────────

import json
import os
import sys
sys.path.append('/home/tanmay/vesper')
from pipelines.memory_client import store_memory, get_memory_count
from config import DATA_PATH

HISTORY_FILE = f"{DATA_PATH}/browser/browser_history.json"
PROCESSED_FILE = f"{DATA_PATH}/browser/last_processed.json"

SKIP_URLS = [
    'localhost', 'chrome://', 'about:',
    'file://', '127.0.0.1', 'newtab',
    'blank', 'extensions', 'settings',
    'google.com/search', 'bing.com/search'
]

def load_processed():
    try:
        if os.path.exists(PROCESSED_FILE):
            with open(PROCESSED_FILE) as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def save_processed(urls):
    try:
        with open(PROCESSED_FILE, "w") as f:
            json.dump(list(urls)[-5000:], f)
    except Exception:
        pass

def ingest_browser():
    if not os.path.exists(HISTORY_FILE):
        print("No browser history file found")
        return 0

    with open(HISTORY_FILE) as f:
        history = json.load(f)

    processed = load_processed()
    count     = 0

    for item in history:
        url      = item.get('url', '')
        title    = item.get('title', '').strip()
        visited  = item.get('visited_at', '')
        visits   = item.get('visit_count', 1)

        if any(s in url for s in SKIP_URLS):
            continue
        if not title or len(title) < 3:
            continue

        # Skip already processed URLs
        url_key = f"{url}_{visited}"
        if url_key in processed:
            continue

        store_memory(
            f"[Browser {visited}] "
            f"Visited {visits}x: {title} | {url}",
            category="browser",
            source="chrome"
        )
        processed.add(url_key)
        count += 1

    save_processed(processed)
    print(f"✅ Browser: {count} new entries ingested")
    return count

if __name__ == "__main__":
    ingest_browser()