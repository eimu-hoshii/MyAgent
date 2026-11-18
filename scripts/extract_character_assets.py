"""
D:\\Documents\\000MyTool\\MyAgent\\picture 内の高解像度キャラクター画像群から、
完全透過・デフリンジ済み、かつ瞬き・口パク時に目・口以外のピクセルが1ピクセルも動かない（チカチカゼロ）
高精細アバタースプライト群を生成するスクリプト。

修正ポイント:
1. 各感情のベース画像を全身原画（通常・微笑み・喜び・怒り・驚き・困り）から直接切り出すことで、
   別表情に「通常時の眉毛の端」が残る現象を完全に解消し、かつ全身表示モードでの口パク・瞬き合成とも100%一致させる。
2. 口の開き量（あ・い・う・え・お）を約52%（半分程度）に抑える局所ワープ処理を導入し、自然で上品なリップシンクを実現。
3. 瞬き時は目のアーチに沿ったポリゴンマスクで「目領域のみ」を差し替え、眉や髪・服の揺れを完全にゼロ化（差分 0.0000）。
"""

import os
import cv2
import numpy as np


PICTURE_DIR = r"D:/Documents/000MyTool/MyAgent/picture"
OUTPUT_DIR = r"d:/Documents/000MyTool/MyAgent/frontend/assets/character"
SCRATCH_DIR = r"C:/Users/shima/.gemini/antigravity/brain/f3966c0a-e1a5-4ae2-942a-00721f7b5f19/scratch"

VOWELS = ["normal", "a", "i", "u", "e", "o"]


def load_image_unicode(filepath: str) -> np.ndarray:
    """日本語ファイル名を含むパスからOpenCV画像を読み込む。"""
    buf = np.fromfile(filepath, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"画像の読み込みに失敗しました: {filepath}")
    return img


def fill_between_dark_edges(
    protect: np.ndarray,
    gray: np.ndarray,
    y1: int,
    y2: int,
    x1: int,
    x2: int,
    dark_thresh: int = 165,
) -> None:
    """指定矩形内の各行yで、左端の暗い輪郭線から右端の暗い輪郭線までの区間をすべて保護(255)にする。"""
    for y in range(y1, y2):
        row = gray[y, x1:x2]
        dark_idx = np.where(row < dark_thresh)[0]
        if len(dark_idx) >= 2:
            protect[y, x1 + dark_idx[0] : x1 + dark_idx[-1] + 1] = 255


def compute_bust_protection_mask(bgr: np.ndarray) -> np.ndarray:
    """
    バストアップ画像 (825x840) の内部白パーツ（パーカー・フード裏地・白目・口内・右胸白ライン）を
    100%保護し、外周およびツインテール・顎横の微小な背景隙間のみを透過対象にする保護マスクを生成する。
    """
    h, w, _ = bgr.shape
    img_f = bgr.astype(np.float32)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    hoodie_passable = (gray > 105).astype(np.uint8) * 255
    ff_mask = np.zeros((h + 2, w + 2), dtype=np.uint8)

    hoodie_seeds = [
        (205, 520), (235, 520), (276, 525), (295, 535), (335, 545),
        (585, 530), (545, 530), (520, 540), (490, 555),
        (205, 650), (235, 600), (276, 610), (295, 610), (335, 625),
        (585, 670), (520, 620), (490, 645),
        (420, 740), (355, 710), (490, 720),
        (385, 810), (460, 810), (520, 810),
    ]
    for sx, sy in hoodie_seeds:
        if sx < w and sy < h and hoodie_passable[sy, sx] == 255:
            cv2.floodFill(hoodie_passable, ff_mask, (sx, sy), 128)

    protect = (hoodie_passable == 128).astype(np.uint8) * 255
    kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (19, 19))
    protect = cv2.morphologyEx(protect, cv2.MORPH_CLOSE, kernel_close)

    # 暖色ピクセル（肌・赤い瞳・口）を保護
    is_warm = (img_f[:, :, 2] - img_f[:, :, 0]) > 14.0
    protect[is_warm] = 255

    # 顔パーツ保護（目: x=275..568, y=345..428 / 口: x=365..475, y=462..525）
    cv2.rectangle(protect, (275, 345), (568, 428), 255, -1)
    cv2.rectangle(protect, (365, 462), (475, 525), 255, -1)

    # 右胸ポケットの白ライン保護
    cv2.rectangle(protect, (620, 670), (705, 795), 255, -1)

    # 顎横・パーカー襟上の極小背景三角形のみピンポイントでクリア
    protect[455:481, 282:299] = 0
    protect[462:488, 540:556] = 0

    return protect


