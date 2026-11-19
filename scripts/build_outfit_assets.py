"""
NIA（ニア）用 衣装アセット自動生成スクリプト（アニメ特化AI透過 `isnet-anime` + 白フリンジ除去）。

【使い方】
1. 特定の衣装画像を1枚から登録（full.png / bust.png を自動生成）:
   python scripts/build_outfit_assets.py --id yukata --src path/to/yukata.png

2. 既存3衣装（pajama / casual / maid）を一括再生成:
   python scripts/build_outfit_assets.py --rebuild-defaults
"""

from __future__ import annotations

import argparse
import os
import sys
import numpy as np
import cv2
from PIL import Image
from rembg import new_session, remove

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTFITS_DIR = os.path.join(BASE_DIR, "frontend", "assets", "outfits")

# 標準衣装の原画パス（生成元アーティファクト）
DEFAULT_SOURCES: dict[str, str] = {
    "pajama": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\pajama_sprite_front_1795094441824.jpg",
    "casual": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\tsundere_casual_date_sprite_1795094448289.jpg",
    "maid": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\maid_sprite_tsundere_1795094443781.jpg",
    "sailor": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\summer_sailor_standing_sprite_1795097623194.jpg",
    "hoodie_street": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\street_fashion_sprite_1795097624367.jpg",
    "gym_jersey": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\gym_uniform_standing_sprite_1795097602879.jpg",
    "gym_shorts": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\gym_uniform_front_sprite_v2_1795097620861.jpg",
    "swim_tankini": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\swimwear_tankini_sprite_1795097600608.jpg",
    "swim_highneck": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\sporty_swimwear_standing_sprite_1795097602516.jpg",
    "onepiece_summer": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\summer_sundress_sprite_1795097644688.jpg",
    "yukata": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\yukata_standing_sprite_1795097626484.jpg",
    "miko": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\miko_character_sprite_1795097629771.jpg",
}

# 襟合わせ補正などが必要な衣装の左右反転フラグ
FLIP_HORIZONTAL_IDS: set[str] = {"yukata"}


