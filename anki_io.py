import os
import re
import io
import csv
import glob
import shutil
import time
import zlib
import tempfile
from datetime import datetime
from html.parser import HTMLParser
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
    load_guid_map_from_dir,
    save_guid_map_to_dir,
    load_ai_cache_from_dir,
    save_ai_cache_to_dir,
    load_summary_cloze_from_dir,
    save_summary_cloze_to_dir,
    get_meta_file_path,
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
# Anki ノートモデル定義（既存スライドモデル ＆ 新設ダイナミックCloze 3Fモデル）
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
        DYNAMIC_CLOZE_MODEL_ID,
        DYNAMIC_CLOZE_MODEL_NAME,
        fields=[
            {'name': 'SortTitle'},
            {'name': 'Front'},
            {'name': 'Back'},
        ],
        templates=[{'name': 'Dynamic Cloze Card', 'qfmt': qfmt, 'afmt': afmt}],
        css=combined_css
    )


# ==============================================================================
# HTMLクリーニング・<u> Cloze変換・Markdown決定論的HTML変換パーサー（標準ライブラリ完結）
# ==============================================================================
class _DOMRepairParser(HTMLParser):
    VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "col"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.out = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        attr_str = "".join(f' {k}="{v}"' if v is not None else f" {k}" for k, v in attrs)
        self.out.append(f"<{t}{attr_str}>")
        if t not in self.VOID_TAGS:
            self.stack.append(t)

    def handle_startendtag(self, tag, attrs):
        t = tag.lower()
        attr_str = "".join(f' {k}="{v}"' if v is not None else f" {k}" for k, v in attrs)
        self.out.append(f"<{t}{attr_str} />")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self.VOID_TAGS or t not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.out.append(f"</{top}>")
            if top == t:
                break

    def handle_data(self, data):
        self.out.append(data)

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")

    def get_repaired_html(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def repair_html_tree(html_str):
    """Python標準のHTMLParserで未閉鎖タグ・タグ交差・孤立閉じタグを自動修復する"""
    parser = _DOMRepairParser()
    parser.feed(html_str)
    parser.close()
    return parser.get_repaired_html()


def strip_embedded_style(html_str):
    """HTML文字列に埋め込まれた <style>...</style> タグを除去する"""
    if not html_str:
        return ""
    return re.sub(r'<style[^>]*>.*?</style>', '', html_str, flags=re.DOTALL | re.IGNORECASE).strip()


def renumber_cloze_spans(html_str):
    """
    <span class="cloze-container"><span class="cloze-num">N.</span>...</span>
    の番号を、出現順に 1. から連番で振り直す。
    """
    counter = 0

    def _repl(match):
        nonlocal counter
        counter += 1
        return f'{match.group(1)}{counter}.{match.group(2)}'

    pattern = re.compile(
        r'(<span\s+class=["\']cloze-container["\']>\s*<span\s+class=["\']cloze-num["\']>)\s*\d+\.?\s*(</span>)',
        re.IGNORECASE,
    )
    return pattern.sub(_repl, html_str)


def normalize_crossed_underline_and_bold(text):
    """
    エディタの Cmd+U 等で生じた `**<u>...**</u>` や `<u>**...</u>**` のタグ交差を正規化し、
    `<u>...</u>` 内の `**...**` を先に <b>...</b> に展開してから、外側の `**...**` を変換する。
    """
    text = re.sub(r'\*\*<u>(.*?)\*\*</u>', r'<u><b>\1</b></u>', text, flags=re.DOTALL)
    text = re.sub(r'<u>\*\*(.*?)</u>\*\*', r'<u><b>\1</b></u>', text, flags=re.DOTALL)

    def _bold_inside_u(m):
        inner = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', m.group(1), flags=re.DOTALL)
        return f'<u>{inner}</u>'

    text = re.sub(r'<u>(.*?)</u>', _bold_inside_u, text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\*\*((?:(?!<u>|</u>).)+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    return text


def convert_markers_to_cloze(html_str):
    """
    1. ** と <u> の交差を正規化
    2. <u>語句</u> を <span class="cloze-container">...</span> に変換（LaTeX [...] には干渉しない）
    3. HTMLParser でHTML階層構造を完全修復
    4. 1. から連番を振る
    """
    s = normalize_crossed_underline_and_bold(html_str)
    s = re.sub(
        r'<u>(.*?)</u>',
        r'<span class="cloze-container"><span class="cloze-num">1.</span><span class="cloze-hint">\1</span></span>',
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )
    s = repair_html_tree(s)
    return renumber_cloze_spans(s)


def _markdown_chapter_to_html(md_body, deck_title, ch_idx, ch_title):
    """1チャプター分のMarkdownテキストを、タグ交差やテーブル崩れなくクリーンなHTMLに変換する"""
    u_tokens = []

    def _stash_u(m):
        inner = m.group(1)
        inner = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', inner, flags=re.DOTALL)
        token = f"@@CLOZE_U_{len(u_tokens)}@@"
        u_tokens.append(f"<u>{inner}</u>")
        return token

    s = re.sub(r'\*\*<u>(.*?)\*\*</u>', r'<u><b>\1</b></u>', md_body, flags=re.DOTALL)
    s = re.sub(r'<u>\*\*(.*?)</u>\*\*', r'<u><b>\1</b></u>', s, flags=re.DOTALL)
    s = re.sub(r'<u>(.*?)</u>', _stash_u, s, flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s, flags=re.DOTALL)

    lines = s.splitlines()
    html_parts = [
        f'<div class="meta-header">{deck_title}（Chapter {ch_idx}）</div>',
        f'<div class="question-box">{ch_title}</div>',
    ]

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # 引用ブロック (> ...) -> feature-box
        if line.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(re.sub(r'^>\s*', '', lines[i].strip()))
                i += 1
            html_parts.append(f'<div class="feature-box">{"<br>".join(quote_lines)}</div>')
            continue

        # レベル2見出し (## ...) -> point-box
        if line.startswith("## "):
            sec_title = line[3:].strip()
            html_parts.append(f'<div class="point-box"><div class="point-title">{sec_title}</div><div class="point-body">')
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if nxt.startswith("## ") or nxt.startswith("|") or nxt.startswith(">"):
                    break
                if not nxt:
                    i += 1
                    continue
                if nxt.startswith("- ") or nxt.startswith("* "):
                    ul_items = []
                    while i < len(lines) and (lines[i].strip().startswith("- ") or lines[i].strip().startswith("* ")):
                        ul_items.append(f"<li>{lines[i].strip()[2:].strip()}</li>")
                        i += 1
                    html_parts.append(f"<ul>{''.join(ul_items)}</ul>")
                else:
                    html_parts.append(f"<p>{nxt}</p>")
                    i += 1
            html_parts.append("</div></div>")
            continue

        # Markdownテーブル (| ... |)
        if line.startswith("|") and "|" in line[1:]:
            tbl_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl_lines.append(lines[i].strip())
                i += 1
            if len(tbl_lines) >= 2:
                headers = [c.strip() for c in tbl_lines[0].strip("|").split("|")]
                start_row = 2 if re.match(r'^[\|\s:\-]+$', tbl_lines[1]) else 1
                thead = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead>"
                tbody_rows = []
                for r_line in tbl_lines[start_row:]:
                    cells = [c.strip() for c in r_line.strip("|").split("|")]
                    while len(cells) < len(headers):
                        cells.append("")
                    tbody_rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells[:len(headers)]) + "</tr>")
                html_parts.append(f'<table class="cbt-table">{thead}<tbody>{"".join(tbody_rows)}</tbody></table>')
            continue

        # 通常の箇条書き (- / *)
        if line.startswith("- ") or line.startswith("* "):
            ul_items = []
            while i < len(lines) and (lines[i].strip().startswith("- ") or lines[i].strip().startswith("* ")):
                ul_items.append(f"<li>{lines[i].strip()[2:].strip()}</li>")
                i += 1
            html_parts.append(f'<div class="point-box"><div class="point-body"><ul>{"".join(ul_items)}</ul></div></div>')
            continue

        # 通常段落
        html_parts.append(f'<div class="feature-box">{line}</div>')
        i += 1

    raw_html = "\n".join(html_parts)
    for idx_tok, orig_u in enumerate(u_tokens):
        raw_html = raw_html.replace(f"@@CLOZE_U_{idx_tok}@@", orig_u)

    return convert_markers_to_cloze(raw_html)


def parse_bulk_summary_input(raw_text, deck_title="講義要約", existing_cards=None):
    """
    Markdown（`# 1. 章タイトル` 形式）またはHTML一括テキストを受け取り、
    チャプター単位に分割して `<u>...</u>` をダイナミックCloze化した辞書リストを返す。
    既存の `existing_cards` がある場合は chapter_idx ごとに GUID・back_extra・休止状態を引き継ぐ。
    """
    if not raw_text or not raw_text.strip():
        return []

    cleaned_raw = strip_embedded_style(raw_text)
    existing_by_idx = {}
    if existing_cards:
        for c in existing_cards:
            existing_by_idx[int(c.get("chapter_idx", 0))] = c

    results = []

    # パターン1: Markdown のレベル1見出し (`# 1. ...` または `# ...`) で分割
    md_heading_matches = list(re.finditer(r'(?m)^#\s+(.+)$', cleaned_raw))
    if md_heading_matches:
        for i, m in enumerate(md_heading_matches):
            ch_idx = i + 1
            ch_title = m.group(1).strip()
            start_pos = m.end()
            end_pos = md_heading_matches[i + 1].start() if (i + 1 < len(md_heading_matches)) else len(cleaned_raw)
            md_body = cleaned_raw[start_pos:end_pos].strip()

            html_card = _markdown_chapter_to_html(md_body, deck_title, ch_idx, ch_title)
            prev = existing_by_idx.get(ch_idx, {})
            guid = prev.get("guid") or genanki.guid_for(f"{deck_title}_summary_ch{ch_idx:02d}")
            results.append({
                "guid": guid,
                "chapter_idx": ch_idx,
                "title": ch_title,
                "html": html_card,
                "back_extra": prev.get("back_extra", ""),
                "is_suspended": prev.get("is_suspended", False),
                "tags": prev.get("tags", []),
            })
        return results

    # パターン2: HTML `<div class="card">` 複数ブロックでの分割
    card_blocks = re.findall(r'<div\s+class=["\']card["\'][^>]*>(.*?)</div>\s*(?=<div\s+class=["\']card["\']|$)', cleaned_raw, flags=re.DOTALL | re.IGNORECASE)
    if not card_blocks:
        card_blocks = [cleaned_raw]

    for i, block in enumerate(card_blocks):
        ch_idx = i + 1
        q_match = re.search(r'<div\s+class=["\']question-box["\'][^>]*>(.*?)</div>', block, flags=re.DOTALL | re.IGNORECASE)
        if q_match:
            ch_title = re.sub(r'<[^>]+>', '', q_match.group(1)).strip()
        else:
            ch_title = f"Chapter {ch_idx}"

        html_card = convert_markers_to_cloze(block.strip())
        prev = existing_by_idx.get(ch_idx, {})
        guid = prev.get("guid") or genanki.guid_for(f"{deck_title}_summary_ch{ch_idx:02d}")
        results.append({
            "guid": guid,
            "chapter_idx": ch_idx,
            "title": ch_title,
            "html": html_card,
            "back_extra": prev.get("back_extra", ""),
            "is_suspended": prev.get("is_suspended", False),
            "tags": prev.get("tags", []),
        })

    return results


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

        # A. 要約圧縮ダイナミックClozeモデルの自動判定（3フィールド: SortTitle / Front / Back 対応）
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

            # 3フィールド形式の場合は row の次の列に Back が入る
            back_extra_extracted = ""
            if len(row) > col_idx["back"] + 1 and col_idx["back"] + 1 != col_idx.get("tags", -1):
                full_back_col = strip_embedded_style(row[col_idx["back"] + 1].strip())
                m_ai = re.search(r'<div\s+class=["\']ai-container["\']>(.*)</div>\s*$', full_back_col, flags=re.DOTALL | re.IGNORECASE)
                if m_ai:
                    back_extra_extracted = m_ai.group(1).strip()

            restored_summary_count += 1
            restored_summary_cards.append({
                "guid": guid,
                "chapter_idx": restored_summary_count,
                "title": title_raw,
                "html": clean_body,
                "back_extra": back_extra_extracted,
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
# PDF抽出（ページ番号基準の解説・GUID・休止自動引き継ぎ対応） & APKG混載ビルド
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


def _extract_page_num_from_b_img(filename):
    """'2026-11-24_140000_003b.jpg' または 'p.003' からページ番号 (3) を抽出する"""
    m = re.search(r'_(\d+)b\.(?:jpg|jpeg|png)$', filename, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m_p = re.search(r'\bp\.(\d+)\b', filename, re.IGNORECASE)
    if m_p:
        return int(m_p.group(1))
    return None


def extract_pdf_pages(
    unmasked_pdf,
    masked_pdf=None,
    page_str="all",
    password="",
    dpi=200,
    dest_dir="exported_jpgs",
    work_dir=None,
    preserve_existing=True,
    deck_name="",
):
    os.makedirs(dest_dir, exist_ok=True)
    poppler_bin = get_poppler_path()

    # 既存データのバックアップ辞書（ページ番号 -> 既存データ）を構築
    old_gm = load_guid_map_from_dir(work_dir) if preserve_existing else {"notes": {}}
    old_notes = old_gm.get("notes", {})
    old_ai = load_ai_cache_from_dir(work_dir) if preserve_existing else {}

    page_to_old_note = {}
    for b_fn, meta in old_notes.items():
        p_no = _extract_page_num_from_b_img(b_fn)
        if p_no is not None:
            page_to_old_note[p_no] = meta

    page_to_old_ai = {}
    for b_fn, html_val in old_ai.items():
        p_no = _extract_page_num_from_b_img(b_fn)
        if p_no is not None and html_val:
            page_to_old_ai[p_no] = html_val

    info = pdfinfo_from_path(unmasked_pdf, poppler_path=poppler_bin, userpw=password)
    total_pages = info["Pages"]
    page_list = parse_pages(page_str, total_pages)

    is_pair = bool(masked_pdf and os.path.exists(masked_pdf))
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    new_notes = {}
    migrated_ai_count = 0

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

        f_fn = f"{timestamp}_{p:03d}a.jpg"
        b_fn = f"{timestamp}_{p:03d}b.jpg"

        if preserve_existing and p in page_to_old_note:
            old_m = page_to_old_note[p]
            new_notes[b_fn] = {
                "guid": old_m.get("guid"),
                "f_img": f_fn,
                "b_img": b_fn,
                "is_suspended": old_m.get("is_suspended", False),
                "tags": old_m.get("tags", []),
            }
        else:
            new_notes[b_fn] = {
                "f_img": f_fn,
                "b_img": b_fn,
                "is_suspended": False,
                "tags": [],
            }

        if preserve_existing and p in page_to_old_ai:
            old_ai[b_fn] = page_to_old_ai[p]
            migrated_ai_count += 1

    if preserve_existing:
        merged_gm = dict(old_gm)
        merged_gm["notes"] = new_notes
        save_guid_map_to_dir(merged_gm, work_dir)
        if migrated_ai_count > 0:
            save_ai_cache_to_dir(old_ai, work_dir)
    else:
        save_guid_map_to_dir({"notes": new_notes}, work_dir)
        save_ai_cache_to_dir({}, work_dir)
        save_summary_cloze_to_dir([], work_dir, deck_name=deck_name)

    return timestamp, len(page_list), migrated_ai_count


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


    fd, tmp_apkg = tempfile.mkstemp(prefix=".tmp_", suffix=".apkg")
    os.close(fd)
    try:
        genanki.Package(deck, media_files=media_list).write_to_file(tmp_apkg)
        shutil.move(tmp_apkg, out_apkg)
    finally:
        if os.path.exists(tmp_apkg):
            try:
                os.remove(tmp_apkg)
            except Exception:
                pass

    return out_apkg, len(deck.notes), slide_note_count, summary_note_count