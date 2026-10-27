import os
import re
import json
import glob
import shutil
from datetime import datetime
from PIL import Image
import genanki
from pdf2image import convert_from_path, pdfinfo_from_path
from google import genai
from google.genai import types

def get_poppler_path():
    """環境に合わせて Poppler の実行バイナリパスを自動解決する"""
    # 1. すでにシステム PATH が通っている場合 (Linux, CI, 一部のMac/Win)
    if shutil.which("pdftoppm"):
        return None
    # 2. Apple Silicon Mac (Homebrew 標準)
    if os.path.exists("/opt/homebrew/bin/pdftoppm"):
        return "/opt/homebrew/bin"
    # 3. Intel Mac (Homebrew 標準)
    if os.path.exists("/usr/local/bin/pdftoppm"):
        return "/usr/local/bin"
    # 4. 見つからない場合は None (pdf2image 側の標準例外ハンドリングに任せる)
    return None

MAX_IMAGE_LENGTH = 1600  # トークン爆発防止のため長辺を1600pxに制限

DEFAULT_PROMPT = """この講義スライド画像を分析し、医学生の復習・国試・定期試験対策として直感的に理解できる「最高品質の補足解説」をHTML形式で作成してください。

【構成・出力要件】
1. <div class="ai-title">テーマ名: 具体的な内容</div> で開始すること。
2. <div class="ai-summary-box"><div class="ai-summary-title">【病理的/臨床的的核心】</div>要点（箇条書き）</div> を含めること。
3. 疾患の分類、特徴、鑑別などの対比がある場合は、クラス「ai-table」の <table> を作成すること（項目、機序/病態、好発部位/疾患など）。
4. 顕微鏡写真、模式図、シグナル伝達の経路図などがある場合は、<div class="ai-cascade-box"> 内に矢印（↓）等を用いて病態や機序の流れを言語化すること。
5. 重要語句は <b> や <span style="color:#d70015;"> で強調し、必ず英語表記を併記すること。"""

SYSTEM_PROMPT = """
あなたは医学部専門の基礎医学・臨床医学の講師です。
提供された講義スライド画像を分析し、学生が復習・国試・定期試験で直感的に理解できる「最高品質の補足解説」をHTMLで作成してください。

【厳格なフォーマット指示】
1. マークダウンの```html ... ```ブロックは出力せず、直接埋め込めるHTML（div等）のみを出力すること。
2. 構成:
   - <div class="ai-title">テーマ名: 具体的な内容</div>
   - <div class="ai-summary-box"><div class="ai-summary-title">【病理的/臨床的的核心】</div>要点（箇条書き）</div>
   - 分類や対比がある場合は、クラス「ai-table」の<table>を作成すること（項目、機序/病態、好発部位/疾患など）。
   - 顕微鏡写真や模式図・シグナル伝達がある場合は、<div class="ai-cascade-box">内に矢印（↓）等で流れを言語化すること。
3. 重要語句は <b> や <span style="color:#d70015;"> で強調し、英語表記を併記すること。
"""

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
    poppler_bin = get_poppler_path()  # ここで動的取得

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

def build_apkg(dest_dir, deck_name, group_title, ai_cache=None):
    files = sorted(glob.glob(os.path.join(dest_dir, "*.jpg")))
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

    model_id = abs(hash("Medical_Card_Model_V2")) % (10**9)
    deck_id = abs(hash(deck_name)) % (10**9)

    model = genanki.Model(
        model_id,
        'Medical AI Slide Model',
        fields=[{'name': 'Front'}, {'name': 'Back'}],
        templates=[{'name': 'Card 1', 'qfmt': '{{Front}}', 'afmt': '{{Front}}<hr id="answer">{{Back}}'}],
        css=CARD_CSS
    )
    deck = genanki.Deck(deck_id, deck_name)
    media_list = []

    for key, data in sorted(pairs.items()):
        if 'a' in data and 'b' in data:
            f_img = os.path.basename(data['a'])
            b_img = os.path.basename(data['b'])
            
            p_label = f"{group_title} p.{data['page']}" if group_title else f"p.{data['page']}"
            front_html = f'<div style="text-align:center;font-size:13px;color:#888;">{p_label}</div><img src="{f_img}">'
            
            ai_body = ai_cache.get(b_img, "") if ai_cache else ""
            ai_html = f'<div class="ai-container">{ai_body}</div>' if ai_body else ""
            back_html = f'<img src="{b_img}">{ai_html}'

            # 決定論的GUID: デッキ名 + ページ識別キー
            guid = genanki.guid_for(f"{deck_name}_{data['page']}")
            note = genanki.Note(model=model, fields=[front_html, back_html], guid=guid)
            deck.add_note(note)

            media_list.extend([data['a'], data['b']])

    out_apkg = f"{deck_name.replace('::', '_').replace('/', '_')}.apkg"
    genanki.Package(deck, media_files=media_list).write_to_file(out_apkg)
    return out_apkg, len(deck.notes)