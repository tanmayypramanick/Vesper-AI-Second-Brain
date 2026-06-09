# ─────────────────────────────────────────
# VESPER CONTACTS INGESTION
# ─────────────────────────────────────────

import sys
import os
sys.path.append('/home/tanmay/vesper')
from pipelines.memory_client import store_memory, get_memory_count
from config import DATA_PATH

CONTACTS_FILE = f"{DATA_PATH}/contacts/contacts.vcf"

def ingest_contacts():
    if not os.path.exists(CONTACTS_FILE):
        print("No contacts file found")
        return 0

    try:
        import vobject
    except ImportError:
        print("Run: pip3 install vobject")
        return 0

    count = 0
    with open(CONTACTS_FILE, errors='ignore') as f:
        content = f.read()

    for card in vobject.readComponents(content):
        try:
            name   = str(card.fn.value) if hasattr(card, 'fn') else 'Unknown'
            phones = [
                str(t.value)
                for t in card.contents.get('tel', [])
            ]
            emails = [
                str(t.value)
                for t in card.contents.get('email', [])
            ]
            org    = ""
            if hasattr(card, 'org'):
                org = str(card.org.value)

            memory = f"[Contact] {name}"
            if org:
                memory += f" | Company: {org}"
            if phones:
                memory += f" | Phone: {', '.join(phones)}"
            if emails:
                memory += f" | Email: {', '.join(emails)}"

            store_memory(
                memory,
                category="contact",
                source="contacts"
            )
            count += 1

        except Exception:
            continue

    print(f"✅ Contacts: {count} contacts ingested")
    return count

if __name__ == "__main__":
    ingest_contacts()