import streamlit as st
import os
import re
import glob
import json
import subprocess
import shutil
import io
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
from dotenv import load_dotenv
from pipeline_util import (
    extract_pdf_pages,
    call_gemini_enhance,
    build_apkg,
    get_poppler_path,
    restore_from_anki_export,
    DEFAULT_PROMPT,
    SVG_OVERLAY_PROMPT,
)

# ==============================================================================
# 環境依存バイナリ（Poppler）の初期化チェック
# ==============================================================================
if not shutil.which("pdftoppm") and get_poppler_path() is None:
    st.error("""
    ⚠️ **Poppler が見つかりません**
    PDFの画像変換を行うには Poppler のインストールが必要です。
    - **macOS**: ターミナルで `brew install poppler` を実行してください。
    - **Windows**: Poppler をダウンロードし、環境変数 PATH に追加してください。
    """)

load_dotenv()

# ==============================================================================
# 設定ファイル・ディレクトリ定数定義
# ==============================================================================
LAST_WORK_DIR_FILE = os.path.expanduser("~/.last_work_dir")
LEGACY_PRESETS_FILE = "lecture_presets.json"
FOLDER_CONFIG_FILENAME = "lecture_config.json"
CACHE_FILE = "exported_jpgs/ai_notes_cache.json"
DEST_DIR = "exported_jpgs"
PROMPTS_FILE = "custom_prompts.json"

PRICING_MODELS = {
    "Gemini 3.6 Flash (バランス標準)":       {"id": "gemini-3.6-flash",      "in": 0.75, "out": 3.75},
    "Gemini 3.5 Flash-Lite (最安推奨)":     {"id": "gemini-3.5-flash-lite", "in": 0.30, "out": 2.50},
    "Gemini 3.8 Flash (高推論Flash)":       {"id": "gemini-3.8-flash",      "in": 0.75, "out": 3.75},
    "Gemini 4 Argon (〜11/30 特割)":        {"id": "gemini-4-argon",        "in": 2.00, "out": 10.00},
    "Gemini 4 Argon (12/1〜 通常)":         {"id": "gemini-4-argon",        "in": 4.00, "out": 20.00},
}
USD_JPY = 155.0


# ==============================================================================
# フォルダ単位設定ロード・永続化ヘルパー
# ==============================================================================
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


# ==============================================================================
# PDFファイル名の自動判定およびペア推論ロジック
# ==============================================================================
def infer_unmasked_from_masked(masked_path):
    if not masked_path or not masked_path.strip():
        return ""
    masked_path = masked_path.strip()
    folder = os.path.dirname(masked_path)
    filename = os.path.basename(masked_path)
    stem, ext = os.path.splitext(filename)

    clean_stem = re.sub(r'[_\s]+masked$', '', stem, flags=re.IGNORECASE)
    if clean_stem == stem:
        clean_stem = re.sub(r'masked$', '', stem, flags=re.IGNORECASE).rstrip('_- ')

    candidate = os.path.join(folder, f"{clean_stem}{ext}")
    if os.path.exists(candidate):
        return candidate

    if folder and os.path.isdir(folder):
        for f in os.listdir(folder):
            if f.lower() == f"{clean_stem}{ext}".lower():
                return os.path.join(folder, f)

    return candidate


def auto_detect_pdfs(folder_path):
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
    if not folder_path:
        return {}
    cfg_path = os.path.join(folder_path, FOLDER_CONFIG_FILENAME)
    if os.path.exists(cfg_path):
        try:
            return json.load(open(cfg_path, "r", encoding="utf-8"))
        except Exception:
            pass

    folder_name = os.path.basename(folder_path.rstrip("/"))
    u_pdf, m_pdf = auto_detect_pdfs(folder_path)
    return {
        "parent_deck": "",
        "deck_name": folder_name,
        "group_title": folder_name,
        "password": "",
        "base_folder": folder_path,
        "unmasked_pdf": u_pdf,
        "masked_pdf": m_pdf,
    }

