#!/bin/bash
# Called by Mac Shortcuts app when a new iMessage arrives
# Arg 1: sender, Arg 2: message text
# Usage: ./imessage_realtime.sh "+1234567890" "Hey, how are you?"
SENDER="${1:-unknown}"
MESSAGE="${2:-}"
SERVER="https://10.0.0.120:5000"

if [ -z "$MESSAGE" ]; then exit 0; fi

python3 - << PYEOF
import requests, urllib3, datetime
urllib3.disable_warnings()
doc = f"[iMessage Live] From {repr('$SENDER')}: $MESSAGE"
doc = doc[:500]
ts  = datetime.datetime.now().isoformat()
try:
    r = requests.post("$SERVER/store_memory",
        json={"text": doc, "category": "imessage", "source": "imessage_realtime"},
        verify=False, timeout=8)
    print(f"iMessage stored: {r.json()}")
except Exception as e:
    print(f"Error: {e}")
PYEOF
