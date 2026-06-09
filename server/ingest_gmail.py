# ─────────────────────────────────────────
# VESPER GMAIL INGESTION
# Reads gmail_emails.json → ChromaDB
# ─────────────────────────────────────────

import json, os, sys, re
sys.path.append('/home/tanmay/vesper')
from pipelines.memory_client import store_memory, get_memory_count
from config import DATA_PATH

EMAILS_FILE    = f'{DATA_PATH}/emails/gmail_emails.json'
PROCESSED_FILE = f'{DATA_PATH}/emails/processed.json'

SKIP_SENDERS = {
    'noreply', 'no-reply', 'mailer-daemon', 'notifications',
    'newsletter', 'unsubscribe', 'donotreply', 'do-not-reply',
    'postmaster', 'bounces', 'automated',
}

def load_processed():
    try:
        if os.path.exists(PROCESSED_FILE):
            with open(PROCESSED_FILE) as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def save_processed(ids):
    with open(PROCESSED_FILE, 'w') as f:
        json.dump(list(ids)[-30000:], f)

def make_id(email):
    return f"{email.get('date','')}|{email.get('from','')}|{email.get('subject','')}".strip()

def is_automated(sender):
    sender_lower = sender.lower()
    return any(skip in sender_lower for skip in SKIP_SENDERS)

def clean_body(body):
    # Remove URLs, excessive whitespace
    body = re.sub(r'https?://\S+', '[link]', body)
    body = re.sub(r'\s{3,}', ' ', body)
    return body.strip()[:1000]

def ingest_gmail():
    if not os.path.exists(EMAILS_FILE):
        print('No Gmail file found — run export_gmail.sh on Mac first')
        return 0

    processed = load_processed()
    try:
        with open(EMAILS_FILE) as f:
            emails = json.load(f)
    except Exception as e:
        print(f'Error reading emails: {e}')
        return 0

    count = 0
    skipped_auto = 0

    for email in emails:
        uid = make_id(email)
        if uid in processed:
            processed.add(uid)
            continue

        sender  = email.get('from', '')
        subject = email.get('subject', '').strip()
        body    = clean_body(email.get('body', ''))
        date    = email.get('date', '')
        folder  = email.get('folder', 'INBOX')
        name    = email.get('from_name', sender)
        to      = email.get('to', '')

        # Skip obviously automated emails
        if is_automated(sender):
            skipped_auto += 1
            processed.add(uid)
            continue

        if not body and not subject:
            processed.add(uid)
            continue

        # Build rich memory text
        direction = 'Sent' if 'Sent' in folder else 'Received'
        memory_text = (
            f'[Gmail {direction} | {date}] '
            f'From: {name} | To: {to[:80]} | '
            f'Subject: {subject} | {body}'
        )

        category = email.get('category', 'email_received')
        success = store_memory(
            memory_text,
            category=category,
            source=f'gmail:{sender}'
        )
        if success:
            count += 1
        processed.add(uid)

    save_processed(processed)
    print(f'Gmail: {count} new emails ingested (skipped {skipped_auto} automated)')
    return count

if __name__ == '__main__':
    ingest_gmail()
