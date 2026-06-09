# ─────────────────────────────────────────
# VESPER CALENDAR INGESTION
# ─────────────────────────────────────────

import json
import os
import sys
sys.path.append('/home/tanmay/vesper')
from pipelines.memory_client import store_memory, get_memory_count
from config import DATA_PATH

CALENDAR_FILE  = f"{DATA_PATH}/calendar/calendar.json"
PROCESSED_FILE = f"{DATA_PATH}/calendar/processed.json"

def load_processed():
    try:
        if os.path.exists(PROCESSED_FILE):
            with open(PROCESSED_FILE) as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def save_processed(ids):
    try:
        with open(PROCESSED_FILE, "w") as f:
            json.dump(list(ids), f)
    except Exception:
        pass

def ingest_calendar():
    if not os.path.exists(CALENDAR_FILE):
        print("No calendar file")
        return 0

    with open(CALENDAR_FILE) as f:
        events = json.load(f)

    processed = load_processed()
    count     = 0

    for event in events:
        title    = event.get('title', '').strip()
        start    = event.get('start', '')
        end      = event.get('end', '')
        location = event.get('location', '').strip()
        notes    = event.get('notes', '').strip()

        if not title:
            continue

        event_key = f"{title}_{start}"
        if event_key in processed:
            continue

        memory = f"[Calendar] {title} | Start: {start}"
        if end:
            memory += f" | End: {end}"
        if location:
            memory += f" | Location: {location}"
        if notes:
            memory += f" | Notes: {notes[:200]}"

        store_memory(
            memory,
            category="calendar",
            source="apple_calendar"
        )
        processed.add(event_key)
        count += 1

    save_processed(processed)
    print(f"✅ Calendar: {count} events ingested")
    return count

if __name__ == "__main__":
    ingest_calendar()