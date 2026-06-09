#!/usr/bin/env python3
"""
VESPER REAL-TIME GMAIL WATCHER
Uses IMAP IDLE — server pushes notification the instant a new email lands.
No 30-minute wait. Runs as a persistent background process on Mac.

Start: python3 ~/vesper_agent/gmail_realtime.py &
Or via LaunchAgent: ~/vesper_agent/launch_gmail_realtime.sh
"""

import imaplib, email as email_lib, re, time, json, os, ssl, sys, signal
import urllib.request, urllib.error
from email.header import decode_header

# ── Credentials (read from export_gmail.sh) ──────────────────────────────────
def _get_creds():
    sh = os.path.expanduser("~/vesper_agent/export_gmail.sh")
    addr = pw = ""
    for line in open(sh):
        m = re.match(r'^GMAIL_ADDRESS="(.+)"', line.strip())
        if m: addr = m.group(1)
        m = re.match(r'^GMAIL_APP_PASSWORD="(.+)"', line.strip())
        if m: pw = m.group(1)
    return addr, pw

GMAIL_ADDRESS, GMAIL_APP_PASSWORD = _get_creds()
IMAP_HOST   = "imap.gmail.com"
VESPER_URL  = "https://10.0.0.120:5000/store_memory"
PROCESSED_F = os.path.expanduser("~/vesper_agent/exports/gmail/realtime_processed.json")
LOG_F       = os.path.expanduser("~/Library/Logs/vesper_gmail_realtime.log")

SKIP_SENDERS = {"noreply","no-reply","mailer-daemon","notifications","newsletter",
                "unsubscribe","donotreply","do-not-reply","postmaster","bounces",
                "automated","support@","bounce","info@","marketing","promo"}

# ── Logging ───────────────────────────────────────────────────────────────────
import logging
os.makedirs(os.path.dirname(LOG_F), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_F),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("gmail_rt")

# ── State ─────────────────────────────────────────────────────────────────────
def load_processed():
    try:
        if os.path.exists(PROCESSED_F):
            return set(json.load(open(PROCESSED_F)))
    except: pass
    return set()

def save_processed(ids):
    os.makedirs(os.path.dirname(PROCESSED_F), exist_ok=True)
    with open(PROCESSED_F, 'w') as f:
        json.dump(list(ids)[-10000:], f)

_processed = load_processed()

# ── Email processing ──────────────────────────────────────────────────────────
def decode_str(s):
    if not s: return ""
    parts = decode_header(s)
    result = []
    for b, charset in parts:
        if isinstance(b, bytes):
            try:
                result.append(b.decode(charset or "utf-8", errors="replace"))
            except: result.append(b.decode("utf-8", errors="replace"))
        else:
            result.append(str(b))
    return " ".join(result).strip()

def get_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="ignore")[:1200]
                    break
                except: pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")[:1200]
        except: pass
    body = re.sub(r'https?://\S+', '[link]', body)
    body = re.sub(r'\s{3,}', ' ', body).strip()
    return body[:800]

def is_automated(sender):
    s = sender.lower()
    return any(skip in s for skip in SKIP_SENDERS)

def send_to_vesper(text, category, source):
    """POST to Vesper /store_memory — ignore TLS cert (self-signed)."""
    payload = json.dumps({"text": text, "category": category, "source": source}).encode()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        VESPER_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        log.warning(f"store_memory failed: {e}")
        return False

def process_email(raw_data, folder_category):
    msg = email_lib.message_from_bytes(raw_data)
    subject = decode_str(msg.get("Subject", ""))
    sender  = decode_str(msg.get("From", ""))
    to_addr = decode_str(msg.get("To", ""))
    date    = msg.get("Date", "")

    uid_key = f"{date}|{sender}|{subject}".strip()
    if uid_key in _processed:
        return False  # already ingested
    if is_automated(sender):
        return False

    body = get_body(msg)
    if not body.strip():
        return False

    # Format for ChromaDB — same as ingest_gmail.py
    mem = (f"[Email Received] {subject} | "
           f"From: {sender} | To: {to_addr} | "
           f"Date: {date} | {body}")

    if send_to_vesper(mem, folder_category, f"email:{subject[:40]}"):
        _processed.add(uid_key)
        save_processed(_processed)
        log.info(f"✅ Stored: [{folder_category}] {subject[:60]}")
        return True
    return False

