"""
FastAPI エントリポイントおよび REST / WebSocket ルーティング定義。
全14機能（デスクトップマスコット起動、画面認識Vision、ポモドーロ、クリップボード/ファイルD&D解析、
YouTube/Twitch実配信接続、ボイス詳細/辞書、話題デッキ、長期記憶CRUD、予定表/スヌーズ）のAPIを提供する。
"""

import base64
import os
import subprocess
import sys
import time

# Windows(cp932)環境での絵文字ログ出力による UnicodeEncodeError 起動クラッシュを防止
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
from contextlib import asynccontextmanager
from typing import Any
from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.audio_io import whisper_transcriber
from backend.automation import approval_manager, mcp_manager
from backend.config import AUDIO_CACHE_DIR, BASE_DIR, KARAOKE_DIR, SKILL_PRESETS, VISION_CAPTURE_DIR, config
from backend.event_loop import is_interactive_default_desktop, orchestrator
from backend.llm_engine import llm_engine
from backend.memory import memory_manager
from backend.models import AgentEvent, ChatRequest, EventSource
from backend.rag_engine import AUDIO_EXTENSIONS, extract_text_from_bytes, rag_engine
from backend.tools import (
    capture_screen_image,
    get_clipboard_text,
    get_system_metrics,
    launch_local_target,
)
from backend.tts_engine import tts_engine
from backend.vtuber_studio import (
    build_stream_archive_artifacts,
    interactive_game,
    karaoke_manager,
    obs_ws_client,
    vts_client,
)


FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """アプリケーション起動時にオーケストレーターの自律ループを開始する。"""
    os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)
    os.makedirs(VISION_CAPTURE_DIR, exist_ok=True)
    await orchestrator.start()
    yield
    await orchestrator.discord_connector.stop()
    if orchestrator.mascot_process and orchestrator.mascot_process.poll() is None:
        try:
            orchestrator.mascot_process.terminate()
        except Exception:
            pass


