import os
import re
import io
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

# 1. 既存のスライドモデル（絶対に変更しない）
CANONICAL_MODEL_ID = 851727058
CANONICAL_MODEL_NAME = "Medical AI Slide Model"

# 2. 新設の要約圧縮ダイナミックClozeモデル（別IDで完全分離）
DYNAMIC_CLOZE_MODEL_ID = 851727059
DYNAMIC_CLOZE_MODEL_NAME = "Medical Dynamic Cloze Model"

# API実測ログ・単価定義
API_USAGE_LOG_FILE = os.path.join(DEST_DIR, "api_usage_log.jsonl")
USD_JPY_RATE = 155.0

MODEL_PRICING_PER_1M = {
    "gemini-3.6-flash":      {"in": 0.75, "out": 3.75},
    "gemini-3.5-flash-lite": {"in": 0.30, "out": 2.50},
    "gemini-3.8-flash":      {"in": 0.75, "out": 3.75},
    "gemini-4-argon":        {"in": 2.00, "out": 10.00},
}

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


# ==============================================================================
# 講義フォルダ直下での3つのJSON分離・永続化ヘルパー（旧データ自動引き継ぎ付き）
# ==============================================================================
def resolve_meta_dir(work_dir=None):
    if work_dir and str(work_dir).strip() and os.path.isdir(str(work_dir).strip()):
        return str(work_dir).strip()
    os.makedirs(DEST_DIR, exist_ok=True)
    return DEST_DIR

def get_meta_file_path(filename, work_dir=None):
    target_dir = resolve_meta_dir(work_dir)
    target_path = os.path.join(target_dir, filename)
    legacy_path = os.path.join(DEST_DIR, filename)
    # 講義フォルダ直下にまだ存在せず、exported_jpgs側に旧ファイルがあれば自動コピーして引き継ぐ
    if target_dir != DEST_DIR and not os.path.exists(target_path) and os.path.exists(legacy_path):
        try:
            shutil.copy2(legacy_path, target_path)
        except Exception:
            pass
    return target_path

def load_guid_map_from_dir(work_dir=None):
    p = get_meta_file_path("guid_map.json", work_dir)
    if os.path.exists(p):
        try:
            return json.load(open(p, "r", encoding="utf-8"))
        except Exception:
            pass
    return {
        "deck_name": "",
        "notetype_name": CANONICAL_MODEL_NAME,
        "is_restored_from_anki": False,
        "notes": {},
    }