def save_folder_config(folder_path, config_data):
    if not folder_path or not os.path.isdir(folder_path):
        return False
    cfg_path = os.path.join(folder_path, FOLDER_CONFIG_FILENAME)
    json.dump(config_data, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    save_last_folder(folder_path)
    return True

def safe_set_state(key, value):
    """ウィジェット描画前なら直接セットし、描画後なら保留キューに退避する"""
    try:
        st.session_state[key] = value
    except Exception:
        if "_pending_state_updates" not in st.session_state:
            st.session_state["_pending_state_updates"] = {}
        st.session_state["_pending_state_updates"][key] = value

def apply_folder_to_state(folder_path, explicit_masked=None, explicit_unmasked=None):
    safe_set_state("base_folder_val", folder_path)
    cfg = load_folder_config(folder_path)
    u_default, m_default = auto_detect_pdfs(folder_path)

    d_name = cfg.get("deck_name", "")
    g_title = cfg.get("group_title", "")
    p_deck = cfg.get("parent_deck", "")

    if "::" in d_name and not p_deck:
        parts = d_name.split("::", 1)
        p_deck = parts[0].strip()
        if not g_title:
            g_title = parts[1].strip()

    final_masked = explicit_masked if explicit_masked else (cfg.get("masked_pdf") or m_default)
    final_unmasked = explicit_unmasked if explicit_unmasked else (cfg.get("unmasked_pdf") or u_default)

    safe_set_state("parent_deck_val", p_deck)
    safe_set_state("parent_deck_input", p_deck)
    safe_set_state("group_title_val", g_title)
    safe_set_state("group_title_input", g_title)
    safe_set_state("deck_name_val", f"{p_deck}::{g_title}" if (p_deck and g_title) else (d_name or g_title))
    safe_set_state("pdf_pass_val", cfg.get("password", ""))
    safe_set_state("masked_pdf_val", final_masked)
    safe_set_state("unmasked_pdf_val", final_unmasked)
    save_last_folder(folder_path)


# ==============================================================================
# AI解説キャッシュ管理 & GUID Map（休止状態永続化）
# ==============================================================================
def get_ai_cache():
    return json.load(open(CACHE_FILE, "r", encoding="utf-8")) if os.path.exists(CACHE_FILE) else {}

def save_ai_cache(data):
    os.makedirs(DEST_DIR, exist_ok=True)
    json.dump(data, open(CACHE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def get_guid_map():
    p = os.path.join(DEST_DIR, "guid_map.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, "r", encoding="utf-8"))
        except Exception:
            pass
    return {"deck_name": "", "notetype_name": "Medical AI Slide Model", "notes": {}}

def save_guid_map(data):
    os.makedirs(DEST_DIR, exist_ok=True)
    p = os.path.join(DEST_DIR, "guid_map.json")
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ==============================================================================
# macOS クリップボード連携
# ==============================================================================
def copy_image_to_clipboard_macos(image_path):
    abs_path = os.path.abspath(image_path)
    if not os.path.exists(abs_path):
        return False
    osa = f'''
    use framework "AppKit"
    set theImage to current application's NSImage's alloc()'s initWithContentsOfFile:"{abs_path}"
    if theImage is not missing value then
        set pb to current application's NSPasteboard's generalPasteboard()
        pb's clearContents()
        pb's writeObjects:{{theImage}}
    else
        error "Failed to load image"
    end if
    '''
    res = subprocess.run(["osascript", "-e", osa], capture_output=True, text=True)
    return res.returncode == 0


# ==============================================================================
# プロンプト保存・履歴管理 & 種別判定ヘルパー
# ==============================================================================
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
    if not p_clean or p_clean in (DEFAULT_PROMPT, SVG_OVERLAY_PROMPT):
        return
    store = get_prompt_store()
    recent = [p for p in store.get("recent", []) if p != p_clean]
    recent.insert(0, p_clean)
    store["recent"] = recent[:5]
    save_prompt_store(store)

def identify_prompt_label(p_text):
    if not p_text:
        return "（未入力）"
    p_clean = " ".join(p_text.split())
    default_clean = " ".join(DEFAULT_PROMPT.split())
    svg_clean = " ".join(SVG_OVERLAY_PROMPT.split())

    if p_clean == default_clean:
        return "🔄 Defaultプロンプト (標準解説・SVGなし)"
    if p_clean == svg_clean or "SVGオーバーレイ" in p_text or "svg-overlay-wrap" in p_text:
        return "🎯 SVGオーバーレイ図示付きプロンプト"
    
    store = get_prompt_store()
    for name, val in store.get("saved", {}).items():
        if " ".join(val.split()) == p_clean:
            return f"💾 保存済みプロンプト: 「{name}」"
            
    return "✏️ カスタム編集プロンプト"


# ==============================================================================
# macOS Finder ダイアログ連携
# ==============================================================================
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


# ==============================================================================
# サムネイル高速化キャッシュ (mtime連動)
# ==============================================================================
@st.cache_data(show_spinner=False)
def get_thumb_b64(img_path, width=200, mtime=None):
    img = Image.open(img_path)
    w, h = img.size
    thumb = img.resize((width, int(h * (width / w))), Image.BILINEAR)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ==============================================================================
# バッチ実行前の安全確認モーダルダイアログ
# ==============================================================================
@st.dialog("⚠️ バッチ生成の実行前確認", width="large")
def confirm_batch_execution_dialog(
    selected_paths,
    all_b_images,
    prompt_text,
    model_label,
    model_id,
    max_workers,
    est_cost_jpy,
    est_cost_usd,
    api_key,
):
    prompt_type_str = identify_prompt_label(prompt_text)
    is_svg = ("SVG" in prompt_type_str) and ("Default" not in prompt_type_str)

    idx_map = {os.path.basename(p): i + 1 for i, p in enumerate(all_b_images)}
    slide_nums = [f"#{idx_map.get(os.path.basename(p), '?')} ({os.path.basename(p)})" for p in selected_paths]

    st.markdown("以下の設定で一括AI解説生成を開始します。**スライドとプロンプトの選択に間違いがないか確認してください。**")

    if is_svg:
        st.warning(f"**選択中のプロンプト:** {prompt_type_str}\n\n※画像上にSVG図形とラベルを重ねるモードです。")
    elif "Default" in prompt_type_str:
        st.info(f"**選択中のプロンプト:** {prompt_type_str}\n\n※標準の箇条書き・表組み・カスケード解説を生成します（SVGなし）。")
    else:
        st.info(f"**選択中のプロンプト:** {prompt_type_str}")

    with st.expander("📄 送信されるプロンプト全文を確認する", expanded=False):
        st.code(prompt_text, language="text")

    st.markdown(f"**🎯 対象スライド ({len(selected_paths)} 枚):**")
    st.markdown(
        f"<div style='max-height: 110px; overflow-y: auto; background: #f8fafc; color: #1e293b; padding: 8px 12px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 13px;'>"
        + " , ".join(slide_nums)
        + "</div>",
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown(
        f"- **実行方式:** Gemini API 並列実行 (**{max_workers} 並列**)\n"
        f"- **使用モデル:** `{model_label}` (`{model_id}`)\n"
        f"- **推定コスト:** **約 {est_cost_jpy:.2f} 円** (`${est_cost_usd:.4f}`)"
    )

    col_ok, col_cancel = st.columns([1.5, 1])
    with col_ok:
        run_confirmed = st.button("✅ はい、この設定で実行する", type="primary", width="stretch")
    with col_cancel:
        if st.button("キャンセル", width="stretch"):
            st.rerun()

    if run_confirmed:
        keep_recent_prompt(prompt_text)
        ai_cache_local = get_ai_cache()
        sel_count = len(selected_paths)
        pbar = st.progress(0)
        status_box = st.empty()
        errors = []
        completed = 0

        def _worker(img_p):
            return os.path.basename(img_p), call_gemini_enhance(img_p, api_key, model_id, prompt_text)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {executor.submit(_worker, p): p for p in selected_paths}
            for future in as_completed(future_to_path):
                p_orig = future_to_path[future]
                f_name = os.path.basename(p_orig)
                try:
                    res_fn, html_res = future.result()
                    ai_cache_local[res_fn] = html_res
                    save_ai_cache(ai_cache_local)
                    st.session_state.pop(f"edit_html_{res_fn}", None)
                    st.session_state.selected_batch_slides.discard(res_fn)
                    st.session_state[f"chk_batch_{res_fn}"] = False
                except Exception as exc:
                    errors.append(f"{f_name}: {exc}")
                completed += 1
                pbar.progress(completed / sel_count)
                status_box.text(f"並列生成中 ({completed}/{sel_count} 完了): 最新完了 {f_name}")

        if errors:
            st.error("一部のスライドでエラーが発生しました:\n" + "\n".join(errors))
        else:
            status_box.success(f"🎉 {sel_count} 枚のAI解説生成が完了しました！")
            st.toast(f"✅ {sel_count} 枚のバッチ生成が完了しました！")
            st.rerun()


# ==============================================================================
# Streamlit UI アプリケーション初期化
# ==============================================================================
st.set_page_config(page_title="PDF to Anki + AI Enhancer", layout="wide")
st.title("医学部講義PDF ➔ Ankiカード & AI強化 生成システム")

# 保留された状態更新があれば、ウィジェット描画前に最優先で一括上書き
if "_pending_state_updates" in st.session_state:
    pending = st.session_state.pop("_pending_state_updates")
    for k, v in pending.items():
        st.session_state[k] = v

if "base_folder_val" not in st.session_state:
    init_bf = get_last_folder()
    if init_bf:
        apply_folder_to_state(init_bf)
    else:
        st.session_state.base_folder_val = ""
        st.session_state.parent_deck_val = ""
        st.session_state.group_title_val = ""
        st.session_state.deck_name_val = ""
        st.session_state.pdf_pass_val = ""
        st.session_state.unmasked_pdf_val = ""
        st.session_state.masked_pdf_val = ""

# 親Deck名またはグループタイトル変更時の自動合成コールバック
def on_deck_naming_change():
    p = st.session_state.get("parent_deck_input", "").strip()
    g = st.session_state.get("group_title_input", "").strip()
    st.session_state.parent_deck_val = p
    st.session_state.group_title_val = g
    if p and g:
        st.session_state.deck_name_val = f"{p}::{g}"
    elif g:
        st.session_state.deck_name_val = g
    elif p:
        st.session_state.deck_name_val = p
    else:
        st.session_state.deck_name_val = ""

def on_base_folder_input_change():
    bf = st.session_state.base_folder_val.strip().rstrip("/")
    if bf and os.path.isdir(bf):
        apply_folder_to_state(bf)

def on_masked_input_change():
    m_val = st.session_state.get("masked_pdf_val", "").strip()
    inferred = infer_unmasked_from_masked(m_val)
    if inferred:
        safe_set_state("unmasked_pdf_val", inferred)
    p_dir = os.path.dirname(m_val)
    if p_dir and os.path.isdir(p_dir):
        apply_folder_to_state(p_dir, explicit_masked=m_val, explicit_unmasked=inferred)

def on_unmasked_input_change():
    st.session_state.unmasked_pdf_val = st.session_state.get("unmasked_pdf_val", "").strip()


# ==============================================================================
# サイドバー: 講義設定 & デッキ名自動合成
# ==============================================================================
with st.sidebar:
    st.header("講義フォルダ・デッキ設定")
    
    st.markdown("**📂 講義フォルダ (PDF選択時に自動同期)**")
    col_bf1, col_bf2 = st.columns([3.2, 1])
    with col_bf2:
        if st.button("📂 選択", key="btn_choose_bf", help="Finderで講義フォルダを選択", width="stretch"):
            cur_dir = st.session_state.get("base_folder_val", "")
            start_dir = os.path.dirname(cur_dir) if (cur_dir and os.path.isdir(cur_dir)) else cur_dir
            chosen_bf = choose_folder_dialog("講義フォルダを選択してください", default_dir=start_dir)
            if chosen_bf:
                apply_folder_to_state(chosen_bf)
                st.rerun()
    with col_bf1:
        base_folder = st.text_input(
            "講義フォルダ",
            value=st.session_state.get("base_folder_val", ""),
            key="base_folder_val",
            on_change=on_base_folder_input_change,
            placeholder="/path/to/lecture_folder",
            label_visibility="collapsed"
        )

    if base_folder and os.path.isdir(base_folder):
        cfg_file_in_dir = os.path.join(base_folder, FOLDER_CONFIG_FILENAME)
        if os.path.exists(cfg_file_in_dir):
            st.caption(f"✅ フォルダ内設定 (`{FOLDER_CONFIG_FILENAME}`) 読込済")
        else:
            st.caption("🆕 新規フォルダ（設定未保存）")
    elif base_folder:
        st.warning("⚠️ フォルダが存在しません")

    st.markdown("---")
    st.markdown("**🗂️ デッキ構成（自動結合）**")
    
    parent_deck = st.text_input(
        "親Deck名 (例: ブロック1_小児疾患)",
        value=st.session_state.get("parent_deck_val", ""),
        key="parent_deck_input",
        on_change=on_deck_naming_change,
        placeholder="ブロック1_小児疾患"
    )
    
    group_title = st.text_input(
        "グループタイトル (例: 8 小児の発熱・発疹性疾患)",
        value=st.session_state.get("group_title_val", ""),
        key="group_title_input",
        on_change=on_deck_naming_change,
        placeholder="8 小児の発熱・発疹性疾患"
    )

    effective_deck = st.session_state.get("deck_name_val", "")
    st.text_input("生成されるDeck名 (完全名)", value=effective_deck, disabled=True, help="親Deck名とグループタイトルから自動合成されます")

    pdf_pass = st.text_input("PDFパスワード (空白可)", key="pdf_pass_val", type="password")
    
    if st.button("💾 このフォルダに設定を保存", type="primary", width="stretch"):
        bf_target = st.session_state.base_folder_val.strip()
        if not bf_target or not os.path.isdir(bf_target):
            st.error("有効な講義フォルダを先に選択してください。")
        else:
            cfg_data = {
                "parent_deck": st.session_state.get("parent_deck_val", ""),
                "group_title": st.session_state.get("group_title_val", ""),
                "deck_name": effective_deck,
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

# セッション状態変数の初期化
if "slide_idx" not in st.session_state:
    st.session_state.slide_idx = 0
if "custom_prompt_val" not in st.session_state:
    st.session_state.custom_prompt_val = DEFAULT_PROMPT
if "selected_batch_slides" not in st.session_state:
    st.session_state.selected_batch_slides = set()

# 画像探索: 常に最新バッチの画像、または抽出された全画像をそのまま表示
all_b_images = sorted(glob.glob(os.path.join(DEST_DIR, "*b.jpg")))
guid_map_data = get_guid_map()
notes_meta = guid_map_data.get("notes", {})
b_images = []

if all_b_images:
    # 最新タイムスタンプのプレフィックス（例: 2026-11-11_152954）を持つ画像群をすべて取得
    latest_ts = sorted({os.path.basename(f)[:17] for f in all_b_images})[-1]
    b_images = [f for f in all_b_images if os.path.basename(f).startswith(latest_ts)]

ai_cache = get_ai_cache()


# ==============================================================================
# メイン画面タブ構成
# ==============================================================================
tab_extract, tab_ai_batch, tab_ai_inspect, tab_build = st.tabs([
    "1. PDF指定 & 抽出", 
    "2. 💰 AI解説の一括追加", 
    "3. 🔍 個別AI解説 & 選択バッチ生成", 
    "4. 📦 Ankiパッケージ出力"
])


# ==============================================================================
# タブ 1: PDFファイルの指定とJPEG書き出し（動線を一本化）
# ==============================================================================
with tab_extract:
    st.subheader("PDFファイルの指定とJPEG書き出し")

    mode = st.radio("Cloze準備の形式", ["事前Cloze済み (Masked PDFあり)", "事後Cloze作成 (Unmaskedのみから生成)"])
    
    masked_pdf = None
    unmasked_pdf = None
    is_masked_valid = True

    if mode == "事前Cloze済み (Masked PDFあり)":
        st.markdown(
            "1️⃣ **マスク済みPDF (Masked) パス** <span style='color:#ef4444; font-size:12px;'>※選択するとUnmasked PDFおよび講義フォルダを自動検出します</span>",
            unsafe_allow_html=True
        )
        col_m1, col_m2 = st.columns([5, 1])
        with col_m2:
            if st.button("📄 選択...", key="btn_choose_masked", width="stretch"):
                chosen_m = choose_file_dialog("マスク済みPDF (Masked) を選択", default_dir=st.session_state.get("base_folder_val"))
                if chosen_m:
                    inferred_u = infer_unmasked_from_masked(chosen_m)
                    parent_dir = os.path.dirname(chosen_m)
                    if parent_dir and os.path.isdir(parent_dir):
                        apply_folder_to_state(parent_dir, explicit_masked=chosen_m, explicit_unmasked=inferred_u)
                    else:
                        safe_set_state("masked_pdf_val", chosen_m)
                        if inferred_u:
                            safe_set_state("unmasked_pdf_val", inferred_u)
                    st.rerun()

        with col_m1:
            masked_pdf = st.text_input(
                "マスク済みPDF (Masked) パス",
                value=st.session_state.get("masked_pdf_val", ""),
                key="masked_pdf_val",
                on_change=on_masked_input_change,
                label_visibility="collapsed"
            )

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

        if is_masked_valid and (not st.session_state.unmasked_pdf_val or not os.path.exists(st.session_state.unmasked_pdf_val)):
            auto_u = infer_unmasked_from_masked(masked_pdf)
            if auto_u and os.path.exists(auto_u):
                st.session_state.unmasked_pdf_val = auto_u

        u_exists = bool(st.session_state.unmasked_pdf_val and os.path.exists(st.session_state.unmasked_pdf_val))
        badge_html = (
            "<span style='color:#16a34a; font-size:12px;'>✅ 自動検出済 (手動変更も可)</span>"
            if u_exists else
            "<span style='color:#d97706; font-size:12px;'>⚠️ 自動検出できませんでした。手動で選択してください</span>"
        )
        st.markdown(f"2️⃣ **講義資料PDF (Unmasked) パス** {badge_html}", unsafe_allow_html=True)
        col_u1, col_u2 = st.columns([5, 1])
        with col_u2:
            if st.button("📄 選択...", key="btn_choose_unmasked", width="stretch"):
                chosen_u = choose_file_dialog("講義資料PDF (Unmasked) を選択", default_dir=st.session_state.get("base_folder_val"))
                if chosen_u:
                    safe_set_state("unmasked_pdf_val", chosen_u)
                    st.rerun()
        with col_u1:
            unmasked_pdf = st.text_input(
                "講義資料PDF (Unmasked) パス",
                value=st.session_state.get("unmasked_pdf_val", ""),
                key="unmasked_pdf_val",
                on_change=on_unmasked_input_change,
                label_visibility="collapsed"
            )

    else:
        st.markdown("**講義資料PDF (Unmasked) パス**")
        col_u1, col_u2 = st.columns([5, 1])
        with col_u2:
            if st.button("📄 選択...", key="btn_choose_unmasked_only", width="stretch"):
                chosen_u = choose_file_dialog("講義資料PDF (Unmasked) を選択", default_dir=st.session_state.get("base_folder_val"))
                if chosen_u:
                    safe_set_state("unmasked_pdf_val", chosen_u)
                    st.rerun()
        with col_u1:
            unmasked_pdf = st.text_input(
                "講義資料PDF (Unmasked) パス",
                value=st.session_state.get("unmasked_pdf_val", ""),
                key="unmasked_pdf_val",
                on_change=on_unmasked_input_change,
                label_visibility="collapsed"
            )

    pages_str = "all"

    col_btn1, col_btn2 = st.columns([1.2, 1])
    with col_btn1:
        btn_extract = st.button("🖼️ JPEG画像を抽出・生成 (全ページ)", type="primary", width="stretch")
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
                            "parent_deck": st.session_state.get("parent_deck_val", ""),
                            "group_title": st.session_state.get("group_title_val", ""),
                            "deck_name": st.session_state.get("deck_name_val", ""),
                            "password": pdf_pass,
                            "base_folder": bf_cur,
                            "unmasked_pdf": unmasked_pdf,
                            "masked_pdf": st.session_state.get("masked_pdf_val", ""),
                        })
                    ts, cnt = extract_pdf_pages(unmasked_pdf, masked_pdf if mode == "事前Cloze済み (Masked PDFあり)" else None, pages_str, pdf_pass)
                    st.session_state.selected_batch_slides = set()
                    st.success(f"完了: {cnt} ページ分の画像（a/bペア）を exported_jpgs に書き出しました。")
                    st.rerun()
    
    with col_btn2:
        if st.button("📂 exported_jpgs フォルダをFinderで開く", width="stretch"):
            os.makedirs(DEST_DIR, exist_ok=True)
            subprocess.run(["open", os.path.abspath(DEST_DIR)])

    # --------------------------------------------------------------------------
    # 既存Ankiデッキからの作業状態復元
    # --------------------------------------------------------------------------
    st.markdown("---")
    st.subheader("🔄 既存Ankiデッキ（ノート書き出し .txt）からの作業状態復元")
    st.caption("すでにAnkiに登録済みのカードから、画像・GUID・既存AI解説をローカル環境へ復元します。")

    if "anki_txt_path_val" not in st.session_state:
        st.session_state.anki_txt_path_val = ""
    if "anki_media_dir_val" not in st.session_state:
        st.session_state.anki_media_dir_val = os.path.expanduser("~/Library/Application Support/Anki2/User 1/collection.media")

    col_r1, col_r2 = st.columns([5, 1])
    with col_r2:
        if st.button("📄 .txt選択", key="btn_choose_anki_txt", width="stretch"):
            chosen_txt = choose_file_dialog("Ankiノート書き出し (.txt) を選択", default_dir=st.session_state.get("base_folder_val"))
            if chosen_txt:
                st.session_state.anki_txt_path_val = chosen_txt
                st.rerun()
    with col_r1:
        anki_txt_path = st.text_input("Ankiエクスポート (.txt) パス", key="anki_txt_path_val")

    anki_media_dir = st.text_input("Anki collection.media フォルダパス", key="anki_media_dir_val")

    def on_restore_anki_click():
        txt_p = st.session_state.get("anki_txt_path_val", "").strip()
        med_d = st.session_state.get("anki_media_dir_val", "").strip()
        if not txt_p or not os.path.exists(txt_p):
            st.session_state.restore_msg = ("error", f"指定された .txt ファイルが見つかりません: '{txt_p}'")
            return

        try:
            res = restore_from_anki_export(txt_p, med_d if med_d else None, DEST_DIR)
            st.cache_data.clear()
            st.session_state.slide_idx = 0
            st.session_state.selected_batch_slides = set()
            
            if res.get("deck_name"):
                d_name = res["deck_name"]
                st.session_state.deck_name_val = d_name
                if "::" in d_name:
                    p, g = d_name.split("::", 1)
                    st.session_state.parent_deck_val = p.strip()
                    st.session_state.group_title_val = g.strip()
                else:
                    st.session_state.group_title_val = d_name
                    
            st.session_state.restore_msg = ("success", res)
        except Exception as e:
            st.session_state.restore_msg = ("error", f"復元中にエラーが発生しました: {e}")
        
    st.button(
        "♻️ Ankiエクスポートから画像・既存AI解説・GUIDを復元",
        key="btn_run_restore_anki",
        width="stretch",
        on_click=on_restore_anki_click,
    )

    if "restore_msg" in st.session_state:
        m_type, m_val = st.session_state.restore_msg
        if m_type == "error":
            st.error(m_val)
        elif m_type == "success":
            st.success(
                f"✅ 復元成功！ ノート数: {m_val['restored_notes']} 枚 / "
                f"AI解説: {m_val['restored_ai']} 件 / "
                f"コピー画像: {m_val['copied_images']} 枚"
            )
            if m_val["missing_images"]:
                st.warning(f"一部メディアが見つかりませんでした: {len(m_val['missing_images'])} 件")


# ==============================================================================
# タブ 2: AI一括強化 (全未処理スライドのシーケンシャル生成)
# ==============================================================================
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

        model_options = list(PRICING_MODELS.keys())
        default_batch_idx = next((i for i, k in enumerate(model_options) if "3.6 Flash" in k), 0)
        sel_model_name = st.selectbox("使用するモデル", model_options, index=default_batch_idx)
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


# ==============================================================================
# タブ 3: 個別スライド確認 & 複数選択バッチ並列生成
# ==============================================================================
with tab_ai_inspect:
    st.subheader("個別スライドの確認・プロンプト再生成 & 複数選択バッチ生成")
    if b_images:
        total_slides = len(b_images)
        st.session_state.slide_idx = max(0, min(st.session_state.slide_idx, total_slides - 1))

        valid_fns = {os.path.basename(f) for f in b_images}
        st.session_state.selected_batch_slides = st.session_state.selected_batch_slides.intersection(valid_fns)

        st.markdown("""
        <style>
            div[data-testid="stCheckbox"] {
                margin-bottom: -8px !important;
                padding-top: 4px !important;
            }
            div[data-testid="stCheckbox"] label {
                min-height: 24px !important;
            }
        </style>
        """, unsafe_allow_html=True)

        st.markdown("##### 🖼️ スライド一覧（縦スクロール可 ＋ アクティブ自動追従 ＋ 複数バッチ選択）")
        sel_bar1, sel_bar2, sel_bar3, sel_bar4, sel_bar5 = st.columns([1.2, 1.1, 1, 1, 1.5])
        with sel_bar1:
            if st.button("⚪ 未処理のみ全選択", width="stretch"):
                unproc_fns = {os.path.basename(f) for f in b_images if os.path.basename(f) not in ai_cache}
                st.session_state.selected_batch_slides = unproc_fns
                for f_path in b_images:
                    f_name = os.path.basename(f_path)
                    st.session_state[f"chk_batch_{f_name}"] = (f_name in unproc_fns)
                st.rerun()
        with sel_bar2:
            if st.button("☑️ 全スライド選択", width="stretch"):
                st.session_state.selected_batch_slides = set(valid_fns)
                for f_name in valid_fns:
                    st.session_state[f"chk_batch_{f_name}"] = True
                st.rerun()
        with sel_bar3:
            if st.button("🔄 選択反転", width="stretch"):
                inverted = valid_fns - st.session_state.selected_batch_slides
                st.session_state.selected_batch_slides = inverted
                for f_name in valid_fns:
                    st.session_state[f"chk_batch_{f_name}"] = (f_name in inverted)
                st.rerun()
        with sel_bar4:
            if st.button("🗑️ 選択解除", width="stretch"):
                st.session_state.selected_batch_slides = set()
                for f_name in valid_fns:
                    st.session_state[f"chk_batch_{f_name}"] = False
                st.rerun()
        with sel_bar5:
            sel_cnt = len(st.session_state.selected_batch_slides)
            st.markdown(
                f"<div style='padding-top:6px; text-align:right; font-weight:bold; color:#f59e0b;'>🎯 バッチ選択中: {sel_cnt} 枚</div>",
                unsafe_allow_html=True
            )

        def make_chk_callback(slide_fn):
            def _cb():
                if st.session_state.get(f"chk_batch_{slide_fn}", False):
                    st.session_state.selected_batch_slides.add(slide_fn)
                else:
                    st.session_state.selected_batch_slides.discard(slide_fn)
            return _cb

        # サムネイル表示ループ（休止状態の破線・減光ビジュアル反映）
        cols_per_row = 4
        with st.container(height=420, border=True):
            for r_start in range(0, total_slides, cols_per_row):
                cols = st.columns(cols_per_row)
                for c_idx in range(cols_per_row):
                    idx = r_start + c_idx
                    if idx < total_slides:
                        p_img = b_images[idx]
                        p_fn = os.path.basename(p_img)
                        has_ai = "✅" if p_fn in ai_cache else "⚪"
                        is_selected = (idx == st.session_state.slide_idx)
                        is_batch_checked = (p_fn in st.session_state.selected_batch_slides)
                        mtime = os.path.getmtime(p_img) if os.path.exists(p_img) else 0
                        b64_str = get_thumb_b64(p_img, width=200, mtime=mtime)
                        
                        chk_key = f"chk_batch_{p_fn}"
                        if chk_key not in st.session_state:
                            st.session_state[chk_key] = is_batch_checked

                        # 休止フラグ判定
                        is_slide_suspended = notes_meta.get(p_fn, {}).get("is_suspended", False)

                        with cols[c_idx]:
                            if is_slide_suspended:
                                # 休止カード: 薄暗いグレー破線枠＋半透明化
                                img_border = "border: 2px dashed #94a3b8; opacity: 0.45; filter: grayscale(50%);"
                                status_icon = "⏸️"
                            elif is_batch_checked:
                                img_border = "border: 3px solid #f59e0b; box-shadow: 0 0 8px rgba(245, 158, 11, 0.45);"
                                status_icon = has_ai
                            else:
                                img_border = "border: 1px solid #475569;"
                                status_icon = has_ai
                            
                            st.markdown(
                                f"""<img id="slide-card-{idx}" src="data:image/jpeg;base64,{b64_str}" title="スライド #{idx + 1} ({p_fn})" style="width: 100%; height: auto; display: block; border-radius: 6px; margin-bottom: 4px; {img_border}">""",
                                unsafe_allow_html=True
                            )
                            
                            c_sub1, c_sub2 = st.columns([0.55, 2.45])
                            with c_sub1:
                                st.checkbox(
                                    "一括処理の対象",
                                    key=chk_key,
                                    on_change=make_chk_callback(p_fn),
                                    label_visibility="collapsed"
                                )
                            with c_sub2:
                                btn_label = f"#{idx + 1} {status_icon}"
                                if st.button(btn_label, key=f"slide_sel_{idx}", type="primary" if is_selected else "secondary", width="stretch"):
                                    st.session_state.slide_idx = idx
                                    st.rerun()

        # 自動スクロール追従スクリプト
        active_scroll_html = f"""
        <div style="display:none;">
            <script>
                (function() {{
                    const targetId = "slide-card-{st.session_state.slide_idx}";
                    const attemptScroll = () => {{
                        try {{
                            const targetEl = document.getElementById(targetId);
                            if (targetEl) {{
                                targetEl.scrollIntoView({{ behavior: "smooth", block: "nearest" }});
                            }}
                            document.querySelectorAll('div[data-testid="stCheckbox"] label').forEach(lbl => {{
                                if (!lbl.getAttribute('title')) {{
                                    lbl.setAttribute('title', '一括処理の対象');
                                }}
                            }});
                        }} catch (e) {{}}
                    }};
                    setTimeout(attemptScroll, 80);
                    setTimeout(attemptScroll, 250);
                }})();
            </script>
        </div>
        """
        st.html(active_scroll_html)

        # 3. ナビゲーションバー（スライド移動 ＋ [ ] ⏸️ 休止 ＋ [ ] 🎯 対象）
        target_img = b_images[st.session_state.slide_idx]
        fn = os.path.basename(target_img)
        is_cur_in_batch = (fn in st.session_state.selected_batch_slides)
        is_cur_suspended = notes_meta.get(fn, {}).get("is_suspended", False)

        nav_batch_key = f"nav_chk_toggle_{fn}"
        if nav_batch_key not in st.session_state:
            st.session_state[nav_batch_key] = is_cur_in_batch

        nav_suspend_key = f"nav_suspend_toggle_{fn}"
        if nav_suspend_key not in st.session_state:
            st.session_state[nav_suspend_key] = is_cur_suspended

        def on_nav_batch_toggle(slide_filename, widget_key):
            def _cb():
                val = st.session_state.get(widget_key, False)
                if val:
                    st.session_state.selected_batch_slides.add(slide_filename)
                    st.session_state[f"chk_batch_{slide_filename}"] = True
                else:
                    st.session_state.selected_batch_slides.discard(slide_filename)
                    st.session_state[f"chk_batch_{slide_filename}"] = False
            return _cb

        def on_nav_suspend_toggle(slide_filename, widget_key):
            def _cb():
                val = st.session_state.get(widget_key, False)
                gm_curr = get_guid_map()
                notes_dict = gm_curr.get("notes", {})
                if slide_filename not in notes_dict:
                    notes_dict[slide_filename] = {"b_img": slide_filename}
                notes_dict[slide_filename]["is_suspended"] = val
                gm_curr["notes"] = notes_dict
                save_guid_map(gm_curr)
            return _cb

        nav_1, nav_2, nav_chk_col1, nav_chk_col2, nav_3 = st.columns([1.1, 2.3, 0.85, 0.85, 1.1])
        with nav_1:
            if st.button("⬅️ 前のスライド", key="btn_prev_slide", disabled=(st.session_state.slide_idx == 0), width="stretch"):
                st.session_state.slide_idx -= 1
                st.rerun()
        with nav_2:
            st.markdown(f"<div style='text-align:center; padding-top:6px; font-weight:bold;'>スライド {st.session_state.slide_idx + 1} / {total_slides} : {fn}</div>", unsafe_allow_html=True)
        with nav_chk_col1:
            # 左側に「休止」
            st.checkbox(
                "⏸️ 休止",
                value=is_cur_suspended,
                key=nav_suspend_key,
                on_change=on_nav_suspend_toggle(fn, nav_suspend_key),
                help="Ankiインポート時に休止カード (queue = -1) に設定"
            )
        with nav_chk_col2:
            # 右側（次のスライドの直前）に頻用する「対象」
            st.checkbox(
                "🎯 対象",
                value=is_cur_in_batch,
                key=nav_batch_key,
                on_change=on_nav_batch_toggle(fn, nav_batch_key),
                help="一括AI生成の対象に含める"
            )
        with nav_3:
            if st.button("次のスライド ➡️", key="btn_next_slide", disabled=(st.session_state.slide_idx >= total_slides - 1), width="stretch"):
                st.session_state.slide_idx += 1
                st.rerun()

        st.markdown("---")

        # 4. スライド画像表示エリア（重複ボタンを廃止し、クリップボードコピーのみに集約）
        col_v1, col_v2 = st.columns([1, 1])
        with col_v1:
            st.image(target_img, caption=fn, width="stretch")
            if st.button("📋 画像をクリップボードにコピー", width="stretch"):
                if copy_image_to_clipboard_macos(target_img):
                    st.toast("✅ 画像をクリップボードにコピーしました！")
                else:
                    st.error("クリップボードへのコピーに失敗しました。")

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

            def on_set_svg_prompt():
                st.session_state.custom_prompt_val = SVG_OVERLAY_PROMPT

            pm_col1, pm_col2, pm_col3 = st.columns([2.2, 1, 1.1])
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
                    "🔄 Default",
                    on_click=on_reset_default,
                    help="通常の解説プロンプトに戻します",
                    width="stretch",
                )
            with pm_col3:
                st.button(
                    "🎯 SVG図示",
                    on_click=on_set_svg_prompt,
                    help="ホバーToggleボタン付きのSVGオーバーレイ図示プロンプトに切り替えます",
                    width="stretch",
                )

            cur_prompt_label = identify_prompt_label(st.session_state.custom_prompt_val)
            st.caption(f"現在のプロンプト設定: **{cur_prompt_label}**")

            custom_prompt = st.text_area(
                "プロンプト（Default または 🎯SVG図示 を選択・編集可能）",
                key="custom_prompt_val",
                height=110,
                label_visibility="collapsed",
            )

            sv_col1, sv_col2, sv_col3 = st.columns([2, 1, 1.2])
            with sv_col1:
                prompt_name = st.text_input("プロンプト保存名", placeholder="名前をつけて保存...", label_visibility="collapsed")
            with sv_col2:
                if st.button("💾 保存", width="stretch"):
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
                st.components.v1.html(copy_btn_html, height=42)

            model_labels = list(PRICING_MODELS.keys())
            default_inspect_idx = next((i for i, label in enumerate(model_labels) if "3.6 Flash" in label), 0)

            gen_c1, gen_c2 = st.columns([1.6, 1])
            with gen_c1:
                sel_inspect_label = st.selectbox(
                    "使用モデル（個別・選択バッチ共通）",
                    model_labels,
                    index=default_inspect_idx,
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

            if st.button("✨ 表示中の1枚のみAPI生成 (Gemini)", disabled=not api_key, width="stretch"):
                keep_recent_prompt(custom_prompt)
                with st.spinner(f"{fn} のAI解説を生成中..."):
                    try:
                        new_html = call_gemini_enhance(target_img, api_key, sel_inspect_model, custom_prompt)
                        ai_cache[fn] = new_html
                        save_ai_cache(ai_cache)
                        st.session_state.pop(f"edit_html_{fn}", None)
                        st.success("更新しました！")
                        st.rerun()
                    except Exception as e:
                        st.error(f"API通信エラーが発生しました: {e}")

            st.markdown("##### 🚀 選択したスライドをまとめて並列API生成")
            selected_paths = [p for p in b_images if os.path.basename(p) in st.session_state.selected_batch_slides]
            sel_count = len(selected_paths)
            batch_cost_jpy = single_cost_jpy * sel_count
            batch_cost_usd = single_cost_usd * sel_count

            b_col1, b_col2 = st.columns([1.2, 1.8])
            with b_col1:
                max_workers = st.slider("同時並列数", min_value=1, max_value=5, value=3, help="並列数を上げると待ち時間を大幅に短縮できます（推奨: 3並列）")
            with b_col2:
                st.info(f"対象: **{sel_count} 枚** ｜ 推定合計: **約 {batch_cost_jpy:.2f} 円** (`${batch_cost_usd:.4f}`)")

            if st.button(
                f"🚀 選択した {sel_count} 枚をAPI一括生成 ({max_workers}並列) ➔ 確認画面へ",
                type="primary",
                disabled=(sel_count == 0 or not api_key),
                width="stretch"
            ):
                confirm_batch_execution_dialog(
                    selected_paths=selected_paths,
                    all_b_images=b_images,
                    prompt_text=custom_prompt,
                    model_label=sel_inspect_label,
                    model_id=sel_inspect_model,
                    max_workers=max_workers,
                    est_cost_jpy=batch_cost_jpy,
                    est_cost_usd=batch_cost_usd,
                    api_key=api_key,
                )
            
            # --- 選択したスライドの一括休止 / 休止解除操作エリア ---
            st.markdown("<div style='height: 6px;'></div>", unsafe_allow_html=True)
            col_sus1, col_sus2 = st.columns(2)
            with col_sus1:
                if st.button(
                    f"⏸️ 選択した {sel_count} 枚を一括休止",
                    disabled=(sel_count == 0),
                    width="stretch",
                    help="選択中のスライドをすべて Anki 休止カード (queue = -1) に設定します"
                ):
                    gm_curr = get_guid_map()
                    notes_dict = gm_curr.get("notes", {})
                    target_b_names = {os.path.basename(p) for p in selected_paths}

                    for b_fn in target_b_names:
                        if b_fn not in notes_dict:
                            inferred_f = re.sub(r'b\.(jpg|jpeg|png)$', r'a.\1', b_fn, flags=re.IGNORECASE)
                            notes_dict[b_fn] = {"b_img": b_fn, "f_img": inferred_f}
                        # ★確実に休止フラグを True にセット
                        notes_dict[b_fn]["is_suspended"] = True

                    gm_curr["notes"] = notes_dict
                    save_guid_map(gm_curr)
                    st.toast(f"⏸️ 選択した {len(target_b_names)} 枚を一括休止に設定しました")
                    st.rerun()

            with col_sus2:
                if st.button(
                    f"▶️ 選択した {sel_count} 枚の休止を解除",
                    disabled=(sel_count == 0),
                    width="stretch",
                    help="選択中のスライドの休止設定をすべて解除（有効化）します"
                ):
                    gm_curr = get_guid_map()
                    notes_dict = gm_curr.get("notes", {})
                    target_b_names = {os.path.basename(p) for p in selected_paths}

                    for b_fn in target_b_names:
                        if b_fn not in notes_dict:
                            inferred_f = re.sub(r'b\.(jpg|jpeg|png)$', r'a.\1', b_fn, flags=re.IGNORECASE)
                            notes_dict[b_fn] = {"b_img": b_fn, "f_img": inferred_f}
                        # ★確実に休止フラグを False にセット
                        notes_dict[b_fn]["is_suspended"] = False

                    gm_curr["notes"] = notes_dict
                    save_guid_map(gm_curr)
                    st.toast(f"▶️ 選択した {len(target_b_names)} 枚の休止を解除しました")
                    st.rerun()

        # ----------------------------------------------------------------------
        # プレビュー表示エリア (components.v1.html による完全隔離レンダリング)
        # ----------------------------------------------------------------------
        st.markdown("---")
        st.markdown("#### プレビュー (Anki Back面)")

        # 休止状態の視覚的強調バナー
        is_cur_suspended = notes_meta.get(fn, {}).get("is_suspended", False)
        if is_cur_suspended:
            st.markdown("""
            <div style="background: repeating-linear-gradient(45deg, #334155, #334155 10px, #1e293b 10px, #1e293b 20px); 
                        border: 1px dashed #94a3b8; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px; color: #f8fafc; font-size: 13px; font-weight: bold; display: flex; align-items: center; gap: 8px;">
                <span>⏸️ 【休止設定中 (Suspended)】 このスライドはAnkiインポート時に休止カード（queue = -1）として取り込まれます。</span>
            </div>
            """, unsafe_allow_html=True)

        if fn in ai_cache:
            preview_html_body = ai_cache[fn]
            has_svg_overlay = ("<svg" in preview_html_body and "<img" in preview_html_body)
            
            # SLIDE_IMAGE.jpg を実スライドBase64画像へ置換
            if has_svg_overlay and os.path.exists(target_img):
                with open(target_img, "rb") as img_f:
                    full_b64 = base64.b64encode(img_f.read()).decode("utf-8")
                preview_html_body = re.sub(
                    r'(<img\s+[^>]*src=["\'])([^"\']+)(["\'])',
                    rf'\g<1>data:image/jpeg;base64,{full_b64}\g<3>',
                    preview_html_body,
                    count=1
                )

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
                /* SVGオーバーレイ位置ずれ完全抑制リセット */
                .svg-overlay-wrap {{
                    position: relative !important;
                    display: block !important;
                    width: 100% !important;
                    max-width: 850px !important;
                    margin: 0 auto 16px auto !important;
                    padding: 0 !important;
                    line-height: 0 !important;
                    font-size: 0 !important;
                }}
                .svg-overlay-wrap img {{
                    width: 100% !important;
                    height: auto !important;
                    display: block !important;
                    margin: 0 !important;
                    padding: 0 !important;
                    border: none !important;
                }}
                .svg-overlay-wrap svg {{
                    position: absolute !important;
                    top: 0 !important;
                    left: 0 !important;
                    width: 100% !important;
                    height: 100% !important;
                    pointer-events: none !important;
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
                    {preview_html_body}
                </div>
            </div>
            """
            st.components.v1.html(preview_style, height=620 if has_svg_overlay else 450, scrolling=True)
        else:
            st.info("このスライドにはまだAI解説がありません。APIで生成するか、下のテキストエリアから手動入力してください。")

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
                    help="API生成されたHTMLソースコードの微修正や、外部LLMで作成したHTMLの貼り付け・保存が行えます。"
                )
            with edit_col2:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("💾 HTMLを手動保存", type="primary", width="stretch"):
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
                    if st.button("🗑️ この解説を消去", width="stretch"):
                        del ai_cache[fn]
                        save_ai_cache(ai_cache)
                        st.session_state.pop(edit_key, None)
                        st.info("AI解説を消去しました。")
                        st.rerun()


# ==============================================================================
# タブ 4: Ankiデッキパッケージ (.apkg) 出力
# ==============================================================================
with tab_build:
    st.subheader("Ankiデッキパッケージ (.apkg) 出力")
    
    deck_full_name = st.session_state.get("deck_name_val", "")
    st.write(f"出力先デッキ名: **{deck_full_name}**")
    
    bf = st.session_state.get("base_folder_val", "").strip()
    target_dir = bf if (bf and os.path.exists(bf)) else "."
    display_dir = os.path.abspath(target_dir)
    st.info(f"📁 出力先フォルダ: `{display_dir}`")

    # 現在の対象画像群における「休止（is_suspended=True）」の枚数を集計
    current_b_names = {os.path.basename(f) for f in b_images}
    gm_latest = get_guid_map().get("notes", {})
    suspended_count = sum(
        1 for b_fn in current_b_names
        if gm_latest.get(b_fn, {}).get("is_suspended", False)
    )

    if suspended_count > 0:
        st.warning(
            f"⚠️ **休止設定のスライドが {suspended_count} 枚含まれています（`queue = -1`）**\n\n"
            f"Ankiで `.apkg` を読み込む際、インポート画面の **「学習進捗も読み込む（Import any learning progress）」** の設定に注意してください：\n"
            f"- **【新規デッキの初回インポート時】**: **ON（青色）** にして読み込んでください。これにより休止設定がそのまま反映されます。\n"
            f"- **【すでに学習中の既存デッキへの再インポート時】**: 学習履歴を守るため **OFF（灰色）** のまま読み込んでください（※ONにすると既存の復習期日がリセットされます）。"
        )
    
    col_out1, col_out2 = st.columns([1, 1])
    with col_out1:
        if st.button("📦 .apkg パッケージを作成", type="primary"):
            with st.spinner("パッケージング中..."):
                apkg_path, count = build_apkg(
                    DEST_DIR, 
                    deck_full_name, 
                    st.session_state.get("group_title_val", ""), 
                    get_ai_cache(), 
                    output_dir=target_dir
                )
                st.success(f"完成: {count} 枚のカードを含む '{os.path.basename(apkg_path)}' を保存しました！")
                st.caption(f"保存場所: `{apkg_path}`")
                
                if suspended_count > 0:
                    st.error(
                        f"🚨 **重要（休止カード {suspended_count} 枚を含む）**: "
                        f"初回取り込み時は Anki の **「学習進捗も読み込む」を ON（青）** に設定してください。"
                    )
                else:
                    st.info("※決定論的GUIDと正統Model IDにより、Ankiに再インポートしても学習進捗を維持したまま上書きされます。")

    with col_out2:
        if st.button("🗑️ 中間JPEG画像フォルダを全削除 (クリーンアップ)"):
            all_jpgs = glob.glob(os.path.join(DEST_DIR, "*.jpg"))
            for f in all_jpgs:
                os.remove(f)
            st.success(f"{len(all_jpgs)} 枚のJPEGを削除しました。")