def _defringe_rgba(rgba: np.ndarray, bg_rgb: tuple[float, float, float] = (255.0, 255.0, 255.0)) -> np.ndarray:
    """
    ダーク背景上でも白フチ（ハロ）・指の隙間・髪の毛先の白残りが一切出ないよう、
    1. 指の隙間・髪の隙間に残った微小な白背景ホールをピンポイント透過（服の広い白領域は100%保護）
    2. 境界の白ハローを Color Decontamination + エッジ引き締めで根絶
    を行う。
    """
    out = rgba.astype(np.float32).copy()
    rgb = out[:, :, :3]
    alpha = out[:, :, 3] / 255.0
    bg = np.array(bg_rgb, dtype=np.float32)

    # 背景色との距離 & 彩度（無彩色白判定）
    bg_dist = np.linalg.norm(rgb - bg, axis=2)
    sat = np.max(rgb, axis=2) - np.min(rgb, axis=2)

    # 極薄ゴミを完全透過
    alpha = np.where(alpha < 0.12, 0.0, alpha)

    # 1. 指の間・手と太ももの間・髪の毛先の隙間に残る「純白の微小背景ホール」のみをピンポイント除去
    # 白い服・ニット・体操服・巫女服のハイライトを絶対に傷つけないよう、
    # 「背景純白に極めて近い (bg_dist < 22, sat < 12)」かつ「周囲リングの80%以上が暗色/肌色/透明背景」の穴のみ抜く
    pure_bg_like = (alpha > 0.05) & (bg_dist < 22.0) & (sat < 12.0)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(pure_bg_like.astype(np.uint8), connectivity=8)
    hole_mask = np.zeros(alpha.shape, dtype=bool)
    h, w = alpha.shape
    max_hole_area = 650

    lum = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    is_dark_or_skin_or_bg = (alpha <= 0.1) | (lum < 160.0) | ((rgb[:, :, 0] - rgb[:, :, 2]) > 22.0)
    ring_kernel = np.ones((5, 5), np.uint8)

    for lbl in range(1, num_labels):
        area = int(stats[lbl, cv2.CC_STAT_AREA])
        if area > max_hole_area:
            continue
        x = max(0, int(stats[lbl, cv2.CC_STAT_LEFT]) - 3)
        y = max(0, int(stats[lbl, cv2.CC_STAT_TOP]) - 3)
        x2 = min(w, int(stats[lbl, cv2.CC_STAT_LEFT] + stats[lbl, cv2.CC_STAT_WIDTH]) + 3)
        y2 = min(h, int(stats[lbl, cv2.CC_STAT_TOP] + stats[lbl, cv2.CC_STAT_HEIGHT]) + 3)

        sub_comp = (labels[y:y2, x:x2] == lbl)
        ring = (cv2.dilate(sub_comp.astype(np.uint8), ring_kernel, iterations=1) > 0) & (~sub_comp)
        ring_cnt = int(np.sum(ring))
        if ring_cnt == 0:
            continue
        # 周囲リングがほぼ暗色（髪・輪郭）・肌色（指・手）・背景で囲まれているか確認
        dark_skin_ratio = float(np.sum(ring & is_dark_or_skin_or_bg[y:y2, x:x2])) / float(ring_cnt)
        if dark_skin_ratio >= 0.80:
            # 目の白目領域（上部中央）は保護
            cy = float(stats[lbl, cv2.CC_STAT_TOP] + stats[lbl, cv2.CC_STAT_HEIGHT] * 0.5) / max(1, h)
            cx = float(stats[lbl, cv2.CC_STAT_LEFT] + stats[lbl, cv2.CC_STAT_WIDTH] * 0.5) / max(1, w)
            if 0.06 <= cy <= 0.24 and 0.36 <= cx <= 0.64:
                continue
            hole_mask[y:y2, x:x2] |= sub_comp

    alpha[hole_mask] = 0.0

    # 2. 境界エッジ（外周2px以内）の白っぽい半透明ハローを削減（白服の外周を削りすぎないよう厳密化）
    fg_bin = (alpha > 0.1).astype(np.uint8)
    eroded_2 = cv2.erode(fg_bin, np.ones((3, 3), np.uint8), iterations=2)
    border_band = (fg_bin > 0) & (eroded_2 == 0)
    white_fringe = border_band & (bg_dist < 42.0) & (sat < 18.0) & (alpha < 0.92)
    alpha = np.where(white_fringe, alpha * np.clip((bg_dist - 16.0) / 26.0, 0.0, 1.0), alpha)

    # 境界1pxをわずかに引き締めて毛先・指先の白フチを根絶
    eroded_a = cv2.erode(alpha, np.ones((3, 3), np.uint8), iterations=1)
    edge_zone = (alpha > 0.0) & (alpha < 0.98)
    alpha = np.where(edge_zone, alpha * 0.35 + eroded_a * 0.65, alpha)
    alpha = np.where(alpha < 0.1, 0.0, alpha)

    # 3. Color Decontamination（半透明エッジから白背景成分を減算）
    a3 = alpha[:, :, None]
    decontam = np.clip((rgb - (1.0 - a3) * bg) / np.maximum(a3, 0.28), 0.0, 255.0)
    out[:, :, :3] = np.where((a3 > 0.0) & (a3 < 0.98), decontam, rgb)
    out[:, :, 3] = np.clip(alpha * 255.0, 0.0, 255.0)

    # 完全透明領域のRGBはゼロ化してバイリニア補間時の白滲みを防止
    zero_mask = out[:, :, 3] == 0
    out[zero_mask, :3] = 0.0
    return out.astype(np.uint8)


def _crop_to_content(rgba: np.ndarray, pad: int = 12) -> np.ndarray:
    """不透明ピクセルのバウンディングボックスへ余白付きで自動トリミングする。"""
    ys, xs = np.where(rgba[:, :, 3] > 20)
    if len(xs) == 0 or len(ys) == 0:
        return rgba
    h, w = rgba.shape[:2]
    x1 = max(0, int(xs.min()) - pad)
    y1 = max(0, int(ys.min()) - pad)
    x2 = min(w, int(xs.max()) + pad + 1)
    y2 = min(h, int(ys.max()) + pad + 1)
    return rgba[y1:y2, x1:x2]


