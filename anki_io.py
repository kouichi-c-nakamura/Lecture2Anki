import os
import re
import io
import csv
import glob
import shutil
import time
import zlib
from datetime import datetime
from PIL import Image
import genanki
from pdf2image import convert_from_path, pdfinfo_from_path
from config import (
    DEST_DIR,
    MAX_IMAGE_LENGTH,
    CANONICAL_MODEL_ID,
    CANONICAL_MODEL_NAME,
    DYNAMIC_CLOZE_MODEL_ID,
    DYNAMIC_CLOZE_MODEL_NAME,
    CARD_CSS,
    DYNAMIC_CLOZE_CSS,
)
from storage import (
    get_meta_file_path,
    load_guid_map_from_dir,
    save_guid_map_to_dir,
    load_ai_cache_from_dir,
    save_ai_cache_to_dir,
    load_summary_cloze_from_dir,
    save_summary_cloze_to_dir,
)

# ==============================================================================
# genanki モンキーパッチ: カードの休止（queue = -1）対応
# ==============================================================================
_orig_deck_write_to_db = genanki.Deck.write_to_db


def _patched_deck_write_to_db(self, cursor, *args, **kwargs):
    _orig_deck_write_to_db(self, cursor, *args, **kwargs)
    for note in self.notes:
        if getattr(note, "is_suspended", False):
            cursor.execute(
                "UPDATE cards SET queue = -1 WHERE nid = (SELECT id FROM notes WHERE guid = ?)",
                (note.guid,)
            )


genanki.Deck.write_to_db = _patched_deck_write_to_db


def get_poppler_path():
    if shutil.which("pdftoppm"):
        return None
    if os.path.exists("/opt/homebrew/bin/pdftoppm"):
        return "/opt/homebrew/bin"
    if os.path.exists("/usr/local/bin/pdftoppm"):
        return "/usr/local/bin"
    return None


# ==============================================================================
# Anki ノートモデル定義（既存スライドモデル ＆ 新設ダイナミックClozeモデル）
# ==============================================================================
def get_canonical_model():
    return genanki.Model(
        CANONICAL_MODEL_ID,
        CANONICAL_MODEL_NAME,
        fields=[{'name': 'Front'}, {'name': 'Back'}],
        templates=[{'name': 'Card 1', 'qfmt': '{{Front}}', 'afmt': '{{Front}}<hr id="answer">{{Back}}'}],
        css=CARD_CSS
    )


def get_dynamic_cloze_model():
    """
    SortTitle / Front / Back の3フィールド構成。
    - Front: ダイナミックCloze付きノート（個別タップ開閉＋全表示切替）
    - Back : Cloze解除済みの見やすいノート ＋ LLM補足解説（.ai-container）
    """
    qfmt = """
<div class="card" id="dc-front-root">
  <div style="display:flex; justify-content:flex-end; margin-bottom:4px;">
    <button type="button" onclick="const el=document.getElementById('dc-front-root'); el.classList.toggle('back-view');" style="font-size:11px; padding:2px 8px; border:1px solid #cbd5e1; border-radius:4px; background:#fff; color:#475569; cursor:pointer;">👁️ 全表示切替</button>
  </div>
  {{Front}}
</div>
<script>
  document.querySelectorAll('#dc-front-root .cloze-container').forEach(function(el) {
    el.addEventListener('click', function() {
      this.classList.toggle('revealed');
    });
  });
</script>
"""
    afmt = """
<div class="card back-view">
  {{Back}}
</div>
"""
    combined_css = DYNAMIC_CLOZE_CSS + "\n" + CARD_CSS
    return genanki.Model(
        851727060,
        "Medical Dynamic Cloze Model 3F",
        fields=[
            {'name': 'SortTitle'},
            {'name': 'Front'},
            {'name': 'Back'},
        ],
        templates=[{'name': 'Dynamic Cloze Card', 'qfmt': qfmt, 'afmt': afmt}],
        css=combined_css
    )


