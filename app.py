import streamlit as st
import os
import re
import glob
import base64
import subprocess
import shutil
import genanki
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

from config import (
    FOLDER_CONFIG_FILENAME,
    DEST_DIR,
    PRICING_MODELS,
    USD_JPY,
    DEFAULT_PROMPT,
    SVG_OVERLAY_PROMPT,
    DYNAMIC_CLOZE_CSS,
    WEB_LLM_MARKDOWN_PROMPT,
)
from storage import (
    get_last_folder,
    save_last_folder,
    infer_unmasked_from_masked,
    auto_detect_pdfs,
    load_folder_config,
    save_folder_config,
    load_guid_map_from_dir,
    save_guid_map_to_dir,
    load_ai_cache_from_dir,
    save_ai_cache_to_dir,
    load_summary_cloze_from_dir,
    save_summary_cloze_to_dir,
    get_prompt_store,
    save_prompt_store,
    keep_recent_prompt,
    identify_prompt_label,
    load_api_usage_stats,
    get_summary_cloze_filepath,
)
from gemini_client import (
    call_gemini_enhance,
    call_gemini_summary_back_explanation,
)
from anki_io import (
    get_poppler_path,
    extract_pdf_pages,
    restore_from_anki_export,
    build_apkg,
    strip_embedded_style,
    renumber_cloze_spans,
    convert_markers_to_cloze,
    parse_bulk_summary_input,
)
from ui_helpers import (
    copy_image_to_clipboard_macos,
    choose_folder_dialog,
    choose_file_dialog,
    get_thumb_b64,
    render_clipboard_button,
    confirm_batch_execution_dialog,
)

# ==============================================================================
# 環境依存バイナリ（Poppler）チェック
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
# セッション状態管理ヘルパー
# ==============================================================================
def get_active_work_dir():
    bf = st.session_state.get("base_folder_val", "").strip()
    if bf and os.path.isdir(bf):
        return bf
    return None