def _estimate_bg_rgb(img_rgb: np.ndarray) -> tuple[float, float, float]:
    """四隅のピクセル中央値から背景色を推定する。"""
    h, w = img_rgb.shape[:2]
    m = max(4, min(24, h // 30, w // 30))
    corners = np.concatenate(
        [
            img_rgb[:m, :m].reshape(-1, 3),
            img_rgb[:m, -m:].reshape(-1, 3),
            img_rgb[-m:, :m].reshape(-1, 3),
            img_rgb[-m:, -m:].reshape(-1, 3),
        ],
        axis=0,
    )
    med = np.median(corners, axis=0)
    return float(med[0]), float(med[1]), float(med[2])


def build_outfit(outfit_id: str, src_path: str, session=None, bust_ratio: float = 0.56) -> tuple[str, str]:
    """元画像から完全透過 full.png と bust.png を生成して保存する。"""
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"元画像が見つかりません: {src_path}")

    if session is None:
        session = new_session("isnet-anime")

    pil_in = Image.open(src_path).convert("RGB")
    if outfit_id in FLIP_HORIZONTAL_IDS:
        pil_in = pil_in.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    bg_rgb = _estimate_bg_rgb(np.array(pil_in))

    # isnet-anime で背景除去（ツインテール内側・腕の間・白い服の境界まで高精度分離）
    pil_cut = remove(pil_in, session=session, post_process_mask=True)
    rgba = np.array(pil_cut.convert("RGBA"))
    rgba = _defringe_rgba(rgba, bg_rgb=bg_rgb)
    rgba = _crop_to_content(rgba, pad=12)

    # 縦解像度を標準化（最大1400px）して描画負荷と鮮明さを両立
    h, w = rgba.shape[:2]
    if h > 1400:
        scale = 1400.0 / float(h)
        nw = max(1, int(round(w * scale)))
        nh = 1400
        rgba = np.array(Image.fromarray(rgba, mode="RGBA").resize((nw, nh), Image.Resampling.LANCZOS))
        h, w = rgba.shape[:2]

    out_dir = os.path.join(OUTFITS_DIR, outfit_id)
    os.makedirs(out_dir, exist_ok=True)

    full_path = os.path.join(out_dir, "full.png")
    bust_path = os.path.join(out_dir, "bust.png")

    Image.fromarray(rgba, mode="RGBA").save(full_path, format="PNG")

    bust_h = max(1, int(round(h * max(0.35, min(0.8, bust_ratio)))))
    bust_rgba = rgba[:bust_h, :]
    Image.fromarray(bust_rgba, mode="RGBA").save(bust_path, format="PNG")

    print(f"[OK] {outfit_id}: full={w}x{h} -> {full_path}, bust={w}x{bust_h} -> {bust_path}")
    return full_path, bust_path


def main() -> int:
    parser = argparse.ArgumentParser(description="NIA 衣装アセット高精度透過ビルダー (isnet-anime)")
    parser.add_argument("--id", help="登録する衣装ID（例: yukata, swimsuit, office）")
    parser.add_argument("--src", help="元画像のパス（PNG/JPG）")
    parser.add_argument("--bust-ratio", type=float, default=0.56, help="バストアップ切出し比率（既定 0.56）")
    parser.add_argument("--rebuild-defaults", action="store_true", help="pajama / casual / maid を一括再生成")
    args = parser.parse_args()

    session = new_session("isnet-anime")

    if args.rebuild_defaults:
        for oid, spath in DEFAULT_SOURCES.items():
            build_outfit(oid, spath, session=session, bust_ratio=args.bust_ratio)
        return 0

    if not args.id or not args.src:
        parser.print_help()
        return 1

    build_outfit(args.id.strip(), args.src.strip(), session=session, bust_ratio=args.bust_ratio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
