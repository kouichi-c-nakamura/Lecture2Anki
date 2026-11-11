import os
import re
import csv
import json
import glob
import shutil
import time
import zlib
from datetime import datetime
from PIL import Image
import genanki
from pdf2image import convert_from_path, pdfinfo_from_path
from google import genai
from google.genai import types

DEST_DIR = "exported_jpgs"
MAX_IMAGE_LENGTH = 1600

# Canonical Model schema locked across all lecture decks
CANONICAL_MODEL_ID = 851727058
CANONICAL_MODEL_NAME = "Medical AI Slide Model"

# ==============================================================================
# genanki モンキーパッチ: カードの休止（queue = -1）対応
# ==============================================================================
_orig_deck_write_to_db = genanki.Deck.write_to_db

def _patched_deck_write_to_db(self, cursor, timestamp):
    # 通常通り genanki に一時DBへ書き込ませる
    _orig_deck_write_to_db(self, cursor, timestamp)
    # 休止フラグが立っているノートのカードだけ、正規の休止ステータス (-1) に書き換える
    for note in self.notes:
        if getattr(note, "is_suspended", False):
            cursor.execute(
                "UPDATE cards SET queue = -1 WHERE nid = (SELECT id FROM notes WHERE guid = ?)",
                (note.guid,)
            )

genanki.Deck.write_to_db = _patched_deck_write_to_db

def get_poppler_path():
    """環境に合わせて Poppler の実行バイナリパスを自動解決する"""
    if shutil.which("pdftoppm"):
        return None
    if os.path.exists("/opt/homebrew/bin/pdftoppm"):
        return "/opt/homebrew/bin"
    if os.path.exists("/usr/local/bin/pdftoppm"):
        return "/usr/local/bin"
    return None

# ==============================================================================
# プロンプト定義
# ==============================================================================

OUTPUT_REQUIREMENTS = r"""【構成・出力要件】
1. 開始タグ:
   <div class="ai-title">テーマ名: 具体的な内容</div> で開始すること。
2. 問題形式スライドの場合（正解表示）:
   問題形式の場合は、以下の核心ボックスの直前に独立した正解表示ボックスを挿入すること。
   <div class="ai-summary-box"><div class="ai-summary-title">【問題の正解】</div><span style="font-size:1.15em; font-weight:bold;">正解の選択肢・語句・解答</span></div>
3. 病理的/臨床的的核心:
   <div class="ai-summary-box"><div class="ai-summary-title">【病理的/臨床的的核心】</div>要点（箇条書き）</div> を含めること。
4. 表組み（分類・対比）:
   疾患の分類、特徴、鑑別などの対比がある場合は、クラス「ai-table」の <table> を作成すること（項目、機序/病態、好発部位/疾患など）。
5. カスケード（病態・機序の流れ）:
   顕微鏡写真、模式図、シグナル伝達の経路図などがある場合は、<div class="ai-cascade-box"> 内に矢印（↓）等を用いて病態や機序の流れを言語化すること。
6. 強調と英語表記:
   重要語句は <b> や <span style="color:#d70015;"> で強調し、必ず英語表記を併記すること。
7. 数式・計算式（Anki MathJax 記法）:
   感度・特異度、オッズ比、薬物動態、クリアランス等の数式・計算式を出力する場合は、$ や $$ は絶対に使わず、必ず Anki 形式の MathJax デリミタを使用すること。
   - インライン数式: \( ... \)
   - ディスプレイ数式（別行立て）: \[ ... \]"""

DEFAULT_PROMPT = f"""この講義スライド画像を分析し、医学生の復習・国試・定期試験対策として直感的に理解できる「最高品質の補足解説」をHTML形式で作成してください。

{OUTPUT_REQUIREMENTS}"""

