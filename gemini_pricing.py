"""
Gemini API モデル単価定義およびトークンコスト計算モジュール
Lecture2Anki / Exam2Anki 共通利用
"""

USD_JPY = 155.0

PRICING_MODELS = {
    "Gemini 3.6 Flash (バランス標準)":       {"id": "gemini-3.6-flash",      "in": 0.75, "out": 3.75},
    "Gemini 3.5 Flash-Lite (最安推奨)":     {"id": "gemini-3.5-flash-lite", "in": 0.30, "out": 2.50},
    "Gemini 3.8 Flash (高推論Flash)":       {"id": "gemini-3.8-flash",      "in": 0.75, "out": 3.75},
    "Gemini 4 Argon (〜11/30 特割)":        {"id": "gemini-4-argon",        "in": 2.00, "out": 10.00},
    "Gemini 4 Argon (12/1〜 通常)":         {"id": "gemini-4-argon",        "in": 4.00, "out": 20.00},
}

# PRICING_MODELS からモデルIDごとの単価辞書を自動生成（先勝ち＝特割期間中は特割単価を優先）
MODEL_PRICING_PER_1M = {}
for _spec in PRICING_MODELS.values():
    if _spec["id"] not in MODEL_PRICING_PER_1M:
        MODEL_PRICING_PER_1M[_spec["id"]] = {
            "in": _spec["in"],
            "out": _spec["out"],
        }


def calculate_gemini_cost(model_id, prompt_tokens, output_tokens, thought_tokens=0, usd_jpy=USD_JPY):
    """
    実測トークン数から USD / JPY コストを計算して返す。
    ※ thought_tokens（思考トークン）は output 単価として合算請求される。
    """
    spec = MODEL_PRICING_PER_1M.get(model_id, {"in": 0.75, "out": 3.75})
    total_out_billed = int(output_tokens or 0) + int(thought_tokens or 0)
    cost_usd = (int(prompt_tokens or 0) / 1_000_000 * spec["in"]) + (total_out_billed / 1_000_000 * spec["out"])
    cost_jpy = cost_usd * usd_jpy
    return {
        "total_out_billed": total_out_billed,
        "total_tokens": int(prompt_tokens or 0) + total_out_billed,
        "cost_usd": round(cost_usd, 6),
        "cost_jpy": round(cost_jpy, 4),
    }


def estimate_batch_cost(model_label_or_id, avg_in_tokens, avg_out_tokens, count=1, usd_jpy=USD_JPY):
    """
    バッチ実行前の推定コスト（USD / JPY）を計算して返す。
    UI上のラベル名（"Gemini 4 Argon (12/1〜 通常)" 等）とモデルIDのどちらでも指定可能。
    """
    if model_label_or_id in PRICING_MODELS:
        spec = PRICING_MODELS[model_label_or_id]
    else:
        spec = MODEL_PRICING_PER_1M.get(model_label_or_id, {"in": 0.75, "out": 3.75})

    single_usd = (avg_in_tokens / 1_000_000 * spec["in"]) + (avg_out_tokens / 1_000_000 * spec["out"])
    total_usd = single_usd * count
    total_jpy = total_usd * usd_jpy
    return {
        "single_usd": single_usd,
        "single_jpy": single_usd * usd_jpy,
        "total_usd": total_usd,
        "total_jpy": total_jpy,
    }