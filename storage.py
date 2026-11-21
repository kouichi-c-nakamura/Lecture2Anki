import os
import re
import glob
import json
import time
import shutil
import hashlib
import tempfile
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
from gemini_pricing import calculate_gemini_cost

LOCAL_WORK_CACHE_ROOT = os.path.expanduser("~/.lecture2anki_local_cache")


def _is_cloud_storage_path(path):
    if not path:
        return False
    norm = os.path.abspath(path)
    return ("/Library/CloudStorage/" in norm) or ("OneDrive" in norm)


def _get_local_mirror_dir(work_dir):
    """OneDrive等のクラウドストレージ配下の場合、高速なローカルSSDミラー先ディレクトリを返す"""
    norm = os.path.abspath(work_dir)
    h = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
    folder_label = re.sub(r'[\\/:*?"<>|]+', "_", os.path.basename(norm))[:30]
    mirror_dir = os.path.join(LOCAL_WORK_CACHE_ROOT, f"{folder_label}_{h}")
    os.makedirs(mirror_dir, exist_ok=True)
    return mirror_dir


def _atomic_write_json(target_path, data, retries=2):
    """
    一時ファイルに書き出してから os.replace でアトミック置換する。
    OneDrive (FileProvider) の同期ロックによる TimeoutError (Errno 60) を防ぐ。
    """
    parent = os.path.dirname(target_path) or "."
    os.makedirs(parent, exist_ok=True)
    for attempt in range(retries):
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=parent)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, target_path)
            return True
        except (TimeoutError, OSError):
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if attempt < retries - 1:
                time.sleep(0.2 * (attempt + 1))
    return False