SVG_OVERLAY_REQUIREMENTS = r"""【SVGオーバーレイ図示の要件（最上部に配置）】
0. スライド画像のSVGオーバーレイ図示:
   画像内の重要な解剖学的指標、病変部位、画像所見、病理組織学的特徴を視覚的にポインティングするため、解説の冒頭（<div class="ai-title">の前）に以下の構造でSVGオーバーレイラッパーを出力すること。
   - 画像のアスペクト比（16:9なら viewBox="0 0 1000 562"、4:3なら viewBox="0 0 1000 750"）に合わせて座標を計算し、対象部位を <ellipse>, <circle>, <rect>, <line> 等で正確に囲み、引き出し線と <text> で [1], [2], [3]... と番号付きの簡潔なラベルを付けること。
   - 視認性を高めるため、部位ごとに鮮やかな色（#ffea00, #00e5ff, #ff6e40, #ff4081 等）を使い分け、文字には黒縁取り（paint-order: stroke; stroke: #000000; stroke-width: 4.5px;）を適用すること。
   - 右上角には Toggle とだけ記した表示切替ボタンを配置し、スライドを隠さないよう「ラッパーにマウスホバーした時だけボタンが浮かび上がる（通常時 opacity: 0; ➔ ホバー時 opacity: 0.85;）」仕様とすること。
   - 画像の src 属性には SLIDE_IMAGE.jpg を指定すること（システム側で実ファイル名に自動置換される）。
   - 以降の【病理的/臨床的的核心】やカスケードの解説本文中では、SVG内で図示した [1], [2]... の番号を引用し、画像上の位置と解説が直感的に対応するように記述すること。

   ▼ 冒頭に出力するSVGラッパーのテンプレート構造:
   <div class="svg-overlay-wrap" style="position: relative; display: inline-block; width: 100%; max-width: 850px; line-height: 0; margin-bottom: 16px;">
     <style>
       .svg-overlay-wrap .svg-toggle-btn { opacity: 0; transition: opacity 0.2s ease-in-out; }
       .svg-overlay-wrap:hover .svg-toggle-btn { opacity: 0.85; }
       .svg-overlay-wrap .svg-toggle-btn:hover { opacity: 1 !important; }
     </style>
     <img src="SLIDE_IMAGE.jpg" style="width: 100%; height: auto; display: block;">
     <button class="svg-toggle-btn" onclick="const s=this.nextElementSibling; s.style.display=s.style.display==='none'?'block':'none';" style="position: absolute; top: 8px; right: 8px; z-index: 10; padding: 4px 10px; font-size: 12px; font-family: monospace; background: rgba(0,0,0,0.75); color: #fff; border: 1px solid #888; border-radius: 4px; cursor: pointer;">
       Toggle
     </button>
     <svg viewBox="0 0 1000 562" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none;">
       <defs>
         <style>
           .lbl { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 15px; font-weight: bold; paint-order: stroke; stroke: #000000; stroke-width: 4.5px; stroke-linecap: round; stroke-linejoin: round; }
           .sub { font-size: 13px; }
         </style>
       </defs>
       <!-- ここに [1], [2], [3]... の図形・引き出し線・ラベルを記述 -->
     </svg>
   </div>"""

SVG_OVERLAY_PROMPT = f"""この講義スライド画像を分析し、医学生の復習・国試・定期試験対策として直感的に理解できる「SVGオーバーレイ図示付きの最高品質の補足解説」をHTML形式で作成してください。
マークダウンのコードブロック（```html 等）は出力せず、HTMLタグのみを直接出力してください。

{SVG_OVERLAY_REQUIREMENTS}

{OUTPUT_REQUIREMENTS}"""

SYSTEM_PROMPT = f"""あなたは医学部専門の基礎医学・臨床医学の講師です。
提供された講義スライド画像を分析し、医学生の復習・国試・定期試験対策として直感的に理解できる「最高品質の補足解説」をHTML形式で作成してください。

【厳格な出力制約】
1. マークダウンのコードブロック（```html ... ``` や ``` ... ```）は一切出力せず、直接埋め込み可能なHTMLタグのみを出力すること。
2. 数式を出力する際、$ や $$ は厳禁。必ず \\(...\\) または [ ... ] を用いること。

{OUTPUT_REQUIREMENTS}"""

CARD_CSS = """
.card { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", sans-serif; color: #222; background: #fafafa; }
.ai-container { margin-top: 20px; text-align: left; background: #fff; border-radius: 8px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); font-size: 0.95em; line-height: 1.6; }
.ai-title { color: #0b4f8c; font-size: 1.15em; font-weight: bold; border-bottom: 2px solid #007aff; padding-bottom: 4px; margin-bottom: 12px; }
.ai-summary-box { background: #fff4f4; border-left: 4px solid #ff3b30; padding: 8px 12px; margin-bottom: 14px; border-radius: 0 4px 4px 0; }
.ai-summary-title { color: #d70015; font-weight: bold; margin-bottom: 2px; }
.ai-cascade-box { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px; padding: 10px 14px; margin-top: 12px; }
.ai-table { width: 100%; border-collapse: collapse; margin: 10px 0; background: #fff; border-radius: 4px; overflow: hidden; box-shadow: 0 0 0 1px #d2e3f8; }
.ai-table th { background: #e8f2fe; color: #1a4f8b; padding: 8px; text-align: left; border-bottom: 1px solid #c2dbf7; }
.ai-table td { padding: 8px; border-bottom: 1px solid #edf2f9; border-right: 1px solid #edf2f9; vertical-align: top; }
"""

