#!/usr/bin/env python3
"""
VESPER INSTAGRAM DATA EXPORTER — incremental, ZIP-aware
Scans iCloud Downloads + local Downloads for Instagram data ZIPs or extracted folders.
Runs incrementally — only exports NEW messages/activity since last run.

HOW TO GET YOUR INSTAGRAM DATA:
  Instagram App → Profile → ☰ → Settings → Account Center
  → Your information and permissions → Download your information
  → Download or transfer information → Some of your information
  → Select: Messages, Comments, Liked Posts, Story Activity,
            Followers and Following, Search History, Posts Viewed
  → Format: JSON → Date range: All time
  → Create files → (wait for email) → Download → Save to iCloud Drive/Downloads

  The zip (e.g. tanmaypramanick06_20260528.zip) auto-syncs to Mac via iCloud.
  This script detects it, extracts, and only stores NEW data.
"""

import os, json, glob, re, zipfile, tempfile, shutil, subprocess, sys, hashlib
from datetime import datetime

EXPORT_DIR  = os.path.expanduser("~/vesper_agent/exports/instagram")
OUT_FILE    = os.path.join(EXPORT_DIR, "instagram_data.json")
PROC_FILE   = os.path.join(EXPORT_DIR, "processed.json")   # set of (date,sender,chat) keys
ZIPS_FILE   = os.path.join(EXPORT_DIR, "processed_zips.json")  # zips already extracted
SSH_KEY     = os.path.expanduser("~/.ssh/vesper_key")
REMOTE      = "tanmay@100.123.15.32:/home/tanmay/vesper/data/instagram/"
REMOTE_HDD  = "tanmay@100.123.15.32:/mnt/hdd/vesper/data/instagram/"

os.makedirs(EXPORT_DIR, exist_ok=True)

SEARCH_DIRS = [
    os.path.expanduser("~/Downloads"),
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Downloads"),
    os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/Desktop"),
    os.path.expanduser("~/Documents"),
]

# ── helpers ──────────────────────────────────────────────────────────────────

def safe_str(v):
    if isinstance(v, str):
        try: return v.encode("latin-1").decode("utf-8")
        except: return v
    return str(v) if v else ""

def ts_to_str(ts):
    try: return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except: return str(ts)

def file_hash(path):
    """Fast hash of first 64KB to identify same zip content."""
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            h.update(f.read(65536))
    except: pass
    return h.hexdigest()

# ── Discover ZIP files and extracted folders ─────────────────────────────────

def find_instagram_zips():
    """Return list of instagram ZIP paths."""
    found = []
    for base in SEARCH_DIRS:
        if not os.path.exists(base): continue
        for p in glob.glob(os.path.join(base, "*instagram*.zip")) + \
                 glob.glob(os.path.join(base, "instagram*.zip")):
            found.append(p)
        # Also check for iCloud placeholder (.icloud suffix = not yet downloaded)
        for p in glob.glob(os.path.join(base, ".instagram*.zip.icloud")):
            print(f"  ⏳ {os.path.basename(p)} not yet downloaded from iCloud — turn on Mac longer")
    return list(set(found))

def find_instagram_folder():
    """Return path to an already-extracted instagram data folder."""
    for base in SEARCH_DIRS:
        if not os.path.exists(base): continue
        for candidate in glob.glob(os.path.join(base, "*instagram*")):
            if os.path.isdir(candidate): return candidate
        for candidate in glob.glob(os.path.join(base, "*")):
            if os.path.isdir(candidate):
                if os.path.exists(os.path.join(candidate, "messages")): return candidate
                if os.path.exists(os.path.join(candidate, "personal_information")): return candidate
    return None

# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_messages(root):
    items = []
    inbox = os.path.join(root, "messages", "inbox")
    if not os.path.exists(inbox): return items
    for thread_dir in os.listdir(inbox):
        thread_path = os.path.join(inbox, thread_dir)
        if not os.path.isdir(thread_path): continue
        thread_name = thread_dir.rsplit("_", 1)[0]
        for fname in sorted(glob.glob(os.path.join(thread_path, "message_*.json"))):
            try:
                data = json.load(open(fname))
                participants = [safe_str(p.get("name","")) for p in data.get("participants", [])]
                for msg in data.get("messages", []):
                    text   = safe_str(msg.get("content", ""))
                    sender = safe_str(msg.get("sender_name", ""))
                    ts     = msg.get("timestamp_ms", 0)
                    if not text or len(text) < 2: continue
                    items.append({
                        "type":         "dm",
                        "date":         ts_to_str(ts // 1000),
                        "sender":       sender,
                        "chat":         safe_str(thread_name),
                        "text":         text,
                        "participants": participants,
                    })
            except: pass
    return items

def parse_comments(root):
    items = []
    for fpath in glob.glob(os.path.join(root, "comments", "**", "*.json"), recursive=True):
        try:
            data = json.load(open(fpath))
            entries = data if isinstance(data, list) else data.get("comments_media_comments", [])
            for entry in entries:
                for sv in entry.get("string_map_data", {}).values():
                    text = safe_str(sv.get("value", ""))
                    ts   = sv.get("timestamp", 0)
                    if text and len(text) > 2:
                        items.append({"type": "comment", "date": ts_to_str(ts), "text": text, "sender": "me", "chat": "instagram"})
        except: pass
    return items

def parse_posts(root):
    items = []
    for fpath in glob.glob(os.path.join(root, "content", "posts_1.json")) + \
                 glob.glob(os.path.join(root, "content", "*.json")):
        try:
            data = json.load(open(fpath))
            posts = data if isinstance(data, list) else []
            for post in posts:
                media = post.get("media", [{}])
                if media:
                    ts    = media[0].get("creation_timestamp", 0)
                    title = safe_str(media[0].get("title", ""))
                    if title:
                        items.append({"type": "post", "date": ts_to_str(ts), "text": title, "sender": "me", "chat": "instagram_posts"})
        except: pass
    return items

def parse_liked_posts(root):
    items = []
    for fpath in glob.glob(os.path.join(root, "likes", "liked_posts.json")):
        try:
            data = json.load(open(fpath))
            likes = data.get("likes_media_likes", data) if isinstance(data, dict) else data
            for entry in likes:
                title = entry.get("title", "")
                ts    = 0
                for sv in entry.get("string_list_data", []):
                    ts = sv.get("timestamp", 0)
                if title:
                    items.append({"type": "liked", "date": ts_to_str(ts), "text": f"Liked: {safe_str(title)}", "sender": "me", "chat": "instagram_likes"})
        except: pass
    return items

def parse_all(root):
    all_items = []
    msgs      = parse_messages(root); print(f"  DMs: {len(msgs)}")
    cmts      = parse_comments(root); print(f"  Comments: {len(cmts)}")
    posts     = parse_posts(root);    print(f"  Posts: {len(posts)}")
    likes     = parse_liked_posts(root); print(f"  Liked posts: {len(likes)}")
    all_items.extend(msgs + cmts + posts + likes)
    return all_items

# ── State tracking ────────────────────────────────────────────────────────────

def load_processed_keys():
    try: return set(json.load(open(PROC_FILE)))
    except: return set()

def save_processed_keys(s):
    with open(PROC_FILE, "w") as f:
        json.dump(list(s), f)

def load_processed_zips():
    try: return set(json.load(open(ZIPS_FILE)))
    except: return set()

def save_processed_zips(s):
    with open(ZIPS_FILE, "w") as f:
        json.dump(list(s), f)

def load_existing():
    try: return json.load(open(OUT_FILE))
    except: return []

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    processed_keys = load_processed_keys()
    processed_zips = load_processed_zips()
    all_data       = load_existing()
    existing_keys  = set(processed_keys)
    new_count      = 0

    roots_to_process = []

    # 1. Find new ZIPs in iCloud/Downloads
    zips = find_instagram_zips()
    if zips:
        for zpath in zips:
            zhash = file_hash(zpath)
            key   = f"{os.path.basename(zpath)}:{zhash}"
            if key in processed_zips:
                print(f"  ⏭️  {os.path.basename(zpath)} (already extracted)")
                continue
            print(f"  📦 Extracting {os.path.basename(zpath)}...")
            tmpdir = tempfile.mkdtemp(prefix="vesper_instagram_")
            try:
                with zipfile.ZipFile(zpath) as zf:
                    zf.extractall(tmpdir)
                # Find the data root inside the extracted dir
                root = tmpdir
                # Instagram zips often have a subfolder
                for sub in os.listdir(tmpdir):
                    sub_path = os.path.join(tmpdir, sub)
                    if os.path.isdir(sub_path) and (
                        os.path.exists(os.path.join(sub_path, "messages")) or
                        os.path.exists(os.path.join(sub_path, "personal_information"))
                    ):
                        root = sub_path
                        break
                roots_to_process.append((root, tmpdir, key))
            except Exception as e:
                print(f"  ❌ Failed to extract {zpath}: {e}")
                shutil.rmtree(tmpdir, ignore_errors=True)

    # 2. Find extracted folder if no zips
    if not zips and not roots_to_process:
        folder = find_instagram_folder()
        if folder:
            print(f"  📁 Found extracted folder: {folder}")
            roots_to_process.append((folder, None, None))

    if not roots_to_process:
        print("❌ No Instagram data found.")
        print()
        print("How to download your Instagram data:")
        print("  1. Instagram app → Profile → ☰ → Settings → Account Center")
        print("  2. Your information and permissions → Download your information")
        print("  3. Download or transfer information → Some of your information")
        print("  4. Select: Messages, Comments, Liked Posts, Posts Viewed, Search History")
        print("  5. Format: JSON → All time → Create files")
        print("  6. Wait for email notification (usually 1-24 hours)")
        print("  7. Download the ZIP from the email link")
        print("  8. Save to: iCloud Drive → Downloads")
        print("     → Mac auto-syncs it → this script processes within 6 hours")
        return

    # 3. Parse each root
    for root, tmpdir, zip_key in roots_to_process:
        items = parse_all(root)
        new_items = []
        for item in items:
            key = f"{item['date']}|{item['sender']}|{item['chat']}|{item['text'][:40]}"
            if key not in existing_keys:
                new_items.append(item)
                existing_keys.add(key)
        all_data.extend(new_items)
        new_count += len(new_items)
        print(f"  ✅ {len(new_items)} new items (out of {len(items)} total)")

        # Mark zip as processed (only after successful parse)
        if zip_key:
            processed_zips.add(zip_key)

        # Clean up temp dir
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # 4. Save
    with open(OUT_FILE, "w") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    save_processed_keys(existing_keys)
    save_processed_zips(processed_zips)

    print(f"\nTotal Instagram items: {len(all_data)} (+{new_count} new)")

    if new_count == 0:
        print("No new data — nothing to sync.")
        return

    # 5. SCP to server
    result = subprocess.run(
        ["scp", "-q", "-i", SSH_KEY, OUT_FILE, REMOTE],
        capture_output=True
    )
    if result.returncode == 0:
        print("✅ Synced to server")
    else:
        result2 = subprocess.run(
            ["scp", "-q", "-i", SSH_KEY, OUT_FILE, REMOTE_HDD],
            capture_output=True
        )
        if result2.returncode == 0:
            print("✅ Synced to server (hdd staging)")
        else:
            print(f"⚠️  SCP unavailable — saved locally at {OUT_FILE}")

if __name__ == "__main__":
    main()
