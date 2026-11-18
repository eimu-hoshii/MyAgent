"""
ローカルドキュメント解析（PDF / Office / コード / テキスト / 音声）、
許可ディレクトリ限定ファイル読み書き検索（WorkspaceFileManager）、
および SQLite FTS5 日本語バイグラム対応ローカル RAG エンジン（LocalRAGEngine）。
"""

import contextlib
import io
import os
import re
import sqlite3
import time
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Iterator
from backend.config import BASE_DIR, DATA_DIR, DB_PATH, RAG_DOCS_DIR, config

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".csv",
    ".tsv",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".html",
    ".css",
    ".xml",
    ".sql",
    ".bat",
    ".ps1",
    ".vbs",
    ".sh",
    ".log",
    ".rst",
}

AUDIO_EXTENSIONS = {".ogg", ".oga", ".mp3", ".wav", ".m4a", ".flac", ".webm"}
DOC_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx", ".xlsx", ".pptx"}

SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "audio_cache",
    "vision_captures",
    "picture",
    ".mypy_cache",
}

SENSITIVE_FILE_NAMES = {
    "secrets.json",
    ".env",
    ".env.local",
    "agent_memory.db",
    "credentials.json",
    "google_token.json",
}


def _is_sensitive_path(real_path: str) -> bool:
    """機密ファイル・DBファイルへのツール直接アクセスをブロックする。"""
    base = os.path.basename(real_path).lower()
    if base in SENSITIVE_FILE_NAMES:
        return True
    if base.endswith((".key", ".pem", ".pfx", ".p12", ".db", ".sqlite", ".sqlite3")):
        return True
    return False


def _tokenize_for_fts(text: str) -> str:
    """
    日本語と英数字が混在するテキストを SQLite FTS5 向けにトークン化する。
    英数字語はそのまま保持し、日本語文字（ひらがな・カタカナ・漢字）の連続文字列は
    2-gram（バイグラム）展開することで日本語の部分一致検索精度を確保する。
    """
    if not text:
        return ""
    tokens: list[str] = []
    # 英数字・記号区切りトークン
    for alnum in re.findall(r"[A-Za-z0-9_.\-]{2,}", text.lower()):
        tokens.append(alnum)

    # 日本語塊を2-gram展開（1文字塊も残す）
    for jp_seq in re.findall(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFー]+", text):
        if len(jp_seq) == 1:
            tokens.append(jp_seq)
        else:
            for i in range(len(jp_seq) - 1):
                tokens.append(jp_seq[i : i + 2])
    return " ".join(tokens)


def extract_text_from_bytes(raw_bytes: bytes, filename: str, max_chars: int = 12000) -> tuple[bool, str]:
    """
    拡張子に応じてバイト列（テキスト・コード・PDF・Word・Excel・PowerPoint）からテキストを抽出する。
    戻り値: (成功フラグ, 抽出テキストまたはエラー文)
    """
    ext = os.path.splitext(filename or "")[1].lower()
    try:
        if ext == ".pdf":
            import pymupdf

            doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
            pages_text: list[str] = []
            total = 0
            for idx, page in enumerate(doc):
                txt = (page.get_text() or "").strip()
                if txt:
                    pages_text.append(f"[Page {idx + 1}]\n{txt}")
                    total += len(txt)
                    if total >= max_chars:
                        break
            doc.close()
            joined = "\n\n".join(pages_text).strip()
            return (True, joined[:max_chars]) if joined else (False, "PDFから抽出可能なテキストが見つかりません（画像スキャンPDFの可能性があります）。")

        if ext == ".docx":
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                xml_data = zf.read("word/document.xml")
            root = ET.fromstring(xml_data)
            paragraphs: list[str] = []
            for p_node in root.iter():
                if p_node.tag.endswith("}p"):
                    texts = [t.text for t in p_node.iter() if t.tag.endswith("}t") and t.text]
                    if texts:
                        paragraphs.append("".join(texts))
            joined = "\n".join(paragraphs).strip()
            return (True, joined[:max_chars]) if joined else (False, "DOCX内にテキストがありません。")

        if ext == ".xlsx":
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
            lines: list[str] = []
            for sheet_name in wb.sheetnames[:6]:
                ws = wb[sheet_name]
                lines.append(f"[Sheet: {sheet_name}]")
                row_count = 0
                for row in ws.iter_rows(values_only=True):
                    vals = [str(v) if v is not None else "" for v in row[:20]]
                    if any(vals):
                        lines.append(" | ".join(vals))
                        row_count += 1
                    if row_count >= 120 or sum(len(x) for x in lines) >= max_chars:
                        break
            wb.close()
            joined = "\n".join(lines).strip()
            return (True, joined[:max_chars]) if joined else (False, "Excelファイル内にデータがありません。")

        if ext == ".pptx":
            from pptx import Presentation

            prs = Presentation(io.BytesIO(raw_bytes))
            slides_text: list[str] = []
            for idx, slide in enumerate(prs.slides):
                chunks = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text and shape.text.strip():
                        chunks.append(shape.text.strip())
                if chunks:
                    slides_text.append(f"[Slide {idx + 1}]\n" + "\n".join(chunks))
                if sum(len(x) for x in slides_text) >= max_chars:
                    break
            joined = "\n\n".join(slides_text).strip()
            return (True, joined[:max_chars]) if joined else (False, "PowerPoint内にテキストがありません。")

        # 一般テキスト・ソースコード（UTF-8 → CP932 フォールバック）
        for enc in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "latin-1"):
            try:
                decoded = raw_bytes.decode(enc)
                if "\x00" in decoded[:512]:
                    return False, "バイナリファイルのためテキストとして読み込めません。"
                return True, decoded[:max_chars]
            except UnicodeDecodeError:
                continue
        return False, "文字コードの判別に失敗しました。"
    except Exception as exc:
        return False, f"ファイル解析エラー ({filename}): {exc}"