def get_canonical_model():
    """全デッキ共通の単一・標準化されたノートモデル定義"""
    return genanki.Model(
        CANONICAL_MODEL_ID,
        CANONICAL_MODEL_NAME,
        fields=[{'name': 'Front'}, {'name': 'Back'}],
        templates=[{'name': 'Card 1', 'qfmt': '{{Front}}', 'afmt': '{{Front}}<hr id="answer">{{Back}}'}],
        css=CARD_CSS
    )

# ==============================================================================
# Ankiエクスポート (.txt / TSV) からの作業状態復元ロジック
# ==============================================================================
def clean_extracted_back_html(back_html, b_img):
    """
    AnkiのBack列HTMLから外側の <img> やラッパー <div> を剥がし、
    キャッシュに格納すべき純粋な解説HTML（またはSVG+解説HTML）を抽出する。
    """
    s = back_html.strip()
    s = re.sub(r'^\s*<img\s+[^>]*src=["\']' + re.escape(b_img) + r'["\'][^>]*>\s*', '', s, count=1, flags=re.IGNORECASE).strip()
    if not s:
        return ""

    svg_splits = re.split(r'(?=(?:<!--\s*ImageSize[^>]*-->\s*)?(?:<!--\s*パート1[^>]*-->\s*)?<div\s+class=["\']svg-overlay-wrap["\'])', s)
    svg_splits = [part.strip() for part in svg_splits if part.strip()]
    if len(svg_splits) > 1:
        s = svg_splits[-1]

    if s.startswith('<div class="ai-container">') and s.endswith('</div>'):
        inner = s[len('<div class="ai-container">'):-len('</div>')].strip()
        if '<div class="ai-container">' not in inner:
            s = inner

    return s

import io

def restore_from_anki_export(
    export_txt_path,
    collection_media_dir=None,
    dest_dir="exported_jpgs",
):
    if not collection_media_dir:
        collection_media_dir = os.path.expanduser("~/Library/Application Support/Anki2/User 1/collection.media")

    os.makedirs(dest_dir, exist_ok=True)
    for old_img in glob.glob(os.path.join(dest_dir, "*.jpg")) + glob.glob(os.path.join(dest_dir, "*.png")):
        try:
            os.remove(old_img)
        except Exception:
            pass

    cache_file = os.path.join(dest_dir, "ai_notes_cache.json")
    guid_map_file = os.path.join(dest_dir, "guid_map.json")

    ai_cache = json.load(open(cache_file, "r", encoding="utf-8")) if os.path.exists(cache_file) else {}
    guid_map_data = {
        "deck_name": "",
        "notetype_name": CANONICAL_MODEL_NAME,
        "notes": {}
    }

    col_idx = {"guid": 0, "notetype": 1, "deck": 2, "front": 3, "back": 4, "tags": 5}
    has_explicit_headers = False

    copied_count = 0
    missing_images = []
    restored_notes_count = 0
    restored_ai_count = 0

    # 確実にデコード（utf-8-sig -> cp932 -> replaceフォールバック）
    with open(export_txt_path, "rb") as f_raw:
        content_bytes = f_raw.read()
    try:
        content_str = content_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            content_str = content_bytes.decode("cp932")
        except UnicodeDecodeError:
            content_str = content_bytes.decode("utf-8", errors="replace")

    reader = csv.reader(io.StringIO(content_str), delimiter="\t")
    for row in reader:
        if not row:
            continue
        first_cell = row[0].strip()
        if first_cell.startswith("#"):
            m_guid = re.match(r'^#guid column:(\d+)', first_cell)
            m_type = re.match(r'^#notetype column:(\d+)', first_cell)
            m_deck = re.match(r'^#deck column:(\d+)', first_cell)
            m_tags = re.match(r'^#tags column:(\d+)', first_cell)
            if m_guid:
                col_idx["guid"] = int(m_guid.group(1)) - 1
                has_explicit_headers = True
            elif m_type:
                col_idx["notetype"] = int(m_type.group(1)) - 1
                has_explicit_headers = True
            elif m_deck:
                col_idx["deck"] = int(m_deck.group(1)) - 1
                has_explicit_headers = True
            elif m_tags:
                col_idx["tags"] = int(m_tags.group(1)) - 1
                has_explicit_headers = True
            continue

        if has_explicit_headers:
            meta_cols = {col_idx["guid"], col_idx["notetype"], col_idx["deck"]}
            field_cols = [i for i in range(len(row)) if i not in meta_cols and i != col_idx.get("tags", -1)]
            if len(field_cols) >= 2:
                col_idx["front"], col_idx["back"] = field_cols[0], field_cols[1]

        if len(row) <= max(col_idx["front"], col_idx["back"]):
            continue

        guid = row[col_idx["guid"]].strip()
        deck_val = row[col_idx["deck"]].strip() if col_idx["deck"] < len(row) else ""
        front_html = row[col_idx["front"]].strip()
        back_html = row[col_idx["back"]].strip()
        tags_str = row[col_idx["tags"]].strip() if col_idx["tags"] < len(row) else ""

        if deck_val and not guid_map_data["deck_name"]:
            guid_map_data["deck_name"] = deck_val

        all_imgs = re.findall(r'<img\s+[^>]*src=["\']([^"\']+)["\']', front_html + " " + back_html, flags=re.IGNORECASE)
        for img_fn in set(all_imgs):
            src_p = os.path.join(collection_media_dir, img_fn)
            dst_p = os.path.join(dest_dir, img_fn)
            if os.path.exists(src_p):
                shutil.copy2(src_p, dst_p)
                copied_count += 1
            elif not os.path.exists(dst_p):
                missing_images.append(img_fn)

        f_match = re.search(r'<img\s+[^>]*src=["\']([^"\']+a\.(?:jpg|jpeg|png))["\']', front_html, flags=re.IGNORECASE)
        b_match = re.search(r'<img\s+[^>]*src=["\']([^"\']+b\.(?:jpg|jpeg|png))["\']', back_html, flags=re.IGNORECASE)
        if not b_match and f_match:
            b_img = re.sub(r'a\.(jpg|jpeg|png)$', r'b.\1', f_match.group(1), flags=re.IGNORECASE)
        elif b_match:
            b_img = b_match.group(1)
        else:
            continue

        f_img = f_match.group(1) if f_match else re.sub(r'b\.(jpg|jpeg|png)$', r'a.\1', b_img, flags=re.IGNORECASE)

        guid_map_data["notes"][b_img] = {
            "guid": guid,
            "f_img": f_img,
            "b_img": b_img,
            "front_html": front_html,
            "tags": tags_str.split() if tags_str else []
        }
        restored_notes_count += 1

        clean_ai = clean_extracted_back_html(back_html, b_img)
        if clean_ai:
            ai_cache[b_img] = clean_ai
            restored_ai_count += 1
        else:
            ai_cache.pop(b_img, None)

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(ai_cache, f, ensure_ascii=False, indent=2)

    with open(guid_map_file, "w", encoding="utf-8") as f:
        json.dump(guid_map_data, f, ensure_ascii=False, indent=2)

    return {
        "deck_name": guid_map_data["deck_name"],
        "notetype_name": CANONICAL_MODEL_NAME,
        "restored_notes": restored_notes_count,
        "restored_ai": restored_ai_count,
        "copied_images": copied_count,
        "missing_images": sorted(set(missing_images)),
    }