def safe_set_state(key, value):
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
# Streamlit UI アプリケーション初期化
# ==============================================================================
st.set_page_config(page_title="Lecture2Anki + Dynamic Cloze", layout="wide")
st.title("医学部講義PDF ➔ Ankiカード & ダイナミックCloze 生成システム")

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
# サイドバー: 講義設定 & デッキ名自動合成 & API実績モニター
# ==============================================================================
with st.sidebar:
    st.header("講義フォルダ・デッキ設定")

    st.markdown("**📂 講義フォルダ (3つのJSON保存先)**")
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
        "親Deck名 (例: ブロック1_呼吸器疾患)",
        key="parent_deck_input",
        on_change=on_deck_naming_change,
        placeholder="ブロック1_呼吸器疾患"
    )

    group_title = st.text_input(
        "グループタイトル (例: 14 呼吸器病治療薬)",
        key="group_title_input",
        on_change=on_deck_naming_change,
        placeholder="14 呼吸器病治療薬"
    )

    effective_deck = st.session_state.get("deck_name_val", "")
    st.text_input("生成されるDeck名 (完全名)", value=effective_deck, disabled=True)

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

    u_stats = load_api_usage_stats()
    st.markdown("**📊 API実測コスト累計（ローカル記録）**")
    st.caption(
        f"実行回数: **{u_stats['count']} 回** ｜ "
        f"総トークン: **{u_stats['total_tokens']:,}**\n\n"
        f"概算累計請求額: **約 {u_stats['total_jpy']:.1f} 円**"
    )
    if u_stats["recent"]:
        with st.expander("直近の実測トークン履歴 (最新10件)", expanded=False):
            for r in u_stats["recent"]:
                mode_tag = "🎯SVG" if r.get("is_svg") else "標準"
                st.markdown(
                    f"<div style='font-size:11px; border-bottom:1px solid #334155; padding:4px 0;'>"
                    f"<b>{r['image']}</b> ({mode_tag})<br>"
                    f"In:{r['prompt_tokens']:,} / Out:{r['output_tokens']:,} / Think:{r['thought_tokens']:,} ➔ <b>{r['cost_jpy']:.2f}円</b>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

# セッション状態初期化
if "slide_idx" not in st.session_state:
    st.session_state.slide_idx = 0
if "summary_idx" not in st.session_state:
    st.session_state.summary_idx = 0
if "custom_prompt_val" not in st.session_state:
    st.session_state.custom_prompt_val = DEFAULT_PROMPT
if "selected_batch_slides" not in st.session_state:
    st.session_state.selected_batch_slides = set()

active_work_dir = get_active_work_dir()
all_b_images = sorted(glob.glob(os.path.join(DEST_DIR, "*b.jpg")))
guid_map_data = load_guid_map_from_dir(active_work_dir)
notes_meta = guid_map_data.get("notes", {})
b_images = []

if all_b_images:
    latest_ts = sorted({os.path.basename(f)[:17] for f in all_b_images})[-1]
    b_images = [f for f in all_b_images if os.path.basename(f).startswith(latest_ts)]

gm_init = load_guid_map_from_dir(active_work_dir)
active_deck_name = (
    gm_init.get("deck_name", "").strip()
    if (gm_init.get("is_restored_from_anki") and gm_init.get("deck_name", "").strip())
    else st.session_state.get("deck_name_val", "").strip()
)

ai_cache = load_ai_cache_from_dir(active_work_dir)
summary_cards = load_summary_cloze_from_dir(active_work_dir, deck_name=active_deck_name)

# ==============================================================================
# メイン画面タブ構成（4タブ構成）
# ==============================================================================
tab_extract, tab_ai_inspect, tab_summary_cloze, tab_build = st.tabs([
    "1. PDF指定 & 抽出 / 復元",
    "2. 🔍 個別スライド解説 & 休止管理",
    "3. ⚡ 要約圧縮ダイナミックCloze",
    "4. 📦 Ankiパッケージ出力",
])


# ==============================================================================
# タブ 1: PDFファイルの指定とJPEG書き出し ＆ 既存Ankiデッキ復元
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
                key="unmasked_pdf_val",
                on_change=on_unmasked_input_change,
                label_visibility="collapsed"
            )

    pages_str = "all"

    col_btn1, col_btn2 = st.columns([1.2, 1])
    with col_btn1:
        preserve_existing_opt = st.checkbox(
            "♻️ 既存のAI解説・GUID・休止状態をページ番号順で引き継ぐ（講義前にUnmaskedで解説作成 ➔ 講義後にMasked差し替え時に推奨）",
            value=True,
            key="chk_preserve_existing_on_extract",
        )
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
                    ts, cnt, mig_cnt = extract_pdf_pages(
                        unmasked_pdf,
                        masked_pdf if mode == "事前Cloze済み (Masked PDFあり)" else None,
                        pages_str,
                        pdf_pass,
                        dest_dir=DEST_DIR,
                        work_dir=get_active_work_dir(),
                        preserve_existing=preserve_existing_opt,
                    )
                    st.session_state.selected_batch_slides = set()
                    mig_msg = f"（既存のAI解説 {mig_cnt} 件・GUIDを引き継ぎました）" if mig_cnt > 0 else ""
                    st.success(f"完了: {cnt} ページ分の画像を書き出しました。{mig_msg}")
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
    st.caption("すでにAnkiに登録済みのカードから、スライド画像・GUID・個別AI解説・要約Clozeカードを講義フォルダの3つのJSONへ自動振り分け復元します。")

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

        txt_dir = os.path.dirname(txt_p)
        if not get_active_work_dir() and txt_dir and os.path.isdir(txt_dir):
            safe_set_state("base_folder_val", txt_dir)

        try:
            res = restore_from_anki_export(
                txt_p,
                med_d if med_d else None,
                DEST_DIR,
                work_dir=get_active_work_dir() or txt_dir,
            )
            st.cache_data.clear()
            st.session_state.slide_idx = 0
            st.session_state.summary_idx = 0
            st.session_state.selected_batch_slides = set()

            if res.get("deck_name"):
                d_name = res["deck_name"]
                safe_set_state("deck_name_val", d_name)
                if "::" in d_name:
                    p, g = d_name.split("::", 1)
                    safe_set_state("parent_deck_val", p.strip())
                    safe_set_state("parent_deck_input", p.strip())
                    safe_set_state("group_title_val", g.strip())
                    safe_set_state("group_title_input", g.strip())
                else:
                    safe_set_state("group_title_val", d_name)
                    safe_set_state("group_title_input", d_name)

            st.session_state.restore_msg = ("success", res)
        except Exception as e:
            st.session_state.restore_msg = ("error", f"復元中にエラーが発生しました: {e}")

    st.button(
        "♻️ Ankiエクスポートから画像・既存AI解説・要約Cloze・GUIDを復元",
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
                f"✅ 復元成功！ スライドノート: {m_val['restored_notes']} 枚 / "
                f"個別AI解説: {m_val['restored_ai']} 件 / "
                f"要約Clozeカード: {m_val.get('restored_summary', 0)} 枚 / "
                f"コピー画像: {m_val['copied_images']} 枚"
            )
            if m_val["missing_images"]:
                st.warning(f"一部メディアが見つかりませんでした: {len(m_val['missing_images'])} 件")