def extract_text_from_file(file_path: str, max_chars: int = 12000) -> tuple[bool, str]:
    """ローカルファイルからテキストを抽出する（PDF / Office / テキスト対応）。"""
    if not os.path.isfile(file_path):
        return False, f"ファイルが存在しません: {file_path}"
    try:
        with open(file_path, "rb") as f:
            raw = f.read(5 * 1024 * 1024)  # 最大5MBまで安全読込
        return extract_text_from_bytes(raw, file_path, max_chars=max_chars)
    except Exception as exc:
        return False, f"ファイル読込エラー: {exc}"


class WorkspaceFileManager:
    """設定された許可ディレクトリ範囲内に厳格制限したファイル読み書き・検索クラス。"""

    @staticmethod
    def resolve_allowed_roots() -> list[str]:
        """設定中の許可フォルダ一覧を正規化実パスで返す。"""
        raw_dirs = config.file_allowed_dirs or [BASE_DIR]
        roots: list[str] = []
        for d in raw_dirs:
            expanded = os.path.expandvars(str(d).strip())
            if expanded:
                real = os.path.realpath(expanded)
                if real not in roots:
                    roots.append(real)
        return roots or [os.path.realpath(BASE_DIR)]

    @classmethod
    def resolve_safe_path(cls, raw_path: str) -> tuple[bool, str]:
        """相対パス・絶対パスを解決し、許可ルート内かつ非機密パスか検証する。"""
        p = (raw_path or "").strip()
        if not p:
            return False, "パスが空です。"
        expanded = os.path.expandvars(p)
        if not os.path.isabs(expanded):
            expanded = os.path.join(BASE_DIR, expanded)
        real_target = os.path.realpath(expanded)

        if _is_sensitive_path(real_target):
            return False, f"セキュリティ保護対象ファイルへのアクセスは禁止されています: {os.path.basename(real_target)}"

        roots = cls.resolve_allowed_roots()
        for root in roots:
            try:
                if os.path.commonpath([real_target, root]) == root:
                    return True, real_target
            except ValueError:
                continue
        allowed_label = ", ".join(roots)
        return False, f"許可ディレクトリ外のパスです: {real_target}（許可ルート: {allowed_label}）"

    @classmethod
    def list_dir(cls, dir_path: str = "") -> str:
        """許可ディレクトリ内のファイル・フォルダ一覧を返す。"""
        target = dir_path.strip() or cls.resolve_allowed_roots()[0]
        ok, resolved = cls.resolve_safe_path(target)
        if not ok:
            return f"【アクセス拒否】{resolved}"
        if not os.path.isdir(resolved):
            return f"ディレクトリが見つかりません: {resolved}"

        entries = []
        try:
            for name in sorted(os.listdir(resolved))[:80]:
                full = os.path.join(resolved, name)
                if _is_sensitive_path(full):
                    continue
                if os.path.isdir(full):
                    entries.append(f"📁 {name}/")
                else:
                    size_kb = round(os.path.getsize(full) / 1024.0, 1)
                    entries.append(f"📄 {name} ({size_kb} KB)")
        except Exception as exc:
            return f"一覧取得エラー: {exc}"
        return f"【ディレクトリ一覧: {resolved}】\n" + ("\n".join(entries) if entries else "（空のディレクトリ）")

    @classmethod
    def read_file(cls, file_path: str, offset: int = 0, limit_chars: int = 4000) -> str:
        """許可ファイル（テキスト/PDF/Office対応）の内容を読み取る。"""
        if not config.file_tools_enabled:
            return "ファイル操作ツールは設定で無効化されています。"
        ok, resolved = cls.resolve_safe_path(file_path)
        if not ok:
            return f"【アクセス拒否】{resolved}"
        success, content = extract_text_from_file(resolved, max_chars=max(20000, offset + limit_chars))
        if not success:
            return f"【ファイル読込失敗】{content}"
        slice_txt = content[offset : offset + limit_chars]
        more = " ...（続きあり）" if len(content) > offset + limit_chars else ""
        return f"【ファイル読込: {resolved} (offset={offset}, len={len(slice_txt)})】\n{slice_txt}{more}"

    @classmethod
    def write_file(cls, file_path: str, content: str, append: bool = False) -> str:
        """許可ディレクトリ内にファイルを新規作成・追記保存する。"""
        if not config.file_tools_enabled:
            return "ファイル操作ツールは設定で無効化されています。"
        ok, resolved = cls.resolve_safe_path(file_path)
        if not ok:
            return f"【アクセス拒否】{resolved}"
        ext = os.path.splitext(resolved)[1].lower()
        if ext in (".exe", ".dll", ".bat", ".ps1", ".vbs", ".cmd", ".reg", ".msi"):
            return f"安全のため実行可能スクリプト/バイナリ拡張子 ({ext}) への直接書き込みは制限されています。"
        try:
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            mode = "a" if append else "w"
            with open(resolved, mode, encoding="utf-8") as f:
                f.write(content)
            return f"【ファイル保存完了】{resolved} ({len(content)} 文字 / mode={'append' if append else 'overwrite'})"
        except Exception as exc:
            return f"【ファイル保存エラー】{exc}"

    @classmethod
    def search_files(cls, keyword: str, dir_path: str = "") -> str:
        """許可ディレクトリ配下のテキスト/コードファイルからキーワードを高速検索する。"""
        if not config.file_tools_enabled:
            return "ファイル操作ツールは設定で無効化されています。"
        kw = (keyword or "").strip()
        if not kw:
            return "検索キーワードが空です。"
        target_dir = dir_path.strip() or cls.resolve_allowed_roots()[0]
        ok, resolved = cls.resolve_safe_path(target_dir)
        if not ok:
            return f"【アクセス拒否】{resolved}"
        if not os.path.isdir(resolved):
            return f"ディレクトリが見つかりません: {resolved}"

        kw_lower = kw.lower()
        matches: list[str] = []
        scanned = 0
        for root, dirs, files in os.walk(resolved):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in TEXT_EXTENSIONS:
                    continue
                fpath = os.path.join(root, fn)
                if _is_sensitive_path(fpath) or os.path.getsize(fpath) > 512 * 1024:
                    continue
                scanned += 1
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        for lineno, line in enumerate(f, start=1):
                            if kw_lower in line.lower():
                                rel = os.path.relpath(fpath, resolved)
                                matches.append(f"- {rel}:{lineno} | {line.strip()[:120]}")
                                if len(matches) >= 20:
                                    return f"【ファイル内検索結果: '{kw}' (走査{scanned}件)】\n" + "\n".join(matches)
                except Exception:
                    continue
                if scanned >= 400:
                    break
        if not matches:
            return f"「{kw}」に一致する記述は {resolved} 配下で見つかりませんでした（走査{scanned}件）。"
        return f"【ファイル内検索結果: '{kw}' (走査{scanned}件)】\n" + "\n".join(matches)


