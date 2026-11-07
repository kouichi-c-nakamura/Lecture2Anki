import os
import sys
from pipeline_util import restore_from_anki_export

DEFAULT_TXT = "ブロック1_肝胆膵疾患__3 胆膵の画像診断.txt"
DEFAULT_MEDIA_DIR = os.path.expanduser("~/Library/Application Support/Anki2/User 1/collection.media")
DEST_DIR = "exported_jpgs"

def main():
    txt_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TXT
    media_dir = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MEDIA_DIR

    if not os.path.exists(txt_path):
        print(f"❌ エラー: エクスポートファイルが見つかりません: {txt_path}")
        sys.exit(1)

    print(f"📖 Ankiエクスポートファイルを読み込み中: {txt_path}")
    print(f"📂 メディア参照元: {media_dir}")

    result = restore_from_anki_export(
        export_txt_path=txt_path,
        collection_media_dir=media_dir,
        dest_dir=DEST_DIR,
    )

    print("\n🎉 作業状態の復元が完了しました！")
    print(f" - 検出デッキ名     : {result['deck_name']}")
    print(f" - 検出ノートタイプ : {result['notetype_name']}")
    print(f" - 復元ノート数     : {result['restored_notes']} 枚 (GUID保持)")
    print(f" - 復元AI解説数     : {result['restored_ai']} 件")
    print(f" - コピーした画像数 : {result['copied_images']} 枚 -> {DEST_DIR}/")

    if result["missing_images"]:
        print(f"⚠️ 見つからなかった画像 ({len(result['missing_images'])}枚): {result['missing_images'][:5]} ...")
    else:
        print(" ✅ すべてのスライド画像 (a/bペア) の配置を確認しました。")

    print("\n👉 次に `streamlit run app.py` を起動し、タブ3でSVGを追加したいスライドを選択してバッチ生成してください。")

if __name__ == "__main__":
    main()