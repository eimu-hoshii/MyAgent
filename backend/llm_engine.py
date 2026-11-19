"""
マルチプロバイダ・マルチモーダル（Vision）対応の自律エージェント脳（LLM Engine）。
ローカルの Ollama モデル自動検出、クラウドAPI（Gemini / OpenAI互換）、
感情タグ付き構造化出力（Structured Output）、自動記憶抽出、および最大3ステップの自律ツール呼び出し（ReAct ループ）を制御する。
"""

import asyncio
import datetime
import json
import re
import time
from typing import Any, Callable, Coroutine, Optional
import httpx
from backend.config import ACCESSORY_CATALOG, OUTFIT_CATALOG, config
from backend.guardrail import sanitize_agent_speech
from backend.memory import memory_manager
from backend.models import AgentEvent, AgentStructuredOutput, EmotionType, EventSource
from backend.tools import LAST_CAPTURE_CACHE, build_tools_spec, capture_screen_image, execute_agent_tool
from backend.vtuber_studio import extract_news_cards_from_web_search


STRUCTURED_OUTPUT_INSTRUCTION = """
【出力フォーマット厳守】
必ず以下のJSON形式のみを出力してください（マークダウンのコードブロックや前後の説明文は不要です）。

{
  "thought": "現在の状況分析・どのツールを使うべきか・どう返答すべきかの簡潔な内部思考（日本語）",
  "tool_call": null または {"name": "ツール名", "arguments": {"引数名": "値"}},
  "emotion": "normal | smile | joy | serious | surprised | troubled のいずれか1つ",
  "speech": "キャラクターとして実際に声に出して喋るセリフのみ（日本語、1〜3文程度。（笑顔で）（微笑みながら）等のト書き・行動説明は絶対書かない）",
  "speech_sub": "",
  "action": "talk | alert | celebrate | thinking のいずれか1つ",
  "memory_to_save": null または {"category": "user_pref | fact", "content": "長期記憶に残すべきユーザーの好み・予定・重要事実", "importance": 4},
  "viewer_note": null または "（視聴者コメント・スパチャへの返答時のみ）このリスナーの特徴・好み・話題を40字以内でまとめた最新メモ"
}

※ 外部検索(`web_search`)やページ本文取得(`fetch_webpage`)、画面認識(`vision_capture`)、システム診断(`system_diagnostics`)などのツールを実行したい場合は、`tool_call` にツール名と引数を指定してください。最終報告を行う段階では `tool_call` を `null` にしてください。
※ 重要: `speech` には純粋な音声発話テキストのみを入れ、`（笑顔で）`・`(笑)` のような演技メモやト書きは入れないでください（表情は `emotion` で表現します）。また直前の応答と全く同じ文面の繰り返しは避け、表現を変えて自然に応答してください。
"""


def _build_structured_output_instruction(bilingual_enabled: bool, target_lang_label: str) -> str:
    """バイリンガル設定に応じて JSON スキーマの指示文を最適化する。"""
    if not bilingual_enabled:
        return STRUCTURED_OUTPUT_INSTRUCTION
    return STRUCTURED_OUTPUT_INSTRUCTION.replace(
        '"speech_sub": "",',
        f'"speech_sub": "speechの内容を自然な{target_lang_label}へ翻訳した一文（空欄不可・必須出力）",',
    )


class IncrementalJsonSpeechExtractor:
    """
    ストリーミング中のJSON出力から `tool_call: null` の安全確定と `speech` フィールド内の
    完成文（句点区切り）を文字化け・途中誤発話なしでインクリメンタル抽出するパーサ。
    """

    _SIMPLE_ESCAPES = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    _PLACEHOLDER_RE = re.compile(r"〇|〇〇+|[○]{1,}(?:度|℃|%|％)|[xX×]{2,}(?:度|℃|%|％)")

    def __init__(self, allow_emit: bool = True) -> None:
        self.allow_emit = allow_emit
        self.state = "OUTSIDE"  # OUTSIDE | TOP_OBJ | KEY_STR | WAIT_COLON | WAIT_VAL | SPEECH_STR | SKIP_STR | COMPLEX | PRIMITIVE
        self.depth = 0
        self.current_key = ""
        self.key_buf: list[str] = []
        self.escape = False
        self.unicode_buf: str | None = None
        self.complex_in_str = False
        self.complex_escape = False
        self.active_val_key = ""

        self.tool_call_is_null: bool | None = None
        self.speech_done = False
        self.speech_chars: list[str] = []
        self.emotion_chars: list[str] = []
        self.emitted_len = 0
        self.emitted_sentences: list[str] = []
        self.aborted = False
        self.speech_ready_fired = False

    @property
    def current_emotion(self) -> str:
        emo = "".join(self.emotion_chars).strip().lower()
        return emo if emo in {"normal", "smile", "joy", "serious", "surprised", "troubled"} else "normal"

    @property
    def full_speech_decoded(self) -> str:
        return "".join(self.speech_chars)

    def pop_speech_ready(self) -> tuple[str, str] | None:
        """`speech` 全文が完成し、`tool_call: null` かつ伏せ字無しと確認された瞬間に一度だけ確定文を返す。"""
        if (
            not self.allow_emit
            or self.aborted
            or self.tool_call_is_null is not True
            or not self.speech_done
            or self.speech_ready_fired
        ):
            return None
        full_text = "".join(self.speech_chars).strip()
        if not full_text or self._PLACEHOLDER_RE.search(full_text):
            return None
        self.speech_ready_fired = True
        return full_text, self.current_emotion

    def _decode_str_char(self, ch: str, target_buf: list[str]) -> bool:
        """文字列リテラル内の1文字をデコードする。閉じクォートに到達したら False を返す。"""
        if self.unicode_buf is not None:
            self.unicode_buf += ch
            if len(self.unicode_buf) == 4:
                try:
                    target_buf.append(chr(int(self.unicode_buf, 16)))
                except Exception:
                    pass
                self.unicode_buf = None
            return True

        if self.escape:
            self.escape = False
            if ch == "u":
                self.unicode_buf = ""
            else:
                target_buf.append(self._SIMPLE_ESCAPES.get(ch, ch))
            return True

        if ch == "\\":
            self.escape = True
            return True

        if ch == '"':
            return False

        target_buf.append(ch)
        return True

    def _flush_ready(self) -> list[str]:
        """`tool_call: null` が確定し、伏せ字が含まれない場合のみ確定文を順次切り出す。"""
        if not self.allow_emit or self.aborted or self.tool_call_is_null is not True:
            return []

        full_text = "".join(self.speech_chars)
        if self._PLACEHOLDER_RE.search(full_text):
            return []

        out: list[str] = []
        while True:
            rem = full_text[self.emitted_len :]
            if not rem:
                break

            # 日本語文末・改行、または英語句読点＋空白を検索
            cut_idx = -1
            m_ja = re.search(r"[。！？\n]", rem)
            m_en = re.search(r"[!?](?:\s|$)|(?<!\d)\.(?:\s|$)", rem)
            if m_ja and m_en:
                cut_idx = min(m_ja.end(), m_en.end())
            elif m_ja:
                cut_idx = m_ja.end()
            elif m_en:
                cut_idx = m_en.end()

            if cut_idx != -1:
                seg = rem[:cut_idx].strip()
                # 極端に短い相槌だけでの細切れ再生を防ぐため、閉じ前なら6文字以上・最終文なら1文字以上で送信
                if len(seg) >= 6 or self.speech_done:
                    self.emitted_len += cut_idx
                    if seg:
                        out.append(seg)
                        self.emitted_sentences.append(seg)
                    continue
                break

            if self.speech_done:
                tail = rem.strip()
                self.emitted_len = len(full_text)
                if tail:
                    out.append(tail)
                    self.emitted_sentences.append(tail)
            break

        return out

    def feed(self, chunk: str) -> list[str]:
        """ストリームチャンクを処理し、新しく音声合成可能な完成文リストを返す。"""
        if not chunk:
            return []
        emitted: list[str] = []

        for ch in chunk:
            if self.state == "OUTSIDE":
                if ch == "{":
                    self.depth = 1
                    self.state = "TOP_OBJ"
                continue

            if self.state == "TOP_OBJ":
                if ch == '"':
                    self.key_buf = []
                    self.escape = False
                    self.unicode_buf = None
                    self.state = "KEY_STR"
                elif ch == "}":
                    self.depth = 0
                    self.state = "OUTSIDE"
                continue

            if self.state == "KEY_STR":
                cont = self._decode_str_char(ch, self.key_buf)
                if not cont:
                    self.current_key = "".join(self.key_buf)
                    self.state = "WAIT_COLON"
                continue

            if self.state == "WAIT_COLON":
                if ch == ":":
                    self.state = "WAIT_VAL"
                continue

            if self.state == "WAIT_VAL":
                if ch.isspace():
                    continue
                self.active_val_key = self.current_key
                if ch == '"':
                    self.escape = False
                    self.unicode_buf = None
                    if self.active_val_key == "speech":
                        self.speech_chars = []
                        self.speech_done = False
                        self.state = "SPEECH_STR"
                    elif self.active_val_key == "emotion":
                        self.emotion_chars = []
                        self.state = "EMOTION_STR"
                    else:
                        self.state = "SKIP_STR"
                    if self.active_val_key == "tool_call":
                        self.tool_call_is_null = False
                        self.aborted = True
                elif ch in ("{", "["):
                    if self.active_val_key == "tool_call":
                        self.tool_call_is_null = False
                        self.aborted = True
                    self.depth = 2
                    self.complex_in_str = False
                    self.complex_escape = False
                    self.state = "COMPLEX"
                else:
                    if self.active_val_key == "tool_call":
                        if ch == "n":
                            self.tool_call_is_null = True
                        else:
                            self.tool_call_is_null = False
                            self.aborted = True
                    self.state = "PRIMITIVE"
                continue

            if self.state == "SPEECH_STR":
                cont = self._decode_str_char(ch, self.speech_chars)
                if not cont:
                    self.speech_done = True
                    self.state = "TOP_OBJ"
                emitted.extend(self._flush_ready())
                continue

            if self.state == "EMOTION_STR":
                dummy = self.emotion_chars
                cont = self._decode_str_char(ch, dummy)
                if not cont:
                    self.state = "TOP_OBJ"
                continue

            if self.state == "SKIP_STR":
                cont = self._decode_str_char(ch, [])
                if not cont:
                    self.state = "TOP_OBJ"
                continue

            if self.state == "COMPLEX":
                if self.complex_in_str:
                    if self.complex_escape:
                        self.complex_escape = False
                    elif ch == "\\":
                        self.complex_escape = True
                    elif ch == '"':
                        self.complex_in_str = False
                else:
                    if ch == '"':
                        self.complex_in_str = True
                    elif ch in ("{", "["):
                        self.depth += 1
                    elif ch in ("}", "]"):
                        self.depth -= 1
                        if self.depth <= 1:
                            self.state = "TOP_OBJ"
                continue

            if self.state == "PRIMITIVE":
                if ch == ",":
                    self.state = "TOP_OBJ"
                elif ch == "}":
                    self.depth = 0
                    self.state = "OUTSIDE"
                continue

        # 途中で `tool_call: null` が確定した直後に溜まっていた speech も吐き出す
        if self.tool_call_is_null is True and self.speech_chars and self.emitted_len < len(self.speech_chars):
            emitted.extend(self._flush_ready())
        return emitted