class LocalRAGEngine:
    """SQLite FTS5 日本語バイグラム対応ローカルドキュメント RAG エンジン。"""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(RAG_DOCS_DIR, exist_ok=True)
        self._init_tables()

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

    def _init_tables(self) -> None:
        """RAG用メタデータテーブルおよびFTS5仮想テーブルを初期化する。"""
        with self._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rag_docs (
                    file_path TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    mtime REAL NOT NULL,
                    file_size INTEGER NOT NULL,
                    chunk_count INTEGER DEFAULT 0,
                    indexed_at REAL NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks USING fts5(
                    file_path UNINDEXED,
                    file_name UNINDEXED,
                    chunk_idx UNINDEXED,
                    raw_text,
                    search_tokens
                );
                """
            )

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 650, overlap: int = 100) -> list[str]:
        """長文テキストを指定文字数とオーバーラップで分割する。"""
        cleaned = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
        if not cleaned:
            return []
        chunks: list[str] = []
        pos = 0
        n = len(cleaned)
        while pos < n:
            end = min(n, pos + chunk_size)
            chunk = cleaned[pos:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= n:
                break
            pos = max(pos + 1, end - overlap)
        return chunks

    def index_directories(self, target_dirs: list[str] | None = None) -> dict[str, Any]:
        """対象ディレクトリのドキュメントをスキャンし、新規/更新分のみをインデックス化する。"""
        dirs = target_dirs if target_dirs is not None else (config.rag_dirs or [RAG_DOCS_DIR, BASE_DIR])
        indexed_files = 0
        skipped_files = 0
        total_chunks = 0
        allowed_exts = DOC_EXTENSIONS - {".log"}

        with self._get_conn() as conn:
            existing = {
                row["file_path"]: (float(row["mtime"]), int(row["file_size"]))
                for row in conn.execute("SELECT file_path, mtime, file_size FROM rag_docs").fetchall()
            }

            seen_paths: set[str] = set()
            for raw_dir in dirs:
                d_path = os.path.realpath(os.path.expandvars(str(raw_dir).strip()))
                if not os.path.isdir(d_path):
                    continue
                for root, subdirs, files in os.walk(d_path):
                    subdirs[:] = [s for s in subdirs if s not in SKIP_DIR_NAMES and not s.startswith(".")]
                    for fn in files:
                        ext = os.path.splitext(fn)[1].lower()
                        if ext not in allowed_exts:
                            continue
                        fpath = os.path.realpath(os.path.join(root, fn))
                        if fpath in seen_paths or _is_sensitive_path(fpath):
                            continue
                        seen_paths.add(fpath)
                        try:
                            stat = os.stat(fpath)
                        except OSError:
                            continue
                        if stat.st_size == 0 or stat.st_size > 4 * 1024 * 1024:
                            continue

                        prev = existing.get(fpath)
                        if prev and abs(prev[0] - stat.st_mtime) < 0.01 and prev[1] == stat.st_size:
                            skipped_files += 1
                            continue

                        ok, extracted = extract_text_from_file(fpath, max_chars=30000)
                        if not ok or not extracted.strip():
                            continue

                        chunks = self._chunk_text(extracted)
                        conn.execute("DELETE FROM rag_chunks WHERE file_path = ?", (fpath,))
                        for idx, ch in enumerate(chunks):
                            conn.execute(
                                "INSERT INTO rag_chunks (file_path, file_name, chunk_idx, raw_text, search_tokens) VALUES (?, ?, ?, ?, ?)",
                                (fpath, fn, idx, ch, _tokenize_for_fts(f"{fn} {ch}")),
                            )
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO rag_docs (file_path, file_name, mtime, file_size, chunk_count, indexed_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (fpath, fn, stat.st_mtime, stat.st_size, len(chunks), time.time()),
                        )
                        indexed_files += 1
                        total_chunks += len(chunks)

            doc_cnt = conn.execute("SELECT COUNT(*) FROM rag_docs").fetchone()[0]
            chunk_cnt = conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0]

        return {
            "ok": True,
            "indexed_files": indexed_files,
            "skipped_files": skipped_files,
            "added_chunks": total_chunks,
            "total_docs": doc_cnt,
            "total_chunks": chunk_cnt,
        }

    def get_stats(self) -> dict[str, Any]:
        """現在の RAG インデックス統計を返す。"""
        with self._get_conn() as conn:
            docs = conn.execute("SELECT COUNT(*) FROM rag_docs").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0]
        return {"total_docs": docs, "total_chunks": chunks, "rag_docs_dir": RAG_DOCS_DIR}

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """クエリに最も関連するローカルドキュメントのチャンクを上位K件返す。"""
        q = (query or "").strip()
        if not q:
            return []
        tokenized = _tokenize_for_fts(q)
        tokens = [t for t in tokenized.split() if t]

        with self._get_conn() as conn:
            results: list[dict[str, Any]] = []
            if tokens:
                # 単語が多い場合は OR 検索でマッチ幅を確保し、bm25 で関連度順に並べる
                fts_query = " OR ".join(f'"{t.replace(chr(34), "")}"' for t in tokens[:18])
                try:
                    rows = conn.execute(
                        """
                        SELECT file_path, file_name, chunk_idx, raw_text,
                               bm25(rag_chunks, 0.0, 0.0, 0.0, 1.0, 2.0) AS score
                        FROM rag_chunks
                        WHERE rag_chunks MATCH ?
                        ORDER BY score ASC
                        LIMIT ?
                        """,
                        (fts_query, top_k),
                    ).fetchall()
                    results = [dict(r) for r in rows]
                except Exception:
                    results = []

            if not results:
                rows = conn.execute(
                    "SELECT file_path, file_name, chunk_idx, raw_text, 0.0 AS score FROM rag_chunks WHERE raw_text LIKE ? OR file_name LIKE ? LIMIT ?",
                    (f"%{q}%", f"%{q}%", top_k),
                ).fetchall()
                results = [dict(r) for r in rows]

        return results


rag_engine = LocalRAGEngine()