# ==============================================================================
# タブ 2: 個別スライド確認 & 複数選択バッチ並列生成・休止管理
# ==============================================================================
with tab_ai_inspect:
    st.subheader("スライド確認・個別AI解説生成 & 休止管理")
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

                        is_slide_suspended = notes_meta.get(p_fn, {}).get("is_suspended", False)

                        with cols[c_idx]:
                            if is_slide_suspended:
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

        # ナビゲーションバー
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
                gm_curr = load_guid_map_from_dir(active_work_dir)
                notes_dict = gm_curr.get("notes", {})
                if slide_filename not in notes_dict:
                    inferred_f = re.sub(r'b\.(jpg|jpeg|png)$', r'a.\1', slide_filename, flags=re.IGNORECASE)
                    notes_dict[slide_filename] = {"b_img": slide_filename, "f_img": inferred_f}
                notes_dict[slide_filename]["is_suspended"] = val
                gm_curr["notes"] = notes_dict
                save_guid_map_to_dir(gm_curr, active_work_dir)
            return _cb

        nav_1, nav_2, nav_chk_col1, nav_chk_col2, nav_3 = st.columns([1.1, 2.3, 0.85, 0.85, 1.1])
        with nav_1:
            if st.button("⬅️ 前のスライド", key="btn_prev_slide", disabled=(st.session_state.slide_idx == 0), width="stretch"):
                st.session_state.slide_idx -= 1
                st.rerun()
        with nav_2:
            st.markdown(f"<div style='text-align:center; padding-top:6px; font-weight:bold;'>スライド {st.session_state.slide_idx + 1} / {total_slides} : {fn}</div>", unsafe_allow_html=True)
        with nav_chk_col1:
            st.checkbox(
                "⏸️ 休止",
                key=nav_suspend_key,
                on_change=on_nav_suspend_toggle(fn, nav_suspend_key),
                help="Ankiインポート時に休止カード (queue = -1) に設定"
            )
        with nav_chk_col2:
            st.checkbox(
                "🎯 対象",
                key=nav_batch_key,
                on_change=on_nav_batch_toggle(fn, nav_batch_key),
                help="一括AI生成・一括休止の対象に含める"
            )
        with nav_3:
            if st.button("次のスライド ➡️", key="btn_next_slide", disabled=(st.session_state.slide_idx >= total_slides - 1), width="stretch"):
                st.session_state.slide_idx += 1
                st.rerun()

        st.markdown("---")

        # スライド画像表示 & AI生成操作エリア
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
                st.button("🔄 Default", on_click=on_reset_default, width="stretch")
            with pm_col3:
                st.button("🎯 SVG図示", on_click=on_set_svg_prompt, width="stretch")

            cur_prompt_label = identify_prompt_label(st.session_state.custom_prompt_val)
            st.caption(f"現在のプロンプト設定: **{cur_prompt_label}**")

            custom_prompt = st.text_area(
                "プロンプト",
                key="custom_prompt_val",
                height=110,
                label_visibility="collapsed",
            )

            sv_col1, sv_col2, sv_col3 = st.columns([2, 0.9, 1.3])
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
                render_clipboard_button(custom_prompt, label="📋 コピー")

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

            u_stats_now = load_api_usage_stats()
            is_svg_now = "svg-overlay-wrap" in custom_prompt
            est_in = u_stats_now["avg_in_svg"] if is_svg_now else u_stats_now["avg_in_std"]
            est_out = u_stats_now["avg_out_svg"] if is_svg_now else u_stats_now["avg_out_std"]

            single_in_cost = (est_in / 1_000_000) * chosen_spec["in"]
            single_out_cost = (est_out / 1_000_000) * chosen_spec["out"]
            single_cost_usd = single_in_cost + single_out_cost
            single_cost_jpy = single_cost_usd * USD_JPY

            with gen_c2:
                st.caption(f"💰 1枚推定: **約 {single_cost_jpy:.2f} 円** (`${single_cost_usd:.4f}`)")

            if st.button("✨ 表示中の1枚のみAPI生成 (Gemini)", disabled=not api_key, width="stretch"):
                keep_recent_prompt(custom_prompt)
                with st.spinner(f"{fn} のAI解説を生成中..."):
                    try:
                        new_html, u_rec = call_gemini_enhance(target_img, api_key, sel_inspect_model, custom_prompt, return_usage=True)
                        ai_cache[fn] = new_html
                        save_ai_cache_to_dir(ai_cache, active_work_dir)
                        st.session_state.pop(f"edit_html_{fn}", None)
                        st.toast(
                            f"✅ 生成完了: 約 {u_rec['cost_jpy']:.2f} 円 "
                            f"(入力:{u_rec['prompt_tokens']:,} / 出力:{u_rec['output_tokens']:,} / 思考:{u_rec['thought_tokens']:,})"
                        )
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
                max_workers = st.slider("同時並列数", min_value=1, max_value=5, value=3)
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
                    work_dir=active_work_dir,
                )

            st.markdown("<div style='height: 6px;'></div>", unsafe_allow_html=True)
            col_sus1, col_sus2 = st.columns(2)
            with col_sus1:
                if st.button(f"⏸️ 選択した {sel_count} 枚を一括休止", disabled=(sel_count == 0), width="stretch"):
                    gm_curr = load_guid_map_from_dir(active_work_dir)
                    notes_dict = gm_curr.get("notes", {})
                    target_b_names = {os.path.basename(p) for p in selected_paths}
                    for b_fn in target_b_names:
                        if b_fn not in notes_dict:
                            inferred_f = re.sub(r'b\.(jpg|jpeg|png)$', r'a.\1', b_fn, flags=re.IGNORECASE)
                            notes_dict[b_fn] = {"b_img": b_fn, "f_img": inferred_f}
                        notes_dict[b_fn]["is_suspended"] = True
                    gm_curr["notes"] = notes_dict
                    save_guid_map_to_dir(gm_curr, active_work_dir)
                    st.toast(f"⏸️ 選択した {len(target_b_names)} 枚を一括休止に設定しました")
                    st.rerun()

            with col_sus2:
                if st.button(f"▶️ 選択した {sel_count} 枚の休止を解除", disabled=(sel_count == 0), width="stretch"):
                    gm_curr = load_guid_map_from_dir(active_work_dir)
                    notes_dict = gm_curr.get("notes", {})
                    target_b_names = {os.path.basename(p) for p in selected_paths}
                    for b_fn in target_b_names:
                        if b_fn not in notes_dict:
                            inferred_f = re.sub(r'b\.(jpg|jpeg|png)$', r'a.\1', b_fn, flags=re.IGNORECASE)
                            notes_dict[b_fn] = {"b_img": b_fn, "f_img": inferred_f}
                        notes_dict[b_fn]["is_suspended"] = False
                    gm_curr["notes"] = notes_dict
                    save_guid_map_to_dir(gm_curr, active_work_dir)
                    st.toast(f"▶️ 選択した {len(target_b_names)} 枚の休止を解除しました")
                    st.rerun()

        # プレビュー表示エリア
        st.markdown("---")
        st.markdown("#### プレビュー (Anki Back面)")

        is_cur_suspended = notes_meta.get(fn, {}).get("is_suspended", False)
        if is_cur_suspended:
            st.markdown("""
            <div style="background: repeating-linear-gradient(45deg, #334155, #334155 10px, #1e293b 10px, #1e293b 20px); 
                        border: 1px dashed #94a3b8; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px; color: #f8fafc; font-size: 13px; font-weight: bold;">
                <span>⏸️ 【休止設定中 (Suspended)】 このスライドはAnkiインポート時に休止カード（queue = -1）として取り込まれます。</span>
            </div>
            """, unsafe_allow_html=True)

        if fn in ai_cache:
            preview_html_body = ai_cache[fn]
            has_svg_overlay = ("<svg" in preview_html_body and "<img" in preview_html_body)

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
        st.markdown("#### 📝 AI解説HTMLの直接編集・貼り付け")

        edit_key = f"edit_html_{fn}"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = ai_cache.get(fn, "")

        with st.container(border=True):
            edit_col1, edit_col2 = st.columns([4, 1])
            with edit_col1:
                edited_html = st.text_area("解説HTMLソースコード", key=edit_key, height=220)
            with edit_col2:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("💾 HTMLを手動保存", type="primary", width="stretch"):
                    clean_text = strip_embedded_style(edited_html)
                    ai_cache[fn] = clean_text
                    save_ai_cache_to_dir(ai_cache, active_work_dir)
                    st.session_state.pop(edit_key, None)
                    st.toast("✅ HTML解説を保存し、プレビューを更新しました！")
                    st.rerun()

                if fn in ai_cache:
                    if st.button("🗑️ この解説を消去", width="stretch"):
                        del ai_cache[fn]
                        save_ai_cache_to_dir(ai_cache, active_work_dir)
                        st.session_state.pop(edit_key, None)
                        st.info("AI解説を消去しました。")
                        st.rerun()
    else:
        st.info("スライド画像がまだありません。タブ1でPDFから抽出するか、既存Ankiデッキから復元してください。")


