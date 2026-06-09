#!/usr/bin/env python3
"""Every 30min: scan for priority WhatsApp/email messages and send WA alert."""

import json, os, time, urllib.request, ssl, hashlib
from datetime import datetime

VESPER       = "https://127.0.0.1:5000"
BOT_API      = "http://127.0.0.1:5001"
STATE_FILE   = "/tmp/proactive_alerts_state.json"
GUARD_SECS   = 1500   # 25-min guard (cron runs every 30min)
CTX          = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

def _get(path):
    try:
        req = urllib.request.Request(f"{VESPER}{path}", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=CTX, timeout=10) as r:
            return json.loads(r.read())
    except:
        return {}

def _post(url, data):
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode()
    except Exception as e:
        return str(e)

def _query(prompt, n=5, where=None):
    body = {"query": prompt, "n": n}
    if where:
        body["where"] = where
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"{VESPER}/query", data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, context=CTX, timeout=15) as r:
            return json.loads(r.read()).get("results", [])
    except:
        return []

def load_state():
    try:
        return json.load(open(STATE_FILE))
    except:
        return {"last_run": 0, "sent_hashes": []}

def save_state(s):
    try:
        json.dump(s, open(STATE_FILE, "w"))
    except:
        pass

def get_owner_jid():
    try:
        cfg = json.load(open("/home/tanmay/vesper/openclaw/config.json"))
        return cfg.get("owner_jid", "")
    except:
        return ""

def send_wa(owner_jid, msg):
    _post(BOT_API, {"jid": owner_jid, "message": msg, "type": "text"})

def main():
    owner_jid = get_owner_jid()
    if not owner_jid:
        print("No owner_jid in config — skipping")
        return

    state = load_state()
    now = time.time()

    if now - state["last_run"] < GUARD_SECS:
        print(f"Too soon (last run {int(now - state['last_run'])}s ago) — skipping")
        return

    state["last_run"] = now
    cutoff = now - 1800  # last 30 min

    alerts = []

    # Priority keywords in recent WhatsApp messages
    wa_results = _query("urgent important please help asap reply", n=6,
                        where={"category": {"$eq": "whatsapp"}})
    for r in wa_results:
        meta = r.get("metadata", {})
        ts = meta.get("timestamp", 0)
        if ts < cutoff:
            continue
        txt = r.get("document", "").strip()
        if not txt or len(txt) < 10:
            continue
        # Only flag if it looks like a question or request
        if any(kw in txt.lower() for kw in ["urgent", "asap", "please", "help", "call", "come", "need", "?"]):
            alerts.append(("WhatsApp", txt[:120]))

    # Priority emails
    em_results = _query("urgent action required important deadline", n=4,
                        where={"category": {"$eq": "email_received"}})
    for r in em_results:
        meta = r.get("metadata", {})
        ts = meta.get("timestamp", 0)
        if ts < cutoff:
            continue
        txt = r.get("document", "").strip()
        if not txt or len(txt) < 10:
            continue
        if any(kw in txt.lower() for kw in ["urgent", "action required", "deadline", "important", "asap"]):
            alerts.append(("Email", txt[:120]))

    if not alerts:
        print("No priority alerts")
        save_state(state)
        return

    # Deduplicate against already-sent hashes
    sent = set(state.get("sent_hashes", []))
    new_alerts = []
    new_hashes = []
    for src, txt in alerts:
        h = hashlib.md5(txt.encode()).hexdigest()[:12]
        if h not in sent:
            new_alerts.append((src, txt))
            new_hashes.append(h)

    if not new_alerts:
        print("All alerts already sent")
        save_state(state)
        return

    ts_str = datetime.now().strftime("%H:%M")
    lines = [f"🔔 *Vesper Alert* ({ts_str})"]
    for src, txt in new_alerts[:3]:
        icon = "📱" if src == "WhatsApp" else "📧"
        lines.append(f"\n{icon} *{src}*: {txt}")

    send_wa(owner_jid, "\n".join(lines))
    print(f"Sent {len(new_alerts)} new alert(s)")

    # Keep last 200 hashes
    state["sent_hashes"] = list(sent)[-180:] + new_hashes
    save_state(state)


if __name__ == "__main__":
    main()
