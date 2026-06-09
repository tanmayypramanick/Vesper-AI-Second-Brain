# ─────────────────────────────────────────
# VESPER MEMORY CLIENT v2
# HTTP client for file_receiver.py
# file_receiver is the ONLY ChromaDB writer/reader
# ─────────────────────────────────────────

import requests, time

RECEIVER_URL = 'http://127.0.0.1:5000'
TIMEOUT_STORE = 120   # embed + write can take time
TIMEOUT_QUERY = 30
TIMEOUT_HEALTH = 5

def store_memory(text, category='general', source='unknown', retries=3):
    """Store a memory via file_receiver HTTP endpoint."""
    if not text or len(text.strip()) < 10:
        return False
    payload = {'text': text, 'category': category, 'source': source}
    for attempt in range(retries):
        try:
            r = requests.post(
                f'{RECEIVER_URL}/store_memory',
                json=payload,
                timeout=TIMEOUT_STORE
            )
            if r.status_code in (200, 202):
                return True
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return False

def search_memory(query, n=5, category=None):
    """Search memories via file_receiver HTTP endpoint."""
    payload = {'query': query, 'n': n}
    if category:
        payload['category'] = category
    try:
        r = requests.post(
            f'{RECEIVER_URL}/query',
            json=payload,
            timeout=TIMEOUT_QUERY
        )
        if r.status_code == 200:
            hits = r.json().get('results', [])
            return [h['text'] for h in hits]
    except Exception as e:
        print(f'Memory search error: {e}')
    return []

def search_memory_full(query, n=5, category=None):
    """Search memories, return full hit objects {text, category, source, score}."""
    payload = {'query': query, 'n': n}
    if category:
        payload['category'] = category
    try:
        r = requests.post(
            f'{RECEIVER_URL}/query',
            json=payload,
            timeout=TIMEOUT_QUERY
        )
        if r.status_code == 200:
            return r.json().get('results', [])
    except Exception as e:
        print(f'Memory search error: {e}')
    return []

def delete_category(category):
    """Delete all memories for a category."""
    try:
        r = requests.post(
            f'{RECEIVER_URL}/delete_category',
            json={'category': category},
            timeout=30
        )
        return r.status_code == 200
    except Exception:
        return False

def get_memory_count():
    """Get total memory count from file_receiver."""
    try:
        r = requests.get(f'{RECEIVER_URL}/health', timeout=TIMEOUT_HEALTH)
        return r.json().get('memories', 0)
    except Exception:
        return -1
