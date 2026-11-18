"""
自律型VTuber ライブ配信スタジオ拡張エンジン。
- OBS Studio WebSocket v5 クライアント（シーン切替・テキスト更新・演出ソース発火）
- VTube Studio WebSocket API クライアント（認証・MouthOpen/表情パラメータ制御）
- ニュース＆トピック引用オーバーレイカードビルダー
- 視聴者参加型 投票 / クイズ / 大喜利 ステートマシン
- 配信ウィンドウ専用 画面変化検知実況ウォッチャー
- 配信アーカイブ・タイムスタンプチャプター・X告知案自動生成
- 歌枠プレイヤー＆LRC歌詞タイムライン管理（テストデモ曲自動生成対応）
"""

import asyncio
import base64
import datetime
import hashlib
import io
import json
import math
import os
import re
import time
import wave
from typing import Any
from PIL import Image, ImageChops
from backend.config import KARAOKE_DIR, STREAM_ARCHIVE_DIR, config
from backend.memory import memory_manager


class OBSWebSocketClient:
    """obs-websocket v5 プロトコル (websockets利用) による OBS 自律制御クライアント。"""

    def __init__(self) -> None:
        self._last_error: str = ""
        self._last_scene: str = ""

    @staticmethod
    def _build_auth(password: str, salt: str, challenge: str) -> str:
        secret_hash = hashlib.sha256((password + salt).encode("utf-8")).digest()
        secret_b64 = base64.b64encode(secret_hash).decode("ascii")
        auth_hash = hashlib.sha256((secret_b64 + challenge).encode("utf-8")).digest()
        return base64.b64encode(auth_hash).decode("ascii")

    async def _request(self, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """OBS WebSocket v5 へ単発RPCリクエストを送り、応答辞書を返す。"""
        if not config.obs_ws_enabled:
            return {"ok": False, "error": "OBS WebSocket 連携は設定で無効化されています。"}
        url = f"ws://{config.obs_ws_host or '127.0.0.1'}:{int(config.obs_ws_port or 4455)}"
        try:
            import websockets

            async with websockets.connect(url, open_timeout=3.0, close_timeout=2.0) as ws:
                hello_raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                hello = json.loads(hello_raw)
                auth_info = hello.get("d", {}).get("authentication")
                identify_d: dict[str, Any] = {"rpcVersion": 1}
                if auth_info and config.obs_ws_password:
                    identify_d["authentication"] = self._build_auth(
                        config.obs_ws_password,
                        auth_info.get("salt", ""),
                        auth_info.get("challenge", ""),
                    )
                await ws.send(json.dumps({"op": 1, "d": identify_d}))
                ident_raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                ident = json.loads(ident_raw)
                if ident.get("op") != 2:
                    return {"ok": False, "error": "OBS 認証またはハンドシェイクに失敗しました（パスワードを確認してください）。"}

                req_id = f"nia_{int(time.time()*1000)}"
                await ws.send(
                    json.dumps(
                        {
                            "op": 6,
                            "d": {
                                "requestType": request_type,
                                "requestId": req_id,
                                "requestData": request_data or {},
                            },
                        }
                    )
                )
                for _ in range(8):
                    resp_raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    resp = json.loads(resp_raw)
                    if resp.get("op") == 7 and resp.get("d", {}).get("requestId") == req_id:
                        status = resp["d"].get("requestStatus", {})
                        if status.get("result"):
                            self._last_error = ""
                            return {"ok": True, "data": resp["d"].get("responseData", {})}
                        err = status.get("comment") or f"code={status.get('code')}"
                        self._last_error = err
                        return {"ok": False, "error": err}
                return {"ok": False, "error": "OBS 応答タイムアウト"}
        except Exception as exc:
            self._last_error = str(exc)[:120]
            return {"ok": False, "error": f"OBS接続失敗 ({url}): {self._last_error}"}

    async def test_connection(self) -> dict[str, Any]:
        """OBSへの接続およびシーン一覧取得をテストする。"""
        res = await self._request("GetSceneList")
        if res.get("ok"):
            scenes = [s.get("sceneName") for s in res.get("data", {}).get("scenes", []) if s.get("sceneName")]
            return {"ok": True, "scenes": scenes, "current": res.get("data", {}).get("currentProgramSceneName", "")}
        return res

    async def switch_scene(self, scene_key_or_name: str) -> dict[str, Any]:
        """設定されたシーンマップ (talk/news/game/karaoke/brb) または直接シーン名へ切り替える。"""
        mapped = (config.obs_scene_map or {}).get(scene_key_or_name, scene_key_or_name)
        res = await self._request("SetCurrentProgramScene", {"sceneName": mapped})
        if res.get("ok"):
            self._last_scene = mapped
        return res


class VTubeStudioClient:
    """VTube Studio Public API (ポート8001) WebSocket クライアント。"""

    def __init__(self) -> None:
        self._last_error: str = ""

    async def _connect_and_auth(self, ws: Any) -> bool:
        """認証トークン取得または既存トークンで認証を行う。"""
        if not config.vts_auth_token:
            await ws.send(
                json.dumps(
                    {
                        "apiName": "VTubeStudioPublicAPI",
                        "apiVersion": "1.0",
                        "requestID": "token_req",
                        "messageType": "AuthenticationTokenRequest",
                        "data": {
                            "pluginName": "NIA Autonomous VTuber",
                            "pluginDeveloper": "MyAgent",
                        },
                    }
                )
            )
            tok_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15.0))
            token = tok_resp.get("data", {}).get("authenticationToken", "")
            if not token:
                self._last_error = tok_resp.get("data", {}).get("message", "トークン拒否")
                return False
            config.vts_auth_token = token
            config.save_secrets()

        await ws.send(
            json.dumps(
                {
                    "apiName": "VTubeStudioPublicAPI",
                    "apiVersion": "1.0",
                    "requestID": "auth_req",
                    "messageType": "AuthenticationRequest",
                    "data": {
                        "pluginName": "NIA Autonomous VTuber",
                        "pluginDeveloper": "MyAgent",
                        "authenticationToken": config.vts_auth_token,
                    },
                }
            )
        )
        auth_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
        ok = bool(auth_resp.get("data", {}).get("authenticated"))
        if not ok:
            self._last_error = auth_resp.get("data", {}).get("reason", "認証失敗")
            config.vts_auth_token = ""
        return ok

    async def test_connection(self) -> dict[str, Any]:
        """VTube Studio への接続とプラグイン認証をテストする。"""
        if not config.vts_enabled:
            return {"ok": False, "error": "VTube Studio 連携は設定で無効化されています。"}
        url = config.vts_ws_url or "ws://127.0.0.1:8001"
        try:
            import websockets

            async with websockets.connect(url, open_timeout=3.0) as ws:
                authed = await self._connect_and_auth(ws)
                if not authed:
                    return {"ok": False, "error": self._last_error or "VTS認証失敗"}
                await ws.send(
                    json.dumps(
                        {
                            "apiName": "VTubeStudioPublicAPI",
                            "apiVersion": "1.0",
                            "requestID": "hotkeys",
                            "messageType": "HotkeysInCurrentModelRequest",
                        }
                    )
                )
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                hotkeys = [h.get("name") for h in resp.get("data", {}).get("availableHotkeys", [])]
                return {"ok": True, "hotkeys": hotkeys[:20]}
        except Exception as exc:
            return {"ok": False, "error": f"VTube Studio 接続エラー ({url}): {exc}"}

    async def inject_emotion_expression(self, emotion: str) -> bool:
        """発話感情に合わせて VTube Studio のパラメータ／微笑み等を送信する。"""
        if not config.vts_enabled or not config.vts_auth_token:
            return False
        url = config.vts_ws_url or "ws://127.0.0.1:8001"
        smile_val = 0.9 if emotion in ("smile", "joy") else (0.15 if emotion == "normal" else 0.0)
        angle_z = 6.0 if emotion == "smile" else (-6.0 if emotion == "troubled" else 0.0)
        try:
            import websockets

            async with websockets.connect(url, open_timeout=2.0) as ws:
                if not await self._connect_and_auth(ws):
                    return False
                await ws.send(
                    json.dumps(
                        {
                            "apiName": "VTubeStudioPublicAPI",
                            "apiVersion": "1.0",
                            "requestID": "param_inj",
                            "messageType": "InjectParameterDataRequest",
                            "data": {
                                "faceFound": True,
                                "mode": "set",
                                "parameterValues": [
                                    {"id": "MouthSmile", "value": smile_val},
                                    {"id": "FaceAngleZ", "value": angle_z},
                                ],
                            },
                        }
                    )
                )
                await asyncio.wait_for(ws.recv(), timeout=2.0)
                return True
        except Exception:
            return False


