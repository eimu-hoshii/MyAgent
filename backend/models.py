"""
イベント・LLM応答・ツール呼び出し・システム状態のPydanticデータモデル定義。
"""

import time
import uuid
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class EmotionType(str, Enum):
    """キャラクターの6種類の基本表情。"""

    NORMAL = "normal"          # 通常
    SMILE = "smile"            # 微笑み
    JOY = "joy"                # 喜び
    SERIOUS = "serious"        # 真剣
    SURPRISED = "surprised"    # 驚き
    TROUBLED = "troubled"      # 困り顔


class EventSource(str, Enum):
    """イベントの発生元種別。"""

    USER_TEXT = "user_text"            # コンソールからのテキスト入力
    USER_VOICE = "user_voice"          # マイクからの音声認識入力
    DISCORD = "discord"                # Discord Bot経由のリモート指示
    LIVE_COMMENT = "live_comment"      # VTuber配信の視聴者コメント
    SUPERCHAT = "superchat"            # スーパーチャット（最優先）
    PROACTIVE = "proactive"            # バックグラウンド監視・自律トリガー
    SILENCE_BREAKER = "silence_breaker"  # 無言防止フリートークトリガー
    SYSTEM = "system"                  # システムイベント


class AgentEvent(BaseModel):
    """エージェントのイベントバスを流れる統一イベントオブジェクト。"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    source: EventSource = EventSource.USER_TEXT
    user: str = "Master"
    content: str
    priority: int = 5  # 小さい数値ほど高優先度 (1: ユーザー直接対話/スパチャ, 5: 通常コメント, 8: 無言防止)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)


class ToolCallRequest(BaseModel):
    """LLMが要求する自律ツール呼び出しスキーマ。"""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentStructuredOutput(BaseModel):
    """LLMが出力する構造化レスポンス。思考・感情・発話・多言語訳・ツール呼び出しを1ターンで同期する。"""

    thought: str = Field(description="内部思考プロセス（状況分析・判断根拠）")
    emotion: EmotionType = Field(default=EmotionType.NORMAL, description="現在の発話に対応する表情")
    speech: str = Field(description="キャラクターが実際に声に出して喋るセリフ")
    speech_sub: str = Field(default="", description="多言語バイリンガル字幕有効時の対訳テキスト（英語等）")
    action: str = Field(default="talk", description="UI演出アクション (talk | alert | celebrate | thinking)")
    tool_call: Optional[ToolCallRequest] = Field(default=None, description="実行が必要な自律ツール（不要ならnull）")
    tool_evidences: list[dict[str, str]] = Field(default_factory=list, description="ツールで取得した根拠データ・引用元のリスト")
    routed_model: str = Field(default="", description="ハイブリッドルーターにより実際に選択・推論されたモデル名")
    routing_tier: str = Field(default="balanced", description="推論ティア (fast | balanced | smart | cloud)")
    self_reviewed: bool = Field(default=False, description="高難易度タスクとして自己レビュー検証を通過したか")


class ChatRequest(BaseModel):
    """フロントエンドからのメッセージ送信リクエスト。"""

    content: str
    user: str = "Master"
    source: EventSource = EventSource.USER_TEXT
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModeSwitchRequest(BaseModel):
    """モードまたは設定・ペルソナ変更リクエスト。"""

    mode: Optional[str] = None
    proactive_enabled: Optional[bool] = None
    ollama_model: Optional[str] = None
    llm_provider: Optional[str] = None
    tts_provider: Optional[str] = None
    silence_timeout_sec: Optional[int] = None
    api_key: Optional[str] = None
    agent_name: Optional[str] = None
    persona_agent: Optional[str] = None
    persona_vtuber: Optional[str] = None
    reset_persona: Optional[bool] = None