# ==============================================================================
# HTMLクリーニング・<u> / [...] Cloze変換・Markdown決定論的HTML変換パーサー
# ==============================================================================
def strip_embedded_style(html_str):
    s = (html_str or "").strip()
    if s.startswith("```html"):
        s = s[7:]
    elif s.startswith("```markdown"):
        s = s[11:]
    elif s.startswith("```md"):
        s = s[5:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    s = re.sub(r'<style[^>]*>.*?</style>', '', s, flags=re.DOTALL | re.IGNORECASE).strip()
    m_card = re.match(r'^<div\s+class=["\']card["\'][^>]*>(.*)</div>\s*$', s, flags=re.DOTALL | re.IGNORECASE)
    if m_card:
        inner = m_card.group(1).strip()
        if not re.search(r'<div\s+class=["\']card["\']', inner, flags=re.IGNORECASE):
            s = inner
    return s


def renumber_cloze_spans(html_str):
    counter = [0]

    def _repl(m):
        counter[0] += 1
        return f'{m.group(1)}{counter[0]}.{m.group(2)}'

    return re.sub(
        r'(<span\s+class=["\']cloze-num["\'][^>]*>)\s*\d+\.?\s*(</span>)',
        _repl,
        html_str,
        flags=re.IGNORECASE,
    )


def convert_markers_to_cloze(html_str):
    """
    <u>語句</u> のみを
    <span class="cloze-container"><span class="cloze-num">N.</span><span class="cloze-hint">語句</span></span>
    に変換し、1. から連番を振る（LaTeXの \\[...\\] や [...] には一切干渉しない）。
    """
    s = re.sub(
        r'<u>(.*?)</u>',
        r'<span class="cloze-container"><span class="cloze-num">1.</span><span class="cloze-hint">\1</span></span>',
        html_str,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return renumber_cloze_spans(s)


def _inline_md_to_html(text):
    """インラインMarkdown（**太字**）を <b> に変換"""
    return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)


def markdown_chapter_to_html(md_chunk, deck_title, ch_idx, total_chapters):
    """
    1チャプター分の Markdown テキストを、決定論的に
    meta-header / question-box / feature-box / point-blocks / cbt-table 構造の HTML に変換する。
    """
    lines = md_chunk.strip().splitlines()
    title_text = f"Chapter {ch_idx}"
    body_lines = []

    for i, line in enumerate(lines):
        m_h1 = re.match(r'^#\s+(.+)$', line.strip())
        if m_h1:
            title_text = _inline_md_to_html(m_h1.group(1).strip())
            body_lines = lines[i + 1:]
            break
    else:
        body_lines = lines

    html_parts = [
        f'<div class="meta-header">{deck_title}（Chapter {ch_idx} / {total_chapters}）</div>',
        f'<div class="question-box">{title_text}</div>',
    ]

    in_point_blocks = False
    in_point_body = False
    in_ul = False
    table_buffer = []

    def _close_ul():
        nonlocal in_ul
        if in_ul:
            html_parts.append("</ul>")
            in_ul = False

    def _close_point_block():
        nonlocal in_point_body
        _close_ul()
        if in_point_body:
            html_parts.append("</div></div>")
            in_point_body = False

    def _close_point_blocks():
        nonlocal in_point_blocks
        _close_point_block()
        if in_point_blocks:
            html_parts.append("</div>")
            in_point_blocks = False

    def _flush_table():
        nonlocal table_buffer
        if not table_buffer:
            return
        rows = []
        for raw_r in table_buffer:
            cells = [c.strip() for c in raw_r.strip().strip("|").split("|")]
            # 区切り行 (| :--- | :---: |) をスキップ
            if all(re.fullmatch(r':?-{2,}:?', c) for c in cells if c):
                continue
            rows.append(cells)
        table_buffer = []
        if not rows:
            return

        _close_point_blocks()
        t_html = ['<table class="cbt-table"><thead><tr>']
        for h_cell in rows[0]:
            t_html.append(f"<th>{_inline_md_to_html(h_cell)}</th>")
        t_html.append("</tr></thead><tbody>")
        for r_cells in rows[1:]:
            t_html.append("<tr>")
            for c_val in r_cells:
                cell_html = _inline_md_to_html(c_val).replace(" / ", "<br>")
                t_html.append(f"<td>{cell_html}</td>")
            t_html.append("</tr>")
        t_html.append("</tbody></table>")
        html_parts.append("".join(t_html))

    for raw_line in body_lines:
        s_line = raw_line.strip()

        # テーブル行の蓄積
        if s_line.startswith("|") and "|" in s_line[1:]:
            table_buffer.append(s_line)
            continue
        else:
            _flush_table()

        if not s_line:
            continue

        # レベル2/3見出し (## / ###) -> .point-block
        m_h2 = re.match(r'^#{2,3}\s+(.+)$', s_line)
        if m_h2:
            _close_point_block()
            if not in_point_blocks:
                html_parts.append('<div class="point-blocks">')
                in_point_blocks = True
            sec_title = _inline_md_to_html(m_h2.group(1).strip())
            html_parts.append(f'<div class="point-block"><div class="point-title">{sec_title}</div><div class="point-body">')
            in_point_body = True
            continue

        # 引用行 (> ...) -> .feature-box
        if s_line.startswith(">"):
            _close_point_blocks()
            q_content = _inline_md_to_html(s_line.lstrip(">").strip())
            html_parts.append(f'<div class="feature-box">{q_content}</div>')
            continue

        # 箇条書き (- / * / 1.)
        m_li = re.match(r'^(?:[-*+]|\d+\.)\s+(.+)$', s_line)
        if m_li:
            if not in_point_blocks:
                html_parts.append('<div class="point-blocks">')
                in_point_blocks = True
            if not in_point_body:
                html_parts.append('<div class="point-block"><div class="point-body">')
                in_point_body = True
            if not in_ul:
                html_parts.append("<ul>")
                in_ul = True
            li_content = _inline_md_to_html(m_li.group(1).strip())
            html_parts.append(f"<li>{li_content}</li>")
            continue

        # 通常の段落行
        if in_point_body:
            _close_ul()
            html_parts.append(f"<p style='margin:4px 0;'>{_inline_md_to_html(s_line)}</p>")
        else:
            html_parts.append(f'<div class="feature-box">{_inline_md_to_html(s_line)}</div>')

    _flush_table()
    _close_point_blocks()

    plain_title = re.sub(r'<[^>]+>', '', title_text).strip()
    return plain_title, "\n".join(html_parts)


def parse_bulk_summary_input(raw_text, deck_title="講義要約", existing_cards=None):
    """
    貼り付けられたテキストが HTML 形式か Markdown 形式かを自動判定し、
    全チャプターに分割したうえで <u>...</u> および [...] を Cloze に変換して返す。
    """
    cleaned_all = strip_embedded_style(raw_text)
    if not cleaned_all:
        return []

    existing_by_idx = {c.get("chapter_idx"): c for c in (existing_cards or [])}
    is_html_mode = bool(re.search(r'<div\s+class=["\'](?:meta-header|question-box|feature-box|point-blocks)["\']', cleaned_all, flags=re.IGNORECASE))

    parsed_cards = []

    if is_html_mode:
        if re.search(r'<div\s+class=["\']meta-header["\']', cleaned_all, flags=re.IGNORECASE):
            chunks = re.split(r'(?=<div\s+class=["\']meta-header["\'])', cleaned_all, flags=re.IGNORECASE)
        else:
            chunks = re.split(r'(?=<div\s+class=["\']question-box["\'])', cleaned_all, flags=re.IGNORECASE)
        chunks = [c.strip() for c in chunks if c.strip()]

        for idx, chunk in enumerate(chunks, 1):
            ch_num = idx
            m_ch = re.search(r'Chapter\s*(\d+)', chunk, flags=re.IGNORECASE)
            if m_ch:
                ch_num = int(m_ch.group(1))
            else:
                m_qnum = re.search(r'<div\s+class=["\']question-box["\'][^>]*>\s*(\d+)\.', chunk, flags=re.IGNORECASE)
                if m_qnum:
                    ch_num = int(m_qnum.group(1))

            m_q = re.search(r'<div\s+class=["\']question-box["\'][^>]*>(.*?)</div>', chunk, flags=re.DOTALL | re.IGNORECASE)
            title_str = re.sub(r'<[^>]+>', '', m_q.group(1)).strip() if m_q else f"Chapter {ch_num}"

            prev = existing_by_idx.get(ch_num, {})
            guid = prev.get("guid") or genanki.guid_for(f"{deck_title}_summary_ch{ch_num:02d}")
            final_html = convert_markers_to_cloze(chunk)

            parsed_cards.append({
                "guid": guid,
                "chapter_idx": ch_num,
                "title": title_str,
                "html": final_html,
                "is_suspended": prev.get("is_suspended", False),
                "tags": prev.get("tags", []),
            })
    else:
        # Markdown モード: `# ` (H1) でチャプター分割
        md_chunks = re.split(r'(?m)(?=^#\s+)', cleaned_all)
        md_chunks = [c.strip() for c in md_chunks if c.strip()]
        total_ch = len(md_chunks)

        for idx, md_chunk in enumerate(md_chunks, 1):
            ch_num = idx
            m_num = re.match(r'^#\s+(?:Chapter\s*)?(\d+)[\.\s:：]', md_chunk, flags=re.IGNORECASE)
            if m_num:
                ch_num = int(m_num.group(1))

            title_str, raw_html = markdown_chapter_to_html(md_chunk, deck_title, ch_num, total_ch)
            prev = existing_by_idx.get(ch_num, {})
            guid = prev.get("guid") or genanki.guid_for(f"{deck_title}_summary_ch{ch_num:02d}")
            final_html = convert_markers_to_cloze(raw_html)

            parsed_cards.append({
                "guid": guid,
                "chapter_idx": ch_num,
                "title": title_str,
                "html": final_html,
                "is_suspended": prev.get("is_suspended", False),
                "tags": prev.get("tags", []),
            })

    return parsed_cards


# ==============================================================================
# Ankiエクスポート (.txt / TSV) からの2モデル自動振り分け復元
# ==============================================================================
def clean_extracted_back_html(back_html, b_img):
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


def restore_from_anki_export(
    export_txt_path,
    collection_media_dir=None,
    dest_dir="exported_jpgs",
    work_dir=None,
):
    if not collection_media_dir:
        collection_media_dir = os.path.expanduser("~/Library/Application Support/Anki2/User 1/collection.media")

    os.makedirs(dest_dir, exist_ok=True)
    for old_img in glob.glob(os.path.join(dest_dir, "*.jpg")) + glob.glob(os.path.join(dest_dir, "*.png")):
        try:
            os.remove(old_img)
        except Exception:
            pass

    ai_cache = load_ai_cache_from_dir(work_dir)
    guid_map_data = {
        "deck_name": "",
        "notetype_name": CANONICAL_MODEL_NAME,
        "is_restored_from_anki": True,
        "notes": {},
    }
    restored_summary_cards = []

    col_idx = {"guid": 0, "notetype": 1, "deck": 2, "front": 3, "back": 4, "tags": 5}
    has_explicit_headers = False

    copied_count = 0
    missing_images = []
    restored_notes_count = 0
    restored_ai_count = 0
    restored_summary_count = 0

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
        notetype_val = row[col_idx["notetype"]].strip() if col_idx["notetype"] < len(row) else ""
        deck_val = row[col_idx["deck"]].strip() if col_idx["deck"] < len(row) else ""
        field1 = row[col_idx["front"]].strip()
        field2 = row[col_idx["back"]].strip()
        tags_str = row[col_idx["tags"]].strip() if col_idx["tags"] < len(row) else ""

        if deck_val and not guid_map_data["deck_name"]:
            guid_map_data["deck_name"] = deck_val

        # A. 新設の要約圧縮ダイナミックClozeモデルの自動判定
        is_dynamic_cloze = (
            notetype_val == DYNAMIC_CLOZE_MODEL_NAME
            or "cloze-container" in field2
            or "cloze-container" in field1
        )
        if is_dynamic_cloze and not re.search(r'[ab]\.(?:jpg|jpeg|png)', field1, re.IGNORECASE):
            body_raw = field2 if ("cloze-container" in field2 or len(field2) > len(field1)) else field1
            title_raw = field1 if body_raw == field2 else ""
            clean_body = strip_embedded_style(body_raw)
            if not title_raw:
                m_q = re.search(r'<div\s+class=["\']question-box["\'][^>]*>(.*?)</div>', clean_body, flags=re.DOTALL | re.IGNORECASE)
                title_raw = re.sub(r'<[^>]+>', '', m_q.group(1)).strip() if m_q else f"Chapter {restored_summary_count + 1}"
            restored_summary_count += 1
            restored_summary_cards.append({
                "guid": guid,
                "chapter_idx": restored_summary_count,
                "title": title_raw,
                "html": clean_body,
                "is_suspended": False,
                "tags": tags_str.split() if tags_str else [],
            })
            continue

        # B. 既存のスライドモデル (Medical AI Slide Model) の復元
        front_html, back_html = field1, field2
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
            "is_suspended": False,
            "tags": tags_str.split() if tags_str else []
        }
        restored_notes_count += 1

        clean_ai = clean_extracted_back_html(back_html, b_img)
        if clean_ai:
            ai_cache[b_img] = clean_ai
            restored_ai_count += 1
        else:
            ai_cache.pop(b_img, None)

    save_ai_cache_to_dir(ai_cache, work_dir)
    save_guid_map_to_dir(guid_map_data, work_dir)
    if restored_summary_cards:
        save_summary_cloze_to_dir(restored_summary_cards, work_dir, deck_name=guid_map_data["deck_name"])

    return {
        "deck_name": guid_map_data["deck_name"],
        "notetype_name": CANONICAL_MODEL_NAME,
        "restored_notes": restored_notes_count,
        "restored_ai": restored_ai_count,
        "restored_summary": restored_summary_count,
        "copied_images": copied_count,
        "missing_images": sorted(set(missing_images)),
    }


