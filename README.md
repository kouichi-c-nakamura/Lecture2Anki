# 🎓 Lecture2Anki

大学の講義スライドPDFから、高速にAnki暗記カードを生成し、**Gemini Flash** による高品質なAI構造化解説を自動付加するツールです。

「教授のスライドが言葉足らずで復習時に意味不明になる」「模式図や病理写真の要点が掴めない」という問題を、AIによる病理・臨床的補完と美しいHTMLテーブルで解決します。

---

## ✨ 主な機能

- **2つのCloze作成ワークフロー**
  - **事前Cloze**: Preview.app等でスライドPDFに直接黄色四角を置いたファイル（Masked）と元ファイル（Unmasked）のペアから一括生成。
  - **事後Cloze**: Unmaskedスライドから生成後、書き出されたFront画像（`*a.jpg`）に直接黄色四角を書き込んでマスク化。
- **決定論的GUID管理（安全な上書き更新）**
  - デッキ名とスライドページ番号から一意のGUIDを算出。
  - **「まずは画像だけで勉強開始 ➔ 後からGeminiでAI解説を追加 ➔ 再インポート」としても、学習間隔や期日（Ease・Interval）を壊さずに更新可能**。
- **Gemini Flash による超低コストなAI講義補足**
  - スライド画像を解析し、「病理的核心」「分類対比表」「病理組織像・シグナル伝達の観察ポイント」をモダンなデザインでBack面に追加。
  - 1枚あたり約0.05〜0.1円程度。事前試算と承認チェックボックスにより、意図しない課金を防止。
- **講義プリセット管理**
  - 講義名、階層デッキ名（例: `病理学::12 環境と発癌`）、パスワード付きPDFの設定をJSONで自動保持。
- **パスワード保護PDF対応**

---

## 📸 デモ・カードイメージ

| 表面（Front） | 裏面（Back）: スライド + AI構造化解説 |
| :---: | :---: |
| *(ここに黄色四角で隠したスライドの画像を配置)* | *(ここにAI解説付きのBack画像プレビューを配置)* |

---

## 🛠 前提条件

PDFの画像レンダリングに **Poppler** を使用します。

### macOS
Homebrew を使用してインストールします:
```bash
brew install poppler

```

### Windows

1. [Poppler for Windows](https://www.google.com/search?q=http://blog.alivate.com.au/poppler-windows/) 等から最新バイナリをダウンロードして展開します。
2. 展開先の `bin` フォルダをシステムの環境変数 `PATH` に追加してください。

---

## 🚀 セットアップ

### 1. リポジトリのクローンと環境構築

```bash
git clone [https://github.com/your-username/Lecture2Anki.git](https://github.com/your-username/Lecture2Anki.git)
cd Lecture2Anki

python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt

```

### 2. 環境変数の設定

```bash
cp .env.example .env

```

`.env` ファイルを開き、Google AI Studio で取得したAPIキーを設定します（アプリ内のサイドバーから直接入力することも可能です）。

```env
GEMINI_API_KEY=AIzaSy...

```

---

## 📖 使い方

### 起動

```bash
streamlit run app.py

```

ブラウザでローカルサーバー（通常は `http://localhost:8501`）が自動で開きます。

### 基本ステップ

1. **設定**: サイドバーで「デッキ名」「パスワード（必要な場合）」を入力します。
2. **抽出**: 対象PDFとページ範囲（例: `1-20`, `all`）を指定し、画像（a/bペア）を書き出します。
* 事後Clozeの場合は「Finderで開く」を押し、`*a.jpg` をプレビュー等で開いて黄色四角を描画・保存します。


3. **AI強化（任意）**: 「AI解説の一括追加」タブで未処理枚数と概算API費用を確認し、承認して生成を実行します。
4. **パッケージ出力**: 「Ankiパッケージ出力」タブで `.apkg` を書き出し、Ankiにダブルクリックで取り込みます。

---

## 💰 API費用とモデルの目安

Gemini API の画像トークン削減のため、長辺1600pxに自動最適化してリクエストを送信します。

| モデル | 特徴 | 100枚あたりの概算費用 |
| --- | --- | --- |
| **Gemini 2.5 Flash-Lite** | 最安・高速。要約・表組み向け | 約 4〜7 円 |
| **Gemini 3.6 Flash** | 推奨。病理写真・模式図の解釈バランス型 | 約 8〜15 円 |
| **Gemini 3.8 Flash** | 高推論。複雑なシグナル伝達・個別修正向け | 約 12〜20 円 |

---

## ⚠️ 免責事項 (Disclaimer)

- **個人的利用の厳守:** 本ツールは、ユーザー個人が自身の復習および学習効率化を目的として私的に利用すること（私的使用のための複製）のみを想定して作成されています。
- **再配布の厳禁（学則・法令遵守）:** 各大学・教員が作成した講義資料PDF、および本ツールによって生成されたAnkiデッキ（.apkg）や抽出画像を、第三者へ無断で送信・共有・公開（クラウドストレージへのアップロード、Googleドライブ等の共有リンク配布、SNSへの投稿など）することは著作権法違反に該当します。また、各大学のコンプライアンス規程や学生心得に抵触し、**停学・退学をはじめとする厳罰（懲戒処分）の対象となる重大なリスク**があります。取り扱いには細心の注意を払ってください。
- **AI生成内容の正確性:** 本ツールのAI解説機能は一般的な医学情報に基づいて生成されますが、その正確性や試験への合否を保証するものではありません。臨床判断や試験対策においては、必ず大学公式の講義内容、シラバス、および指定教科書をご確認ください。
- **責任の所在:** 本ツールの利用に伴って生じた損害、トラブル、または懲戒処分等のいかなる不利益についても、開発者は一切の責任を負いません。

---

## 📄 ライセンス

[MIT License](https://www.google.com/search?q=LICENSE)


# TODO

## UseCases
- [ ] Replace/Update the jpegs for specific subset of the pages
- [ ] Multiple pages selection for individual AI enhancement
- [ ] Add AI enhancements to a subset of existing Anki notes while maintaining GUID