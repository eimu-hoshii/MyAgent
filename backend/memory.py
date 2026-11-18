"""
SQLiteを用いたエージェントの長期記憶（重要度・編集対応）・会話履歴・カレンダー予定表/自律タスク（日時指定・繰り返し・スヌーズ対応）管理モジュール。
ペルソナ設定（キャラクター名・AIエージェント設定・VTuber設定）との自動整合性同期機能を備える。
"""

import contextlib
import datetime
import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Any, Iterator
import numpy as np
from backend.config import DB_PATH, DATA_DIR, config


_EMBEDDING_DIM = 256
_SUDACHI_TOKENIZER: Any = None
_SUDACHI_MODE: Any = None

# 日本語・英語の日常・開発ドメインにおける概念シノニムグループ（同義表現の意味類似検索精度を向上）
_SEMANTIC_CONCEPT_CLUSTERS: dict[str, set[str]] = {
    "_C_PREF_": {"好き", "好物", "お気に入り", "好み", "愛飲", "趣味", "推し", "ファン", "favorite", "like"},
    "_C_DISLIKE_": {"嫌い", "苦手", "ダメ", "避ける", "ストレス", "嫌", "dislike"},
    "_C_DRINK_": {"コーヒー", "珈琲", "カフェ", "カフェラテ", "エスプレッソ", "紅茶", "緑茶", "お茶", "茶", "飲み物", "飲物", "飲料", "水", "お酒", "ビール", "coffee", "tea"},
    "_C_FOOD_": {"食べ物", "料理", "ご飯", "食事", "朝食", "昼食", "夕食", "夜食", "ラーメン", "カレー", "肉", "魚", "お菓子", "スイーツ", "パン", "店", "グルメ"},
    "_C_DEV_": {"開発", "プログラミング", "プログラム", "コード", "実装", "設計", "アーキテクチャ", "テスト", "デバッグ", "バグ", "修正", "リファクタ", "python", "javascript", "api", "git", "github"},
    "_C_AI_": {"ai", "エージェント", "llm", "ollama", "gemma", "プロンプト", "モデル", "自律", "vtuber", "音声", "tts", "whisper", "rag", "mcp"},
    "_C_SCHED_": {"予定", "スケジュール", "会議", "ミーティング", "打ち合わせ", "締切", "期限", "タスク", "提出", "発表", "出社", "仕事", "業務", "残業"},
    "_C_HEALTH_": {"睡眠", "寝る", "起きる", "朝", "夜", "夜更かし", "疲れる", "疲労", "休憩", "体調", "健康", "運動", "病院", "目", "肩こり"},
    "_C_PC_": {"パソコン", "pc", "gpu", "cpu", "メモリ", "ram", "ディスク", "windows", "グラボ", "スペック", "設定"},
    "_C_LOC_": {"天気", "気温", "雨", "晴れ", "雪", "住む", "自宅", "地域", "京都", "亀岡", "東京", "大阪", "駅"},
    "_C_STREAM_": {"配信", "ライブ", "youtube", "twitch", "obs", "スパチャ", "リスナー", "視聴者", "コメント", "歌枠", "お便り"},
    "_C_ENT_": {"ゲーム", "アニメ", "映画", "音楽", "曲", "歌", "漫画", "小説", "読書", "動画"},
    "_C_USER_": {"ユーザー", "マスター", "私", "自分", "俺", "僕", "名前", "性格", "特徴", "目標"},
}


def _get_sudachi() -> tuple[Any, Any]:
    """SudachiPy トークナイザーを遅延ロードして共有利用する。"""
    global _SUDACHI_TOKENIZER, _SUDACHI_MODE
    if _SUDACHI_TOKENIZER is not None:
        return _SUDACHI_TOKENIZER, _SUDACHI_MODE
    try:
        from sudachipy import dictionary, tokenizer

        _SUDACHI_TOKENIZER = dictionary.Dictionary(dict="core").create()
        _SUDACHI_MODE = tokenizer.Tokenizer.SplitMode.B
    except Exception:
        _SUDACHI_TOKENIZER = False
        _SUDACHI_MODE = None
    return _SUDACHI_TOKENIZER, _SUDACHI_MODE


