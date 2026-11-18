"""
Discord Bot 双方向連携コネクタモジュール。
- OpenClaw 等で作成した既存の Discord Bot トークンをそのまま流用し、NIA のイベントバスと双方向接続する。
- 許可された Discord ユーザーID（ホワイトリスト）からの DM / 指定チャンネル / メンションのみを受け付ける安全設計。
- テキスト・画像添付（マルチモーダル解析）・テキストファイル添付の受信、タイピング表示、
  および画面キャプチャ画像・音声ファイル添付での返信・能動通知DMに対応する。
"""

import asyncio
import base64
import io
import os
import re
import time
from typing import Any, Callable, Coroutine

import discord
from PIL import Image

from backend.audio_io import _get_ffmpeg_exe, whisper_transcriber
from backend.automation import approval_manager
from backend.config import config
from backend.memory import memory_manager
from backend.models import AgentEvent, EventSource
from backend.rag_engine import AUDIO_EXTENSIONS, extract_text_from_bytes
from backend import tools as tools_mod
from backend.tools import capture_screen_image, get_system_metrics


TEXT_FILE_EXTENSIONS = (
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".log", ".html", ".css", ".sql", ".sh", ".bat", ".ps1",
)
DOCUMENT_EXTENSIONS = (".pdf", ".docx", ".xlsx", ".pptx")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")


class CommandApprovalView(discord.ui.View):
    """Discord DM/チャンネル上でシェル実行の許可/拒否を行うボタンUI。"""

    def __init__(self, req_id: str, command: str) -> None:
        super().__init__(timeout=65.0)
        self.req_id = req_id
        self.command = command

    @staticmethod
    def _is_authorized(user_id: int) -> bool:
        allowed = {str(u).strip() for u in (config.discord_allowed_user_ids or []) if str(u).strip()}
        return str(user_id) in allowed

    async def _handle_decision(self, interaction: discord.Interaction, approved: bool) -> None:
        if not self._is_authorized(interaction.user.id):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        res = await approval_manager.resolve(
            self.req_id, approved=approved, actor=f"Discord ({interaction.user.display_name})"
        )
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True  # type: ignore[attr-defined]
        status_label = "✅ 承認済み（実行中）" if approved else "❌ 拒否済み（キャンセル）"
        if not res.get("ok"):
            status_label = f"⚠️ 処理済み: {res.get('error', '')}"
        try:
            await interaction.response.edit_message(
                content=f"🛡️ **コマンド実行リクエスト (`{self.req_id}`)**\n```bash\n{self.command}\n```\n結果: **{status_label}**",
                view=self,
            )
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="✅ 許可して実行", style=discord.ButtonStyle.success)
    async def approve_btn(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self._handle_decision(interaction, approved=True)

    @discord.ui.button(label="❌ 拒否する", style=discord.ButtonStyle.danger)
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button[Any]) -> None:
        await self._handle_decision(interaction, approved=False)


