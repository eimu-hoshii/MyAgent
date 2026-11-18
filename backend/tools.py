"""
自律エージェントが自身の判断で実行できるツール群（Hands レイヤー）。
- Web検索（Google News RSS + DuckDuckGo + Wikipedia）
- Webページ本文深掘り取得（fetch_webpage）
- 画面キャプチャ＆アクティブウィンドウ認識（Vision / Active Window / フルスクリーン検知）
- PCシステム診断
- 長期記憶の記録・検索・重要度管理
- カレンダー予定表・タスク・繰り返しリマインダー管理
"""

import base64
import ctypes
from ctypes import wintypes
import datetime
import email.utils
import html
import io
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
from typing import Any
import httpx
from PIL import ImageGrab
from backend.config import VISION_CAPTURE_DIR, config
from backend.memory import memory_manager
from backend.models import EventSource


# サーバー側で直接実行できないクライアント側アクション（URLをブラウザで開く等）の待ち行列
PENDING_CLIENT_ACTIONS: list[dict[str, Any]] = []
# 直近の画面キャプチャ結果を保持し、ツール実行とLLM画像添付での二重キャプチャを防ぐ
LAST_CAPTURE_CACHE: dict[str, Any] = {}
# 気象庁 area.json のキャッシュ（TTL 24時間）
_JMA_AREA_CACHE: dict[str, Any] = {"fetched_at": 0.0, "data": None}


def is_interactive_default_desktop() -> bool:
    """現在のバックエンドプロセスがユーザーの対話型デスクトップ(Default)上で稼働しているか判定する。"""
    if os.name != "nt":
        return True
    try:
        u32 = ctypes.windll.user32
        hdesk = u32.GetThreadDesktop(ctypes.windll.kernel32.GetCurrentThreadId())
        buf = ctypes.create_unicode_buffer(256)
        u32.GetUserObjectInformationW(hdesk, 2, buf, 512, None)
        return buf.value.strip().lower() == "default"
    except Exception:
        return True


def _find_launch_entry(name: str) -> dict[str, str] | None:
    """許可リストから名前（完全一致 → 部分一致）でエントリを検索する。"""
    key = name.strip().lower()
    if not key:
        return None
    entries = config.local_launch_allowlist or []
    for e in entries:
        if e.get("name", "").strip().lower() == key:
            return e
    for e in entries:
        ename = e.get("name", "").strip().lower()
        if key in ename or ename in key:
            return e
    return None


def launch_local_target(name: str) -> dict[str, Any]:
    """
    許可リストに登録されたアプリ起動・URLオープン・フォルダオープンを実行する。
    許可リスト外の任意コマンドは一切実行しない（安全設計）。
    """
    if not config.local_launch_enabled:
        return {"ok": False, "message": "ローカル操作ツールは設定で無効化されています。"}
    entry = _find_launch_entry(name)
    if not entry:
        names = "、".join(e.get("name", "") for e in (config.local_launch_allowlist or []))
        return {"ok": False, "message": f"「{name}」は許可リストに未登録です。登録済み: {names or 'なし'}"}

    ltype = entry["type"]
    target = os.path.expandvars(entry["target"])
    interactive = is_interactive_default_desktop()

    # URL はブラウザ側で開けるため、非対話デスクトップ時はクライアントへ委譲する
    if ltype == "url":
        if interactive:
            webbrowser.open(target)
            return {"ok": True, "message": f"URL「{entry['name']}」({target}) を開きました。"}
        PENDING_CLIENT_ACTIONS.append({"type": "client_open_url", "url": target, "name": entry["name"]})
        return {"ok": True, "message": f"URL「{entry['name']}」({target}) をブラウザ側で開くよう指示しました。"}

    if not interactive:
        return {
            "ok": False,
            "message": "現在サーバーが隔離セッションで稼働しているため、アプリ/フォルダを画面に表示できません。『NIA_起動・再起動.vbs』から起動すると利用できます。",
        }

    try:
        if ltype == "folder":
            if not os.path.isdir(target):
                return {"ok": False, "message": f"フォルダが見つかりません: {target}"}
            os.startfile(target)  # type: ignore[attr-defined]
            return {"ok": True, "message": f"フォルダ「{entry['name']}」({target}) を開きました。"}
        # app
        subprocess.Popen(
            f'start "" "{target}"',
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        return {"ok": True, "message": f"アプリ「{entry['name']}」({target}) を起動しました。"}
    except Exception as exc:
        return {"ok": False, "message": f"起動エラー ({entry['name']}): {exc}"}


async def check_external_engines() -> dict[str, Any]:
    """Ollama / AivisSpeech / VOICEVOX の稼働状態を確認する。"""

    async def _ping(client: httpx.AsyncClient, url: str) -> bool:
        try:
            resp = await client.get(url)
            return resp.status_code < 500
        except Exception:
            return False

    async with httpx.AsyncClient(timeout=1.5) as client:
        ollama_ok = await _ping(client, f"{config.ollama_base_url}/api/tags")
        aivis_ok = await _ping(client, f"{config.aivisspeech_base_url}/version")
        voicevox_ok = await _ping(client, f"{config.voicevox_base_url}/version")

    provider = config.tts_provider.lower()
    if provider == "aivisspeech":
        tts_ok = aivis_ok
    elif provider == "voicevox":
        tts_ok = voicevox_ok
    else:
        tts_ok = True  # Edge-TTS / ブラウザTTS は常時利用可能扱い

    llm_ok = ollama_ok if config.llm_provider == "ollama" else True
    return {
        "ollama": ollama_ok,
        "aivisspeech": aivis_ok,
        "voicevox": voicevox_ok,
        "llm_provider": config.llm_provider,
        "tts_provider": provider,
        "llm_ok": llm_ok,
        "tts_ok": tts_ok,
        "checked_at": time.strftime("%H:%M:%S"),
    }


def _find_aivisspeech_binaries() -> tuple[str, str]:
    """ローカルにインストールされた AivisSpeech-Engine (run.exe) と GUI (AivisSpeech.exe) のパスを検出する。"""
    local_app = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    engine_candidates = [
        os.path.join(local_app, "Programs", "AivisSpeech", "AivisSpeech-Engine", "run.exe"),
        os.path.join(prog_files, "AivisSpeech", "AivisSpeech-Engine", "run.exe"),
    ]
    gui_candidates = [
        os.path.join(local_app, "Programs", "AivisSpeech", "AivisSpeech.exe"),
        os.path.join(prog_files, "AivisSpeech", "AivisSpeech.exe"),
    ]
    engine_exe = next((p for p in engine_candidates if os.path.isfile(p)), "")
    gui_exe = next((p for p in gui_candidates if os.path.isfile(p)), "")
    return engine_exe, gui_exe


def _hide_aivis_gui_windows() -> int:
    """表に出ている AivisSpeech GUI ウィンドウを裏（非表示/トレイ相当）へ格納する。"""
    if os.name != "nt":
        return 0
    hidden_count = 0
    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd: int, _lparam: int) -> bool:
            nonlocal hidden_count
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = (buf.value or "").strip()
            if "aivisspeech" in title.lower():
                # SW_MINIMIZE (6) のあと SW_HIDE (0) でタスクバー・画面から裏へ隠す
                user32.ShowWindow(hwnd, 6)
                user32.ShowWindow(hwnd, 0)
                hidden_count += 1
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    return hidden_count


