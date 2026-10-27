import streamlit as st
import os
import glob
import json
import subprocess
import shutil
from PIL import Image
from dotenv import load_dotenv
from pipeline_util import (
    extract_pdf_pages,
    call_gemini_enhance,
    build_apkg,
    get_poppler_path,
    DEFAULT_PROMPT,
)

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
PROMPTS_FILE = "custom_prompts.json"

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

# --- カスタムプロンプト保存・最新5件キープ用ヘルパー ---
def get_prompt_store():
    if os.path.exists(PROMPTS_FILE):
        try:
            return json.load(open(PROMPTS_FILE, "r", encoding="utf-8"))
        except Exception:
            pass
    return {"recent": [], "saved": {}}

def save_prompt_store(store):
    json.dump(store, open(PROMPTS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def keep_recent_prompt(p_text):
    p_clean = p_text.strip()
    if not p_clean or p_clean == DEFAULT_PROMPT:
        return
    store = get_prompt_store()
    recent = [p for p in store.get("recent", []) if p != p_clean]
    recent.insert(0, p_clean)
    store["recent"] = recent[:5]  # 最新5個を自動でkeep
    save_prompt_store(store)

# --- ファイル / フォルダ選択ダイアログ (macOS Finder) ---
def choose_folder_dialog(title="フォルダを選択"):
    script = f'POSIX path of (choose folder with prompt "{title}")'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return res.stdout.strip().rstrip("/") if res.returncode == 0 else None

def choose_file_dialog(title="ファイルを選択"):
    script = f'POSIX path of (choose file with prompt "{title}")'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None

@st.cache_data(show_spinner=False)
def get_low_res_thumb(img_path, width=180):
    img = Image.open(img_path)
    w, h = img.size
    return img.resize((width, int(h * (width / w))), Image.BILINEAR)


st.set_page_config(page_title="PDF to Anki + AI Enhancer", layout="wide")
st.title("医学部講義PDF ➔ Ankiカード & AI強化 生成システム")

# --- サイドバー: 講義設定と履歴 ---
presets = get_presets()
preset_names = ["(新規入力)"] + list(presets.keys())
selected_p = st.sidebar.selectbox("📂 講義プリセット呼び出し", preset_names)
cur_data = presets.get(selected_p, {}) if selected_p != "(新規入力)" else {}

# プリセット切り替え時のパス同期
if "last_selected_preset" not in st.session_state or st.session_state.last_selected_preset != selected_p:
    st.session_state.last_selected_preset = selected_p
    bf = cur_data.get("base_folder", "")
    st.session_state.base_folder_val = bf
    st.session_state.unmasked_pdf_val = os.path.join(bf, "講義.pdf") if bf else ""
    st.session_state.masked_pdf_val = os.path.join(bf, "講義 masked.pdf") if bf else ""

if "slide_idx" not in st.session_state:
    st.session_state.slide_idx = 0
if "custom_prompt_val" not in st.session_state:
    st.session_state.custom_prompt_val = DEFAULT_PROMPT

with st.sidebar:
    st.header("講義・デッキ設定")
    deck_name = st.text_input("Deck名 (階層可)", value=cur_data.get("deck_name", "病理学::12 環境と発癌"))
    group_title = st.text_input("グループタイトル", value=cur_data.get("group_title", "環境と発癌"))
    pdf_pass = st.text_input("PDFパスワード (空白可)", value=cur_data.get("password", ""), type="password")
    
    st.markdown("講義フォルダ")
    col_bf1, col_bf2 = st.columns([3, 1])
    with col_bf2:
        if st.button("📂", key="btn_choose_bf", help="フォルダ選択ダイアログを開く"):
            chosen_bf = choose_folder_dialog("講義フォルダを選択してください")
            if chosen_bf:
                st.session_state.base_folder_val = chosen_bf
                st.session_state.unmasked_pdf_val = os.path.join(chosen_bf, "講義.pdf")
                st.session_state.masked_pdf_val = os.path.join(chosen_bf, "講義 masked.pdf")
                st.rerun()
    with col_bf1:
        base_folder = st.text_input("講義フォルダ", value=st.session_state.base_folder_val, label_visibility="collapsed")
        st.session_state.base_folder_val = base_folder
    
    if st.button("現在の設定をプリセット保存"):
        presets[deck_name] = {
            "deck_name": deck_name, "group_title": group_title, 
            "password": pdf_pass, "base_folder": base_folder
        }
        save_presets(presets)
        st.success("プリセットを保存しました")

    st.markdown("---")
    api_key = st.text_input("Gemini API Key", type="password", value=os.environ.get("GEMINI_API_KEY", ""))

# 書き出し済み画像の確認
b_images = sorted(glob.glob(os.path.join(DEST_DIR, "*b.jpg")))
ai_cache = get_ai_cache()

# --- メインエリアのタブ構成 ---
tab_extract, tab_ai_batch, tab_ai_inspect, tab_build = st.tabs([
    "1. PDF抽出・Cloze準備", 
    "2. 💰 AI解説の一括追加 (試算＆承認)", 
    "3. 🔍 個別確認・カスタムプロンプト", 
    "4. 📦 Ankiパッケージ出力"
])

# タブ 1: 抽出 (Skip可能 & ファイル選択ダイアログ対応)
with tab_extract:
    st.subheader("PDFファイルの指定とJPEG書き出し")
    if b_images:
        st.info(f"💡 すでに `{DEST_DIR}` に **{len(b_images)} 枚** のJPGが書き出されています。この工程をスキップしてタブ 2・タブ 3 へ進めます。")

    with st.expander("PDFからJPEGを抽出する（未抽出または再抽出時のみ開く）", expanded=(len(b_images) == 0)):
        mode = st.radio("Cloze準備の形式", ["事前Cloze済み (Masked PDFあり)", "事後Cloze作成 (Unmaskedのみから生成)"])
        
        st.markdown("講義資料PDF (Unmasked) パス")
        col_u1, col_u2 = st.columns([5, 1])
        with col_u2:
            if st.button("📄 選択...", key="btn_choose_unmasked"):
                chosen_u = choose_file_dialog("講義資料PDF (Unmasked) を選択")
                if chosen_u:
                    st.session_state.unmasked_pdf_val = chosen_u
                    st.rerun()
        with col_u1:
            unmasked_pdf = st.text_input("講義資料PDF (Unmasked) パス", value=st.session_state.unmasked_pdf_val, label_visibility="collapsed")
            st.session_state.unmasked_pdf_val = unmasked_pdf

        masked_pdf = None
        if mode == "事前Cloze済み (Masked PDFあり)":
            st.markdown("マスク済みPDF (Masked) パス")
            col_m1, col_m2 = st.columns([5, 1])
            with col_m2:
                if st.button("📄 選択...", key="btn_choose_masked"):
                    chosen_m = choose_file_dialog("マスク済みPDF (Masked) を選択")
                    if chosen_m:
                        st.session_state.masked_pdf_val = chosen_m
                        st.rerun()
            with col_m1:
                masked_pdf = st.text_input("マスク済みPDF (Masked) パス", value=st.session_state.masked_pdf_val, label_visibility="collapsed")
                st.session_state.masked_pdf_val = masked_pdf

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
                        st.rerun()
        
        with col_btn2:
            if st.button("📂 exported_jpgs フォルダをFinderで開く"):
                subprocess.run(["open", os.path.abspath(DEST_DIR)])

# タブ 2: AI一括強化 (試算・モデル選択・承認) ※元コード完全維持
with tab_ai_batch:
    st.subheader("Gemini Flash による一括スライドAI強化")
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

# タブ 3: 個別確認・カスタム指示 (サムネイル選択・←→移動・プロンプト管理)
with tab_ai_inspect:
    st.subheader("個別スライドの確認・プロンプト再生成")
    if b_images:
        total_slides = len(b_images)
        st.session_state.slide_idx = max(0, min(st.session_state.slide_idx, total_slides - 1))

        # 1. 低解像度サムネイル一覧でスライドを選択
        with st.expander("🖼️ 低解像度サムネイルでスライドを選択", expanded=False):
            cols_per_row = 6
            for r_start in range(0, total_slides, cols_per_row):
                cols = st.columns(cols_per_row)
                for c_idx in range(cols_per_row):
                    idx = r_start + c_idx
                    if idx < total_slides:
                        p_img = b_images[idx]
                        p_fn = os.path.basename(p_img)
                        mark = "✅" if p_fn in ai_cache else "⚪"
                        is_selected = (idx == st.session_state.slide_idx)
                        with cols[c_idx]:
                            st.image(get_low_res_thumb(p_img), use_container_width=True)
                            if st.button(f"{idx+1} {mark}", key=f"thumb_{idx}", type="primary" if is_selected else "secondary", use_container_width=True):
                                st.session_state.slide_idx = idx
                                st.rerun()

        # 2. ← → ボタンで移動
        nav_1, nav_2, nav_3 = st.columns([1, 3, 1])
        with nav_1:
            if st.button("← 前のスライド", disabled=(st.session_state.slide_idx == 0), use_container_width=True):
                st.session_state.slide_idx -= 1
                st.rerun()
        with nav_3:
            if st.button("次のスライド →", disabled=(st.session_state.slide_idx >= total_slides - 1), use_container_width=True):
                st.session_state.slide_idx += 1
                st.rerun()
        with nav_2:
            target_img = b_images[st.session_state.slide_idx]
            fn = os.path.basename(target_img)
            st.markdown(f"<div style='text-align:center; padding-top:6px; font-weight:bold;'>{st.session_state.slide_idx + 1} / {total_slides} : {fn}</div>", unsafe_allow_html=True)

        col_v1, col_v2 = st.columns([1, 1])
        with col_v1:
            st.image(target_img, caption=fn, use_container_width=True)

        with col_v2:
            # 3. プロンプト管理（Default呼び出しボタン・最新5件・名前付き保存・text書き出し）
            p_store = get_prompt_store()
            menu_items = ["(保存済み / 最新5件から呼び出し)"]
            menu_map = {}

            for k, v in p_store.get("saved", {}).items():
                label = f"💾 {k}"
                menu_items.append(label)
                menu_map[label] = v
            for i, r_p in enumerate(p_store.get("recent", []), 1):
                short = r_p.replace("\n", " ")[:25] + ("..." if len(r_p) > 25 else "")
                label = f"🕒 最新{i}: {short}"
                menu_items.append(label)
                menu_map[label] = r_p

            pm_col1, pm_col2 = st.columns([2.5, 1])
            with pm_col1:
                selected_menu = st.selectbox("プロンプト呼び出しメニュー", menu_items, label_visibility="collapsed")
                if selected_menu != "(保存済み / 最新5件から呼び出し)":
                    st.session_state.custom_prompt_val = menu_map[selected_menu]
                    st.rerun()
            with pm_col2:
                if st.button("🔄 Defaultに戻す", use_container_width=True):
                    st.session_state.custom_prompt_val = DEFAULT_PROMPT
                    st.rerun()

            custom_prompt = st.text_area(
                "プロンプト（初期値: 一括追加と同じDefaultプロンプト）",
                value=st.session_state.custom_prompt_val,
                height=120,
            )
            st.session_state.custom_prompt_val = custom_prompt

            # 名前をつけて保存 & text書き出し
            sv_col1, sv_col2, sv_col3 = st.columns([2, 1, 1])
            with sv_col1:
                prompt_name = st.text_input("プロンプト保存名", placeholder="名前をつけて保存...", label_visibility="collapsed")
            with sv_col2:
                if st.button("💾 保存", use_container_width=True):
                    if prompt_name.strip():
                        p_store.setdefault("saved", {})[prompt_name.strip()] = custom_prompt
                        save_prompt_store(p_store)
                        st.success(f"'{prompt_name}' を保存しました")
                        st.rerun()
            with sv_col3:
                export_text = f"=== Current Prompt ===\n{custom_prompt}\n\n=== Saved Prompts ===\n"
                for k, v in p_store.get("saved", {}).items():
                    export_text += f"\n[{k}]\n{v}\n"
                export_text += "\n=== Recent 5 Prompts ===\n"
                for i, v in enumerate(p_store.get("recent", []), 1):
                    export_text += f"\n[Recent {i}]\n{v}\n"
                st.download_button("📄 text書き出し", data=export_text, file_name="custom_prompts.txt", mime="text/plain", use_container_width=True)

            sel_inspect_model = st.selectbox("使用モデル（個別）", [m["id"] for m in PRICING_MODELS.values()], index=2)
            
            if st.button("このスライドのみ再生成", type="primary", disabled=not api_key):
                keep_recent_prompt(custom_prompt)
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

# タブ 4: Ankiパッケージ出力・クリーンアップ ※元コード完全維持
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