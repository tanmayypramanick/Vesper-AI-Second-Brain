#!/bin/bash
mkdir -p ~/vesper_agent/exports

CHROME="$HOME/Library/Application Support/Google/Chrome/Default/History"
if [ -f "$CHROME" ]; then
    cp "$CHROME" /tmp/chrome_copy.db
    sqlite3 -json /tmp/chrome_copy.db "
    SELECT url, title,
    datetime(last_visit_time/1000000-11644473600,'unixepoch') as visited_at
    FROM urls WHERE title != '' AND last_visit_time > 0
    AND url NOT LIKE 'chrome://%' AND url NOT LIKE 'about:%'
    ORDER BY last_visit_time DESC LIMIT 2000
    " > ~/vesper_agent/exports/browser_history.json 2>/dev/null
    rm /tmp/chrome_copy.db
fi

SSH_KEY="$HOME/.ssh/vesper_key"
# Primary: main data path; Fallback: /mnt/hdd staging (used when root FS is read-only)
scp -q -i "$SSH_KEY" ~/vesper_agent/exports/browser_history.json tanmay@100.123.15.32:/home/tanmay/vesper/data/browser/ 2>/dev/null \
|| scp -q -i "$SSH_KEY" ~/vesper_agent/exports/browser_history.json tanmay@100.123.15.32:/mnt/hdd/vesper/data/browser/ 2>/dev/null \
|| scp -q -i "$SSH_KEY" ~/vesper_agent/exports/browser_history.json tanmay@10.0.0.120:/home/tanmay/vesper/data/browser/ 2>/dev/null \
|| echo "SCP unavailable — saved locally, will sync when server is reachable"
echo "Browser synced: $(date)"