class DiscordBotConnector:
    """Discord Bot クライアントの起動・停止・送受信ルーティングを管理するクラス。"""

    def __init__(
        self,
        enqueue_fn: Callable[[AgentEvent], Coroutine[Any, Any, bool]],
        on_status_change: Callable[[], Coroutine[Any, Any, None]] | None = None,
        on_mode_command: Callable[[str], Coroutine[Any, Any, dict[str, Any]]] | None = None,
        on_clear_command: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """各種コールバックと接続状態管理フィールドを初期化する。"""
        self.enqueue_fn = enqueue_fn
        self.on_status_change = on_status_change
        self.on_mode_command = on_mode_command
        self.on_clear_command = on_clear_command

        self._client: discord.Client | None = None
        self._runner_task: asyncio.Task[Any] | None = None
        self._active_token: str = ""
        self._state: str = "stopped"  # stopped | connecting | connected | error
        self._bot_user: str = ""
        self._error: str = ""
        self._message_content_warning: bool = False

        # 返信待ちのイベントID → チャンネル・メッセージ参照・タイピングタスク
        self._reply_targets: dict[str, dict[str, Any]] = {}
        self._typing_tasks: dict[str, asyncio.Task[Any]] = {}
        self._voice_client: discord.VoiceClient | None = None

    def get_status(self) -> dict[str, Any]:
        """フロントエンドへ配信する Discord Bot の現在状態辞書を返す（トークンは含めない）。"""
        return {
            "enabled": bool(config.discord_enabled),
            "has_token": bool(config.discord_bot_token),
            "state": self._state,
            "bot_user": self._bot_user,
            "error": self._error,
            "message_content_warning": self._message_content_warning,
            "allowed_user_count": len(config.discord_allowed_user_ids or []),
        }

    async def _notify_status(self) -> None:
        """状態変化をコールバック経由でクライアントへブロードキャストする。"""
        if self.on_status_change:
            try:
                await self.on_status_change()
            except Exception:
                pass

    async def stop(self) -> None:
        """稼働中の Discord Bot クライアントとバックグラウンドタスクを停止する。"""
        for tid, task in list(self._typing_tasks.items()):
            task.cancel()
            self._typing_tasks.pop(tid, None)

        if self._voice_client is not None:
            try:
                await self._voice_client.disconnect(force=True)
            except Exception:
                pass
            self._voice_client = None

        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

        if self._runner_task is not None and not self._runner_task.done():
            self._runner_task.cancel()
            try:
                await self._runner_task
            except BaseException:
                pass
        self._runner_task = None
        self._active_token = ""
        self._bot_user = ""
        self._state = "stopped"
        self._error = ""
        self._message_content_warning = False
        await self._notify_status()

    async def sync_state(self) -> None:
        """現在の設定値（有効フラグ・トークン）に合わせて接続状態を同期する。"""
        token = (config.discord_bot_token or "").strip()
        if not config.discord_enabled or not token:
            if self._state != "stopped" or self._runner_task is not None:
                await self.stop()
            elif not token and config.discord_enabled:
                self._error = "Botトークンが未設定です。"
                self._state = "error"
                await self._notify_status()
            return

        # 同じトークンで既に接続または接続処理中なら維持する
        if (
            self._runner_task is not None
            and not self._runner_task.done()
            and self._active_token == token
        ):
            return

        await self.stop()
        self._active_token = token
        self._state = "connecting"
        self._error = ""
        self._message_content_warning = False
        await self._notify_status()
        self._runner_task = asyncio.create_task(self._run_loop(token))

    def _build_client(self, use_message_content: bool) -> discord.Client:
        """受信イベントをハンドリングする discord.Client インスタンスを構築する。"""
        intents = discord.Intents.default()
        intents.message_content = use_message_content
        intents.voice_states = True
        client = discord.Client(intents=intents)
        connector = self

        @client.event
        async def on_ready() -> None:
            connector._state = "connected"
            connector._bot_user = str(client.user) if client.user else "Connected"
            connector._error = ""
            print(f"[Discord] Bot 接続完了: {connector._bot_user} (message_content={use_message_content})")
            await connector._notify_status()

        @client.event
        async def on_message(message: discord.Message) -> None:
            await connector._handle_incoming_message(client, message)

        return client

    async def _run_loop(self, token: str) -> None:
        """Discord Gateway 接続ループ（特権インテント未設定時の自動フォールバック対応）。"""
        use_msg_content = True
        while config.discord_enabled and (config.discord_bot_token or "").strip() == token:
            client = self._build_client(use_message_content=use_msg_content)
            self._client = client
            try:
                await client.start(token)
                break
            except discord.PrivilegedIntentsRequired:
                if use_msg_content:
                    use_msg_content = False
                    self._message_content_warning = True
                    print("[Discord] Message Content Intent が無効のためフォールバック起動します。")
                    try:
                        await client.close()
                    except Exception:
                        pass
                    continue
                self._state = "error"
                self._error = "Discord Developer Portal で必要な Intents が許可されていません。"
                await self._notify_status()
                break
            except discord.LoginFailure:
                self._state = "error"
                self._error = "Discord Bot トークンが無効です。設定を確認してください。"
                print("[Discord] ログイン失敗: トークンが無効です。")
                await self._notify_status()
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._state = "error"
                self._error = f"接続エラー: {str(exc)[:120]}"
                print(f"[Discord] 接続エラー: {exc}")
                await self._notify_status()
                try:
                    await client.close()
                except Exception:
                    pass
                await asyncio.sleep(15.0)
            finally:
                if self._client is client:
                    self._client = None

    async def _handle_incoming_message(self, client: discord.Client, message: discord.Message) -> None:
        """Discord からのメッセージを受信し、認可チェック・コマンド判定・NIAイベント投入を行う。"""
        if message.author.bot or (client.user and message.author.id == client.user.id):
            return

        # 1. ホワイトリスト認可チェック（未登録時は安全のため全員拒否）
        allowed_users = {str(u).strip() for u in (config.discord_allowed_user_ids or []) if str(u).strip()}
        author_id = str(message.author.id)
        if not allowed_users or author_id not in allowed_users:
            return

        # 2. チャンネル／スレッド／メンション条件チェック
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_thread = isinstance(message.channel, discord.Thread)
        parent_id = str(getattr(message.channel, "parent_id", "") or "")
        allowed_channels = {str(c).strip() for c in (config.discord_allowed_channel_ids or []) if str(c).strip()}
        in_allowed_channel = (str(message.channel.id) in allowed_channels) or (
            bool(parent_id) and parent_id in allowed_channels
        )
        is_mentioned = bool(client.user and client.user in message.mentions)

        if not is_dm and not in_allowed_channel and not is_mentioned:
            return

        # メンションタグを除去して純粋なテキストを取得
        raw_text = message.content or ""
        if client.user:
            raw_text = re.sub(rf"<@!?{client.user.id}>", "", raw_text).strip()

        # 3. 組み込みクイックコマンドの処理
        lower_cmd = raw_text.lower()
        if lower_cmd in ("!help", "/help"):
            await self._safe_reply(
                message,
                "**🤖 NIA Discord コマンド一覧**\n"
                "- `!status`: PC負荷・稼働モデル確認\n"
                "- `!vision` / `!screen`: PC画面スクショ＋視覚解説\n"
                "- `!mode agent` / `!mode vtuber`: モード即時切替\n"
                "- `!briefing`: タスク・予定・環境の戦術ブリーフィング実行\n"
                "- `!research <テーマ>`: 非同期バックグラウンド深掘り調査開始\n"
                "- `!otayori <質問>`: VTuberお便り・質問箱へ投稿\n"
                "- `!approve <ID>` / `!deny <ID>`: コマンド実行の許可/拒否\n"
                "- `!joinvc` / `!leavevc`: ボイスチャンネル接続（音声再生対応）\n"
                "- `!clear`: 現在モードの会話履歴クリア\n"
                "※ 画像・PDF・Word/Excel/PPTX・コード・音声メッセージ(.ogg/.mp3)の添付解析にも対応しています。",
            )
            return

        if lower_cmd in ("!status", "/status"):
            await self._reply_quick_status(message)
            return

        if lower_cmd in ("!mode agent", "!mode vtuber"):
            target_mode = lower_cmd.split()[1]
            if self.on_mode_command:
                res = await self.on_mode_command(target_mode)
                active_model = res.get("active_ollama_model") or config.get_effective_ollama_model()
                await self._safe_reply(
                    message,
                    f"モードを **{target_mode}** に切り替えたわよ。（稼働LLM: `{active_model}`）",
                )
            return

        if lower_cmd in ("!clear", "!reset"):
            if self.on_clear_command:
                await self.on_clear_command()
            await self._safe_reply(message, f"現在のモード（`{config.mode}`）の会話履歴をクリアしたわ。")
            return

        if lower_cmd.startswith("!approve ") or lower_cmd.startswith("!deny "):
            parts = raw_text.split(maxsplit=1)
            if len(parts) == 2:
                approved = lower_cmd.startswith("!approve")
                res = await approval_manager.resolve(
                    parts[1].strip(), approved=approved, actor=f"Discord ({message.author.display_name})"
                )
                msg = (
                    f"承認ID `{parts[1].strip()}` を **{'承認' if approved else '拒否'}** したわ。"
                    if res.get("ok")
                    else str(res.get("error"))
                )
                await self._safe_reply(message, msg)
            return

        if lower_cmd.startswith("!otayori "):
            q_body = raw_text[len("!otayori ") :].strip()
            if q_body:
                mid = memory_manager.add_mailbag(
                    question=q_body,
                    sender=f"{message.author.display_name} (Discord)",
                    source="discord",
                )
                await self._safe_reply(message, f"📮 お便り箱 (ID: {mid}) に追加しておいたわよ！")
            return

        if lower_cmd.startswith("!research "):
            topic = raw_text[len("!research ") :].strip()
            if topic and tools_mod.DEEP_RESEARCH_TRIGGER is not None:
                info = await tools_mod.DEEP_RESEARCH_TRIGGER(topic)
                await self._safe_reply(
                    message,
                    f"🔍 バックグラウンド深掘り調査（ID: `{info.get('job_id')}`）を開始したわ: **{topic}**\n完了したらレポートと要約を届けるから待ちなさいよね。",
                )
                return

        if lower_cmd in ("!briefing", "/briefing"):
            raw_text = "現在の未完了タスク・予定・システム状態を確認して、短く戦術ブリーフィングをして"

        if lower_cmd == "!joinvc":
            v_state = getattr(message.author, "voice", None)
            if not v_state or not v_state.channel:
                await self._safe_reply(message, "あなたがサーバーのボイスチャンネルに入ってから `!joinvc` を呼びなさいよね。")
                return
            try:
                if self._voice_client and self._voice_client.is_connected():
                    await self._voice_client.move_to(v_state.channel)
                else:
                    self._voice_client = await v_state.channel.connect()
                await self._safe_reply(
                    message,
                    f"🔊 ボイスチャンネル「{v_state.channel.name}」に接続したわ（※Discord.py標準制限により音声出力・発話読み上げ専用）。",
                )
            except Exception as exc:
                await self._safe_reply(message, f"VC接続エラー: {exc}")
            return

        if lower_cmd == "!leavevc":
            if self._voice_client and self._voice_client.is_connected():
                await self._voice_client.disconnect(force=True)
                self._voice_client = None
                await self._safe_reply(message, "ボイスチャンネルから切断したわ。")
            else:
                await self._safe_reply(message, "今はボイスチャンネルに接続してないわよ。")
            return

        # 4. 添付ファイル（画像・PDF・Office・テキスト・音声メッセージ）の抽出
        meta: dict[str, Any] = {
            "discord_channel_id": str(message.channel.id),
            "discord_user_id": author_id,
        }
        if is_thread and config.discord_thread_isolation:
            meta["session_key"] = f"discord_thread_{message.channel.id}"

        attached_texts: list[str] = []
        for att in message.attachments[:3]:
            fname = (att.filename or "").lower()
            ctype = (att.content_type or "").lower()
            ext = os.path.splitext(fname)[1].lower()

            if (ctype.startswith("image/") or fname.endswith(IMAGE_EXTENSIONS)) and att.size <= 12 * 1024 * 1024:
                if "image_base64" not in meta:
                    b64 = await self._download_image_as_base64(att)
                    if b64:
                        meta["image_base64"] = b64
            elif (ctype.startswith("audio/") or ext in AUDIO_EXTENSIONS) and att.size <= 12 * 1024 * 1024:
                try:
                    raw_audio = await att.read()
                    transcript = await whisper_transcriber.transcribe_bytes(raw_audio, filename=att.filename or "voice.ogg")
                    if transcript:
                        attached_texts.append(f"--- 音声メッセージ文字起こし ({att.filename}) ---\n{transcript}")
                    else:
                        attached_texts.append(f"--- 音声ファイル ({att.filename}) ---\n（音声内容を認識できませんでした）")
                except Exception:
                    pass
            elif (
                ctype.startswith("text/")
                or fname.endswith(TEXT_FILE_EXTENSIONS)
                or fname.endswith(DOCUMENT_EXTENSIONS)
            ) and att.size <= 5 * 1024 * 1024:
                try:
                    raw_bytes = await att.read()
                    ok_ext, extracted_txt = extract_text_from_bytes(
                        raw_bytes, att.filename or "file.txt", max_chars=int(config.fetch_max_chars)
                    )
                    if ok_ext and extracted_txt.strip():
                        attached_texts.append(f"--- 添付ファイル: {att.filename} ---\n{extracted_txt}")
                except Exception:
                    pass

        if lower_cmd in ("!vision", "!screen"):
            cap = capture_screen_image(target_override=config.vision_target)
            if not cap.get("ok"):
                await self._safe_reply(message, f"画面キャプチャに失敗したわ: {cap.get('error')}")
                return
            meta["image_base64"] = cap["base64_jpeg"]
            meta["captured_image_path"] = cap.get("file_path")
            raw_text = f"【Discord画面確認リクエスト (ウィンドウ: {cap['window_title']})】現在見えているPC画面の内容を確認して簡潔に報告・解説して。"

        if attached_texts:
            joined = "\n\n".join(attached_texts)
            base_q = raw_text or "添付されたファイル・音声の内容を確認して、分かりやすく要約・返答してください。"
            raw_text = f"{base_q}\n\n{joined}"

        if not raw_text and "image_base64" in meta:
            raw_text = "Discordから送られたこの画像の内容を確認して、解説・返答してください。"

        if not raw_text:
            if not is_dm and self._message_content_warning:
                await self._safe_reply(
                    message,
                    "メッセージ本文を読み取れなかったわ。Discord Developer Portal で `MESSAGE CONTENT INTENT` を ON にするか、DMで話しかけなさいよね。",
                )
            return

        event = AgentEvent(
            source=EventSource.DISCORD,
            user=f"{message.author.display_name} (Discord)",
            content=raw_text,
            priority=1,
            metadata=meta,
        )
        self._cleanup_stale_targets()
        self._reply_targets[event.id] = {
            "channel": message.channel,
            "message": message,
            "created_at": time.time(),
        }
        accepted = await self.enqueue_fn(event)
        if not accepted:
            self._reply_targets.pop(event.id, None)

    @staticmethod
    async def _download_image_as_base64(att: discord.Attachment) -> str | None:
        """Discord 添付画像をダウンロードし、Vision入力用の縮小JPEG Base64文字列へ変換する。"""
        try:
            raw = await att.read()
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            w, h = im.size
            max_dim = 1280
            if max(w, h) > max_dim:
                scale = max_dim / float(max(w, h))
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=84)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return None

    async def _reply_quick_status(self, message: discord.Message) -> None:
        """`!status` コマンドに対して LLM を経由せず現在のPC・NIA状態を即時返信する。"""
        m = get_system_metrics()
        tasks = memory_manager.get_tasks(status="pending")
        model_name = config.get_effective_ollama_model()
        lines = [
            f"**🤖 NIA ステータス ({m['timestamp']})**",
            f"- モード: `{config.mode}` / LLM: `{model_name}`",
            f"- RAM: `{m['memory_percent']}%` ({m['memory_used_gb']}/{m['memory_total_gb']} GB)",
            f"- DISK: `{m['disk_percent']}%` (空き {m['disk_free_gb']} GB)",
            f"- 操作中ウィンドウ: `{m['active_window']}`",
            f"- 未完了タスク: `{len(tasks)}件`",
        ]
        await self._safe_reply(message, "\n".join(lines))

    def start_typing(self, event: AgentEvent) -> None:
        """Discord 由来イベントの推論開始時に「入力中…」インジケーターを開始する。"""
        if event.source != EventSource.DISCORD:
            return
        target = self._reply_targets.get(event.id)
        if not target:
            return
        channel = target["channel"]

        async def _typing_worker() -> None:
            try:
                async with channel.typing():
                    await asyncio.sleep(90.0)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        old = self._typing_tasks.pop(event.id, None)
        if old:
            old.cancel()
        self._typing_tasks[event.id] = asyncio.create_task(_typing_worker())

    def stop_typing(self, event_id: str) -> None:
        """指定イベントのタイピングインジケーターを終了する。"""
        task = self._typing_tasks.pop(event_id, None)
        if task:
            task.cancel()

    @staticmethod
    def _suppress_discord_url_embeds(text: str) -> str:
        """Discordの自動埋め込みカード爆発を防ぎつつ、Markdownリンクを維持する。"""
        def _replace(match: re.Match[str]) -> str:
            md_label = match.group("md_label")
            if md_label is not None:
                return f"[{md_label}](<{match.group('md_url')}>)"
            if match.group("paren_url") is not None:
                return f"(<{match.group('paren_url')}>)"
            return f"<{match.group('bare_url')}>"

        return re.sub(
            r"\[(?P<md_label>[^\]]+)\]\((?P<md_url>https?://[^\s>)]+)\)"
            r"|(?<!\])\((?P<paren_url>https?://[^\s>)]+)\)"
            r"|(?<![<\(\[])(?P<bare_url>https?://[^\s<>)\]]+)",
            _replace,
            text,
        )

    @classmethod
    def _format_discord_evidences(cls, evidences: list[dict[str, str]] | None) -> str:
        """AIエージェントモード時のツール取得データ・引用元をDiscord返信向けに整形する。"""
        if not evidences or config.mode != "agent":
            return ""
        selected_lines: list[str] = []
        total_len = 0
        limit = 1450
        truncated = False

        for ev in evidences:
            raw = (ev.get("data") or "").strip()
            if not raw:
                continue
            tool_name = (ev.get("tool") or "").strip()
            header = f"📊 **参照データ・引用元 ({tool_name})**" if tool_name else "📊 **参照データ・引用元**"
            safe_raw = cls._suppress_discord_url_embeds(raw)
            block_lines = [header, *[ln.rstrip() for ln in safe_raw.splitlines() if ln.strip()]]

            if selected_lines:
                if total_len + 1 > limit:
                    truncated = True
                    break
                selected_lines.append("")
                total_len += 1

            for ln in block_lines:
                add_len = len(ln) + (1 if selected_lines else 0)
                if total_len + add_len > limit:
                    truncated = True
                    break
                selected_lines.append(ln)
                total_len += add_len
            if truncated:
                break

        if not selected_lines:
            return ""
        if truncated:
            selected_lines.append("…（以下省略）")
        return "\n".join(selected_lines)

    async def send_reply(
        self,
        event: AgentEvent,
        speech_text: str,
        image_path: str | None = None,
        audio_path: str | None = None,
        evidences: list[dict[str, str]] | None = None,
    ) -> None:
        """推論完了後の根拠データ（エージェントモード時）・セリフ・画像・音声を Discord へ返信する。"""
        self.stop_typing(event.id)
        target = self._reply_targets.pop(event.id, None)
        if not target:
            return

        channel = target["channel"]
        orig_msg = target.get("message")
        files: list[discord.File] = []
        try:
            if image_path and config.discord_send_vision_image and os.path.isfile(image_path):
                files.append(discord.File(image_path))
            if audio_path and config.discord_send_voice and os.path.isfile(audio_path):
                files.append(discord.File(audio_path))

            speech = (speech_text or "（応答なし）").strip()
            ev_text = self._format_discord_evidences(evidences)
            full_text = f"{ev_text}\n\n{speech}" if ev_text else speech

            chunks = self._split_message(full_text, limit=1900)
            for idx, chunk in enumerate(chunks):
                attach = files if idx == 0 else []
                if idx == 0 and orig_msg is not None:
                    await self._safe_reply(orig_msg, chunk, files=attach, fallback_channel=channel)
                else:
                    await self._safe_send(channel, chunk, files=attach)

            if audio_path and os.path.isfile(audio_path):
                await self.play_voice_in_vc(audio_path)
        finally:
            for f in files:
                try:
                    f.close()
                except Exception:
                    pass

    async def play_voice_in_vc(self, audio_path: str) -> bool:
        """ボイスチャンネルに接続中の場合、生成されたTTS音声を再生する。"""
        if not self._voice_client or not self._voice_client.is_connected():
            return False
        if not audio_path or not os.path.isfile(audio_path):
            return False
        try:
            if self._voice_client.is_playing():
                self._voice_client.stop()
            ffmpeg_exe = _get_ffmpeg_exe()
            source = discord.FFmpegPCMAudio(audio_path, executable=ffmpeg_exe)
            self._voice_client.play(source)
            return True
        except Exception as exc:
            print(f"[Discord VC] 音声再生エラー: {exc}")
            return False

    async def send_approval_request(self, req_info: dict[str, Any]) -> bool:
        """コマンド実行承認リクエスト（ボタンUI付き）を通知先 Discord ユーザーへ送信する。"""
        if not self._client or self._state != "connected":
            return False
        target_uid = (config.discord_notify_user_id or "").strip()
        if not target_uid and config.discord_allowed_user_ids:
            target_uid = str(config.discord_allowed_user_ids[0]).strip()
        if not target_uid or not target_uid.isdigit():
            return False
        try:
            uid_int = int(target_uid)
            user = self._client.get_user(uid_int) or await self._client.fetch_user(uid_int)
            req_id = str(req_info["id"])
            cmd = str(req_info["command"])
            reason = str(req_info.get("reason") or "")
            view = CommandApprovalView(req_id=req_id, command=cmd)
            msg_body = (
                f"🛡️ **コマンド実行の承認リクエスト (`{req_id}`)**\n"
                f"理由: {reason}\n"
                f"```bash\n{cmd}\n```\n"
                f"60秒以内に下のボタン（または `!approve {req_id}` / `!deny {req_id}`）で判断しなさいよね。"
            )
            await user.send(msg_body, view=view)
            return True
        except Exception as exc:
            print(f"[Discord] 承認リクエスト送信エラー: {exc}")
            return False

    async def send_proactive_dm(self, text: str) -> dict[str, Any]:
        """設定された宛先ユーザーへ Discord DM（予定通知・警告・テスト）を送信する。"""
        if not self._client or self._state != "connected":
            return {"ok": False, "error": "Discord Bot が接続されていません。"}
        target_uid = (config.discord_notify_user_id or "").strip()
        if not target_uid and config.discord_allowed_user_ids:
            target_uid = str(config.discord_allowed_user_ids[0]).strip()
        if not target_uid or not target_uid.isdigit():
            return {"ok": False, "error": "通知先（または許可リスト）の Discord User ID が未設定です。"}

        try:
            uid_int = int(target_uid)
            user = self._client.get_user(uid_int) or await self._client.fetch_user(uid_int)
            for chunk in self._split_message(text, limit=1900):
                await user.send(chunk)
            return {"ok": True, "user": str(user)}
        except Exception as exc:
            return {"ok": False, "error": f"DM送信エラー: {exc}"}

    @staticmethod
    async def _safe_reply(
        message: discord.Message,
        text: str,
        files: list[discord.File] | None = None,
        fallback_channel: Any = None,
    ) -> None:
        """元メッセージへ返信し、元メッセージ削除済みなどの場合はチャンネル通常送信へフォールバックする。"""
        try:
            await message.reply(text, files=files or [], mention_author=False)
        except Exception:
            ch = fallback_channel or message.channel
            await DiscordBotConnector._safe_send(ch, text, files=files)

    @staticmethod
    async def _safe_send(
        channel: Any, text: str, files: list[discord.File] | None = None
    ) -> None:
        """チャンネルへ安全にメッセージ送信する。"""
        try:
            await channel.send(text, files=files or [])
        except Exception as exc:
            print(f"[Discord] メッセージ送信エラー: {exc}")

    @staticmethod
    def _split_message(text: str, limit: int = 1900) -> list[str]:
        """Discord の 2000 文字制限を超えないようテキストを分割する。"""
        s = (text or "").strip()
        if len(s) <= limit:
            return [s]
        return [s[i : i + limit] for i in range(0, len(s), limit)]

    def _cleanup_stale_targets(self) -> None:
        """応答されず一定時間経過した古い返信先参照を掃除する。"""
        now = time.time()
        expired = [k for k, v in self._reply_targets.items() if (now - v.get("created_at", 0)) > 300.0]
        for k in expired:
            self._reply_targets.pop(k, None)
            self.stop_typing(k)
