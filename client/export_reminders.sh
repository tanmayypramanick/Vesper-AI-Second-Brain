#!/bin/bash
# Export Apple Reminders to server
SERVER="https://10.0.0.120:5000"
TMPFILE="/tmp/vesper_reminders_export.json"

python3 << 'PYEOF' > "$TMPFILE" 2>/dev/null
import subprocess, json, sys

script = '''
set output to "["
set needComma to false
tell application "Reminders"
    repeat with theList in lists
        try
            set listName to name of theList
            repeat with theReminder in reminders of theList
                try
                    set rName to name of theReminder
                    set rDone to completed of theReminder
                    set rDue to ""
                    try
                        set rDue to (due date of theReminder) as string
                    end try
                    set rNotes to ""
                    try
                        set rNotes to body of theReminder
                    end try
                    if needComma then
                        set output to output & ","
                    end if
                    set output to output & "{\\"list\\":\\"" & listName & "\\",\\"name\\":\\"" & rName & "\\",\\"done\\":" & rDone & ",\\"due\\":\\"" & rDue & "\\",\\"notes\\":\\"" & rNotes & "\\"}"
                    set needComma to true
                end try
            end repeat
        end try
    end repeat
end tell
return output & "]"
'''
result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=30)
print(result.stdout.strip())
PYEOF

if [ ! -s "$TMPFILE" ] || [ "$(cat $TMPFILE)" = "[]" ]; then
    echo "No reminders"
    exit 0
fi

python3 << PYEOF
import requests, urllib3, json, datetime
urllib3.disable_warnings()
SERVER = "$SERVER"

with open("$TMPFILE") as f:
    content = f.read().strip()

try:
    items = json.loads(content)
except:
    print(f"Parse error: {content[:200]}")
    exit()

# Group into one batch doc per list
lists = {}
for item in items:
    lst = item.get('list', 'Reminders')
    lists.setdefault(lst, []).append(item)

count = 0
for lst_name, reminders in lists.items():
    lines = [f"[Apple Reminders] List: {lst_name}"]
    for r in reminders:
        status = "✓" if r.get('done') else "○"
        due = f" (due: {r['due']})" if r.get('due') else ""
        notes = f" — {r['notes']}" if r.get('notes') else ""
        lines.append(f"  {status} {r['name']}{due}{notes}")
    doc = '\n'.join(lines)
    try:
        resp = requests.post(f"{SERVER}/store_memory",
            json={"text": doc, "category": "reminders", "source": "apple_reminders"},
            verify=False, timeout=15)
        if resp.json().get("stored"):
            count += 1
    except Exception as e:
        print(f"Error: {e}")
print(f"Stored {count} reminder lists ({len(items)} items total)")
PYEOF
rm -f "$TMPFILE"