def _is_aivis_engine_process_alive() -> bool:
    """AivisSpeech-Engine (run.exe) または AivisSpeech.exe プロセスが既に存在するか確認する。"""
    if os.name != "nt":
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            creationflags=0x08000000,
            timeout=3.0,
        ).decode("cp932", errors="ignore")
        low = out.lower()
        if '"aivisspeech.exe"' in low:
            return True
    except Exception:
        pass
    return False


async def ensure_aivisspeech_background(wait_ready_sec: float = 12.0, hide_gui: bool = True) -> dict[str, Any]:
    """
    AivisSpeech を二重起動防止つきで裏起動（ウィンドウ非表示の Engine 直接起動または GUI 裏格納）する。
    既に稼働中の場合は新規起動せず、必要に応じて表の GUI ウィンドウを裏へ隠してステータスを返す。
    """
    base_url = (config.aivisspeech_base_url or "http://127.0.0.1:10101").rstrip("/")

    async def _is_api_up() -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.2) as client:
                r = await client.get(f"{base_url}/version")
                return r.status_code < 500
        except Exception:
            return False

    if await _is_api_up():
        hidden = _hide_aivis_gui_windows() if hide_gui else 0
        return {
            "ok": True,
            "already_running": True,
            "hidden_windows": hidden,
            "message": "AivisSpeech は既に稼働中です（二重起動をスキップしました）。"
            + (f" 表のウィンドウを裏へ格納しました。" if hidden > 0 else ""),
        }

    engine_exe, gui_exe = _find_aivisspeech_binaries()
    if not engine_exe and not gui_exe:
        return {
            "ok": False,
            "already_running": False,
            "error": "AivisSpeech の実行ファイルが見つかりませんでした（%LOCALAPPDATA%\\Programs\\AivisSpeech）。",
        }

    # プロセス自体が起動途中の場合は二重起動せず待機のみ行う
    proc_alive = _is_aivis_engine_process_alive()
    launched_mode = "existing_process"

    if not proc_alive:
        try:
            parsed = urllib.parse.urlparse(base_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 10101
            if engine_exe:
                # ウィンドウやGUIを一切出さず Engine (run.exe) のみを完全バックグラウンド起動
                subprocess.Popen(
                    [engine_exe, "--use_gpu", "--host", host, "--port", str(port)],
                    cwd=os.path.dirname(engine_exe),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                )
                launched_mode = "headless_engine"
            else:
                # フォールバック: GUI本体を非アクティブ最小化で起動し、直後に裏へ隠す
                si = subprocess.STARTUPINFO() if os.name == "nt" else None
                if si is not None:
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = 7  # SW_SHOWMINNOACTIVE
                subprocess.Popen(
                    [gui_exe],
                    cwd=os.path.dirname(gui_exe),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=si,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                )
                launched_mode = "minimized_gui"
        except Exception as exc:
            return {"ok": False, "already_running": False, "error": f"AivisSpeech 裏起動エラー: {exc}"}

    import asyncio

    steps = max(1, int(wait_ready_sec / 0.4))
    for _ in range(steps):
        await asyncio.sleep(0.4)
        if hide_gui:
            _hide_aivis_gui_windows()
        if await _is_api_up():
            return {
                "ok": True,
                "already_running": False,
                "launched_mode": launched_mode,
                "message": "AivisSpeech をバックグラウンド（裏常駐）で起動しました。",
            }

    return {
        "ok": True,
        "already_running": False,
        "launched_mode": launched_mode,
        "warming_up": True,
        "message": "AivisSpeech の裏起動を開始しました（初期化中です。数秒後に自動で接続されます）。",
    }


class MEMORYSTATUSEX(ctypes.Structure):
    """Windows API GlobalMemoryStatusEx 用の構造体。"""

    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def get_active_window_info() -> dict[str, Any]:
    """
    現在フォアグラウンドにあるアクティブウィンドウのタイトル、矩形座標、およびフルスクリーン状態を取得する。
    """
    info: dict[str, Any] = {
        "title": "Desktop",
        "rect": None,
        "is_fullscreen": False,
        "is_excluded": False,
    }
    if os.name != "nt":
        return info

    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return info

        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip() or "Desktop"
        info["title"] = title

        # プライバシー除外アプリ判定
        for ex in config.vision_exclude_apps:
            if ex and ex.lower() in title.lower():
                info["is_excluded"] = True
                break

        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 50 and h > 50:
                info["rect"] = (rect.left, rect.top, rect.right, rect.bottom)
                screen_w = user32.GetSystemMetrics(0)
                screen_h = user32.GetSystemMetrics(1)
                # フルスクリーン判定（タスクバーを含め画面全体を覆っているか）
                if w >= screen_w and h >= screen_h and title not in ("Program Manager", "Desktop"):
                    info["is_fullscreen"] = True
    except Exception:
        pass
    return info


def get_clipboard_text() -> str:
    """Windowsクリップボード内のテキストを安全に取得する。"""
    if os.name != "nt":
        return ""
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    text = ""
    try:
        if user32.OpenClipboard(0):
            try:
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if handle:
                    kernel32.GlobalLock.restype = ctypes.c_void_p
                    ptr = kernel32.GlobalLock(handle)
                    if ptr:
                        try:
                            text = ctypes.wstring_at(ptr)
                        finally:
                            kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
    except Exception:
        pass
    return (text or "").strip()


def capture_screen_image(target_override: str | None = None) -> dict[str, Any]:
    """
    設定されたキャプチャ対象（active_window | fullscreen | clipboard）から画面画像を取得し、
    縮小JPEGのBase64文字列および保存ファイルURL、ウィンドウ情報を返す。
    """
    os.makedirs(VISION_CAPTURE_DIR, exist_ok=True)
    win_info = get_active_window_info()

    if win_info["is_excluded"]:
        return {
            "ok": False,
            "error": f"アクティブウィンドウ「{win_info['title']}」はプライバシー保護対象（除外アプリ設定）のためキャプチャをブロックしました。",
            "window_title": win_info["title"],
        }

    target = (target_override or config.vision_target or "active_window").lower()
    img = None

    try:
        if target == "clipboard":
            clip_img = ImageGrab.grabclipboard()
            if clip_img is not None and hasattr(clip_img, "convert"):
                img = clip_img.convert("RGB")
            else:
                # クリップボードに画像がない場合は全画面にフォールバック
                img = ImageGrab.grab()
        elif target == "active_window" and win_info.get("rect"):
            l, t, r, b = win_info["rect"]
            l, t = max(0, l), max(0, t)
            if r - l > 80 and b - t > 80:
                img = ImageGrab.grab(bbox=(l, t, r, b))
            else:
                img = ImageGrab.grab()
        else:
            img = ImageGrab.grab()
    except Exception as exc:
        return {"ok": False, "error": f"画面キャプチャ失敗: {exc}", "window_title": win_info["title"]}

    if img is None:
        return {"ok": False, "error": "キャプチャ画像を取得できませんでした。", "window_title": win_info["title"]}

    # マルチモーダルLLM送信＆プレビュー用に最大幅1280pxにリサイズ
    orig_w, orig_h = img.size
    max_dim = 1280
    if max(orig_w, orig_h) > max_dim:
        scale = max_dim / float(max(orig_w, orig_h))
        img = img.resize((int(orig_w * scale), int(orig_h * scale)))

    fname = f"vision_{int(time.time()*1000)}.jpg"
    fpath = os.path.join(VISION_CAPTURE_DIR, fname)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=84)
    jpg_bytes = buf.getvalue()
    with open(fpath, "wb") as f:
        f.write(jpg_bytes)

    # 古いキャプチャのクリーンアップ
    now = time.time()
    try:
        for old_f in os.listdir(VISION_CAPTURE_DIR):
            old_p = os.path.join(VISION_CAPTURE_DIR, old_f)
            if os.path.isfile(old_p) and (now - os.path.getmtime(old_p)) > 600:
                os.remove(old_p)
    except Exception:
        pass

    result = {
        "ok": True,
        "target": target,
        "window_title": win_info["title"],
        "resolution": f"{orig_w}x{orig_h}",
        "file_path": fpath,
        "image_url": f"/api/vision_file/{fname}",
        "base64_jpeg": base64.b64encode(jpg_bytes).decode("ascii"),
        "captured_at": time.time(),
    }
    LAST_CAPTURE_CACHE.clear()
    LAST_CAPTURE_CACHE.update(result)
    return result