def apply_bust_transparency(bgr: np.ndarray, protect: np.ndarray, fixed_alpha: np.ndarray | None = None) -> np.ndarray:
    """
    バストアップ画像に連続アルファマットとColor Decontamination（背景白成分除去）を適用してBGRAを返す。
    """
    img_f = bgr.astype(np.float32)
    bg_col = np.array([242.0, 236.0, 235.0], dtype=np.float32)

    if fixed_alpha is None:
        color_dist = np.linalg.norm(img_f - bg_col, axis=2)
        outside_alpha = np.clip((color_dist - 55.0) / 105.0, 0.0, 1.0)
        outside_alpha = np.power(outside_alpha, 1.5)
        alpha = np.where(protect > 0, 1.0, outside_alpha).astype(np.float32)

        alpha_eroded = cv2.erode(alpha, np.ones((3, 3), np.uint8), iterations=1)
        alpha = np.where(protect > 0, 1.0, alpha * 0.35 + alpha_eroded * 0.65)
        alpha = cv2.GaussianBlur(alpha, (3, 3), 0.7)
        alpha = np.where(protect > 0, 1.0, alpha)
        alpha = np.where(alpha < 0.08, 0.0, alpha)
    else:
        alpha = fixed_alpha

    a_3d = alpha[:, :, None]
    decontam = np.clip((img_f - (1.0 - a_3d) * bg_col) / np.maximum(a_3d, 0.25), 0, 255)
    edge_darken = np.clip(a_3d + 0.15, 0.3, 1.0)
    clean_bgr = np.where((protect[:, :, None] == 0) & (a_3d < 0.96), decontam * edge_darken, img_f).astype(np.uint8)

    bgra = cv2.cvtColor(clean_bgr, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)
    return bgra


def remove_bg_full_body(bgr: np.ndarray) -> np.ndarray:
    """全身立ち絵 (1696x2528) を完全透過・デフリンジ処理してBGRAを返す。"""
    h, w, _ = bgr.shape
    img_f = bgr.astype(np.float32)
    bg_col = np.array([243.0, 238.0, 237.0], dtype=np.float32)
    color_dist = np.linalg.norm(img_f - bg_col, axis=2)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    protect = np.zeros((h, w), dtype=np.uint8)

    is_warm = (img_f[:, :, 2] - img_f[:, :, 0]) > 14.0
    protect[is_warm] = 255

    hy1, hy2, hx1, hx2 = 430, 1150, 635, 1065
    h_sub = (gray[hy1:hy2, hx1:hx2] > 105).astype(np.uint8) * 255
    h_mask = np.zeros((hy2 - hy1 + 2, hx2 - hx1 + 2), dtype=np.uint8)
    h_seeds = [
        (675, 480), (700, 480), (735, 485), (775, 500),
        (995, 490), (960, 485), (945, 500), (915, 520),
        (675, 550), (700, 515), (735, 520), (775, 535),
        (995, 570), (945, 530), (915, 550),
        (850, 630), (800, 610), (905, 615),
        (850, 780), (850, 920), (850, 1080), (800, 1080), (920, 1080),
    ]
    for sx, sy in h_seeds:
        lx, ly = sx - hx1, sy - hy1
        if 0 <= lx < (hx2 - hx1) and 0 <= ly < (hy2 - hy1) and h_sub[ly, lx] == 255:
            cv2.floodFill(h_sub, h_mask, (lx, ly), 128)
    h_prot = (h_sub == 128).astype(np.uint8) * 255
    h_prot = cv2.morphologyEx(h_prot, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (19, 19)))
    protect[hy1:hy2, hx1:hx2] = np.maximum(protect[hy1:hy2, hx1:hx2], h_prot)

    sock_prot = np.zeros((h, w), dtype=np.uint8)
    fill_between_dark_edges(sock_prot, gray, 1770, 2300, 660, 815, dark_thresh=175)
    fill_between_dark_edges(sock_prot, gray, 1770, 2300, 880, 1035, dark_thresh=175)
    sock_prot = cv2.morphologyEx(sock_prot, cv2.MORPH_CLOSE, np.ones((35, 7), dtype=np.uint8))
    protect = np.maximum(protect, sock_prot)

    cv2.rectangle(protect, (730, 325), (980, 405), 255, -1)
    cv2.rectangle(protect, (805, 425), (900, 480), 255, -1)
    cv2.rectangle(protect, (1020, 600), (1095, 715), 255, -1)

    protect[425:447, 736:751] = 0
    protect[431:453, 956:970] = 0

    in_shoes = np.zeros((h, w), dtype=bool)
    in_shoes[2240:2485, 682:822] = True
    in_shoes[2240:2485, 878:1012] = True
    shadow_zone = np.zeros((h, w), dtype=bool)
    shadow_zone[2200:, :] = True
    shadow_zone = shadow_zone & (~in_shoes)

    thresh = np.where(shadow_zone, 110.0, 55.0)
    outside_alpha = np.clip((color_dist - thresh) / 105.0, 0.0, 1.0)
    outside_alpha = np.power(outside_alpha, 1.5)
    alpha = np.where(protect > 0, 1.0, outside_alpha).astype(np.float32)

    alpha_eroded = cv2.erode(alpha, np.ones((3, 3), np.uint8), iterations=1)
    alpha = np.where(protect > 0, 1.0, alpha * 0.35 + alpha_eroded * 0.65)
    alpha = cv2.GaussianBlur(alpha, (3, 3), 0.7)
    alpha = np.where(protect > 0, 1.0, alpha)
    alpha = np.where(alpha < 0.08, 0.0, alpha)

    a_3d = alpha[:, :, None]
    decontam = np.clip((img_f - (1.0 - a_3d) * bg_col) / np.maximum(a_3d, 0.25), 0, 255)
    edge_darken = np.clip(a_3d + 0.15, 0.3, 1.0)
    clean_bgr = np.where((protect[:, :, None] == 0) & (a_3d < 0.96), decontam * edge_darken, img_f).astype(np.uint8)

    bgra = cv2.cvtColor(clean_bgr, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)
    return bgra


