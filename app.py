import streamlit as st
import os
import glob
import json
import subprocess
from pipeline_util import extract_pdf_pages, call_gemini_enhance, build_apkg, get_poppler_path
from dotenv import load_dotenv
import shutil

# Popplerが実行可能かチェック
if not shutil.which("pdftoppm") and get_poppler_path() is None:
    st.error("""
    ⚠️ **Poppler が見つかりません**
    PDFの画像変換を行うには Poppler のインストールが必要です。
    - **macOS**: ターミナルで `brew install poppler` を実行してください。
    - **Windows**: Poppler をダウンロードし、環境変数 PATH に追加してください。
    """)


load_dotenv()
CONFIG_FILE = "lecture_presets.json"
CACHE_FILE = "exported_jpgs/ai_notes_cache.json"
DEST_DIR = "exported_jpgs"

# 最新Flashモデル料金設定 (USD / 1M tokens)
PRICING_MODELS = {
    "Gemini 2.5 Flash-Lite (最安)": {"id": "gemini-2.5-flash-lite", "in": 0.10, "out": 0.40},
    "Gemini 3.5 Flash-Lite (高コスパ)": {"id": "gemini-3.5-flash-lite", "in": 0.30, "out": 2.50},
    "Gemini 3.6 Flash (バランス推奨)": {"id": "gemini-3.6-flash",      "in": 0.75, "out": 3.75},
    "Gemini 3.8 Flash (高推論)":       {"id": "gemini-3.8-flash",      "in": 0.75, "out": 3.75},
}
USD_JPY = 155.0

def get_presets():
    return json.load(open(CONFIG_FILE, "r", encoding="utf-8")) if os.path.exists(CONFIG_FILE) else {}