def get_system_metrics() -> dict[str, Any]:
    """
    OS標準APIおよびshutilを使用して現在のPCリソース状態（RAM・ディスク・OS情報・現在時刻・アクティブアプリ）を高速取得する。
    """
    mem_percent = 0.0
    mem_used_gb = 0.0
    mem_total_gb = 0.0

    if os.name == "nt":
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        mem_percent = float(stat.dwMemoryLoad)
        mem_total_gb = round(stat.ullTotalPhys / (1024**3), 1)
        mem_used_gb = round((stat.ullTotalPhys - stat.ullAvailPhys) / (1024**3), 1)

    total_disk, used_disk, free_disk = shutil.disk_usage(os.getcwd())
    disk_percent = round((used_disk / total_disk) * 100.0, 1)
    disk_free_gb = round(free_disk / (1024**3), 1)

    win_info = get_active_window_info()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "timestamp": now_str,
        "os": f"{platform.system()} {platform.release()}",
        "cpu_cores": os.cpu_count() or 1,
        "memory_percent": mem_percent,
        "memory_used_gb": mem_used_gb,
        "memory_total_gb": mem_total_gb,
        "disk_percent": disk_percent,
        "disk_free_gb": disk_free_gb,
        "active_window": win_info["title"],
        "is_fullscreen": win_info["is_fullscreen"],
    }


async def _get_jma_area_data(client: httpx.AsyncClient) -> dict[str, Any]:
    """気象庁の全国エリア定義JSON（area.json）をキャッシュ付きで取得する。"""
    now = time.time()
    if _JMA_AREA_CACHE.get("data") and (now - float(_JMA_AREA_CACHE.get("fetched_at", 0.0))) < 86400.0:
        return _JMA_AREA_CACHE["data"]
    resp = await client.get("https://www.jma.go.jp/bosai/common/const/area.json")
    if resp.status_code == 200:
        data = resp.json()
        _JMA_AREA_CACHE["data"] = data
        _JMA_AREA_CACHE["fetched_at"] = now
        return data
    return _JMA_AREA_CACHE.get("data") or {}


def _resolve_jma_location(area_data: dict[str, Any], location: str) -> dict[str, Any] | None:
    """日本の地名（都道府県・地方・市区町村）から気象庁の予報区コードと代表地点を解決する。"""
    q = (location or "").strip()
    for suffix in ("の天気予報", "の天気", "天気", "地方"):
        if q.endswith(suffix) and len(q) > len(suffix):
            q = q[: -len(suffix)].strip()
    if not q:
        q = "東京"

    # 地方名エイリアス
    aliases = {
        "沖縄": "沖縄本島地方",
        "那覇": "那覇市",
        "札幌": "札幌市",
        "北海道": "石狩・空知・後志地方",
        "仙台": "仙台市",
    }
    q_resolved = aliases.get(q, q)

    offices = area_data.get("offices", {})
    c10s = area_data.get("class10s", {})
    c15s = area_data.get("class15s", {})
    c20s = area_data.get("class20s", {})

    # 1. 都道府県・予報区オフィス一致（例: 東京 -> 東京都, 京都 -> 京都府）
    for ocode, oinfo in offices.items():
        oname = oinfo.get("name", "")
        if q_resolved == oname or any(q_resolved + suf == oname for suf in ("都", "府", "県")):
            children = oinfo.get("children", [])
            c10_code = children[0] if children else ocode
            c10_name = c10s.get(c10_code, {}).get("name", oname)
            return {
                "display_name": oname,
                "office_code": ocode,
                "office_name": oname,
                "class10_code": c10_code,
                "class10_name": c10_name,
                "class10_index": 0,
            }

    # 2. class10s 一致（例: 東京地方, 北部, 南部 等）
    for c10_code, c10_info in c10s.items():
        cname = c10_info.get("name", "")
        if q_resolved == cname or q_resolved + "地方" == cname:
            ocode = c10_info.get("parent", "")
            oname = offices.get(ocode, {}).get("name", "")
            children = offices.get(ocode, {}).get("children", [])
            c10_idx = children.index(c10_code) if c10_code in children else 0
            return {
                "display_name": f"{oname} {cname}".strip(),
                "office_code": ocode,
                "office_name": oname,
                "class10_code": c10_code,
                "class10_name": cname,
                "class10_index": c10_idx,
            }

    # 3. class20s 市区町村一致（政令指定都市の「〇〇市〇〇区」前方一致を町・村より優先）
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for c20_code, c20_info in c20s.items():
        mname = c20_info.get("name", "")
        score = 0
        if q_resolved == mname:
            score = 100
        elif q_resolved + "市" == mname:
            score = 96
        elif mname.startswith(q_resolved + "市"):
            score = 92
        elif q_resolved + "区" == mname:
            score = 88
        elif q_resolved + "町" == mname or q_resolved + "村" == mname:
            score = 82
        elif q_resolved in mname:
            score = max(10, 60 - len(mname))
        if score > 0:
            candidates.append((score, c20_code, c20_info))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, _, c20_info = candidates[0]
        c15_code = c20_info.get("parent", "")
        c10_code = c15s.get(c15_code, {}).get("parent", "")
        ocode = c10s.get(c10_code, {}).get("parent", "")
        oname = offices.get(ocode, {}).get("name", "")
        cname = c10s.get(c10_code, {}).get("name", "")
        children = offices.get(ocode, {}).get("children", [])
        c10_idx = children.index(c10_code) if c10_code in children else 0
        mname = c20_info.get("name", q_resolved)
        # 政令市区名（例: 横浜市鶴見区）を「〇〇市」に丸める
        if mname.startswith(q_resolved + "市") and q_resolved + "市" != mname:
            mname = q_resolved + "市"
        return {
            "display_name": f"{oname}{mname}".strip(),
            "office_code": ocode,
            "office_name": oname,
            "class10_code": c10_code,
            "class10_name": cname,
            "class10_index": c10_idx,
        }

    return None


def _pick_area_entry(areas: list[dict[str, Any]], target_code: str, fallback_idx: int = 0) -> dict[str, Any]:
    """areas配列から指定エリアコード、またはインデックス一致で該当エリア辞書を返す。"""
    for item in areas:
        if item.get("area", {}).get("code") == target_code:
            return item
    if 0 <= fallback_idx < len(areas):
        return areas[fallback_idx]
    return areas[0] if areas else {}