# ==============================================================================
# タブ 3: ⚡ 要約圧縮ダイナミックCloze（Markdown / HTML 決定論的変換 ＆ <u>...</u> 特化）
# ==============================================================================
with tab_summary_cloze:
    st.subheader("⚡ 要約圧縮ダイナミックCloze (Medical Dynamic Cloze Model 3F)")
    st.caption("Markdown または HTML に `<u>語句</u>` を付けて貼り付けると、全チャプターを一括分割・Cloze化し、同時に裏面（Back）のAI補足解説も並列自動生成します。")
    cur_sc_json_name = os.path.basename(get_summary_cloze_filepath(active_work_dir, active_deck_name))
    st.caption(f"保存先JSON: `{cur_sc_json_name}`")

    def _batch_generate_all_backs(cards_list, target_model_id, deck_title_label):
        """全チャプターの Back 補足解説を並列API生成して保存する"""
        total_c = len(cards_list)
        pbar = st.progress(0)
        status_txt = st.empty()
        total_jpy = 0.0
        errors = []
        done = 0

        def _job(idx_and_card):
            i_pos, c_obj = idx_and_card
            exp_html, u_rec = call_gemini_summary_back_explanation(
                c_obj.get("html", ""),
                api_key,
                target_model_id,
                deck_title=deck_title_label,
            )
            return i_pos, exp_html, u_rec

        with ThreadPoolExecutor(max_workers=min(4, max(1, total_c))) as ex:
            futures = {ex.submit(_job, (i, c)): i for i, c in enumerate(cards_list)}
            for fut in as_completed(futures):
                try:
                    i_pos, exp_html, u_rec = fut.result()
                    cards_list[i_pos]["back_extra"] = exp_html
                    total_jpy += u_rec["cost_jpy"]
                    ch_no = cards_list[i_pos].get("chapter_idx", i_pos + 1)
                    st.session_state.pop(f"edit_sum_back_{ch_no}", None)
                except Exception as exc:
                    errors.append(str(exc))
                done += 1
                pbar.progress(done / total_c)
                status_txt.text(f"Back解説を並列生成中 ({done}/{total_c} 完了) ｜ 累計実測: 約 {total_jpy:.2f} 円")

        save_summary_cloze_to_dir(cards_list, active_work_dir, deck_name=active_deck_name)
        return total_jpy, errors

    with st.container(border=True):
        st.markdown("##### 📥 1. Markdown / HTML 一括貼り付け取り込み（`<u>語句</u>` ➔ Cloze化 ＋ Back解説自動生成）")

        opt_c1, opt_c2 = st.columns([2.2, 1.8])
        with opt_c1:
            auto_gen_back = st.checkbox(
                "✨ 取り込みと同時に全チャプターの Back 解説を並列API生成する",
                value=bool(api_key),
                disabled=not api_key,
            )
        with opt_c2:
            model_labels_sc = list(PRICING_MODELS.keys())
            def_sc_idx = next((i for i, lbl in enumerate(model_labels_sc) if "3.6 Flash" in lbl), 0)
            sel_sc_label = st.selectbox(
                "Back解説生成モデル",
                model_labels_sc,
                index=def_sc_idx,
                key="sel_sc_back_model",
                label_visibility="collapsed",
            )
            sc_back_model_id = PRICING_MODELS[sel_sc_label]["id"]

        bulk_col1, bulk_col2 = st.columns([4, 1])
        with bulk_col1:
            bulk_text_input = st.text_area(
                "Markdown または HTML をここにペースト",
                key="bulk_summary_text_input",
                height=190,
                placeholder="# 1. 気管支平滑筋の収縮と弛緩\n\n> **気道の防御機構：** 肺でのガス交換を維持するため...\n\n| 状態 | 主な刺激・受容体 | 細胞内シグナル |\n| :--- | :--- | :--- |\n| 気管支収縮 | ACh（<u>M<sub>3</sub>受容体</u>） | <u>G<sub>q</sub></u>活性化 → <u>PLC</u>活性化 |",
                label_visibility="collapsed",
            )
        with bulk_col2:
            if st.button("📥 一括分割して取り込む", type="primary", width="stretch"):
                if not bulk_text_input.strip():
                    st.warning("Markdown または HTML テキストを貼り付けてください。")
                else:
                    deck_title_str = st.session_state.get("group_title_val") or st.session_state.get("deck_name_val") or "講義要約"
                    parsed_list = parse_bulk_summary_input(
                        bulk_text_input,
                        deck_title=deck_title_str,
                        existing_cards=summary_cards,
                    )
                    if parsed_list:
                        save_summary_cloze_to_dir(parsed_list, active_work_dir, deck_name=active_deck_name)
                        for c in parsed_list:
                            st.session_state.pop(f"edit_sum_html_{c['chapter_idx']}", None)
                            st.session_state.pop(f"edit_sum_title_{c['chapter_idx']}", None)
                            st.session_state.pop(f"edit_sum_back_{c['chapter_idx']}", None)
                        st.session_state.summary_idx = 0

                        if auto_gen_back and api_key:
                            cost_jpy, errs = _batch_generate_all_backs(parsed_list, sc_back_model_id, deck_title_str)
                            if errs:
                                st.error("一部のBack解説生成でエラー: " + " / ".join(errs))
                            else:
                                st.toast(f"✅ 全 {len(parsed_list)} ChのCloze化＆Back解説生成が完了！（約 {cost_jpy:.2f} 円）")
                        else:
                            st.toast(f"✅ 全 {len(parsed_list)} チャプターを変換して登録しました！")
                        st.rerun()
                    else:
                        st.error("有効なチャプターデータを検出できませんでした。")

            if summary_cards:
                if st.button(
                    f"🚀 既存全{len(summary_cards)}ChのBackを一括生成",
                    disabled=not api_key,
                    width="stretch",
                    help="すでに登録済みの全チャプター（古い8枚など）に対してBack解説を一括生成します",
                ):
                    deck_title_str = st.session_state.get("group_title_val") or st.session_state.get("deck_name_val") or "講義要約"
                    cost_jpy, errs = _batch_generate_all_backs(summary_cards, sc_back_model_id, deck_title_str)
                    if errs:
                        st.error("一部のBack解説生成でエラー: " + " / ".join(errs))
                    else:
                        st.toast(f"✅ 全 {len(summary_cards)} ChのBack解説を一括生成しました！（約 {cost_jpy:.2f} 円）")
                    st.rerun()

            if st.button("➕ 空のChを1枚追加", width="stretch"):
                next_ch = (max([c.get("chapter_idx", 0) for c in summary_cards]) + 1) if summary_cards else 1
                deck_title_str = st.session_state.get("group_title_val") or st.session_state.get("deck_name_val") or "講義要約"
                new_guid = genanki.guid_for(f"{deck_title_str}_summary_ch{next_ch:02d}")
                template_initial = (
                    f'<div class="meta-header">{deck_title_str}（Chapter {next_ch}）</div>\n'
                    f'<div class="question-box">{next_ch}. 新規チャプタータイトル</div>\n'
                    f'<div class="feature-box"><b>要点：</b>ここに解説と <u>重要語句</u> を記述してください。</div>'
                )
                summary_cards.append({
                    "guid": new_guid,
                    "chapter_idx": next_ch,
                    "title": f"{next_ch}. 新規チャプター",
                    "html": convert_markers_to_cloze(template_initial),
                    "back_extra": "",
                    "is_suspended": False,
                    "tags": [],
                })
                save_summary_cloze_to_dir(summary_cards, active_work_dir, deck_name=active_deck_name)
                st.session_state.summary_idx = len(summary_cards) - 1
                st.rerun()

    # --------------------------------------------------------------------------
    # 2. 登録済み要約Clozeカードのプレビュー＆個別編集セクション
    # --------------------------------------------------------------------------
    st.markdown("---")
    if summary_cards:
        total_sum = len(summary_cards)
        st.session_state.summary_idx = max(0, min(st.session_state.summary_idx, total_sum - 1))

        st.markdown(f"##### 📑 2. 要約Clozeカード一覧 ({total_sum} 枚) ＆ プレビュー・HTML編集")
        ch_cols = st.columns(min(total_sum, 8))
        for idx, c_item in enumerate(summary_cards):
            col_target = ch_cols[idx % len(ch_cols)]
            with col_target:
                ch_num = c_item.get("chapter_idx", idx + 1)
                sus_mark = "⏸️" if c_item.get("is_suspended") else "⚡"
                is_cur_sc = (idx == st.session_state.summary_idx)
                if st.button(
                    f"Ch.{ch_num} {sus_mark}",
                    key=f"btn_sel_sum_{idx}",
                    type="primary" if is_cur_sc else "secondary",
                    width="stretch",
                ):
                    st.session_state.summary_idx = idx
                    st.rerun()

        cur_card = summary_cards[st.session_state.summary_idx]
        cur_ch_idx = cur_card.get("chapter_idx", st.session_state.summary_idx + 1)

        s_nav1, s_nav2, s_nav3, s_nav4, s_nav5 = st.columns([1.1, 2.5, 1.0, 1.1, 1.1])
        with s_nav1:
            if st.button("⬅️ 前のCh", disabled=(st.session_state.summary_idx == 0), width="stretch"):
                st.session_state.summary_idx -= 1
                st.rerun()
        with s_nav2:
            st.markdown(
                f"<div style='text-align:center; padding-top:6px; font-weight:bold;'>"
                f"Chapter {cur_ch_idx} ({st.session_state.summary_idx + 1} / {total_sum}) ｜ GUID: <code>{cur_card.get('guid', '')[:10]}</code></div>",
                unsafe_allow_html=True,
            )
        with s_nav3:
            sum_sus_key = f"chk_sum_sus_{cur_ch_idx}"
            is_sc_sus = cur_card.get("is_suspended", False)
            new_sc_sus = st.checkbox("⏸️ 休止", value=is_sc_sus, key=sum_sus_key)
            if new_sc_sus != is_sc_sus:
                cur_card["is_suspended"] = new_sc_sus
                save_summary_cloze_to_dir(summary_cards, active_work_dir, deck_name=active_deck_name)
                st.rerun()
        with s_nav4:
            if st.button("🔢 Cloze連番振直", width="stretch"):
                cur_card["html"] = renumber_cloze_spans(cur_card.get("html", ""))
                save_summary_cloze_to_dir(summary_cards, active_work_dir, deck_name=active_deck_name)
                st.session_state.pop(f"edit_sum_html_{cur_ch_idx}", None)
                st.toast("✅ Cloze番号を 1. から連番に振り直しました")
                st.rerun()
        with s_nav5:
            if st.button("次のCh ➡️", disabled=(st.session_state.summary_idx >= total_sum - 1), width="stretch"):
                st.session_state.summary_idx += 1
                st.rerun()
        
        # Front ＆ Back プレビュー（Front＝Clozeマスク / Back＝Cloze解除済み全文＋AI補足解説）
        back_extra_clean = (
            cur_card.get("back_extra", "")
            .replace(r"\\(", r"\(")
            .replace(r"\\)", r"\)")
            .replace(r"\\[", r"\[")
            .replace(r"\\]", r"\]")
        )
        back_ai_preview = (
            f'<hr style="margin:16px 0; border:none; border-top:2px dashed #cbd5e1;">'
            f'<div class="ai-container">{back_extra_clean}</div>'
            if back_extra_clean.strip() else ""
        )

        preview_mode = st.radio(
            "プレビュー表示面",
            ["🔄 表面 (Front: ダイナミックCloze)", "📖 裏面 (Back: Cloze解除済みノート ＋ AI補足解説)"],
            horizontal=True,
            key=f"prev_side_{cur_ch_idx}",
        )
        is_back_mode = preview_mode.startswith("📖")

        preview_dc_html = f"""
        <style>
            html, body {{
                margin: 0;
                padding: 8px;
                background: #e2e8f0;
            }}
            {DYNAMIC_CLOZE_CSS}
            .ai-container {{ margin-top: 12px; text-align: left; background: #fff; border-radius: 8px; padding: 14px; border: 1px solid #cbd5e1; font-size: 0.95em; line-height: 1.6; color: #1e293b; }}
            .ai-title {{ background: #0b4f8c; color: #fff; font-size: 1.05em; font-weight: bold; padding: 8px 12px; border-radius: 6px; margin-bottom: 12px; }}
            .ai-summary-box {{ background: #fff4f4; border-left: 4px solid #ff3b30; padding: 8px 12px; margin-bottom: 12px; border-radius: 0 4px 4px 0; }}
            .ai-summary-title {{ color: #d70015; font-weight: bold; margin-bottom: 2px; }}
            .ai-cascade-box {{ background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 6px; padding: 10px 14px; margin-top: 10px; }}
        </style>
        <div class="card {'back-view' if is_back_mode else ''}" id="dc-preview-root" style="border-radius: 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.08);">
            {cur_card.get("html", "")}
            {back_ai_preview if is_back_mode else ""}
        </div>
        <script>
            document.querySelectorAll('#dc-preview-root .cloze-container').forEach(function(el) {{
                el.addEventListener('click', function() {{
                    this.classList.toggle('revealed');
                }});
            }});
        </script>
        """
        st.components.v1.html(preview_dc_html, height=620 if is_back_mode else 480, scrolling=True)

        # Front / Back 編集コンテナ
        title_key = f"edit_sum_title_{cur_ch_idx}"
        html_key = f"edit_sum_html_{cur_ch_idx}"
        back_key = f"edit_sum_back_{cur_ch_idx}"
        if title_key not in st.session_state:
            st.session_state[title_key] = cur_card.get("title", f"Chapter {cur_ch_idx}")
        if html_key not in st.session_state:
            st.session_state[html_key] = cur_card.get("html", "")
        if back_key not in st.session_state:
            st.session_state[back_key] = cur_card.get("back_extra", "")

        with st.container(border=True):
            ed_c1, ed_c2 = st.columns([4, 1])
            with ed_c1:
                new_title_val = st.text_input("Chapterタイトル (SortTitle用)", key=title_key)
                new_html_val = st.text_area(
                    "Front HTML（追加で <u>語句</u> と囲んで保存すると自動でClozeタグに変換されます）",
                    key=html_key,
                    height=200,
                )
                new_back_val = st.text_area(
                    "Back 補足解説 HTML（Cloze解除済みノートの直下に表示されるLLM解説欄・空欄可）",
                    key=back_key,
                    height=140,
                )
            with ed_c2:
                st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                if st.button("💾 このChを保存", type="primary", width="stretch"):
                    cleaned_front = convert_markers_to_cloze(strip_embedded_style(new_html_val))
                    cleaned_back = strip_embedded_style(new_back_val)
                    cur_card["title"] = new_title_val.strip() or f"Chapter {cur_ch_idx}"
                    cur_card["html"] = cleaned_front
                    cur_card["back_extra"] = cleaned_back
                    save_summary_cloze_to_dir(summary_cards, active_work_dir, deck_name=active_deck_name)
                    st.session_state.pop(html_key, None)
                    st.session_state.pop(back_key, None)
                    st.toast(f"✅ Chapter {cur_ch_idx} を保存しました！")
                    st.rerun()

                if st.button("✨ Back補足解説を生成", disabled=not api_key, width="stretch", help="現在のFrontノートから裏面用の補足解説をGemini APIで生成します"):
                    with st.spinner("裏面用の補足解説を生成中..."):
                        try:
                            deck_title_str = st.session_state.get("group_title_val") or st.session_state.get("deck_name_val") or "講義要約"
                            exp_html, u_rec = call_gemini_summary_back_explanation(
                                cur_card.get("html", ""),
                                api_key,
                                sc_back_model_id,
                                deck_title=deck_title_str,
                            )
                            cur_card["back_extra"] = exp_html
                            save_summary_cloze_to_dir(summary_cards, active_work_dir, deck_name=active_deck_name)
                            st.session_state.pop(back_key, None)
                            st.toast(f"✅ Back補足解説を生成しました！（約 {u_rec['cost_jpy']:.2f} 円）")
                            st.rerun()
                        except Exception as e:
                            st.error(f"生成エラー: {e}")

                render_clipboard_button(cur_card.get("html", ""), label="📋 Frontコピー")

                if st.button("🗑️ このChを削除", width="stretch"):
                    summary_cards.pop(st.session_state.summary_idx)
                    save_summary_cloze_to_dir(summary_cards, active_work_dir, deck_name=active_deck_name)
                    st.session_state.summary_idx = max(0, st.session_state.summary_idx - 1)
                    st.rerun()
    else:
        st.info("まだ要約圧縮ダイナミックClozeカードがありません。上の「1」に Markdown または HTML を貼り付けて取り込んでください。")

    # --------------------------------------------------------------------------
    # 3. 最下部：Web LLM Chat で高粒度Markdownまとめを作成させるプロンプトコピー欄
    # --------------------------------------------------------------------------
    st.markdown("---")
    st.markdown("#### 📋 3. Web LLM Chat で高粒度Markdownまとめを作成するためのプロンプト")
    st.caption("定額制の LLM Chat に講義PDFを渡し、以下のプロンプトで粒度を落とさないMarkdownを作成させてから、好きなエディタで `Cmd+U`（`<u>...</u>`）を付けて上の「1」に貼り付けてください。")

    pr_col1, pr_col2 = st.columns([4, 1])
    with pr_col1:
        st.code(WEB_LLM_MARKDOWN_PROMPT, language="text")
    with pr_col2:
        st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)
        render_clipboard_button(WEB_LLM_MARKDOWN_PROMPT, label="📋 プロンプトをコピー")
        with st.expander("🎨 内蔵CSSを確認", expanded=False):
            st.code(DYNAMIC_CLOZE_CSS, language="css")


