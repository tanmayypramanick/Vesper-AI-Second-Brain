# ─────────────────────────────────────────
# VESPER IMESSAGE INGESTION
# ─────────────────────────────────────────

import json
import os
import sys
sys.path.append('/home/tanmay/vesper')
from pipelines.memory_client import store_memory, get_memory_count
from config import DATA_PATH

MESSAGES_FILE   = f"{DATA_PATH}/imessages/imessages.json"
PROCESSED_FILE  = f"{DATA_PATH}/imessages/processed.json"

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
            json.dump(list(ids)[-10000:], f)
    except Exception:
        pass

def ingest_messages():
    if not os.path.exists(MESSAGES_FILE):
        print("No iMessages file")
        return 0

    with open(MESSAGES_FILE) as f:
        messages = json.load(f)

    processed = load_processed()
    count     = 0

    for msg in messages:
        text    = msg.get('text', '').strip()
        contact = msg.get('contact', 'unknown')
        date    = msg.get('date', '')
        is_me   = msg.get('is_from_me', 0)

        if not text or len(text) < 3:
            continue

        # Unique key per message
        msg_key = f"{date}_{contact}_{text[:20]}"
        if msg_key in processed:
            continue

        direction = "You to" if is_me else "From"

        store_memory(
            f"[iMessage {date}] "
            f"{direction} {contact}: {text}",
            category="imessage",
            source="iMessage"
        )
        processed.add(msg_key)
        count += 1

    save_processed(processed)
    print(f"✅ iMessages: {count} new messages ingested")
    return count

if __name__ == "__main__":
    ingest_messages()