class LLMEngine:
    """LLMとの通信・ハイブリッドルーティング・ストリーミング先読み・自己レビューを担当するクラス。"""

    def __init__(self) -> None:
        """初期化処理。"""
        self.available_ollama_models: list[str] = []
        self.last_viewer_note: str = ""

    async def detect_ollama_models(self) -> list[str]:
        """ローカルのOllamaサーバーから利用可能なモデル一覧を取得し、最適モデルを自動選択する。"""
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(f"{config.ollama_base_url}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    models = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
                    self.available_ollama_models = models
                    if models and config.ollama_model not in models:
                        for pref in config.preferred_ollama_models:
                            if pref in models:
                                config.ollama_model = pref
                                break
                        else:
                            config.ollama_model = models[0]
                    if models and config.ollama_model_vtuber not in models:
                        safe_models = [
                            m for m in models if "abliterated" not in m.lower() and "uncensored" not in m.lower()
                        ]
                        vtuber_pool = safe_models or models
                        for pref in config.preferred_ollama_models:
                            if pref in vtuber_pool:
                                config.ollama_model_vtuber = pref
                                break
                        else:
                            config.ollama_model_vtuber = vtuber_pool[0]
                    return models
        except Exception as exc:
            print(f"[LLMEngine] Ollama モデル検出失敗: {exc}")
        return []

    def _build_system_prompt(self, query_text: str = "") -> str:
        """現在のモード、現在日時、UIで設定されたペルソナ、意味ベクトル長期記憶、未完了タスク、多言語指示を組み込む。"""
        raw_template = config.persona_vtuber if config.mode == "vtuber" else config.persona_agent
        base_persona = raw_template.replace("{agent_name}", config.agent_name)

        now = datetime.datetime.now()
        yesterday = now - datetime.timedelta(days=1)
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        now_str = f"{now.strftime('%Y年%m月%d日')} ({weekdays[now.weekday()]}) {now.strftime('%H:%M')}"
        yesterday_str = f"{yesterday.strftime('%Y年%m月%d日')} ({weekdays[yesterday.weekday()]})"

        private_context = ""
        if config.mode == "agent":
            limit_mem = max(1, int(config.memory_prompt_limit))
            if config.semantic_memory_enabled:
                memories = memory_manager.search_memories_semantic(
                    query=query_text, limit=limit_mem, exclude_persona=True
                )
            else:
                memories = [
                    m
                    for m in memory_manager.search_memories(limit=limit_mem + 4)
                    if m["category"] not in ("persona_agent", "persona_vtuber")
                ]
            mem_text = (
                "\n".join([f"- [{m['category']} / ★{m['importance']}] {m['content']}" for m in memories[:limit_mem]])
                or "なし"
            )

            tasks = memory_manager.get_tasks(status="pending")
            task_text = (
                "\n".join(
                    [
                        f"- ID {t['id']}: {t['title']}"
                        + (f" (期限: {t['due_str']})" if t.get("due_str") else "")
                        for t in tasks
                    ]
                )
                or "なし"
            )
            wf_count = len(memory_manager.list_workflows())
            private_context = (
                f"【保持している長期記憶（意味関連度順）】\n{mem_text}\n\n"
                f"【現在の未完了タスク・予定】\n{task_text}\n"
                f"（登録済み自律ワークフロー数: {wf_count}件 / ローカル資料検索 `rag_search`・ファイル操作 `file_op`・詳細調査 `deep_research`・`browser_action`・`external_service`・`windows_control` 利用可能）\n\n"
            )

        fact_check_rule = (
            f"【現在日時コンテキスト】\n"
            f"- 現在日時: {now_str}\n"
            f"- 昨日の日付: {yesterday_str}\n"
            f"- 天気・気温・降水確率・降水量・風の予報を聞かれた場合は、推測やニュース検索ではなく**必ず `get_weather` ツール**（例: {{\"location\": \"亀岡\"}}）を実行して実数値を取得してください。\n"
            f"- 「今日」「昨日」「最近」「試合結果」「ニュース」などの時事・事実関係を聞かれた場合は、推測で答えず必ず `web_search` ツールを実行してください（さらに詳細が必要なら検索結果のURLに対して `fetch_webpage` を実行できます）。\n"
            f"- 検索クエリには可能な限り対象の日付や年（例: `{yesterday.strftime('%Y年%m月%d日')}` など）を含め、検索結果の `[最新ニュース (YYYY-MM-DD)]` の日付と照合して、過去の別大会や古い記事のスコアと絶対に混同しないでください。\n"
            f"- **PC操作の実行必須ルール**: 「PCロックして」「スリープして」「音量を上げて／下げて／ミュート」などのWindows操作を指示された場合は、**実行したふりの空返事は絶対禁止**です。必ず `windows_control` ツールを実行してから報告してください。\n"
            f"- **絶対禁止**: ツールを実行していない・またはツール結果に存在しない数値や事実を捏造すること、および**「〇」「〇〇」「XX」などの伏せ字・プレースホルダーを出力に含めることは絶対禁止**です。実数値が取得できなかった場合は正直に「取得できなかった」と答えてください。"
            + (
                "\n- なお、AIエージェントモードではツールで取得したデータ・引用元がユーザー画面（チャット／Discord）へ自動併記されるため、`speech` には重要な数値・要点とアドバイスを端的にまとめて報告してください。"
                if config.mode == "agent"
                else ""
            )
        )

        bilingual_rule = ""
        target_lang_label = "英語 (English)"
        if config.bilingual_enabled:
            lang_map = {"en": "英語 (English)", "ko": "韓国語 (Korean)", "zh": "中国語 (Chinese)"}
            target_lang_label = lang_map.get((config.bilingual_target_lang or "en").lower(), "英語 (English)")
            bilingual_rule = (
                f"\n【リアルタイム多言語翻訳＆バイリンガル字幕ルール】\n"
                f"- JSON出力の `\"speech_sub\"` フィールドに、`speech` の内容を自然な **{target_lang_label}** へ翻訳した一文を必ず記載してください（空欄不可）。\n"
                f"- 入力やコメントが外国語（英語・韓国語・中国語など）の場合は、まず相手の言語で親しみを込めて一言挨拶/反応を添えた上で分かりやすく日本語で本編を喋り、`speech_sub` に対訳を出力してください。\n"
            )

        topic_text = f"\n【現在の配信テーマ】{config.stream_topic}\n" if (config.mode == "vtuber" and config.stream_topic) else ""

        outfit_info = OUTFIT_CATALOG.get(config.active_outfit, {"name": config.active_outfit})
        acc_names = [ACCESSORY_CATALOG[a]["name"] for a in (config.active_accessories or []) if a in ACCESSORY_CATALOG]
        appearance_text = (
            f"\n【現在の見た目・親密度】\n"
            f"- 現在の服装: {outfit_info['name']} / アクセサリー: {', '.join(acc_names) if acc_names else 'なし'}\n"
        )
        if config.affection_enabled:
            aff = memory_manager.get_affection_state()
            appearance_text += (
                f"- マスターとの親密度: Lv.{aff['level']}「{aff['title']}」({aff['points']}pt) — {aff['prompt_hint']}\n"
            )

        return (
            f"{base_persona}\n\n"
            f"{fact_check_rule}\n{bilingual_rule}{topic_text}{appearance_text}\n"
            f"{private_context}"
            f"{build_tools_spec()}\n\n"
            f"{_build_structured_output_instruction(config.bilingual_enabled, target_lang_label)}"
        )

    @staticmethod
    def _format_viewer_context(profile: dict[str, Any] | None) -> str:
        """視聴者プロファイルをプロンプト注入用の短い文脈文字列に整形する。"""
        if not profile or not config.viewer_profile_enabled:
            return ""
        if profile.get("is_first_time"):
            return "\n【リスナー記憶】このリスナーは今回が初コメント（初見）です。歓迎してあげてください。"
        parts = [f"来訪{profile.get('visit_count', 1)}回目", f"累計コメント{profile.get('comment_count', 1)}件"]
        if profile.get("superchat_count"):
            parts.append(f"スパチャ{profile['superchat_count']}回")
        ago = profile.get("last_seen_ago_min")
        if ago is not None and profile.get("is_new_visit"):
            parts.append(f"前回コメントは約{round(ago / 60.0, 1)}時間前")
        note = profile.get("note") or ""
        note_txt = f" / メモ: {note}" if note else ""
        return f"\n【リスナー記憶（常連）】{' / '.join(parts)}{note_txt}\n※常連なら覚えていることを自然に匂わせて反応してください（毎回くどく言及しないこと）。"

    def _build_messages(self, event: AgentEvent) -> list[dict[str, Any]]:
        """システムプロンプト・直近の会話履歴・今回の入力イベントからメッセージ配列を構築する。"""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._build_system_prompt(query_text=event.content)}
        ]

        history_limit = max(0, int(config.conversation_history_limit))
        session_key = str((event.metadata or {}).get("session_key") or "")
        recent = (
            memory_manager.get_recent_conversations(limit=history_limit, mode=config.mode, session_key=session_key)
            if history_limit > 0
            else []
        )
        for conv in recent:
            messages.append(
                {"role": "user", "content": f"[{conv['user_name']}] {conv['user_input']}"}
            )
            assistant_json = json.dumps(
                {
                    "thought": conv.get("thought") or "",
                    "emotion": conv.get("emotion") or "normal",
                    "speech": conv.get("agent_speech") or "",
                    "action": "talk",
                    "tool_call": None,
                },
                ensure_ascii=False,
            )
            messages.append({"role": "assistant", "content": assistant_json})

        viewer_ctx = self._format_viewer_context(event.metadata.get("viewer_profile") if event.metadata else None)
        if (event.metadata or {}).get("is_collab"):
            user_prompt = f"【共同配信の相方（マスター音声）からの語りかけ】: {event.content}\n※視聴者コメントと区別し、同じ配信ブースに座る相棒として自然なテンポで掛け合い・ツッコミを返してください。"
        elif event.source == EventSource.SUPERCHAT:
            amount = event.metadata.get("amount", "¥1,000")
            user_prompt = f"【スーパーチャット受信！ 送信者: {event.user} / 金額: {amount}】\nメッセージ: 「{event.content}」\n※大喜び(joy または smile)で感謝しつつメッセージに反応してください！{viewer_ctx}"
        elif event.source == EventSource.LIVE_COMMENT:
            user_prompt = f"【視聴者コメント ({event.user})】: {event.content}{viewer_ctx}"
        elif event.source == EventSource.SILENCE_BREAKER:
            user_prompt = f"【自律配信トリガー（企画デッキ・無言防止）】: {event.content}"
        elif event.source == EventSource.PROACTIVE:
            user_prompt = f"【自律監視トリガー（能動報告）】: {event.content}"
        else:
            user_prompt = f"【{event.user}からの入力】: {event.content}"

        user_msg: dict[str, Any] = {"role": "user", "content": user_prompt}
        if event.metadata and event.metadata.get("image_base64"):
            user_msg["images"] = [event.metadata["image_base64"]]
        messages.append(user_msg)
        return messages

    @staticmethod
    def _trim_lines_safely(lines: list[str], limit: int) -> str:
        """行単位で上限文字数以内に収め、単語やURLの途中切れを防ぐ。"""
        selected: list[str] = []
        total = 0
        truncated = False
        for line in lines:
            clean_line = line.rstrip()
            if not clean_line:
                continue
            added_len = len(clean_line) + (1 if selected else 0)
            if total + added_len > limit:
                if not selected:
                    selected.append(clean_line[: max(1, limit - 1)].rstrip() + "…")
                truncated = True
                break
            selected.append(clean_line)
            total += added_len
        if truncated and selected:
            selected.append("…（以下省略。詳細はTHOUGHT LOGを参照）")
        return "\n".join(selected)

    @staticmethod
    def _escape_md_label(text: str, max_len: int = 90) -> str:
        """Markdownリンクのラベル文字列として安全な形式へ整形する。"""
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        cleaned = cleaned.replace("[", "［").replace("]", "］")
        if len(cleaned) > max_len:
            cleaned = cleaned[: max_len - 1].rstrip() + "…"
        return cleaned

    @classmethod
    def _format_web_search_evidence(cls, observation: str) -> str:
        """web_searchの生ログをユーザー閲覧向けの簡潔な引用リストへ整形する。"""
        formatted: list[str] = []
        for raw_line in observation.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("[システム助言]"):
                continue

            # [最新ニュース (YYYY-MM-DD HH:MM)] タイトル (URL)
            m_news = re.match(
                r"^\[最新ニュース \(([^)]+)\)\]\s+(.+?)(?:\s+\((https?://[^\s)]+)\))?$",
                line,
            )
            if m_news:
                date_label, title, url = m_news.groups()
                safe_title = cls._escape_md_label(title)
                link_part = f"[{safe_title}]({url})" if url else safe_title
                formatted.append(f"• {link_part} ({date_label})")
            else:
                # [Web: タイトル] (URL) スニペット
                m_web = re.match(
                    r"^\[Web:\s*(.+?)\]\s+\((https?://[^\s)]+)\)(?:\s+(.*))?$",
                    line,
                )
                if m_web:
                    title, url, snippet = m_web.groups()
                    safe_title = cls._escape_md_label(title)
                    snippet_clean = re.sub(r"\s+", " ", (snippet or "").strip())
                    if len(snippet_clean) > 85:
                        snippet_clean = snippet_clean[:84].rstrip() + "…"
                    snippet_part = f" — {snippet_clean}" if snippet_clean else ""
                    formatted.append(f"• [{safe_title}]({url}){snippet_part}")
                else:
                    # [Wikipedia: タイトル] スニペット
                    m_wiki = re.match(r"^\[Wikipedia:\s*(.+?)\]\s*(.*)$", line)
                    if m_wiki:
                        title, snippet = m_wiki.groups()
                        snippet_clean = re.sub(r"\s+", " ", snippet.strip())
                        if len(snippet_clean) > 100:
                            snippet_clean = snippet_clean[:99].rstrip() + "…"
                        formatted.append(f"• Wikipedia「{title.strip()}」: {snippet_clean}")
                    else:
                        formatted.append(f"• {line[:140]}")

            if len(formatted) >= 6:
                break

        return "\n".join(formatted)

    @classmethod
    def _format_tool_evidence(cls, tool_name: str, observation: str) -> str:
        """チャット・Discordへ併記するツール根拠データを見やすく構造化整形する。"""
        text = (observation or "").strip()
        if not text:
            return ""
        tname = tool_name.strip().lower()
        if tname == "web_search":
            formatted = cls._format_web_search_evidence(text)
            if formatted:
                return formatted

        lines = [
            ln for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("[システム助言]")
        ]
        limit = 900 if tname == "fetch_webpage" else 1300
        return cls._trim_lines_safely(lines, limit)

    @staticmethod
    def classify_routing_tier(event: AgentEvent) -> tuple[str, str]:
        """
        入力イベントの複雑度・重要度を判定し、(tier: 'fast'|'balanced'|'smart', 根拠) を返す。
        """
        mode_pref = (config.llm_routing_mode or "auto").lower()
        if mode_pref == "off":
            return "balanced", "固定モデル"
        if mode_pref == "fast":
            return "fast", "高速優先設定"
        if mode_pref == "smart":
            return "smart", "高精度優先設定"

        # auto ハイブリッド判定
        has_img = bool((event.metadata or {}).get("image_base64"))
        txt = (event.content or "").strip()
        txt_len = len(txt)

        if config.mode == "vtuber":
            if has_img:
                return "balanced", "VTuber画面視覚認識"
            if txt_len <= 160 and not any(w in txt for w in ("比較", "徹底", "詳細分析", "設計")):
                return "fast", "VTuberリアルタイム応答"
            return "balanced", "VTuber標準応答"

        # Agentモード判定
        if has_img:
            return "balanced", "画像/Vision入力"

        smart_keywords = (
            "設計",
            "アーキテクチャ",
            "比較",
            "徹底",
            "詳細分析",
            "戦略",
            "デバッグ",
            "リファクタ",
            "原因を特定",
            "レビュー",
            "プログラム",
            "コード",
            "エラー",
            "アルゴリズム",
        )
        if txt_len >= 260 or any(kw in txt for kw in smart_keywords):
            return "smart", "高度推論/設計/長文クエリ"

        simple_greetings = ("おはよう", "おやすみ", "こんにちは", "こんばんは", "ありがとう", "ただいま", "よろしく", "テスト")
        if (
            txt_len <= 95
            and event.source in (EventSource.USER_TEXT, EventSource.USER_VOICE, EventSource.DISCORD)
            and (
                any(txt.startswith(g) or g == txt for g in simple_greetings)
                or not any(
                    t in txt
                    for t in (
                        "調べ",
                        "検索",
                        "天気",
                        "予定",
                        "ファイル",
                        "コマンド",
                        "http",
                        "github",
                        "音量",
                        "ミュート",
                        "ロック",
                        "スリープ",
                        "通知",
                        "再生",
                        "停止",
                        "起動",
                        "開いて",
                    )
                )
            )
        ):
            return "fast", "軽量会話・即時応答"

        return "balanced", "標準自律処理"

    def select_routed_model(self, event: AgentEvent) -> tuple[str, str, str]:
        """
        現在利用可能なローカルOllamaモデルとモード安全規則を踏まえ、
        (model_name, tier, 選択理由) を返す。
        """
        tier, reason = self.classify_routing_tier(event)
        base_model = config.get_effective_ollama_model()
        avail = set(self.available_ollama_models or [])

        def _resolve_existing(candidates: list[str], safe_only: bool) -> str:
            for c in candidates:
                c_str = (c or "").strip()
                if not c_str:
                    continue
                if safe_only and ("abliterated" in c_str.lower() or "uncensored" in c_str.lower()):
                    continue
                if not avail or c_str in avail:
                    return c_str
            return base_model

        if config.mode == "vtuber":
            if tier == "fast":
                m = _resolve_existing([config.ollama_model_fast, "gemma4:e4b", "gemma3:4b", base_model], safe_only=True)
                return m, "fast", reason
            return base_model, "balanced", reason

        # Agentモード
        if tier == "fast":
            m = _resolve_existing([config.ollama_model_fast, "gemma4:e4b", "gemma3:4b", base_model], safe_only=False)
            return m, "fast", reason
        if tier == "smart":
            m = _resolve_existing([config.ollama_model_smart, "gemma4:26b", base_model], safe_only=False)
            return m, "smart", reason
        return base_model, "balanced", reason

    async def _run_self_review(
        self,
        model_name: str,
        user_content: str,
        draft_speech: str,
        evidences: list[dict[str, str]],
    ) -> tuple[bool, str, str]:
        """高難易度タスクの最終回答に対して、矛盾・要求漏れ・数値不整合を1パス自己検証する。"""
        ev_summary = "\n".join(f"[{e.get('tool')}] {e.get('data', '')[:300]}" for e in evidences[:3]) or "ツール観測なし"
        review_msgs = [
            {
                "role": "system",
                "content": (
                    "あなたは厳格な回答品質レビュアーです。ユーザーの質問と参照根拠に対し、"
                    "回答ドラフトに事実誤認・伏せ字・要求漏れ・論理破綻がないか検査し、"
                    'JSON {"approved": true|false, "reason": "簡潔な根拠", "improved_speech": "修正版(不要時は空文字)"} のみを出力してください。'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"【質問】{user_content[:500]}\n\n"
                    f"【参照根拠】\n{ev_summary}\n\n"
                    f"【回答案ドラフト】\n{draft_speech}\n"
                ),
            },
        ]
        try:
            raw = await self._call_ollama(review_msgs, model_override=model_name, timeout_sec=22.0)
            data = json.loads(re.search(r"\{.*\}", raw, flags=re.DOTALL).group(0))
            approved = bool(data.get("approved", True))
            reason = str(data.get("reason", "検証完了")).strip()[:100]
            improved = str(data.get("improved_speech", "")).strip()
            return approved, reason, improved
        except Exception:
            return True, "レビュースキップ", ""

    @staticmethod
    def _detect_direct_windows_control(text: str) -> dict[str, Any] | None:
        """
        「PCスリープして」「PCをロックして」「音量を上げて」等の明快な直接操作コマンドを
        質問文や否定文を誤検出しにくい形で検出し、`windows_control` 引数を返す。
        """
        raw = (text or "").strip()
        if not raw or len(raw) > 52:
            return None
        low = raw.lower()
        # 疑問・相談・否定文は除外してLLMの自然対話へ委ねる
        if any(
            ng in low
            for ng in (
                "?",
                "？",
                "できる",
                "方法",
                "やり方",
                "とは",
                "意味",
                "なぜ",
                "しないで",
                "するな",
                "やめて",
                "止めて",
                "不要",
                "もし",
                "場合",
            )
        ):
            return None

        if re.search(r"(?:pc|ｐｃ|パソコン|画面|スクリーン)?(?:を)?ロック(?:して|しろ|してください|お願い|頼む|してくれ)?[。！!\s]*$", low):
            if "ロック" in low:
                return {"action": "lock_pc"}
        if re.search(
            r"(?:(?:pc|ｐｃ|パソコン)(?:を)?スリープ|スリープ(?:して|しろ|させて|にして|してください|お願い|頼む|してくれ)|パソコン(?:を)?寝かせて)[。！!\s]*$",
            low,
        ):
            return {"action": "sleep_pc"}

        m_steps = re.search(r"(\d+)\s*(?:段階|回|ステップ)", raw)
        steps = max(1, min(15, int(m_steps.group(1)))) if m_steps else 2

        if re.search(r"(?:音量|音|ボリューム).*(?:上げ|大きく|増やし)|(?:上げ|大きく).*(?:音量|ボリューム)", raw):
            return {"action": "volume_up", "steps": steps}
        if re.search(r"(?:音量|音|ボリューム).*(?:下げ|小さく|減らし)|(?:下げ|小さく).*(?:音量|ボリューム)", raw):
            return {"action": "volume_down", "steps": steps}
        if re.search(r"(?:音量|音|pcの音)?(?:を)?(?:ミュート|消音)(?:に)?(?:して|しろ|お願い)?[。！!\s]*$", low):
            return {"action": "volume_mute"}
        return None

    @staticmethod
    def _detect_direct_appearance_command(text: str) -> dict[str, Any] | None:
        """「メイド服に着替えて」「猫耳つけて」「メガネ外して」等の着せ替え指示を確実に検出する。"""
        raw = (text or "").strip()
        if not raw or len(raw) > 56:
            return None
        low = raw.lower()
        if any(ng in low for ng in ("?", "？", "似合う", "どう思", "しないで", "着替えないで")):
            return None

        args: dict[str, Any] = {}
        change_verb = any(
            v in raw for v in ("着替え", "にして", "になって", "に着", "着て", "へ戻", "に戻", "つけて", "付けて", "かけて", "掛けて", "装着", "外して", "とって", "取って", "チェンジ")
        )
        if not change_verb:
            return None

        if any(w in low for w in ("パジャマ", "部屋着", "ルームウェア", "pajama")):
            args["outfit"] = "pajama"
        elif any(w in low for w in ("メイド", "maid")):
            args["outfit"] = "maid"
        elif any(w in low for w in ("セーラー", "夏服", "sailor")):
            args["outfit"] = "sailor"
        elif any(w in low for w in ("ストリート", "パーカー", "hoodie")):
            args["outfit"] = "hoodie_street"
        elif any(w in low for w in ("ワンピース", "ワンピ", "サマードレス", "onepiece")):
            args["outfit"] = "onepiece_summer"
        elif any(w in low for w in ("ジャージ", "jersey")):
            args["outfit"] = "gym_jersey"
        elif any(w in low for w in ("体操服", "体操着", "ブルマ", "ショーツ", "gym")):
            args["outfit"] = "gym_shorts"
        elif any(w in low for w in ("ハイネック", "ホルター", "highneck")):
            args["outfit"] = "swim_highneck"
        elif any(w in low for w in ("水着", "タンキニ", "ビキニ", "swim", "tankini")):
            args["outfit"] = "swim_tankini"
        elif any(w in low for w in ("浴衣", "ゆかた", "yukata")):
            args["outfit"] = "yukata"
        elif any(w in low for w in ("巫女", "みこ", "miko")):
            args["outfit"] = "miko"
        elif any(w in low for w in ("私服", "ニット", "デート服", "casual")):
            args["outfit"] = "casual"
        elif any(w in low for w in ("制服", "ブレザー", "標準", "元に戻", "元の服", "default")):
            args["outfit"] = "default"

        return args or None

    @staticmethod
    def _is_briefing_request(event: AgentEvent) -> bool:
        """定時ブリーフィングワークフローまたはユーザー/Discordからの戦術ブリーフィング要求かを判定する。"""
        if config.mode != "agent":
            return False
        meta = event.metadata or {}
        if str(meta.get("workflow_type", "")).lower() == "briefing":
            return True
        txt = (event.content or "").strip().lower()
        if not txt:
            return False
        if ("ブリーフィング" in txt or "briefing" in txt) and not any(
            ng in txt for ng in ("とは", "何？", "停止", "削除", "無効", "オフ")
        ):
            return True
        return False

    @staticmethod
    def _resolve_briefing_location(text: str) -> str:
        """プロンプトまたは長期記憶からブリーフィング対象の天気地域名を特定する（既定: 亀岡）。"""
        candidate_cities = (
            "亀岡",
            "京都",
            "大阪",
            "神戸",
            "奈良",
            "大津",
            "東京",
            "横浜",
            "名古屋",
            "福岡",
            "札幌",
            "仙台",
            "広島",
        )
        for c in candidate_cities:
            if c in (text or ""):
                return c
        try:
            for m in memory_manager.search_memories(limit=20):
                cnt = str(m.get("content", ""))
                for c in candidate_cities:
                    if c in cnt:
                        return c
        except Exception:
            pass
        return "亀岡"

    async def process_event(
        self,
        event: AgentEvent,
        on_status_Callback: Optional[Callable[[dict[str, Any]], Coroutine[Any, Any, None]]] = None,
        on_speech_sentence: Optional[Callable[[str, int, str], Coroutine[Any, Any, None]]] = None,
        on_speech_ready: Optional[Callable[[str, str], Coroutine[Any, Any, None]]] = None,
    ) -> AgentStructuredOutput:
        """
        イベントを受け取り、ハイブリッドモデル選択・ストリーミング文先読み・ReActツールループ・
        自己レビュー・意味記憶自動保存を経て最終応答を返す。
        """
        routed_model, routing_tier, routing_reason = self.select_routed_model(event)
        if on_status_Callback and config.llm_provider.lower() == "ollama":
            await on_status_Callback(
                {
                    "type": "model_routed",
                    "model": routed_model,
                    "tier": routing_tier,
                    "reason": routing_reason,
                }
            )

        messages = self._build_messages(event)
        parsed: AgentStructuredOutput | None = None
        captured_viewer_note = ""
        executed_evidences: list[dict[str, str]] = []
        placeholder_re = re.compile(r"〇|〇〇+|[○]{1,}(?:度|℃|%|％)|[xX×]{2,}(?:度|℃|%|％)")
        streamed_sentences: list[str] = []
        executed_windows_actions: set[str] = set()
        prefetched_briefing_tools: set[str] = set()
        executed_appearance_direct = False

        # 着せ替え・アクセサリー指示は空振りゼロで即座に反映し、照れ反応セリフを1パス生成
        if event.source in (EventSource.USER_TEXT, EventSource.USER_VOICE, EventSource.DISCORD, EventSource.LIVE_COMMENT):
            direct_app = self._detect_direct_appearance_command(event.content)
            if direct_app:
                app_obs = await execute_agent_tool("change_appearance", direct_app, event_source=event.source)
                executed_appearance_direct = True
                if on_status_Callback:
                    await on_status_Callback(
                        {
                            "type": "tool_execution",
                            "tool_name": "change_appearance",
                            "arguments": direct_app,
                            "thought": "リクエストに合わせて衣装・アクセサリーを着替えます。",
                            "emotion": EmotionType.SMILE.value,
                        }
                    )
                    await on_status_Callback(
                        {"type": "tool_result", "tool_name": "change_appearance", "observation": app_obs}
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"【着せ替え完了通知】\n{app_obs}\n\n"
                            "※既に着替えは画面へ反映済みです。追加の `tool_call` は必ず `null` にし、"
                            "着替えた服装やアクセサリーについて少し照れながら可愛く（ツンデレらしく）反応してください。"
                        ),
                    }
                )

        # 定時ブリーフィング・戦術ブリーフィング時は天気・ニュース・システム診断の実データを100%先行取得して注入
        if self._is_briefing_request(event):
            loc = self._resolve_briefing_location(event.content)
            news_query = "最新 IT AI テクノロジー ニュース"
            if on_status_Callback:
                await on_status_Callback(
                    {
                        "type": "tool_execution",
                        "tool_name": "get_weather + web_search + system_diagnostics",
                        "arguments": {"location": loc, "query": news_query},
                        "thought": "戦術ブリーフィングのため気象庁天気・最新テックニュース・PC状態・未完了タスクを一括取得します。",
                        "emotion": EmotionType.SERIOUS.value,
                    }
                )
            weather_obs, news_obs, diag_obs = await asyncio.gather(
                execute_agent_tool("get_weather", {"location": loc}, event_source=event.source),
                execute_agent_tool("web_search", {"query": news_query}, event_source=event.source),
                execute_agent_tool("system_diagnostics", {}, event_source=event.source),
            )
            for t_name, t_obs in (
                ("get_weather", weather_obs),
                ("web_search", news_obs),
                ("system_diagnostics", diag_obs),
            ):
                prefetched_briefing_tools.add(t_name)
                f_ev = self._format_tool_evidence(t_name, t_obs)
                if f_ev:
                    executed_evidences.append({"tool": t_name, "data": f_ev})
                if on_status_Callback:
                    await on_status_Callback(
                        {
                            "type": "tool_result",
                            "tool_name": t_name,
                            "observation": t_obs,
                        }
                    )

            pending_tasks = memory_manager.get_tasks(status="pending")
            tasks_summary = (
                "\n".join(
                    f"- {t['title']}" + (f" (期限: {t['due_str']})" if t.get("due_str") else "")
                    for t in pending_tasks
                )
                or "現在未完了の予定・タスクはありません。"
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "【戦術ブリーフィング用・システム先行取得済み実データ】\n"
                        f"1. 天気予報 ({loc}):\n{weather_obs}\n\n"
                        f"2. 未完了の予定・タスク:\n{tasks_summary}\n\n"
                        f"3. PCシステム状態:\n{diag_obs}\n\n"
                        f"4. 最新ニュース検索結果:\n{news_obs}\n\n"
                        "※上記の通り実データはすべて取得済みです（画面にも引用データとして自動併記されます）。"
                        "追加のツール呼び出し (`tool_call`) は必ず `null` にし、取得した実数値（今日の天気・最高/最低気温・降水確率・タスク有無・注目ニュース1〜2件・PC状態）を端的にまとめた戦術ブリーフィングを報告してください。"
                    ),
                }
            )

        # Agentモードかつ直接ユーザー依頼の明確なWindows制御は、空振りゼロ＆1パス完結で即実行
        if config.mode == "agent" and event.source in (
            EventSource.USER_TEXT,
            EventSource.USER_VOICE,
            EventSource.DISCORD,
        ):
            direct_args = self._detect_direct_windows_control(event.content)
            if direct_args:
                if on_status_Callback:
                    await on_status_Callback(
                        {
                            "type": "tool_execution",
                            "tool_name": "windows_control",
                            "arguments": direct_args,
                            "thought": f"マスターの直接指示に基づき Windows 環境制御 ({direct_args['action']}) を実行します。",
                            "emotion": EmotionType.SERIOUS.value,
                        }
                    )
                direct_obs = await execute_agent_tool("windows_control", direct_args, event_source=event.source)
                executed_windows_actions.add(str(direct_args.get("action", "")))
                formatted_ev = self._format_tool_evidence("windows_control", direct_obs)
                if formatted_ev:
                    executed_evidences.append({"tool": "windows_control", "data": formatted_ev})
                if on_status_Callback:
                    await on_status_Callback(
                        {
                            "type": "tool_result",
                            "tool_name": "windows_control",
                            "observation": direct_obs,
                        }
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"【ツール `windows_control` 実行完了通知】\n{direct_obs}\n\n"
                            "※上記の通り実行処理は完了済みです。追加のツール呼び出し (`tool_call`) は必ず `null` にして、"
                            "マスターへ実行した旨をキャラクターらしく簡潔に報告してください。"
                        ),
                    }
                )

        # smart + 自己レビュー有り時はレビュー前テキストを誤発声させないためストリーム先読みは抑止
        allow_stream_tts = bool(
            on_speech_sentence
            and config.streaming_tts_enabled
            and config.llm_provider.lower() == "ollama"
            and not (routing_tier == "smart" and config.llm_self_review_enabled and config.mode == "agent")
        )

        for step in range(4):
            step_sentences: list[str] = []

            async def _on_chunk_sentence(sentence: str, _idx: int, emo_hint: str) -> None:
                safe_s = sanitize_agent_speech(sentence, allow_fallback=False)
                if not safe_s:
                    return
                seq_no = len(streamed_sentences) + len(step_sentences)
                step_sentences.append(safe_s)
                if on_speech_sentence:
                    await on_speech_sentence(safe_s, seq_no, emo_hint)

            async def _on_speech_done(full_speech: str, emo_hint: str) -> None:
                safe_full = sanitize_agent_speech(full_speech, allow_fallback=False)
                if safe_full and on_speech_ready:
                    await on_speech_ready(safe_full, emo_hint)

            raw_text = await self._call_llm(
                messages,
                model_override=routed_model,
                on_sentence=_on_chunk_sentence if allow_stream_tts else None,
                on_speech_ready=_on_speech_done if allow_stream_tts else None,
            )
            self.last_viewer_note = ""
            parsed, mem_to_save = self._parse_structured_output_with_memory(raw_text)
            if self.last_viewer_note:
                captured_viewer_note = self.last_viewer_note

            # 自動記憶抽出があれば保存（配信中の会話から私的長期記憶へ誤混入しないようAgentモード時のみ保存）
            if config.mode == "agent" and config.auto_memory_extract and mem_to_save:
                cat = str(mem_to_save.get("category", "fact"))
                cnt = str(mem_to_save.get("content", "")).strip()
                imp = int(mem_to_save.get("importance", 4) or 4)
                if cnt:
                    memory_manager.save_memory(category=cat, content=cnt, importance=imp)

            if not parsed.tool_call or not parsed.tool_call.name or step >= 3:
                if placeholder_re.search(parsed.speech or ""):
                    if step < 3:
                        messages.append({"role": "assistant", "content": raw_text})
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "【システム警告：伏せ字・プレースホルダー禁止】\n"
                                    "あなたの直前の応答には「〇」「〇〇」などの伏せ字が含まれています。\n"
                                    "実数値を伏せ字で誤魔化す、またはツール実行せずに推測することを禁じます。必ず実データを取得し、得られない場合は正直に回答してください。"
                                ),
                            }
                        )
                        continue
                    parsed.speech = "ごめんなさい、正確な数値データを確認できなかったわ。地名や条件をもう少し具体的に教えてくれる？"
                else:
                    streamed_sentences.extend(step_sentences)
                parsed.tool_call = None
                break

            tool_name = parsed.tool_call.name
            tool_args = parsed.tool_call.arguments

            # 既にダイレクト実行済みの同一 windows_control アクションや先行取得済みブリーフィングツールの重複呼び出しを抑止
            if (
                tool_name.strip().lower() == "windows_control"
                and str(tool_args.get("action", "")).strip().lower() in executed_windows_actions
            ) or (tool_name.strip().lower() in prefetched_briefing_tools) or (
                executed_appearance_direct and tool_name.strip().lower() == "change_appearance"
            ):
                parsed.tool_call = None
                streamed_sentences.extend(step_sentences)
                break

            if on_status_Callback:
                await on_status_Callback(
                    {
                        "type": "tool_execution",
                        "tool_name": tool_name,
                        "arguments": tool_args,
                        "thought": parsed.thought,
                        "emotion": EmotionType.SERIOUS.value,
                    }
                )

            observation = await execute_agent_tool(tool_name, tool_args, event_source=event.source)
            formatted_ev = self._format_tool_evidence(tool_name, observation)
            if formatted_ev:
                executed_evidences.append({"tool": tool_name, "data": formatted_ev})

            if on_status_Callback:
                await on_status_Callback(
                    {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "observation": observation,
                    }
                )
                if (
                    config.mode == "vtuber"
                    and config.vtuber_news_card_enabled
                    and tool_name.strip().lower() == "web_search"
                ):
                    cards = extract_news_cards_from_web_search(observation, max_cards=3)
                    if cards:
                        await on_status_Callback({"type": "news_overlay", "cards": cards})

            messages.append({"role": "assistant", "content": raw_text})
            next_user_msg: dict[str, Any] = {
                "role": "user",
                "content": (
                    f"【ツール `{tool_name}` の実行結果】\n{observation}\n\n"
                    "上記の検索・実行結果を踏まえ、特に日付（`[最新ニュース (YYYY-MM-DD)]` 等）が質問の対象日と一致しているか厳密に確認してください。"
                    "もし検索結果のURL本文をさらに詳しく読む必要があれば `fetch_webpage` を呼び出し、十分な情報が揃っていれば `tool_call` を `null` にして最終報告をJSON形式で出力してください。"
                ),
            }
            if tool_name.strip().lower() in ("vision_capture", "browser_action"):
                if (
                    LAST_CAPTURE_CACHE.get("ok")
                    and (time.time() - float(LAST_CAPTURE_CACHE.get("captured_at", 0))) < 12.0
                ):
                    cap = LAST_CAPTURE_CACHE
                elif tool_name.strip().lower() == "vision_capture":
                    cap = capture_screen_image(target_override=str(tool_args.get("target", config.vision_target)))
                else:
                    cap = {}
                if cap.get("ok") and cap.get("base64_jpeg"):
                    next_user_msg["images"] = [cap["base64_jpeg"]]
                    if cap.get("file_path"):
                        event.metadata = dict(event.metadata or {})
                        event.metadata["captured_image_path"] = cap["file_path"]
            messages.append(next_user_msg)

        assert parsed is not None
        parsed.routed_model = routed_model
        parsed.routing_tier = routing_tier

        # 高難易度 smart ルーティング時は1パス自己レビューを実施
        if (
            config.mode == "agent"
            and routing_tier == "smart"
            and config.llm_self_review_enabled
            and config.llm_provider.lower() == "ollama"
            and len(parsed.speech or "") >= 45
        ):
            approved, rev_reason, improved = await self._run_self_review(
                model_name=routed_model,
                user_content=event.content,
                draft_speech=parsed.speech,
                evidences=executed_evidences,
            )
            parsed.self_reviewed = True
            if not approved and improved:
                parsed.speech = improved
                parsed.thought = f"{parsed.thought}\n[自己レビュー改善: {rev_reason}]".strip()
            else:
                parsed.thought = f"{parsed.thought}\n[自己レビュー承認: {rev_reason}]".strip()

        parsed.speech = sanitize_agent_speech(parsed.speech)
        if config.bilingual_enabled and not parsed.speech_sub and len(parsed.speech) >= 2:
            lang_names = {"en": "English", "ko": "Korean", "zh": "Chinese"}
            t_lang = lang_names.get((config.bilingual_target_lang or "en").lower(), "English")
            try:
                tr_txt = await self.generate_markdown(
                    system_prompt=f"Translate the user's Japanese utterance into natural, concise {t_lang}. Output ONLY the translated text without quotes or explanations.",
                    user_prompt=parsed.speech,
                    timeout_sec=18.0,
                    model_override=routed_model,
                )
                if tr_txt:
                    parsed.speech_sub = tr_txt.strip().strip('"').strip("'")
            except Exception as exc:
                print(f"[LLMEngine] バイリンガル翻訳フォールバック失敗: {exc}")
        if parsed.speech_sub:
            parsed.speech_sub = sanitize_agent_speech(parsed.speech_sub, allow_fallback=False)
        parsed.tool_evidences = executed_evidences if config.mode == "agent" else []

        # ストリーミングで既に送出済みの文数を記録（重複合成回避）
        event.metadata = dict(event.metadata or {})
        event.metadata["streamed_sentences"] = streamed_sentences

        # 常連リスナー記憶：視聴者の特徴メモを更新
        if (
            config.viewer_profile_enabled
            and captured_viewer_note
            and event.source in (EventSource.LIVE_COMMENT, EventSource.SUPERCHAT)
        ):
            memory_manager.set_viewer_note(event.user, captured_viewer_note)

        memory_manager.log_conversation(
            mode=config.mode,
            source=event.source.value,
            user_name=event.user,
            user_input=event.content,
            thought=parsed.thought,
            emotion=parsed.emotion.value,
            agent_speech=parsed.speech,
            evidences=parsed.tool_evidences,
            session_key=str((event.metadata or {}).get("session_key") or ""),
            speech_sub=parsed.speech_sub,
        )

        if config.affection_enabled and event.source in (
            EventSource.USER_TEXT,
            EventSource.USER_VOICE,
            EventSource.DISCORD,
        ):
            memory_manager.add_affection(delta=2, kind="chat")

        return parsed

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        model_override: str | None = None,
        on_sentence: Optional[Callable[[str, int, str], Coroutine[Any, Any, None]]] = None,
        on_speech_ready: Optional[Callable[[str, str], Coroutine[Any, Any, None]]] = None,
    ) -> str:
        """現在選択されているLLMプロバイダを呼び出して応答テキストを取得する。"""
        provider = config.llm_provider.lower()

        if provider == "gemini" and config.gemini_api_key:
            return await self._call_gemini(messages)
        if provider == "openai" and config.openai_api_key:
            return await self._call_openai(messages)
        return await self._call_ollama(
            messages,
            model_override=model_override,
            on_sentence=on_sentence,
            on_speech_ready=on_speech_ready,
        )

    async def _call_ollama(
        self,
        messages: list[dict[str, Any]],
        model_override: str | None = None,
        on_sentence: Optional[Callable[[str, int, str], Coroutine[Any, Any, None]]] = None,
        on_speech_ready: Optional[Callable[[str, str], Coroutine[Any, Any, None]]] = None,
        timeout_sec: float = 55.0,
    ) -> str:
        """ローカルのOllamaチャットエンドポイントを呼び出す（ストリーミング文単位抽出・非Vision再試行対応）。"""
        model_to_use = model_override or config.get_effective_ollama_model()
        use_stream = (on_sentence is not None) or (on_speech_ready is not None)

        async def _execute_request(req_messages: list[dict[str, Any]]) -> str | None:
            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                payload = {
                    "model": model_to_use,
                    "messages": req_messages,
                    "stream": use_stream,
                    "think": False,
                    "format": "json",
                    "options": {
                        "temperature": 0.65,
                        "num_predict": 600,
                    },
                }
                if not use_stream:
                    resp = await client.post(f"{config.ollama_base_url}/api/chat", json=payload)
                    if resp.status_code == 200:
                        return resp.json().get("message", {}).get("content", "")
                    print(f"[LLMEngine] Ollama HTTP {resp.status_code}: {resp.text[:160]}")
                    return None

                extractor = IncrementalJsonSpeechExtractor(allow_emit=True)
                full_chunks: list[str] = []
                sent_count = 0
                async with client.stream("POST", f"{config.ollama_base_url}/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        print(f"[LLMEngine] Ollama Stream HTTP {resp.status_code}: {body[:160]}")
                        return None
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk_obj = json.loads(line)
                        except Exception:
                            continue
                        delta = chunk_obj.get("message", {}).get("content", "")
                        if delta:
                            full_chunks.append(delta)
                            ready = extractor.feed(delta)
                            for s in ready:
                                if on_sentence:
                                    await on_sentence(s, sent_count, extractor.current_emotion)
                                    sent_count += 1
                            ready_full = extractor.pop_speech_ready()
                            if ready_full and on_speech_ready:
                                await on_speech_ready(ready_full[0], ready_full[1])
                        if chunk_obj.get("done"):
                            break
                return "".join(full_chunks)

        try:
            res_txt = await _execute_request(messages)
            if res_txt is not None:
                return res_txt

            # 画像添付（images）が原因でHTTP 400等になった場合はimagesを外して再試行
            if any("images" in m for m in messages):
                stripped_msgs = [{k: v for k, v in m.items() if k != "images"} for m in messages]
                res_txt2 = await _execute_request(stripped_msgs)
                if res_txt2 is not None:
                    return res_txt2
        except Exception as exc:
            print(f"[LLMEngine] Ollama 通信エラー: {exc}")

        return json.dumps(
            {
                "thought": "ローカルLLMとの通信がタイムアウトまたは失敗しました。",
                "tool_call": None,
                "emotion": "troubled",
                "speech": "マスター、ローカル推論エンジンとの通信が一時的に不安定です。接続状態を確認してください。",
                "action": "alert",
            },
            ensure_ascii=False,
        )

    async def _call_gemini(self, messages: list[dict[str, Any]]) -> str:
        """Google Gemini API を呼び出す（マルチモーダル対応）。"""
        try:
            system_text = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
            contents = []
            for m in messages[1:]:
                role = "model" if m["role"] == "assistant" else "user"
                parts: list[dict[str, Any]] = [{"text": m["content"]}]
                for b64 in m.get("images", []):
                    parts.append({"inlineData": {"mimeType": "image/jpeg", "data": b64}})
                contents.append({"role": role, "parts": parts})

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.gemini_model}:generateContent"
            payload = {
                "systemInstruction": {"parts": [{"text": system_text}]},
                "contents": contents,
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.65},
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, params={"key": config.gemini_api_key}, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            print(f"[LLMEngine] Gemini API エラー: {exc}")
        return await self._call_ollama(messages)

    async def _call_openai(self, messages: list[dict[str, Any]]) -> str:
        """OpenAI 互換 API を呼び出す。"""
        try:
            headers = {"Authorization": f"Bearer {config.openai_api_key}"}
            clean_msgs = [{k: v for k, v in m.items() if k != "images"} for m in messages]
            payload = {
                "model": config.openai_model,
                "messages": clean_msgs,
                "response_format": {"type": "json_object"},
                "temperature": 0.65,
            }
            async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
                resp = await client.post(f"{config.openai_base_url}/chat/completions", json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    return data["choices"][0]["message"]["content"]
        except Exception as exc:
            print(f"[LLMEngine] OpenAI API エラー: {exc}")
        return await self._call_ollama(messages)

    def _parse_structured_output_with_memory(
        self, raw_text: str
    ) -> tuple[AgentStructuredOutput, dict[str, Any] | None]:
        """
        LLMの生出力テキストからJSONを抽出し、(AgentStructuredOutput, 自動抽出記憶データ) を返す。
        """
        cleaned = raw_text.strip()
        think_match = re.search(r"<think>(.*?)</think>", cleaned, flags=re.DOTALL)
        extracted_think = think_match.group(1).strip() if think_match else ""
        if think_match:
            cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()

        json_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        target_str = json_match.group(0) if json_match else cleaned

        try:
            data = json.loads(target_str)
            emotion_raw = str(data.get("emotion", "normal")).lower().strip()
            valid_emotions = {e.value for e in EmotionType}
            emotion = EmotionType(emotion_raw) if emotion_raw in valid_emotions else EmotionType.NORMAL

            thought = str(data.get("thought") or extracted_think or "状況分析完了。")
            speech = str(data.get("speech") or data.get("response") or data.get("message") or "").strip()
            speech_sub = str(data.get("speech_sub") or "").strip()
            action = str(data.get("action") or "talk")
            tool_data = data.get("tool_call")
            mem_data = data.get("memory_to_save")
            viewer_note_raw = data.get("viewer_note")
            if isinstance(viewer_note_raw, str) and viewer_note_raw.strip() and viewer_note_raw.strip().lower() != "null":
                self.last_viewer_note = viewer_note_raw.strip()[:120]

            tool_call = None
            if isinstance(tool_data, dict) and tool_data.get("name"):
                tool_call = {
                    "name": str(tool_data["name"]),
                    "arguments": tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else {},
                }

            mem_to_save = None
            if isinstance(mem_data, dict) and mem_data.get("content"):
                mem_to_save = mem_data

            if not speech and not tool_call:
                speech = "了解しました、マスター。"

            return (
                AgentStructuredOutput(
                    thought=thought,
                    emotion=emotion,
                    speech=speech or "データを確認中です。",
                    speech_sub=speech_sub,
                    action=action,
                    tool_call=tool_call,
                ),
                mem_to_save,
            )
        except Exception:
            fallback_speech = re.sub(r"[{}\"\[\]]", "", cleaned)[:180].strip() or "了解しました。"
            return (
                AgentStructuredOutput(
                    thought=extracted_think or "非JSON出力からの自動変換",
                    emotion=EmotionType.NORMAL,
                    speech=fallback_speech,
                    action="talk",
                    tool_call=None,
                ),
                None,
            )

    async def generate_markdown(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_sec: float = 45.0,
        model_override: str | None = None,
    ) -> str:
        """Deep Research・アーカイブ要約・翻訳フォールバック向けに自由形式テキストを生成する。"""
        try:
            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                payload = {
                    "model": model_override or config.get_effective_ollama_model(),
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.4, "num_predict": 1200},
                }
                resp = await client.post(f"{config.ollama_base_url}/api/chat", json=payload)
                if resp.status_code == 200:
                    raw = (resp.json().get("message", {}).get("content", "") or "").strip()
                    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        except Exception as exc:
            print(f"[LLMEngine] Markdown生成エラー: {exc}")
        return ""


llm_engine = LLMEngine()