# ── IMAP IDLE watcher ─────────────────────────────────────────────────────────
class IdleWatcher:
    def __init__(self, folder, category):
        self.folder = folder
        self.category = category
        self.conn = None
        self.last_uid = 0

    def connect(self):
        log.info(f"Connecting to {IMAP_HOST}...")
        self.conn = imaplib.IMAP4_SSL(IMAP_HOST)
        self.conn.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        self.conn.select(self.folder, readonly=True)
        # Get the highest existing UID so we only process NEW mail
        _, data = self.conn.uid("search", None, "ALL")
        uids = data[0].split() if data[0] else []
        self.last_uid = int(uids[-1]) if uids else 0
        log.info(f"[{self.folder}] Connected. Highest UID: {self.last_uid}. Entering IDLE...")

    def fetch_new(self):
        """Fetch any emails with UID > last_uid."""
        _, data = self.conn.uid("search", None, f"UID {self.last_uid+1}:*")
        uids = data[0].split() if data[0] else []
        new_uids = [u for u in uids if int(u) > self.last_uid]
        count = 0
        for uid in new_uids:
            try:
                _, raw = self.conn.uid("fetch", uid, "(RFC822)")
                if raw and raw[0] and isinstance(raw[0], tuple):
                    if process_email(raw[0][1], self.category):
                        count += 1
                self.last_uid = max(self.last_uid, int(uid))
            except Exception as e:
                log.warning(f"Fetch error UID {uid}: {e}")
        return count

    def idle_wait(self, timeout=240):
        """Enter IDLE mode, wait for EXISTS/RECENT response, or timeout."""
        # Send IDLE command
        tag = self.conn._new_tag().decode()
        self.conn.send(f"{tag} IDLE\r\n".encode())
        # Read the "+" continuation response
        resp = self.conn.readline()
        if b'+' not in resp:
            return False  # IDLE not accepted
        # Now wait for EXISTS or RECENT (or timeout)
        self.conn.sock.settimeout(timeout)
        try:
            while True:
                line = self.conn.readline()
                if not line:
                    return False
                decoded = line.decode("utf-8", errors="ignore")
                if "EXISTS" in decoded or "RECENT" in decoded or "FETCH" in decoded:
                    # New mail! Stop IDLE.
                    self.conn.send(b"DONE\r\n")
                    # Drain remaining IDLE responses
                    self.conn.readline()
                    return True
                if "BYE" in decoded or "LOGOUT" in decoded:
                    return False
        except Exception:
            # Timeout or disconnect
            try: self.conn.send(b"DONE\r\n")
            except: pass
            return True  # reconnect and poll

    def run(self):
        RECONNECT_DELAY = 30
        while True:
            try:
                self.connect()
                while True:
                    self.fetch_new()  # check for any mail we missed
                    got_new = self.idle_wait(timeout=240)  # 4-min IDLE keepalive
                    if got_new:
                        n = self.fetch_new()
                        if n:
                            log.info(f"[{self.folder}] {n} new email(s) ingested")
            except Exception as e:
                log.error(f"[{self.folder}] Error: {e} — reconnecting in {RECONNECT_DELAY}s")
                try: self.conn.logout()
                except: pass
                time.sleep(RECONNECT_DELAY)

# ── Main — watch INBOX ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import threading
    log.info("Vesper Real-Time Gmail Watcher starting...")
    log.info(f"Account: {GMAIL_ADDRESS}")
    log.info(f"Vesper endpoint: {VESPER_URL}")

    # Watch INBOX for received emails
    inbox_watcher = IdleWatcher("INBOX", "email_received")

    # Optionally watch Sent Mail too
    # sent_watcher = IdleWatcher("[Gmail]/Sent Mail", "email_sent")
    # threading.Thread(target=sent_watcher.run, daemon=True).start()

    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda s, f: (log.info("Stopping..."), sys.exit(0)))

    inbox_watcher.run()  # blocks forever