# ==============================================================================
# PDF抽出 & APKG混載ビルド（元スライド先 ➔ 要約Cloze後の順序固定）
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


def extract_pdf_pages(unmasked_pdf, masked_pdf=None, page_str="all", password="", dpi=200, dest_dir="exported_jpgs", work_dir=None):
    os.makedirs(dest_dir, exist_ok=True)
    poppler_bin = get_poppler_path()

    gm_path = get_meta_file_path("guid_map.json", work_dir)
    if os.path.exists(gm_path):
        try:
            os.remove(gm_path)
        except Exception:
            pass

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


def build_apkg(dest_dir, deck_name, group_title, ai_cache=None, output_dir=None, work_dir=None, summary_cards=None):
    files = sorted(glob.glob(os.path.join(dest_dir, "*.jpg")))
    guid_map_data = load_guid_map_from_dir(work_dir)
    notes_meta = guid_map_data.get("notes", {})

    # Ankiから復元された正確な deck_name がある場合はそれを最優先し、デッキ分裂を防ぐ
    restored_deck = (guid_map_data.get("deck_name") or "").strip()
    input_deck = (deck_name or "").strip()
    effective_deck_name = restored_deck if (guid_map_data.get("is_restored_from_anki") and restored_deck) else (input_deck or restored_deck or "Default_Deck")

    if summary_cards is None:
        summary_cards = load_summary_cloze_from_dir(work_dir, deck_name=effective_deck_name)

    if not files and not summary_cards:
        return "", 0, 0, 0

    deck_id = zlib.crc32(effective_deck_name.encode("utf-8"))
    deck = genanki.Deck(deck_id, effective_deck_name)
    media_list = []
    current_timestamp = int(time.time())

    slide_note_count = 0
    summary_note_count = 0

    # ① 元スライドカード (Medical AI Slide Model) を先に追加
    if files:
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

        slide_model = get_canonical_model()

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

                guid = meta.get("guid") or genanki.guid_for(f"{effective_deck_name}_{data['page']}")
                tags = meta.get("tags", [])

                note = genanki.Note(model=slide_model, fields=[front_html, back_html], guid=guid, tags=tags)
                note.mod = current_timestamp
                note.is_suspended = meta.get("is_suspended", False)

                deck.add_note(note)
                media_list.extend([data['a'], data['b']])
                slide_note_count += 1

    # ② 要約圧縮ダイナミックClozeカード (SortTitle / Front / Back) を後に追加
    if summary_cards:
        dc_model = get_dynamic_cloze_model()
        prefix_base = group_title.strip() if (group_title and group_title.strip()) else effective_deck_name.split("::")[-1].strip()
        for idx, card in enumerate(sorted(summary_cards, key=lambda c: c.get("chapter_idx", 999)), 1):
            front_html = strip_embedded_style(card.get("html", ""))
            if not front_html:
                continue
            ch_idx = int(card.get("chapter_idx") or idx)
            raw_title = (card.get("title") or f"Chapter {ch_idx}").strip()
            if prefix_base and not raw_title.startswith(prefix_base):
                sort_title = f"{prefix_base} s.{ch_idx:02d} {raw_title}"
            else:
                sort_title = raw_title

            # Back フィールド: ① Cloze解除済みノート本体 ＋ ② LLM補足解説（二重バックスラッシュも補正）
            back_ai = strip_embedded_style(card.get("back_extra", ""))
            back_ai = back_ai.replace(r"\\(", r"\(").replace(r"\\)", r"\)").replace(r"\\[", r"\[").replace(r"\\]", r"\]")
            ai_block = f'<hr id="answer"><div class="ai-container">{back_ai}</div>' if back_ai else ""
            back_html = f'<div class="back-view">{front_html}</div>{ai_block}'

            guid = card.get("guid") or genanki.guid_for(f"{effective_deck_name}_summary_ch{ch_idx:02d}")
            tags = card.get("tags", [])

            note = genanki.Note(
                model=dc_model,
                fields=[sort_title, front_html, back_html],
                guid=guid,
                tags=tags,
            )
            note.mod = current_timestamp
            note.is_suspended = card.get("is_suspended", False)
            deck.add_note(note)
            summary_note_count += 1

    filename = f"{effective_deck_name.replace('::', '_').replace('/', '_')}.apkg"
    target_dir = output_dir if (output_dir and os.path.exists(output_dir)) else "."
    out_apkg = os.path.join(target_dir, filename)

    genanki.Package(deck, media_files=media_list).write_to_file(out_apkg)
    return out_apkg, len(deck.notes), slide_note_count, summary_note_count