import matplotlib.pyplot as plt
import matplotlib.patches as patches

def generate_demo_slides():
    topics = [
        {
            "page": 1,
            "title": "炎症の5大徴候（Cardinal Signs of Inflammation）",
            "items": [
                "1) 発赤 (Rubor) : 局所の血管拡張・血流増加（充血）",
                "2) 熱感 (Calor) : 血流増加による局所温度の上昇",
                "3) 腫脹 (Tumor) : 血管透過性亢進による浸出液の貯留（浮腫）",
                "4) 疼痛 (Dolor) : ブラジキニン、PGE2等による知覚神経刺激",
                "5) 機能障害 (Functio laesa) : 組織損傷や腫脹に伴う運動・機能制限",
            ],
            # マスクする矩形座標: [x, y, width, height]
            "masks": [
                [0.15, 0.67, 0.12, 0.05], # 発赤
                [0.15, 0.57, 0.12, 0.05], # 熱感
                [0.15, 0.47, 0.12, 0.05], # 腫脹
                [0.15, 0.37, 0.12, 0.05], # 疼痛
                [0.15, 0.27, 0.18, 0.05], # 機能障害
            ]
        }
    ]

    for is_masked in [False, True]:
        fig, ax = plt.subplots(figsize=(10, 7.5), dpi=150)
        ax.set_facecolor("#ffffff")

        slide = topics[0]
        # タイトル
        ax.text(0.08, 0.88, slide["title"], fontsize=18, fontweight="bold", color="#111111", va="top")
        ax.axhline(0.82, color="#0055aa", linewidth=2.5, xmin=0.08, xmax=0.92)

        # 本文テキスト
        y_pos = 0.70
        for item in slide["items"]:
            ax.text(0.10, y_pos, item, fontsize=13, color="#222222", va="top")
            y_pos -= 0.10

        # 右下の枠（模式図プレースホルダー）
        rect_box = patches.FancyBboxPatch((0.65, 0.25), 0.25, 0.45, boxstyle="round,pad=0.02",
                                          edgecolor="#888888", facecolor="#f0f4f8", linewidth=1.5)
        ax.add_patch(rect_box)
        ax.text(0.775, 0.50, "急性炎症の局所反応\n（模式図）", fontsize=11, color="#555555", ha="center", va="center")

        # マスクの適用（黄色矩形）
        if is_masked:
            for m in slide["masks"]:
                mask_patch = patches.Rectangle((m[0], m[1]), m[2], m[3], facecolor="#ffee00", edgecolor="none", zorder=10)
                ax.add_patch(mask_patch)

        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

        filename = "demo_lecture_masked.pdf" if is_masked else "demo_lecture.pdf"
        plt.savefig(filename, bbox_inches="tight", pad_inches=0.1)
        plt.close()
        print(f"作成完了: {filename}")

if __name__ == "__main__":
    generate_demo_slides()