app = FastAPI(
    title="MyAgent: AIエージェント × 自律型VTuber 統合基盤 (NIA)",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    """現在のエージェント全設定、システムメトリクス、記憶、タスク一覧を返す。"""
    await llm_engine.detect_ollama_models()
    recent = memory_manager.get_recent_conversations(limit=1, mode=config.mode)
    return {
        "config": orchestrator.get_public_config(),
        "system_metrics": get_system_metrics(),
        "memories": memory_manager.search_memories(limit=15),
        "tasks": memory_manager.get_tasks(status="pending"),
        "recent_last": recent[-1] if recent else None,
    }


@app.post("/api/chat")
async def post_chat(req: ChatRequest) -> dict[str, Any]:
    """ユーザー入力や外部コメント・スパチャをイベントキューへ投入する。"""
    event = AgentEvent(
        source=req.source,
        user=req.user,
        content=req.content,
        metadata=req.metadata,
    )
    accepted = await orchestrator.enqueue_event(event)
    return {"accepted": accepted, "event_id": event.id}


@app.post("/api/config")
async def update_config(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """モード切替、ペルソナ設定、および全14機能の詳細設定をリアルタイム反映・永続化する。"""
    prev_mode = config.mode
    prev_outfit_auto = config.outfit_auto_mode
    prev_outfit_interval = config.outfit_interval_min
    if payload.get("api_key"):
        provider = payload.get("llm_provider") or config.llm_provider
        if provider == "gemini":
            config.gemini_api_key = str(payload["api_key"]).strip()
        elif provider == "openai":
            config.openai_api_key = str(payload["api_key"]).strip()

    if payload.get("clear_discord_token"):
        config.discord_bot_token = ""
        config.save_secrets()
    elif payload.get("discord_bot_token") and str(payload["discord_bot_token"]).strip():
        config.discord_bot_token = str(payload["discord_bot_token"]).strip()
        config.save_secrets()

    secrets_dirty = False
    if payload.get("clear_github_token"):
        config.github_token = ""
        secrets_dirty = True
    elif payload.get("github_token") and str(payload["github_token"]).strip():
        config.github_token = str(payload["github_token"]).strip()
        secrets_dirty = True

    if payload.get("clear_obs_password"):
        config.obs_ws_password = ""
        secrets_dirty = True
    elif payload.get("obs_ws_password") and str(payload["obs_ws_password"]).strip():
        config.obs_ws_password = str(payload["obs_ws_password"]).strip()
        secrets_dirty = True

    if payload.get("clear_twitch_oauth_token"):
        config.twitch_oauth_token = ""
        secrets_dirty = True
    elif payload.get("twitch_oauth_token") and str(payload["twitch_oauth_token"]).strip():
        config.twitch_oauth_token = str(payload["twitch_oauth_token"]).strip()
        secrets_dirty = True

    if secrets_dirty:
        config.save_secrets()

    if payload.get("reset_persona"):
        config.reset_persona_settings()
        memory_manager.sync_with_persona(
            config.agent_name, config.persona_agent, config.persona_vtuber
        )
    else:
        persona_changed = config.update_from_dict(payload)
        config.save_persona_settings()
        if persona_changed:
            memory_manager.sync_with_persona(
                config.agent_name, config.persona_agent, config.persona_vtuber
            )

    # AIエージェントモードへ戻った場合はVTuber模擬配信ストリームを自動停止する
    if config.mode != "vtuber":
        orchestrator.demo_stream_active = False

    # 自動お着替えモードや間隔が変更された場合はタイマーをリセットしてログ出力
    if config.outfit_auto_mode != prev_outfit_auto or config.outfit_interval_min != prev_outfit_interval:
        orchestrator.last_outfit_random_time = time.time()
        print(
            f"[Outfit] 自動お着替え設定更新: mode={config.outfit_auto_mode}, "
            f"interval={config.outfit_interval_min}分"
        )

    # Discord Bot およびローカルマイク STT の稼働状態を設定値に同期する
    await orchestrator.discord_connector.sync_state()
    orchestrator.sync_voice_listener()

    # ポモドーロ設定時間が変更され待機中なら残り時間を更新
    if not orchestrator.pomodoro["active"]:
        orchestrator.pomodoro["remaining_sec"] = (
            config.pomodoro_work_min * 60
            if orchestrator.pomodoro["phase"] == "work"
            else config.pomodoro_break_min * 60
        )

    state_payload: dict[str, Any] = {
        "type": "config_updated",
        "config": orchestrator.get_public_config(),
        "memories": memory_manager.search_memories(limit=15),
    }
    if config.mode != prev_mode:
        state_payload["history"] = memory_manager.get_recent_conversations(
            limit=min(50, max(10, int(config.conversation_history_limit))),
            mode=config.mode,
        )
    await orchestrator.broadcast(state_payload)
    return state_payload


@app.post("/api/mascot/toggle")
async def toggle_desktop_mascot(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Windows透過・最前面デスクトップマスコット常駐プロセスを起動または停止する。"""
    native_ok = is_interactive_default_desktop()
    if not native_ok:
        # サーバーが隔離コンソール(exebox等)内で稼働している場合はブラウザ側PiPマスコットの状態と連動する
        if "active" in payload:
            orchestrator.browser_mascot_active = bool(payload["active"])
        else:
            orchestrator.browser_mascot_active = not orchestrator.browser_mascot_active
        await orchestrator.broadcast(
            {"type": "config_updated", "config": orchestrator.get_public_config()}
        )
        return {
            "ok": True,
            "mascot_running": orchestrator.browser_mascot_active,
            "native_desktop_available": False,
        }

    is_running = (
        orchestrator.mascot_process is not None and orchestrator.mascot_process.poll() is None
    )
    if is_running:
        try:
            orchestrator.mascot_process.terminate()
            orchestrator.mascot_process = None
        except Exception:
            pass
        running = False
    else:
        script_path = os.path.join(BASE_DIR, "scripts", "desktop_mascot.py")
        py_dir = os.path.dirname(sys.executable)
        pythonw_exe = os.path.join(py_dir, "pythonw.exe")
        exe_to_use = pythonw_exe if os.path.exists(pythonw_exe) else sys.executable
        orchestrator.mascot_process = subprocess.Popen(
            [exe_to_use, script_path],
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        running = True

    await orchestrator.broadcast(
        {"type": "config_updated", "config": orchestrator.get_public_config()}
    )
    return {"ok": True, "mascot_running": running, "native_desktop_available": True}


@app.post("/api/vision_analyze")
async def vision_analyze_api(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """現在の画面またはアクティブウィンドウをキャプチャし、ニアに視覚解析させる。"""
    target = payload.get("target") or config.vision_target
    user_prompt = payload.get("prompt") or "現在見えているPC画面の内容を確認して、状況の要約やアドバイスを教えて"
    cap = capture_screen_image(target_override=target)
    if not cap.get("ok"):
        return {"ok": False, "error": cap.get("error")}

    full_prompt = (
        f"【画面キャプチャ解析リクエスト (対象: {cap['target']} / ウィンドウ名: {cap['window_title']})】\n"
        f"マスターからの指示: {user_prompt}"
    )
    event = AgentEvent(
        source=EventSource.USER_TEXT,
        user="Master (Vision)",
        content=full_prompt,
        metadata={"image_base64": cap["base64_jpeg"], "image_url": cap["image_url"]},
        priority=1,
    )
    await orchestrator.enqueue_event(event)
    return {"ok": True, "capture": {k: v for k, v in cap.items() if k != "base64_jpeg"}}


@app.post("/api/clipboard_analyze")
async def clipboard_analyze_api() -> dict[str, Any]:
    """現在のクリップボードテキストを取得してニアに解析・要約させる。"""
    clip = get_clipboard_text()
    if not clip:
        return {"ok": False, "error": "クリップボードにテキストデータがありません。"}
    orchestrator.last_clipboard_text = clip
    event = AgentEvent(
        source=EventSource.USER_TEXT,
        user="Master (Clipboard)",
        content=f"クリップボードの以下のテキストを解析・要約してアドバイスして:\n「{clip[:1500]}」",
        priority=1,
    )
    await orchestrator.enqueue_event(event)
    return {"ok": True, "preview": clip[:120]}


@app.post("/api/upload_analyze")
async def upload_analyze_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """ドラッグ＆ドロップされたファイル（テキスト・コード・PDF・Office・音声・画像Base64）を解析する。"""
    filename = str(payload.get("filename", "dropped_file")).strip()
    text_content = str(payload.get("text_content", "")).strip()
    image_base64 = str(payload.get("image_base64", "")).strip()
    file_base64 = str(payload.get("file_base64", "")).strip()
    custom_prompt = str(payload.get("prompt", "このファイルの内容を要約・解説して")).strip()
    ext = os.path.splitext(filename)[1].lower()

    meta: dict[str, Any] = {"filename": filename}
    if image_base64:
        if "," in image_base64:
            image_base64 = image_base64.split(",", 1)[1]
        meta["image_base64"] = image_base64
        content = f"【画像ファイルドロップ: {filename}】{custom_prompt}"
    elif file_base64:
        if "," in file_base64:
            file_base64 = file_base64.split(",", 1)[1]
        try:
            raw_bytes = base64.b64decode(file_base64)
        except Exception as exc:
            return {"ok": False, "error": f"ファイルデコード失敗: {exc}"}

        if ext in AUDIO_EXTENSIONS:
            transcript = await whisper_transcriber.transcribe_bytes(raw_bytes, filename=filename)
            if not transcript:
                return {"ok": False, "error": "音声ファイルから発話内容を抽出できませんでした。"}
            content = (
                f"【音声ファイル文字起こし解析: {filename}】\n"
                f"指示: {custom_prompt}\n\n--- 文字起こしテキスト ---\n{transcript[:int(config.fetch_max_chars)]}"
            )
        else:
            ok_ext, extracted = extract_text_from_bytes(
                raw_bytes, filename, max_chars=int(config.fetch_max_chars)
            )
            if not ok_ext:
                return {"ok": False, "error": extracted}
            content = f"【ドキュメント解析 ({filename})】\n指示: {custom_prompt}\n\n--- 抽出内容 ---\n{extracted}"
    else:
        excerpt = text_content[: int(config.fetch_max_chars)]
        content = f"【ファイルドロップ解析: {filename}】\n指示: {custom_prompt}\n\n--- ファイル内容 ---\n{excerpt}"

    event = AgentEvent(
        source=EventSource.USER_TEXT,
        user="Master (FileDrop)",
        content=content,
        metadata=meta,
        priority=1,
    )
    await orchestrator.enqueue_event(event)
    return {"ok": True, "filename": filename}


@app.post("/api/pomodoro")
async def control_pomodoro(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """ポモドーロタイマーの開始・一時停止・リセット・フェーズ切替を制御する。"""
    action = str(payload.get("action", "toggle"))
    pomo = orchestrator.pomodoro
    pomo["last_tick"] = time.time()

    if action == "toggle":
        pomo["active"] = not pomo["active"]
    elif action == "reset":
        pomo["active"] = False
        pomo["phase"] = "work"
        pomo["remaining_sec"] = config.pomodoro_work_min * 60
    elif action == "switch_phase":
        pomo["phase"] = "break" if pomo["phase"] == "work" else "work"
        pomo["remaining_sec"] = (
            config.pomodoro_work_min * 60
            if pomo["phase"] == "work"
            else config.pomodoro_break_min * 60
        )

    await orchestrator.broadcast({"type": "pomodoro_tick", "pomodoro": pomo})
    return {"ok": True, "pomodoro": pomo}


@app.post("/api/talk_deck/next")
async def trigger_talk_deck_api() -> dict[str, Any]:
    """VTuber話題デッキから次のトーク企画を即時発動する。"""
    return await orchestrator.trigger_next_talk_deck(advance_theme=True)


@app.post("/api/talk_deck/refresh_web")
async def refresh_talk_deck_web_api(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Web検索＋AI生成で最新の面白いお題を話題デッキへ自動補充する。"""
    append = bool(payload.get("append", True))
    return await orchestrator.refresh_talk_deck_from_web(append=append)


@app.post("/api/talk_deck/load_default50")
async def load_default_talk_deck_50_api() -> dict[str, Any]:
    """標準搭載のおすすめ配信お題50選を話題デッキへ一括セットする。"""
    from backend.config import DEFAULT_TALK_DECK

    config.talk_deck_themes = list(DEFAULT_TALK_DECK)
    config.save_persona_settings()
    await orchestrator.broadcast({"type": "config_updated", "config": orchestrator.get_public_config()})
    return {"ok": True, "total_count": len(config.talk_deck_themes), "themes": config.talk_deck_themes}


@app.post("/api/demo_stream")
async def toggle_demo_stream(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """VTuberモード用の模擬ライブコメント自動ストリームのON/OFFを切り替える。"""
    prev_mode = config.mode
    active = bool(payload.get("active", not orchestrator.demo_stream_active))
    orchestrator.demo_stream_active = active
    if active and config.mode != "vtuber":
        config.mode = "vtuber"
    demo_payload: dict[str, Any] = {
        "type": "demo_stream_toggled",
        "demo_stream_active": orchestrator.demo_stream_active,
        "mode": config.mode,
        "active_ollama_model": config.get_effective_ollama_model(),
    }
    if config.mode != prev_mode:
        demo_payload["history"] = memory_manager.get_recent_conversations(
            limit=min(50, max(10, int(config.conversation_history_limit))),
            mode=config.mode,
        )
    await orchestrator.broadcast(demo_payload)
    return {"demo_stream_active": orchestrator.demo_stream_active, "mode": config.mode}


@app.post("/api/memories")
async def add_memory_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """UIから長期記憶を手動追加する。"""
    category = str(payload.get("category", "fact")).strip() or "fact"
    content = str(payload.get("content", "")).strip()
    importance = int(payload.get("importance", 4) or 4)
    if not content:
        return {"ok": False, "error": "内容が空です"}
    mid = memory_manager.save_memory(category=category, content=content, importance=importance)
    memories = memory_manager.search_memories(limit=15)
    await orchestrator.broadcast({"type": "memories_updated", "memories": memories})
    return {"ok": True, "id": mid, "memories": memories}


@app.put("/api/memories/{memory_id}")
async def update_memory_api(memory_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """指定IDの長期記憶の内容や重要度（★1〜5）を更新する。"""
    ok = memory_manager.update_memory(
        memory_id=memory_id,
        category=payload.get("category"),
        content=payload.get("content"),
        importance=payload.get("importance"),
    )
    memories = memory_manager.search_memories(limit=15)
    await orchestrator.broadcast({"type": "memories_updated", "memories": memories})
    return {"ok": ok, "memories": memories}


@app.delete("/api/memories/{memory_id}")
async def delete_memory_api(memory_id: int) -> dict[str, Any]:
    """指定IDの長期記憶を削除する。"""
    memory_manager.delete_memory(memory_id)
    memories = memory_manager.search_memories(limit=15)
    await orchestrator.broadcast({"type": "memories_updated", "memories": memories})
    return {"ok": True, "memories": memories}


@app.delete("/api/memories")
async def clear_user_memories_api() -> dict[str, Any]:
    """ペルソナ設定連動以外の獲得記憶をすべて一括クリアする。"""
    memory_manager.clear_user_memories()
    memories = memory_manager.search_memories(limit=15)
    await orchestrator.broadcast({"type": "memories_updated", "memories": memories})
    return {"ok": True, "memories": memories}


@app.get("/api/memories/semantic")
async def semantic_search_memories_api(q: str = "", limit: int = 8) -> dict[str, Any]:
    """意味ベクトル＋概念類似度を用いた長期記憶ハイブリッド検索を実行する。"""
    items = memory_manager.search_memories_semantic(query=q, limit=max(1, min(20, limit)), exclude_persona=True)
    return {"ok": True, "query": q, "items": items}


@app.post("/api/memories/consolidate")
async def consolidate_memories_api() -> dict[str, Any]:
    """重複・高類似記憶のマージと古い低重要度ノイズの整理（Sleep Consolidation）を即時実行する。"""
    res = memory_manager.consolidate_memories()
    memories = memory_manager.search_memories(limit=15)
    await orchestrator.broadcast(
        {"type": "memories_updated", "memories": memories, "consolidation_report": res}
    )
    return {**res, "memories": memories}


@app.get("/api/presets")
async def list_skill_presets_api() -> dict[str, Any]:
    """定義済みスキルプリセットパック一覧を返す。"""
    items = [
        {"id": pid, "name": p["name"], "description": p["description"], "mode": p["mode"]}
        for pid, p in SKILL_PRESETS.items()
    ]
    return {"ok": True, "presets": items, "active_preset_id": config.active_preset_id}


@app.post("/api/presets/apply")
async def apply_skill_preset_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """スキルプリセットパックを適用してモード・UI粒度・ツール・ルーターを一括設定する。"""
    preset_id = str(payload.get("preset_id", "")).strip()
    prev_mode = config.mode
    res = config.apply_skill_preset(preset_id)
    if not res.get("ok"):
        return res

    if config.mode != "vtuber":
        orchestrator.demo_stream_active = False

    if res["preset"].get("ensure_morning_workflow"):
        existing_wfs = memory_manager.list_workflows()
        if not any(w.get("workflow_type") == "briefing" for w in existing_wfs):
            memory_manager.add_workflow(
                name="朝の戦術ブリーフィング",
                workflow_type="briefing",
                schedule_kind="daily",
                schedule_value="08:00",
                prompt="天気・今日期限の予定・最新テックニュースを簡潔にまとめて報告してください。",
            )
            await orchestrator.broadcast({"type": "workflows_updated", "workflows": memory_manager.list_workflows()})

    await orchestrator.discord_connector.sync_state()
    orchestrator.sync_voice_listener()

    state_payload: dict[str, Any] = {
        "type": "config_updated",
        "config": orchestrator.get_public_config(),
        "memories": memory_manager.search_memories(limit=15),
    }
    if config.mode != prev_mode:
        state_payload["history"] = memory_manager.get_recent_conversations(
            limit=min(50, max(10, int(config.conversation_history_limit))),
            mode=config.mode,
        )
    await orchestrator.broadcast(state_payload)
    return {"ok": True, "preset_id": preset_id, "preset": res["preset"], **state_payload}


@app.get("/api/outfits")
async def list_outfits_api() -> dict[str, Any]:
    """利用可能な衣装・アクセサリー・親密度・時間帯/天気環境状態を返す。"""
    from backend.config import ACCESSORY_CATALOG, OUTFIT_GENRES
    from backend.tools import list_available_outfits

    return {
        "ok": True,
        "active_outfit": config.active_outfit,
        "active_accessories": list(config.active_accessories or []),
        "outfits": list_available_outfits(),
        "accessories": list(ACCESSORY_CATALOG.values()),
        "outfit_genres": OUTFIT_GENRES,
        "affection": memory_manager.get_affection_state(),
        "environment": orchestrator.get_environment_state(),
    }


@app.post("/api/appearance")
async def update_appearance_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """衣装やアクセサリー装着状態、時間帯自動着替え・天気背景演出を更新して全クライアントへ配信する。"""
    from backend.config import ACCESSORY_CATALOG, OUTFIT_GENRES
    from backend.tools import list_available_outfits

    avail_ids = {o["id"] for o in list_available_outfits() if o.get("available")}
    if "outfit" in payload and payload["outfit"] is not None:
        oid = str(payload["outfit"]).strip().lower()
        if oid in avail_ids:
            config.active_outfit = oid
            orchestrator.last_outfit_random_time = time.time()
    if "toggle_accessory" in payload and payload["toggle_accessory"]:
        acc = str(payload["toggle_accessory"]).strip().lower()
        if acc in ACCESSORY_CATALOG:
            cur = list(config.active_accessories or [])
            if acc in cur:
                cur.remove(acc)
            else:
                cur.append(acc)
            config.active_accessories = cur
    if "accessories" in payload and isinstance(payload["accessories"], list):
        config.active_accessories = [
            str(a).strip().lower() for a in payload["accessories"] if str(a).strip().lower() in ACCESSORY_CATALOG
        ]
    for bf in ("auto_outfit_by_time", "weather_bg_effect_enabled", "interaction_enabled", "affection_enabled"):
        if bf in payload and payload[bf] is not None:
            setattr(config, bf, bool(payload[bf]))

    config.save_persona_settings()
    state = await orchestrator.broadcast_appearance_state()
    await orchestrator.broadcast({"type": "config_updated", "config": orchestrator.get_public_config()})
    return {"ok": True, **state}


@app.post("/api/outfits/random")
async def random_outfit_api() -> dict[str, Any]:
    """利用可能な衣装からランダムに1着選んで即時着替える（メインページのワンタッチボタン用）。"""
    return await orchestrator.pick_random_outfit(exclude_current=True)


@app.post("/api/outfits/upload")
async def upload_custom_outfit_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """
    Nano Banana Pro 等で生成した衣装画像（クロマキー緑 #00FF00 / 白背景 / 透過PNG 自動判別）を
    アップロード受け付けし、高精度透過 full.png / bust.png 生成・meta.json 保存・即時着替え反映する。
    """
    import asyncio
    import datetime as _dt
    import io
    import re
    from PIL import Image
    from scripts.build_outfit_assets import build_outfit_from_pil

    full_b64 = str(payload.get("full_image_base64", "") or "").strip()
    if not full_b64:
        return {"ok": False, "error": "全身画像が選択されていません。"}
    if "," in full_b64:
        full_b64 = full_b64.split(",", 1)[1]

    raw_id = str(payload.get("outfit_id", "") or "").strip().lower()
    safe_id = re.sub(r"[^a-z0-9_-]", "", raw_id)[:40]
    if not safe_id or safe_id == "default":
        safe_id = f"custom_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    name = str(payload.get("name", "") or "").strip()[:60] or f"✨ {safe_id}"
    genre = str(payload.get("genre", "custom") or "custom").strip().lower()
    if genre not in {"school", "casual", "sport", "swimwear", "cosplay", "custom"}:
        genre = "custom"
    description = str(payload.get("description", "") or "").strip()[:160] or f"カスタム登録衣装 ({name})"
    bust_ratio = max(0.22, min(0.65, float(payload.get("bust_ratio", 0.28) or 0.28)))
    flip_horizontal = bool(payload.get("flip_horizontal", False))
    face_sync = bool(payload.get("face_sync", True))

    try:
        full_bytes = base64.b64decode(full_b64)
        full_pil = Image.open(io.BytesIO(full_bytes))
        full_pil.load()
    except Exception as exc:
        return {"ok": False, "error": f"全身画像の読み込みに失敗しました: {exc}"}

    bust_pil_in = None
    bust_b64 = str(payload.get("bust_image_base64", "") or "").strip()
    if bust_b64:
        if "," in bust_b64:
            bust_b64 = bust_b64.split(",", 1)[1]
        try:
            bust_bytes = base64.b64decode(bust_b64)
            bust_pil_in = Image.open(io.BytesIO(bust_bytes))
            bust_pil_in.load()
        except Exception as exc:
            return {"ok": False, "error": f"バストアップ画像の読み込みに失敗しました: {exc}"}

    try:
        build_res = await asyncio.to_thread(
            build_outfit_from_pil,
            safe_id,
            full_pil,
            bust_pil_in=bust_pil_in,
            bust_ratio=bust_ratio,
            flip_horizontal=flip_horizontal,
            face_sync=face_sync,
            meta={"name": name, "short": name, "genre": genre, "description": description},
        )
    except Exception as exc:
        return {"ok": False, "error": f"透過処理に失敗しました: {exc}"}

    # 登録完了後、即座にその衣装へ着替えて全クライアントへ配信
    config.active_outfit = safe_id
    config.save_persona_settings()
    state = await orchestrator.broadcast_appearance_state()
    await orchestrator.broadcast({"type": "config_updated", "config": orchestrator.get_public_config()})
    return {
        "ok": True,
        "outfit_id": safe_id,
        "bg_mode": build_res.get("bg_mode", ""),
        "face_sync": bool(build_res.get("face_sync", False)),
        "full_size": build_res.get("full_size", []),
        "bust_size": build_res.get("bust_size", []),
        **state,
    }


@app.patch("/api/outfits/{outfit_id}")
async def update_outfit_meta_api(outfit_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """登録済み衣装の表示名・ジャンル・口パク/瞬き連動(face_sync)をあとから編集して meta.json へ保存する。"""
    import json as _json
    import re

    safe_id = re.sub(r"[^a-z0-9_-]", "", (outfit_id or "").strip().lower())
    if not safe_id or safe_id == "default":
        return {"ok": False, "error": "標準制服 (default) のメタデータは変更できません。"}
    target_dir = os.path.join(FRONTEND_DIR, "assets", "outfits", safe_id)
    if not os.path.isdir(target_dir):
        return {"ok": False, "error": "指定の衣装が見つかりません。"}

    meta_path = os.path.join(target_dir, "meta.json")
    cur: dict[str, Any] = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                cur = _json.load(f)
        except Exception:
            cur = {}

    if "name" in payload and str(payload["name"]).strip():
        nm = str(payload["name"]).strip()[:60]
        cur["name"] = nm
        cur["short"] = nm
    if "genre" in payload:
        g = str(payload["genre"]).strip().lower()
        if g in {"school", "casual", "sport", "swimwear", "cosplay", "custom"}:
            cur["genre"] = g
    if "face_sync" in payload:
        want_sync = bool(payload["face_sync"])
        if want_sync:
            import asyncio
            from PIL import Image as _PILImage
            from scripts.build_outfit_assets import realign_outfit_dir

            bust_p = os.path.join(target_dir, "bust.png")
            need_realign = True
            if os.path.isfile(bust_p):
                try:
                    with _PILImage.open(bust_p) as _bi:
                        if _bi.size == (825, 840):
                            need_realign = False
                except Exception:
                    pass
            if need_realign:
                aligned_ok = await asyncio.to_thread(realign_outfit_dir, safe_id)
                cur["face_sync"] = bool(aligned_ok)
            else:
                cur["face_sync"] = True
        else:
            cur["face_sync"] = False
    cur["id"] = safe_id

    with open(meta_path, "w", encoding="utf-8") as f:
        _json.dump(cur, f, ensure_ascii=False, indent=2)

    state = await orchestrator.broadcast_appearance_state()
    return {"ok": True, "outfit_id": safe_id, "meta": cur, **state}


@app.delete("/api/outfits/{outfit_id}")
async def delete_custom_outfit_api(outfit_id: str) -> dict[str, Any]:
    """カスタム追加した衣装フォルダを削除する（標準制服 default は削除不可）。"""
    import re
    import shutil

    safe_id = re.sub(r"[^a-z0-9_-]", "", (outfit_id or "").strip().lower())
    if not safe_id or safe_id == "default":
        return {"ok": False, "error": "標準制服 (default) は削除できません。"}
    target_dir = os.path.join(FRONTEND_DIR, "assets", "outfits", safe_id)
    if not os.path.isdir(target_dir):
        return {"ok": False, "error": "指定の衣装が見つかりません。"}
    shutil.rmtree(target_dir, ignore_errors=True)
    if config.active_outfit == safe_id:
        config.active_outfit = "default"
        config.save_persona_settings()
    state = await orchestrator.broadcast_appearance_state()
    await orchestrator.broadcast({"type": "config_updated", "config": orchestrator.get_public_config()})
    return {"ok": True, "deleted": safe_id, **state}


@app.post("/api/interact")
async def interact_avatar_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """頭なでなで (`pet`) やほっぺつつき (`poke`) のインタラクションを実行する。"""
    kind = str(payload.get("kind", "pet") or "pet").strip().lower()
    return await orchestrator.handle_avatar_interaction(kind=kind)


@app.post("/api/affection/reset")
async def reset_affection_api() -> dict[str, Any]:
    """本日のデレ度を手動で初期ツン状態 (Lv.1 / 10pt) へ戻す（累計ふれあい回数は保持）。"""
    aff = memory_manager.reset_affection_mood()
    await orchestrator.broadcast_appearance_state()
    return {"ok": True, "affection": aff}


@app.post("/api/tasks")
async def add_task_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """UIからカレンダー予定・タスク・リマインダーを追加する。"""
    title = str(payload.get("title", "")).strip()
    due_minutes = float(payload.get("due_minutes", 0) or 0)
    due_iso = str(payload.get("due_iso", "") or "")
    repeat_rule = str(payload.get("repeat_rule", "none") or "none")
    if not title:
        return {"ok": False, "error": "タイトルが空です"}
    tid = memory_manager.add_task(
        title=title, due_minutes=due_minutes, due_iso=due_iso, repeat_rule=repeat_rule
    )
    tasks = memory_manager.get_tasks(status="pending")
    await orchestrator.broadcast({"type": "tasks_updated", "tasks": tasks})
    return {"ok": True, "id": tid, "tasks": tasks}


@app.post("/api/tasks/{task_id}/complete")
async def complete_task_api(task_id: int) -> dict[str, Any]:
    """指定IDのタスクを完了にする。"""
    ok = memory_manager.complete_task(task_id)
    tasks = memory_manager.get_tasks(status="pending")
    await orchestrator.broadcast({"type": "tasks_updated", "tasks": tasks})
    return {"ok": ok, "tasks": tasks}


@app.post("/api/tasks/{task_id}/snooze")
async def snooze_task_api(task_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """指定IDのタスクを指定分数後へスヌーズする。"""
    mins = float(payload.get("snooze_minutes") or config.schedule_snooze_min or 5)
    ok = memory_manager.snooze_task(task_id, snooze_minutes=mins)
    tasks = memory_manager.get_tasks(status="pending")
    await orchestrator.broadcast({"type": "tasks_updated", "tasks": tasks})
    return {"ok": ok, "tasks": tasks}


@app.delete("/api/tasks/{task_id}")
async def delete_task_api(task_id: int) -> dict[str, Any]:
    """指定IDのタスクを削除する。"""
    memory_manager.delete_task(task_id)
    tasks = memory_manager.get_tasks(status="pending")
    await orchestrator.broadcast({"type": "tasks_updated", "tasks": tasks})
    return {"ok": True, "tasks": tasks}


@app.delete("/api/conversations")
async def clear_conversations_api() -> dict[str, Any]:
    """現在アクティブなモードの対話・配信コメント履歴をクリアする。"""
    memory_manager.clear_conversations(mode=config.mode)
    await orchestrator.broadcast({"type": "conversations_cleared", "mode": config.mode})
    return {"ok": True, "mode": config.mode}


@app.post("/api/speech/stop")
async def stop_speech_api(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """現在再生中の音声・口パクを即座に停止する。"""
    clear_queue = bool(payload.get("clear_queue", False))
    dropped = await orchestrator.stop_speech(clear_queue=clear_queue)
    return {"ok": True, "dropped": dropped}


@app.post("/api/voice/mute")
async def toggle_voice_mute_api(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """音声出力ミュートのON/OFFを切り替える。"""
    if "muted" in payload:
        config.voice_muted = bool(payload["muted"])
    else:
        config.voice_muted = not config.voice_muted
    config.save_persona_settings()
    if config.voice_muted:
        await orchestrator.stop_speech(clear_queue=False)
    await orchestrator.broadcast(
        {"type": "config_updated", "config": orchestrator.get_public_config()}
    )
    return {"ok": True, "voice_muted": config.voice_muted}


@app.get("/api/engines/status")
async def get_engines_status_api() -> dict[str, Any]:
    """Ollama / AivisSpeech / VOICEVOX の接続状態および音声キャッシュ統計を返す。"""
    status = await orchestrator.refresh_engine_status()
    return {"ok": True, "engine_status": status}


@app.post("/api/engines/aivisspeech/start")
async def start_aivisspeech_background_api() -> dict[str, Any]:
    """AivisSpeech を二重起動防止つきで裏起動（バックグラウンド常駐）し、TTSプロバイダを自動同期する。"""
    from backend.tools import ensure_aivisspeech_background

    res = await ensure_aivisspeech_background(wait_ready_sec=12.0, hide_gui=True)
    if res.get("ok") and config.tts_provider != "aivisspeech":
        config.tts_provider = "aivisspeech"
        config.save_persona_settings()
    status = await orchestrator.refresh_engine_status()
    await orchestrator.broadcast(
        {"type": "config_updated", "config": orchestrator.get_public_config()}
    )
    return {**res, "engine_status": status}


@app.post("/api/audio/cleanup")
async def cleanup_audio_cache_api() -> dict[str, Any]:
    """古い音声キャッシュファイルを今すぐ整理する。"""
    removed = tts_engine.cleanup_old_cache()
    status = await orchestrator.refresh_engine_status()
    return {"ok": True, "removed": removed, "engine_status": status}


def _get_startup_vbs_path() -> str:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return ""
    return os.path.join(
        appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "NIA_Mascot_AutoStart.vbs"
    )


@app.get("/api/startup/status")
async def get_startup_status_api() -> dict[str, Any]:
    """Windowsスタートアップ自動起動の登録状態を返す。"""
    path = _get_startup_vbs_path()
    return {"ok": True, "enabled": bool(path and os.path.exists(path)), "path": path}


@app.post("/api/startup/toggle")
async def toggle_startup_api(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Windowsスタートアップへの自動起動登録をON/OFFする。"""
    if os.name != "nt":
        return {"ok": False, "error": "Windows専用機能です。"}
    path = _get_startup_vbs_path()
    if not path:
        return {"ok": False, "error": "APPDATAパスを取得できませんでした。"}

    current = os.path.exists(path)
    target = bool(payload["enabled"]) if "enabled" in payload else (not current)
    try:
        if target:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            launcher_pyw = os.path.join(BASE_DIR, "scripts", "launcher.pyw")
            vbs_body = (
                'Set shell = CreateObject("WScript.Shell")\n'
                f'shell.CurrentDirectory = "{BASE_DIR}"\n'
                f'cmd = "pythonw ""{launcher_pyw}"" --mode=mascot"\n'
                "shell.Run cmd, 1, False\n"
            )
            with open(path, "w", encoding="cp932", errors="ignore") as f:
                f.write(vbs_body)
        else:
            if os.path.exists(path):
                os.remove(path)
        return {"ok": True, "enabled": os.path.exists(path), "path": path}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/viewers")
async def list_viewers_api() -> dict[str, Any]:
    """常連リスナー記憶一覧を返す。"""
    return {"ok": True, "viewers": memory_manager.list_viewers(limit=50)}


@app.delete("/api/viewers/{user_name}")
async def delete_viewer_api(user_name: str) -> dict[str, Any]:
    """指定視聴者の記憶を削除する。"""
    memory_manager.delete_viewer(user_name)
    viewers = memory_manager.list_viewers(limit=50)
    await orchestrator.broadcast({"type": "viewers_updated", "viewers": viewers})
    return {"ok": True, "viewers": viewers}


@app.delete("/api/viewers")
async def clear_viewers_api() -> dict[str, Any]:
    """全視聴者の記憶をクリアする。"""
    memory_manager.clear_viewers()
    await orchestrator.broadcast({"type": "viewers_updated", "viewers": []})
    return {"ok": True, "viewers": []}


@app.post("/api/topic")
async def set_topic_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """OBSモードの配信テーマ・サブテロップを更新する。"""
    topic = str(payload.get("topic", ""))
    await orchestrator.set_stream_topic(topic)
    return {"ok": True, "topic": config.stream_topic}


@app.post("/api/launch")
async def local_launch_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """許可リスト登録済みのローカルアプリ/URL/フォルダを起動する。"""
    name = str(payload.get("name", ""))
    res = launch_local_target(name)
    await orchestrator._flush_client_actions()
    return res


@app.post("/api/discord/test")
async def test_discord_dm_api() -> dict[str, Any]:
    """接続中の Discord Bot から通知先ユーザーへテストDMを送信する。"""
    res = await orchestrator.discord_connector.send_proactive_dm(
        "……テスト送信よ。Discord連携は正常に稼働してるわ。用があるならここから話しかけなさいよね。"
    )
    return {**res, "discord_status": orchestrator.discord_connector.get_status()}


@app.get("/api/audio/{filename}")
async def serve_audio(filename: str):
    """合成された音声ファイルを返す。"""
    safe_name = os.path.basename(filename)
    fpath = os.path.join(AUDIO_CACHE_DIR, safe_name)
    if not os.path.exists(fpath):
        return {"error": "Audio not found"}
    media_type = "audio/mpeg" if safe_name.endswith(".mp3") else "audio/wav"
    return FileResponse(fpath, media_type=media_type)


@app.get("/api/vision_file/{filename}")
async def serve_vision_file(filename: str):
    """キャプチャされた画面画像ファイルを返す。"""
    safe_name = os.path.basename(filename)
    fpath = os.path.join(VISION_CAPTURE_DIR, safe_name)
    if not os.path.exists(fpath):
        return {"error": "Vision image not found"}
    return FileResponse(fpath, media_type="image/jpeg")


# ===== AIエージェント高度拡張 REST API (承認 / ワークフロー / RAG / Deep Research / MCP / 通知) =====
@app.get("/api/agent/approvals")
async def list_approvals_api() -> dict[str, Any]:
    """承認待ちのコマンド実行リクエスト一覧を返す。"""
    return {"ok": True, "pending": approval_manager.list_pending()}


@app.post("/api/agent/approvals/{req_id}")
async def resolve_approval_api(req_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Web UI からコマンド実行リクエストを承認または拒否する。"""
    approved = bool(payload.get("approved", False))
    return await approval_manager.resolve(req_id, approved=approved, actor="WebUI")


@app.get("/api/agent/workflows")
async def list_workflows_api() -> dict[str, Any]:
    """登録済みの定時自律ワークフロー一覧を返す。"""
    return {"ok": True, "workflows": memory_manager.list_workflows()}


@app.post("/api/agent/workflows")
async def add_workflow_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """定時自律ワークフロー（朝ブリーフィング / Web更新監視 / カスタム）を登録する。"""
    name = str(payload.get("name", "")).strip() or "定時ブリーフィング"
    wf_type = str(payload.get("workflow_type", "briefing")).strip()
    sched_kind = str(payload.get("schedule_kind", "daily")).strip()
    sched_val = str(payload.get("schedule_value", "08:00")).strip()
    target_url = str(payload.get("target_url", "")).strip()
    prompt = str(payload.get("prompt", "")).strip()
    wid = memory_manager.add_workflow(
        name=name,
        workflow_type=wf_type,
        schedule_kind=sched_kind,
        schedule_value=sched_val,
        target_url=target_url,
        prompt=prompt,
    )
    workflows = memory_manager.list_workflows()
    await orchestrator.broadcast({"type": "workflows_updated", "workflows": workflows})
    return {"ok": True, "id": wid, "workflows": workflows}


@app.post("/api/agent/workflows/{workflow_id}/run")
async def run_workflow_now_api(workflow_id: int) -> dict[str, Any]:
    """指定IDのワークフローを今すぐテスト実行する。"""
    wf = memory_manager.get_workflow(workflow_id)
    if not wf:
        return {"ok": False, "error": "ワークフローが見つかりません。"}
    res = await orchestrator.execute_workflow_now(wf)
    await orchestrator.broadcast({"type": "workflows_updated", "workflows": memory_manager.list_workflows()})
    return res


@app.post("/api/agent/workflows/{workflow_id}/toggle")
async def toggle_workflow_api(workflow_id: int) -> dict[str, Any]:
    """指定IDのワークフローの有効/無効を切り替える。"""
    ok = memory_manager.toggle_workflow(workflow_id)
    workflows = memory_manager.list_workflows()
    await orchestrator.broadcast({"type": "workflows_updated", "workflows": workflows})
    return {"ok": ok, "workflows": workflows}


@app.delete("/api/agent/workflows/{workflow_id}")
async def delete_workflow_api(workflow_id: int) -> dict[str, Any]:
    """指定IDのワークフローを削除する。"""
    memory_manager.delete_workflow(workflow_id)
    workflows = memory_manager.list_workflows()
    await orchestrator.broadcast({"type": "workflows_updated", "workflows": workflows})
    return {"ok": True, "workflows": workflows}


@app.post("/api/agent/rag/reindex")
async def rag_reindex_api() -> dict[str, Any]:
    """対象フォルダのドキュメントを走査してローカル RAG インデックスを再構築する。"""
    res = rag_engine.index_directories()
    await orchestrator.broadcast({"type": "rag_updated", "stats": rag_engine.get_stats()})
    return res


@app.get("/api/agent/rag/search")
async def rag_search_api(q: str = "") -> dict[str, Any]:
    """ローカル RAG インデックスを直接検索してヒットチャンクを返す。"""
    hits = rag_engine.search(q, top_k=5)
    return {"ok": True, "query": q, "hits": hits, "stats": rag_engine.get_stats()}


@app.get("/api/agent/research")
async def list_research_api() -> dict[str, Any]:
    """バックグラウンド深掘り調査ジョブ履歴を返す。"""
    return {"ok": True, "jobs": orchestrator.research_jobs}


@app.post("/api/agent/research")
async def start_research_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """非同期バックグラウンド深掘り調査を新規起動する。"""
    topic = str(payload.get("topic", "")).strip()
    if not topic:
        return {"ok": False, "error": "調査テーマ (topic) が空です。"}
    job = await orchestrator.start_deep_research(topic)
    return {"ok": True, "job": job}


@app.post("/api/agent/mcp/list")
async def mcp_list_tools_api(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """設定中の MCP サーバーのツール一覧を取得する。"""
    srv = str(payload.get("server", ""))
    txt = await mcp_manager.invoke(server_name=srv, action="list_tools")
    return {"ok": True, "result": txt}


# ===== VTuber配信高度拡張 REST API (OBS / VTS / 投票企画 / お便り / アーカイブ / 歌枠) =====
@app.post("/api/vtuber/obs/test")
async def test_obs_ws_api() -> dict[str, Any]:
    """OBS Studio WebSocket v5 への接続テストを行う。"""
    return await obs_ws_client.test_connection()


@app.post("/api/vtuber/obs/scene")
async def switch_obs_scene_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """OBS シーンを手動または企画連動で切り替える。"""
    scene = str(payload.get("scene", "talk")).strip()
    res = await obs_ws_client.switch_scene(scene)
    await orchestrator.broadcast({"type": "obs_scene_changed", "scene": scene, "result": res})
    return res


@app.post("/api/vtuber/vts/test")
async def test_vts_api() -> dict[str, Any]:
    """VTube Studio Public API への接続・認証テストを行う。"""
    return await vts_client.test_connection()


@app.get("/api/vtuber/poll/state")
async def get_poll_state_api() -> dict[str, Any]:
    """現在の参加型投票・ゲーム状態を返す。"""
    return {"ok": True, "poll": interactive_game.get_state()}


@app.post("/api/vtuber/poll/start")
async def start_poll_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """視聴者参加型の投票 / クイズ / 大喜利を開始する。"""
    game_type = str(payload.get("game_type", "poll"))
    title = str(payload.get("title", "二択アンケート"))
    options = payload.get("options") if isinstance(payload.get("options"), list) else []
    duration_sec = int(payload.get("duration_sec", 45) or 45)
    answer_hint = str(payload.get("answer_hint", ""))
    state = interactive_game.start_game(
        game_type=game_type,
        title=title,
        options=options,
        duration_sec=duration_sec,
        answer_hint=answer_hint,
    )
    memory_manager.add_stream_highlight("poll", f"企画開始: {title}")
    await orchestrator.broadcast({"type": "poll_update", "poll": state})
    await orchestrator.enqueue_event(
        AgentEvent(
            source=EventSource.SILENCE_BREAKER,
            user="InteractiveGame",
            content=f"【参加型コーナー開始】お題「{title}」を開始しました！（{duration_sec}秒間受付）リスナーへ参加を呼びかけて盛り上げてください。",
            priority=2,
            metadata={"discord_category": "autonomous"},
        )
    )
    return {"ok": True, "poll": state}


@app.post("/api/vtuber/poll/stop")
async def stop_poll_api() -> dict[str, Any]:
    """進行中の参加型投票・ゲームを即時締切して結果発表を行う。"""
    if not interactive_game.active:
        return {"ok": False, "error": "進行中の企画がありません。"}
    finished = interactive_game.finish_game()
    memory_manager.add_stream_highlight("poll", f"企画結果: {finished['state']['title']}")
    await orchestrator.broadcast({"type": "poll_update", "poll": interactive_game.get_state()})
    await orchestrator.enqueue_event(
        AgentEvent(
            source=EventSource.SILENCE_BREAKER,
            user="InteractiveGame",
            content=finished["prompt"],
            priority=2,
            metadata={"discord_category": "autonomous"},
        )
    )
    return {"ok": True, "result": finished["state"]}


@app.get("/api/vtuber/mailbag")
async def list_mailbag_api() -> dict[str, Any]:
    """匿名お便り・質問箱一覧を返す。"""
    return {"ok": True, "items": memory_manager.list_mailbag(limit=50)}


@app.post("/api/vtuber/mailbag")
async def add_mailbag_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """匿名お便り・質問箱へ1件追加する。"""
    sender = str(payload.get("sender", "匿名リスナー")).strip() or "匿名リスナー"
    question = str(payload.get("question", "")).strip()
    if not question:
        return {"ok": False, "error": "質問本文が空です。"}
    mid = memory_manager.add_mailbag(question=question, sender=sender, source="web")
    items = memory_manager.list_mailbag(limit=50)
    await orchestrator.broadcast({"type": "mailbag_updated", "items": items})
    return {"ok": True, "id": mid, "items": items}


@app.post("/api/vtuber/mailbag/read_next")
async def read_next_mailbag_api() -> dict[str, Any]:
    """未読のお便りを1通取り出し、画面カード表示＆ニアの読み上げ回答を開始する。"""
    item = memory_manager.pop_next_unread_mailbag()
    if not item:
        return {"ok": False, "error": "未読のお便りがありません。"}
    items = memory_manager.list_mailbag(limit=50)
    memory_manager.add_stream_highlight("mailbag", f"お便り: {item['sender']}", item["question"][:80])
    await orchestrator.broadcast({"type": "mailbag_overlay", "item": item})
    await orchestrator.broadcast({"type": "mailbag_updated", "items": items})
    await orchestrator.enqueue_event(
        AgentEvent(
            source=EventSource.SILENCE_BREAKER,
            user="MailbagCorner",
            content=(
                f"【匿名お便り・質問箱コーナー】\n"
                f"投稿者: {item['sender']}\n"
                f"内容: 「{item['question']}」\n"
                "※まずお便りを紹介し、配信者らしくウィットに富んだ回答・ツッコミで答えてください。"
            ),
            priority=2,
            metadata={"discord_category": "autonomous"},
        )
    )
    return {"ok": True, "item": item}


@app.delete("/api/vtuber/mailbag/{item_id}")
async def delete_mailbag_api(item_id: int) -> dict[str, Any]:
    """指定IDのお便りを削除する。"""
    memory_manager.delete_mailbag(item_id)
    items = memory_manager.list_mailbag(limit=50)
    await orchestrator.broadcast({"type": "mailbag_updated", "items": items})
    return {"ok": True, "items": items}


@app.post("/api/vtuber/session/start")
async def start_stream_session_api(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """配信セッション（チャプター・ハイライト自動記録）を開始する。"""
    title = str(payload.get("title", "")).strip() or config.stream_topic or "ニアのライブ配信"
    sid = memory_manager.start_stream_session(title=title)
    session = memory_manager.get_active_stream_session()
    await orchestrator.broadcast(
        {
            "type": "stream_session_updated",
            "active_session": session,
            "sessions": memory_manager.list_stream_sessions(limit=8),
        }
    )
    return {"ok": True, "session_id": sid, "session": session}


@app.post("/api/vtuber/session/end")
async def end_stream_session_api() -> dict[str, Any]:
    """配信セッションを終了し、YouTubeチャプター・アーカイブ要約・X告知ポスト案を自動生成する。"""
    session = memory_manager.get_active_stream_session()
    if not session:
        return {"ok": False, "error": "現在記録中の配信セッションがありません。"}
    highlights = memory_manager.get_stream_highlights(int(session["id"]))
    artifacts = build_stream_archive_artifacts(session, highlights)
    sessions = memory_manager.list_stream_sessions(limit=8)
    await orchestrator.broadcast(
        {
            "type": "stream_session_updated",
            "active_session": None,
            "sessions": sessions,
            "archive_result": artifacts,
        }
    )
    if config.discord_enabled:
        await orchestrator.discord_connector.send_proactive_dm(
            f"📺 **配信終了レポート**\n保存先: `{artifacts['archive_path']}`\n\n**⏱️ チャプター案**\n```text\n{artifacts['chapters_text'][:900]}\n```\n\n**🐦 X告知案**\n{artifacts['x_post']}"
        )
    return {"ok": True, **artifacts}


@app.get("/api/vtuber/karaoke/tracks")
async def list_karaoke_tracks_api() -> dict[str, Any]:
    """利用可能な歌枠トラック（音声＋LRC歌詞）一覧を返す。"""
    return {"ok": True, "tracks": karaoke_manager.list_tracks()}


@app.get("/api/vtuber/karaoke_audio/{filename}")
async def serve_karaoke_audio(filename: str):
    """歌枠フォルダ内の音声ファイルを配信する。"""
    safe_name = os.path.basename(filename)
    fpath = os.path.join(KARAOKE_DIR, safe_name)
    if not os.path.exists(fpath):
        return {"error": "Karaoke audio not found"}
    media_type = "audio/mpeg" if safe_name.endswith(".mp3") else "audio/wav"
    return FileResponse(fpath, media_type=media_type)


@app.post("/api/vtuber/karaoke/play")
async def play_karaoke_track_api(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """指定歌枠トラックの再生指示（歌詞テロップ同期付き）を全クライアントへブロードキャストする。"""
    track_id = str(payload.get("track_id", "")).strip() or "nia_stage_demo"
    tracks = karaoke_manager.list_tracks()
    target = next((t for t in tracks if t["id"] == track_id), tracks[0] if tracks else None)
    if not target:
        return {"ok": False, "error": "楽曲が見つかりません。"}
    memory_manager.add_stream_highlight("karaoke", f"歌唱: {target['title']}")
    if config.obs_ws_enabled:
        asyncio.create_task(obs_ws_client.switch_scene("karaoke"))
    await orchestrator.broadcast({"type": "karaoke_play", "track": target})
    return {"ok": True, "track": target}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """リアルタイム双方向通信用WebSocketエンドポイント。"""
    await orchestrator.connect_ws(ws)
    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "chat")
            if msg_type == "chat":
                src_raw = data.get("source", "user_text")
                try:
                    src = EventSource(src_raw)
                except ValueError:
                    src = EventSource.USER_TEXT
                event = AgentEvent(
                    source=src,
                    user=data.get("user", "Master"),
                    content=data.get("content", ""),
                    metadata=data.get("metadata", {}),
                )
                await orchestrator.enqueue_event(event)
            elif msg_type == "trigger_proactive":
                prompt = data.get(
                    "prompt",
                    "現在のシステム状態と未完了タスクを確認し、マスターへ簡潔な戦術ブリーフィングを行ってください。",
                )
                await orchestrator.enqueue_event(
                    AgentEvent(
                        source=EventSource.PROACTIVE,
                        user="TacticalTrigger",
                        content=prompt,
                        priority=2,
                        metadata={"discord_category": "alert"},
                    )
                )
    except WebSocketDisconnect:
        orchestrator.disconnect_ws(ws)
    except Exception:
        orchestrator.disconnect_ws(ws)


# フロントエンド静的ファイルのマウント
app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIR, "assets")), name="assets")
app.mount("/css", StaticFiles(directory=os.path.join(FRONTEND_DIR, "css")), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(FRONTEND_DIR, "js")), name="js")


@app.get("/")
async def serve_index():
    """メインUI（および ?mode=obs / ?mode=mascot）のHTMLを返す。"""
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
