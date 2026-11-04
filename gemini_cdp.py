import os
import time
import json
import urllib.request
import subprocess
from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"


# ==============================================================================
# Chrome CDP 死活監視・起動ヘルパー
# ==============================================================================
def is_cdp_available(cdp_url=CDP_URL):
    """Port 9222 の Chrome DevTools Protocol が応答するか確認する"""
    try:
        with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def launch_debug_chrome_macos(port=9222):
    """
    macOS でリモートデバッグ有効化の専用 Chrome プロファイルを起動する。
    既存の通常 Chrome と競合しないよう独立した user-data-dir を使用する。
    """
    user_data_dir = os.path.expanduser("~/.chrome_gemini_cdp_profile")
    cmd = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "https://gemini.google.com/app"
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ==============================================================================
# macOS クリップボード操作 (画像 / テキスト)
# ==============================================================================
def _copy_image_to_clipboard(image_path):
    """AppKit NSPasteboard を用いて JPEG 画像を macOS クリップボードへ格納"""
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


def _copy_text_to_clipboard(text):
    """pbcopy を用いてプロンプト文字列を macOS クリップボードへ格納"""
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(input=text.encode("utf-8"))


def _clean_html_output(raw_text):
    """コードブロックのバッククォート等を除去して純粋なHTMLを取り出す"""
    text = raw_text.strip()
    if text.startswith("```html"):
        text = text[7:]
    elif text.startswith("```xml"):
        text = text[6:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ==============================================================================
# Gemini Web App 1スライド自動処理 (Playwright CDP)
# ==============================================================================
def run_single_slide_WaitAndScrape(page, img_path, prompt_text, timeout_sec=120):
    """
    開かれている Gemini タブに対して:
    1. 入力欄をフォーカスしてクリア
    2. 画像をクリップボード経由で Cmd+V ペースト
    3. プロンプトをクリップボード経由で Cmd+V ペースト
    4. 送信してコードブロックの出力完了を待機・抽出する
    """
    # Gemini のプロンプト入力欄ロケータ (rich-textarea 内の contenteditable)
    editor_sel = 'div.ql-editor[contenteditable="true"], div[contenteditable="true"][role="textbox"]'
    page.wait_for_selector(editor_sel, timeout=15000)
    editor = page.locator(editor_sel).first

    # 実行前のコードブロック数・メッセージ数を記録
    initial_code_count = page.locator("pre code, code-block").count()

    # 1. 入力欄をクリックして既存文字をクリア
    editor.click()
    page.keyboard.press("Meta+a")
    page.keyboard.press("Backspace")
    time.sleep(0.3)

    # 2. 画像をクリップボードに入れて Cmd+V で貼り付け
    if not _copy_image_to_clipboard(img_path):
        raise RuntimeError(f"画像のクリップボードコピーに失敗しました: {img_path}")
    
    editor.focus()
    page.keyboard.press("Meta+v")
    
    # 画像サムネイルがコンポーザーに認識・アップロードされるのを待機
    time.sleep(2.5)

    # 3. プロンプトテキストをクリップボードに入れて Cmd+V で貼り付け
    _copy_text_to_clipboard(prompt_text)
    editor.focus()
    page.keyboard.press("Meta+v")
    time.sleep(0.8)

    # 4. 送信ボタンをクリック（または Enter 送信）
    send_btn = page.locator(
        'button.send-button, button[aria-label*="送信"], button[aria-label*="Send"]'
    ).first
    if send_btn.is_visible():
        send_btn.click()
    else:
        page.keyboard.press("Enter")

    # 5. 生成開始を待機してから、生成完了（ストリーミング終了）までポーリング
    time.sleep(3.0)
    start_time = time.time()
    last_text = ""
    stable_ticks = 0

    while time.time() - start_time < timeout_sec:
        # 「回答を停止 / Stop」ボタンが存在するかチェック
        stop_btn = page.locator(
            'button[aria-label*="停止"], button[aria-label*="Stop"]'
        )
        is_generating = stop_btn.count() > 0 and stop_btn.first.is_visible()

        # コードブロックの取得を試みる
        code_blocks = page.locator("pre code")
        current_code_count = code_blocks.count()

        if current_code_count > initial_code_count:
            latest_code = code_blocks.nth(current_code_count - 1).inner_text().strip()
        else:
            # フォールバック: 最新の model-response 全体から取得
            responses = page.locator("model-response, message-content")
            if responses.count() > 0:
                latest_code = responses.nth(responses.count() - 1).inner_text().strip()
            else:
                latest_code = ""

        # ストリーミングが止まり、HTMLテキストが一定時間変化しなくなったら完了とみなす
        if not is_generating and latest_code and ("<div" in latest_code or "<svg" in latest_code):
            if latest_code == last_text:
                stable_ticks += 1
                if stable_ticks >= 2:  # 2秒間テキスト変化なし＆停止ボタン消滅で確定
                    return _clean_html_output(latest_code)
            else:
                last_text = latest_code
                stable_ticks = 0
        else:
            last_text = latest_code
            stable_ticks = 0

        time.sleep(1.0)

    raise TimeoutError(f"Gemini Web App からの応答がタイムアウトしました ({timeout_sec}秒)")


def execute_cdp_batch(
    image_paths,
    prompt_text,
    target_url="https://gemini.google.com/app",
    cdp_url=CDP_URL,
    cooldown_sec=4.0,
    on_progress=None,
):
    """
    選択された複数のスライド画像を順番に Chrome CDP 経由で Gemini App に送信し、
    完了するごとに on_progress(idx, total, filename, html_result, error) をコールバックする。
    """
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        # 既に開いている Gemini タブを探す。なければ新規タブで開く
        target_page = None
        for pg in context.pages:
            if "gemini.google.com" in pg.url:
                target_page = pg
                break

        if target_page is None:
            target_page = context.new_page()
            target_page.goto(target_url, wait_until="domcontentloaded")
            time.sleep(3.0)
        else:
            target_page.bring_to_front()
            # 指定されたカスタムGemのURLがあり、現在のURLと異なる場合は遷移
            if target_url and target_url.strip() and target_url.strip() not in target_page.url:
                target_page.goto(target_url.strip(), wait_until="domcontentloaded")
                time.sleep(3.0)

        total = len(image_paths)
        for idx, img_path in enumerate(image_paths):
            fn = os.path.basename(img_path)
            try:
                html_res = run_single_slide_WaitAndScrape(target_page, img_path, prompt_text)
                if on_progress:
                    on_progress(idx + 1, total, fn, html_res, None)
            except Exception as exc:
                if on_progress:
                    on_progress(idx + 1, total, fn, None, str(exc))

            # 次のスライドがある場合はレートリミット回避のクールダウン
            if idx < total - 1:
                time.sleep(cooldown_sec)