# VESPER INSTAGRAM INGESTION
import json, os, sys
sys.path.append('/home/tanmay/vesper')
from pipelines.memory_client import store_memory, get_memory_count
from config import DATA_PATH

DATA_FILE  = f'{DATA_PATH}/instagram/instagram_data.json'
PROC_FILE  = f'{DATA_PATH}/instagram/processed.json'

def make_id(item):
    return f"{item.get('type','')}|{item.get('date','')}|{item.get('text','')[:40]}"

def load_processed():
    try:
        if os.path.exists(PROC_FILE):
            return set(json.load(open(PROC_FILE)))
    except: pass
    return set()

def save_processed(s):
    with open(PROC_FILE,'w') as f: json.dump(list(s)[-30000:], f)

def ingest():
    if not os.path.exists(DATA_FILE):
        print('No Instagram data file found'); return 0
    processed = load_processed()
    try:
        data = json.load(open(DATA_FILE))
    except Exception as e:
        print(f'Error: {e}'); return 0

    count = 0
    for item in data:
        uid = make_id(item)
        if uid in processed: continue
        kind   = item.get('type','')
        date   = item.get('date','')
        text   = item.get('text','').strip()
        sender = item.get('sender','')
        chat   = item.get('chat','')
        if not text or len(text) < 2:
            processed.add(uid); continue
        if kind == 'dm':
            memory = f'[Instagram DM | {chat} | {date}] {sender}: {text}'
        elif kind == 'comment':
            memory = f'[Instagram Comment | {date}] Tanmay commented: {text}'
        elif kind == 'post':
            memory = f'[Instagram Post | {date}] Tanmay posted: {text}'
        elif kind == 'liked':
            memory = f'[Instagram Activity | {date}] {text}'
        else:
            memory = f'[Instagram | {date}] {text}'
        ok = store_memory(memory, category='instagram', source=f'instagram:{kind}')
        if ok: count += 1
        processed.add(uid)
    save_processed(processed)
    print(f'Instagram: {count} new items ingested')
    return count

if __name__ == '__main__': ingest()
