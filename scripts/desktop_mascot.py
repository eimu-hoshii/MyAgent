"""
Windows ネイティブ・完全背景透過・枠なし・最前面（Always on Top）対応のデスクトップマスコット（NIA / ニア）。
- 四角いウィンドウ枠やアドレスバーを一切排し、デスクトップ上にキャラクター立ち絵単体のみを透過表示する。
- 普段は立ち絵のみを表示し、発話時のみ頭上に吹き出しを表示（数秒後に自動フェードアウト）。
- ダブルクリックで足元のミニ入力バーを開閉、右クリックでコンテキストメニュー（全身/バストアップ切替、サイズ変更、画面認識、クリックスルー切替、終了）を表示。
"""

import ctypes
import json
import os
import random
import time
import tkinter as tk
import urllib.request
import webbrowser
from PIL import Image, ImageTk

# 高DPIディスプレイでのボヤけ防止（Windows Per-Monitor DPI Aware）
if os.name == "nt":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAR_DIR = os.path.join(BASE_DIR, "frontend", "assets", "character")
API_BASE = "http://127.0.0.1:8000"
# キャラクターの黒髪のフチに馴染む極暗色を透過カラーキーに使用
TRANSPARENT_KEY = "#040507"
KEY_RGB = (4, 5, 7)
# 吹き出し表示時にキャラクター位置が上下に動かないよう、頭上・足元の固定予約領域を確保する
BUBBLE_AREA_H = 165
INPUT_AREA_H = 42