async def tool_get_weather(location: str) -> str:
    """
    気象庁公式防災情報JSONから指定地域の今日・明日・明後日および週間天気・最高/最低気温・降水確率(%)を取得する。
    日本国内の地名に該当しない場合は wttr.in JSON API へフォールバックする。
    """
    loc = (location or "").strip() or "東京"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(
            timeout=float(config.fetch_timeout_sec), follow_redirects=True, headers=headers
        ) as client:
            area_data = await _get_jma_area_data(client)
            resolved = _resolve_jma_location(area_data, loc) if area_data else None

            if resolved and resolved.get("office_code"):
                ocode = resolved["office_code"]
                c10_code = resolved["class10_code"]
                c10_idx = resolved["class10_index"]
                fc_resp = await client.get(f"https://www.jma.go.jp/bosai/forecast/data/forecast/{ocode}.json")
                if fc_resp.status_code == 200:
                    fc = fc_resp.json()
                    days: dict[str, dict[str, Any]] = {}

                    def ensure_day(d_str: str) -> dict[str, Any]:
                        if d_str not in days:
                            days[d_str] = {
                                "weather": "",
                                "wind": "",
                                "pops": [],
                                "weekly_pop": "",
                                "temp_min": "",
                                "temp_max": "",
                            }
                        return days[d_str]

                    # 1. 短期予報 (block 0: 今日・明日・明後日)
                    temp_station_name = ""
                    if len(fc) >= 1:
                        ts_list = fc[0].get("timeSeries", [])
                        if len(ts_list) >= 1:
                            t_defs = ts_list[0].get("timeDefines", [])
                            a_entry = _pick_area_entry(ts_list[0].get("areas", []), c10_code, c10_idx)
                            weathers = a_entry.get("weathers", [])
                            winds = a_entry.get("winds", [])
                            for i, td in enumerate(t_defs):
                                d = ensure_day(td[:10])
                                if i < len(weathers) and weathers[i]:
                                    d["weather"] = re.sub(r"\s+", " ", weathers[i].replace("\u3000", " ")).strip()
                                if i < len(winds) and winds[i]:
                                    d["wind"] = re.sub(r"\s+", " ", winds[i].replace("\u3000", " ")).strip()

                        if len(ts_list) >= 2:
                            t_defs = ts_list[1].get("timeDefines", [])
                            a_entry = _pick_area_entry(ts_list[1].get("areas", []), c10_code, c10_idx)
                            pops = a_entry.get("pops", [])
                            for i, td in enumerate(t_defs):
                                if i < len(pops) and str(pops[i]).strip() != "":
                                    d = ensure_day(td[:10])
                                    hh = td[11:13]
                                    d["pops"].append((hh, str(pops[i]).strip()))

                        if len(ts_list) >= 3:
                            t_defs = ts_list[2].get("timeDefines", [])
                            a_entry = _pick_area_entry(ts_list[2].get("areas", []), c10_code, c10_idx)
                            temp_station_name = a_entry.get("area", {}).get("name", "")
                            temps = a_entry.get("temps", [])
                            per_day_temps: dict[str, list[tuple[str, str]]] = {}
                            for i, td in enumerate(t_defs):
                                if i < len(temps) and str(temps[i]).strip() != "":
                                    per_day_temps.setdefault(td[:10], []).append((td[11:13], str(temps[i]).strip()))
                            for d_str, pairs in per_day_temps.items():
                                d = ensure_day(d_str)
                                if len(pairs) >= 2:
                                    vals = sorted(int(float(v)) for _, v in pairs)
                                    d["temp_min"] = str(vals[0])
                                    d["temp_max"] = str(vals[-1])
                                elif len(pairs) == 1:
                                    hh, val = pairs[0]
                                    if hh in ("00", "03", "06"):
                                        d["temp_min"] = val
                                    else:
                                        d["temp_max"] = val

                    # 2. 週間予報 (block 1: 明後日以降の補完)
                    if len(fc) >= 2:
                        ts_list = fc[1].get("timeSeries", [])
                        if len(ts_list) >= 1:
                            t_defs = ts_list[0].get("timeDefines", [])
                            a_entry = _pick_area_entry(ts_list[0].get("areas", []), c10_code, c10_idx)
                            pops = a_entry.get("pops", [])
                            for i, td in enumerate(t_defs):
                                if i < len(pops) and str(pops[i]).strip() != "":
                                    ensure_day(td[:10])["weekly_pop"] = str(pops[i]).strip()
                        if len(ts_list) >= 2:
                            t_defs = ts_list[1].get("timeDefines", [])
                            a_entry = _pick_area_entry(ts_list[1].get("areas", []), c10_code, c10_idx)
                            if not temp_station_name:
                                temp_station_name = a_entry.get("area", {}).get("name", "")
                            t_mins = a_entry.get("tempsMin", [])
                            t_maxs = a_entry.get("tempsMax", [])
                            for i, td in enumerate(t_defs):
                                d = ensure_day(td[:10])
                                if not d["temp_min"] and i < len(t_mins) and str(t_mins[i]).strip() != "":
                                    d["temp_min"] = str(t_mins[i]).strip()
                                if not d["temp_max"] and i < len(t_maxs) and str(t_maxs[i]).strip() != "":
                                    d["temp_max"] = str(t_maxs[i]).strip()

                    today_dt = datetime.date.today()
                    label_map = {
                        today_dt.strftime("%Y-%m-%d"): "今日",
                        (today_dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d"): "明日",
                        (today_dt + datetime.timedelta(days=2)).strftime("%Y-%m-%d"): "明後日",
                    }
                    station_note = f" / 気温観測基準: {temp_station_name}" if temp_station_name else ""
                    lines = [
                        f"【気象庁 公式天気予報: {resolved['display_name']} (予報区: {resolved['class10_name']}{station_note})】"
                    ]
                    for d_str in sorted(days.keys())[:5]:
                        info = days[d_str]
                        day_tag = label_map.get(d_str, "")
                        head = f"- {d_str}" + (f" ({day_tag})" if day_tag else "")
                        parts: list[str] = []
                        if info["weather"]:
                            parts.append(f"天気: {info['weather']}")
                        if info["temp_min"] or info["temp_max"]:
                            t_parts = []
                            if info["temp_min"]:
                                t_parts.append(f"最低 {info['temp_min']}℃")
                            if info["temp_max"]:
                                t_parts.append(f"最高 {info['temp_max']}℃")
                            parts.append(" / ".join(t_parts))
                        if info["pops"]:
                            max_pop = max(int(p) for _, p in info["pops"] if p.isdigit())
                            detail = ", ".join(f"{hh}時〜:{p}%" for hh, p in info["pops"])
                            parts.append(f"降水確率: {detail} (期間最大 {max_pop}%)")
                        elif info["weekly_pop"]:
                            parts.append(f"降水確率: {info['weekly_pop']}%")
                        if info["wind"]:
                            parts.append(f"風: {info['wind']}")
                        if parts:
                            lines.append(f"{head}: " + " | ".join(parts))

                    if len(lines) > 1:
                        return "\n".join(lines)

            # 海外都市または気象庁未一致時のフォールバック (wttr.in)
            wttr_resp = await client.get(f"https://wttr.in/{urllib.parse.quote(loc)}", params={"format": "j1", "lang": "ja"})
            if wttr_resp.status_code == 200:
                wdata = wttr_resp.json()
                lines = [f"【天気予報 (wttr.in): {loc}】"]
                for wday in wdata.get("weather", [])[:3]:
                    d_str = wday.get("date", "")
                    tmin = wday.get("mintempC", "")
                    tmax = wday.get("maxtempC", "")
                    hourly = wday.get("hourly", [])
                    descs = []
                    pops = []
                    for h in hourly:
                        lang_ja = h.get("lang_ja") or h.get("weatherDesc") or []
                        if lang_ja and isinstance(lang_ja, list):
                            val = lang_ja[0].get("value", "")
                            if val and val not in descs:
                                descs.append(val)
                        if h.get("chanceofrain") is not None:
                            pops.append(int(h["chanceofrain"]))
                    max_p = max(pops) if pops else 0
                    lines.append(
                        f"- {d_str}: 天気: {' / '.join(descs[:2]) or '不明'} | 最低 {tmin}℃ / 最高 {tmax}℃ | 最大降水確率 {max_p}%"
                    )
                if len(lines) > 1:
                    return "\n".join(lines)
    except Exception as exc:
        return f"天気予報の取得中にエラーが発生しました ({loc}): {exc}"

    return f"「{loc}」の天気予報データを取得できませんでした。"


def _is_generic_news_query(query: str) -> tuple[bool, bool]:
    """
    クエリが特定の固有名詞を含まない「最新ニュース・話題」の汎用問い合わせか判定する。
    戻り値: (is_generic_news, prefers_tech)
    """
    q_lower = query.lower()
    has_news_word = any(
        w in q_lower for w in ("ニュース", "トピック", "速報", "トレンド", "最新情報", "news", "topic", "topics")
    )
    prefers_tech = any(
        w in q_lower
        for w in ("テック", "テクノロジー", "tech", "it", "ai", "人工知能", "生成ai", "ソフトウェア", "ガジェット")
    )
    if not has_news_word:
        return False, prefers_tech

    # 日付表現や汎用的なニュース修飾語・助詞を取り除き、特定キーワードが残るか確認する
    stripped = re.sub(r"20\d{2}年?|\d{1,2}月|\d{1,2}日", " ", q_lower)
    generic_tokens = (
        "web検索を使って",
        "web検索で",
        "web検索",
        "最新ニュース",
        "テックニュース",
        "注目トピック",
        "トピックス",
        "ニュース",
        "トピック",
        "テクノロジー",
        "テック",
        "最新情報",
        "今日",
        "昨日",
        "本日",
        "今週",
        "最近",
        "現在",
        "最新",
        "注目",
        "話題",
        "速報",
        "主要",
        "国内",
        "海外",
        "日本",
        "世界",
        "トレンド",
        "動向",
        "まとめて",
        "まとめ",
        "教えて",
        "調べて",
        "要約して",
        "要約",
        "news",
        "latest",
        "today",
        "yesterday",
        "tech",
        "technology",
        "topics",
        "topic",
        "it",
        "ai",
    )
    for tok in generic_tokens:
        stripped = stripped.replace(tok, " ")
    stripped = re.sub(r"[・、。,\.\!\?！？「」『』\(\)（）\[\]\sをのにがはへとやもからまで]+", "", stripped)
    return len(stripped) <= 2, prefers_tech


def _format_rss_pubdate(pub_raw: str) -> tuple[str, float]:
    """RSSのpubDateをJST文字列とUnixタイムスタンプに変換する。"""
    if not pub_raw:
        return "日時不明", 0.0
    try:
        dt = email.utils.parsedate_to_datetime(pub_raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        jst = datetime.timezone(datetime.timedelta(hours=9))
        dt_jst = dt.astimezone(jst)
        return dt_jst.strftime("%Y-%m-%d %H:%M"), dt_jst.timestamp()
    except Exception:
        return pub_raw[:16], 0.0


async def _fetch_rss_news_items(
    client: httpx.AsyncClient, url: str, limit: int = 5
) -> list[tuple[float, str, str, str]]:
    """RSSフィードを取得し、(timestamp, date_label, title, link) のリストを返す。"""
    items: list[tuple[float, str, str, str]] = []
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return items
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item")[:limit]:
            title = html.unescape((item.findtext("title") or "").strip())
            pub_raw = (item.findtext("pubDate") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title:
                continue
            date_label, ts = _format_rss_pubdate(pub_raw)
            items.append((ts, date_label, title, link))
    except Exception:
        pass
    return items


async def tool_web_search(query: str) -> str:
    """
    Yahoo! Japan 検索（一般Webページ・URL・スニペット）、Yahoo!ニュース / Google News RSS
    （トピック別・キーワード検索）、Bing Search RSS、Wikipedia API を併用して高精度にWeb情報を検索する。
    """
    if not query.strip():
        return "検索クエリが空です。"

    results: list[str] = []
    if any(w in query for w in ("天気", "気温", "降水確率", "予報")):
        results.append("[システム助言] 天気・気温・降水確率の正確な数値は `get_weather` ツールで取得してください。")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    is_generic_news, prefers_tech = _is_generic_news_query(query)

    async with httpx.AsyncClient(
        timeout=float(config.fetch_timeout_sec), follow_redirects=True, headers=headers
    ) as client:
        # 0. 「最新ニュース・注目トピック」など汎用ニュースクエリは公式トピックRSSから取得（古いWeb記事や誤爆を防止）
        if is_generic_news:
            if prefers_tech:
                feed_urls = [
                    "https://news.yahoo.co.jp/rss/topics/it.xml",
                    "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=ja&gl=JP&ceid=JP:ja",
                ]
            else:
                feed_urls = [
                    "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
                    "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja",
                    "https://news.yahoo.co.jp/rss/topics/it.xml",
                ]

            collected: list[tuple[float, str, str, str]] = []
            seen_titles: set[str] = set()

            async def _append_feed(feed_url: str, limit: int) -> None:
                for ts, date_label, title, link in await _fetch_rss_news_items(client, feed_url, limit=limit):
                    clean_title = re.sub(r"\s+", " ", title).strip()
                    # Google News 末尾の「 - メディア名」を除いて同一記事の重複を防止する
                    base_title = re.sub(r"\s+-\s+[^-]+$", "", clean_title)
                    norm_title = re.sub(r"\s+", "", base_title)
                    if not clean_title or norm_title in seen_titles:
                        continue
                    seen_titles.add(norm_title)
                    collected.append((ts, date_label, clean_title, link))

            for f_url in feed_urls:
                await _append_feed(f_url, limit=5)

            if prefers_tech and len(collected) < 4:
                await _append_feed("https://news.yahoo.co.jp/rss/topics/top-picks.xml", limit=4)

            collected.sort(key=lambda x: x[0], reverse=True)
            for _ts, date_label, title, link in collected[:8]:
                url_part = f" ({link})" if link else ""
                results.append(f"[最新ニュース ({date_label})] {title}{url_part}")

            if results:
                return "\n".join(results)

        # 1. Yahoo! Japan Web検索（一般Web・公式ページ・スニペット・URL付き）
        web_count = 0
        seen_urls: set[str] = set()
        try:
            yahoo_url = "https://search.yahoo.co.jp/search"
            resp = await client.get(yahoo_url, params={"p": query})
            if resp.status_code == 200:
                cards = re.findall(
                    r'<div class="sw-CardBase[^"]*".*?(?=<div class="sw-CardBase|$)',
                    resp.text,
                    flags=re.DOTALL,
                )
                for card in cards:
                    if web_count >= 5:
                        break
                    # パンくず・補助リンク誤取得を防ぐため、<h3>見出しに紐づくリンクを優先抽出する
                    m_hl = re.search(
                        r'<a[^>]+href="(https?://[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>',
                        card,
                        flags=re.DOTALL,
                    )
                    if not m_hl:
                        m_hl = re.search(
                            r'<h3[^>]*>.*?<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>.*?</h3>',
                            card,
                            flags=re.DOTALL,
                        )
                    if m_hl:
                        url = m_hl.group(1)
                        raw_title = m_hl.group(2)
                    else:
                        m_a = re.search(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', card, flags=re.DOTALL)
                        if not m_a:
                            continue
                        url = m_a.group(1)
                        m_h3 = re.search(r"<h3[^>]*>(.*?)</h3>", card, flags=re.DOTALL)
                        m_t = m_h3 or re.search(r'class="[^"]*sw-Card__title[^"]*"[^>]*>(.*?)</', card, flags=re.DOTALL)
                        raw_title = m_t.group(1) if m_t else m_a.group(2)

                    if (
                        "search.yahoo.co.jp/" in url
                        or "rdsig.yahoo.co.jp" in url
                        or "rd.listing.yahoo.co.jp" in url
                        or "listing.yahoo" in url
                        or url in seen_urls
                    ):
                        continue

                    clean_t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", raw_title))).strip()
                    m_s = re.search(r'class="[^"]*sw-Card__summary[^"]*"[^>]*>(.*?)</div>', card, flags=re.DOTALL)
                    if not m_s:
                        m_s = re.search(r"<p[^>]*>(.*?)</p>", card, flags=re.DOTALL)
                    clean_s = (
                        re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m_s.group(1)))).strip()
                        if m_s
                        else ""
                    )
                    if clean_t and url:
                        seen_urls.add(url)
                        snippet_part = f" {clean_s[:160]}" if clean_s else ""
                        results.append(f"[Web: {clean_t}] ({url}){snippet_part}")
                        web_count += 1
        except Exception:
            pass

        # 2. Bing Search RSS（Yahoo!検索の補完・フォールバック）
        if web_count < 3:
            try:
                bing_url = "https://www.bing.com/search"
                resp = await client.get(bing_url, params={"q": query, "format": "rss"})
                if resp.status_code == 200 and "<item>" in resp.text:
                    root = ET.fromstring(resp.content)
                    for item in root.findall(".//item")[:4]:
                        title = re.sub(r"\s+", " ", html.unescape((item.findtext("title") or "").strip())).strip()
                        link = (item.findtext("link") or "").strip()
                        desc = re.sub(r"\s+", " ", html.unescape((item.findtext("description") or "").strip())).strip()
                        if title and link and link not in seen_urls:
                            seen_urls.add(link)
                            results.append(f"[Web: {title}] ({link}) {desc[:160]}")
            except Exception:
                pass

        # 3. Google News RSS 日本語版（特定キーワードの最新ニュース・時事速報）
        news_url = (
            f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}"
            "&hl=ja&gl=JP&ceid=JP:ja"
        )
        for _ts, date_label, title, link in await _fetch_rss_news_items(client, news_url, limit=4):
            clean_news_title = re.sub(r"\s+", " ", title).strip()
            url_part = f" ({link})" if link else ""
            results.append(f"[最新ニュース ({date_label})] {clean_news_title}{url_part}")

        # 4. Wikipedia 日本語版 API での基礎知識検索
        try:
            wiki_url = "https://ja.wikipedia.org/w/api.php"
            params = {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "utf8": 1,
                "srlimit": 2,
            }
            resp = await client.get(wiki_url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("query", {}).get("search", []):
                    title = re.sub(r"\s+", " ", item.get("title", "")).strip()
                    snippet = re.sub(r"<[^>]+>", "", item.get("snippet", ""))
                    snippet = re.sub(r"\s+", " ", html.unescape(snippet)).strip()
                    results.append(f"[Wikipedia: {title}] {snippet}")
        except Exception:
            pass

    if not results:
        return f"「{query}」に関する外部検索結果が見つかりませんでした。情報がない場合は正直にその旨を伝えてください。"
    return "\n".join(results[:12])


async def tool_fetch_webpage(url: str) -> str:
    """
    指定されたURLのWebページ本文を取得し、不要なスクリプトやHTMLタグを除去してテキスト抽出する。
    """
    target_url = url.strip()
    if not target_url.startswith(("http://", "https://")):
        return f"無効なURLです: {url}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(
            timeout=float(config.fetch_timeout_sec), follow_redirects=True, headers=headers
        ) as client:
            resp = await client.get(target_url)
            if resp.status_code != 200:
                return f"ページ取得失敗 (HTTP {resp.status_code}): {target_url}"
            raw_html = resp.text

            # タイトル抽出
            title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.DOTALL | re.IGNORECASE)
            page_title = html.unescape(title_match.group(1)).strip() if title_match else target_url

            # 不要なタグブロック（script, style, nav, footer, svg, noscript）を除去
            cleaned = re.sub(
                r"<(script|style|noscript|svg|nav|footer|header|aside)[^>]*>.*?</\1>",
                " ",
                raw_html,
                flags=re.DOTALL | re.IGNORECASE,
            )
            cleaned = re.sub(r"<[^>]+>", " ", cleaned)
            cleaned = html.unescape(cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()

            max_len = int(config.fetch_max_chars)
            excerpt = cleaned[:max_len] + ("..." if len(cleaned) > max_len else "")
            return f"【Webページ本文抽出: {page_title}】\nURL: {target_url}\n本文:\n{excerpt}"
    except Exception as exc:
        return f"Webページ取得エラー ({target_url}): {exc}"


# 非同期バックグラウンド深掘り調査を起動するコールバック（オーケストレーターが登録）
DEEP_RESEARCH_TRIGGER: Any = None


def set_deep_research_trigger(cb: Any) -> None:
    """深掘り調査の非同期起動コールバックを登録する。"""
    global DEEP_RESEARCH_TRIGGER
    DEEP_RESEARCH_TRIGGER = cb


_AGENT_ONLY_TOOLS = {
    "save_memory",
    "search_memory",
    "manage_task",
    "local_launch",
    "file_op",
    "rag_search",
    "run_command",
    "browser_action",
    "deep_research",
    "mcp_tool",
    "external_service",
    "windows_control",
}


async def execute_agent_tool(
    tool_name: str, arguments: dict[str, Any], event_source: EventSource | None = None
) -> str:
    """
    LLMから要求された自律ツールを実行し、観測結果文字列を返す。
    """
    name = tool_name.strip().lower()

    if name in _AGENT_ONLY_TOOLS and config.mode == "vtuber":
        return f"ツール「{tool_name}」はVTuberモードでは安全のため利用できません。"

    if name == "get_weather":
        loc = str(arguments.get("location", "") or arguments.get("query", "") or "東京")
        return await tool_get_weather(loc)

    if name == "web_search":
        query = str(arguments.get("query", ""))
        return await tool_web_search(query)

    if name == "fetch_webpage":
        url = str(arguments.get("url", ""))
        return await tool_fetch_webpage(url)

    if name == "vision_capture":
        target = str(arguments.get("target", config.vision_target))
        cap = capture_screen_image(target_override=target)
        if not cap.get("ok"):
            return f"【画面認識エラー】{cap.get('error')}"
        clip_txt = get_clipboard_text()
        clip_info = f"\n- 現在のクリップボード文字列: {clip_txt[:200]}" if clip_txt else ""
        return (
            f"【画面キャプチャ＆ウィンドウ認識結果】\n"
            f"- キャプチャ対象: {cap['target']}\n"
            f"- アクティブウィンドウ名: 「{cap['window_title']}」\n"
            f"- 画面解像度: {cap['resolution']}{clip_info}\n"
            f"※画像データもマルチモーダル入力として添付されました。"
        )

    if name == "system_diagnostics":
        m = get_system_metrics()
        return (
            f"【システム診断結果 ({m['timestamp']})】\n"
            f"- OS: {m['os']} (論理コア数: {m['cpu_cores']})\n"
            f"- メモリ使用率: {m['memory_percent']}% ({m['memory_used_gb']}GB / {m['memory_total_gb']}GB)\n"
            f"- ディスク使用率: {m['disk_percent']}% (空き容量: {m['disk_free_gb']}GB)\n"
            f"- 現在のアクティブウィンドウ: 「{m['active_window']}」 (フルスクリーン: {m['is_fullscreen']})"
        )

    if name == "save_memory":
        category = str(arguments.get("category", "memo"))
        content = str(arguments.get("content", ""))
        importance = int(arguments.get("importance", 4) or 4)
        if not content:
            return "保存する内容が空です。"
        mem_id = memory_manager.save_memory(category=category, content=content, importance=importance)
        return f"長期記憶DB（ID: {mem_id}, カテゴリ: {category}, 重要度: ★{importance}）に保存完了しました: {content}"

    if name == "search_memory":
        keyword = str(arguments.get("keyword", ""))
        items = memory_manager.search_memories(keyword=keyword, limit=8)
        if not items:
            return f"キーワード「{keyword}」に一致する長期記憶はありません。"
        lines = [f"- [ID:{it['id']} / {it['category']} / ★{it['importance']}] {it['content']}" for it in items]
        return "【長期記憶検索結果】\n" + "\n".join(lines)

    if name == "manage_task":
        action = str(arguments.get("action", "add"))
        title = str(arguments.get("title", ""))
        due_minutes = float(arguments.get("due_minutes", 0) or 0)
        due_iso = str(arguments.get("due_iso", "") or "")
        repeat_rule = str(arguments.get("repeat_rule", "none") or "none")
        if action == "add" and title:
            tid = memory_manager.add_task(
                title=title, due_minutes=due_minutes, due_iso=due_iso, repeat_rule=repeat_rule
            )
            due_msg = f"（{due_iso} に通知）" if due_iso else (f"（{due_minutes}分後に通知）" if due_minutes > 0 else "")
            rep_msg = f" [繰り返し: {repeat_rule}]" if repeat_rule != "none" else ""
            return f"予定・タスク登録完了 (ID: {tid}): 「{title}」{due_msg}{rep_msg}"
        tasks = memory_manager.get_tasks(status="pending")
        if not tasks:
            return "現在未完了の予定・タスクはありません。"
        return "【未完了タスク・予定一覧】\n" + "\n".join(
            [f"- ID {t['id']}: {t['title']} ({t.get('due_str') or '期限なし'})" for t in tasks]
        )

    if name == "local_launch":
        target_name = str(arguments.get("name", "") or arguments.get("target", ""))
        res = launch_local_target(target_name)
        return f"【ローカル操作{'成功' if res['ok'] else '失敗'}】{res['message']}"

    if name == "file_op":
        from backend.rag_engine import WorkspaceFileManager

        action = str(arguments.get("action", "list_dir")).strip().lower()
        path_arg = str(arguments.get("path", "") or "")
        if action == "read_file":
            offset = int(arguments.get("offset", 0) or 0)
            return WorkspaceFileManager.read_file(path_arg, offset=offset)
        if action == "write_file":
            content = str(arguments.get("content", ""))
            append = bool(arguments.get("append", False))
            return WorkspaceFileManager.write_file(path_arg, content=content, append=append)
        if action == "search_files":
            kw = str(arguments.get("keyword", "") or arguments.get("query", ""))
            return WorkspaceFileManager.search_files(kw, dir_path=path_arg)
        return WorkspaceFileManager.list_dir(path_arg)

    if name == "rag_search":
        from backend.rag_engine import rag_engine

        if not config.rag_enabled:
            return "ローカル資料RAG機能は設定で無効化されています。"
        query = str(arguments.get("query", "") or arguments.get("keyword", ""))
        if arguments.get("reindex"):
            rag_engine.index_directories()
        hits = rag_engine.search(query, top_k=5)
        if not hits:
            rag_engine.index_directories()
            hits = rag_engine.search(query, top_k=5)
        if not hits:
            return f"ローカル資料RAGから「{query}」に関連するドキュメントが見つかりませんでした。"
        lines = [
            f"- [{h['file_name']} #chunk{h['chunk_idx']}] ({h['file_path']})\n  {h['raw_text'][:280].replace(chr(10), ' ')}"
            for h in hits
        ]
        return "【ローカル資料 RAG 検索結果】\n" + "\n".join(lines)

    if name == "run_command":
        from backend.automation import approval_manager

        cmd = str(arguments.get("command", ""))
        reason = str(arguments.get("reason", ""))
        cwd = str(arguments.get("cwd", ""))
        return await approval_manager.execute_command(command=cmd, reason=reason, cwd=cwd)

    if name == "browser_action":
        from backend.automation import browser_manager

        action = str(arguments.get("action", "read_page"))
        url = str(arguments.get("url", ""))
        selector = str(arguments.get("selector", ""))
        text = str(arguments.get("text", ""))
        res = await browser_manager.execute(action=action, url=url, selector=selector, text=text)
        if not res.get("ok"):
            return f"【ブラウザ操作失敗】{res.get('message')}"
        if res.get("image_base64"):
            LAST_CAPTURE_CACHE.clear()
            LAST_CAPTURE_CACHE.update(
                {
                    "ok": True,
                    "base64_jpeg": res["image_base64"],
                    "file_path": res.get("image_path", ""),
                    "captured_at": time.time(),
                }
            )
        links_txt = "\n".join(f"- [{l['text'][:40]}]({l['href']})" for l in (res.get("links") or [])[:5])
        return (
            f"【Playwright ブラウザ取得: {res.get('title', url)}】\n"
            f"URL: {res.get('url')}\n"
            f"本文抜粋:\n{res.get('text', '')[:3200]}"
            + (f"\n\n主要リンク:\n{links_txt}" if links_txt else "")
        )

    if name == "deep_research":
        topic = str(arguments.get("topic", "") or arguments.get("query", "")).strip()
        if not topic:
            return "調査テーマ（topic）を指定してください。"
        if DEEP_RESEARCH_TRIGGER is not None:
            info = await DEEP_RESEARCH_TRIGGER(topic)
            return f"【バックグラウンド深掘り調査開始】テーマ「{topic}」の非同期並列調査（ID: {info.get('job_id')}）を開始しました。完了次第、音声とDiscordで報告します。"
        return "バックグラウンド深掘り調査ランナーが未初期化です。"

    if name == "mcp_tool":
        from backend.automation import mcp_manager

        srv = str(arguments.get("server", ""))
        action = str(arguments.get("action", "call_tool"))
        tname = str(arguments.get("tool_name", ""))
        targs = arguments.get("arguments") if isinstance(arguments.get("arguments"), dict) else {}
        return await mcp_manager.invoke(server_name=srv, action=action, tool_name=tname, arguments=targs)

    if name == "external_service":
        from backend.automation import external_service_manager

        service = str(arguments.get("service", "github")).strip().lower()
        action = str(arguments.get("action", "issues")).strip().lower()
        if service == "github":
            repo = str(arguments.get("repo", ""))
            return await external_service_manager.query_github(action=action, repo=repo)
        if service == "calendar":
            days = int(arguments.get("days", 7) or 7)
            return await external_service_manager.query_calendar(days=days)
        if service == "gmail":
            return await external_service_manager.query_gmail(max_results=5)
        return f"未対応の外部サービスです: {service}（対応: github | calendar | gmail）"

    if name == "windows_control":
        from backend.automation import windows_control_manager

        action = str(arguments.get("action", "")).strip().lower()
        if action in ("lock_pc", "lock", "sleep_pc", "sleep", "suspend"):
            # 自律ループ・クリップボード監視・他者コメ等での誤爆を防ぐため、直接のマスター依頼のみ即時実行
            if event_source is not None and event_source not in (
                EventSource.USER_TEXT,
                EventSource.USER_VOICE,
                EventSource.DISCORD,
            ):
                return "【安全ガード】PCロック・スリープはマスターの直接指示（チャット・音声・Discord）でのみ実行可能です。"
            return windows_control_manager.power_action(action=action)
        if action == "notifications":
            notes = windows_control_manager.read_notifications(limit=6)
            if not notes:
                return "最近の Windows トースト通知はありません（または取得対象外）。"
            return "【Windows 最新通知一覧】\n" + "\n".join(
                f"- [{n['app_id']}] {n['title']}: {n['body']}" for n in notes
            )
        steps = int(arguments.get("steps", 1) or 1)
        return windows_control_manager.media_action(action=action, steps=steps)

    return f"未知のツール名です: {tool_name}"


def build_tools_spec() -> str:
    """現在のモード・設定（ローカル操作の許可リスト・新機能群）を反映したツール仕様文字列を生成する。"""
    if config.mode == "vtuber":
        return VTUBER_TOOLS_SPEC
    spec = AVAILABLE_TOOLS_SPEC
    if config.local_launch_enabled and config.local_launch_allowlist:
        names = ", ".join(f"「{e['name']}」" for e in config.local_launch_allowlist)
        spec += (
            f'9. "local_launch": {{"name": "登録名"}} -> マスターの指示でPC上のアプリ起動・URLオープン・フォルダオープンを行う。'
            f"許可リストに登録された名前のみ実行可能: {names}\n"
        )
    if config.file_tools_enabled:
        spec += '10. "file_op": {"action": "list_dir | read_file | write_file | search_files", "path": "ファイル/フォルダパス", "content": "書き込み文字列", "keyword": "検索語"} -> 許可ディレクトリ内の資料(テキスト/コード/PDF/Word/Excel/PPTX)を読み書き・検索する。\n'
    if config.rag_enabled:
        spec += '11. "rag_search": {"query": "検索キーワード"} -> ローカル資料フォルダ(RAGインデックス)から関連ドキュメントを検索する。\n'
    if config.command_execution_enabled:
        spec += '12. "run_command": {"command": "実行コマンド", "reason": "理由"} -> 承認ゲート経由でシェル/スクリプトコマンドを実行する。\n'
    if config.playwright_enabled:
        spec += '13. "browser_action": {"action": "read_page | screenshot | click | fill", "url": "https://...", "selector": "CSSセレクタ", "text": "入力文字"} -> ヘッドレスブラウザでSPA動的Webサイトを閲覧・操作する。\n'
    spec += '14. "deep_research": {"topic": "調査テーマ"} -> 会話を止めずにバックグラウンドで複数Web記事を並列調査してレポート保存する。\n'
    spec += '15. "external_service": {"service": "github | calendar | gmail", "action": "issues | prs | commits | notifications | upcoming", "repo": "owner/repo"} -> GitHub/カレンダー/メール情報を取得する。\n'
    spec += '16. "mcp_tool": {"server": "MCPサーバー名", "action": "list_tools | call_tool", "tool_name": "ツール名", "arguments": {}} -> 接続済み外部MCPサーバーのツールを実行する。\n'
    spec += '17. "windows_control": {"action": "play_pause | next_track | prev_track | volume_up | volume_down | volume_mute | notifications | lock_pc | sleep_pc", "steps": 2} -> Windowsメディア/音量操作・通知確認を行う。マスターから明示的にロックやスリープを指示された場合のみ `lock_pc` / `sleep_pc` を確認なしで即実行する。\n'
    return spec


VTUBER_TOOLS_SPEC = """
利用可能な自律ツール一覧（必要な場合のみ tool_call に指定してください）:
1. "get_weather": {"location": "地名（例: 亀岡, 東京, 大阪）"} -> 日本全国・海外の今日・明日・週間の天気、最高/最低気温(℃)、降水確率(%)を気象庁データから正確に取得する。天気・気温・降水確率の質問には必ずこれを使うこと。
2. "web_search": {"query": "検索キーワード"} -> Web(Yahoo!/Bing)・最新ニュース・WikipediaからURLと公開日付付きで検索する。
3. "fetch_webpage": {"url": "https://..."} -> 指定したURLのWebページ本文テキストを深掘り取得して詳細を確認する。
4. "vision_capture": {"target": "active_window | fullscreen | clipboard"} -> 現在の配信画面やウィンドウをキャプチャして視覚認識する。
5. "system_diagnostics": {} -> 配信PCのメモリ・ディスク・現在時刻などを診断する。
"""


AVAILABLE_TOOLS_SPEC = """
利用可能な自律ツール一覧（必要な場合のみ tool_call に指定してください）:
1. "get_weather": {"location": "地名（例: 亀岡, 東京, 大阪）"} -> 日本全国・海外の今日・明日・週間の天気、最高/最低気温(℃)、降水確率(%)を気象庁データから正確に取得する。天気・気温・降水確率の質問には必ずこれを使うこと。
2. "web_search": {"query": "検索キーワード"} -> Web(Yahoo!/Bing)・最新ニュース・WikipediaからURLと公開日付付きで検索する。
3. "fetch_webpage": {"url": "https://..."} -> 指定したURLのWebページ本文テキストを深掘り取得して詳細を確認する。
4. "vision_capture": {"target": "active_window | fullscreen | clipboard"} -> 現在のPC画面やアクティブウィンドウをキャプチャして視覚認識する。
5. "system_diagnostics": {} -> PCのメモリ・ディスク・現在時刻・現在開いているアクティブウィンドウ名を診断する。
6. "save_memory": {"category": "user_pref | fact | research | memo", "content": "記録する内容", "importance": 4} -> 重要な情報を長期記憶DBへ永続保存する（importanceは1〜5）。
7. "search_memory": {"keyword": "検索語"} -> 長期記憶DBから過去の記録を検索する。
8. "manage_task": {"action": "add | list", "title": "予定・タスク名", "due_minutes": 0, "due_iso": "HH:MM または YYYY-MM-DDTHH:MM", "repeat_rule": "none | hourly | daily | weekly"} -> 予定表・タイマーアラート・繰り返しリマインダーを登録・確認する。
"""
