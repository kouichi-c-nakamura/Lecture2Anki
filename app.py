import streamlit as st
import os
import re
import glob
import json
import subprocess
import shutil
import io
import base64
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

LAST_WORK_DIR_FILE = os.path.expanduser("~/.last_work_dir")
LEGACY_PRESETS_FILE = "lecture_presets.json"
FOLDER_CONFIG_FILENAME = "lecture_config.json"
CACHE_FILE = "exported_jpgs/ai_notes_cache.json"
DEST_DIR = "exported_jpgs"
PROMPTS_FILE = "custom_prompts.json"

PRICING_MODELS = {
    "Gemini 3.5 Flash-Lite (最安推奨)":     {"id": "gemini-3.5-flash-lite", "in": 0.30, "out": 2.50},
    "Gemini 3.6 Flash (バランス標準)":       {"id": "gemini-3.6-flash",      "in": 0.75, "out": 3.75},
    "Gemini 3.8 Flash (高推論Flash)":       {"id": "gemini-3.8-flash",      "in": 0.75, "out": 3.75},
    "Gemini 4 Argon (〜11/30 特割)":        {"id": "gemini-4-argon",        "in": 2.00, "out": 10.00},
    "Gemini 4 Argon (12/1〜 通常)":         {"id": "gemini-4-argon",        "in": 4.00, "out": 20.00},
    "Gemini 2.5 Flash (レガシー)":          {"id": "gemini-2.5-flash",      "in": 0.30, "out": 2.50},
}
USD_JPY = 155.0


