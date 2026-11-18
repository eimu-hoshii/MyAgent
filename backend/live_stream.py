"""
YouTube Live（pytchat優先＋HTMLフォールバック）および Twitch IRCv3（タグ・Bits・Sub・Raid対応）を
リアルタイム受信し、高速コメント時のバッチ集約・投票集計フック連携を行うライブストリーム接続モジュール。
"""

import asyncio
import re
import time
from typing import Any, Callable, Coroutine
import httpx
from backend.config import config
from backend.models import AgentEvent, EventSource


class LiveStreamConnector:
    """YouTube Live / Twitch チャット接続と高速コメントバッチ集約を管理するクラス。"""

    def __init__(
        self,
        enqueue_fn: Callable[[AgentEvent], Coroutine[Any, Any, bool]],
        on_raw_comment: Callable[[str, str, str], Coroutine[Any, Any, bool]] | None = None,
    ) -> None:
        """
        Args:
            enqueue_fn: 処理用イベント投入コールバック
            on_raw_comment: (user_name, text, platform) を受け取り投票等で消費したら True を返すフック
        """
        self.enqueue_fn = enqueue_fn
        self.on_raw_comment = on_raw_comment
        self.seen_yt_ids: set[str] = set()
        self._yt_task: asyncio.Task[Any] | None = None
        self._twitch_task: asyncio.Task[Any] | None = None
        self._batch_task: asyncio.Task[Any] | None = None
        self._current_twitch_ch: str = ""
        self._comment_batch: list[dict[str, str]] = []
        self._batch_lock = asyncio.Lock()
        self._last_comment_added_at: float = 0.0

    def start(self) -> None:
        """バックグラウンド監視・バッチフラッシュタスクを開始する。"""
        if self._yt_task is None or self._yt_task.done():
            self._yt_task = asyncio.create_task(self._youtube_loop())
        if self._twitch_task is None or self._twitch_task.done():
            self._twitch_task = asyncio.create_task(self._twitch_loop())
        if self._batch_task is None or self._batch_task.done():
            self._batch_task = asyncio.create_task(self._batch_flush_loop())

    @staticmethod
    def _extract_yt_video_id(raw: str) -> str:
        """YouTubeのURLまたはID文字列から11文字のVideo IDを抽出する。"""
        s = raw.strip()
        if not s:
            return ""
        m = re.search(r"(?:v=|/live/|youtu\.be/)([A-Za-z0-9_\-]{11})", s)
        if m:
            return m.group(1)
        if re.match(r"^[A-Za-z0-9_\-]{11}$", s):
            return s
        return ""

    async def submit_viewer_comment(
        self,
        user_name: str,
        text: str,
        platform: str = "local",
        is_special: bool = False,
        special_amount: str = "",
        extra_meta: dict[str, Any] | None = None,
    ) -> None:
        """受信した視聴者コメントを投票フックへ通し、通常コメントはバッチ判定してキュー投入する。"""
        clean_text = (text or "").strip()
        if not clean_text:
            return

        # 投票コマンド（!1 等）なら即集計のみ実施し、連投でトークキューを埋めない選択も許容
        if not is_special and self.on_raw_comment is not None:
            try:
                consumed = await self.on_raw_comment(user_name, clean_text, platform)
                if consumed:
                    return
            except Exception:
                pass

        if is_special:
            meta = {"amount": special_amount or "¥1,000", "platform": platform, **(extra_meta or {})}
            await self.enqueue_fn(
                AgentEvent(
                    source=EventSource.SUPERCHAT,
                    user=user_name,
                    content=clean_text,
                    metadata=meta,
                    priority=1,
                )
            )
            return

        if config.live_batch_enabled:
            async with self._batch_lock:
                self._comment_batch.append({"user": user_name, "text": clean_text, "platform": platform})
                self._last_comment_added_at = time.time()
                if len(self._comment_batch) >= 5:
                    await self._flush_batch_unlocked()
        else:
            await self.enqueue_fn(
                AgentEvent(
                    source=EventSource.LIVE_COMMENT,
                    user=user_name,
                    content=clean_text,
                    metadata={"platform": platform, **(extra_meta or {})},
                    priority=4,
                )
            )

    async def flush_comment_batch(self) -> int:
        """現在バッファに溜まっている通常コメントをバッチ集約（または単発投入）する。"""
        async with self._batch_lock:
            return await self._flush_batch_unlocked()

    async def _flush_batch_unlocked(self) -> int:
        if not self._comment_batch:
            return 0
        items = list(self._comment_batch)
        self._comment_batch.clear()
        count = len(items)

        if count == 1:
            it = items[0]
            await self.enqueue_fn(
                AgentEvent(
                    source=EventSource.LIVE_COMMENT,
                    user=it["user"],
                    content=it["text"],
                    metadata={"platform": it["platform"]},
                    priority=4,
                )
            )
            return 1

        # 複数件が同時流入した場合は1つのまとめコメントへ集約して配信テンポを維持
        lines = [f"- {it['user']}: {it['text']}" for it in items]
        lead_user = items[-1]["user"]
        summary_content = (
            f"【視聴者コメントまとめ ({count}件が同時流入)】\n"
            + "\n".join(lines)
            + "\n※全体の空気（盛り上がりや共通のツッコミ）を拾いつつ代表的なコメントへ自然に返答してください。"
        )
        await self.enqueue_fn(
            AgentEvent(
                source=EventSource.LIVE_COMMENT,
                user=f"{lead_user} ほか{count - 1}名",
                content=summary_content,
                metadata={"platform": items[-1]["platform"], "batched_count": count},
                priority=3,
            )
        )
        return count

    async def _batch_flush_loop(self) -> None:
        """バッファにコメントが溜まった後、一定時間で自動フラッシュするループ。"""
        while True:
            await asyncio.sleep(1.0)
            if self._comment_batch and (time.time() - self._last_comment_added_at) >= 2.5:
                await self.flush_comment_batch()

    async def _youtube_loop(self) -> None:
        """pytchat（優先）または HTML ポーリングで YouTube Live のコメント・スパチャ・メンバー加入を取得する。"""
        while True:
            await asyncio.sleep(2.0)
            vid = self._extract_yt_video_id(config.youtube_live_id)
            if not vid:
                continue

            # 1. pytchat で安定ストリーミング取得を試行
            used_pytchat = False
            try:
                import pytchat

                chat = await asyncio.to_thread(lambda: pytchat.create(video_id=vid, interruptable=False))
                used_pytchat = True
                while (
                    self._extract_yt_video_id(config.youtube_live_id) == vid
                    and await asyncio.to_thread(chat.is_alive)
                ):
                    data = await asyncio.to_thread(chat.get)
                    items = await asyncio.to_thread(data.sync_items)
                    for c in items:
                        cid = getattr(c, "id", "")
                        if cid and cid in self.seen_yt_ids:
                            continue
                        if cid:
                            self.seen_yt_ids.add(cid)
                            if len(self.seen_yt_ids) > 2000:
                                self.seen_yt_ids.clear()
                        author_name = getattr(getattr(c, "author", None), "name", "視聴者")
                        ctype = getattr(c, "type", "textMessage")
                        msg_text = getattr(c, "message", "") or ""
                        amount_str = getattr(c, "amountString", "") or ""

                        if ctype in ("superChat", "superSticker"):
                            await self.submit_viewer_comment(
                                f"{author_name} (YT)",
                                msg_text or "スパチャ/スタンプを送信しました！",
                                platform="youtube",
                                is_special=True,
                                special_amount=amount_str or "SuperChat",
                            )
                        elif ctype == "newSponsor":
                            await self.submit_viewer_comment(
                                f"{author_name} (YT)",
                                msg_text or "メンバーシップに新規加入しました！",
                                platform="youtube",
                                is_special=True,
                                special_amount="メンバー加入",
                            )
                        elif msg_text:
                            await self.submit_viewer_comment(
                                f"{author_name} (YT)",
                                msg_text,
                                platform="youtube",
                                is_special=False,
                            )
                    await asyncio.sleep(max(1.5, float(config.live_poll_interval_sec)))
                try:
                    chat.terminate()
                except Exception:
                    pass
            except Exception:
                pass

            # 2. pytchat 接続不可時の HTML フォールバック
            if not used_pytchat:
                await self._youtube_html_fallback_once(vid)
                await asyncio.sleep(max(4, int(config.live_poll_interval_sec)))

    async def _youtube_html_fallback_once(self, vid: str) -> None:
        """pytchat 未稼働時の HTTP HTML スクレイプ フォールバック。"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        }
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, headers=headers) as client:
                resp = await client.get(f"https://www.youtube.com/live_chat?v={vid}")
                if resp.status_code != 200:
                    return
                html_text = resp.text

                text_matches = re.findall(
                    r'"liveChatTextMessageRenderer":\{.*?"id":"([^"]+)".*?"authorName":\{"simpleText":"([^"]+)"\}.*?"message":\{"runs":\[\{"text":"([^"]+)"\}',
                    html_text,
                )
                for msg_id, author, msg_text in text_matches[-6:]:
                    if msg_id in self.seen_yt_ids:
                        continue
                    self.seen_yt_ids.add(msg_id)
                    await self.submit_viewer_comment(f"{author} (YT)", msg_text, platform="youtube")

                sc_matches = re.findall(
                    r'"liveChatPaidMessageRenderer":\{.*?"id":"([^"]+)".*?"authorName":\{"simpleText":"([^"]+)"\}.*?"purchaseAmountText":\{"simpleText":"([^"]+)"\}.*?"message":\{"runs":\[\{"text":"([^"]+)"\}',
                    html_text,
                )
                for msg_id, author, amount, msg_text in sc_matches[-3:]:
                    if msg_id in self.seen_yt_ids:
                        continue
                    self.seen_yt_ids.add(msg_id)
                    await self.submit_viewer_comment(
                        f"{author} (YT)",
                        msg_text,
                        platform="youtube",
                        is_special=True,
                        special_amount=amount,
                    )
        except Exception:
            pass

    @staticmethod
    def _parse_twitch_tags(raw_tags: str) -> dict[str, str]:
        """IRCv3 タグ文字列 (@key=val;...) を辞書へパースする。"""
        tags: dict[str, str] = {}
        for pair in raw_tags.lstrip("@").split(";"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                tags[k] = v.replace("\\s", " ").replace("\\n", " ")
        return tags

    @classmethod
    def _parse_twitch_irc_line(cls, line: str) -> dict[str, Any] | None:
        """Twitch IRCv3 行から PRIVMSG / USERNOTICE (Sub/Bits/Raid) を構造化抽出する。"""
        tags: dict[str, str] = {}
        rest = line.strip()
        if rest.startswith("@"):
            parts = rest.split(" ", 1)
            if len(parts) < 2:
                return None
            tags = cls._parse_twitch_tags(parts[0])
            rest = parts[1].strip()

        m_priv = re.match(r"^:([^!]+)!.*PRIVMSG #\S+ :(.+)$", rest)
        if m_priv:
            user_name = tags.get("display-name") or m_priv.group(1)
            msg_text = m_priv.group(2).strip()
            bits = int(tags["bits"]) if tags.get("bits", "").isdigit() else 0
            return {
                "kind": "bits" if bits > 0 else "chat",
                "user": f"{user_name} (Twitch)",
                "text": msg_text,
                "amount": f"{bits} Bits" if bits > 0 else "",
            }

        m_notice = re.match(r"^:tmi\.twitch\.tv USERNOTICE #\S+(?: :(.+))?$", rest)
        if m_notice:
            msg_id = tags.get("msg-id", "")
            user_name = tags.get("display-name") or tags.get("login") or "視聴者"
            sys_msg = tags.get("system-msg") or ""
            user_msg = (m_notice.group(1) or "").strip()
            if msg_id in ("sub", "resub", "subgift", "submysterygift", "raid"):
                label_map = {
                    "sub": "Twitch Sub",
                    "resub": "Twitch 再Sub",
                    "subgift": "Subギフト",
                    "submysterygift": "大量Subギフト",
                    "raid": "Twitch Raid",
                }
                return {
                    "kind": "special",
                    "user": f"{user_name} (Twitch)",
                    "text": user_msg or sys_msg or f"{label_map[msg_id]} ありがとうございます！",
                    "amount": label_map.get(msg_id, "Twitch支援"),
                }
        return None

    async def _twitch_loop(self) -> None:
        """Twitch 匿名IRC (IRCv3タグ付き) を用いてチャット・Bits・Sub・Raid をリアルタイム受信する。"""
        while True:
            ch = config.twitch_channel.strip().lstrip("#").lower()
            if not ch:
                await asyncio.sleep(4.0)
                continue

            try:
                reader, writer = await asyncio.open_connection("irc.chat.twitch.tv", 6667)
                writer.write(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
                writer.write(b"PASS SCHMOOPIIE\r\n")
                writer.write(b"NICK justinfan67420\r\n")
                writer.write(f"JOIN #{ch}\r\n".encode("utf-8"))
                await writer.drain()
                self._current_twitch_ch = ch

                while config.twitch_channel.strip().lstrip("#").lower() == ch:
                    line_bytes = await asyncio.wait_for(reader.readline(), timeout=45.0)
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8", errors="ignore").strip()
                    if line.startswith("PING"):
                        writer.write(b"PONG :tmi.twitch.tv\r\n")
                        await writer.drain()
                        continue
                    parsed = self._parse_twitch_irc_line(line)
                    if parsed:
                        is_sp = parsed["kind"] in ("bits", "special")
                        await self.submit_viewer_comment(
                            parsed["user"],
                            parsed["text"],
                            platform="twitch",
                            is_special=is_sp,
                            special_amount=parsed.get("amount", ""),
                        )
                writer.close()
            except Exception:
                await asyncio.sleep(5.0)