def reduce_mouth_opening(img: np.ndarray, factor: float = 0.52) -> np.ndarray:
    """
    口の縦方向の開き量を約半分（factor=0.52）に縮小する局所ワープ処理。
    鼻 (y<=458) や顎先 (y>=526) は一切動かさず、口の中心線 (cx=420, cy=492) に向かって上下唇を引き寄せる。
    """
    h, w, _ = img.shape
    grid_x, grid_y = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    wx = np.exp(-((grid_x - 420.0) ** 2) / (2.0 * (34.0 ** 2)))
    wy = np.exp(-((grid_y - 492.0) ** 2) / (2.0 * (14.5 ** 2)))
    weight = wx * wy
    # 鼻や顎の境界外では重みを完全に0にする
    weight = np.where((grid_y < 460.0) | (grid_y > 524.0), 0.0, weight)

    scale_y = 1.0 - (1.0 - factor) * weight
    scale_x = 1.0 - 0.12 * weight
    src_y = 492.0 + (grid_y - 492.0) / scale_y
    src_x = 420.0 + (grid_x - 420.0) / scale_x
    return cv2.remap(
        img,
        src_x.astype(np.float32),
        src_y.astype(np.float32),
        interpolation=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REPLICATE,
    )


def extract_scale_aligned_mouth_panels(open_grid: np.ndarray) -> dict[str, np.ndarray]:
    """
    目開け口パターン.jpg の6セル（通常・あ・い・う・え・お）を鼻・顎輪郭基準でマルチスケール位置合わせし、
    さらに開口母音（a, i, u, e, o）の開き量を約半分（52%）に抑えた画像を返す。
    """
    cells = [
        ("normal", 0, 832, 0, 842),
        ("a", 846, 1682, 0, 842),
        ("i", 1696, 2528, 0, 842),
        ("u", 0, 832, 855, 1696),
        ("e", 846, 1682, 855, 1696),
        ("o", 1696, 2528, 855, 1696),
    ]

    base = open_grid[0:840, 0:825]
    tmpl = base[420:540, 270:570].astype(np.float32)
    mask = np.ones((120, 300, 3), dtype=np.float32)
    mask[45:102, 100:200] = 0.0

    aligned: dict[str, np.ndarray] = {"normal": base.copy()}

    for name, x1, x2, y1, y2 in cells[1:]:
        cell = open_grid[y1:y2, x1:x2]
        best = (1e18, 1.0, 0, 0)
        for s in np.linspace(0.955, 1.005, 21):
            scaled = cv2.resize(cell, (0, 0), fx=s, fy=s, interpolation=cv2.INTER_LANCZOS4)
            for dy in range(-16, 16):
                for dx in range(-18, 18):
                    y0, x0 = 420 + dy, 270 + dx
                    if y0 < 0 or x0 < 0 or y0 + 120 > scaled.shape[0] or x0 + 300 > scaled.shape[1]:
                        continue
                    patch = scaled[y0 : y0 + 120, x0 : x0 + 300].astype(np.float32)
                    err = float(np.abs((patch - tmpl) * mask).mean())
                    if err < best[0]:
                        best = (err, float(s), int(dx), int(dy))

        _, s, dx, dy = best
        M = np.float32([[s, 0, -dx], [0, s, -dy]])
        warped = cv2.warpAffine(
            cell, M, (825, 840), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE
        )
        # 口の開き量を約半分（52%）に縮小して自然な会話口パクにする
        aligned[name] = reduce_mouth_opening(warped, factor=0.52)

    return aligned