# ==============================================================================
# タブ 4: Ankiデッキパッケージ (.apkg) 出力
# ==============================================================================
with tab_build:
    st.subheader("Ankiデッキパッケージ (.apkg) 出力")

    bf = st.session_state.get("base_folder_val", "").strip()
    target_dir = bf if (bf and os.path.exists(bf)) else "."
    display_dir = os.path.abspath(target_dir)
    st.info(f"📁 出力先フォルダ（および3つのJSON保存先）: `{display_dir}`")

    gm_full = load_guid_map_from_dir(active_work_dir)
    restored_deck_name = (gm_full.get("deck_name") or "").strip()
    input_deck_name = st.session_state.get("deck_name_val", "").strip()
    deck_full_name = restored_deck_name if (gm_full.get("is_restored_from_anki") and restored_deck_name) else input_deck_name

    if restored_deck_name and input_deck_name and restored_deck_name != input_deck_name:
        st.warning(
            f"⚠️ サイドバーのDeck名 (`{input_deck_name}`) とAnki復元元のDeck名 (`{restored_deck_name}`) が異なります。"
            f"デッキ分裂を防ぐため、出力先には **`{restored_deck_name}`** を自動採用します。"
        )
    st.write(f"出力先デッキ名: **{deck_full_name}**")
    
    is_restored_deck = gm_full.get("is_restored_from_anki", False)
    gm_notes = gm_full.get("notes", {})

    current_b_names = {os.path.basename(f) for f in b_images}
    suspended_slide_count = sum(
        1 for b_fn in current_b_names
        if gm_notes.get(b_fn, {}).get("is_suspended", False)
    )
    suspended_sum_count = sum(
        1 for c in summary_cards
        if c.get("is_suspended", False)
    )
    total_suspended = suspended_slide_count + suspended_sum_count

    st.markdown(
        f"**📦 パッケージ格納順序（自動固定）:**\n"
        f"1. **元スライド (`Medical AI Slide Model`)**: **{len(b_images)} 枚** （うち個別AI解説あり: {len(ai_cache)} 枚 / 休止設定: {suspended_slide_count} 枚）\n"
        f"2. **要約圧縮Cloze (`Medical Dynamic Cloze Model`)**: **{len(summary_cards)} 枚** （うち休止設定: {suspended_sum_count} 枚）"
    )

    if is_restored_deck:
        st.error(
            "🚨 **【最重要：既存・学習中デッキの更新モード（学習履歴の保護）】**\n\n"
            "このデッキは既存のAnkiエクスポート（`.txt`）から復元されています。すでにAnki側にある学習履歴・復習期日・間隔を絶対に壊さないため、Ankiで `.apkg` を読み込む際は **「学習進捗も読み込む（Import any learning progress）」を必ず OFF（灰色）** にしてインポートしてください！\n"
            "- ※誤って ON にすると、Anki側で積み上げた既存の学習履歴がすべて初期状態に上書きリセットされてしまいます。\n"
            "- ※既存デッキのカード休止変更は、インポート後にAnkiブラウザ上で操作（`Cmd + J`）するのが最も安全です。"
        )
    elif total_suspended > 0:
        st.warning(
            f"⚠️ **休止設定のカードが合計 {total_suspended} 枚含まれています（`queue = -1`）**\n\n"
            f"Ankiで `.apkg` を読み込む際、インポート画面の **「学習進捗も読み込む（Import any learning progress）」** の設定に注意してください：\n"
            f"- **【新規デッキの初回インポート時】**: **ON（青色）** にして読み込んでください。これにより休止設定（{total_suspended}枚）がそのまま反映されます。\n"
            f"- **【すでにAnkiで学習を開始した後の再インポート時】**: 学習履歴を守るため **OFF（灰色）** のまま読み込んでください（※ONにすると既存の復習期日がリセットされます）。"
        )

    col_out1, col_out2 = st.columns([1, 1])
    with col_out1:
        if st.button("📦 .apkg パッケージを作成", type="primary", width="stretch"):
            with st.spinner("パッケージング中（元スライド ➔ 要約Clozeの順で統合）..."):
                apkg_path, total_cnt, s_cnt, dc_cnt = build_apkg(
                    DEST_DIR,
                    deck_full_name,
                    st.session_state.get("group_title_val", ""),
                    load_ai_cache_from_dir(active_work_dir),
                    output_dir=target_dir,
                    work_dir=active_work_dir,
                    summary_cards=summary_cards,
                )
                st.success(
                    f"完成: 合計 {total_cnt} 枚（元スライド {s_cnt} 枚 ＋ 要約Cloze {dc_cnt} 枚）を含む "
                    f"'{os.path.basename(apkg_path)}' を保存しました！"
                )
                st.caption(f"保存場所: `{apkg_path}`")

                if is_restored_deck:
                    st.error("🚨 **確認**: 既存デッキの更新です。Ankiインポート時は **「学習進捗も読み込む」を必ず OFF（灰色）** にしてください。")
                elif total_suspended > 0:
                    st.error(
                        f"🚨 **重要（休止カード {total_suspended} 枚を含む）**: "
                        f"新規デッキの初回取り込み時は Anki の **「学習進捗も読み込む」を ON（青）** に設定してください（既習デッキの再インポート時は OFF）。"
                    )
                else:
                    st.info("※決定論的GUIDと2つの固定Model IDにより、Ankiに再インポートしても学習進捗を維持したまま安全に更新されます。")

    with col_out2:
        if st.button("🗑️ 中間JPEG画像フォルダを全削除 (クリーンアップ)", width="stretch"):
            all_jpgs = glob.glob(os.path.join(DEST_DIR, "*.jpg"))
            for f in all_jpgs:
                os.remove(f)
            st.success(f"{len(all_jpgs)} 枚のJPEGを削除しました（講義フォルダ内の3つのJSONは安全に保持されています）。")