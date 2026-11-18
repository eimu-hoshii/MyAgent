"""
感情タグ連動・詳細パラメーター（話速・ピッチ・音量・抑揚・話者ID）調整・読み上げ辞書対応のマルチ音声合成（TTS）エンジン。
Edge-TTS、VOICEVOX、AivisSpeech に対応する。
"""

import os
import time
import uuid
from typing import Optional
import httpx
import edge_tts
from backend.config import AUDIO_CACHE_DIR, EMOTION_TTS_OFFSETS, config
from backend.guardrail import apply_pronunciation_dict


class TTSEngine:
    """テキストと感情タグから音声ファイルを生成し、配信用URLを返すクラス。"""

    def __init__(self) -> None:
        """音声キャッシュディレクトリを初期化する。"""
        os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

    def cleanup_old_cache(self, max_age_sec: int | None = None, max_files: int | None = None) -> int:
        """
        音声キャッシュを自動クリーンアップする。
        - 設定時間（audio_cache_max_age_min）を超えた古いファイルを削除
        - 設定件数（audio_cache_max_files）を超えた分を古い順に削除
        削除件数を返す。
        """
        age_limit = max_age_sec if max_age_sec is not None else int(config.audio_cache_max_age_min) * 60
        count_limit = max_files if max_files is not None else int(config.audio_cache_max_files)
        now = time.time()
        removed = 0
        try:
            entries = []
            for fname in os.listdir(AUDIO_CACHE_DIR):
                fpath = os.path.join(AUDIO_CACHE_DIR, fname)
                if os.path.isfile(fpath):
                    entries.append((os.path.getmtime(fpath), fpath))
            entries.sort(reverse=True)  # 新しい順
            for idx, (mtime, fpath) in enumerate(entries):
                if (now - mtime) > age_limit or idx >= count_limit:
                    try:
                        os.remove(fpath)
                        removed += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return removed

    def get_cache_stats(self) -> dict:
        """音声キャッシュの件数と合計サイズ(MB)を返す。"""
        count = 0
        total = 0
        try:
            for fname in os.listdir(AUDIO_CACHE_DIR):
                fpath = os.path.join(AUDIO_CACHE_DIR, fname)
                if os.path.isfile(fpath):
                    count += 1
                    total += os.path.getsize(fpath)
        except Exception:
            pass
        return {"files": count, "size_mb": round(total / (1024 * 1024), 2)}

    async def synthesize(self, text: str, emotion: str = "normal") -> Optional[str]:
        """
        セリフと感情タグから読み上げ辞書と詳細ボイス設定を反映した音声ファイルを生成し、
        Webアクセス用の相対パス (`/api/audio/...`) を返す。
        """
        if not text.strip():
            return None

        self.cleanup_old_cache()
        # 読み上げ辞書（固有名詞 → カタカナ読み）を適用
        spoken_text = apply_pronunciation_dict(text)
        provider = config.tts_provider.lower()

        if provider in ("voicevox", "aivisspeech"):
            base_url = (
                config.voicevox_base_url if provider == "voicevox" else config.aivisspeech_base_url
            )
            speaker_id = (
                config.voicevox_speaker_id if provider == "voicevox" else config.aivisspeech_speaker_id
            )
            wav_url = await self._synthesize_voicevox_compatible(
                spoken_text, emotion, base_url, speaker_id, is_aivis=(provider == "aivisspeech")
            )
            if wav_url:
                return wav_url

        if provider != "browser":
            mp3_url = await self._synthesize_edge_tts(spoken_text, emotion)
            if mp3_url:
                return mp3_url

        return None

    async def _synthesize_edge_tts(self, text: str, emotion: str) -> Optional[str]:
        """Edge-TTS を用いて感情オフセット＋ユーザー設定（話速・ピッチ・音量・声種）で MP3 音声を生成する。"""
        try:
            offset = EMOTION_TTS_OFFSETS.get(emotion, EMOTION_TTS_OFFSETS["normal"])
            base_rate_pct = int(round((config.tts_speed - 1.0) * 100)) + offset["rate_pct"]
            base_pitch_hz = int(round((config.tts_pitch - 1.0) * 50)) + offset["pitch_hz"]
            base_vol_pct = int(round((config.tts_volume - 1.0) * 100))

            rate_str = f"{base_rate_pct:+d}%"
            pitch_str = f"{base_pitch_hz:+d}Hz"
            vol_str = f"{base_vol_pct:+d}%"

            filename = f"speech_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}.mp3"
            filepath = os.path.join(AUDIO_CACHE_DIR, filename)

            communicate = edge_tts.Communicate(
                text=text,
                voice=config.edge_tts_voice or "ja-JP-NanamiNeural",
                rate=rate_str,
                pitch=pitch_str,
                volume=vol_str,
            )
            await communicate.save(filepath)
            if os.path.exists(filepath) and os.path.getsize(filepath) > 100:
                return f"/api/audio/{filename}"
        except Exception as exc:
            print(f"[TTSEngine] Edge-TTS 生成エラー: {exc}")
        return None

    async def _synthesize_voicevox_compatible(
        self, text: str, emotion: str, base_url: str, speaker_id: int, is_aivis: bool = False
    ) -> Optional[str]:
        """VOICEVOX または AivisSpeech エンジンで詳細パラメータを適用して WAV 音声を生成する。"""
        try:
            offset = EMOTION_TTS_OFFSETS.get(emotion, EMOTION_TTS_OFFSETS["normal"])
            async with httpx.AsyncClient(timeout=10.0) as client:
                q_resp = await client.post(
                    f"{base_url}/audio_query",
                    params={"text": text, "speaker": speaker_id},
                )
                if q_resp.status_code != 200:
                    return None
                query_data = q_resp.json()

                if is_aivis:
                    # AivisSpeech (Style-Bert-VITS2系) 音割れ防止チューニング:
                    # 1. pitchScale が 0.0 以外だと後処理ピッチシフトによる位相歪み・音割れが起きるため、
                    #    ユーザーが明示的にピッチを変更していない限り 0.0 に固定する。
                    user_pitch_diff = abs(config.tts_pitch - 1.0)
                    query_data["pitchScale"] = (
                        round((config.tts_pitch - 1.0) * 0.12, 3) if user_pitch_diff >= 0.03 else 0.0
                    )
                    # 2. AivisSpeech は元の音圧が 0dBFS ギリギリのため、0.82倍(-1.8dB)のヘッドルームを確保してクリッピングを防ぐ。
                    query_data["volumeScale"] = round(config.tts_volume * 0.82, 2)
                    # 3. 抑揚(intonationScale)が1.0を超えるとピークで割れやすいため1.0基準に抑える。
                    query_data["intonationScale"] = round(min(1.05, config.tts_intonation * 0.92), 2)
                    query_data["speedScale"] = round(
                        config.tts_speed * (1.0 + offset["rate_pct"] / 220.0), 2
                    )
                else:
                    # VOICEVOX 用のパラメータ適用
                    query_data["speedScale"] = round(
                        config.tts_speed * (1.0 + offset["rate_pct"] / 150.0), 2
                    )
                    query_data["pitchScale"] = round(
                        (config.tts_pitch - 1.0) * 0.15 + (offset["pitch_hz"] * 0.002), 3
                    )
                    query_data["intonationScale"] = round(config.tts_intonation, 2)
                    query_data["volumeScale"] = round(config.tts_volume, 2)

                s_resp = await client.post(
                    f"{base_url}/synthesis",
                    params={"speaker": speaker_id},
                    json=query_data,
                )
                if s_resp.status_code != 200:
                    return None

                filename = f"speech_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}.wav"
                filepath = os.path.join(AUDIO_CACHE_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(s_resp.content)
                return f"/api/audio/{filename}"
        except Exception:
            return None


tts_engine = TTSEngine()