class InteractiveGameEngine:
    """
    視聴者参加型の投票（!1〜!4）・クイズ・大喜利を管理する確定ステートマシン。
    """

    def __init__(self) -> None:
        self.active: bool = False
        self.game_type: str = "poll"  # "poll" | "quiz" | "ogiri"
        self.title: str = ""
        self.options: list[str] = []
        self.votes: dict[str, int] = {}
        self.submissions: list[dict[str, str]] = []
        self.ends_at: float = 0.0
        self.answer_hint: str = ""

    def start_game(
        self,
        game_type: str,
        title: str,
        options: list[str] | None = None,
        duration_sec: int = 45,
        answer_hint: str = "",
    ) -> dict[str, Any]:
        """新しい投票または参加型お題ゲームを開始する。"""
        self.active = True
        self.game_type = game_type if game_type in ("poll", "quiz", "ogiri") else "poll"
        self.title = (title or "視聴者アンケート").strip()[:80]
        clean_opts = [str(o).strip()[:40] for o in (options or []) if str(o).strip()][:4]
        if self.game_type == "poll" and len(clean_opts) < 2:
            clean_opts = ["賛成 / あり！", "反対 / なし！"]
        self.options = clean_opts
        self.votes = {}
        self.submissions = []
        self.answer_hint = (answer_hint or "").strip()
        self.ends_at = time.time() + max(10, min(300, int(duration_sec)))
        return self.get_state()

    def process_viewer_comment(self, user_name: str, text: str, platform: str = "local") -> tuple[bool, bool]:
        """
        コメントが進行中の投票・企画に該当するか判定し集計する。
        戻り値: (投票等が更新されたか, 通常チャットキューへの流入を消費しスキップするか)
        """
        if not self.active:
            return False, False

        s = (text or "").strip()
        if self.game_type == "poll" and self.options:
            m = re.match(r"^[!！]?([1-4])$", s)
            if m:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(self.options):
                    self.votes[user_name] = idx
                    return True, True
            return False, False

        # quiz / ogiri: !回答 テキスト または通常コメントとして収集（消費せず通常反応も可能に）
        if len(s) >= 2 and len(self.submissions) < 20:
            clean_ans = re.sub(r"^[!！](?:a|ans|回答|answer)\s*", "", s, flags=re.IGNORECASE).strip()
            if clean_ans:
                self.submissions.append({"user": user_name, "text": clean_ans[:100]})
                return True, s.startswith(("!a ", "!ans ", "!回答 "))
        return False, False

    def get_state(self) -> dict[str, Any]:
        """OBSオーバーレイ＆ディレクターUIへ配信する現在状態辞書を返す。"""
        remain = max(0, int(self.ends_at - time.time())) if self.active else 0
        counts = [0] * len(self.options)
        for choice in self.votes.values():
            if 0 <= choice < len(counts):
                counts[choice] += 1
        total_votes = sum(counts)
        option_rows = [
            {
                "index": i + 1,
                "label": opt,
                "count": counts[i],
                "percent": round((counts[i] / total_votes) * 100.0, 1) if total_votes > 0 else 0.0,
            }
            for i, opt in enumerate(self.options)
        ]
        return {
            "active": self.active,
            "game_type": self.game_type,
            "title": self.title,
            "options": option_rows,
            "total_votes": total_votes,
            "submissions": self.submissions[-8:],
            "submission_count": len(self.submissions),
            "remaining_sec": remain,
        }

    def finish_game(self) -> dict[str, Any]:
        """集計を確定終了し、NIAが読み上げる結果プロンプトを返す。"""
        state = self.get_state()
        self.active = False
        if state["game_type"] == "poll":
            rows = sorted(state["options"], key=lambda x: x["count"], reverse=True)
            lines = [f"{r['index']}番「{r['label']}」: {r['count']}票 ({r['percent']}%)" for r in state["options"]]
            winner = rows[0]["label"] if rows else ""
            prompt = (
                f"【視聴者投票 締切＆結果発表】お題: 「{state['title']}」\n"
                f"合計投票数: {state['total_votes']}票\n"
                + "\n".join(lines)
                + f"\n※1位は「{winner}」でした！リスナーへの感謝と結果へのコメント・ツッコミを配信者らしく楽しく喋ってください。"
            )
        else:
            subs = "\n".join(f"- {s['user']}: {s['text']}" for s in self.submissions[:10]) or "（回答なし）"
            hint_part = f"\n想定正解/ヒント: {self.answer_hint}" if self.answer_hint else ""
            prompt = (
                f"【視聴者参加型「{state['game_type'].upper()}」結果発表】お題: 「{state['title']}」{hint_part}\n"
                f"集まった回答（{len(self.submissions)}件）:\n{subs}\n"
                "※面白かった回答や正解者にツッコミを入れながら結果を発表して盛り上げてください！"
            )
        return {"state": state, "prompt": prompt}


