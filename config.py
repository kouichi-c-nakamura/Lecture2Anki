import os

LAST_WORK_DIR_FILE = os.path.expanduser("~/.last_work_dir")
LEGACY_PRESETS_FILE = "lecture_presets.json"
FOLDER_CONFIG_FILENAME = "lecture_config.json"
DEST_DIR = "exported_jpgs"
PROMPTS_FILE = "custom_prompts.json"
API_USAGE_LOG_FILE = os.path.join(DEST_DIR, "api_usage_log.jsonl")
MAX_IMAGE_LENGTH = 1600

# 1. 既存のスライドモデル（ID・フィールド構成は完全不変）
CANONICAL_MODEL_ID = 851727058
CANONICAL_MODEL_NAME = "Medical AI Slide Model"

# 2. 新設の要約圧縮ダイナミックClozeモデル（別IDで完全分離）
DYNAMIC_CLOZE_MODEL_ID = 851727059
DYNAMIC_CLOZE_MODEL_NAME = "Medical Dynamic Cloze Model"

PRICING_MODELS = {
    "Gemini 3.6 Flash (バランス標準)":       {"id": "gemini-3.6-flash",      "in": 0.75, "out": 3.75},
    "Gemini 3.5 Flash-Lite (最安推奨)":     {"id": "gemini-3.5-flash-lite", "in": 0.30, "out": 2.50},
    "Gemini 3.8 Flash (高推論Flash)":       {"id": "gemini-3.8-flash",      "in": 0.75, "out": 3.75},
    "Gemini 4 Argon (〜11/30 特割)":        {"id": "gemini-4-argon",        "in": 2.00, "out": 10.00},
    "Gemini 4 Argon (12/1〜 通常)":         {"id": "gemini-4-argon",        "in": 4.00, "out": 20.00},
}

MODEL_PRICING_PER_1M = {
    "gemini-3.6-flash":      {"in": 0.75, "out": 3.75},
    "gemini-3.5-flash-lite": {"in": 0.30, "out": 2.50},
    "gemini-3.8-flash":      {"in": 0.75, "out": 3.75},
    "gemini-4-argon":        {"in": 2.00, "out": 10.00},
}

USD_JPY = 155.0

# ==============================================================================
# 個別スライド解説用プロンプト
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
   - インライン数式: \(...\)
   - ディスプレイ数式（別行立て）: [ ... ]"""

DEFAULT_PROMPT = f"""この講義スライド画像を分析し、医学生の復習・国試・定期試験対策として直感的に理解できる「最高品質の補足解説」をHTML形式で作成してください。

{OUTPUT_REQUIREMENTS}"""

SVG_OVERLAY_REQUIREMENTS = r"""【SVGオーバーレイ図示の要件（最上部に配置）】
0. スライド画像のSVGオーバーレイ図示:
   画像内の重要な解剖学的指標、病変部位、画像所見、病理組織学的特徴を視覚的にポインティングするため、解説の冒頭（<div class="ai-title">の前）に以下の構造でSVGオーバーレイラッパーを出力すること。
   - 画像のアスペクト比（16:9なら viewBox="0 0 1000 562"、4:3なら viewBox="0 0 1000 750"）に合わせて座標を計算し、対象部位を <ellipse>, <circle>, <rect>, <line> 等で正確に囲み、引き出し線と <text> で [1], [2], [3]... と番号付きの簡潔なラベルを付けること。
   - 視認性を高めるため、部位ごとに鮮やかな色（#ffea00, #00e5ff, #ff6e40, #ff4081 等）を使い分け、文字には黒縁取り（paint-order: stroke; stroke: #000000; stroke-width: 4.5px;）を適用すること。
   - 右上角には Toggle とだけ記した表示切替ボタンを配置し、スライドを隠さないよう「ラッパーにマウスホバーした時だけボタンが浮か上がる（通常時 opacity: 0; ➔ ホバー時 opacity: 0.85;）」仕様とすること。
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
# 赤線マーキング済みPDF ➔ ダイナミックCloze変換用プロンプト
# ==============================================================================
REDLINE_PDF_TO_CLOZE_PROMPT = r"""添付したPDF（全 {total_pages} ページ）には、暗記すべき重要語句に**手書きの赤線（アンダーライン、取り消し線風の上線、または赤いマーカーハイライト）**が引かれています。
各ページを1チャプター（全 {total_pages} 枚）として、**赤線が引かれている部分だけを穴埋め（Cloze）にした学習用HTML**に変換してください。

【絶対厳守ルール】
1. **文章・表・数値の改変・省略を一切禁止（完全転写）**:
   - PDFに記載されている見出し、本文、箇条書き、表（table）のセル内容、数値、薬品名、括弧内の補足は**1文字も削らず・要約せず**そのまま忠実にテキスト化すること。
2. **赤線が引かれた箇所だけを `[[...]]` で囲む（自己判断での穴埋め禁止）**:
   - **赤線（または赤マーカー）が引かれている語句・数値・項目名だけ**を二重角括弧 `[[語句]]` で囲むこと。
   - 赤線が引かれていない箇所を勝手に `[[...]]` にすることは絶対に禁止。
   - 1本の赤線が「1. 発赤（Flush）」のように番号や括弧を含む語句全体に引かれている場合は `1. [[発赤（Flush）]]` のように語句単位で囲み、複数の単語に別々の赤線が引かれている場合はそれぞれ個別に `[[語句A]]`、`[[語句B]]` と囲むこと。
3. **数式・上付き/下付き文字の表記**:
   - `Ca2+`、`PGI2`、`ETA` などは `Ca<sup>2+</sup>`、`PGI<sub>2</sub>`、`ET<sub>A</sub>` のようにHTMLタグで表現すること。
4. **出力フォーマット（`<style>` や `<html>` タグは含めない）**:
   各ページ（Chapter 1 〜 {total_pages}）ごとに以下のクラス構造のみを出力してください。

<div class="meta-header">{deck_title}（Chapter 1 / {total_pages}）</div>
<div class="question-box">1. ページの主題タイトル</div>

<div class="feature-box">
  <b>見出し：</b>本文テキスト（赤線箇所は `[[語句]]` で囲む）
</div>

<div class="point-blocks">
  <div class="point-block">
    <div class="point-title">① セクション見出し</div>
    <div class="point-body">
      <ul>
        <li><b>項目名：</b>解説テキスト（赤線箇所は `[[語句]]` で囲む）</li>
      </ul>
    </div>
  </div>
</div>

<table class="cbt-table">
  <thead>
    <tr><th>列1</th><th>列2</th><th>列3</th><th>列4</th></tr>
  </thead>
  <tbody>
    <tr><td>セル1</td><td>[[赤線が引かれたセル2]]</td><td>セル3</td><td>セル4</td></tr>
  </tbody>
</table>"""