class NativeDesktopMascot:
    """ウィンドウ枠ゼロ・完全透過でデスクトップ上にニアを常駐させるクラス。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("NIA Mascot")
        self.root.overrideredirect(True)
        self.root.config(bg=TRANSPARENT_KEY)
        self.root.wm_attributes("-transparentcolor", TRANSPARENT_KEY)
        self.root.wm_attributes("-topmost", True)
        self._applied_topmost = True
        self._desired_topmost = True
        self._menu_open = False

        self.view_mode = "full"  # デスクトップマスコットらしくデフォルトは「全身立ち絵 ("full")」、右クリックで "bust" 切替可
        self.scale = 1.0
        self.char_offset_y = 0
        self.position_mode = "bottom-right"
        self.click_through_hover = False
        self.current_emotion = "normal"
        self.is_blinking = False
        self.next_blink_time = time.time() + 2.8
        self.blink_end_time = 0.0
        self.speak_until = 0.0
        self.bubble_hide_at = time.time() + 6.0
        self.last_speech_seen = ""
        self.show_input_bar = False
        self._bubble_visible = True

        self.sprite_cache: dict[str, ImageTk.PhotoImage] = {}
        self.raw_full_cache: dict[str, Image.Image] = {}
        self.raw_bust_cache: dict[str, Image.Image] = {}

        self._drag_start_x = 0
        self._drag_start_y = 0

        self._load_config()
        self._build_ui()
        self._build_context_menu()
        self._reposition_window()
        self._force_visible()

        self._tick_animation()
        self._tick_backend_sync()

    def _force_visible(self) -> None:
        """Windows上でSW_HIDE継承等により非表示になるのを防ぎ、確実に最前面表示する。"""
        self.root.deiconify()
        self.root.lift()
        self.root.update_idletasks()
        if os.name == "nt":
            try:
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
                ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW
                ctypes.windll.user32.SetWindowPos(
                    hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040
                )  # HWND_TOPMOST | SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
            except Exception:
                pass

    def _load_config(self) -> None:
        """バックエンドからマスコット設定を取得する。"""
        try:
            req = urllib.request.urlopen(f"{API_BASE}/api/status", timeout=1.5)
            data = json.loads(req.read().decode("utf-8"))
            cfg = data.get("config", {})
            self.scale = float(cfg.get("mascot_scale", 1.0))
            self.position_mode = str(cfg.get("mascot_position", "bottom-right"))
            self.click_through_hover = bool(cfg.get("mascot_click_through", False))
            self.root.wm_attributes("-topmost", bool(cfg.get("mascot_always_on_top", True)))
            recent = data.get("recent_last")
            if recent and recent.get("agent_speech"):
                self.last_speech_seen = recent["agent_speech"]
                self.current_emotion = recent.get("emotion", "normal")
        except Exception:
            pass

    def _get_canvas_dimensions(self) -> tuple[int, int]:
        """現在の表示モード（全身/バストアップ）と倍率に応じた描画サイズを返す。"""
        if self.view_mode == "full":
            w = int(420 * self.scale)
            h = int(620 * self.scale)
        else:
            w = int(350 * self.scale)
            h = int(356 * self.scale)
        return max(160, w), max(160, h)

    def _get_window_dimensions(self) -> tuple[int, int, int, int]:
        """吹き出し・入力バーの固定予約領域を含めたウィンドウ全体サイズとキャンバスサイズを返す。"""
        cw, ch = self._get_canvas_dimensions()
        ww = max(320, cw + 20)
        wh = BUBBLE_AREA_H + ch + INPUT_AREA_H
        return ww, wh, cw, ch

    def _place_bubble(self) -> None:
        """吹き出しをキャラクター頭上の固定基準位置（下端アンカー）へ配置する。"""
        ww, _, _, _ = self._get_window_dimensions()
        wrap_w = min(380, max(250, ww - 24))
        self.bubble_label.config(wraplength=wrap_w)
        self.bubble_frame.place(x=ww // 2, y=BUBBLE_AREA_H - 2, anchor="s")
        self.bubble_frame.lift()

    def _place_input_bar(self) -> None:
        """足元のミニ入力バーをキャラクター下部の固定領域へ配置する。"""
        ww, _, _, ch = self._get_window_dimensions()
        self.input_frame.place(x=10, y=BUBBLE_AREA_H + ch + 4, width=ww - 20, height=34)
        self.input_frame.lift()

    def _layout_widgets(self) -> None:
        """キャラクター位置が吹き出し・入力欄の有無で動かないよう全ウィジェットを絶対座標配置する。"""
        ww, _, cw, ch = self._get_window_dimensions()
        self.canvas.config(width=cw, height=ch)
        self.canvas.place(x=(ww - cw) // 2, y=BUBBLE_AREA_H, width=cw, height=ch)

        if self._bubble_visible:
            self._place_bubble()
        else:
            self.bubble_frame.place_forget()

        if self.show_input_bar:
            self._place_input_bar()
        else:
            self.input_frame.place_forget()

    def _build_ui(self) -> None:
        """吹き出し・透過キャラクターキャンバス・開閉式ミニ入力欄を構築する。"""
        self.container = tk.Frame(self.root, bg=TRANSPARENT_KEY, bd=0, highlightthickness=0)
        self.container.pack(fill="both", expand=True)

        # キャラクター描画キャンバス（完全背景透過・座標固定）
        cw, ch = self._get_canvas_dimensions()
        self.canvas = tk.Canvas(
            self.container,
            width=cw,
            height=ch,
            bg=TRANSPARENT_KEY,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )

        # 頭上セリフ吹き出し（発話時のみ固定予約領域に下端合わせで表示）
        init_text = (
            self.last_speech_seen
            or "……べ、別にあなたのために待ってたわけじゃないわよ。\n（ダブルクリックで会話 / 右クリックでメニュー）"
        )
        self.bubble_frame = tk.Frame(
            self.container,
            bg="#38bdf8",
            bd=0,
            padx=1,
            pady=1,
        )

        self.bubble_label = tk.Label(
            self.bubble_frame,
            text=init_text,
            bg="#0b1324",
            fg="#f8fafc",
            font=("Meiryo UI", 9, "bold"),
            wraplength=340,
            justify="left",
            padx=10,
            pady=6,
        )
        self.bubble_label.pack()

        # 足元のミニ入力バー（デフォルト非表示、ダブルクリックまたは右クリックで開閉）
        self.input_frame = tk.Frame(self.container, bg="#0b1324", bd=1, relief="solid")
        self.entry = tk.Entry(
            self.input_frame,
            bg="#131c31",
            fg="#ffffff",
            insertbackground="#38bdf8",
            font=("Meiryo UI", 9),
            bd=0,
            width=20,
        )
        self.entry.pack(side="left", padx=6, pady=4, fill="x", expand=True)
        self.entry.bind("<Return>", self._on_send_chat)

        btn_vision = tk.Button(
            self.input_frame,
            text="📸",
            bg="#0284c7",
            fg="#ffffff",
            font=("Meiryo UI", 8, "bold"),
            bd=0,
            padx=5,
            command=self._on_capture_vision,
        )
        btn_vision.pack(side="left", padx=2, pady=3)

        self._layout_widgets()

        # イベントバインド（左ドラッグ移動、マウスホイール拡大縮小、ダブルクリックで入力欄開閉、右クリックメニュー）
        for w in (self.canvas, self.bubble_frame, self.bubble_label):
            w.bind("<ButtonPress-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_motion)
            w.bind("<MouseWheel>", self._on_mouse_wheel)
            w.bind("<Double-Button-1>", self._toggle_input_bar)
            w.bind("<Button-3>", self._show_context_menu)
            w.bind("<Enter>", self._on_mouse_enter)
            w.bind("<Leave>", self._on_mouse_leave)

    def _on_mouse_wheel(self, event) -> None:
        """マウスホイール回転でマスコットのサイズを拡大・縮小する。"""
        delta = 0.1 if event.delta > 0 else -0.1
        new_scale = max(0.55, min(2.2, round(self.scale + delta, 2)))
        if new_scale != self.scale:
            self._set_scale(new_scale)

    def _shift_char_y(self, dy: int) -> None:
        """キャラクターの上下描画位置を調整する。"""
        self.char_offset_y = max(-250, min(250, self.char_offset_y + dy))

    def _build_context_menu(self) -> None:
        """右クリックで開くマスコット専用コンテキストメニューを構築する。"""
        self.menu = tk.Menu(
            self.root,
            tearoff=0,
            bg="#0d1526",
            fg="#f1f5f9",
            activebackground="#0284c7",
            activeforeground="#ffffff",
            font=("Meiryo UI", 9),
        )
        self.menu.add_command(label="💬 ミニ入力欄を表示 / 非表示", command=self._toggle_input_bar)
        self.menu.add_command(label="🧍 全身立ち絵 ⇄ バストアップ切替", command=self._toggle_view_mode)
        self.menu.add_separator()
        self.menu.add_command(label="➕ サイズを拡大 (マウスホイール上でも可)", command=lambda: self._set_scale(min(2.2, round(self.scale + 0.15, 2))))
        self.menu.add_command(label="➖ サイズを縮小 (マウスホイール下でも可)", command=lambda: self._set_scale(max(0.55, round(self.scale - 0.15, 2))))
        self.menu.add_command(label="⬆️ キャラ表示位置を上へ移動", command=lambda: self._shift_char_y(-30))
        self.menu.add_command(label="⬇️ キャラ表示位置を下へ移動", command=lambda: self._shift_char_y(30))
        self.menu.add_command(label="🔄 サイズ・上下位置を標準に戻す (100%)", command=lambda: (setattr(self, "char_offset_y", 0), self._set_scale(1.0)))
        self.menu.add_separator()
        self.menu.add_command(label="📸 今の画面を見せて相談 (Vision)", command=self._on_capture_vision)
        self.menu.add_command(label="📋 クリップボードを解析させる", command=self._on_analyze_clipboard)
        self.menu.add_separator()
        self.menu.add_command(label="👻 ホバー時透過 (邪魔にならないモード) ON/OFF", command=self._toggle_hover_ghost)
        self.menu.add_command(label="🖥️ メイン管制コンソールをブラウザで開く", command=lambda: webbrowser.open(API_BASE))
        self.menu.add_separator()
        self.menu.add_command(label="❌ マスコットを閉じる", command=self.root.destroy)

    def _show_context_menu(self, event) -> None:
        # 右クリックメニュー表示中に _tick_backend_sync が -topmost を再適用すると
        # ニアがメニューの前面に出てしまうため、メニュー表示中は一時的に無効化する
        self._menu_open = True
        try:
            self.root.wm_attributes("-topmost", False)
            self._applied_topmost = False
        except Exception:
            pass
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                self.menu.grab_release()
            except Exception:
                pass
            self.root.after(250, self._restore_topmost_after_menu)

    def _restore_topmost_after_menu(self) -> None:
        self._menu_open = False
        try:
            if self._applied_topmost != self._desired_topmost:
                self.root.wm_attributes("-topmost", self._desired_topmost)
                self._applied_topmost = self._desired_topmost
        except Exception:
            pass

    def _toggle_input_bar(self, _event=None) -> None:
        self.show_input_bar = not self.show_input_bar
        if self.show_input_bar:
            self._place_input_bar()
            self.entry.focus_set()
            # 入力欄を開いたときは吹き出しも表示（キャラクター位置は動かさない）
            self.bubble_hide_at = time.time() + 15.0
            self._bubble_visible = True
            self._place_bubble()
        else:
            self.input_frame.place_forget()

    def _toggle_view_mode(self) -> None:
        self.view_mode = "bust" if self.view_mode == "full" else "full"
        self._reposition_window(keep_current_xy=True)

    def _set_scale(self, new_scale: float) -> None:
        self.scale = new_scale
        self.sprite_cache.clear()
        self._reposition_window(keep_current_xy=True)

    def _toggle_hover_ghost(self) -> None:
        self.click_through_hover = not self.click_through_hover
        self._show_bubble(
            "ホバー時透過モードを ON にしたわよ。" if self.click_through_hover else "ホバー時透過モードを OFF にしたわ。",
            duration=3.5,
        )

    def _show_bubble(self, text: str, duration: float = 7.0) -> None:
        """吹き出しにテキストを表示し、指定秒数後に自動で隠す（キャラクター座標は固定）。"""
        self.bubble_label.config(text=text)
        self.bubble_hide_at = time.time() + duration
        self._bubble_visible = True
        self._place_bubble()

    def _reposition_window(self, keep_current_xy: bool = False) -> None:
        self.root.update_idletasks()
        ww, wh, _, _ = self._get_window_dimensions()
        self._layout_widgets()

        if keep_current_xy:
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            self.root.geometry(f"{ww}x{wh}+{x}+{y}")
            return

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        margin_x = 16
        margin_y = 24
        x = margin_x if "left" in self.position_mode else max(0, sw - ww - margin_x)
        y = 12 if "top" in self.position_mode else max(0, sh - wh - margin_y)
        self.root.geometry(f"{ww}x{wh}+{x}+{y}")

    def _on_drag_start(self, event) -> None:
        self._drag_start_x = event.x_root - self.root.winfo_x()
        self._drag_start_y = event.y_root - self.root.winfo_y()

    def _on_drag_motion(self, event) -> None:
        nx = event.x_root - self._drag_start_x
        ny = event.y_root - self._drag_start_y
        self.root.geometry(f"+{nx}+{ny}")

    def _on_mouse_enter(self, _event) -> None:
        if self.click_through_hover:
            self.root.wm_attributes("-alpha", 0.28)

    def _on_mouse_leave(self, _event) -> None:
        self.root.wm_attributes("-alpha", 1.0)

    def _load_raw_bust(self, emotion: str, eye_state: str, vowel: str) -> Image.Image | None:
        key = f"{emotion}_{eye_state}_{vowel}"
        if key in self.raw_bust_cache:
            return self.raw_bust_cache[key]
        fpath = os.path.join(CHAR_DIR, f"{key}.png")
        if not os.path.exists(fpath):
            fpath = os.path.join(CHAR_DIR, "normal_open_normal.png")
        if not os.path.exists(fpath):
            return None
        img = Image.open(fpath).convert("RGBA")
        self.raw_bust_cache[key] = img
        return img

    def _load_raw_full(self, emotion: str) -> Image.Image | None:
        if emotion in self.raw_full_cache:
            return self.raw_full_cache[emotion]
        fpath = os.path.join(CHAR_DIR, f"full_{emotion}.png")
        if not os.path.exists(fpath):
            fpath = os.path.join(CHAR_DIR, "full_normal.png")
        if not os.path.exists(fpath):
            return None
        img = Image.open(fpath).convert("RGBA")
        self.raw_full_cache[emotion] = img
        return img

    def _get_composite_sprite(self, emotion: str, eye_state: str, vowel: str) -> ImageTk.PhotoImage | None:
        """
        現在の表示モード（全身/バストアップ）・感情・瞬き・口形に対応する透過済みPhotoImageを生成・キャッシュする。
        全身モード時も顔パーツを合成して瞬き＆口パクに対応する。
        """
        cw, ch = self._get_canvas_dimensions()
        cache_key = f"{self.view_mode}_{emotion}_{eye_state}_{vowel}_{cw}x{ch}"
        if cache_key in self.sprite_cache:
            return self.sprite_cache[cache_key]

        bust_img = self._load_raw_bust(emotion, eye_state, vowel)
        if bust_img is None:
            return None

        if self.view_mode == "full":
            full_base = self._load_raw_full(emotion)
            if full_base is None:
                return None
            work = full_base.copy()
            # 瞬き中または発話中はバストアップの顔領域を全身画像へオーバーレイ
            if eye_state != "open" or vowel != "normal":
                sx, sy, sw, sh = 265, 340, 310, 185
                face_patch = bust_img.crop((sx, sy, sx + sw, sy + sh))
                fw, fh = work.size
                dx = int(round((497.0 + sx * (701.0 / 825.0)) * (fw / 1696.0)))
                dy = int(round((35.0 + (sy + 4.0) * (714.0 / 840.0)) * (fh / 2528.0)))
                dw = int(round((sw * (701.0 / 825.0)) * (fw / 1696.0)))
                dh = int(round((sh * (714.0 / 840.0)) * (fh / 2528.0)))
                face_resized = face_patch.resize((dw, dh), Image.Resampling.LANCZOS)
                work.paste(face_resized, (dx, dy), face_resized)
            src_rgba = work
        else:
            src_rgba = bust_img

        # アスペクト比を維持してキャンバス内にリサイズ
        iw, ih = src_rgba.size
        ratio = min(cw / float(iw), ch / float(ih))
        tw = max(1, int(iw * ratio))
        th = max(1, int(ih * ratio))
        resized = src_rgba.resize((tw, th), Image.Resampling.LANCZOS)

        # 透過キー色とのクリーンな合成（半透明エッジに白いゴミが出ないよう閾値処理）
        r, g, b, a = resized.split()
        # α < 90 は完全透明キー色にし、α >= 90 はダークキー色と馴染ませて完全にジャギーを防ぐ
        bg = Image.new("RGBA", (tw, th), (*KEY_RGB, 255))
        comp = Image.alpha_composite(bg, resized).convert("RGB")
        mask = a.point(lambda p: 255 if p >= 85 else 0)
        final_img = Image.new("RGB", (tw, th), KEY_RGB)
        final_img.paste(comp, (0, 0), mask)

        photo = ImageTk.PhotoImage(final_img)
        self.sprite_cache[cache_key] = photo
        return photo

    def _tick_animation(self) -> None:
        """瞬き・口パク・呼吸揺れ・吹き出し自動消去を更新する（約30fps）。"""
        now = time.time()

        # 吹き出しの自動フェードアウト（キャラクターの座標を動かさずに非表示にする）
        if not self.show_input_bar and now > self.bubble_hide_at and self._bubble_visible:
            self._bubble_visible = False
            self.bubble_frame.place_forget()

        # 自然な自動瞬き
        if not self.is_blinking and now >= self.next_blink_time:
            self.is_blinking = True
            self.blink_end_time = now + 0.13
        elif self.is_blinking and now >= self.blink_end_time:
            self.is_blinking = False
            self.next_blink_time = now + random.uniform(2.8, 5.5)

        eye_state = "blink" if self.is_blinking else "open"
        if now < self.speak_until:
            step = int(now * 8.5)
            vowels = ["a", "e", "o", "u", "i", "a", "normal"]
            vowel = vowels[step % len(vowels)]
        else:
            vowel = "normal"
            # 発話終了から2.5秒経過したら表情を「通常 (normal)」へ自動リセット
            if self.speak_until > 0 and now > self.speak_until + 2.5 and self.current_emotion != "normal":
                self.current_emotion = "normal"

        photo = self._get_composite_sprite(self.current_emotion, eye_state, vowel)
        if photo:
            cw, ch = self._get_canvas_dimensions()
            breath_y = int(round(2.0 * (1.0 if int(now * 1.5) % 2 == 0 else -1.0)))
            self.canvas.delete("all")
            self.canvas.create_image(cw // 2, ch // 2 + breath_y + self.char_offset_y, image=photo)

        self.root.after(45, self._tick_animation)

    def _tick_backend_sync(self) -> None:
        """バックエンドから最新のニアの発話・感情・設定を同期する。"""
        try:
            req = urllib.request.urlopen(f"{API_BASE}/api/status", timeout=1.2)
            data = json.loads(req.read().decode("utf-8"))
            cfg = data.get("config", {})
            self._desired_topmost = bool(cfg.get("mascot_always_on_top", True))
            if not self._menu_open and self._applied_topmost != self._desired_topmost:
                self.root.wm_attributes("-topmost", self._desired_topmost)
                self._applied_topmost = self._desired_topmost

            recent = data.get("recent_last")
            if recent and recent.get("agent_speech"):
                speech = recent["agent_speech"]
                if speech != self.last_speech_seen:
                    self.last_speech_seen = speech
                    self.current_emotion = recent.get("emotion", "normal")
                    dur = max(2.0, min(9.0, len(speech) * 0.12))
                    self.speak_until = time.time() + dur
                    self._show_bubble(speech, duration=dur + 4.0)
            else:
                self.last_speech_seen = ""
        except Exception:
            pass
        self.root.after(1200, self._tick_backend_sync)

    def _on_send_chat(self, _event=None) -> None:
        txt = self.entry.get().strip()
        if not txt:
            return
        self.entry.delete(0, tk.END)
        self._show_bubble("……ちょっと待ちなさい、今考えてるから。", duration=10.0)
        self._post_json("/api/chat", {"content": txt, "user": "Master", "source": "user_text"})

    def _on_capture_vision(self) -> None:
        self._show_bubble("……どれ、今の画面を見せなさいよ。解析してあげるわ。", duration=10.0)
        self._post_json("/api/vision_analyze", {"prompt": "現在見えている画面の内容を確認して簡潔に報告・アドバイスして"})

    def _on_analyze_clipboard(self) -> None:
        self._show_bubble("クリップボードのデータね。確認するわよ。", duration=8.0)
        self._post_json("/api/clipboard_analyze", {})

    @staticmethod
    def _post_json(path: str, payload: dict) -> None:
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{API_BASE}{path}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3.0)
        except Exception:
            pass

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    app = NativeDesktopMascot()
    app.run()
