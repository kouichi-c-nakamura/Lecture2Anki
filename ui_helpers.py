import os
import io
import json
import base64
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
import streamlit as st
from storage import (
    get_last_folder,
    keep_recent_prompt,
    identify_prompt_label,
    load_ai_cache_from_dir,
    save_ai_cache_to_dir,
)
from gemini_client import call_gemini_enhance


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
def get_thumb_b64(img_path, width=200, mtime=None):
    img = Image.open(img_path)
    w, h = img.size
    thumb = img.resize((width, int(h * (width / w))), Image.BILINEAR)
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def render_clipboard_button(text_to_copy, label="📋 コピー", height=40):
    text_js = json.dumps(text_to_copy)
    label_js = json.dumps(label)
    btn_html = f"""
    <style>
        html, body {{
            margin: 0;
            padding: 0;
            background: transparent;
            overflow: hidden;
        }}
        .copy-btn {{
            width: 100%;
            height: 38px;
            background-color: #262730;
            color: #fafafa;
            border: 1px solid rgba(250, 250, 250, 0.2);
            border-radius: 0.5rem;
            cursor: pointer;
            font-size: 14px;
            font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", sans-serif;
            white-space: nowrap;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
            box-sizing: border-box;
            transition: border-color 0.15s, color 0.15s;
        }}
        .copy-btn:hover {{
            border-color: #ff4b4b;
            color: #ff4b4b;
        }}
    </style>
    <button class="copy-btn" onclick='navigator.clipboard.writeText({text_js}); this.innerText="✅ コピー済"; setTimeout(()=>this.innerText={label_js}, 1500);'>
        {label}
    </button>
    """
    st.components.v1.html(btn_html, height=height)


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
    work_dir=None,
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
        f"- **推定コスト（実績基準）:** **約 {est_cost_jpy:.2f} 円** (`${est_cost_usd:.4f}`)"
    )

    col_ok, col_cancel = st.columns([1.5, 1])
    with col_ok:
        run_confirmed = st.button("✅ はい、この設定で実行する", type="primary", width="stretch")
    with col_cancel:
        if st.button("キャンセル", width="stretch"):
            st.rerun()

    if run_confirmed:
        keep_recent_prompt(prompt_text)
        ai_cache_local = load_ai_cache_from_dir(work_dir)
        sel_count = len(selected_paths)
        pbar = st.progress(0)
        status_box = st.empty()
        errors = []
        completed = 0
        total_actual_jpy = 0.0
        total_actual_tokens = 0

        def _worker(img_p):
            html_out, u_rec = call_gemini_enhance(img_p, api_key, model_id, prompt_text, return_usage=True)
            return os.path.basename(img_p), html_out, u_rec

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {executor.submit(_worker, p): p for p in selected_paths}
            for future in as_completed(future_to_path):
                p_orig = future_to_path[future]
                f_name = os.path.basename(p_orig)
                try:
                    res_fn, html_res, u_rec = future.result()
                    ai_cache_local[res_fn] = html_res
                    save_ai_cache_to_dir(ai_cache_local, work_dir)
                    total_actual_jpy += u_rec["cost_jpy"]
                    total_actual_tokens += u_rec["total_tokens"]
                    st.session_state.pop(f"edit_html_{res_fn}", None)
                    st.session_state.selected_batch_slides.discard(res_fn)
                    st.session_state[f"chk_batch_{res_fn}"] = False
                except Exception as exc:
                    errors.append(f"{f_name}: {exc}")
                completed += 1
                pbar.progress(completed / sel_count)
                status_box.text(f"並列生成中 ({completed}/{sel_count} 完了): 最新 {f_name} ｜ 累計実測 約 {total_actual_jpy:.2f} 円")

        if errors:
            st.error("一部のスライドでエラーが発生しました:\n" + "\n".join(errors))
        else:
            status_box.success(f"🎉 {sel_count} 枚完了！ 実測合計: 約 {total_actual_jpy:.2f} 円 ({total_actual_tokens:,} tok)")
            st.toast(f"✅ {sel_count} 枚完了（実測: 約 {total_actual_jpy:.2f} 円）")
            st.rerun()