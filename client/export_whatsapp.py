#!/usr/bin/env python3
"""
VESPER WHATSAPP EXPORTER
Scans iCloud Downloads and local Downloads for WhatsApp chat export zips,
parses them, writes to exports/whatsapp/whatsapp_messages.json, SCPs to server.

iPhone export steps:
  WhatsApp → open chat → ⋮ → More → Export Chat → Without Media
  → Save to Files → iCloud Drive/Downloads
"""

import os, re, json, zipfile, glob, subprocess, sys
from datetime import datetime

EXPORT_DIR = os.path.expanduser("~/vesper_agent/exports/whatsapp")
PROC_FILE  = os.path.join(EXPORT_DIR, "processed_files.json")
OUT_FILE   = os.path.join(EXPORT_DIR, "whatsapp_messages.json")
SSH_KEY    = os.path.expanduser("~/.ssh/vesper_key")
REMOTE     = "tanmay@100.123.15.32:/home/tanmay/vesper/data/whatsapp/"

os.makedirs(EXPORT_DIR, exist_ok=True)

# ── Regex: matches all WhatsApp date/time formats ──────────────────────────
# [M/D/YY, HH:MM:SS] or [DD/MM/YYYY, HH:MM:SS] or with AM/PM
# The ‎? handles the invisible LTR mark WhatsApp prepends to system lines
MESSAGE_RE = re.compile(
    r'^‎?\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*'
    r'(\d{1,2}:\d{2}:\d{2}(?:\s*[APap][Mm])?)\]\s*'
    r'(.+?):\s+(.+)$'
)

# Characters to strip from sender/body (unicode directional marks)
STRIP_CHARS = '‎‏‪‬'

SYSTEM_SUBSTRINGS = [
    "end-to-end encrypted",
    "Missed video call",
    "Missed voice call",
    "missed video call",
    "missed voice call",
    "changed the subject",
    "added you",
    " left",
    "changed this group",
    "joined using",
    " removed ",
    "<Media omitted>",
    "document omitted",
    "image omitted",
    "video omitted",
    "audio omitted",
    "sticker omitted",
    "GIF omitted",
    "message was deleted",
    "deleted this message",
    "changed their phone number",
    "was added",
    "security code changed",
    "created group",
    "You're now an admin",
    "You are now an admin",
    "Tap to call back",
]

def is_system(text):
    clean = text.strip(STRIP_CHARS)
    return any(s in clean for s in SYSTEM_SUBSTRINGS)

def parse_whatsapp_txt(text, chat_name="unknown"):
    messages = []
    current  = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = MESSAGE_RE.match(line)
        if m:
            if current:
                messages.append(current)
                current = None
            date_str, time_str, sender, body = m.groups()
            body   = body.strip(STRIP_CHARS).strip()
            sender = sender.strip(STRIP_CHARS).strip()
            if is_system(body) or len(body) < 2:
                continue
            current = {
                "date":   f"{date_str} {time_str}",
                "sender": sender,
                "text":   body,
                "chat":   chat_name,
                "source": "whatsapp",
            }
        elif current:
            extra = line.strip(STRIP_CHARS).strip()
            if extra:
                current["text"] += " " + extra
    if current:
        messages.append(current)
    return messages

def find_exports():
    search_dirs = [
        os.path.expanduser("~/Downloads"),
        os.path.expanduser("~/Desktop"),
        os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Downloads"),
        os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Desktop"),
        os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs"),
    ]
    found = []
    for d in search_dirs:
        if not os.path.exists(d):
            continue
        for p in glob.glob(os.path.join(d, "WhatsApp Chat*.zip")):
            found.append(("zip", p))
        for p in glob.glob(os.path.join(d, "WhatsApp Chat*.txt")):
            found.append(("txt", p))
        for p in glob.glob(os.path.join(d, "*", "WhatsApp Chat*.txt")):
            found.append(("txt", p))
    # Deduplicate by path
    seen = set()
    unique = []
    for item in found:
        if item[1] not in seen:
            seen.add(item[1])
            unique.append(item)
    return unique

def load_processed():
    try:
        return set(json.load(open(PROC_FILE)))
    except Exception:
        return set()

def save_processed(s):
    with open(PROC_FILE, "w") as f:
        json.dump(list(s), f)

def load_existing():
    try:
        return json.load(open(OUT_FILE))
    except Exception:
        return []

def main():
    processed     = load_processed()
    all_messages  = load_existing()
    existing_keys = {(m["date"], m["sender"], m["chat"]) for m in all_messages}
    new_count     = 0
    exports       = find_exports()

    if not exports:
        print("No WhatsApp exports found.")
        print("Export from iPhone: WhatsApp → chat → ⋮ → More → Export Chat → Without Media")
        print("Then save to iCloud Drive/Downloads → auto-syncs to Mac.")
        return

    for kind, fpath in exports:
        fname     = os.path.basename(fpath)
        chat_name = re.sub(r'^WhatsApp Chat\s*[-–]\s*', '', fname, flags=re.IGNORECASE)
        chat_name = re.sub(r'\.(zip|txt)$', '', chat_name, flags=re.IGNORECASE).strip()
        if not chat_name:
            chat_name = re.sub(r'\.(zip|txt)$', '', fname, flags=re.IGNORECASE).strip()

        if fpath in processed:
            print(f"⏭️  {fname} (already processed)")
            continue

        texts = []
        if kind == "zip":
            try:
                with zipfile.ZipFile(fpath) as zf:
                    for inner in zf.namelist():
                        if inner.endswith(".txt"):
                            texts.append(zf.read(inner).decode("utf-8", errors="replace"))
            except Exception as e:
                print(f"⚠️  Cannot read {fname}: {e}")
                continue
        else:
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    texts.append(f.read())
            except Exception as e:
                print(f"⚠️  Cannot read {fname}: {e}")
                continue

        file_msgs = []
        for text in texts:
            file_msgs.extend(parse_whatsapp_txt(text, chat_name))

        new_msgs = [
            m for m in file_msgs
            if (m["date"], m["sender"], m["chat"]) not in existing_keys
        ]
        all_messages.extend(new_msgs)
        existing_keys.update((m["date"], m["sender"], m["chat"]) for m in new_msgs)
        new_count += len(new_msgs)

        # Only mark as processed if we got messages (so we retry if 0)
        if len(file_msgs) > 0:
            processed.add(fpath)
            print(f"✅ {fname}: {len(new_msgs)} new messages ({len(file_msgs)} total in file)")
        else:
            print(f"⚠️  {fname}: 0 messages parsed (will retry next run)")

    # Sort newest-ish first (approximate — date strings are not ISO)
    all_messages = all_messages  # keep insertion order; server can sort

    with open(OUT_FILE, "w") as f:
        json.dump(all_messages, f, ensure_ascii=False, indent=2)

    save_processed(processed)
    print(f"Total WhatsApp messages: {len(all_messages)} (+{new_count} new)")

    # SCP to server
    if new_count > 0 and os.path.exists(OUT_FILE):
        result = subprocess.run(
            ["scp", "-q", "-i", SSH_KEY, OUT_FILE, REMOTE],
            capture_output=True
        )
        if result.returncode == 0:
            print(f"✅ Synced to server")
        else:
            print(f"⚠️  SCP failed — saved locally at {OUT_FILE}")

if __name__ == "__main__":
    main()