# ==============================================================================
# PDF抽出 & Gemini API呼び出し & APKGビルド
# ==============================================================================
def parse_pages(page_str, total_pages):
    if not page_str or page_str.strip() == "all":
        return list(range(1, total_pages + 1))
    pages = set()
    for part in page_str.split(","):
        part = part.strip()
        if "-" in part:
            s, e = part.split("-")
            pages.update(range(int(s), int(e) + 1))
        elif part.isdigit():
            pages.add(int(part))
    return sorted([p for p in pages if 1 <= p <= total_pages])

def extract_pdf_pages(unmasked_pdf, masked_pdf=None, page_str="all", password="", dpi=200, dest_dir="exported_jpgs"):
    os.makedirs(dest_dir, exist_ok=True)
    poppler_bin = get_poppler_path()

    guid_map_file = os.path.join(dest_dir, "guid_map.json")
    if os.path.exists(guid_map_file):
        os.remove(guid_map_file)

    info = pdfinfo_from_path(unmasked_pdf, poppler_path=poppler_bin, userpw=password)
    total_pages = info["Pages"]
    page_list = parse_pages(page_str, total_pages)

    is_pair = bool(masked_pdf and os.path.exists(masked_pdf))
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    for p in page_list:
        img_b = convert_from_path(unmasked_pdf, first_page=p, last_page=p, dpi=dpi, poppler_path=poppler_bin, userpw=password)[0]
        if is_pair:
            img_a = convert_from_path(masked_pdf, first_page=p, last_page=p, dpi=dpi, poppler_path=poppler_bin, userpw=password)[0]
        else:
            img_a = img_b.copy()

        for img, suffix in [(img_a, "a"), (img_b, "b")]:
            w, h = img.size
            if max(w, h) > MAX_IMAGE_LENGTH:
                ratio = MAX_IMAGE_LENGTH / max(w, h)
                img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            out_name = os.path.join(dest_dir, f"{timestamp}_{p:03d}{suffix}.jpg")
            img.save(out_name, "JPEG", quality=88)

    return timestamp, len(page_list)

