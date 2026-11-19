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
import json
import os
import sys
import numpy as np
import cv2
from PIL import Image

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAR_DIR = os.path.join(BASE_DIR, "frontend", "assets", "character")
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

    # 1. ツインテール毛束の間・指の間・手と腰の間に残る「白〜薄灰の背景隙間」をピンポイント除去
    gap_like = (alpha > 0.05) & (bg_dist < 68.0) & (sat < 30.0)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(gap_like.astype(np.uint8), connectivity=8)
    hole_mask = np.zeros(alpha.shape, dtype=bool)
    h, w = alpha.shape
    max_hole_area = 3200

    lum = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    is_white_cloth = (alpha > 0.25) & (lum >= 178.0) & (sat < 30.0) & (~gap_like)
    ring_kernel = np.ones((5, 5), np.uint8)

    for lbl in range(1, num_labels):
        area = int(stats[lbl, cv2.CC_STAT_AREA])
        if area > max_hole_area:
            continue
        x = max(0, int(stats[lbl, cv2.CC_STAT_LEFT]) - 3)
        y = max(0, int(stats[lbl, cv2.CC_STAT_TOP]) - 3)
        x2 = min(w, int(stats[lbl, cv2.CC_STAT_LEFT] + stats[lbl, cv2.CC_STAT_WIDTH]) + 3)
        y2 = min(h, int(stats[lbl, cv2.CC_STAT_TOP] + stats[lbl, cv2.CC_STAT_HEIGHT]) + 3)

        cy = float(stats[lbl, cv2.CC_STAT_TOP] + stats[lbl, cv2.CC_STAT_HEIGHT] * 0.5) / max(1, h)
        cx = float(stats[lbl, cv2.CC_STAT_LEFT] + stats[lbl, cv2.CC_STAT_WIDTH] * 0.5) / max(1, w)
        # 目の白目領域は保護
        if 0.08 <= cy <= 0.175 and 0.41 <= cx <= 0.59:
            continue
        # 襟・胸・胴体中央（セーラー服の白ライン・ボタン・白リボン等）は絶対に抜かないよう保護
        if 0.19 <= cy <= 0.48 and 0.25 <= cx <= 0.75:
            continue

        sub_comp = (labels[y:y2, x:x2] == lbl)
        ring = (cv2.dilate(sub_comp.astype(np.uint8), ring_kernel, iterations=1) > 0) & (~sub_comp)
        ring_cnt = int(np.sum(ring))
        if ring_cnt == 0:
            continue

        # ツインテール毛束の間（顔より左右外側）または指の間のみ抜く
        is_twintail_zone = (0.07 <= cy <= 0.34) and (cx < 0.36 or cx > 0.64)
        is_dark_or_bg = (alpha <= 0.15) | (lum < 140.0) | ((rgb[:, :, 0] - rgb[:, :, 2]) > 20.0)
        dark_bg_ratio = float(np.sum(ring & is_dark_or_bg[y:y2, x:x2])) / float(ring_cnt)
        white_cloth_ratio = float(np.sum(ring & is_white_cloth[y:y2, x:x2])) / float(ring_cnt)

        if (is_twintail_zone and dark_bg_ratio >= 0.50) or (white_cloth_ratio < 0.12 and area <= 700):
            hole_mask[y:y2, x:x2] |= sub_comp

    # 抜いた隙間を透過にし、境界は滑らかアンチエイリアスでジャギを防止
    alpha[hole_mask] = 0.0

    # 2. 境界エッジの白っぽい半透明ハローを削減
    fg_bin = (alpha > 0.1).astype(np.uint8)
    eroded_2 = cv2.erode(fg_bin, np.ones((3, 3), np.uint8), iterations=2)
    border_band = (fg_bin > 0) & (eroded_2 == 0)
    white_fringe = border_band & (bg_dist < 42.0) & (sat < 18.0) & (alpha < 0.90)
    alpha = np.where(white_fringe, alpha * np.clip((bg_dist - 16.0) / 26.0, 0.0, 1.0), alpha)

    # ジャギ防止のためエッジ帯のみ滑らかにガウシアン平滑化
    alpha_blur = cv2.GaussianBlur(alpha, (3, 3), 0.65)
    edge_zone = (alpha > 0.0) & (alpha < 0.98)
    alpha = np.where(edge_zone | (hole_mask & (alpha_blur < 0.4)), alpha_blur, alpha)
    alpha = np.where(alpha < 0.05, 0.0, alpha)

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


