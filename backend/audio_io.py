"""
ローカル音声認識（faster-whisper + webrtcvad + sounddevice）統合モジュール。
- 音声ファイル（Discordボイスメッセージ .ogg / 録音 .mp3 / .wav / .m4a）のローカル文字起こし
- PCマイク常時待機（VAD自動発話区間検出 + ウェイクワード / VTuber相方コラボ入力）
"""

import asyncio
import io
import os
import subprocess
import tempfile
import threading
import time
import wave
from typing import Any, Callable, Coroutine
import numpy as np
from backend.config import config


def _get_ffmpeg_exe() -> str:
    """環境に同梱またはインストール済みの ffmpeg 実行ファイルパスを返す。"""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return "ffmpeg"


class WhisperTranscriber:
    """faster-whisper モデルを遅延ロードして共有利用する文字起こしクラス。"""

    def __init__(self) -> None:
        self._model: Any = None
        self._loaded_size: str = ""
        self._lock = threading.Lock()

    def _ensure_model(self) -> Any:
        """Ollama(GPU)とのVRAM競合を避けるため CPU int8 モードでモデルを安全にロードする。"""
        target_size = (config.whisper_model_size or "tiny").strip()
        with self._lock:
            if self._model is not None and self._loaded_size == target_size:
                return self._model
            from faster_whisper import WhisperModel

            self._model = WhisperModel(target_size, device="cpu", compute_type="int8")
            self._loaded_size = target_size
            return self._model

    def transcribe_file_sync(self, file_path: str, language: str = "ja") -> str:
        """音声/動画ファイルを文字起こししてテキストを返す（同期版）。"""
        if not os.path.exists(file_path):
            return ""
        model = self._ensure_model()
        wav_tmp: str | None = None
        target_path = file_path
        ext = os.path.splitext(file_path)[1].lower()

        # .ogg / .m4a / .webm 等でデコードが不安定な場合に備え ffmpeg で 16kHz WAV 変換をフォールバック用意
        try:
            segments, _info = model.transcribe(
                target_path,
                language=language,
                beam_size=3,
                vad_filter=True,
            )
            texts = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
            return " ".join(texts).strip()
        except Exception:
            if ext != ".wav":
                try:
                    fd, wav_tmp = tempfile.mkstemp(suffix=".wav")
                    os.close(fd)
                    ffmpeg_exe = _get_ffmpeg_exe()
                    subprocess.run(
                        [ffmpeg_exe, "-y", "-i", file_path, "-ar", "16000", "-ac", "1", wav_tmp],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=30,
                        check=True,
                    )
                    segments, _info = model.transcribe(wav_tmp, language=language, beam_size=3)
                    texts = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
                    return " ".join(texts).strip()
                except Exception:
                    return ""
                finally:
                    if wav_tmp and os.path.exists(wav_tmp):
                        try:
                            os.remove(wav_tmp)
                        except Exception:
                            pass
            return ""

    async def transcribe_bytes(self, raw_bytes: bytes, filename: str = "audio.ogg") -> str:
        """バイト列（Discord添付音声・アップロードファイル等）を非同期で文字起こしする。"""
        if not raw_bytes:
            return ""
        ext = os.path.splitext(filename)[1].lower() or ".ogg"
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        try:
            with open(tmp_path, "wb") as f:
                f.write(raw_bytes)
            return await asyncio.to_thread(self.transcribe_file_sync, tmp_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def transcribe_pcm16_sync(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        """16kHz 16bitモノラルPCMバイト列を文字起こしする。"""
        if len(pcm_bytes) < sample_rate:  # 0.5秒未満の極短ノイズは無視
            return ""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        buf.seek(0)
        fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with open(tmp_wav, "wb") as f:
                f.write(buf.getvalue())
            return self.transcribe_file_sync(tmp_wav)
        finally:
            if os.path.exists(tmp_wav):
                try:
                    os.remove(tmp_wav)
                except Exception:
                    pass


class LocalVoiceListener:
    """webrtcvad + sounddevice + faster-whisper を用いた常時マイク待機エンジン。"""

    SAMPLE_RATE = 16000
    FRAME_MS = 30
    FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_MS / 1000)  # 480サンプル

    def __init__(
        self,
        transcriber: WhisperTranscriber,
        on_transcript: Callable[[str], Coroutine[Any, Any, None]],
    ) -> None:
        self.transcriber = transcriber
        self.on_transcript = on_transcript
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._status = "stopped"
        self._last_error = ""

    def get_status(self) -> dict[str, Any]:
        """現在のローカル音声待機状態を返す。"""
        return {
            "enabled": bool(config.local_stt_enabled),
            "running": self._running,
            "status": self._status,
            "error": self._last_error,
            "model_size": config.whisper_model_size,
        }

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """設定が有効ならバックグラウンドマイク監視スレッドを起動する。"""
        if self._running:
            return
        self._loop = loop
        self._stop_event.clear()
        self._last_error = ""
        self._thread = threading.Thread(target=self._capture_worker, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """マイク監視スレッドを安全に停止する。"""
        self._stop_event.set()
        self._running = False
        self._status = "stopped"

    def _capture_worker(self) -> None:
        """VADで発話区間を切り出し、無音終了時にWhisperへ渡すワーカー。"""
        try:
            import sounddevice as sd
            import webrtcvad
        except Exception as exc:
            self._status = "error"
            self._last_error = f"音声認識ライブラリ読込失敗: {exc}"
            self._running = False
            return

        vad = webrtcvad.Vad(2)
        speech_frames: list[bytes] = []
        triggered = False
        silence_count = 0
        max_silence_frames = int(750 / self.FRAME_MS)  # 約0.75秒の無音で確定
        max_speech_frames = int(12000 / self.FRAME_MS)  # 最大12秒

        try:
            self._running = True
            self._status = "listening"
            with sd.RawInputStream(
                samplerate=self.SAMPLE_RATE,
                blocksize=self.FRAME_SAMPLES,
                dtype="int16",
                channels=1,
            ) as stream:
                while not self._stop_event.is_set():
                    data, _overflowed = stream.read(self.FRAME_SAMPLES)
                    frame = bytes(data)
                    if len(frame) != self.FRAME_SAMPLES * 2:
                        continue
                    is_speech = False
                    try:
                        is_speech = vad.is_speech(frame, self.SAMPLE_RATE)
                    except Exception:
                        pass

                    if not triggered:
                        if is_speech:
                            triggered = True
                            speech_frames = [frame]
                            silence_count = 0
                    else:
                        speech_frames.append(frame)
                        if is_speech:
                            silence_count = 0
                        else:
                            silence_count += 1

                        if silence_count >= max_silence_frames or len(speech_frames) >= max_speech_frames:
                            pcm_data = b"".join(speech_frames)
                            speech_frames = []
                            triggered = False
                            silence_count = 0
                            text = self.transcriber.transcribe_pcm16_sync(pcm_data, self.SAMPLE_RATE)
                            if text and self._loop and not self._loop.is_closed():
                                asyncio.run_coroutine_threadsafe(self.on_transcript(text), self._loop)
        except Exception as exc:
            self._status = "error"
            self._last_error = str(exc)[:140]
        finally:
            self._running = False
            if self._status == "listening":
                self._status = "stopped"


whisper_transcriber = WhisperTranscriber()