def save_presets(data):
    json.dump(data, open(CONFIG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def get_ai_cache():
    return json.load(open(CACHE_FILE, "r", encoding="utf-8")) if os.path.exists(CACHE_FILE) else {}

def save_ai_cache(data):
    os.makedirs(DEST_DIR, exist_ok=True)
    json.dump(data, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

st.set_page_config(page_title="PDF to Anki + AI Enhancer", layout="wide")
st.title("医学部講義PDF ➔ Ankiカード & AI強化 生成システム")

# --- サイドバー: 講義設定と履歴 ---
presets = get_presets()
preset_names = ["(新規入力)"] + list(presets.keys())
selected_p = st.sidebar.selectbox("📂 講義プリセット呼び出し", preset_names)
cur_data = presets.get(selected_p, {}) if selected_p != "(新規入力)" else {}

with st.sidebar:
    st.header("講義・デッキ設定")
    deck_name = st.text_input("Deck名 (階層可)", value=cur_data.get("deck_name", "病理学::12 環境と発癌"))[cite: 1]
    group_title = st.text_input("グループタイトル", value=cur_data.get("group_title", "環境と発癌"))[cite: 1]
    pdf_pass = st.text_input("PDFパスワード (空白可)", value=cur_data.get("password", ""), type="password")[cite: 1]
    base_folder = st.text_input("講義フォルダ", value=cur_data.get("base_folder", ""))
    
    if st.button("現在の設定をプリセット保存"):
        presets[deck_name] = {
            "deck_name": deck_name, "group_title": group_title, 
            "password": pdf_pass, "base_folder": base_folder
        }
        save_presets(presets)
        st.success("プリセットを保存しました")

    st.markdown("---")
    api_key = st.text_input("Gemini API Key", type="password", value=os.environ.get("GEMINI_API_KEY", ""))

# --- メインエリアのタブ構成 ---
tab_extract, tab_ai_batch, tab_ai_inspect, tab_build = st.tabs([
    "1. PDF抽出・Cloze準備", 
    "2. 💰 AI解説の一括追加 (試算＆承認)", 
    "3. 🔍 個別確認・カスタムプロンプト", 
    "4. 📦 Ankiパッケージ出力"
])

# タブ 1: 抽出
with tab_extract:
    st.subheader("PDFファイルの指定とJPEG書き出し")
    mode = st.radio("Cloze準備の形式", ["事前Cloze済み (Masked PDFあり)", "事後Cloze作成 (Unmaskedのみから生成)"])
    
    unmasked_pdf = st.text_input("講義資料PDF (Unmasked) パス", value=os.path.join(base_folder, "講義.pdf") if base_folder else "")
    masked_pdf = None
    if mode == "事前Cloze済み (Masked PDFあり)":
        masked_pdf = st.text_input("マスク済みPDF (Masked) パス", value=os.path.join(base_folder, "講義 masked.pdf") if base_folder else "")

    pages_str = st.text_input("対象ページ (例: 1-10, 14, 25 または all)", value="all")

    col_btn1, col_btn2 = st.columns([1, 1])
    with col_btn1:
        if st.button("🖼️ JPEG画像を抽出・生成", type="primary"):
            if not os.path.exists(unmasked_pdf):
                st.error("Unmasked PDFが見つかりません。")
            else:
                with st.spinner("PDFレンダリング中..."):
                    ts, cnt = extract_pdf_pages(unmasked_pdf, masked_pdf, pages_str, pdf_pass)
                    st.success(f"完了: {cnt} ページ分の画像（a/bペア）を exported_jpgs に書き出しました。")
    
    with col_btn2:
        if st.button("📂 exported_jpgs フォルダをFinderで開く"):
            subprocess.run(["open", os.path.abspath(DEST_DIR)])

# タブ 2: AI一括強化 (試算・モデル選択・承認)
with tab_ai_batch:
    st.subheader("Gemini Flash による一括スライドAI強化")
    b_images = sorted(glob.glob(os.path.join(DEST_DIR, "*b.jpg")))
    ai_cache = get_ai_cache()
    unprocessed = [f for f in b_images if os.path.basename(f) not in ai_cache]

    st.write(f"検出されたBack画像: **{len(b_images)} 枚**（うち未処理: **{len(unprocessed)} 枚**）")

    if unprocessed:
        # トークン試算（長辺1600px制限: 入力約700、出力HTML約1200）
        in_tokens = len(unprocessed) * 700
        out_tokens = len(unprocessed) * 1200

        st.markdown("#### モデル別 費用試算（未処理枚数分）")
        calc_rows = []
        for name, spec in PRICING_MODELS.items():
            cost_usd = (in_tokens / 1_000_000 * spec["in"]) + (out_tokens / 1_000_000 * spec["out"])
            cost_jpy = cost_usd * USD_JPY
            calc_rows.append({
                "モデル": name,
                "全体費用 (JPY)": f"約 {cost_jpy:.2f} 円",
                "1枚あたり": f"{(cost_jpy/len(unprocessed)):.3f} 円",
                "USD換算": f"${cost_usd:.4f}"
            })
        st.table(calc_rows)

        sel_model_name = st.selectbox("使用するモデル", list(PRICING_MODELS.keys()), index=2)
        model_id = PRICING_MODELS[sel_model_name]["id"]

        is_approved = st.checkbox("上記費用の試算を確認し、一括実行を承認します", value=False)
        if st.button("🚀 承認して一括AI解説を生成", type="primary", disabled=not (is_approved and api_key)):
            pbar = st.progress(0)
            status_box = st.empty()
            
            for idx, img_path in enumerate(unprocessed):
                fn = os.path.basename(img_path)
                status_box.text(f"解説作成中 ({idx+1}/{len(unprocessed)}): {fn}")
                try:
                    html_text = call_gemini_enhance(img_path, api_key, model_id)
                    ai_cache[fn] = html_text
                    save_ai_cache(ai_cache)
                except Exception as e:
                    st.error(f"{fn} でAPIエラー発生: {e}")
                    break
                pbar.progress((idx + 1) / len(unprocessed))
            
            status_box.success("一括生成が完了しました！")
            st.rerun()
    else:
        st.success("すべてのスライドのAI解説が完了済みです。")

# タブ 3: 個別確認・カスタム指示
with tab_ai_inspect:
    st.subheader("個別スライドの確認・プロンプト再生成")
    if b_images:
        target_img = st.selectbox("プレビューするスライドを選択", b_images, format_func=lambda x: os.path.basename(x))
        fn = os.path.basename(target_img)

        col_v1, col_v2 = st.columns([1, 1])
        with col_v1:
            st.image(target_img, caption=fn, use_container_width=True)
        with col_v2:
            custom_prompt = st.text_area("個別追加指示（例: 『右側の電顕写真と組織像の対比を詳しく解説して』）")
            sel_inspect_model = st.selectbox("使用モデル（個別）", [m["id"] for m in PRICING_MODELS.values()], index=2)
            
            if st.button("このスライドのみ再生成", disabled=not api_key):
                with st.spinner("AI解説を更新中..."):
                    new_html = call_gemini_enhance(target_img, api_key, sel_inspect_model, custom_prompt)
                    ai_cache[fn] = new_html
                    save_ai_cache(ai_cache)
                    st.success("更新しました。")
                    st.rerun()

        st.markdown("#### 現在のBack面AI解説プレビュー")
        if fn in ai_cache:
            st.components.v1.html(f"<div class='ai-container'>{ai_cache[fn]}</div>", height=450, scrolling=True)
        else:
            st.info("このスライドにはまだAI解説がありません。")

# タブ 4: Ankiパッケージ出力・クリーンアップ
with tab_build:
    st.subheader("Ankiデッキパッケージ (.apkg) 出力")
    st.write(f"出力先デッキ名: **{deck_name}**")
    
    col_out1, col_out2 = st.columns([1, 1])
    with col_out1:
        if st.button("📦 .apkg パッケージを作成", type="primary"):
            with st.spinner("パッケージング中..."):
                apkg_name, count = build_apkg(DEST_DIR, deck_name, group_title, get_ai_cache())
                st.success(f"完成: {count} 枚のカードを含む '{apkg_name}' を書き出しました！")
                st.info("※決定論的GUIDにより、Ankiに再インポートしても学習進捗を維持したまま上書きされます。")

    with col_out2:
        if st.button("🗑️ 中間JPEG画像フォルダを全削除 (クリーンアップ)"):
            all_jpgs = glob.glob(os.path.join(DEST_DIR, "*.jpg"))
            for f in all_jpgs:
                os.remove(f)
            st.success(f"{len(all_jpgs)} 枚のJPEGを削除しました。")