def _read_json_with_mirror(cloud_path, mirror_path, default_val):
    """ローカルSSDミラーを最優先で即座に読み込み、初回のみOneDrive側から同期する"""
    if os.path.exists(mirror_path):
        try:
            with open(mirror_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    if os.path.exists(cloud_path):
        try:
            with open(cloud_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _atomic_write_json(mirror_path, data, retries=1)
            return data
        except Exception:
            pass
    return default_val


def _write_json_with_mirror(cloud_path, mirror_path, data):
    """ローカルSSDミラーへ即座に保存し、OneDrive側へも安全に書き出す（OneDriveロック時もフリーズさせない）"""
    _atomic_write_json(mirror_path, data, retries=2)
    if cloud_path != mirror_path:
        _atomic_write_json(cloud_path, data, retries=1)


def get_last_folder():
    if os.path.exists(LAST_WORK_DIR_FILE):
        try:
            with open(LAST_WORK_DIR_FILE, "r", encoding="utf-8") as f:
                d = json.load(f).get("last_folder", "")
            if d and os.path.isdir(d):
                return d
        except Exception:
            pass
    return ""


def save_last_folder(folder_path):
    if folder_path and os.path.isdir(folder_path):
        _atomic_write_json(LAST_WORK_DIR_FILE, {"last_folder": folder_path}, retries=1)


def infer_unmasked_from_masked(masked_path):
    if not masked_path:
        return ""
    directory = os.path.dirname(masked_path)
    filename = os.path.basename(masked_path)
    new_filename = re.sub(r'[_\s]*masked(\.pdf)$', r'\1', filename, flags=re.IGNORECASE)
    if new_filename != filename:
        candidate = os.path.join(directory, new_filename)
        if os.path.exists(candidate):
            return candidate
        return candidate
    return ""


def auto_detect_pdfs(folder_path):
    if not folder_path or not os.path.isdir(folder_path):
        return "", ""
    pdfs = sorted(glob.glob(os.path.join(folder_path, "*.pdf")))
    masked_list = [p for p in pdfs if re.search(r'[_\s]*masked\.pdf$', os.path.basename(p), re.IGNORECASE)]
    unmasked_list = [p for p in pdfs if not re.search(r'[_\s]*masked\.pdf$', os.path.basename(p), re.IGNORECASE)]

    m_path = masked_list[0] if masked_list else ""
    u_path = ""
    if m_path:
        inferred = infer_unmasked_from_masked(m_path)
        if inferred and os.path.exists(inferred):
            u_path = inferred
    if not u_path and unmasked_list:
        u_path = unmasked_list[0]
    return u_path, m_path


def load_folder_config(folder_path):
    if not folder_path or not os.path.isdir(folder_path):
        return {}
    cloud_p = os.path.join(folder_path, FOLDER_CONFIG_FILENAME)
    mirror_p = (
        os.path.join(_get_local_mirror_dir(folder_path), FOLDER_CONFIG_FILENAME)
        if _is_cloud_storage_path(folder_path)
        else cloud_p
    )
    cfg = _read_json_with_mirror(cloud_p, mirror_p, {})
    return cfg if isinstance(cfg, dict) else {}


def save_folder_config(folder_path, cfg_dict):
    if not folder_path or not os.path.isdir(folder_path):
        return
    cloud_p = os.path.join(folder_path, FOLDER_CONFIG_FILENAME)
    mirror_p = (
        os.path.join(_get_local_mirror_dir(folder_path), FOLDER_CONFIG_FILENAME)
        if _is_cloud_storage_path(folder_path)
        else cloud_p
    )
    _write_json_with_mirror(cloud_p, mirror_p, cfg_dict)


def resolve_meta_dir(work_dir=None):
    if work_dir and os.path.isdir(work_dir):
        return work_dir
    return "."

def get_meta_file_path(filename, work_dir=None):
    """メタJSONファイルの実効パス（クラウド配下ならローカルSSDミラーパス）を返す"""
    target_dir = resolve_meta_dir(work_dir)
    cloud_p = os.path.join(target_dir, filename)
    if _is_cloud_storage_path(target_dir):
        mirror_p = os.path.join(_get_local_mirror_dir(target_dir), filename)
        if not os.path.exists(mirror_p) and os.path.exists(cloud_p):
            try:
                shutil.copy2(cloud_p, mirror_p)
            except Exception:
                pass
        return mirror_p
    return cloud_p

def load_guid_map_from_dir(work_dir=None):
    target_dir = resolve_meta_dir(work_dir)
    cloud_p = os.path.join(target_dir, "guid_map.json")
    mirror_p = (
        os.path.join(_get_local_mirror_dir(target_dir), "guid_map.json")
        if _is_cloud_storage_path(target_dir)
        else cloud_p
    )
    data = _read_json_with_mirror(cloud_p, mirror_p, {"notes": {}})
    return data if isinstance(data, dict) else {"notes": {}}


def save_guid_map_to_dir(data, work_dir=None):
    target_dir = resolve_meta_dir(work_dir)
    cloud_p = os.path.join(target_dir, "guid_map.json")
    mirror_p = (
        os.path.join(_get_local_mirror_dir(target_dir), "guid_map.json")
        if _is_cloud_storage_path(target_dir)
        else cloud_p
    )
    _write_json_with_mirror(cloud_p, mirror_p, data)


def load_ai_cache_from_dir(work_dir=None):
    target_dir = resolve_meta_dir(work_dir)
    cloud_p = os.path.join(target_dir, "ai_explanations_cache.json")
    mirror_p = (
        os.path.join(_get_local_mirror_dir(target_dir), "ai_explanations_cache.json")
        if _is_cloud_storage_path(target_dir)
        else cloud_p
    )
    data = _read_json_with_mirror(cloud_p, mirror_p, {})
    return data if isinstance(data, dict) else {}


def save_ai_cache_to_dir(cache_dict, work_dir=None):
    target_dir = resolve_meta_dir(work_dir)
    cloud_p = os.path.join(target_dir, "ai_explanations_cache.json")
    mirror_p = (
        os.path.join(_get_local_mirror_dir(target_dir), "ai_explanations_cache.json")
        if _is_cloud_storage_path(target_dir)
        else cloud_p
    )
    _write_json_with_mirror(cloud_p, mirror_p, cache_dict)


def sanitize_deck_filename_prefix(deck_name):
    if not deck_name or not str(deck_name).strip():
        return "default"
    s = str(deck_name).strip().replace("::", "__")
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    return s


def get_summary_cloze_filepath(work_dir=None, deck_name=""):
    target_dir = resolve_meta_dir(work_dir)
    prefix = sanitize_deck_filename_prefix(deck_name)
    filename = f"{prefix}_summary_cloze.json"
    cloud_p = os.path.join(target_dir, filename)
    legacy_cloud_p = os.path.join(target_dir, "summary_cloze_cache.json")

    if _is_cloud_storage_path(target_dir):
        mirror_dir = _get_local_mirror_dir(target_dir)
        mirror_p = os.path.join(mirror_dir, filename)
        if not os.path.exists(mirror_p):
            if os.path.exists(cloud_p):
                try:
                    shutil.copy2(cloud_p, mirror_p)
                except Exception:
                    pass
            elif os.path.exists(legacy_cloud_p):
                try:
                    shutil.copy2(legacy_cloud_p, mirror_p)
                    _atomic_write_json(cloud_p, json.load(open(legacy_cloud_p, "r", encoding="utf-8")), retries=1)
                except Exception:
                    pass
        return mirror_p

    if not os.path.exists(cloud_p) and os.path.exists(legacy_cloud_p):
        try:
            shutil.copy2(legacy_cloud_p, cloud_p)
        except Exception:
            pass
    return cloud_p


def load_summary_cloze_from_dir(work_dir=None, deck_name=""):
    p = get_summary_cloze_filepath(work_dir, deck_name)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_summary_cloze_to_dir(cards_list, work_dir=None, deck_name=""):
    target_dir = resolve_meta_dir(work_dir)
    prefix = sanitize_deck_filename_prefix(deck_name)
    filename = f"{prefix}_summary_cloze.json"
    cloud_p = os.path.join(target_dir, filename)
    mirror_p = get_summary_cloze_filepath(work_dir, deck_name)
    _write_json_with_mirror(cloud_p, mirror_p, cards_list)
    return mirror_p


def get_prompt_store():
    if os.path.exists(PROMPTS_FILE):
        try:
            with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "saved": data.get("saved", {}),
                "recent": data.get("recent", []),
            }
        except Exception:
            pass
    return {"saved": {}, "recent": []}


def save_prompt_store(store):
    _atomic_write_json(PROMPTS_FILE, store, retries=1)


def keep_recent_prompt(prompt_text):
    p = prompt_text.strip()
    if not p:
        return
    store = get_prompt_store()
    recent = [r for r in store.get("recent", []) if r.strip() != p]
    recent.insert(0, p)
    store["recent"] = recent[:5]
    save_prompt_store(store)


def identify_prompt_label(prompt_text):
    p = prompt_text.strip()
    if p == DEFAULT_PROMPT.strip():
        return "🔄 Default (標準解説・表・カスケード)"
    if p == SVG_OVERLAY_PROMPT.strip():
        return "🎯 SVG図示 (矢印・枠線オーバーレイ強調)"
    store = get_prompt_store()
    for name, saved_p in store.get("saved", {}).items():
        if saved_p.strip() == p:
            return f"💾 保存済み: {name}"
    return "✏️ カスタム編集プロンプト"


def log_api_usage(img_filename, model_id, prompt_tokens, output_tokens, thought_tokens, is_svg=False):
    os.makedirs(os.path.dirname(API_USAGE_LOG_FILE), exist_ok=True)
    calc = calculate_gemini_cost(model_id, prompt_tokens, output_tokens, thought_tokens)

    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "image": img_filename,
        "model_id": model_id,
        "is_svg": is_svg,
        "prompt_tokens": prompt_tokens,
        "output_tokens": output_tokens,
        "thought_tokens": thought_tokens,
        "total_tokens": calc["total_tokens"],
        "cost_usd": calc["cost_usd"],
        "cost_jpy": calc["cost_jpy"],
    }
    try:
        with open(API_USAGE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return record


def load_api_usage_stats():
    stats = {
        "count": 0,
        "total_tokens": 0,
        "total_usd": 0.0,
        "total_jpy": 0.0,
        "avg_in_std": 2200,
        "avg_out_std": 3200,
        "avg_in_svg": 2400,
        "avg_out_svg": 4500,
        "recent": [],
    }
    if not os.path.exists(API_USAGE_LOG_FILE):
        return stats

    records = []
    try:
        with open(API_USAGE_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    except Exception:
        return stats

    if not records:
        return stats

    stats["count"] = len(records)
    stats["total_tokens"] = sum(r.get("total_tokens", 0) for r in records)
    stats["total_usd"] = sum(r.get("cost_usd", 0.0) for r in records)
    stats["total_jpy"] = sum(r.get("cost_jpy", 0.0) for r in records)
    stats["recent"] = list(reversed(records[-10:]))

    std_recs = [r for r in records if not r.get("is_svg")]
    svg_recs = [r for r in records if r.get("is_svg")]

    if std_recs:
        stats["avg_in_std"] = int(sum(r.get("prompt_tokens", 0) for r in std_recs) / len(std_recs))
        stats["avg_out_std"] = int(
            sum(r.get("output_tokens", 0) + r.get("thought_tokens", 0) for r in std_recs) / len(std_recs)
        )
    if svg_recs:
        stats["avg_in_svg"] = int(sum(r.get("prompt_tokens", 0) for r in svg_recs) / len(svg_recs))
        stats["avg_out_svg"] = int(
            sum(r.get("output_tokens", 0) + r.get("thought_tokens", 0) for r in svg_recs) / len(svg_recs)
        )

    return stats