WEB_LLM_REDLINE_COPY_PROMPT = r"""添付したPDF（全Nページ）には、暗記すべき重要語句に**手書きの赤線（アンダーライン、取り消し線風の上線、または赤いマーカーハイライト）**が引かれています。
各ページを1チャプター（全N枚）として、**赤線が引かれている部分だけを穴埋め（Cloze）にした学習用HTML**に変換してください。

【絶対厳守ルール】
1. **文章・表・数値の改変・省略を一切禁止（完全転写）**:
   - PDFに記載されている見出し、本文、箇条書き、表（table）のセル内容、数値、薬品名、括弧内の補足は**1文字も削らず・要約せず**そのまま忠実にテキスト化すること。
2. **赤線が引かれた箇所だけを `[[...]]` で囲む（自己判断での穴埋め禁止）**:
   - **赤線（または赤マーカー）が引かれている語句・数値・項目名だけ**を二重角括弧 `[[語句]]` で囲むこと。
   - 赤線が引かれていない箇所を勝手に `[[...]]` にすることは絶対に禁止。
   - 1本の赤線が「1. 発赤（Flush）」のように番号や括弧を含む語句全体に引かれている場合は `1. [[発赤（Flush）]]` のように語句単位で囲み、複数の単語に別々の赤線が引かれている場合はそれぞれ個別に `[[語句A]]`、`[[語句B]]` と囲むこと。
3. **数式・上付き/下付き文字の表記**:
   - `Ca2+`、`PGI2`、`ETA` などは `Ca<sup>2+</sup>`、`PGI<sub>2</sub>`、`ET<sub>A</sub>` のようにHTMLタグで表現すること。
4. **出力フォーマット（全チャプター分を1つのHTMLコードブロック内に連続出力）**:
   - `<style>` や `<html>` タグは含めず、各ページ（Chapter 1 〜 N）ごとに以下のクラス構造のみを出力してください。

<div class="meta-header">講義タイトル（Chapter 1 / N）</div>
<div class="question-box">1. ページの主題タイトル</div>

<div class="feature-box">
  <b>見出し：</b>本文テキスト（赤線箇所は `[[語句]]` で囲む）
</div>

<div class="point-blocks">
  <div class="point-block">
    <div class="point-title">① セクション見出し</div>
    <div class="point-body">
      <ul>
        <li><b>項目名：</b>解説テキスト（赤線箇所は `[[語句]]` で囲む）</li>
      </ul>
    </div>
  </div>
</div>

<table class="cbt-table">
  <thead>
    <tr>
      <th>列1</th>
      <th>列2</th>
      <th>列3</th>
      <th>列4</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>セル1</td>
      <td>[[赤線が引かれたセル2]]</td>
      <td>セル3</td>
      <td>セル4</td>
    </tr>
  </tbody>
</table>"""


# ==============================================================================
# CSS定義
# ==============================================================================
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