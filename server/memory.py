# ─────────────────────────────────────────
# VESPER MEMORY ENGINE v2
# - File-locked ChromaDB writes (safe for multiple processes)
# - CPU yield between embeddings
# - Single collection, cosine similarity
# ─────────────────────────────────────────

import chromadb
import ollama
import uuid
import time
import fcntl
import sys
from datetime import datetime
from pathlib import Path

sys.path.append('/home/tanmay/vesper')
from config import MEMORY_PATH, EMBED_MODEL

# Connect to Chroma on SSD
client = chromadb.PersistentClient(path=MEMORY_PATH)
collection = client.get_or_create_collection(
    name='vesper_life',
    metadata={'hnsw:space': 'cosine'}
)

# Lock file — ALL processes (file_receiver, ingest_*) share this lock
# Embedding happens WITHOUT the lock; only collection.add() is locked
LOCK_PATH = Path(MEMORY_PATH) / '.write.lock'

def _locked_add(documents, embeddings, metadatas, ids):
    """Atomic ChromaDB write — serialized across all processes via flock."""
    LOCK_PATH.touch(exist_ok=True)
    with open(LOCK_PATH, 'r') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            collection.add(
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
                ids=ids
            )
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

def _locked_delete(where):
    """Atomic ChromaDB delete — serialized via flock."""
    LOCK_PATH.touch(exist_ok=True)
    with open(LOCK_PATH, 'r') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            collection.delete(where=where)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

def store_memory(text, category='general', source='unknown'):
    if not text or len(text.strip()) < 10:
        return False
    try:
        # Embedding is safe without lock (pure HTTP call to ollama)
        response = ollama.embeddings(
            model=EMBED_MODEL,
            prompt=text[:1500]
        )
        embedding = response['embedding']

        # Only the ChromaDB write needs a lock
        _locked_add(
            documents=[text[:2000]],
            embeddings=[embedding],
            metadatas=[{
                'category': category,
                'source': source,
                'timestamp': datetime.now().isoformat(),
                'date': datetime.now().strftime('%Y-%m-%d')
            }],
            ids=[str(uuid.uuid4())]
        )
        # Yield CPU between embeddings
        time.sleep(0.05)
        return True
    except Exception as e:
        print(f'Memory store error: {e}')
        return False

def delete_by_source(source):
    """Delete all memories for a given source path."""
    try:
        _locked_delete(where={'source': source})
        return True
    except Exception as e:
        print(f'Memory delete error: {e}')
        return False

def delete_by_category(category):
    """Delete all memories for a given category."""
    try:
        _locked_delete(where={'category': category})
        return True
    except Exception as e:
        print(f'Memory delete error: {e}')
        return False

def search_memory(query, n=5, category=None):
    try:
        response = ollama.embeddings(
            model=EMBED_MODEL,
            prompt=query
        )
        embedding = response['embedding']
        where = {'category': category} if category else None
        results = collection.query(
            query_embeddings=[embedding],
            n_results=n,
            where=where
        )
        if results['documents']:
            return results['documents'][0]
        return []
    except Exception as e:
        print(f'Memory search error: {e}')
        return []

def get_stats():
    count = collection.count()
    print(f'Total memories stored: {count}')
    return count

if __name__ == '__main__':
    print('Testing memory engine v2...')
    success = store_memory(
        'Vesper memory engine v2 — file-locked writes.',
        category='system',
        source='setup'
    )
    print(f"Store: {'✅' if success else '❌'}")
    results = search_memory('Vesper memory engine')
    print(f"Search: {'✅' if results else '❌'}")
    get_stats()