def _hash_feature(vec: np.ndarray, token: str, weight: float) -> None:
    """Feature Hashing (符号付き256次元射影) でベクトル要素を加算する。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    val = int.from_bytes(digest, "little")
    idx = val % _EMBEDDING_DIM
    sign = 1.0 if ((val >> 16) & 1) == 0 else -1.0
    vec[idx] += sign * weight


def compute_semantic_vector(text: str) -> np.ndarray:
    """
    SudachiPy 形態素原形・概念シノニムクラスタ・文字2-gramを用いた
    256次元 L2正規化ローカル意味ベクトルを生成する（外部VRAM/ネットワーク消費ゼロ）。
    """
    vec = np.zeros(_EMBEDDING_DIM, dtype=np.float32)
    clean = (text or "").strip()
    if not clean:
        return vec

    tok, mode = _get_sudachi()
    lemmas: list[str] = []
    if tok:
        try:
            for m in tok.tokenize(clean[:1200], mode):
                pos0 = m.part_of_speech()[0]
                if pos0 in ("助詞", "助動詞", "記号", "補助記号"):
                    continue
                norm = m.normalized_form().lower().strip()
                surf = m.surface().lower().strip()
                if norm:
                    lemmas.append(norm)
                if surf and surf != norm:
                    lemmas.append(surf)
        except Exception:
            pass

    if not lemmas:
        lemmas = re.findall(r"[a-z0-9_]+|[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]{2,}", clean.lower())

    for lm in lemmas:
        _hash_feature(vec, f"w:{lm}", 2.0)
        for cluster_tag, words in _SEMANTIC_CONCEPT_CLUSTERS.items():
            if lm in words:
                _hash_feature(vec, cluster_tag, 2.6)

    # 文字2-gram補助（未知語・表記揺れ・活用吸収）
    compact = re.sub(r"\s+", "", clean.lower())
    for i in range(max(0, len(compact) - 1)):
        _hash_feature(vec, f"b:{compact[i:i+2]}", 0.55)

    # シノニム部分一致チェック（短い合成語中のキーワードも拾う）
    lower_txt = clean.lower()
    for cluster_tag, words in _SEMANTIC_CONCEPT_CLUSTERS.items():
        for w in words:
            if len(w) >= 2 and w in lower_txt:
                _hash_feature(vec, cluster_tag, 1.4)
                break

    norm_val = float(np.linalg.norm(vec))
    if norm_val > 1e-8:
        vec /= norm_val
    return vec


def _vec_to_bytes(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def _bytes_to_vec(raw: bytes | None) -> np.ndarray | None:
    if not raw or len(raw) != _EMBEDDING_DIM * 4:
        return None
    return np.frombuffer(raw, dtype=np.float32).copy()


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class MemoryManager:
    """会話ログ、長期記憶ファクト、および自律監視タスク・予定表を永続化するクラス。"""

    def __init__(self, db_path: str = DB_PATH) -> None:
        """データベース接続を初期化し、必要なテーブルを作成する。"""
        os.makedirs(DATA_DIR, exist_ok=True)
        self.db_path = db_path
        self._init_db()
        # 起動時に現在のペルソナ設定と長期記憶の整合性を同期する
        self.sync_with_persona(
            agent_name=config.agent_name,
            persona_agent=config.persona_agent,
            persona_vtuber=config.persona_vtuber,
        )

    @contextlib.contextmanager
    def _get_conn(self) -> Iterator[sqlite3.Connection]:
        """SQLiteコネクションをトランザクション管理および確実なクローズ付きで取得する。"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """テーブルスキーマを初期化・マイグレーションする。"""
        with self._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    source TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    user_input TEXT NOT NULL,
                    thought TEXT,
                    emotion TEXT NOT NULL,
                    agent_speech TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance INTEGER DEFAULT 3,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    due_at REAL,
                    repeat_rule TEXT DEFAULT 'none',
                    notified INTEGER DEFAULT 0,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS viewer_profiles (
                    user_name TEXT PRIMARY KEY,
                    platform TEXT DEFAULT 'local',
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    visit_count INTEGER DEFAULT 1,
                    comment_count INTEGER DEFAULT 0,
                    superchat_count INTEGER DEFAULT 0,
                    note TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS workflows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    workflow_type TEXT NOT NULL DEFAULT 'briefing',
                    schedule_kind TEXT NOT NULL DEFAULT 'daily',
                    schedule_value TEXT NOT NULL,
                    target_url TEXT DEFAULT '',
                    prompt TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    last_run_at REAL DEFAULT 0,
                    last_hash TEXT DEFAULT '',
                    next_run_at REAL DEFAULT 0,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS mailbag_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT DEFAULT '匿名リスナー',
                    question TEXT NOT NULL,
                    source TEXT DEFAULT 'local',
                    status TEXT DEFAULT 'unread',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stream_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL DEFAULT 0,
                    status TEXT DEFAULT 'live',
                    summary_md TEXT DEFAULT '',
                    chapters_text TEXT DEFAULT '',
                    archive_path TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS stream_highlights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    elapsed_sec REAL NOT NULL,
                    event_kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    created_at REAL NOT NULL
                );
                """
            )
            # 既存DBへの repeat_rule / evidences_json / session_key / memories意味ベクトル用カラム追加マイグレーション
            for ddl in (
                "ALTER TABLE tasks ADD COLUMN repeat_rule TEXT DEFAULT 'none'",
                "ALTER TABLE conversations ADD COLUMN evidences_json TEXT DEFAULT '[]'",
                "ALTER TABLE conversations ADD COLUMN session_key TEXT DEFAULT ''",
                "ALTER TABLE conversations ADD COLUMN speech_sub TEXT DEFAULT ''",
                "ALTER TABLE memories ADD COLUMN embedding BLOB",
                "ALTER TABLE memories ADD COLUMN last_accessed REAL DEFAULT 0",
                "ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 0",
            ):
                try:
                    conn.execute(ddl)
                except Exception:
                    pass

            # 旧バージョンの固定初期シードが残っている場合は整理する
            conn.execute(
                """
                DELETE FROM memories
                WHERE category IN ('system', 'profile')
                  AND (
                    content LIKE '%私は自律型AIエージェント%'
                    OR content LIKE '%マスターは全体設計・概念構築を行う司令塔%'
                    OR content LIKE '%AIハヤト%'
                    OR content LIKE '%ノア%'
                  )
                """
            )

    @staticmethod
    def _summarize_persona_text(agent_name: str, persona_text: str) -> str:
        """ペルソナ設定テキストから長期記憶表示用の簡潔な要約文を生成する。"""
        resolved = persona_text.replace("{agent_name}", agent_name).strip()
        lines = [
            re.sub(r"^[-*・\s]+", "", line.strip())
            for line in resolved.splitlines()
            if line.strip() and not line.strip().startswith("【")
        ]
        if not lines:
            return f"名称: {agent_name}"
        summary = " / ".join(lines[:3])
        return summary[:160] + ("…" if len(summary) > 160 else "")

    def sync_with_persona(
        self, agent_name: str, persona_agent: str, persona_vtuber: str
    ) -> None:
        """
        UIのペルソナ設定が変更・初期化された際、SQLiteの長期記憶内のペルソナ定義レコードを
        最新のペルソナ設定内容と完全に一致させる。
        """
        now = time.time()
        agent_summary = self._summarize_persona_text(agent_name, persona_agent)
        vtuber_summary = self._summarize_persona_text(agent_name, persona_vtuber)

        with self._get_conn() as conn:
            conn.execute("DELETE FROM memories WHERE category IN ('persona_agent', 'persona_vtuber')")
            conn.executemany(
                "INSERT INTO memories (category, content, importance, created_at) VALUES (?, ?, ?, ?)",
                [
                    ("persona_vtuber", f"[VTuber設定] {vtuber_summary}", 5, now),
                    ("persona_agent", f"[AIエージェント設定] {agent_summary}", 5, now),
                ],
            )

    def log_conversation(
        self,
        mode: str,
        source: str,
        user_name: str,
        user_input: str,
        thought: str,
        emotion: str,
        agent_speech: str,
        evidences: list[dict[str, str]] | None = None,
        session_key: str = "",
        speech_sub: str = "",
    ) -> None:
        """1ターンの対話結果および参照根拠データを履歴テーブルに保存する。"""
        ev_json = json.dumps(evidences or [], ensure_ascii=False)
        s_key = (session_key or "").strip()[:120]
        s_sub = (speech_sub or "").strip()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO conversations (mode, source, user_name, user_input, thought, emotion, agent_speech, speech_sub, evidences_json, session_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mode, source, user_name, user_input, thought, emotion, agent_speech, s_sub, ev_json, s_key, time.time()),
            )

    def get_recent_conversations(
        self, limit: int = 8, mode: str | None = None, session_key: str | None = ""
    ) -> list[dict[str, Any]]:
        """直近の会話履歴を時系列順で取得する（modeおよび任意session_keyで分離抽出）。"""
        if limit <= 0:
            return []
        s_filter = "" if session_key is None else (session_key or "").strip()[:120]
        with self._get_conn() as conn:
            if mode == "agent":
                if session_key is None:
                    rows = conn.execute(
                        "SELECT * FROM conversations WHERE mode IN ('agent', 'hayato') ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM conversations WHERE mode IN ('agent', 'hayato') AND COALESCE(session_key, '') = ? ORDER BY id DESC LIMIT ?",
                        (s_filter, limit),
                    ).fetchall()
            elif mode:
                if session_key is None:
                    rows = conn.execute(
                        "SELECT * FROM conversations WHERE mode = ? ORDER BY id DESC LIMIT ?",
                        (mode, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM conversations WHERE mode = ? AND COALESCE(session_key, '') = ? ORDER BY id DESC LIMIT ?",
                        (mode, s_filter, limit),
                    ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM conversations ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        result: list[dict[str, Any]] = []
        for r in reversed(rows):
            d = dict(r)
            try:
                raw_ev = d.get("evidences_json") or "[]"
                parsed_ev = json.loads(raw_ev)
                d["evidences"] = parsed_ev if isinstance(parsed_ev, list) else []
            except Exception:
                d["evidences"] = []
            result.append(d)
        return result

    def clear_conversations(self, mode: str | None = None, session_key: str | None = None) -> None:
        """対話・配信コメント履歴をクリアする（modeおよび任意session_key指定対応）。"""
        with self._get_conn() as conn:
            if session_key is not None and mode:
                conn.execute(
                    "DELETE FROM conversations WHERE mode = ? AND COALESCE(session_key, '') = ?",
                    (mode, session_key.strip()[:120]),
                )
            elif mode == "agent":
                conn.execute("DELETE FROM conversations WHERE mode IN ('agent', 'hayato')")
            elif mode:
                conn.execute("DELETE FROM conversations WHERE mode = ?", (mode,))
            else:
                conn.execute("DELETE FROM conversations")

    # ===== 常連リスナー記憶（視聴者プロファイル） =====
    VISIT_GAP_SEC = 3 * 3600  # 前回コメントから3時間以上空いたら「新しい来訪」としてカウント

    def touch_viewer(self, user_name: str, platform: str = "local", is_superchat: bool = False) -> dict[str, Any]:
        """
        視聴者のコメント受信を記録し、更新前の状態（初見判定・前回来訪からの経過）を含むプロファイルを返す。
        """
        name = (user_name or "").strip()[:80]
        if not name:
            return {}
        now = time.time()
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM viewer_profiles WHERE user_name = ?", (name,)).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO viewer_profiles (user_name, platform, first_seen, last_seen, visit_count, comment_count, superchat_count, note)
                    VALUES (?, ?, ?, ?, 1, 1, ?, '')
                    """,
                    (name, platform, now, now, 1 if is_superchat else 0),
                )
                return {
                    "user_name": name,
                    "is_first_time": True,
                    "visit_count": 1,
                    "comment_count": 1,
                    "superchat_count": 1 if is_superchat else 0,
                    "last_seen_ago_min": None,
                    "note": "",
                }
            prev = dict(row)
            new_visit = (now - float(prev["last_seen"])) >= self.VISIT_GAP_SEC
            visit_count = int(prev["visit_count"]) + (1 if new_visit else 0)
            comment_count = int(prev["comment_count"]) + 1
            sc_count = int(prev["superchat_count"]) + (1 if is_superchat else 0)
            conn.execute(
                """
                UPDATE viewer_profiles
                SET last_seen = ?, visit_count = ?, comment_count = ?, superchat_count = ?, platform = ?
                WHERE user_name = ?
                """,
                (now, visit_count, comment_count, sc_count, platform, name),
            )
        return {
            "user_name": name,
            "is_first_time": False,
            "is_new_visit": new_visit,
            "visit_count": visit_count,
            "comment_count": comment_count,
            "superchat_count": sc_count,
            "last_seen_ago_min": round((now - float(prev["last_seen"])) / 60.0, 1),
            "note": prev.get("note") or "",
        }

    def set_viewer_note(self, user_name: str, note: str) -> None:
        """視聴者の特徴メモ（最大120文字）を更新する。"""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE viewer_profiles SET note = ? WHERE user_name = ?",
                (str(note).strip()[:120], (user_name or "").strip()[:80]),
            )

    def list_viewers(self, limit: int = 50) -> list[dict[str, Any]]:
        """最近コメントした順に視聴者プロファイルを返す。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM viewer_profiles ORDER BY last_seen DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["last_seen_str"] = datetime.datetime.fromtimestamp(d["last_seen"]).strftime("%m/%d %H:%M")
            result.append(d)
        return result

    def delete_viewer(self, user_name: str) -> None:
        """指定視聴者のプロファイルを削除する。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM viewer_profiles WHERE user_name = ?", (user_name,))

    def clear_viewers(self) -> None:
        """全視聴者プロファイルを削除する。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM viewer_profiles")

    def save_memory(self, category: str, content: str, importance: int = 3) -> int:
        """長期記憶ファクトを新規保存する（意味ベクトル付与・同一内容があれば更新）。"""
        imp = max(1, min(5, int(importance)))
        cat = category.strip()
        cnt = content.strip()
        now = time.time()
        emb_bytes = _vec_to_bytes(compute_semantic_vector(cnt))
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM memories WHERE content = ? AND category = ?",
                (cnt, cat),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memories SET importance = ?, created_at = ?, embedding = ? WHERE id = ?",
                    (imp, now, emb_bytes, existing["id"]),
                )
                return int(existing["id"])
            cur = conn.execute(
                """
                INSERT INTO memories (category, content, importance, created_at, embedding, last_accessed, access_count)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (cat, cnt, imp, now, emb_bytes, now),
            )
            return int(cur.lastrowid or 0)

    def update_memory(
        self, memory_id: int, category: str | None = None, content: str | None = None, importance: int | None = None
    ) -> bool:
        """指定IDの長期記憶の内容や重要度（★1〜5）を更新し、意味ベクトルを再計算する。"""
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if not row:
                return False
            new_cat = category.strip() if category else row["category"]
            new_content = content.strip() if content else row["content"]
            new_imp = max(1, min(5, int(importance))) if importance is not None else row["importance"]
            emb_bytes = _vec_to_bytes(compute_semantic_vector(new_content))
            conn.execute(
                "UPDATE memories SET category = ?, content = ?, importance = ?, embedding = ? WHERE id = ?",
                (new_cat, new_content, new_imp, emb_bytes, memory_id),
            )
            return True

    def search_memories(self, keyword: str = "", limit: int = 12) -> list[dict[str, Any]]:
        """長期記憶を検索または重要度順で取得する（バイナリembedding列は除外してJSON安全化）。"""
        cols = "id, category, content, importance, created_at, COALESCE(last_accessed, 0) AS last_accessed, COALESCE(access_count, 0) AS access_count"
        with self._get_conn() as conn:
            if keyword.strip():
                rows = conn.execute(
                    f"SELECT {cols} FROM memories WHERE content LIKE ? OR category LIKE ? ORDER BY importance DESC, id DESC LIMIT ?",
                    (f"%{keyword}%", f"%{keyword}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {cols} FROM memories ORDER BY importance DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def search_memories_semantic(
        self, query: str, limit: int = 8, exclude_persona: bool = True
    ) -> list[dict[str, Any]]:
        """
        ローカル意味ベクトル（Sudachi形態素＋概念シノニム＋2-gram）と
        キーワード一致・重要度・参照頻度を掛け合わせたハイブリッド検索を行う。
        """
        q = (query or "").strip()
        q_vec = compute_semantic_vector(q)
        q_lower = q.lower()
        q_words = [w for w in re.split(r"[\s、。！？,]+", q_lower) if len(w) >= 2]
        now = time.time()

        with self._get_conn() as conn:
            where_clause = (
                "WHERE category NOT IN ('persona_agent', 'persona_vtuber')" if exclude_persona else ""
            )
            rows = conn.execute(
                f"SELECT id, category, content, importance, created_at, embedding, COALESCE(last_accessed, 0) AS last_accessed, COALESCE(access_count, 0) AS access_count FROM memories {where_clause}"
            ).fetchall()

            scored: list[tuple[float, dict[str, Any]]] = []
            backfill_updates: list[tuple[bytes, int]] = []

            for r in rows:
                d = dict(r)
                raw_emb = d.pop("embedding", None)
                vec = _bytes_to_vec(raw_emb)
                if vec is None:
                    vec = compute_semantic_vector(d["content"])
                    backfill_updates.append((_vec_to_bytes(vec), int(d["id"])))

                cos_sim = max(0.0, cosine_similarity(q_vec, vec)) if q else 0.0
                content_lower = (d.get("content") or "").lower()
                kw_hit = 0.0
                if q_lower and q_lower in content_lower:
                    kw_hit = 1.0
                elif q_words:
                    hits = sum(1 for w in q_words if w in content_lower)
                    kw_hit = min(1.0, hits / max(1, len(q_words)))

                imp_norm = max(0.0, min(1.0, float(d.get("importance") or 3) / 5.0))
                acc_bonus = min(1.0, float(d.get("access_count") or 0) / 10.0)
                total_score = (
                    (0.55 * cos_sim) + (0.25 * kw_hit) + (0.15 * imp_norm) + (0.05 * acc_bonus)
                    if q
                    else (0.75 * imp_norm + 0.25 * acc_bonus)
                )
                d["similarity_score"] = round(total_score, 4)
                scored.append((total_score, d))

            for emb_b, mid in backfill_updates:
                conn.execute("UPDATE memories SET embedding = ? WHERE id = ?", (emb_b, mid))

            scored.sort(key=lambda x: (x[0], x[1]["id"]), reverse=True)
            top_items = [item for _sc, item in scored[:limit]]

            if q and top_items:
                for it in top_items[:4]:
                    conn.execute(
                        "UPDATE memories SET last_accessed = ?, access_count = COALESCE(access_count, 0) + 1 WHERE id = ?",
                        (now, it["id"]),
                    )

        return top_items

    def consolidate_memories(self, sim_threshold: float = 0.84) -> dict[str, Any]:
        """
        重複・高類似の長期記憶を統合マージし、肥大化した低重要度・未参照ノイズを整理する
        （夜間 / アイドル時自動整理 Sleep Consolidation）。
        """
        now = time.time()
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id, category, content, importance, created_at, embedding,
                       COALESCE(last_accessed, 0) AS last_accessed, COALESCE(access_count, 0) AS access_count
                FROM memories
                WHERE category NOT IN ('persona_agent', 'persona_vtuber')
                ORDER BY importance DESC, id DESC
                """
            ).fetchall()

            items = [dict(r) for r in rows]
            before_count = len(items)
            if before_count <= 1:
                return {
                    "ok": True,
                    "before_count": before_count,
                    "after_count": before_count,
                    "merged_count": 0,
                    "pruned_count": 0,
                    "actions": [],
                }

            vecs: dict[int, np.ndarray] = {}
            for it in items:
                v = _bytes_to_vec(it.get("embedding"))
                if v is None:
                    v = compute_semantic_vector(it["content"])
                    conn.execute("UPDATE memories SET embedding = ? WHERE id = ?", (_vec_to_bytes(v), it["id"]))
                vecs[int(it["id"])] = v

            removed_ids: set[int] = set()
            merged_count = 0
            actions: list[str] = []

            for i in range(len(items)):
                base = items[i]
                bid = int(base["id"])
                if bid in removed_ids:
                    continue
                for j in range(i + 1, len(items)):
                    cand = items[j]
                    cid = int(cand["id"])
                    if cid in removed_ids:
                        continue
                    sim = cosine_similarity(vecs[bid], vecs[cid])
                    b_txt = (base["content"] or "").strip()
                    c_txt = (cand["content"] or "").strip()
                    is_subset = (len(c_txt) >= 6 and c_txt in b_txt) or (len(b_txt) >= 6 and b_txt in c_txt)
                    if sim >= sim_threshold or is_subset:
                        # より詳細（長い）内容と高い重要度を保持して統合
                        kept_txt = b_txt if len(b_txt) >= len(c_txt) else c_txt
                        new_imp = max(int(base["importance"] or 3), int(cand["importance"] or 3))
                        new_acc = int(base["access_count"] or 0) + int(cand["access_count"] or 0)
                        conn.execute(
                            "UPDATE memories SET content = ?, importance = ?, access_count = ?, embedding = ? WHERE id = ?",
                            (kept_txt, new_imp, new_acc, _vec_to_bytes(compute_semantic_vector(kept_txt)), bid),
                        )
                        conn.execute("DELETE FROM memories WHERE id = ?", (cid,))
                        removed_ids.add(cid)
                        merged_count += 1
                        base["content"] = kept_txt
                        actions.append(f"統合: 「{c_txt[:28]}」→「{kept_txt[:32]}」 (sim={sim:.2f})")

            # 記憶件数が 45 件を超える場合、30日以上未参照かつ重要度2以下の古い雑メモを整理
            pruned_count = 0
            remaining_count = before_count - len(removed_ids)
            if remaining_count > 45:
                stale_cutoff = now - 30 * 86400
                stale_rows = conn.execute(
                    """
                    SELECT id, content FROM memories
                    WHERE category NOT IN ('persona_agent', 'persona_vtuber')
                      AND importance <= 2
                      AND COALESCE(last_accessed, created_at) < ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (stale_cutoff, remaining_count - 40),
                ).fetchall()
                for sr in stale_rows:
                    conn.execute("DELETE FROM memories WHERE id = ?", (sr["id"],))
                    pruned_count += 1
                    actions.append(f"整理: 古い低重要度メモ「{str(sr['content'])[:28]}」")

            after_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE category NOT IN ('persona_agent', 'persona_vtuber')"
            ).fetchone()[0]

        return {
            "ok": True,
            "before_count": before_count,
            "after_count": after_count,
            "merged_count": merged_count,
            "pruned_count": pruned_count,
            "actions": actions[:12],
        }

    def delete_memory(self, memory_id: int) -> None:
        """指定IDの記憶を削除する。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))

    def clear_user_memories(self) -> None:
        """ペルソナ同期レコード以外の獲得記憶をすべてクリアする。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM memories WHERE category NOT IN ('persona_agent', 'persona_vtuber')")

    @staticmethod
    def _parse_due_time(due_minutes: float = 0, due_iso: str = "") -> float | None:
        """分数指定または日時文字列（YYYY-MM-DDTHH:MM / HH:MM）からUNIXタイムスタンプを算出する。"""
        now = time.time()
        if due_iso and due_iso.strip():
            s = due_iso.strip()
            try:
                # HH:MM 形式の場合
                if re.match(r"^\d{1,2}:\d{2}$", s):
                    today = datetime.date.today()
                    hh, mm = map(int, s.split(":"))
                    dt = datetime.datetime(today.year, today.month, today.day, hh, mm)
                    if dt.timestamp() <= now:
                        dt += datetime.timedelta(days=1)
                    return dt.timestamp()
                # ISO 日時形式
                dt = datetime.datetime.fromisoformat(s.replace(" ", "T"))
                return dt.timestamp()
            except Exception:
                pass
        if due_minutes and due_minutes > 0:
            return now + float(due_minutes) * 60.0
        return None

    def add_task(
        self, title: str, due_minutes: float = 0, due_iso: str = "", repeat_rule: str = "none"
    ) -> int:
        """自律監視タスク・カレンダー予定・リマインダーを追加する。"""
        now = time.time()
        due_at = self._parse_due_time(due_minutes=due_minutes, due_iso=due_iso)
        rule = repeat_rule if repeat_rule in ("none", "hourly", "daily", "weekly") else "none"
        with self._get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO tasks (title, status, due_at, repeat_rule, notified, created_at) VALUES (?, 'pending', ?, ?, 0, ?)",
                (title.strip(), due_at, rule, now),
            )
            return int(cur.lastrowid or 0)

    def get_tasks(self, status: str = "all") -> list[dict[str, Any]]:
        """タスク・予定一覧を取得する（日時フォーマット文字列付き）。"""
        with self._get_conn() as conn:
            if status == "all":
                rows = conn.execute("SELECT * FROM tasks ORDER BY status DESC, COALESCE(due_at, 9999999999) ASC, id DESC LIMIT 30").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY COALESCE(due_at, 9999999999) ASC, id DESC LIMIT 30",
                    (status,),
                ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("due_at"):
                dt = datetime.datetime.fromtimestamp(d["due_at"])
                d["due_str"] = dt.strftime("%m/%d %H:%M")
            else:
                d["due_str"] = ""
            result.append(d)
        return result

    def complete_task(self, task_id: int) -> bool:
        """タスクを完了状態に更新する。"""
        with self._get_conn() as conn:
            cur = conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,))
            return cur.rowcount > 0

    def delete_task(self, task_id: int) -> None:
        """指定IDのタスクを削除する。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def snooze_task(self, task_id: int, snooze_minutes: float = 5.0) -> bool:
        """タスクの通知時刻を指定分数後に再設定（スヌーズ）する。"""
        new_due = time.time() + max(1.0, float(snooze_minutes)) * 60.0
        with self._get_conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET due_at = ?, notified = 0, status = 'pending' WHERE id = ?",
                (new_due, task_id),
            )
            return cur.rowcount > 0

    def pop_due_tasks(self) -> list[dict[str, Any]]:
        """期限が到来しており未通知のタスクを取得し、繰り返し設定があれば次回期限へ更新する。"""
        now = time.time()
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'pending' AND due_at IS NOT NULL AND due_at <= ? AND notified = 0",
                (now,),
            ).fetchall()
            due_list = [dict(r) for r in rows]
            for t in due_list:
                rule = t.get("repeat_rule") or "none"
                if rule == "hourly":
                    conn.execute(
                        "UPDATE tasks SET due_at = ?, notified = 0 WHERE id = ?",
                        (now + 3600.0, t["id"]),
                    )
                elif rule == "daily":
                    conn.execute(
                        "UPDATE tasks SET due_at = ?, notified = 0 WHERE id = ?",
                        (now + 86400.0, t["id"]),
                    )
                elif rule == "weekly":
                    conn.execute(
                        "UPDATE tasks SET due_at = ?, notified = 0 WHERE id = ?",
                        (now + 604800.0, t["id"]),
                    )
                else:
                    conn.execute("UPDATE tasks SET notified = 1 WHERE id = ?", (t["id"],))
        return due_list

    # ===== 定時自律ワークフロー (Heartbeat / 朝ブリーフィング / Web差分監視) =====
    @staticmethod
    def _compute_next_workflow_run(schedule_kind: str, schedule_value: str, base_ts: float | None = None) -> float:
        """ワークフロー種別とスケジュール文字列から次回実行UNIXタイムスタンプを算出する。"""
        now = base_ts if base_ts is not None else time.time()
        kind = (schedule_kind or "interval").lower()
        val = (schedule_value or "").strip()
        if kind == "daily":
            m = re.match(r"^(\d{1,2}):(\d{2})$", val)
            if m:
                hh, mm = max(0, min(23, int(m.group(1)))), max(0, min(59, int(m.group(2))))
                today = datetime.date.today()
                dt = datetime.datetime(today.year, today.month, today.day, hh, mm)
                if dt.timestamp() <= now + 5:
                    dt += datetime.timedelta(days=1)
                return dt.timestamp()
        try:
            mins = max(1.0, float(val))
        except Exception:
            mins = 60.0
        return now + mins * 60.0

    def add_workflow(
        self,
        name: str,
        workflow_type: str = "custom_prompt",
        schedule_kind: str = "daily",
        schedule_value: str = "08:00",
        target_url: str = "",
        prompt: str = "",
        enabled: bool = True,
    ) -> int:
        """定時自律ワークフロー（朝ブリーフィング・Web更新監視等）を登録する。"""
        now = time.time()
        next_run = self._compute_next_workflow_run(schedule_kind, schedule_value, now)
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO workflows (name, workflow_type, schedule_kind, schedule_value, target_url, prompt, enabled, last_run_at, last_hash, next_run_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?)
                """,
                (
                    name.strip()[:80],
                    workflow_type.strip(),
                    schedule_kind.strip(),
                    schedule_value.strip(),
                    target_url.strip(),
                    prompt.strip(),
                    1 if enabled else 0,
                    next_run,
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_workflows(self) -> list[dict[str, Any]]:
        """登録済みワークフロー一覧を返す。"""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM workflows ORDER BY id ASC").fetchall()
        res = []
        for r in rows:
            d = dict(r)
            d["enabled"] = bool(d.get("enabled"))
            d["next_run_str"] = (
                datetime.datetime.fromtimestamp(d["next_run_at"]).strftime("%m/%d %H:%M")
                if d.get("next_run_at")
                else "--"
            )
            d["last_run_str"] = (
                datetime.datetime.fromtimestamp(d["last_run_at"]).strftime("%m/%d %H:%M")
                if d.get("last_run_at")
                else "未実行"
            )
            res.append(d)
        return res

    def get_workflow(self, workflow_id: int) -> dict[str, Any] | None:
        """ID指定でワークフローを1件取得する。"""
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        return dict(row) if row else None

    def toggle_workflow(self, workflow_id: int, enabled: bool | None = None) -> bool:
        """ワークフローの有効/無効を切り替える。"""
        with self._get_conn() as conn:
            row = conn.execute("SELECT enabled FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
            if not row:
                return False
            new_val = (1 if enabled else 0) if enabled is not None else (0 if row["enabled"] else 1)
            conn.execute("UPDATE workflows SET enabled = ? WHERE id = ?", (new_val, workflow_id))
            return True

    def delete_workflow(self, workflow_id: int) -> None:
        """指定IDのワークフローを削除する。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))

    def pop_due_workflows(self) -> list[dict[str, Any]]:
        """実行予定時刻が到来した有効ワークフローを取得し、次回予定時刻へ更新する。"""
        now = time.time()
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workflows WHERE enabled = 1 AND next_run_at > 0 AND next_run_at <= ?",
                (now,),
            ).fetchall()
            due = [dict(r) for r in rows]
            for wf in due:
                nxt = self._compute_next_workflow_run(wf["schedule_kind"], wf["schedule_value"], now)
                conn.execute(
                    "UPDATE workflows SET next_run_at = ?, last_run_at = ? WHERE id = ?",
                    (nxt, now, wf["id"]),
                )
        return due

    def update_workflow_hash(self, workflow_id: int, content_hash: str) -> None:
        """Web監視ワークフローの最終ハッシュと実行時刻を更新する。"""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE workflows SET last_hash = ?, last_run_at = ? WHERE id = ?",
                (content_hash, time.time(), workflow_id),
            )

    # ===== 匿名お便り・質問箱 (VTuberコーナー用) =====
    def add_mailbag(self, question: str, sender: str = "匿名リスナー", source: str = "local") -> int:
        """匿名お便り・質問箱アイテムを追加する。"""
        q = (question or "").strip()
        if not q:
            return 0
        with self._get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO mailbag_items (sender, question, source, status, created_at) VALUES (?, ?, ?, 'unread', ?)",
                ((sender or "匿名リスナー").strip()[:60], q[:600], source.strip()[:30], time.time()),
            )
            return int(cur.lastrowid or 0)

    def list_mailbag(self, status: str = "all", limit: int = 50) -> list[dict[str, Any]]:
        """お便り一覧を取得する（未読優先・新しい順）。"""
        with self._get_conn() as conn:
            if status in ("unread", "read"):
                rows = conn.execute(
                    "SELECT * FROM mailbag_items WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM mailbag_items ORDER BY CASE WHEN status='unread' THEN 0 ELSE 1 END, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            d["created_str"] = datetime.datetime.fromtimestamp(d["created_at"]).strftime("%m/%d %H:%M")
            res.append(d)
        return res

    def pop_next_unread_mailbag(self) -> dict[str, Any] | None:
        """最も古い未読お便りを1件取り出して既読に更新する。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM mailbag_items WHERE status = 'unread' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute("UPDATE mailbag_items SET status = 'read' WHERE id = ?", (row["id"],))
            d = dict(row)
            d["status"] = "read"
            return d

    def delete_mailbag(self, item_id: int) -> None:
        """指定IDのお便りを削除する。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM mailbag_items WHERE id = ?", (item_id,))

    # ===== 配信セッション・アーカイブハイライトログ =====
    def start_stream_session(self, title: str = "ニアのライブ配信") -> int:
        """新規配信セッションを開始し、過去の未終了セッションがあれば閉じる。"""
        now = time.time()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE stream_sessions SET status = 'ended', ended_at = ? WHERE status = 'live'",
                (now,),
            )
            cur = conn.execute(
                "INSERT INTO stream_sessions (title, started_at, status) VALUES (?, ?, 'live')",
                ((title or "ニアのライブ配信").strip()[:120], now),
            )
            return int(cur.lastrowid or 0)

    def get_active_stream_session(self) -> dict[str, Any] | None:
        """現在記録中のライブ配信セッションを返す。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM stream_sessions WHERE status = 'live' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def add_stream_highlight(self, event_kind: str, label: str, detail: str = "") -> None:
        """記録中配信セッションがあれば経過秒数付きでハイライトを記録する。"""
        session = self.get_active_stream_session()
        if not session:
            return
        now = time.time()
        elapsed = max(0.0, now - float(session["started_at"]))
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO stream_highlights (session_id, elapsed_sec, event_kind, label, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session["id"], elapsed, event_kind[:40], label[:120], detail[:400], now),
            )

    def get_stream_highlights(self, session_id: int) -> list[dict[str, Any]]:
        """指定配信セッションの全ハイライト一覧を経過時間順で返す。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM stream_highlights WHERE session_id = ? ORDER BY elapsed_sec ASC, id ASC",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def end_stream_session(
        self, session_id: int, summary_md: str = "", chapters_text: str = "", archive_path: str = ""
    ) -> None:
        """配信セッションを終了状態にし、生成されたチャプター・要約・ファイルパスを保存する。"""
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE stream_sessions
                SET status = 'ended', ended_at = ?, summary_md = ?, chapters_text = ?, archive_path = ?
                WHERE id = ?
                """,
                (time.time(), summary_md, chapters_text, archive_path, session_id),
            )

    def list_stream_sessions(self, limit: int = 10) -> list[dict[str, Any]]:
        """過去の配信セッション一覧を新しい順で返す。"""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM stream_sessions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        res = []
        for r in rows:
            d = dict(r)
            d["started_str"] = datetime.datetime.fromtimestamp(d["started_at"]).strftime("%Y-%m-%d %H:%M")
            res.append(d)
        return res


memory_manager = MemoryManager()
