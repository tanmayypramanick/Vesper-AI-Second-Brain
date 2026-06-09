#!/usr/bin/env python3
"""
VESPER INSTAGRAM PARSER
Converts Instagram data export (ZIP) to vesper's normalized format.

How to get Instagram data:
1. Instagram app → Profile → Menu (☰) → Settings → Your activity
2. → Download your information → Request download
3. Select: JSON format, date range: all time
4. Download when ready (email notification)
5. SCP the ZIP to server:
   scp -i ~/.ssh/vesper_key ~/Downloads/instagram-*.zip tanmay@100.123.15.32:/home/tanmay/vesper/data/instagram/

Run: python3 parse_instagram.py /path/to/instagram-export.zip
Output: /home/tanmay/vesper/data/instagram/instagram_data.json
"""

import json, os, sys, zipfile
from datetime import datetime
from pathlib import Path

DATA_PATH  = '/home/tanmay/vesper/data/instagram'
OUTPUT_FILE = f'{DATA_PATH}/instagram_data.json'

def fix_encoding(text):
    """Instagram uses latin-1 encoded emoji in JSON strings."""
    if not text:
        return text
    try:
        return text.encode('latin-1').decode('utf-8')
    except:
        return text

def ts_to_date(ts):
    if not ts:
        return ''
    try:
        return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')
    except:
        return str(ts)

