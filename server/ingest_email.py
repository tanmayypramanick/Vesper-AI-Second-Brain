# ─────────────────────────────────────────
# VESPER EMAIL INGESTION
# Pulls Gmail via IMAP → Chroma memory
# ─────────────────────────────────────────

import imapclient
import email as email_lib
import sys
sys.path.append('/home/tanmay/vesper')
from pipelines.memory import store_memory

# ← FILL THESE IN
EMAIL_ADDRESS = "your@gmail.com"
APP_PASSWORD = "xxxx xxxx xxxx xxxx"
IMAP_HOST = "imap.gmail.com"

def sync_emails(limit=200):
    print(f"Connecting to Gmail...")
    try:
        client = imapclient.IMAPClient(IMAP_HOST, ssl=True)
        client.login(EMAIL_ADDRESS, APP_PASSWORD)
        print("Connected ✅")
        client.select_folder("INBOX")
        messages = client.search(["NOT", "DELETED"])
        recent = messages[-limit:]
        print(f"Processing {len(recent)} emails...")
        count = 0
        for uid in recent:
            try:
                data = client.fetch([uid], ["RFC822"])
                msg = email_lib.message_from_bytes(
                    data[uid][b"RFC822"]
                )
                subject = msg.get("subject", "")
                sender = msg.get("from", "")
                date = msg.get("date", "")
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(
                                decode=True
                            ).decode("utf-8", errors="ignore")[:800]
                            break
                else:
                    body = msg.get_payload(
                        decode=True
                    ).decode("utf-8", errors="ignore")[:800]
                if not body.strip():
                    continue
                store_memory(
                    f"[Email {date}] From: {sender} | "
                    f"Subject: {subject} | {body}",
                    category="email",
                    source="gmail"
                )
                count += 1
            except:
                continue
        client.logout()
        print(f"✅ Synced {count} emails")
    except Exception as e:
        print(f"❌ Email error: {e}")

if __name__ == "__main__":
    sync_emails()