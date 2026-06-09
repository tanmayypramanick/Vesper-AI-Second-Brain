#!/bin/bash
# Vesper iMessages export — decodes both text and attributedBody columns
# macOS Ventura+ stores message content in attributedBody (binary typedstream),
# not in the plain text column. This script handles both.

mkdir -p ~/vesper_agent/exports

python3 << 'PYEOF'
import sqlite3, os, re, json
from datetime import datetime

DB  = os.path.expanduser("~/Library/Messages/chat.db")
OUT = os.path.expanduser("~/vesper_agent/exports/imessages.json")

def decode_attributed_body(blob):
    """
    Extract plain text from an NSAttributedString typedstream binary blob.
    Apple moved message text from the `text` column to `attributedBody`
    starting in macOS Ventura / iOS 16.
    """
    if not blob:
        return None
    try:
        raw = blob.decode("utf-8", errors="replace")

        SKIP_EXACT = {
            "streamtyped", "NSMutableAttributedString", "NSAttributedString",
            "NSObject", "NSMutableString", "NSString", "NSDictionary", "NSArray",
            "NSValue", "NSNumber", "NSColor", "NSFont",
        }
        SKIP_PREFIX = ("__kIM", "_kIM", "NS", "kIM")

        # Pull all printable ASCII runs (>=4 chars)
        chunks = re.findall(r"[\x20-\x7e]{4,}", raw)

        result = []
        for chunk in chunks:
            # Strip leading non-alphanumeric junk (length bytes that leak as printable)
            chunk = re.sub(r'^[^a-zA-Z0-9\'"({\[<@#$%!?/\\-]+', "", chunk)
            if not chunk:
                continue
            if chunk in SKIP_EXACT:
                continue
            if any(chunk.startswith(p) for p in SKIP_PREFIX):
                continue
            result.append(chunk)

        text = " ".join(result).strip()
        return text if text else None
    except Exception:
        return None


try:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    cur.execute("""
        SELECT
            m.handle_id,
            m.is_from_me,
            m.date,
            m.text,
            m.attributedBody,
            m.service
        FROM message m
        WHERE m.handle_id IS NOT NULL
          AND m.handle_id != 0
        ORDER BY m.date DESC
        LIMIT 5000
    """)

    rows = cur.fetchall()
    messages = []

    for row in rows:
        # Prefer plain text column; fall back to attributedBody decode
        text = row["text"]
        if not text or text.strip() == "":
            text = decode_attributed_body(row["attributedBody"])

        if not text or len(text.strip()) < 2:
            continue  # skip attachment-only or empty messages

        # Resolve handle_id -> phone/email
        cur.execute("SELECT id FROM handle WHERE rowid = ?", (row["handle_id"],))
        handle_row = cur.fetchone()
        contact = handle_row[0] if handle_row else f"handle_{row['handle_id']}"

        # Convert Apple epoch to readable date.
        # Modern macOS (Ventura+) stores nanoseconds since Jan 1 2001.
        # Values > 1e12 are nanoseconds; smaller values are legacy seconds.
        apple_ts = row["date"]
        try:
            if apple_ts and apple_ts > 1_000_000_000_000:
                unix_ts = apple_ts / 1_000_000_000 + 978307200  # nanoseconds
            else:
                unix_ts = apple_ts + 978307200                   # legacy seconds
            dt = datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            dt = "unknown"

        messages.append({
            "date":       dt,
            "contact":    contact,
            "text":       text.strip(),
            "is_from_me": int(row["is_from_me"]),
            "service":    row["service"] or "iMessage",
        })

    db.close()

    with open(OUT, "w") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)

    print(f"Exported {len(messages)} messages")

except sqlite3.OperationalError as e:
    print(f"DB error: {e}")
    print("Make sure Terminal has Full Disk Access in System Settings > Privacy & Security")
    exit(1)
except Exception as e:
    print(f"Unexpected error: {e}")
    exit(1)
PYEOF

# Sync to server — Tailscale first (always reachable), fallback to local IP
SSH_KEY="$HOME/.ssh/vesper_key"
REMOTE="tanmay@100.123.15.32"
REMOTE_LOCAL="tanmay@10.0.0.120"
REMOTE_PATH="/home/tanmay/vesper/data/imessages/"

scp -q -i "$SSH_KEY" ~/vesper_agent/exports/imessages.json "${REMOTE}:${REMOTE_PATH}" 2>/dev/null \
|| scp -q -i "$SSH_KEY" ~/vesper_agent/exports/imessages.json "${REMOTE}:/mnt/hdd/vesper/data/imessages/" 2>/dev/null \
|| scp -q -i "$SSH_KEY" ~/vesper_agent/exports/imessages.json "${REMOTE_LOCAL}:${REMOTE_PATH}" 2>/dev/null \
|| echo "SCP unavailable — file saved locally, will sync when SSH is configured"

echo "iMessages synced: $(date)"
