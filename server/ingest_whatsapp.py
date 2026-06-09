# ─────────────────────────────────────────
# VESPER WHATSAPP INGEST v3
# - Conversation-chunked (12 msgs/chunk, 10x fewer embeddings)
# - Incremental: skips existing chunks by content hash
# - Uses HTTP API (no direct ChromaDB access = no crashes)
# ─────────────────────────────────────────

import json, os, sys, time, hashlib
sys.path.append('/home/tanmay/vesper')
from pipelines.memory_client import store_memory, delete_category, get_memory_count
from config import DATA_PATH

MESSAGES_FILE  = f'{DATA_PATH}/whatsapp/whatsapp_messages.json'
PROCESSED_FILE = f'{DATA_PATH}/whatsapp/processed_chunks.json'

WINDOW_SIZE = 12   # messages per conversation chunk
OVERLAP     = 2    # chunk overlap for context continuity
MIN_CHARS   = 60   # minimum chars to embed a chunk
SAVE_EVERY  = 20   # save processed.json every N new chunks

def load_processed():
    try:
        if os.path.exists(PROCESSED_FILE):
            d = json.load(open(PROCESSED_FILE))
            return set(d) if d else set()
    except Exception:
        pass
    return set()

def save_processed(ids):
    tmp = PROCESSED_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(list(ids), f)
    os.replace(tmp, PROCESSED_FILE)

def chunk_id(chat, messages):
    """Stable ID for a chunk — based on first 3 msgs content."""
    key = chat + ''.join(
        m.get('date','') + m.get('sender','') + m.get('text','')[:20]
        for m in messages[:3]
    )
    return hashlib.md5(key.encode()).hexdigest()[:16]

def sliding_windows(messages, window=WINDOW_SIZE, overlap=OVERLAP):
    step = window - overlap
    for i in range(0, len(messages), step):
        chunk = messages[i:i + window]
        if chunk:
            yield chunk

def ingest_whatsapp():
    if not os.path.exists(MESSAGES_FILE):
        print('No WhatsApp messages file found')
        return 0

    processed = load_processed()

    try:
        with open(MESSAGES_FILE) as f:
            all_msgs = json.load(f)
    except Exception as e:
        print(f'Error reading messages: {e}')
        return 0

    # Group by chat
    by_chat = {}
    for msg in all_msgs:
        chat = msg.get('chat', 'unknown')
        by_chat.setdefault(chat, []).append(msg)

    total_chunks = sum(
        sum(1 for _ in sliding_windows(msgs)) for msgs in by_chat.values()
    )
    already_done = len(processed)
    print(f'WhatsApp: {len(all_msgs)} msgs → {total_chunks} chunks')
    print(f'Already done: {already_done}, to embed: {total_chunks - already_done}')

    if total_chunks == already_done:
        print('WhatsApp: nothing new')
        return 0

    count = skipped = failed = 0

    for chat, messages in by_chat.items():
        chat_stored = 0
        for chunk in sliding_windows(messages):
            cid = chunk_id(chat, chunk)
            if cid in processed:
                skipped += 1
                continue

            # Build conversation text
            lines = []
            for m in chunk:
                text = m.get('text','').strip()
                if text and len(text) >= 2:
                    lines.append(f"{m.get('sender','?')} [{m.get('date','')}]: {text}")
            if not lines:
                processed.add(cid)
                continue

            conv = '\n'.join(lines)
            if len(conv) < MIN_CHARS:
                processed.add(cid)
                continue

            dates = [m.get('date','') for m in chunk if m.get('date')]
            mem = (f'[WhatsApp | {chat}] {dates[0] if dates else ""}\n{conv}')

            ok = store_memory(mem, category='whatsapp', source=f'whatsapp:{chat}')
            if ok:
                count += 1
                chat_stored += 1
            else:
                failed += 1
            processed.add(cid)

            if (count + skipped + failed) % SAVE_EVERY == 0:
                save_processed(processed)
                done = count + skipped + failed
                pct = done * 100 // total_chunks
                print(f'  [{pct}%] stored:{count} skipped:{skipped} failed:{failed}', flush=True)

        if chat_stored:
            print(f'  {chat}: +{chat_stored} new chunks', flush=True)

    save_processed(processed)
    print(f'WhatsApp done: {count} new chunks stored, {skipped} already existed, {failed} failed')
    return count

if __name__ == '__main__':
    t0 = time.time()
    n = ingest_whatsapp()
    elapsed = int(time.time() - t0)
    m = get_memory_count()
    print(f'Time: {elapsed//60}m {elapsed%60}s | Total DB memories: {m}')