def call_gemini_enhance(img_path, api_key, model_id, custom_prompt=None):
    client = genai.Client(api_key=api_key)
    img = Image.open(img_path)
    prompt = custom_prompt or DEFAULT_PROMPT

    if "svg-overlay-wrap" in prompt:
        w, h = img.size
        target_h = int(round(1000 * h / w))
        prompt = re.sub(
            r'(<svg\s+viewBox=["\']0\s+0\s+1000\s+)\d+(["\'])',
            rf'\g<1>{target_h}\g<2>',
            prompt
        )
    
    response = client.models.generate_content(
        model=model_id,
        contents=[img, prompt],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2
        )
    )
    text = response.text.strip()
    if text.startswith("```html"): text = text[7:]
    if text.startswith("```"): text = text[3:]
    if text.endswith("```"): text = text[:-3]
    return text.strip()

def build_apkg(dest_dir, deck_name, group_title, ai_cache=None, output_dir=None):
    files = sorted(glob.glob(os.path.join(dest_dir, "*.jpg")))
    if not files:
        return "", 0

    guid_map_file = os.path.join(dest_dir, "guid_map.json")
    guid_map_data = {}
    if os.path.exists(guid_map_file):
        try:
            guid_map_data = json.load(open(guid_map_file, "r", encoding="utf-8"))
        except Exception:
            guid_map_data = {}

    notes_meta = guid_map_data.get("notes", {})
    effective_deck_name = deck_name.strip() or guid_map_data.get("deck_name", "Default_Deck")

    # 対象画像の抽出（guid_mapがある場合はその対象ノートの画像、無ければ最新タイムスタンプ）
    if notes_meta:
        target_fns = {m["f_img"] for m in notes_meta.values()} | {m["b_img"] for m in notes_meta.values()}
        files = [f for f in files if os.path.basename(f) in target_fns]
    else:
        latest_ts = sorted({os.path.basename(f)[:17] for f in files})[-1]
        files = [f for f in files if os.path.basename(f).startswith(latest_ts)]
    
    pairs = {}
    pattern = re.compile(r'(\d{4}-\d{2}-\d{2}_\d{6})_(\d{3})([ab])\.jpg')

    for f in files:
        m = pattern.search(os.path.basename(f))
        if m:
            key = f"{m.group(1)}_{m.group(2)}"
            page_num = m.group(2)
            if key not in pairs:
                pairs[key] = {"page": page_num}
            pairs[key][m.group(3)] = f

    # 共通の標準モデルと決定論的デッキID
    model = get_canonical_model()
    deck_id = zlib.crc32(effective_deck_name.encode("utf-8"))
    deck = genanki.Deck(deck_id, effective_deck_name)
    media_list = []

    # Ankiが「新しいノート」と認識するように現時刻タイムスタンプを明示
    current_timestamp = int(time.time())

    for key, data in sorted(pairs.items()):
        if 'a' in data and 'b' in data:
            f_img = os.path.basename(data['a'])
            b_img = os.path.basename(data['b'])
            meta = notes_meta.get(b_img, {})

            if meta.get("front_html"):
                front_html = meta["front_html"]
            else:
                p_label = f"{group_title} p.{data['page']}" if group_title else f"p.{data['page']}"
                front_html = f'<div style="text-align:center;font-size:13px;color:#888;">{p_label}</div><img src="{f_img}">'
            
            ai_body = ai_cache.get(b_img, "").strip() if ai_cache else ""
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

            # pipeline_util.py の build_apkg 内（ノート作成ループ）
            guid = meta.get("guid") or genanki.guid_for(f"{effective_deck_name}_{data['page']}")
            tags = meta.get("tags", [])
            
            note = genanki.Note(model=model, fields=[front_html, back_html], guid=guid, tags=tags)
            note.mod = current_timestamp
            
            # 休止フラグを渡す（タグは一切増やさない）
            note.is_suspended = meta.get("is_suspended", False)
            
            deck.add_note(note)

            media_list.extend([data['a'], data['b']])

    filename = f"{effective_deck_name.replace('::', '_').replace('/', '_')}.apkg"
    target_dir = output_dir if (output_dir and os.path.exists(output_dir)) else "."
    out_apkg = os.path.join(target_dir, filename)

    genanki.Package(deck, media_files=media_list).write_to_file(out_apkg)
    return out_apkg, len(deck.notes)