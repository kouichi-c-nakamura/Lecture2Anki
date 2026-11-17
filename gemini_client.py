import os
import re
import json
import genanki
from PIL import Image
from pdf2image import convert_from_path, pdfinfo_from_path
from google import genai
from google.genai import types
from config import (
    DEFAULT_PROMPT,
    SYSTEM_PROMPT,
    REDLINE_PDF_TO_CLOZE_PROMPT,
)
from storage import log_api_usage
from anki_io import (
    get_poppler_path,
    strip_embedded_style,
    convert_brackets_to_cloze,
)


def call_gemini_enhance(img_path, api_key, model_id, custom_prompt=None, return_usage=False):
    """個別スライド画像に対するAI補足解説・SVGオーバーレイ生成"""
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
    if text.startswith("```html"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    clean_html = text.strip()

    if return_usage:
        return clean_html, usage_record
    return clean_html


def call_gemini_redline_pdf_to_cloze(
    summary_pdf_path,
    api_key,
    model_id,
    deck_title="講義要約",
    password="",
    existing_cards=None,
):
    """
    手書き赤線（アンダーライン／マーカー）が引かれたまとめPDFをページ画像化し、
    内容を1文字も削らずに、赤線箇所だけを [[...]] で囲んでダイナミックClozeカード化する。
    """
    poppler_bin = get_poppler_path()
    info = pdfinfo_from_path(summary_pdf_path, poppler_path=poppler_bin, userpw=password)
    total_pages = info["Pages"]

    pil_pages = []
    for p in range(1, total_pages + 1):
        im = convert_from_path(
            summary_pdf_path,
            first_page=p,
            last_page=p,
            dpi=200,
            poppler_path=poppler_bin,
            userpw=password,
        )[0]
        w, h = im.size
        if max(w, h) > 1600:
            r = 1600 / max(w, h)
            im = im.resize((int(w * r), int(h * r)), Image.LANCZOS)
        pil_pages.append(im)

    prompt = REDLINE_PDF_TO_CLOZE_PROMPT.format(
        total_pages=total_pages,
        deck_title=deck_title or "講義要約",
    )

    schema = types.Schema(
        type=types.Type.ARRAY,
        description=f"全{total_pages}ページのチャプターHTML配列",
        items=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "chapter_idx": types.Schema(type=types.Type.INTEGER, description="1から始まるページ（チャプター）番号"),
                "title": types.Schema(type=types.Type.STRING, description="ページの主題タイトル"),
                "body_html": types.Schema(type=types.Type.STRING, description="赤線箇所を [[...]] で囲んだHTML本体（<style>は含めない）"),
            },
            required=["chapter_idx", "title", "body_html"],
        ),
    )

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model_id,
        contents=[*pil_pages, prompt],
        config=types.GenerateContentConfig(
            system_instruction="あなたは厳密なOCR・構造化転写アシスタントです。PDF内のテキスト・数値・表構造を1文字も省略せず忠実にHTML化し、赤い線または赤いハイライトが引かれている語句のみを [[...]] で囲んでください。",
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    p_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
    o_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
    t_tok = int(getattr(usage, "thoughts_token_count", 0) or 0)
    usage_record = log_api_usage(
        f"REDLINE_PDF_{total_pages}pages",
        model_id,
        p_tok,
        o_tok,
        t_tok,
        is_svg=False,
    )

    raw_items = json.loads(response.text)
    existing_by_idx = {c.get("chapter_idx"): c for c in (existing_cards or [])}

    result_cards = []
    for idx, item in enumerate(raw_items, 1):
        ch_num = int(item.get("chapter_idx") or idx)
        prev = existing_by_idx.get(ch_num, {})
        guid = prev.get("guid") or genanki.guid_for(f"{deck_title}_summary_ch{ch_num:02d}")
        clean_html = convert_brackets_to_cloze(strip_embedded_style(item.get("body_html", "")))
        result_cards.append({
            "guid": guid,
            "chapter_idx": ch_num,
            "title": item.get("title", f"Chapter {ch_num}"),
            "html": clean_html,
            "is_suspended": prev.get("is_suspended", False),
            "tags": prev.get("tags", []),
        })

    return result_cards, usage_record