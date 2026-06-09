#!/bin/bash
# Export Apple Notes to server for ChromaDB ingestion
SERVER="https://10.0.0.120:5000"
TMPFILE="/tmp/vesper_notes_export.txt"

osascript << 'EOF' > "$TMPFILE" 2>/dev/null
set output to ""
tell application "Notes"
    repeat with theFolder in folders
        try
            set folderName to name of theFolder
            repeat with theNote in notes of theFolder
                try
                    set noteTitle to name of theNote
                    set noteBody to plaintext of theNote
                    set noteDate to modification date of theNote
                    set output to output & "=== NOTE: " & noteTitle & " [" & folderName & "] [" & noteDate & "] ===" & linefeed
                    set output to output & noteBody & linefeed & linefeed
                end try
            end repeat
        end try
    end repeat
end tell
return output
EOF

if [ ! -s "$TMPFILE" ]; then
    echo "No notes exported"
    exit 0
fi

# Split into chunks and POST each to server
python3 << PYEOF
import requests, urllib3, re, os
urllib3.disable_warnings()
SERVER = "$SERVER"

with open("$TMPFILE") as f:
    content = f.read()

# Split on note boundaries
notes = re.split(r'=== NOTE: ', content)
count = 0
for note in notes:
    note = note.strip()
    if len(note) < 20:
        continue
    # Extract title (first line before [folder])
    first_line = note.split('\n')[0]
    body = '\n'.join(note.split('\n')[1:]).strip()
    doc = f"[Apple Note] {first_line}\n{body[:1500]}"
    try:
        r = requests.post(f"{SERVER}/store_memory",
            json={"text": doc, "category": "notes", "source": "apple_notes"},
            verify=False, timeout=15)
        if r.json().get("stored"):
            count += 1
    except Exception as e:
        print(f"Error: {e}")
print(f"Stored {count} notes to Vesper")
PYEOF

rm -f "$TMPFILE"
