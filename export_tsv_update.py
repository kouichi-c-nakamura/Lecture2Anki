import os
import re
import csv
import json
import sqlite3

DEST_DIR = "exported_jpgs"
CACHE_FILE = os.path.join(DEST_DIR, "ai_notes_cache.json")
GUID_MAP_FILE = os.path.join(DEST_DIR, "guid_map.json")
OUT_TSV = "3_胆膵_update.txt"
ANKI_DB = os.path.expanduser("~/Library/Application Support/Anki2/User 1/collection.anki2")

print("=== 1. Checking Anki SQLite Database ===")
if os.path.exists(ANKI_DB):
    try:
        conn = sqlite3.connect(f"file:{ANKI_DB}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT models FROM col")
        row = cursor.fetchone()
        if row and row[0]:
            models = json.loads(row[0])
            print("Existing models in Anki collection:")
            for mid, mdata in models.items():
                if "Medical" in mdata.get("name", ""):
                    print(f"  - Name: '{mdata['name']}', ID: {mid}, Fields: {[f['name'] for f in mdata.get('flds', [])]}")
        conn.close()
    except Exception as e:
        print(f"Could not inspect collection.anki2: {e}")
else:
    print("collection.anki2 not found at default path.")

print("\n=== 2. Generating Update TSV ===")
if not os.path.exists(GUID_MAP_FILE):
    print(f"Error: {GUID_MAP_FILE} missing.")
    exit(1)

guid_data = json.load(open(GUID_MAP_FILE, "r", encoding="utf-8"))
ai_cache = json.load(open(CACHE_FILE, "r", encoding="utf-8")) if os.path.exists(CACHE_FILE) else {}
notes_meta = guid_data.get("notes", {})

rows = []
for b_img, meta in sorted(notes_meta.items()):
    guid = meta["guid"]
    front_html = meta["front_html"]
    tags_str = " ".join(meta.get("tags", []))
    ai_body = ai_cache.get(b_img, "").strip()

    if "<svg" in ai_body and "<img" in ai_body:
        ai_body_clean = re.sub(
            r'^\s*<img\s+[^>]*src=["\']' + re.escape(b_img) + r'["\'][^>]*>\s*',
            '',
            ai_body,
            count=1,
            flags=re.IGNORECASE
        ).strip()
        ai_body_fixed = re.sub(
            r'(<img\s+[^>]*src=["\'])([^"\']+)(["\'])',
            rf'\g<1>{b_img}\g<3>',
            ai_body_clean,
            count=1
        )
        back_html = f'<div class="ai-container">{ai_body_fixed}</div>'
    else:
        ai_html = f'<div class="ai-container">{ai_body}</div>' if ai_body else ""
        back_html = f'<img src="{b_img}">{ai_html}'

    rows.append([guid, front_html, back_html, tags_str])

with open(OUT_TSV, "w", encoding="utf-8", newline="") as f:
    f.write("#separator:tab\n#html:true\n#guid column:1\n#tags column:4\n")
    writer = csv.writer(f, delimiter="\t")
    for r in rows:
        writer.writerow(r)

print(f"Output generated: {OUT_TSV} ({len(rows)} notes)")