# --- ~/.last_work_dir による最後の作業フォルダ記憶ヘルパー ---
def get_last_folder():
    if os.path.exists(LAST_WORK_DIR_FILE):
        try:
            with open(LAST_WORK_DIR_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content.startswith("{"):
                bf = json.loads(content).get("last_base_folder", "")
            else:
                bf = content
            if bf and os.path.isdir(bf):
                return bf
        except Exception:
            pass
    if os.path.exists(LEGACY_PRESETS_FILE):
        try:
            presets = json.load(open(LEGACY_PRESETS_FILE, "r", encoding="utf-8"))
            for v in presets.values():
                bf = v.get("base_folder", "")
                if bf and os.path.isdir(bf):
                    return bf
        except Exception:
            pass
    return ""

def save_last_folder(folder_path):
    if not folder_path:
        return
    try:
        with open(LAST_WORK_DIR_FILE, "w", encoding="utf-8") as f:
            f.write(folder_path.strip())
    except Exception:
        pass


# --- Masked PDF パスから Unmasked PDF パスを自動推論するヘルパー ---
def infer_unmasked_from_masked(masked_path):
    """
    与えられた masked_path のファイル名（拡張子除く）から [_\s]+masked$ を除去し、
    同一フォルダ内の対応する unmasked PDF パスを検出して返す。
    """
    if not masked_path or not masked_path.strip():
        return ""
    masked_path = masked_path.strip()
    folder = os.path.dirname(masked_path)
    filename = os.path.basename(masked_path)
    stem, ext = os.path.splitext(filename)

    # 末尾の "_masked" や " masked" を除去 (大文字小文字不問)
    clean_stem = re.sub(r'[_\s]+masked$', '', stem, flags=re.IGNORECASE)
    if clean_stem == stem:
        # アンダースコアや空白なしで末尾が masked の場合のフォールバック
        clean_stem = re.sub(r'masked$', '', stem, flags=re.IGNORECASE).rstrip('_- ')

    candidate = os.path.join(folder, f"{clean_stem}{ext}")
    if os.path.exists(candidate):
        return candidate

    # フォルダ内で大文字小文字の違いを含めて一致するPDFを探す
    if folder and os.path.isdir(folder):
        for f in os.listdir(folder):
            if f.lower() == f"{clean_stem}{ext}".lower():
                return os.path.join(folder, f)

    # まだファイルが存在しない場合でも推論した標準パスを返す
    return candidate


def auto_detect_pdfs(folder_path):
    """フォルダ内のPDFから unmasked / masked を自動推測する"""
    if not folder_path or not os.path.isdir(folder_path):
        return "", ""
    pdfs = sorted(glob.glob(os.path.join(folder_path, "*.pdf")))
    masked = [p for p in pdfs if re.search(r'[_\s]*masked\.pdf$', os.path.basename(p), re.IGNORECASE)]
    unmasked = [p for p in pdfs if p not in masked]
    
    m_path = masked[0] if masked else os.path.join(folder_path, "講義 masked.pdf")
    if masked:
        inferred_u = infer_unmasked_from_masked(m_path)
        u_path = inferred_u if (inferred_u and os.path.exists(inferred_u)) else (unmasked[0] if unmasked else inferred_u)
    else:
        u_path = unmasked[0] if unmasked else os.path.join(folder_path, "講義.pdf")
    return u_path, m_path


def load_folder_config(folder_path):
    """指定フォルダ内の lecture_config.json を読み込む。無ければ旧プリセットや自動検出で補完"""
    if not folder_path:
        return {}
    cfg_path = os.path.join(folder_path, FOLDER_CONFIG_FILENAME)
    if os.path.exists(cfg_path):
        try:
            return json.load(open(cfg_path, "r", encoding="utf-8"))
        except Exception:
            pass

    if os.path.exists(LEGACY_PRESETS_FILE):
        try:
            presets = json.load(open(LEGACY_PRESETS_FILE, "r", encoding="utf-8"))
            for v in presets.values():
                if v.get("base_folder") == folder_path:
                    return v
        except Exception:
            pass

    folder_name = os.path.basename(folder_path.rstrip("/"))
    u_pdf, m_pdf = auto_detect_pdfs(folder_path)
    return {
        "deck_name": folder_name,
        "group_title": folder_name,
        "password": "",
        "base_folder": folder_path,
        "unmasked_pdf": u_pdf,
        "masked_pdf": m_pdf,
    }

def save_folder_config(folder_path, config_data):
    """講義フォルダ直下に lecture_config.json を保存する"""
    if not folder_path or not os.path.isdir(folder_path):
        return False
    cfg_path = os.path.join(folder_path, FOLDER_CONFIG_FILENAME)
    json.dump(config_data, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    save_last_folder(folder_path)
    return True

def apply_folder_to_state(folder_path):
    """フォルダ切り替え時に Session State 一式を更新する"""
    st.session_state.base_folder_val = folder_path
    cfg = load_folder_config(folder_path)
    u_default, m_default = auto_detect_pdfs(folder_path)
    st.session_state.deck_name_val = cfg.get("deck_name", "")
    st.session_state.group_title_val = cfg.get("group_title", "")
    st.session_state.pdf_pass_val = cfg.get("password", "")
    st.session_state.masked_pdf_val = cfg.get("masked_pdf") or m_default
    st.session_state.unmasked_pdf_val = cfg.get("unmasked_pdf") or u_default
    save_last_folder(folder_path)


def get_ai_cache():
    return json.load(open(CACHE_FILE, "r", encoding="utf-8")) if os.path.exists(CACHE_FILE) else {}

def save_ai_cache(data):
    os.makedirs(DEST_DIR, exist_ok=True)
    json.dump(data, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# --- 画像をmacOSクリップボードへコピーするヘルパー ---
def copy_image_to_clipboard_macos(image_path):
    abs_path = os.path.abspath(image_path)
    osa = f'''
    set theFile to POSIX file "{abs_path}"
    set theImage to read theFile as «class PNG »
    set the clipboard to theImage
    '''
    res = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True)
    return res.returncode == 0

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
    store["recent"] = recent[:5]
    save_prompt_store(store)

# --- ファイル / フォルダ選択ダイアログ (macOS Finder: ~/.last_work_dir 起点) ---
def choose_folder_dialog(title="フォルダを選択", default_dir=None):
    target = default_dir or get_last_folder()
    if target and os.path.isdir(target):
        script = f'POSIX path of (choose folder with prompt "{title}" default location (POSIX file "{target}"))'
    else:
        script = f'POSIX path of (choose folder with prompt "{title}")'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return res.stdout.strip().rstrip("/") if res.returncode == 0 else None

def choose_file_dialog(title="ファイルを選択", default_dir=None):
    target = default_dir or get_last_folder()
    if target and os.path.isdir(target):
        script = f'POSIX path of (choose file with prompt "{title}" default location (POSIX file "{target}"))'
    else:
        script = f'POSIX path of (choose file with prompt "{title}")'
    res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None

@st.cache_data(show_spinner=False)
def get_thumb_b64(img_path, width=200):
    """幅200px固定のサムネイルを生成してキャッシュする"""
    img = Image.open(img_path)
    w, h = img.size
    thumb = img.resize((width, int(h * (width / w))), Image.BILINEAR)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


st.set_page_config(page_title="PDF to Anki + AI Enhancer", layout="wide")
st.title("医学部講義PDF ➔ Ankiカード & AI強化 生成システム")

# --- 初回起動時のセッションステート初期化（最後に使ったフォルダの設定を自動ロード） ---
if "base_folder_val" not in st.session_state:
    init_bf = get_last_folder()
    if init_bf:
        apply_folder_to_state(init_bf)
    else:
        st.session_state.base_folder_val = ""
        st.session_state.deck_name_val = ""
        st.session_state.group_title_val = ""
        st.session_state.pdf_pass_val = ""
        st.session_state.unmasked_pdf_val = ""
        st.session_state.masked_pdf_val = ""

def on_base_folder_input_change():
    bf = st.session_state.base_folder_val.strip().rstrip("/")
    if bf and os.path.isdir(bf):
        apply_folder_to_state(bf)

# Masked PDF テキストボックスが直接編集されたときに Unmasked を自動推論するコールバック
def on_masked_input_change():
    m_val = st.session_state.masked_input_widget.strip()
    st.session_state.masked_pdf_val = m_val
    inferred = infer_unmasked_from_masked(m_val)
    if inferred:
        st.session_state.unmasked_pdf_val = inferred

def on_unmasked_input_change():
    st.session_state.unmasked_pdf_val = st.session_state.unmasked_input_widget.strip()

# --- サイドバー: フォルダ起点の設定管理 (Exam2Anki方式) ---
with st.sidebar:
    st.header("講義フォルダ・デッキ設定")
    
    st.markdown("**📂 講義フォルダ (最優先・自動読込)**")
    col_bf1, col_bf2 = st.columns([3.2, 1])
    with col_bf2:
        if st.button("📂 選択", key="btn_choose_bf", help="Finderで講義フォルダを選択", use_container_width=True):
            cur_dir = st.session_state.get("base_folder_val", "")
            start_dir = os.path.dirname(cur_dir) if (cur_dir and os.path.isdir(cur_dir)) else cur_dir
            chosen_bf = choose_folder_dialog("講義フォルダを選択してください", default_dir=start_dir)
            if chosen_bf:
                apply_folder_to_state(chosen_bf)
                st.rerun()
    with col_bf1:
        base_folder = st.text_input(
            "講義フォルダ",
            key="base_folder_val",
            on_change=on_base_folder_input_change,
            placeholder="/path/to/lecture_folder",
            label_visibility="collapsed"
        )

    # フォルダ内の設定ファイル有無をステータス表示
    if base_folder and os.path.isdir(base_folder):
        cfg_file_in_dir = os.path.join(base_folder, FOLDER_CONFIG_FILENAME)
        if os.path.exists(cfg_file_in_dir):
            st.caption(f"✅ フォルダ内設定 (`{FOLDER_CONFIG_FILENAME}`) 読込済")
        else:
            st.caption("🆕 新規フォルダ（設定未保存）")
    elif base_folder:
        st.warning("⚠️ フォルダが存在しません")

    deck_name = st.text_input("Deck名 (階層可)", key="deck_name_val")
    group_title = st.text_input("グループタイトル", key="group_title_val")
    pdf_pass = st.text_input("PDFパスワード (空白可)", key="pdf_pass_val", type="password")
    
    if st.button("💾 このフォルダに設定を保存", type="primary", use_container_width=True):
        bf_target = st.session_state.base_folder_val.strip()
        if not bf_target or not os.path.isdir(bf_target):
            st.error("有効な講義フォルダを先に選択してください。")
        else:
            cfg_data = {
                "deck_name": deck_name,
                "group_title": group_title,
                "password": pdf_pass,
                "base_folder": bf_target,
                "unmasked_pdf": st.session_state.get("unmasked_pdf_val", ""),
                "masked_pdf": st.session_state.get("masked_pdf_val", ""),
            }
            save_folder_config(bf_target, cfg_data)
            st.toast(f"✅ フォルダ内に {FOLDER_CONFIG_FILENAME} を保存しました")
            st.rerun()

    st.markdown("---")
    api_key = st.text_input("Gemini API Key", type="password", value=os.environ.get("GEMINI_API_KEY", ""))

if "slide_idx" not in st.session_state:
    st.session_state.slide_idx = 0
if "custom_prompt_val" not in st.session_state:
    st.session_state.custom_prompt_val = DEFAULT_PROMPT

# 最新のタイムスタンプのバッチのみを自動抽出
all_b_images = sorted(glob.glob(os.path.join(DEST_DIR, "*b.jpg")))
if all_b_images:
    latest_ts = sorted({os.path.basename(f)[:17] for f in all_b_images})[-1]
    b_images = [f for f in all_b_images if os.path.basename(f).startswith(latest_ts)]
else:
    b_images = []

ai_cache = get_ai_cache()

# --- メインエリアのタブ構成 ---
tab_extract, tab_ai_batch, tab_ai_inspect, tab_build = st.tabs([
    "1. PDF指定", 
    "2. 💰 AI解説の一括追加", 
    "3. 🔍 個別AI解説", 
    "4. 📦 Ankiパッケージ出力"
])

# タブ 1: 抽出
with tab_extract:
    st.subheader("PDFファイルの指定とJPEG書き出し")

    mode = st.radio("Cloze準備の形式", ["事前Cloze済み (Masked PDFあり)", "事後Cloze作成 (Unmaskedのみから生成)"])
    
    masked_pdf = None
    unmasked_pdf = None
    is_masked_valid = True

    if mode == "事前Cloze済み (Masked PDFあり)":
        # 1. 上段: Masked PDF パス（主選択）
        st.markdown(
            "1️⃣ **マスク済みPDF (Masked) パス** <span style='color:#ef4444; font-size:12px;'>※これを選択すると下のUnmasked PDFを自動検出します</span>",
            unsafe_allow_html=True
        )
        col_m1, col_m2 = st.columns([5, 1])
        with col_m2:
            if st.button("📄 選択...", key="btn_choose_masked", use_container_width=True):
                chosen_m = choose_file_dialog("マスク済みPDF (Masked) を選択", default_dir=st.session_state.get("base_folder_val"))
                if chosen_m:
                    st.session_state.masked_pdf_val = chosen_m
                    # [_\s]+masked$ を除去して Unmasked PDF を自動セット
                    inferred_u = infer_unmasked_from_masked(chosen_m)
                    if inferred_u:
                        st.session_state.unmasked_pdf_val = inferred_u
                    # 講義フォルダが未設定なら親フォルダをセット
                    parent_dir = os.path.dirname(chosen_m)
                    if parent_dir and not st.session_state.get("base_folder_val"):
                        st.session_state.base_folder_val = parent_dir
                    st.rerun()
        with col_m1:
            st.session_state.masked_input_widget = st.session_state.masked_pdf_val
            masked_pdf = st.text_input(
                "マスク済みPDF (Masked) パス",
                key="masked_input_widget",
                on_change=on_masked_input_change,
                label_visibility="collapsed"
            )
            st.session_state.masked_pdf_val = masked_pdf

        # Masked PDF バリデーション
        masked_fn = os.path.basename(masked_pdf.strip()) if masked_pdf else ""
        if not masked_pdf or not masked_pdf.strip():
            st.warning("⚠️ 事前Clozeモードでは Masked PDF の指定が必須です。")
            is_masked_valid = False
        elif not os.path.exists(masked_pdf):
            st.error(f"⚠️ 指定された Masked PDF が見つかりません: `{masked_pdf}`")
            is_masked_valid = False
        elif not re.search(r'[_\s]*masked\.pdf$', masked_fn, re.IGNORECASE):
            st.error(f"⚠️ **無効なファイル名**: 事前Cloze用のPDF名には末尾に `masked` を含める必要があります (指定名: `{masked_fn}`)")
            is_masked_valid = False

        # もし unmasked_pdf_val が空、または存在しない場合は現在の masked_pdf から自動補完を試みる
        if is_masked_valid and (not st.session_state.unmasked_pdf_val or not os.path.exists(st.session_state.unmasked_pdf_val)):
            auto_u = infer_unmasked_from_masked(masked_pdf)
            if auto_u and os.path.exists(auto_u):
                st.session_state.unmasked_pdf_val = auto_u

        # 2. 下段: Unmasked PDF パス（自動検出 ＋ Manual Override 可能）
        u_exists = bool(st.session_state.unmasked_pdf_val and os.path.exists(st.session_state.unmasked_pdf_val))
        badge_html = (
            "<span style='color:#16a34a; font-size:12px;'>✅ 自動検出済 (手動変更も可)</span>"
            if u_exists else
            "<span style='color:#d97706; font-size:12px;'>⚠️ 自動検出できませんでした。手動で選択してください</span>"
        )
        st.markdown(f"2️⃣ **講義資料PDF (Unmasked) パス** {badge_html}", unsafe_allow_html=True)
        col_u1, col_u2 = st.columns([5, 1])
        with col_u2:
            if st.button("📄 選択...", key="btn_choose_unmasked", use_container_width=True):
                chosen_u = choose_file_dialog("講義資料PDF (Unmasked) を選択", default_dir=st.session_state.get("base_folder_val"))
                if chosen_u:
                    st.session_state.unmasked_pdf_val = chosen_u
                    st.rerun()
        with col_u1:
            st.session_state.unmasked_input_widget = st.session_state.unmasked_pdf_val
            unmasked_pdf = st.text_input(
                "講義資料PDF (Unmasked) パス",
                key="unmasked_input_widget",
                on_change=on_unmasked_input_change,
                label_visibility="collapsed"
            )
            st.session_state.unmasked_pdf_val = unmasked_pdf

    else:
        # 事後Cloze作成モード（Unmaskedのみ）
        st.markdown("**講義資料PDF (Unmasked) パス**")
        col_u1, col_u2 = st.columns([5, 1])
        with col_u2:
            if st.button("📄 選択...", key="btn_choose_unmasked_only", use_container_width=True):
                chosen_u = choose_file_dialog("講義資料PDF (Unmasked) を選択", default_dir=st.session_state.get("base_folder_val"))
                if chosen_u:
                    st.session_state.unmasked_pdf_val = chosen_u
                    st.rerun()
        with col_u1:
            st.session_state.unmasked_input_widget = st.session_state.unmasked_pdf_val
            unmasked_pdf = st.text_input(
                "講義資料PDF (Unmasked) パス",
                key="unmasked_input_widget",
                on_change=on_unmasked_input_change,
                label_visibility="collapsed"
            )
            st.session_state.unmasked_pdf_val = unmasked_pdf

    pages_str = st.text_input("対象ページ (例: 1-10, 14, 25 または all)", value="all")

    col_btn1, col_btn2 = st.columns([1, 1])
    with col_btn1:
        btn_extract = st.button("🖼️ JPEG画像を抽出・生成", type="primary")
        if btn_extract:
            if not unmasked_pdf or not os.path.exists(unmasked_pdf):
                st.error(f"Unmasked PDFが見つかりません: `{unmasked_pdf}`")
            elif mode == "事前Cloze済み (Masked PDFあり)" and not is_masked_valid:
                st.error("Masked PDF のバリデーションエラーを解消してください。")
            else:
                with st.spinner("PDFレンダリング中..."):
                    bf_cur = st.session_state.base_folder_val.strip()
                    if bf_cur and os.path.isdir(bf_cur):
                        save_folder_config(bf_cur, {
                            "deck_name": deck_name,
                            "group_title": group_title,
                            "password": pdf_pass,
                            "base_folder": bf_cur,
                            "unmasked_pdf": unmasked_pdf,
                            "masked_pdf": st.session_state.get("masked_pdf_val", ""),
                        })
                    ts, cnt = extract_pdf_pages(unmasked_pdf, masked_pdf if mode == "事前Cloze済み (Masked PDFあり)" else None, pages_str, pdf_pass)
                    st.success(f"完了: {cnt} ページ分の画像（a/bペア）を exported_jpgs に書き出しました。")
                    st.rerun()
    
    with col_btn2:
        if st.button("📂 exported_jpgs フォルダをFinderで開く"):
            os.makedirs(DEST_DIR, exist_ok=True)
            subprocess.run(["open", os.path.abspath(DEST_DIR)])

# タブ 2: AI一括強化
with tab_ai_batch:
    st.subheader("Gemini Flash による一括スライドAI強化")
    unprocessed = [f for f in b_images if os.path.basename(f) not in ai_cache]

    st.write(f"検出されたBack画像: **{len(b_images)} 枚**（うち未処理: **{len(unprocessed)} 枚**）")

    if unprocessed:
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
                    st.session_state.pop(f"edit_html_{fn}", None)
                except Exception as e:
                    st.error(f"{fn} でAPIエラー発生: {e}")
                    break
                pbar.progress((idx + 1) / len(unprocessed))
            
            status_box.success("一括生成が完了しました！")
            st.rerun()
    else:
        st.success("すべてのスライドのAI解説が完了済みです。")

# タブ 3: 個別確認
with tab_ai_inspect:
    st.subheader("個別スライドの確認・プロンプト再生成 & 手動編集")
    if b_images:
        total_slides = len(b_images)
        st.session_state.slide_idx = max(0, min(st.session_state.slide_idx, total_slides - 1))

        st.markdown("##### 🖼️ スライド一覧（幅200px固定サムネイル・クリックで選択）")

        with st.container(height=380, border=True):
            cols_per_row = 4
            for r_start in range(0, total_slides, cols_per_row):
                cols = st.columns(cols_per_row)
                for c_idx in range(cols_per_row):
                    idx = r_start + c_idx
                    if idx < total_slides:
                        p_img = b_images[idx]
                        p_fn = os.path.basename(p_img)
                        has_ai = "✅" if p_fn in ai_cache else "⚪"
                        is_selected = (idx == st.session_state.slide_idx)
                        b64_str = get_thumb_b64(p_img, width=200)
                        
                        with cols[c_idx]:
                            border_color = "#2563eb" if is_selected else "#cbd5e1"
                            bg_color = "#eff6ff" if is_selected else "#ffffff"
                            
                            st.markdown(
                                f"""<div style="border: 2px solid {border_color}; background-color: {bg_color}; border-radius: 6px; padding: 4px; margin-bottom: 6px; text-align: center;">""",
                                unsafe_allow_html=True
                            )
                            st.markdown(f"![thumb](data:image/jpeg;base64,{b64_str})", unsafe_allow_html=True)
                            btn_label = f"スライド {idx + 1} {has_ai}"
                            if st.button(btn_label, key=f"slide_sel_{idx}", type="primary" if is_selected else "secondary", use_container_width=True):
                                st.session_state.slide_idx = idx
                                st.rerun()
                                
                            st.markdown("</div>", unsafe_allow_html=True)

        nav_1, nav_2, nav_3 = st.columns([1, 3, 1])
        with nav_1:
            if st.button("⬅️ 前のスライド", disabled=(st.session_state.slide_idx == 0), use_container_width=True):
                st.session_state.slide_idx -= 1
                st.rerun()
        with nav_3:
            if st.button("次のスライド ➡️", disabled=(st.session_state.slide_idx >= total_slides - 1), use_container_width=True):
                st.session_state.slide_idx += 1
                st.rerun()
        with nav_2:
            target_img = b_images[st.session_state.slide_idx]
            fn = os.path.basename(target_img)
            st.markdown(f"<div style='text-align:center; padding-top:6px; font-weight:bold;'>スライド {st.session_state.slide_idx + 1} / {total_slides} : {fn}</div>", unsafe_allow_html=True)

        st.markdown("---")

        col_v1, col_v2 = st.columns([1, 1])
        with col_v1:
            st.image(target_img, caption=fn, use_container_width=True)
            
            if st.button("📋 画像をクリップボードにコピー (for ChatGPT / Claude)", use_container_width=True):
                if copy_image_to_clipboard_macos(target_img):
                    st.toast("✅ 画像をクリップボードにコピーしました！外部チャットにそのまま貼り付け可能です。")
                else:
                    st.error("クリップボードへのコピーに失敗しました（macOS限定機能です）。")

        with col_v2:
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

            def on_menu_select():
                sel = st.session_state.prompt_menu_sel
                if sel in menu_map:
                    st.session_state.custom_prompt_val = menu_map[sel]
                    st.session_state.prompt_menu_sel = "(保存済み / 最新5件から呼び出し)"

            def on_reset_default():
                st.session_state.custom_prompt_val = DEFAULT_PROMPT

            pm_col1, pm_col2 = st.columns([2.5, 1])
            with pm_col1:
                st.selectbox(
                    "プロンプト呼び出しメニュー",
                    menu_items,
                    key="prompt_menu_sel",
                    on_change=on_menu_select,
                    label_visibility="collapsed",
                )
            with pm_col2:
                st.button(
                    "🔄 Defaultに戻す",
                    on_click=on_reset_default,
                    use_container_width=True,
                )

            custom_prompt = st.text_area(
                "プロンプト（初期値: 一括追加と同じDefaultプロンプト）",
                key="custom_prompt_val",
                height=110,
            )

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
                prompt_js = json.dumps(custom_prompt)
                copy_btn_html = f"""
                <button onclick='navigator.clipboard.writeText({prompt_js}); alert("プロンプトをクリップボードにコピーしました！");' 
                        style="width: 100%; height: 38px; background-color: #f1f5f9; color: #1e293b; border: 1px solid #cbd5e1; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 500;">
                    📋 Promptコピー
                </button>
                """
                st.components.v1.html(copy_btn_html, height=45)

            model_labels = list(PRICING_MODELS.keys())
            default_idx = 0
            for idx, label in enumerate(model_labels):
                if "2.5 Flash" in label:
                    default_idx = idx
                    break

            gen_c1, gen_c2 = st.columns([1.6, 1])
            with gen_c1:
                sel_inspect_label = st.selectbox(
                    "使用モデル（個別）",
                    model_labels,
                    index=default_idx,
                    label_visibility="collapsed"
                )
            
            chosen_spec = PRICING_MODELS[sel_inspect_label]
            sel_inspect_model = chosen_spec["id"]
            
            single_in_cost = (700 / 1_000_000) * chosen_spec["in"]
            single_out_cost = (1200 / 1_000_000) * chosen_spec["out"]
            single_cost_usd = single_in_cost + single_out_cost
            single_cost_jpy = single_cost_usd * USD_JPY

            with gen_c2:
                st.caption(f"💰 1枚推定: **約 {single_cost_jpy:.3f} 円** (`${single_cost_usd:.4f}`)")

            if st.button("✨ このスライドのみAPI再生成 (Gemini)", type="primary", disabled=not api_key, use_container_width=True):
                keep_recent_prompt(custom_prompt)
                with st.spinner(f"{fn} のAI解説を再生成中..."):
                    new_html = call_gemini_enhance(target_img, api_key, sel_inspect_model, custom_prompt)
                    ai_cache[fn] = new_html
                    save_ai_cache(ai_cache)
                    st.session_state.pop(f"edit_html_{fn}", None)
                    st.success("更新しました！")
                    st.rerun()

        # --- 1. 現在のBack面AI解説プレビュー (Anki実機準拠 Lightテーマ) ---
        st.markdown("---")
        st.markdown("#### プレビュー (Anki Back面)")
        if fn in ai_cache:
            preview_style = f"""
            <style>
                html, body {{
                    margin: 0;
                    padding: 8px;
                    background-color: #f1f5f9 !important;
                    color: #1e293b !important;
                    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", "Segoe UI", Roboto, sans-serif;
                }}
                .card-preview {{
                    background: #ffffff !important;
                    border: 1px solid #cbd5e1;
                    border-radius: 8px;
                    padding: 18px;
                    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.08);
                    color: #1e293b !important;
                }}
                .ai-container {{
                    margin-top: 0;
                    text-align: left;
                    font-size: 0.95em;
                    line-height: 1.6;
                    color: #1e293b !important;
                }}
                .ai-title {{
                    background: #0b4f8c !important;
                    color: #ffffff !important;
                    font-size: 1.1em;
                    font-weight: bold;
                    padding: 8px 12px;
                    border-radius: 6px;
                    margin-bottom: 14px;
                    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
                }}
                .ai-summary-box {{
                    background: #fff4f4 !important;
                    border-left: 4px solid #ff3b30 !important;
                    padding: 8px 12px;
                    margin-bottom: 14px;
                    border-radius: 0 4px 4px 0;
                    color: #222222 !important;
                }}
                .ai-summary-title {{
                    color: #d70015 !important;
                    font-weight: bold;
                    margin-bottom: 2px;
                }}
                .ai-cascade-box {{
                    background: #f6f8fa !important;
                    border: 1px solid #e1e4e8 !important;
                    border-radius: 6px;
                    padding: 10px 14px;
                    margin-top: 12px;
                    color: #24292e !important;
                }}
                .ai-table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin: 10px 0;
                    background: #ffffff !important;
                    border-radius: 4px;
                    overflow: hidden;
                    box-shadow: 0 0 0 1px #d2e3f8;
                    color: #1e293b !important;
                }}
                .ai-table th {{
                    background: #e8f2fe !important;
                    color: #1a4f8b !important;
                    padding: 8px;
                    text-align: left;
                    border-bottom: 1px solid #c2dbf7;
                    font-weight: bold;
                }}
                .ai-table td {{
                    padding: 8px;
                    border-bottom: 1px solid #edf2f9;
                    border-right: 1px solid #edf2f9;
                    vertical-align: top;
                    color: #222222 !important;
                }}
                ul, ol {{
                    margin: 8px 0;
                    padding-left: 20px;
                    color: #1e293b !important;
                }}
                li {{
                    margin-bottom: 4px;
                }}
                b, strong {{
                    color: #0f172a !important;
                }}
            </style>
            <div class="card-preview">
                <div class="ai-container">
                    {ai_cache[fn]}
                </div>
            </div>
            """
            st.components.v1.html(preview_style, height=450, scrolling=True)
        else:
            st.info("このスライドにはまだAI解説がありません。APIで生成するか、下のテキストエリアから手動入力してください。")

        # --- 2. 一番下: HTMLソースコードの直接編集・手動貼り付け用エリア ---
        st.markdown("---")
        st.markdown("#### 📝 AI解説HTMLの直接編集・貼り付け (ChatGPT/Claudeなどの結果を反映)")
        
        edit_key = f"edit_html_{fn}"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = ai_cache.get(fn, "")

        with st.container(border=True):
            edit_col1, edit_col2 = st.columns([4, 1])
            with edit_col1:
                edited_html = st.text_area(
                    "解説HTMLソースコード (生成後に自動表示・直接編集または外部LLMの出力を貼り付け可能)",
                    key=edit_key,
                    height=220,
                    help="API生成されたHTMLソースコードの微修正や、外部LLM（ChatGPT, Claude等）で作成したHTMLの貼り付け・保存が行えます。"
                )
            with edit_col2:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("💾 HTMLを手動保存", type="primary", use_container_width=True):
                    clean_text = edited_html.strip()
                    if clean_text.startswith("```html"): clean_text = clean_text[7:]
                    if clean_text.startswith("```"): clean_text = clean_text[3:]
                    if clean_text.endswith("```"): clean_text = clean_text[:-3]
                    clean_text = clean_text.strip()
                    
                    ai_cache[fn] = clean_text
                    save_ai_cache(ai_cache)
                    st.session_state.pop(edit_key, None)
                    st.toast("✅ HTML解説を保存し、プレビューを更新しました！")
                    st.rerun()
                
                if fn in ai_cache:
                    if st.button("🗑️ この解説を消去", use_container_width=True):
                        del ai_cache[fn]
                        save_ai_cache(ai_cache)
                        st.session_state.pop(edit_key, None)
                        st.info("AI解説を消去しました。")
                        st.rerun()

# タブ 4: Ankiパッケージ出力
with tab_build:
    st.subheader("Ankiデッキパッケージ (.apkg) 出力")
    st.write(f"出力先デッキ名: **{deck_name}**")
    
    bf = st.session_state.get("base_folder_val", "").strip()
    target_dir = bf if (bf and os.path.exists(bf)) else "."
    display_dir = os.path.abspath(target_dir)
    st.info(f"📁 出力先フォルダ: `{display_dir}`")
    
    col_out1, col_out2 = st.columns([1, 1])
    with col_out1:
        if st.button("📦 .apkg パッケージを作成", type="primary"):
            with st.spinner("パッケージング中..."):
                apkg_path, count = build_apkg(
                    DEST_DIR, 
                    deck_name, 
                    group_title, 
                    get_ai_cache(), 
                    output_dir=target_dir
                )
                st.success(f"完成: {count} 枚のカードを含む '{os.path.basename(apkg_path)}' を保存しました！")
                st.caption(f"保存場所: `{apkg_path}`")
                st.info("※決定論的GUIDにより、Ankiに再インポートしても学習進捗を維持したまま上書きされます。")

    with col_out2:
        if st.button("🗑️ 中間JPEG画像フォルダを全削除 (クリーンアップ)"):
            all_jpgs = glob.glob(os.path.join(DEST_DIR, "*.jpg"))
            for f in all_jpgs:
                os.remove(f)
            st.success(f"{len(all_jpgs)} 枚のJPEGを削除しました。")