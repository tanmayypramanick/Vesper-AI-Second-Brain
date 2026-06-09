#!/bin/bash
python3 << 'PYEOF'
import subprocess, json, os

script = """
tell application "Calendar"
    set output to ""
    set s to current date - (30 * days)
    set e to current date + (90 * days)
    repeat with cal in calendars
        repeat with ev in events of cal
            try
                if (start date of ev) >= s and (start date of ev) <= e then
                    set t to summary of ev
                    set sd to start date of ev as string
                    set loc to ""
                    try
                        set loc to location of ev
                    end try
                    set output to output & t & "|" & sd & "|" & loc & "\n"
                end if
            end try
        end repeat
    end repeat
    return output
end tell
"""

result = subprocess.run(
    ['osascript', '-e', script],
    capture_output=True, text=True
)

events = []
for line in result.stdout.strip().split('\n'):
    if '|' in line:
        parts = line.split('|')
        if len(parts) >= 2:
            events.append({
                'title': parts[0].strip(),
                'start': parts[1].strip(),
                'location': parts[2].strip() if len(parts) > 2 else ''
            })

output = os.path.expanduser('~/vesper_agent/exports/calendar.json')
with open(output, 'w') as f:
    json.dump(events, f)
print(f"Exported {len(events)} events")
PYEOF

SSH_KEY="$HOME/.ssh/vesper_key"
scp -q -i "$SSH_KEY" ~/vesper_agent/exports/calendar.json tanmay@100.123.15.32:/home/tanmay/vesper/data/calendar/ 2>/dev/null \
|| scp -q -i "$SSH_KEY" ~/vesper_agent/exports/calendar.json tanmay@100.123.15.32:/mnt/hdd/vesper/data/calendar/ 2>/dev/null \
|| scp -q -i "$SSH_KEY" ~/vesper_agent/exports/calendar.json tanmay@10.0.0.120:/home/tanmay/vesper/data/calendar/ 2>/dev/null \
|| echo "SCP unavailable — saved locally, will sync when server is reachable"
echo "Calendar synced: $(date)"