class ScreenCommentator:
    """配信ウィンドウの画面変化を検知し、安全な画面実況イベントを発火させる判定クラス。"""

    def __init__(self) -> None:
        self._last_thumb: Image.Image | None = None
        self._last_commentary_at: float = 0.0

    def is_window_safe_for_stream(self, window_title: str) -> bool:
        """設定された許可ウィンドウフィルタに合致する場合のみ配信画面キャプチャを許可する。"""
        filt = (config.vtuber_watch_window_filter or "").strip()
        if not filt:
            return True
        return filt.lower() in (window_title or "").lower()

    def evaluate_change(self, base64_jpeg: str) -> float:
        """前回キャプチャとのサムネイル平均輝度差分 (0.0〜255.0) を算出する。"""
        try:
            raw = base64.b64decode(base64_jpeg)
            img = Image.open(io.BytesIO(raw)).convert("L").resize((64, 36))
            if self._last_thumb is None:
                self._last_thumb = img
                return 25.0
            diff = ImageChops.difference(self._last_thumb, img)
            pixels = list(diff.getdata())
            mean_diff = sum(pixels) / max(1, len(pixels))
            self._last_thumb = img
            return float(mean_diff)
        except Exception:
            return 0.0


class KaraokeManager:
    """歌枠用の音源フォルダスキャン・LRC歌詞パース・内蔵デモ曲生成を行うクラス。"""

    def __init__(self) -> None:
        os.makedirs(KARAOKE_DIR, exist_ok=True)

    @staticmethod
    def _parse_lrc(lrc_text: str) -> list[dict[str, Any]]:
        """[mm:ss.xx] 形式のLRCテキストをタイムスタンプ配列へ変換する。"""
        rows: list[dict[str, Any]] = []
        for raw_line in lrc_text.splitlines():
            m = re.match(r"^\s*\[(\d{1,2}):(\d{2}(?:\.\d+)?)\]\s*(.+)$", raw_line)
            if not m:
                continue
            sec = int(m.group(1)) * 60.0 + float(m.group(2))
            rows.append({"time_sec": round(sec, 2), "text": m.group(3).strip()})
        rows.sort(key=lambda x: x["time_sec"])
        return rows

    def ensure_demo_track(self) -> str:
        """事前準備なしで歌枠をすぐ試せる 7秒間の軽量シンセメロディデモ曲(.wav + .lrc)を生成する。"""
        os.makedirs(KARAOKE_DIR, exist_ok=True)
        wav_path = os.path.join(KARAOKE_DIR, "nia_stage_demo.wav")
        lrc_path = os.path.join(KARAOKE_DIR, "nia_stage_demo.lrc")
        if not os.path.exists(wav_path):
            sr = 22050
            # C5 - E5 - G5 - A5 - G5 - E5 - D5 - C5 の明るいチップチューン調デモメロディ
            melody = [
                (523.25, 0.7),
                (659.25, 0.7),
                (783.99, 0.8),
                (880.00, 0.8),
                (783.99, 0.7),
                (659.25, 0.7),
                (587.33, 0.8),
                (523.25, 1.3),
            ]
            frames = bytearray()
            for freq, dur in melody:
                n_samples = int(sr * dur)
                for i in range(n_samples):
                    t = i / sr
                    env = min(1.0, i / (sr * 0.03)) * min(1.0, (n_samples - i) / (sr * 0.06))
                    # 基音＋2倍音＋和音ベースで心地よいシンセ音を作る
                    val = (
                        0.55 * math.sin(2 * math.pi * freq * t)
                        + 0.20 * math.sin(2 * math.pi * freq * 2.0 * t)
                        + 0.20 * math.sin(2 * math.pi * (freq * 0.5) * t)
                    ) * env
                    sample_i16 = max(-32767, min(32767, int(val * 12000)))
                    frames += sample_i16.to_bytes(2, byteorder="little", signed=True)
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(bytes(frames))

        if not os.path.exists(lrc_path):
            lrc_body = (
                "[00:00.10] ♪ ようこそ！ ニアのライブステージへ ♪\n"
                "[00:01.60] ツインテール揺らして コードを奏でよう\n"
                "[00:03.20] コメントもスパチャも 全力で受け止めるから\n"
                "[00:05.00] みんなで一緒に 最高の夜にしよう！ ♪\n"
            )
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(lrc_body)
        return "nia_stage_demo"

    def list_tracks(self) -> list[dict[str, Any]]:
        """data/karaoke 内の音声ファイル一覧と対応LRC歌詞を返す。"""
        self.ensure_demo_track()
        tracks: list[dict[str, Any]] = []
        for fn in sorted(os.listdir(KARAOKE_DIR)):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".wav", ".mp3", ".ogg", ".m4a"):
                continue
            stem = os.path.splitext(fn)[0]
            lrc_path = os.path.join(KARAOKE_DIR, f"{stem}.lrc")
            lyrics: list[dict[str, Any]] = []
            if os.path.exists(lrc_path):
                try:
                    with open(lrc_path, "r", encoding="utf-8") as f:
                        lyrics = self._parse_lrc(f.read())
                except Exception:
                    pass
            tracks.append(
                {
                    "id": stem,
                    "title": "🌟 ニア・テストライブデモ (標準搭載)" if stem == "nia_stage_demo" else stem,
                    "audio_url": f"/api/vtuber/karaoke_audio/{fn}",
                    "has_lyrics": len(lyrics) > 0,
                    "lyrics": lyrics,
                }
            )
        return tracks


