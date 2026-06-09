#!/usr/bin/env python3
"""
VESPER GMAIL EXPORTER
Pulls Gmail INBOX + Sent via IMAP → JSON → SCPs to server.
Credentials are read from export_gmail.sh (single source of truth).
"""

import imaplib, email as email_lib, json, os, re, subprocess, sys
from email.header import decode_header

# ── Read creds from the shell script so they stay in one place ──
def get_creds():
    script = os.path.expanduser("~/vesper_agent/export_gmail.sh")
    addr = pw = ""
    try:
        for line in open(script):
            m = re.match(r'^GMAIL_ADDRESS="(.+)"', line.strip())
            if m: addr = m.group(1)
            m = re.match(r'^GMAIL_APP_PASSWORD="(.+)"', line.strip())
            if m: pw = m.group(1)
    except Exception as e:
        print(f"❌ Could not read credentials from export_gmail.sh: {e}")
        sys.exit(1)
    return addr, pw

GMAIL_ADDRESS, GMAIL_APP_PASSWORD = get_creds()
IMAP_HOST  = "imap.gmail.com"
OUT_FILE   = os.path.expanduser("~/vesper_agent/exports/gmail/gmail_emails.json")
PROC_FILE  = os.path.expanduser("~/vesper_agent/exports/gmail/processed_ids.json")
SSH_KEY    = os.path.expanduser("~/.ssh/vesper_key")
REMOTE     = "tanmay@100.123.15.32:/home/tanmay/vesper/data/emails/"
REMOTE_HDD = "tanmay@100.123.15.32:/mnt/hdd/vesper/data/emails/"  # fallback when root FS read-only
FETCH_LIMIT = 500
BODY_LIMIT  = 1200

FOLDERS = [
    ("INBOX",               "email_received"),
    ("[Gmail]/Sent Mail",   "email_sent"),
]

SKIP_SENDERS = [
    "noreply", "no-reply", "mailer-daemon", "notifications",
    "newsletter", "unsubscribe", "donotreply", "do-not-reply",
    "postmaster", "bounces", "automated", "support@",
]

def decode_str(s):
    if not s: return ""
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)

def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                    break
                except Exception:
                    continue
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            body = ""
    lines = [l for l in body.splitlines() if not l.strip().startswith(">")]
    return "\n".join(lines).strip()[:BODY_LIMIT]

def load_processed():
    try: return set(json.load(open(PROC_FILE)))
    except: return set()

def save_processed(s):
    with open(PROC_FILE, "w") as f:
        json.dump(list(s)[-20000:], f)

def load_existing():
    try: return json.load(open(OUT_FILE))
    except: return []

def main():
    if not GMAIL_APP_PASSWORD:
        print("❌ GMAIL_APP_PASSWORD not set in export_gmail.sh")
        sys.exit(1)

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        print(f"✅ Connected to Gmail as {GMAIL_ADDRESS}")
    except Exception as e:
        print(f"❌ Gmail login failed: {e}")
        sys.exit(1)

    processed = load_processed()
    existing  = load_existing()
    new_count = 0

    for folder_name, category in FOLDERS:
        try:
            # Use proper IMAP quoting for folders with brackets
            status, _ = mail.select(f'"{folder_name}"', readonly=True)
            if status != "OK":
                print(f"  ⚠️  Could not select {folder_name}, trying unquoted...")
                status, _ = mail.select(folder_name, readonly=True)
                if status != "OK":
                    print(f"  ❌ Skipping {folder_name}")
                    continue

            status, data = mail.search(None, "ALL")
            if status != "OK" or not data[0]:
                continue

            ids    = data[0].split()
            recent = ids[-FETCH_LIMIT:]
            folder_new = 0

            for uid in reversed(recent):
                uid_key = f"{folder_name}:{uid.decode()}"
                if uid_key in processed:
                    continue
                try:
                    status, data = mail.fetch(uid, "(RFC822)")
                    if status != "OK": continue
                    msg     = email_lib.message_from_bytes(data[0][1])
                    subject = decode_str(msg.get("subject", ""))
                    sender  = decode_str(msg.get("from", ""))
                    to      = decode_str(msg.get("to", ""))
                    date    = msg.get("date", "")
                    body    = get_body(msg)

                    if not body.strip() and not subject.strip():
                        processed.add(uid_key)
                        continue

                    # Skip obvious automated senders
                    if any(s in sender.lower() for s in SKIP_SENDERS):
                        processed.add(uid_key)
                        continue

                    sender_name = re.sub(r'<[^>]+>', '', sender).strip().strip('"')
                    existing.append({
                        "date":      date,
                        "from":      sender,
                        "from_name": sender_name,
                        "to":        to,
                        "subject":   subject,
                        "body":      body,
                        "folder":    folder_name,
                        "category":  category,
                    })
                    processed.add(uid_key)
                    new_count   += 1
                    folder_new  += 1
                except Exception:
                    continue

            print(f"  ✅ {folder_name}: {folder_new} new emails")

        except Exception as e:
            print(f"  ❌ {folder_name}: {e}")
            continue

    mail.logout()

    existing.sort(key=lambda e: e.get("date", ""), reverse=True)
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    save_processed(processed)

    print(f"✅ Gmail total: {len(existing)} emails ({new_count} new)")

    if new_count > 0:
        result = subprocess.run(
            ["scp", "-q", "-i", SSH_KEY, OUT_FILE, REMOTE],
            capture_output=True
        )
        if result.returncode == 0:
            print("✅ Synced to server")
        else:
            # Fallback: /mnt/hdd staging (used when root FS is read-only after crash)
            result2 = subprocess.run(
                ["scp", "-q", "-i", SSH_KEY, OUT_FILE, REMOTE_HDD],
                capture_output=True
            )
            if result2.returncode == 0:
                print("✅ Synced to server (hdd staging)")
            else:
                print("⚠️  SCP failed — saved locally")

if __name__ == "__main__":
    main()
