import json, os, sys, time
sys.path.append('/home/tanmay/vesper')
from pipelines.memory_client import store_memory, get_memory_count
from config import DATA_PATH
from collections import defaultdict

DATA_FILE = f'{DATA_PATH}/instagram/instagram_data.json'
PROC_FILE = f'{DATA_PATH}/instagram/processed_smart.json'
MSGS_PER_CHUNK = 10
MAX_CHUNKS = 3

def load_proc():
    try:
        if os.path.exists(PROC_FILE): return set(json.load(open(PROC_FILE)))
    except: pass
    return set()

def save_proc(s):
    with open(PROC_FILE,'w') as f: json.dump(list(s)[-50000:], f)

def ingest():
    t0 = time.time()
    if not os.path.exists(DATA_FILE):
        print('No data'); return
    data = json.load(open(DATA_FILE))
    proc = load_proc()
    total = 0

    comments = [d for d in data if d.get('type') == 'comment']
    print(f'Comments: {len(comments)}')
    for c in comments:
        uid = 'comment|'+c.get('date','')+c.get('text','')[:20]
        if uid in proc: continue
        text = c.get('text','').strip()
        date = c.get('date','')[:10]
        if text and store_memory(f'[Instagram Comment | {date}] Tanmay commented: {text}', category='instagram', source='instagram:comment'):
            total += 1
        proc.add(uid)

    dms = [d for d in data if d.get('type') == 'dm']
    print(f'DMs: {len(dms)}')
    convs = defaultdict(list)
    for dm in dms: convs[dm.get('chat','?')].append(dm)
    print(f'Conversations: {len(convs)}')

    for n, (chat, msgs) in enumerate(convs.items()):
        msgs.sort(key=lambda m: m.get('date',''))
        dates = [m.get('date','') for m in msgs]
        others = set(m.get('sender','') for m in msgs) - {'Tanmay Pramanick'}
        other = ', '.join(sorted(others)[:2]) or chat
        last_d = dates[-1][:10] if dates else ''
        first_d = dates[0][:10] if dates else ''

        uid_s = f'ig_sum|{chat}'
        if uid_s not in proc:
            mem = f'[Instagram DM | {chat}] Tanmay DM’d {other} ({len(msgs)} messages, {first_d} to {last_d})'
            if store_memory(mem, category='instagram', source='instagram:dm_summary'): total += 1
            proc.add(uid_s)

        recent = msgs[-(MAX_CHUNKS * MSGS_PER_CHUNK):]
        for i in range(0, len(recent), MSGS_PER_CHUNK):
            chunk = recent[i:i+MSGS_PER_CHUNK]
            uid_c = f'ig_chunk|{chat}|{chunk[0].get("date","")}'
            if uid_c in proc: continue
            lines = []
            for m in chunk:
                snd = 'Tanmay' if m.get('sender','') == 'Tanmay Pramanick' else m.get('sender','?')
                txt = m.get('text','').strip()
                if txt: lines.append(f'  [{m.get("date","")[:16]}] {snd}: {txt}')
            if not lines: continue
            mem = f'[Instagram DM | {other} | {chunk[-1].get("date","")[:10]}]\n' + '\n'.join(lines)
            if store_memory(mem, category='instagram', source='instagram:dm'): total += 1
            proc.add(uid_c)

        if n % 50 == 49:
            print(f'  {n+1}/{len(convs)} convs | {total} stored | {int(time.time()-t0)}s')
            save_proc(proc)

    save_proc(proc)
    print(f'Done: {total} stored in {int(time.time()-t0)}s')

ingest()