def _is_chroma_green_bg(bg_rgb: tuple[float, float, float]) -> bool:
    """推定背景色がクロマキーグリーン（#00FF00系）かどうかを判定する。"""
    r, g, b = bg_rgb
    return g >= 135.0 and (g - max(r, b)) >= 48.0


def _remove_chroma_green(img_rgb: np.ndarray, bg_rgb: tuple[float, float, float]) -> np.ndarray:
    """
    クロマキーグリーン背景（#00FF00系）を1pxのゴミ・緑スピルなしで完全透過する。
    白服・黒髪・褐色肌・赤い瞳には緑優位度がほぼないため、毛束の隙間や指の間まで完璧に分離できる。
    """
    rgb = img_rgb.astype(np.float32)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    max_rb = np.maximum(r, b)
    bg_r, bg_g, bg_b = bg_rgb
    bg_excess = max(60.0, bg_g - max(bg_r, bg_b))

    # 緑優位度 (G - max(R, B)) と背景色からの距離で連続マットを算出
    g_excess = g - max_rb
    bg_vec = np.array([bg_r, bg_g, bg_b], dtype=np.float32)
    bg_dist = np.linalg.norm(rgb - bg_vec, axis=2)

    # 背景判定: g_excess が bg_excess の 48% 以上なら完全透過、18%〜48% は滑らかな半透明遷移
    low_th = max(24.0, bg_excess * 0.16)
    high_th = max(58.0, bg_excess * 0.46)
    alpha_g = 1.0 - np.clip((g_excess - low_th) / max(1.0, high_th - low_th), 0.0, 1.0)

    # 非常に暗い緑影（背景の少し暗い緑）も距離ベースで補助的に抜く
    dark_green_bg = (g_excess > 28.0) & (g > 75.0) & (bg_dist < 135.0)
    alpha = np.where(dark_green_bg, np.minimum(alpha_g, np.clip((bg_dist - 40.0) / 95.0, 0.0, 1.0)), alpha_g)
    alpha = np.where(alpha < 0.06, 0.0, alpha)

    # エッジの滑らかアンチエイリアス
    alpha_blur = cv2.GaussianBlur(alpha, (3, 3), 0.55)
    edge_zone = (alpha > 0.0) & (alpha < 0.98)
    alpha = np.where(edge_zone, alpha_blur, alpha)
    alpha = np.where(alpha < 0.04, 0.0, alpha)

    # グリーン・スピル除去（Despill）：髪・白服・肌のフチに乗った緑反射を自然に中和
    fg_bin = (alpha > 0.04).astype(np.uint8)
    eroded_3 = cv2.erode(fg_bin, np.ones((3, 3), np.uint8), iterations=3)
    spill_zone = (fg_bin > 0) & ((eroded_3 == 0) | (alpha < 0.99))

    # 半透明境界から緑背景成分を引き算
    a3 = alpha[:, :, None]
    decontam = np.clip((rgb - (1.0 - a3) * bg_vec) / np.maximum(a3, 0.25), 0.0, 255.0)
    clean = np.where((a3 > 0.0) & (a3 < 0.96), decontam, rgb)

    # スピル帯で G > max(R, B) の余剰緑を max(R, B) へクランプして緑フチを完全消去
    cr, cg, cb = clean[:, :, 0], clean[:, :, 1], clean[:, :, 2]
    c_max_rb = np.maximum(cr, cb)
    despilled_g = np.minimum(cg, c_max_rb)
    # わずかな緑かぶりは境界帯全体で除去
    clean[:, :, 1] = np.where(spill_zone & (cg > c_max_rb), despilled_g, cg)

    out = np.zeros((img_rgb.shape[0], img_rgb.shape[1], 4), dtype=np.uint8)
    out[:, :, :3] = np.clip(clean, 0.0, 255.0).astype(np.uint8)
    out[:, :, 3] = np.clip(alpha * 255.0, 0.0, 255.0).astype(np.uint8)
    out[out[:, :, 3] == 0, :3] = 0
    return out


