import os
import re
from PIL import Image
from google import genai
from google.genai import types
from config import DEFAULT_PROMPT, SYSTEM_PROMPT, OUTPUT_REQUIREMENTS
from storage import log_api_usage


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


def call_gemini_summary_back_explanation(front_html, api_key, model_id, deck_title="講義要約"):
    """要約ノートのHTMLから、個別解説と同一フォーマットの裏面（Back）補足解説HTMLを生成する"""
    client = genai.Client(api_key=api_key)
    prompt = f"""以下は医学部講義「{deck_title}」の要約ノートHTMLです。
このノートの内容を補完し、Ankiカードの裏面（Back）に配置する「医学生の復習・国試・定期試験対策として直感的に理解できる最高品質の補足解説」をHTML形式で作成してください。

【重要方針】
- 表面の表や箇条書きをそのまま丸写しするのではなく、「なぜそうなるかの生理学的・薬理学的機序」「紛らわしい薬剤・疾患との鑑別ポイント」「試験で狙われる引っ掛け・禁忌の理由」に重点を置いて解説すること。
- マークダウンのコードブロック（```html 等）は出力せず、HTMLタグのみを出力すること。

{OUTPUT_REQUIREMENTS}

【対象要約ノートHTML】
{front_html}
"""
    response = client.models.generate_content(
        model=model_id,
        contents=[prompt],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
        ),
    )

    usage = getattr(response, "usage_metadata", None)
    p_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
    o_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
    t_tok = int(getattr(usage, "thoughts_token_count", 0) or 0)
    usage_record = log_api_usage("SUMMARY_BACK_EXPLAIN", model_id, p_tok, o_tok, t_tok, is_svg=False)

    text = response.text.strip()
    if text.startswith("```html"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip(), usage_record