def save_guid_map_to_dir(data, work_dir=None):
    p = get_meta_file_path("guid_map.json", work_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def load_ai_cache_from_dir(work_dir=None):
    p = get_meta_file_path("ai_notes_cache.json", work_dir)
    if os.path.exists(p):
        try:
            return json.load(open(p, "r", encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_ai_cache_to_dir(data, work_dir=None):
    p = get_meta_file_path("ai_notes_cache.json", work_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def load_summary_cloze_from_dir(work_dir=None):
    p = get_meta_file_path("summary_cloze_cache.json", work_dir)
    if os.path.exists(p):
        try:
            data = json.load(open(p, "r", encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []

def save_summary_cloze_to_dir(cards_list, work_dir=None):
    p = get_meta_file_path("summary_cloze_cache.json", work_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(cards_list, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def log_api_usage(img_filename, model_id, prompt_tokens, output_tokens, thought_tokens, is_svg=False):
    """Gemini APIの実測トークン消費量（思考トークン含む）と概算コストをJSONL形式で記録する"""
    os.makedirs(os.path.dirname(API_USAGE_LOG_FILE), exist_ok=True)
    spec = MODEL_PRICING_PER_1M.get(model_id, {"in": 0.75, "out": 3.75})

    total_out_billed = output_tokens + thought_tokens
    cost_usd = (prompt_tokens / 1_000_000 * spec["in"]) + (total_out_billed / 1_000_000 * spec["out"])
    cost_jpy = cost_usd * USD_JPY_RATE

    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "image": img_filename,
        "model_id": model_id,
        "is_svg": is_svg,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "thought_tokens": thought_tokens,
        "total_tokens": prompt_tokens + total_out_billed,
        "cost_usd": round(cost_usd, 6),
        "cost_jpy": round(cost_jpy, 4),
    }
    try:
        with open(API_USAGE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return record

def get_poppler_path():
    if shutil.which("pdftoppm"):
        return None
    if os.path.exists("/opt/homebrew/bin/pdftoppm"):
        return "/opt/homebrew/bin"
    if os.path.exists("/usr/local/bin/pdftoppm"):
        return "/usr/local/bin"
    return None


# ==============================================================================
# プロンプト定義（既存スライド解説 ＆ 要約圧縮ダイナミックCloze）
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


# ==============================================================================
# 要約圧縮ダイナミックCloze用 CSS・プロンプト・テンプレート
# ==============================================================================
DYNAMIC_CLOZE_CSS = """
.card {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Hiragino Kaku Gothic ProN", sans-serif;
  font-size: 14.5px;
  color: #1e293b;
  background-color: #f8fafc;
  padding: 14px;
  line-height: 1.6;
  text-align: left;
}
.meta-header {
  font-size: 11.5px;
  color: #64748b;
  margin-bottom: 6px;
  font-weight: 600;
}
.question-box {
  font-size: 15.5px;
  font-weight: 600;
  line-height: 1.6;
  margin-bottom: 10px;
  color: #0f172a;
}
.feature-box {
  background: #f1f5f9;
  border-left: 3px solid #64748b;
  padding: 8px 12px;
  margin: 8px 0;
  font-size: 13.5px;
  border-radius: 0 4px 4px 0;
}
.point-blocks {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 12px 0;
}
.point-block {
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-left: 4px solid #3b82f6;
  border-radius: 4px 6px 6px 4px;
  padding: 8px 12px;
}
.point-title {
  font-size: 12.5px;
  font-weight: bold;
  color: #1e40af;
  margin-bottom: 2px;
}
.point-body {
  font-size: 13.5px;
  color: #334155;
  line-height: 1.5;
}
.point-body .em-red {
  color: #dc2626;
  font-weight: 600;
}
.point-body b {
  color: #0f172a;
}
.point-body ul {
  margin: 4px 0;
  padding-left: 18px;
}
.cloze-container {
  display: inline-flex;
  align-items: baseline;
  cursor: pointer;
  user-select: none;
  margin: 0 1px;
}
.cloze-num {
  color: #dc2626;
  font-weight: bold;
  margin-right: 2px;
  font-size: 0.85em;
}
.cloze-hint {
  position: relative;
  background-color: #fef08a !important;
  color: transparent !important;
  border-radius: 3px;
  padding: 0 4px;
  display: inline-block;
  transition: all 0.15s ease-in-out;
  min-width: 2.2em;
  text-align: center;
}
.cloze-hint[data-hint]::after {
  content: attr(data-hint);
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  color: #854d0e;
  font-size: 0.75em;
  font-weight: 600;
  white-space: nowrap;
  pointer-events: none;
}
.cloze-container:hover .cloze-hint,
.cloze-container:active .cloze-hint,
.cloze-container.revealed .cloze-hint,
.back-view .cloze-hint {
  background-color: transparent !important;
  color: #0f172a !important;
  font-weight: 600;
  text-decoration: underline;
  text-decoration-color: #3b82f6;
  text-underline-offset: 3px;
}
.cloze-container:hover .cloze-hint::after,
.cloze-container:active .cloze-hint::after,
.cloze-container.revealed .cloze-hint::after,
.back-view .cloze-hint::after {
  display: none !important;
}
.cbt-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13.5px;
  line-height: 1.5;
  margin: 10px 0;
  background: #ffffff;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid #cbd5e1;
}
.cbt-table th, .cbt-table td {
  border: 1px solid #e2e8f0;
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}
.cbt-table th {
  background-color: #f1f5f9;
  color: #334155;
  font-weight: 600;
}
"""

SUMMARY_CLOZE_PROMPT_TEMPLATE = r"""提供された講義スライド群の内容を体系的に整理・統合し、試験前に短時間で全範囲を巡回・暗記できる「超高密度・ダイナミックCloze要約カード」を 全 {target_count} 枚（Chapter 1 〜 {target_count}）作成してください。

【重要な設計指針】
1. 膨大な薬品名、病原体名、疾患分類、検査基準値、作用機序などを漏れなく構造化し、丸暗記ではなく「対比・系統」で理解できるように再編成してください。
2. 内容に応じて「概要ボックス（.feature-box）」「要点ブロック（.point-blocks / .point-block）」「比較表（table.cbt-table）」を最も見やすいバランスで組み合わせてください（表が適する箇所は表、機序や原則は箇条書きブロック）。
3. 試験で問われる重要語句・薬品名・受容体・基準値・病態名には、1枚のカードあたり 8〜16 個程度のダイナミックClozeタグを埋め込んでください。
   - Clozeタグの厳格な書式（カードごとに 1. から連番）:
     <span class="cloze-container"><span class="cloze-num">1.</span><span class="cloze-hint">隠す語句</span></span>
4. 各カードの `body_html` は以下のHTML構造のみを出力してください（`<style>`タグは不要です）:
<div class="meta-header">{deck_title}（Chapter X / {target_count}）</div>
<div class="question-box">X. チャプターの主題タイトル</div>
<div class="feature-box"><b>核心・定義：</b>全体像や診断基準の要約（Cloze含む）</div>
<div class="point-blocks">
  <div class="point-block">
    <div class="point-title">① セクション見出し</div>
    <div class="point-body">
      <ul>
        <li><b>項目名：</b>具体的な解説と<span class="cloze-container"><span class="cloze-num">1.</span><span class="cloze-hint">重要語句</span></span></li>
      </ul>
    </div>
  </div>
</div>
<table class="cbt-table">
  <thead><tr><th>分類・系統</th><th>代表薬・病原体</th><th>作用機序・特徴</th><th>臨床上の注意点</th></tr></thead>
  <tbody>
    <tr><td><b>系統名</b></td><td><span class="cloze-container"><span class="cloze-num">2.</span><span class="cloze-hint">薬品名</span></span></td><td>機序</td><td>注意点</td></tr>
  </tbody>
</table>"""

WEB_LLM_COPY_PROMPT = r"""以下の講義資料（または貼り付けたスライド・テキスト・既存HTML）をもとに、試験直前に高速巡回できる「ダイナミックCloze要約カード」のHTMLを作成・修正してください。

【厳守するHTML・Cloze書式ルール】
1. `<style>` タグは含めず、以下のクラス構造で出力すること（CSSはAnkiテンプレート側に内蔵済み）。
2. 重要語句（薬品名、病原体、受容体、セカンドメッセンジャー、基準値、鑑別点）は以下のダイナミックClozeタグで囲み、1. から連番を振ること：
   <span class="cloze-container"><span class="cloze-num">1.</span><span class="cloze-hint">正解語句</span></span>
3. 内容に応じて `.feature-box`（全体像・定義）、`.point-blocks`（箇条書き整理）、`<table class="cbt-table">`（分類・比較表）を使い分けること。

【HTMLテンプレート】
<div class="meta-header">科目・講義名（Chapter 1 / N）</div>
<div class="question-box">1. テーマ・章タイトル</div>

<div class="feature-box">
  <b>要点の核心：</b>ここに定義や診断基準、病態の全体像を記述（例：<span class="cloze-container"><span class="cloze-num">1.</span><span class="cloze-hint">重要語句</span></span>）。
</div>

<div class="point-blocks">
  <div class="point-block">
    <div class="point-title">① サブテーマ名</div>
    <div class="point-body">
      <ul>
        <li><b>項目A：</b>解説文と <span class="cloze-container"><span class="cloze-num">2.</span><span class="cloze-hint">重要語句</span></span></li>
      </ul>
    </div>
  </div>
</div>

<table class="cbt-table">
  <thead>
    <tr>
      <th>治療系統・分類</th>
      <th>代表薬・病原体</th>
      <th>作用機序・病態</th>
      <th>適応・副作用・注意点</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><b>系統A</b></td>
      <td><span class="cloze-container"><span class="cloze-num">3.</span><span class="cloze-hint">代表薬名</span></span></td>
      <td>機序の説明</td>
      <td>注意点</td>
    </tr>
  </tbody>
</table>"""


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
    """既存のスライドモデル（ID・フィールド完全不変）"""
    return genanki.Model(
        CANONICAL_MODEL_ID,
        CANONICAL_MODEL_NAME,
        fields=[{'name': 'Front'}, {'name': 'Back'}],
        templates=[{'name': 'Card 1', 'qfmt': '{{Front}}', 'afmt': '{{Front}}<hr id="answer">{{Back}}'}],
        css=CARD_CSS
    )

def get_dynamic_cloze_model():
    """新設の要約圧縮ダイナミックCloze専用モデル（タップ開閉JS・裏面全開表示対応）"""
    qfmt = """
<div class="card" id="dc-front-root">
  <div style="display:flex; justify-content:flex-end; margin-bottom:4px;">
    <button type="button" onclick="const el=document.getElementById('dc-front-root'); el.classList.toggle('back-view');" style="font-size:11px; padding:2px 8px; border:1px solid #cbd5e1; border-radius:4px; background:#fff; color:#475569; cursor:pointer;">👁️ 全表示切替</button>
  </div>
  {{BodyHTML}}
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
  {{BodyHTML}}
</div>
"""
    return genanki.Model(
        DYNAMIC_CLOZE_MODEL_ID,
        DYNAMIC_CLOZE_MODEL_NAME,
        fields=[{'name': 'ChapterTitle'}, {'name': 'BodyHTML'}],
        templates=[{'name': 'Dynamic Cloze Card', 'qfmt': qfmt, 'afmt': afmt}],
        css=DYNAMIC_CLOZE_CSS
    )


def strip_embedded_style(html_str):
    """手動貼り付けされたHTMLに <style>...</style> や外側の <div class="card"> が含まれていれば剥がす"""
    s = (html_str or "").strip()
    if s.startswith("```html"):
        s = s[7:]
    if s.startswith("```"):
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
    """カード内の <span class="cloze-num">N.</span> を 1. から順番に振り直す"""
    counter = [0]
    def _repl(m):
        counter[0] += 1
        return f'{m.group(1)}{counter[0]}.{m.group(2)}'
    return re.sub(r'(<span\s+class=["\']cloze-num["\'][^>]*>)\s*\d+\.?\s*(</span>)', _repl, html_str, flags=re.IGNORECASE)


# ==============================================================================
# Ankiエクスポート (.txt / TSV) からの2モデル自動振り分け復元ロジック
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

        # A. 新設の要約圧縮ダイナミックClozeモデルの自動判定と振り分け
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
        save_summary_cloze_to_dir(restored_summary_cards, work_dir)

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
# PDF抽出 & Gemini API呼び出し（個別・一括圧縮） & APKG混載ビルド
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

    # 新規抽出時は新規デッキ扱いに戻す
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

def call_gemini_enhance(img_path, api_key, model_id, custom_prompt=None, return_usage=False):
    client = genai.Client(api_key=api_key)
    img = Image.open(img_path)
    prompt = custom_prompt or DEFAULT_PROMPT
    is_svg = "svg-overlay-wrap" in prompt

    if is_svg:
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

    usage = getattr(response, "usage_metadata", None)
    p_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
    o_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
    t_tok = int(getattr(usage, "thoughts_token_count", 0) or 0)
    usage_record = log_api_usage(os.path.basename(img_path), model_id, p_tok, o_tok, t_tok, is_svg=is_svg)

    text = response.text.strip()
    if text.startswith("```html"): text = text[7:]
    if text.startswith("```"): text = text[3:]
    if text.endswith("```"): text = text[:-3]
    clean_html = text.strip()

    if return_usage:
        return clean_html, usage_record
    return clean_html


def call_gemini_summary_cloze(
    img_paths,
    api_key,
    model_id,
    target_count=8,
    deck_title="講義要約",
    extra_instruction="",
    existing_cards=None,
):
    """
    複数枚のスライド画像を一括でGeminiに渡し、指定枚数のダイナミックCloze要約カードをJSON構造化出力で生成する。
    既存のGUIDがあればチャプター番号に応じて引き継ぐ。
    """
    client = genai.Client(api_key=api_key)

    # トークン節約と高速化のため長辺1024pxのサムネイル配列に変換して送信
    pil_imgs = []
    for p in img_paths:
        if os.path.exists(p):
            im = Image.open(p)
            w, h = im.size
            if max(w, h) > 1024:
                r = 1024 / max(w, h)
                im = im.resize((int(w * r), int(h * r)), Image.BILINEAR)
            pil_imgs.append(im)

    prompt_body = SUMMARY_CLOZE_PROMPT_TEMPLATE.format(
        target_count=target_count,
        deck_title=deck_title or "講義要約",
    )
    if extra_instruction and extra_instruction.strip():
        prompt_body += f"\n\n【追加の特記事項・指示】\n{extra_instruction.strip()}"

    schema = types.Schema(
        type=types.Type.ARRAY,
        description=f"全{target_count}枚のダイナミックCloze要約カード配列",
        items=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "chapter_idx": types.Schema(type=types.Type.INTEGER, description="1から始まるチャプター番号"),
                "title": types.Schema(type=types.Type.STRING, description="チャプターの主題タイトル"),
                "body_html": types.Schema(type=types.Type.STRING, description="meta-header, question-box, feature-box, point-blocks, cbt-tableを含むHTML本体（<style>は含めない）"),
            },
            required=["chapter_idx", "title", "body_html"],
        ),
    )

    response = client.models.generate_content(
        model=model_id,
        contents=[*pil_imgs, prompt_body],
        config=types.GenerateContentConfig(
            system_instruction="あなたは医学部専門の教育コンテンツ設計者です。膨大な講義資料を試験対策用に構造化・圧縮し、指定されたJSONスキーマで出力してください。",
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    p_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
    o_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
    t_tok = int(getattr(usage, "thoughts_token_count", 0) or 0)
    usage_record = log_api_usage(f"SUMMARY_BATCH_{len(pil_imgs)}slides", model_id, p_tok, o_tok, t_tok, is_svg=False)

    raw_items = json.loads(response.text)
    existing_by_idx = {}
    if existing_cards:
        for c in existing_cards:
            existing_by_idx[c.get("chapter_idx")] = c

    result_cards = []
    for idx, item in enumerate(raw_items, 1):
        ch_num = int(item.get("chapter_idx") or idx)
        prev = existing_by_idx.get(ch_num, {})
        guid = prev.get("guid") or genanki.guid_for(f"{deck_title}_summary_ch{ch_num:02d}")
        clean_html = renumber_cloze_spans(strip_embedded_style(item.get("body_html", "")))
        result_cards.append({
            "guid": guid,
            "chapter_idx": ch_num,
            "title": item.get("title", f"Chapter {ch_num}"),
            "html": clean_html,
            "is_suspended": prev.get("is_suspended", False),
            "tags": prev.get("tags", []),
        })

    return result_cards, usage_record


def build_apkg(dest_dir, deck_name, group_title, ai_cache=None, output_dir=None, work_dir=None, summary_cards=None):
    """
    1つの .apkg デッキ内に
      ① 元スライドカード (Medical AI Slide Model) をページ順で先に追加
      ② 要約圧縮ダイナミックClozeカード (Medical Dynamic Cloze Model) をチャプター順で後に追加
    して出力する。
    """
    files = sorted(glob.glob(os.path.join(dest_dir, "*.jpg")))
    guid_map_data = load_guid_map_from_dir(work_dir)
    notes_meta = guid_map_data.get("notes", {})
    effective_deck_name = deck_name.strip() or guid_map_data.get("deck_name", "Default_Deck")

    if summary_cards is None:
        summary_cards = load_summary_cloze_from_dir(work_dir)

    if not files and not summary_cards:
        return "", 0, 0, 0

    deck_id = zlib.crc32(effective_deck_name.encode("utf-8"))
    deck = genanki.Deck(deck_id, effective_deck_name)
    media_list = []
    current_timestamp = int(time.time())

    slide_note_count = 0
    summary_note_count = 0

    # ------------------------------------------------------------------
    # ① 元スライドカード (Medical AI Slide Model) を先に追加
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # ② 要約圧縮ダイナミックClozeカード (Medical Dynamic Cloze Model) を後に追加
    # ------------------------------------------------------------------
    if summary_cards:
        dc_model = get_dynamic_cloze_model()
        for idx, card in enumerate(sorted(summary_cards, key=lambda c: c.get("chapter_idx", 999)), 1):
            body_html = strip_embedded_style(card.get("html", ""))
            if not body_html:
                continue
            ch_idx = card.get("chapter_idx") or idx
            ch_title = card.get("title") or f"Chapter {ch_idx}"
            guid = card.get("guid") or genanki.guid_for(f"{effective_deck_name}_summary_ch{ch_idx:02d}")
            tags = card.get("tags", [])

            note = genanki.Note(
                model=dc_model,
                fields=[ch_title, body_html],
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