def blend_mouth_patch(base_bgr: np.ndarray, vowel_bgr: np.ndarray, mouth_mask: np.ndarray) -> np.ndarray:
    """
    base_bgr の口領域のみに vowel_bgr の口を肌色トーン補正付きで合成する。
    """
    ring = (mouth_mask[:, :, 0] > 0.02) & (mouth_mask[:, :, 0] < 0.45)
    if np.any(ring):
        diff_skin = np.median(base_bgr[ring].astype(np.float32) - vowel_bgr[ring].astype(np.float32), axis=0)
    else:
        diff_skin = np.zeros(3, dtype=np.float32)

    v_f = vowel_bgr.astype(np.float32)
    base_skin_color = np.median(base_bgr[ring].astype(np.float32), axis=0) if np.any(ring) else np.array([115.0, 130.0, 180.0])
    skin_dist = np.linalg.norm(v_f - base_skin_color, axis=2, keepdims=True)
    skin_weight = np.clip(1.0 - (skin_dist - 15.0) / 35.0, 0.0, 1.0)
    v_corrected = np.clip(v_f + diff_skin * skin_weight, 0, 255)

    out = base_bgr.astype(np.float32) * (1.0 - mouth_mask) + v_corrected * mouth_mask
    return out.astype(np.uint8)


