import os
import re
import glob
import json
import shutil
from datetime import datetime
from config import (
    LAST_WORK_DIR_FILE,
    LEGACY_PRESETS_FILE,
    FOLDER_CONFIG_FILENAME,
    DEST_DIR,
    PROMPTS_FILE,
    API_USAGE_LOG_FILE,
    CANONICAL_MODEL_NAME,
    MODEL_PRICING_PER_1M,
    USD_JPY,
    DEFAULT_PROMPT,
    SVG_OVERLAY_PROMPT,
)


# ==============================================================================
# 作業ディレクトリ・フォルダ設定の永続化
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
    # まとめPDFは通常の元講義PDF検出から除外
    unmasked = [
        p for p in pdfs
        if p not in masked and not re.search(r'(まとめ|summary)', os.path.basename(p), re.IGNORECASE)
    ]
    if not unmasked:
        unmasked = [p for p in pdfs if p not in masked]

    m_path = masked[0] if masked else os.path.join(folder_path, "講義 masked.pdf")
    if masked:
        inferred_u = infer_unmasked_from_masked(m_path)
        u_path = inferred_u if (inferred_u and os.path.exists(inferred_u)) else (unmasked[0] if unmasked else inferred_u)
    else:
        u_path = unmasked[0] if unmasked else os.path.join(folder_path, "講義.pdf")
    return u_path, m_path


def auto_detect_summary_pdf(folder_path):
    """講義フォルダ内の `*まとめ*.pdf` または `*summary*.pdf` を自動検出する"""
    if not folder_path or not os.path.isdir(folder_path):
        return ""
    pdfs = sorted(glob.glob(os.path.join(folder_path, "*.pdf")))
    for p in pdfs:
        if re.search(r'(まとめ|summary)', os.path.basename(p), re.IGNORECASE):
            return p
    return ""


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


# ==============================================================================
# 講義フォルダ直下での3つのJSON完全分離・永続化ヘルパー
# ==============================================================================
def resolve_meta_dir(work_dir=None):
    if work_dir and str(work_dir).strip() and os.path.isdir(str(work_dir).strip()):
        return str(work_dir).strip()
    os.makedirs(DEST_DIR, exist_ok=True)
    return DEST_DIR


def get_meta_file_path(filename, work_dir=None):
    target_dir = resolve_meta_dir(work_dir)
    target_path = os.path.join(target_dir, filename)
    legacy_path = os.path.join(DEST_DIR, filename)
    if target_dir != DEST_DIR and not os.path.exists(target_path) and os.path.exists(legacy_path):
        try:
            shutil.copy2(legacy_path, target_path)
        except Exception:
            pass
    return target_path


def load_guid_map_from_dir(work_dir=None):
    p = get_meta_file_path("guid_map.json", work_dir)
    if os.path.exists(p):
        try:
            return json.load(open(p, "r", encoding="utf-8"))
        except Exception:
            pass
    return {
        "deck_name": "",
        "notetype_name": CANONICAL_MODEL_NAME,
        "is_restored_from_anki": False,
        "notes": {},
    }


def save_guid_map_to_dir(data, work_dir=None):
    p = get_meta_file_path("guid_map.json", work_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_ai_cache_from_dir(work_dir=None):
    p = get_meta_file_path("ai_notes_cache.json", work_dir)
    if os.path.exists(p):
        try:
            return json.load(open(p, "r", encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_ai_cache_to_dir(data, work_dir=None):
    p = get_meta_file_path("ai_notes_cache.json", work_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def load_summary_cloze_from_dir(work_dir=None):
    p = get_meta_file_path("summary_cloze_cache.json", work_dir)
    if os.path.exists(p):
        try:
            data = json.load(open(p, "r", encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_summary_cloze_to_dir(cards_list, work_dir=None):
    p = get_meta_file_path("summary_cloze_cache.json", work_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(cards_list, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


# ==============================================================================
# プロンプトストア管理
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
# API実測トークンログの記録と集計
# ==============================================================================
def log_api_usage(img_filename, model_id, prompt_tokens, output_tokens, thought_tokens, is_svg=False):
    os.makedirs(os.path.dirname(API_USAGE_LOG_FILE), exist_ok=True)
    spec = MODEL_PRICING_PER_1M.get(model_id, {"in": 0.75, "out": 3.75})

    total_out_billed = output_tokens + thought_tokens
    cost_usd = (prompt_tokens / 1_000_000 * spec["in"]) + (total_out_billed / 1_000_000 * spec["out"])
    cost_jpy = cost_usd * USD_JPY

    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "image": img_filename,
        "model_id": model_id,
        "is_svg": is_svg,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "thought_tokens": thought_tokens,
        "total_tokens": prompt_tokens + total_out_billed,
        "cost_usd": round(cost_usd, 6),
        "cost_jpy": round(cost_jpy, 4),
    }
    try:
        with open(API_USAGE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return record


def load_api_usage_stats():
    records = []
    if os.path.exists(API_USAGE_LOG_FILE):
        try:
            with open(API_USAGE_LOG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except Exception:
            pass

    if not records:
        return {
            "count": 0,
            "total_jpy": 0.0,
            "total_tokens": 0,
            "avg_in_std": 2800,
            "avg_out_std": 3500,
            "avg_in_svg": 3800,
            "avg_out_svg": 5500,
            "recent": [],
        }

    total_jpy = sum(r.get("cost_jpy", 0.0) for r in records)
    total_tokens = sum(r.get("total_tokens", 0) for r in records)

    std_recs = [
        r for r in records
        if not r.get("is_svg") and not str(r.get("image", "")).startswith("REDLINE_PDF")
    ]
    svg_recs = [r for r in records if r.get("is_svg")]

    avg_in_std = int(sum(r["prompt_tokens"] for r in std_recs[-20:]) / len(std_recs[-20:])) if std_recs else 2800
    avg_out_std = int(sum((r["output_tokens"] + r["thought_tokens"]) for r in std_recs[-20:]) / len(std_recs[-20:])) if std_recs else 3500

    avg_in_svg = int(sum(r["prompt_tokens"] for r in svg_recs[-20:]) / len(svg_recs[-20:])) if svg_recs else 3800
    avg_out_svg = int(sum((r["output_tokens"] + r["thought_tokens"]) for r in svg_recs[-20:]) / len(svg_recs[-20:])) if svg_recs else 5500

    return {
        "count": len(records),
        "total_jpy": total_jpy,
        "total_tokens": total_tokens,
        "avg_in_std": avg_in_std,
        "avg_out_std": avg_out_std,
        "avg_in_svg": avg_in_svg,
        "avg_out_svg": avg_out_svg,
        "recent": records[-10:][::-1],
    }