def parse_instagram_zip(zip_path):
    items = []

    with zipfile.ZipFile(zip_path, 'r') as zf:
        names = zf.namelist()

        # ── Direct Messages ──────────────────────────────
        dm_files = [n for n in names if 'messages/inbox' in n and n.endswith('.json')]
        for fname in dm_files:
            try:
                data = json.loads(zf.read(fname))
                chat_name = fix_encoding(data.get('title', ''))
                for msg in data.get('messages', []):
                    sender = fix_encoding(msg.get('sender_name', ''))
                    content = fix_encoding(msg.get('content', ''))
                    ts = msg.get('timestamp_ms', 0) // 1000
                    if content and len(content) > 1:
                        items.append({
                            'type': 'dm',
                            'date': ts_to_date(ts),
                            'text': content,
                            'sender': sender,
                            'chat': chat_name
                        })
            except Exception as e:
                print(f'  DM parse error {fname}: {e}')

        # ── Post Comments ─────────────────────────────────
        comment_files = [n for n in names if 'comments' in n and n.endswith('.json')]
        for fname in comment_files:
            try:
                data = json.loads(zf.read(fname))
                # Instagram comments format varies
                if isinstance(data, dict):
                    comment_list = data.get('comments_media_comments', data.get('comments', []))
                elif isinstance(data, list):
                    comment_list = data
                else:
                    continue
                for c in comment_list:
                    if isinstance(c, dict):
                        string_map = c.get('string_map_data', {})
                        comment_text = ''
                        ts = 0
                        for key, val in string_map.items():
                            if 'comment' in key.lower() or 'text' in key.lower():
                                comment_text = fix_encoding(val.get('value', ''))
                            if 'time' in key.lower():
                                ts = val.get('timestamp', 0)
                        if comment_text and len(comment_text) > 1:
                            items.append({
                                'type': 'comment',
                                'date': ts_to_date(ts),
                                'text': comment_text,
                                'sender': 'me',
                                'chat': ''
                            })
            except Exception as e:
                print(f'  Comment parse error {fname}: {e}')

        # ── Your Posts ────────────────────────────────────
        post_files = [n for n in names if 'content/posts' in n and n.endswith('.json')]
        for fname in post_files:
            try:
                data = json.loads(zf.read(fname))
                if isinstance(data, list):
                    posts = data
                elif isinstance(data, dict):
                    posts = [data]
                else:
                    continue
                for post in posts:
                    media_list = post if isinstance(post, list) else [post]
                    for media in media_list:
                        ts = media.get('creation_timestamp', 0)
                        title = fix_encoding(media.get('title', ''))
                        for item in media.get('media', [media]):
                            caption = fix_encoding(item.get('title', ''))
                            if not caption:
                                caption = title
                            if caption and len(caption) > 1:
                                items.append({
                                    'type': 'post',
                                    'date': ts_to_date(ts),
                                    'text': caption,
                                    'sender': 'me',
                                    'chat': ''
                                })
            except Exception as e:
                print(f'  Post parse error {fname}: {e}')

        # ── Stories ───────────────────────────────────────
        story_files = [n for n in names if 'stories' in n and n.endswith('.json')]
        for fname in story_files:
            try:
                data = json.loads(zf.read(fname))
                stories = data if isinstance(data, list) else data.get('ig_stories', [])
                for story in stories:
                    ts = story.get('creation_timestamp', 0)
                    caption = fix_encoding(story.get('title', ''))
                    if caption and len(caption) > 1:
                        items.append({
                            'type': 'story',
                            'date': ts_to_date(ts),
                            'text': caption,
                            'sender': 'me',
                            'chat': ''
                        })
            except Exception as e:
                print(f'  Story parse error {fname}: {e}')

        # ── Liked Posts ───────────────────────────────────
        liked_files = [n for n in names if 'likes/liked_posts' in n and n.endswith('.json')]
        for fname in liked_files:
            try:
                data = json.loads(zf.read(fname))
                likes = data if isinstance(data, list) else data.get('likes_media_likes', [])
                for like in likes:
                    string_map = like.get('string_map_data', {})
                    ts = 0
                    link = ''
                    for key, val in string_map.items():
                        if 'time' in key.lower():
                            ts = val.get('timestamp', 0)
                        if 'link' in key.lower() or 'href' in key.lower():
                            link = val.get('href', val.get('value', ''))
                    if link:
                        items.append({
                            'type': 'liked',
                            'date': ts_to_date(ts),
                            'text': f'Liked post: {link}',
                            'sender': 'me',
                            'chat': ''
                        })
            except Exception as e:
                print(f'  Liked parse error {fname}: {e}')

        # ── Search History ────────────────────────────────
        search_files = [n for n in names if 'recent_searches' in n and n.endswith('.json')]
        for fname in search_files:
            try:
                data = json.loads(zf.read(fname))
                searches = data if isinstance(data, list) else []
                for s in searches:
                    string_map = s.get('string_map_data', {})
                    ts = 0
                    query = ''
                    for key, val in string_map.items():
                        if 'time' in key.lower():
                            ts = val.get('timestamp', 0)
                        if 'search' in key.lower() or 'query' in key.lower():
                            query = fix_encoding(val.get('value', ''))
                    if query:
                        items.append({
                            'type': 'search',
                            'date': ts_to_date(ts),
                            'text': f'Searched on Instagram: {query}',
                            'sender': 'me',
                            'chat': ''
                        })
            except Exception as e:
                print(f'  Search parse error {fname}: {e}')

    return items

def main():
    if len(sys.argv) < 2:
        # Look for ZIP in data/instagram/
        zips = list(Path(DATA_PATH).glob('*.zip'))
        if not zips:
            print('Usage: python3 parse_instagram.py <instagram-export.zip>')
            print(f'Or place ZIP in {DATA_PATH}/')
            return
        zip_path = str(zips[0])
        print(f'Found ZIP: {zip_path}')
    else:
        zip_path = sys.argv[1]

    if not os.path.exists(zip_path):
        print(f'File not found: {zip_path}')
        return

    os.makedirs(DATA_PATH, exist_ok=True)

    print(f'Parsing {zip_path}...')
    items = parse_instagram_zip(zip_path)
    print(f'Parsed {len(items)} items total')

    # Stats
    from collections import Counter
    types = Counter(i['type'] for i in items)
    for t, c in types.most_common():
        print(f'  {t}: {c}')

    # Save
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    print(f'\nSaved to {OUTPUT_FILE}')
    print('Now run: python3 /home/tanmay/vesper/pipelines/ingest_instagram.py')

if __name__ == '__main__':
    main()