def main() -> None:
    """高解像度アセット抽出・生成メイン処理。"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(SCRATCH_DIR, exist_ok=True)

    print("[1/4] 高解像度原画ファイルを読み込み中...")
    open_grid = load_image_unicode(os.path.join(PICTURE_DIR, "目開け口パターン.jpg"))
    closed_grid = load_image_unicode(os.path.join(PICTURE_DIR, "目閉じ口パターン.jpg"))

    emotion_files = {
        "normal": "通常.jpg",
        "smile": "微笑み.jpg",
        "joy": "喜び.jpg",
        "serious": "怒り.jpg",
        "surprised": "驚き.jpg",
        "troubled": "困り.jpg",
    }
    full_emotions = {
        emo: load_image_unicode(os.path.join(PICTURE_DIR, fname))
        for emo, fname in emotion_files.items()
    }

    print("[2/4] 6母音パネルのスケール位置合わせ＋開口量50%縮小処理を実行中...")
    aligned_open_vowels = extract_scale_aligned_mouth_panels(open_grid)
    c_closed_base = closed_grid[0:840, 0:825].copy()

    # 口領域のみの差し替えマスク (x:370..470, y:465..520)
    mouth_mask = np.zeros((840, 825), dtype=np.float32)
    cv2.rectangle(mouth_mask, (370, 465), (470, 520), 1.0, -1)
    mouth_mask = cv2.GaussianBlur(mouth_mask, (13, 13), 3.5)
    mouth_mask = np.clip(mouth_mask * 1.35, 0.0, 1.0)[:, :, None]

    # 目領域のみの差し替えポリゴンマスク（各感情の眉毛を100%保護しつつ目のアーチのみをカバー）
    eye_mask = np.zeros((840, 825), dtype=np.float32)
    left_poly = np.array([[270, 355], [295, 344], [335, 344], [390, 368], [392, 422], [270, 422]], dtype=np.int32)
    right_poly = np.array([[448, 368], [505, 345], [545, 345], [570, 355], [570, 424], [448, 424]], dtype=np.int32)
    cv2.fillPoly(eye_mask, [left_poly, right_poly], 1.0)
    eye_mask = cv2.GaussianBlur(eye_mask, (13, 13), 3.2)
    eye_mask = np.clip(eye_mask * 1.35, 0.0, 1.0)[:, :, None]

    print("[3/4] 6感情 × 12状態（眉毛残りゼロ・半開口リップシンク）の透過PNGを生成中...")
    preview_rows = []

    for emo_key, full_bgr in full_emotions.items():
        # 全身原画から直接バストアップを切り出す（別表情に通常眉の端が残る現象を完全に防止＆全身表示と100%一致）
        up_bust = cv2.resize(
            full_bgr[35:749, 497:1198], (825, 840), interpolation=cv2.INTER_LANCZOS4
        )
        M_align = np.float32([[1, 0, 0], [0, 1, -4]])
        emo_base = cv2.warpAffine(up_bust, M_align, (825, 840), borderMode=cv2.BORDER_REPLICATE)

        # この感情のベース画像から固定アルファチャンネルを生成（同一感情内の瞬き・口パクでの輪郭ブレ0）
        emo_protect = compute_bust_protection_mask(emo_base)
        emo_canonical_bgra = apply_bust_transparency(emo_base, emo_protect)
        emo_alpha = emo_canonical_bgra[:, :, 3].astype(np.float32) / 255.0

        row_thumbs = []

        for v_name in VOWELS:
            # --- (A) 目開け + 母音 v_name ---
            if v_name == "normal":
                open_bgr = emo_base.copy()
            else:
                open_bgr = blend_mouth_patch(emo_base, aligned_open_vowels[v_name], mouth_mask)

            open_bgra = apply_bust_transparency(open_bgr, emo_protect, fixed_alpha=emo_alpha)
            cv2.imwrite(os.path.join(OUTPUT_DIR, f"{emo_key}_open_{v_name}.png"), open_bgra)

            # --- (B) 目閉じ（瞬き） + 母音 v_name ---
            blink_bgr = (
                open_bgr.astype(np.float32) * (1.0 - eye_mask)
                + c_closed_base.astype(np.float32) * eye_mask
            ).astype(np.uint8)
            blink_bgra = apply_bust_transparency(blink_bgr, emo_protect, fixed_alpha=emo_alpha)
            cv2.imwrite(os.path.join(OUTPUT_DIR, f"{emo_key}_blink_{v_name}.png"), blink_bgra)

            if emo_key == "normal":
                cv2.imwrite(os.path.join(OUTPUT_DIR, f"blink_{v_name}.png"), blink_bgra)

            if v_name in ("normal", "a", "e", "o"):
                a_f = open_bgra[:, :, 3:4].astype(np.float32) / 255.0
                dark_bg = np.full_like(open_bgr, (20, 11, 7), dtype=np.uint8)
                comp = (open_bgra[:, :, :3].astype(np.float32) * a_f + dark_bg * (1.0 - a_f)).astype(np.uint8)
                row_thumbs.append(cv2.resize(comp, (180, 183), interpolation=cv2.INTER_AREA))

        b_normal_bgr = (
            emo_base.astype(np.float32) * (1.0 - eye_mask)
            + c_closed_base.astype(np.float32) * eye_mask
        ).astype(np.uint8)
        b_normal_bgra = apply_bust_transparency(b_normal_bgr, emo_protect, fixed_alpha=emo_alpha)
        a_f = b_normal_bgra[:, :, 3:4].astype(np.float32) / 255.0
        dark_bg = np.full_like(emo_base, (20, 11, 7), dtype=np.uint8)
        comp_b = (b_normal_bgra[:, :, :3].astype(np.float32) * a_f + dark_bg * (1.0 - a_f)).astype(np.uint8)
        row_thumbs.append(cv2.resize(comp_b, (180, 183), interpolation=cv2.INTER_AREA))

        preview_rows.append(np.hstack(row_thumbs))

    cv2.imwrite(os.path.join(SCRATCH_DIR, "hires_sprites_verify_v4.png"), np.vstack(preview_rows))

    print("[4/4] 全身立ち絵 6種類（高解像度透過PNG）を生成中...")
    for emo_key, full_bgr in full_emotions.items():
        full_bgra = remove_bg_full_body(full_bgr)
        resized_full = cv2.resize(full_bgra, (1018, 1517), interpolation=cv2.INTER_AREA)
        cv2.imwrite(os.path.join(OUTPUT_DIR, f"full_{emo_key}.png"), resized_full)
        if emo_key == "normal":
            cv2.imwrite(os.path.join(OUTPUT_DIR, "full_front.png"), resized_full)

    print("すべての高解像度キャラクターアセット（72枚のバストアップ＋6枚の全身立ち絵）の生成が完了しました！")


if __name__ == "__main__":
    main()