def extract_news_cards_from_web_search(observation: str, max_cards: int = 3) -> list[dict[str, str]]:
    """web_search の生出力から VTuber 配信オーバーレイ表示用のクリーンなニュースカードを抽出する。"""
    cards: list[dict[str, str]] = []
    for line in (observation or "").splitlines():
        s = line.strip()
        m_news = re.match(r"^\[最新ニュース \(([^)]+)\)\]\s+(.+?)(?:\s+\((https?://[^\s)]+)\))?$", s)
        if m_news:
            dt_str, title, url = m_news.groups()
            cards.append(
                {
                    "tag": f"NEWS · {dt_str}",
                    "title": re.sub(r"\s+", " ", title).strip()[:75],
                    "url": url or "",
                }
            )
        else:
            m_web = re.match(r"^\[Web:\s*(.+?)\]\s+\((https?://[^\s)]+)\)", s)
            if m_web:
                title, url = m_web.groups()
                cards.append(
                    {
                        "tag": "WEB SOURCE",
                        "title": re.sub(r"\s+", " ", title).strip()[:75],
                        "url": url,
                    }
                )
        if len(cards) >= max_cards:
            break
    return cards


def build_stream_archive_artifacts(session: dict[str, Any], highlights: list[dict[str, Any]]) -> dict[str, str]:
    """配信セッションのハイライトからチャプター・アーカイブ要約・X告知案を生成しファイル保存する。"""
    os.makedirs(STREAM_ARCHIVE_DIR, exist_ok=True)
    started_dt = datetime.datetime.fromtimestamp(float(session["started_at"]))
    ended_dt = datetime.datetime.now()
    duration_min = max(1, int((time.time() - float(session["started_at"])) / 60.0))

    def _fmt_ts(sec: float) -> str:
        s = max(0, int(sec))
        hh, rem = divmod(s, 3600)
        mm, ss = divmod(rem, 60)
        return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh > 0 else f"{mm:02d}:{ss:02d}"

    chapter_lines = ["00:00 配信開始"]
    sc_count = 0
    comment_count = 0
    topics: list[str] = []
    for h in highlights:
        kind = h.get("event_kind", "")
        ts = _fmt_ts(float(h.get("elapsed_sec", 0)))
        lbl = h.get("label", "")
        if kind == "superchat":
            sc_count += 1
            chapter_lines.append(f"{ts} 💎 スパチャ: {lbl}")
        elif kind in ("topic", "poll", "mailbag", "karaoke"):
            chapter_lines.append(f"{ts} {lbl}")
            if lbl not in topics:
                topics.append(lbl)
        elif kind == "comment":
            comment_count += 1

    chapters_text = "\n".join(chapter_lines[:30])
    topic_summary = " / ".join(topics[:5]) if topics else "フリートーク・リスナー交流"
    x_post = (
        f"『{session['title']}』配信ありがとう！（{duration_min}分間）\n"
        f"今日の話題: {topic_summary}\n"
        f"スパチャ {sc_count}件・コメントいっぱい嬉しかったわ！また次回の配信で会いなさいよね！ #NIA配信"
    )
    summary_md = (
        f"# 📺 配信アーカイブレポート: {session['title']}\n\n"
        f"- 開始: {started_dt.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 終了: {ended_dt.strftime('%Y-%m-%d %H:%M:%S')} (約{duration_min}分)\n"
        f"- ハイライト数: {len(highlights)} / スパチャ回数: {sc_count} / 記録コメントイベント: {comment_count}\n\n"
        f"## ⏱️ YouTube 概要欄用チャプター\n```text\n{chapters_text}\n```\n\n"
        f"## 🐦 X（旧Twitter）お礼ポスト案\n```text\n{x_post}\n```\n"
    )

    fname = f"archive_{started_dt.strftime('%Y%m%d_%H%M%S')}_id{session['id']}.md"
    fpath = os.path.join(STREAM_ARCHIVE_DIR, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(summary_md)

    memory_manager.end_stream_session(
        session_id=int(session["id"]),
        summary_md=summary_md,
        chapters_text=chapters_text,
        archive_path=fpath,
    )
    return {
        "chapters_text": chapters_text,
        "x_post": x_post,
        "summary_md": summary_md,
        "archive_path": fpath,
    }


obs_ws_client = OBSWebSocketClient()
vts_client = VTubeStudioClient()
interactive_game = InteractiveGameEngine()
screen_commentator = ScreenCommentator()
karaoke_manager = KaraokeManager()