def _cutout_pil_image(pil_img: Image.Image, session=None) -> tuple[np.ndarray, str, object]:
    """
    画像を自動判別して最適な方法で透過RGBAへ変換する。
    1. 既に透過済みPNG（外周に十分な透明ピクセルあり）→ そのまま利用 ("pre_transparent")
    2. クロマキーグリーン背景（#00FF00系）→ 超高精度クロマキー＋スピル除去 ("chroma_green")
    3. 白背景等の通常背景 → アニメ特化AI `isnet-anime` ＋デフリンジ ("isnet_anime")
    """
    if pil_img.mode in ("RGBA", "LA"):
        arr_rgba = np.array(pil_img.convert("RGBA"))
        h, w = arr_rgba.shape[:2]
        m = max(4, min(20, h // 30, w // 30))
        corner_alpha = np.concatenate(
            [
                arr_rgba[:m, :m, 3].ravel(),
                arr_rgba[:m, -m:, 3].ravel(),
                arr_rgba[-m:, :m, 3].ravel(),
                arr_rgba[-m:, -m:, 3].ravel(),
            ]
        )
        if float(np.mean(corner_alpha < 20)) > 0.7:
            return arr_rgba, "pre_transparent", session

    pil_rgb = pil_img.convert("RGB")
    arr_rgb = np.array(pil_rgb)
    bg_rgb = _estimate_bg_rgb(arr_rgb)

    if _is_chroma_green_bg(bg_rgb):
        rgba = _remove_chroma_green(arr_rgb, bg_rgb=bg_rgb)
        return rgba, "chroma_green", session

    from rembg import new_session, remove

    if session is None:
        session = new_session("isnet-anime")
    pil_cut = remove(pil_rgb, session=session, post_process_mask=True)
    rgba = np.array(pil_cut.convert("RGBA"))
    rgba = _defringe_rgba(rgba, bg_rgb=bg_rgb)
    return rgba, "isnet_anime", session


def _align_to_canonical_face(rgba: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """
    基準立ち絵（full_normal.png: 1696x2528）の顔領域（目〜口）へマルチスケールテンプレートマッチングを行い、
    新衣装の顔位置・スケールを基準制服と完全一致させた (aligned_full_1696x2528, aligned_bust_825x840) を返す。
    これにより別衣装でも基準制服の瞬き・6感情・あいうえお口パクを1pxズレなく流用できる。
    """
    ref_full_path = os.path.join(CHAR_DIR, "full_normal.png")
    if not os.path.isfile(ref_full_path):
        return None
    ref_rgba = np.array(Image.open(ref_full_path).convert("RGBA"))
    ref_h, ref_w = ref_rgba.shape[:2]  # 2528, 1696

    # 基準立ち絵の顔テンプレート（眉〜口: x=715..985, y=315..495）
    tx1, ty1, tx2, ty2 = 715, 315, 985, 495
    tpl = cv2.cvtColor(ref_rgba[ty1:ty2, tx1:tx2, :3], cv2.COLOR_RGB2GRAY)

    # 入力画像を基準高さ (2528) に概算リサイズして探索空間を作る
    src_h, src_w = rgba.shape[:2]
    base_scale = float(ref_h) / float(max(1, src_h))
    search_h = ref_h
    search_w = max(1, int(round(src_w * base_scale)))
    resized_src = cv2.resize(rgba, (search_w, search_h), interpolation=cv2.INTER_LANCZOS4)
    # 顔は必ず画像の上位40%にある
    search_roi = cv2.cvtColor(resized_src[: int(search_h * 0.42), :, :3], cv2.COLOR_RGB2GRAY)

    best_score = -1.0
    best_s = 1.0
    best_loc = (0, 0)
    # 0.80x 〜 1.25x の範囲で顔スケールを探索
    for s in np.linspace(0.80, 1.25, 19):
        tw = max(16, int(round(tpl.shape[1] * s)))
        th = max(16, int(round(tpl.shape[0] * s)))
        if th >= search_roi.shape[0] or tw >= search_roi.shape[1]:
            continue
        tpl_s = cv2.resize(tpl, (tw, th), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(search_roi, tpl_s, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > best_score:
            best_score = float(max_val)
            best_s = float(s)
            best_loc = max_loc

    # 基準顔との相関が低すぎる場合は別構図とみなしてフォールバック
    if best_score < 0.32:
        return None

    # 入力画像を基準顔スケールと完全一致するよう再スケーリング
    final_scale = base_scale / best_s
    fw = max(1, int(round(src_w * final_scale)))
    fh = max(1, int(round(src_h * final_scale)))
    scaled_rgba = cv2.resize(rgba, (fw, fh), interpolation=cv2.INTER_LANCZOS4)

    # 検出された顔左上座標を最終スケールへ変換し、基準 (tx1, ty1) へ合わせるオフセットを算出
    det_x = int(round(best_loc[0] / best_s))
    det_y = int(round(best_loc[1] / best_s))
    off_x = tx1 - det_x
    off_y = ty1 - det_y

    # 1696x2528 の基準キャンバスへ配置
    canvas_full = np.zeros((ref_h, ref_w, 4), dtype=np.uint8)
    dx1 = max(0, off_x)
    dy1 = max(0, off_y)
    sx1 = max(0, -off_x)
    sy1 = max(0, -off_y)
    cw = min(ref_w - dx1, fw - sx1)
    ch = min(ref_h - dy1, fh - sy1)
    if cw <= 0 or ch <= 0:
        return None
    canvas_full[dy1 : dy1 + ch, dx1 : dx1 + cw] = scaled_rgba[sy1 : sy1 + ch, sx1 : sx1 + cw]

    # 基準制服 (normal_open_normal.png: 825x840) と完全に同一座標 (x=497..1198, y=35..749 -> 825x840) でバストアップ切り出し
    bx1, by1, bw, bh = 497, 35, 701, 714
    bust_patch = canvas_full[by1 : by1 + bh, bx1 : bx1 + bw]
    canvas_bust = cv2.resize(bust_patch, (825, 840), interpolation=cv2.INTER_LANCZOS4)
    print(f"[FaceSync] 基準顔へ自動アライメント成功 (score={best_score:.3f}, scale={final_scale:.3f}, offset=({off_x},{off_y}))")
    return canvas_full, canvas_bust


def build_outfit_from_pil(
    outfit_id: str,
    full_pil: Image.Image,
    *,
    bust_pil_in: Image.Image | None = None,
    bust_ratio: float = 0.28,
    flip_horizontal: bool = False,
    face_sync: bool = False,
    meta: dict | None = None,
    session=None,
) -> dict:
    """PIL画像から完全透過 full.png / bust.png と meta.json を生成・保存する。"""
    if flip_horizontal or outfit_id in FLIP_HORIZONTAL_IDS:
        full_pil = full_pil.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if bust_pil_in is not None:
            bust_pil_in = bust_pil_in.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    rgba, bg_mode, session = _cutout_pil_image(full_pil, session=session)

    out_dir = os.path.join(OUTFITS_DIR, outfit_id)
    os.makedirs(out_dir, exist_ok=True)
    full_path = os.path.join(out_dir, "full.png")
    bust_path = os.path.join(out_dir, "bust.png")

    face_sync_applied = False
    if face_sync and bust_pil_in is None:
        aligned = _align_to_canonical_face(rgba)
        if aligned is not None:
            aligned_full, aligned_bust = aligned
            Image.fromarray(aligned_full, mode="RGBA").save(full_path, format="PNG")
            bust_pil = Image.fromarray(aligned_bust, mode="RGBA")
            bust_pil.save(bust_path, format="PNG")
            h, w = aligned_full.shape[:2]
            face_sync_applied = True

    if not face_sync_applied:
        rgba = _crop_to_content(rgba, pad=12)
        h, w = rgba.shape[:2]
        if h > 1600:
            scale = 1600.0 / float(h)
            nw = max(1, int(round(w * scale)))
            nh = 1600
            rgba = np.array(Image.fromarray(rgba, mode="RGBA").resize((nw, nh), Image.Resampling.LANCZOS))
            h, w = rgba.shape[:2]
        Image.fromarray(rgba, mode="RGBA").save(full_path, format="PNG")

        if bust_pil_in is not None:
            b_rgba, _, session = _cutout_pil_image(bust_pil_in, session=session)
            b_rgba = _crop_to_content(b_rgba, pad=8)
            bh, bw = b_rgba.shape[:2]
            scale_b = 840.0 / float(max(1, bh))
            bust_pil = Image.fromarray(b_rgba, mode="RGBA").resize(
                (max(1, int(round(bw * scale_b))), 840), Image.Resampling.LANCZOS
            )
        else:
            bust_h = max(1, int(round(h * max(0.22, min(0.65, bust_ratio)))))
            bust_crop = rgba[:bust_h, :]
            target_w = max(bust_crop.shape[1], int(round(bust_h * 0.98)))
            bust_canvas = np.zeros((bust_h, target_w, 4), dtype=np.uint8)
            x_off = (target_w - bust_crop.shape[1]) // 2
            bust_canvas[:, x_off : x_off + bust_crop.shape[1]] = bust_crop
            if bust_h != 840:
                scale_b = 840.0 / float(bust_h)
                bust_pil = Image.fromarray(bust_canvas, mode="RGBA").resize(
                    (max(1, int(round(target_w * scale_b))), 840), Image.Resampling.LANCZOS
                )
            else:
                bust_pil = Image.fromarray(bust_canvas, mode="RGBA")
        bust_pil.save(bust_path, format="PNG")

    if meta:
        meta_path = os.path.join(out_dir, "meta.json")
        save_meta = {
            "id": outfit_id,
            "name": str(meta.get("name") or f"✨ {outfit_id}"),
            "short": str(meta.get("short") or meta.get("name") or outfit_id),
            "genre": str(meta.get("genre") or "custom"),
            "description": str(meta.get("description") or f"カスタム衣装 ({outfit_id})"),
            "bg_mode": bg_mode,
            "face_sync": face_sync_applied,
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(save_meta, f, ensure_ascii=False, indent=2)

    print(f"[OK] {outfit_id} ({bg_mode}, face_sync={face_sync_applied}): full={w}x{h} -> {full_path}, bust={bust_pil.width}x{bust_pil.height} -> {bust_path}")
    return {
        "outfit_id": outfit_id,
        "bg_mode": bg_mode,
        "face_sync": face_sync_applied,
        "full_path": full_path,
        "bust_path": bust_path,
        "full_size": [w, h],
        "bust_size": [bust_pil.width, bust_pil.height],
        "session": session,
    }


def build_outfit(
    outfit_id: str,
    src_path: str,
    session=None,
    bust_ratio: float = 0.28,
    bust_src_path: str | None = None,
    flip_horizontal: bool = False,
    meta: dict | None = None,
) -> tuple[str, str]:
    """元画像ファイルから完全透過 full.png と、基準制服と顔サイズ一致の bust.png を生成して保存する。"""
    if not os.path.exists(src_path):
        raise FileNotFoundError(f"元画像が見つかりません: {src_path}")
    full_pil = Image.open(src_path)
    bust_pil_in = Image.open(bust_src_path) if (bust_src_path and os.path.exists(bust_src_path)) else None
    res = build_outfit_from_pil(
        outfit_id=outfit_id,
        full_pil=full_pil,
        bust_pil_in=bust_pil_in,
        bust_ratio=bust_ratio,
        flip_horizontal=flip_horizontal,
        meta=meta,
        session=session,
    )
    return res["full_path"], res["bust_path"]


def main() -> int:
    parser = argparse.ArgumentParser(description="NIA 衣装アセット高精度透過ビルダー (クロマキー緑自動判別 / isnet-anime)")
    parser.add_argument("--id", help="登録する衣装ID（例: santa, halloween, bunny）")
    parser.add_argument("--src", help="全身元画像のパス（PNG/JPG）")
    parser.add_argument("--bust-src", help="バストアップ専用の高解像度元画像パス（任意・指定時はネイティブ高精細）")
    parser.add_argument("--bust-ratio", type=float, default=0.28, help="バストアップ切出し比率（既定 0.28 = 基準制服と顔サイズ完全一致）")
    parser.add_argument("--genre", default="custom", help="ジャンルID (school/casual/sport/swimwear/cosplay/custom)")
    parser.add_argument("--name", default="", help="UI表示名（例: 🎅 サンタコスプレ）")
    parser.add_argument("--flip", action="store_true", help="画像を左右反転する（和服の襟補正用）")
    parser.add_argument("--rebuild-defaults", action="store_true", help="標準収録の全衣装を一括再生成")
    args = parser.parse_args()

    if args.rebuild_defaults:
        from rembg import new_session
        session = new_session("isnet-anime")
        for oid, spath in DEFAULT_SOURCES.items():
            build_outfit(oid, spath, session=session, bust_ratio=args.bust_ratio)
        return 0

    if not args.id or not args.src:
        parser.print_help()
        return 1

    meta = {"name": args.name, "genre": args.genre} if (args.name or args.genre != "custom") else None
    build_outfit(
        args.id.strip(),
        args.src.strip(),
        bust_ratio=args.bust_ratio,
        bust_src_path=args.bust_src.strip() if args.bust_src else None,
        flip_horizontal=bool(args.flip),
        meta=meta,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
