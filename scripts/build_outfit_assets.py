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

# デフォルト3衣装の原画パス（生成元アーティファクト）
DEFAULT_SOURCES: dict[str, str] = {
    "pajama": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\pajama_sprite_front_1795094441824.jpg",
    "casual": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\tsundere_casual_date_sprite_1795094448289.jpg",
    "maid": r"C:\Users\shima\.gemini\antigravity\brain\ba42d1c5-61d6-415e-a802-87291bb74f9f\maid_sprite_tsundere_1795094443781.jpg",
}


def _defringe_rgba(rgba: np.ndarray, bg_rgb: tuple[float, float, float] = (255.0, 255.0, 255.0)) -> np.ndarray:
    """
    ダーク背景上でも白フチ（ハロ）が一切出ないよう、半透明境界の白背景成分をデコンタミネーションし、
    アルファエッジを1pxだけ引き締める。
    """
    out = rgba.astype(np.float32).copy()
    alpha = out[:, :, 3] / 255.0

    # 極薄ゴミを完全透過
    alpha = np.where(alpha < 0.08, 0.0, alpha)

    # 境界1pxをわずかに引き締めて白ハローを根絶
    eroded = cv2.erode(alpha, np.ones((3, 3), np.uint8), iterations=1)
    edge_zone = (alpha > 0.0) & (alpha < 0.97)
    alpha = np.where(edge_zone, alpha * 0.45 + eroded * 0.55, alpha)
    alpha = np.where(alpha < 0.06, 0.0, alpha)

    # 白背景成分の引き算（Color Decontamination）
    bg = np.array(bg_rgb, dtype=np.float32)
    a3 = alpha[:, :, None]
    rgb = out[:, :, :3]
    decontam = np.clip((rgb - (1.0 - a3) * bg) / np.maximum(a3, 0.22), 0.0, 255.0)
    # 境界付近の極端な白飛びを抑制
    out[:, :, :3] = np.where((a3 > 0.0) & (a3 < 0.96), decontam, rgb)
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
