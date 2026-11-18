"""
OpenClaw 型 AIエージェント自律実行・外部連携・環境制御エンジン。
- 承認制シェル/コマンド実行 (HITL: Human-in-the-Loop 承認ゲート)
- Playwright ヘッドレスブラウザ操作（SPA閲覧・クリック・入力・スクショ）
- MCP (Model Context Protocol) JSON-RPC stdio クライアント
- GitHub / Google Calendar (OAuth & iCal URL) / Gmail 連携
- Windows メディア・音量制御 & Action Center トースト通知監視
- Webページ差分監視ハッシュチェック & 深掘りレポート保存
"""

import asyncio
import datetime
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Callable, Coroutine
import httpx
from backend.config import DATA_DIR, RESEARCH_REPORT_DIR, VISION_CAPTURE_DIR, BASE_DIR, config


# いかなる設定でも絶対実行を拒否する危険コマンドパターン
_HARD_BLOCKED_PATTERNS = [
    r"\bformat\s+[a-z]:",
    r"\bdiskpart\b",
    r"\bshutdown\b",
    r"\breg\s+delete\b",
    r"\brm\s+-rf\s+[/~\\]",
    r"\brmdir\s+/s\s+/q\s+[a-z]:\\",
    r"\bdel\s+.*secrets\.json",
    r"secrets\.json",
    r"Invoke-Expression",
]

# "safe_readonly_auto" モード時のみ自動実行を許可する純読み取りコマンド接頭辞（チェーン記号無し限定）
_SAFE_READONLY_PREFIXES = (
    "dir",
    "ls",
    "pwd",
    "cd",
    "whoami",
    "hostname",
    "python --version",
    "python -V",
    "node --version",
    "git status",
    "git log",
    "git diff",
    "git branch",
    "pip list",
    "ollama list",
)


class ApprovalManager:
    """
    コマンド実行前の Human-in-the-Loop (HITL) 承認待ち管理および安全実行クラス。
    Web UI と Discord ボタン双方から同じリクエストIDを承認・拒否できる。
    """

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}
        self.on_requested: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None
        self.on_resolved: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None = None

    @staticmethod
    def _is_hard_blocked(command: str) -> bool:
        for pat in _HARD_BLOCKED_PATTERNS:
            if re.search(pat, command, flags=re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _is_safe_readonly(command: str) -> bool:
        cmd = command.strip()
        # コマンド連結やリダイレクトがある場合は絶対自動許可しない
        if any(op in cmd for op in ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n")):
            return False
        cmd_lower = cmd.lower()
        return any(cmd_lower == p or cmd_lower.startswith(p + " ") for p in _SAFE_READONLY_PREFIXES)

    def list_pending(self) -> list[dict[str, Any]]:
        """現在承認待機中のリクエスト一覧（JSON化可能形式）を返す。"""
        return [
            {
                "id": req_id,
                "command": item["command"],
                "cwd": item["cwd"],
                "reason": item["reason"],
                "created_at": item["created_at"],
            }
            for req_id, item in self._pending.items()
        ]

    async def resolve(self, req_id: str, approved: bool, actor: str = "UI") -> dict[str, Any]:
        """待機中の承認リクエストに許可/拒否結果を反映する。"""
        item = self._pending.get(req_id)
        if not item:
            return {"ok": False, "error": f"承認待ちID「{req_id}」は見つからないか既に処理済みです。"}
        fut: asyncio.Future[bool] = item["future"]
        if not fut.done():
            fut.set_result(bool(approved))
        summary = {
            "id": req_id,
            "command": item["command"],
            "approved": bool(approved),
            "actor": actor,
        }
        if self.on_resolved:
            try:
                await self.on_resolved(summary)
            except Exception:
                pass
        return {"ok": True, **summary}

    async def execute_command(
        self, command: str, reason: str = "", cwd: str = "", timeout_sec: int = 20, approval_wait_sec: int = 60
    ) -> str:
        """承認ポリシーを検証し、承認後にコマンドを実行して標準出力・エラーを返す。"""
        if not config.command_execution_enabled:
            return "コマンド実行機能 (`run_command`) は現在設定で無効化されています。"

        cmd = (command or "").strip()
        if not cmd:
            return "実行コマンドが空です。"
        if self._is_hard_blocked(cmd):
            return f"【セキュリティブロック】危険または機密ファイルに関わるコマンドのため実行を禁止しました: `{cmd}`"

        work_dir = os.path.realpath(os.path.expandvars(cwd.strip())) if cwd.strip() else BASE_DIR
        if not os.path.isdir(work_dir):
            work_dir = BASE_DIR

        policy = (config.command_approval_mode or "always").lower()
        need_approval = True
        if policy == "never":
            need_approval = False
        elif policy == "safe_readonly_auto" and self._is_safe_readonly(cmd):
            need_approval = False

        if need_approval:
            req_id = uuid.uuid4().hex[:8]
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bool] = loop.create_future()
            entry = {
                "id": req_id,
                "command": cmd,
                "cwd": work_dir,
                "reason": (reason or "自律ツールによるコマンド実行").strip()[:160],
                "created_at": time.time(),
                "future": fut,
            }
            self._pending[req_id] = entry
            if self.on_requested:
                try:
                    await self.on_requested(
                        {
                            "id": req_id,
                            "command": cmd,
                            "cwd": work_dir,
                            "reason": entry["reason"],
                            "created_at": entry["created_at"],
                        }
                    )
                except Exception:
                    pass

            try:
                approved = await asyncio.wait_for(fut, timeout=float(approval_wait_sec))
            except asyncio.TimeoutError:
                self._pending.pop(req_id, None)
                if self.on_resolved:
                    try:
                        await self.on_resolved(
                            {"id": req_id, "command": cmd, "approved": False, "actor": "Timeout"}
                        )
                    except Exception:
                        pass
                return f"【承認タイムアウト ({approval_wait_sec}秒)】ユーザー承認が得られなかったためコマンド `{cmd}` をキャンセルしました。"
            finally:
                self._pending.pop(req_id, None)

            if not approved:
                return f"【実行拒否】マスターがコマンド `{cmd}` の実行を拒否しました。"

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_sec))

            def _decode(raw: bytes) -> str:
                for enc in ("utf-8", "cp932", "shift_jis", "latin-1"):
                    try:
                        return raw.decode(enc)
                    except UnicodeDecodeError:
                        continue
                return raw.decode("utf-8", errors="replace")

            out_txt = _decode(stdout_b).strip()
            err_txt = _decode(stderr_b).strip()
            body = out_txt[:2500]
            if err_txt:
                body += ("\n[stderr]\n" + err_txt[:1000]) if body else f"[stderr]\n{err_txt[:1000]}"
            return (
                f"【コマンド実行結果 (exit={proc.returncode}, cwd={work_dir})】\n"
                f"コマンド: `{cmd}`\n"
                f"{body or '（出力なし）'}"
            )
        except asyncio.TimeoutError:
            return f"【タイムアウト】コマンド `{cmd}` が {timeout_sec} 秒以内に終了しませんでした。"
        except Exception as exc:
            return f"【コマンド実行エラー】{exc}"


class BrowserAutomationManager:
    """Playwright を用いた動的Webページ閲覧・スクショ・クリック・フォーム入力マネージャ。"""

    @staticmethod
    def _validate_target_url(url: str) -> tuple[bool, str]:
        target = (url or "").strip()
        if not target.startswith(("http://", "https://")):
            return False, "http:// または https:// で始まるURLを指定してください。"
        parsed = urllib.parse.urlparse(target)
        host = (parsed.hostname or "").lower()
        # 自サーバーやクラウドメタデータへのSSRFを抑止
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and parsed.port in (8000, None):
            return False, "安全のため、ローカル管理サーバー自身へのアクセスはブロックされています。"
        if host == "169.254.169.254":
            return False, "安全のため、メタデータIPへのアクセスは禁止されています。"
        return True, target

    async def execute(
        self,
        action: str = "read_page",
        url: str = "",
        selector: str = "",
        text: str = "",
        wait_ms: int = 1200,
    ) -> dict[str, Any]:
        """ヘッドレスChromiumを起動し、動的ページの本文読取・操作・スクショを行う。"""
        if not config.playwright_enabled:
            return {"ok": False, "message": "Playwright ブラウザ操作ツールは設定で無効化されています。"}

        ok, validated = self._validate_target_url(url)
        if not ok:
            return {"ok": False, "message": validated}

        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            return {"ok": False, "message": f"Playwright が利用できません: {exc}"}

        act = (action or "read_page").strip().lower()
        timeout_ms = max(5000, int(config.fetch_timeout_sec) * 1000)

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 1280, "height": 800})
                    await page.goto(validated, wait_until="domcontentloaded", timeout=timeout_ms)
                    await page.wait_for_timeout(max(200, min(5000, int(wait_ms))))

                    if act == "click" and selector:
                        await page.click(selector, timeout=5000)
                        await page.wait_for_timeout(800)
                    elif act == "fill" and selector:
                        await page.fill(selector, text or "", timeout=5000)
                        await page.wait_for_timeout(400)

                    title = await page.title()
                    body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                    clean_text = re.sub(r"\n{3,}", "\n\n", (body_text or "").strip())
                    max_chars = int(config.fetch_max_chars)

                    result: dict[str, Any] = {
                        "ok": True,
                        "url": page.url,
                        "title": title,
                        "text": clean_text[:max_chars],
                    }

                    if act == "screenshot":
                        os.makedirs(VISION_CAPTURE_DIR, exist_ok=True)
                        fname = f"browser_{int(time.time()*1000)}.jpg"
                        fpath = os.path.join(VISION_CAPTURE_DIR, fname)
                        jpg_bytes = await page.screenshot(type="jpeg", quality=82, full_page=False)
                        with open(fpath, "wb") as f:
                            f.write(jpg_bytes)
                        import base64

                        result["image_path"] = fpath
                        result["image_url"] = f"/api/vision_file/{fname}"
                        result["image_base64"] = base64.b64encode(jpg_bytes).decode("ascii")

                    # 主要リンクも最大8件抽出
                    links = await page.evaluate(
                        """() => Array.from(document.querySelectorAll('a[href]'))
                        .slice(0, 30)
                        .map(a => ({text: (a.innerText || '').trim(), href: a.href}))
                        .filter(x => x.text && x.href.startsWith('http'))
                        .slice(0, 8)"""
                    )
                    result["links"] = links
                    return result
                finally:
                    await browser.close()
        except Exception as exc:
            return {"ok": False, "message": f"ブラウザ操作エラー ({validated}): {exc}"}


class MCPClientManager:
    """
    Model Context Protocol (MCP) stdio JSON-RPC 2.0 クライアント。
    設定された外部 MCP サーバーのツール一覧取得 (`tools/list`) と実行 (`tools/call`) を提供する。
    """

    @staticmethod
    def load_servers() -> list[dict[str, Any]]:
        """設定文字列 `mcp_servers_json` からサーバー定義リストを読み込む。"""
        raw = (config.mcp_servers_json or "[]").strip()
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [s for s in data if isinstance(s, dict) and s.get("name") and s.get("command")]
        except Exception:
            pass
        return []

    @staticmethod
    async def _send_rpc(proc: asyncio.subprocess.Process, msg: dict[str, Any]) -> None:
        assert proc.stdin is not None
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        proc.stdin.write(line)
        await proc.stdin.drain()

    @staticmethod
    async def _read_rpc_response(
        proc: asyncio.subprocess.Process, target_id: int, timeout_sec: float = 12.0
    ) -> dict[str, Any]:
        assert proc.stdout is not None
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            remain = max(0.2, deadline - time.time())
            line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=remain)
            if not line_b:
                raise RuntimeError("MCP サーバーが応答せず終了しました。")
            text = line_b.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            # Content-Length ヘッダ形式にも対応
            if text.lower().startswith("content-length:"):
                length = int(text.split(":", 1)[1].strip())
                while True:
                    hdr = (await proc.stdout.readline()).decode("utf-8", errors="ignore").strip()
                    if not hdr:
                        break
                body_b = await proc.stdout.readexactly(length)
                text = body_b.decode("utf-8", errors="replace").strip()
            try:
                obj = json.loads(text)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("id") == target_id:
                return obj
        raise TimeoutError("MCP サーバー応答がタイムアウトしました。")

    async def invoke(
        self, server_name: str, action: str = "list_tools", tool_name: str = "", arguments: dict[str, Any] | None = None
    ) -> str:
        """指定 MCP サーバーを起動して初期化し、ツール一覧取得またはツール呼び出しを行う。"""
        servers = self.load_servers()
        if not servers:
            return "MCPサーバーが未設定です。統合設定の「🤖 自律操作・RAG・MCP」タブに MCP サーバーJSONを登録してください。"

        target_srv = None
        if server_name.strip():
            for s in servers:
                if str(s["name"]).strip().lower() == server_name.strip().lower():
                    target_srv = s
                    break
        else:
            target_srv = servers[0]

        if not target_srv:
            names = ", ".join(str(s["name"]) for s in servers)
            return f"指定されたMCPサーバー「{server_name}」が見つかりません。登録済み: {names}"

        env = os.environ.copy()
        if isinstance(target_srv.get("env"), dict):
            for k, v in target_srv["env"].items():
                env[str(k)] = str(v)

        cmd_list = [str(target_srv["command"]), *[str(a) for a in (target_srv.get("args") or [])]]
        proc = await asyncio.create_subprocess_exec(
            *cmd_list,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            await self._send_rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "NIA-MyAgent", "version": "3.0"},
                    },
                },
            )
            await self._read_rpc_response(proc, 1)
            await self._send_rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

            if action == "list_tools" or not tool_name:
                await self._send_rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                resp = await self._read_rpc_response(proc, 2)
                tools = resp.get("result", {}).get("tools", [])
                if not tools:
                    return f"MCPサーバー「{target_srv['name']}」からツールが見つかりませんでした。"
                lines = [
                    f"- {t.get('name')}: {str(t.get('description') or '')[:100]}"
                    for t in tools[:25]
                ]
                return f"【MCPツール一覧 ({target_srv['name']})】\n" + "\n".join(lines)

            await self._send_rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments or {}},
                },
            )
            resp = await self._read_rpc_response(proc, 3, timeout_sec=20.0)
            if "error" in resp:
                return f"【MCPエラー ({target_srv['name']}.{tool_name})】{resp['error']}"
            content_blocks = resp.get("result", {}).get("content", [])
            texts = [
                str(b.get("text", "")) if isinstance(b, dict) else str(b)
                for b in content_blocks
            ]
            joined = "\n".join(t for t in texts if t).strip() or json.dumps(resp.get("result"), ensure_ascii=False)
            return f"【MCP実行結果 ({target_srv['name']}.{tool_name})】\n{joined[:3500]}"
        except Exception as exc:
            return f"【MCP通信エラー ({target_srv['name']})】{exc}"
        finally:
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
            except Exception:
                pass
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except Exception:
                        pass


class ExternalServiceManager:
    """GitHub REST API および Google Calendar (iCal URL / OAuth) / Gmail 連携マネージャ。"""

    async def query_github(self, action: str = "issues", repo: str = "", limit: int = 5) -> str:
        """GitHub リポジトリの Issue / PR / コミット / 通知を取得する。"""
        target_repo = (repo or config.github_default_repo or "").strip()
        if action != "notifications" and "/" not in target_repo:
            return "GitHubリポジトリを `owner/repo` 形式で指定するか、設定でデフォルトリポジトリを登録してください。"

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "NIA-MyAgent/3.0",
        }
        if config.github_token:
            headers["Authorization"] = f"Bearer {config.github_token}"

        act = (action or "issues").lower()
        n = max(1, min(15, int(limit or 5)))

        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            if act == "notifications":
                if not config.github_token:
                    return "GitHub通知の取得には GitHub Token が必要です（設定のシークレット欄に入力してください）。"
                resp = await client.get("https://api.github.com/notifications", params={"per_page": n})
                if resp.status_code != 200:
                    return f"GitHub通知取得エラー (HTTP {resp.status_code}): {resp.text[:160]}"
                items = resp.json()
                if not items:
                    return "現在未読の GitHub 通知はありません。"
                lines = [
                    f"- [{it.get('repository', {}).get('full_name')}] {it.get('subject', {}).get('title')} ({it.get('reason')})"
                    for it in items[:n]
                ]
                return "【GitHub 未読通知】\n" + "\n".join(lines)

            if act in ("prs", "pulls"):
                url = f"https://api.github.com/repos/{target_repo}/pulls"
                resp = await client.get(url, params={"state": "open", "per_page": n})
                if resp.status_code != 200:
                    return f"GitHub PR取得エラー (HTTP {resp.status_code}): {resp.text[:160]}"
                pulls = resp.json()
                if not pulls:
                    return f"{target_repo} にオープンな PR はありません。"
                lines = [
                    f"- #{pr['number']} {pr['title']} (by {pr['user']['login']}) ({pr['html_url']})"
                    for pr in pulls[:n]
                ]
                return f"【GitHub Open PR ({target_repo})】\n" + "\n".join(lines)

            if act == "commits":
                url = f"https://api.github.com/repos/{target_repo}/commits"
                resp = await client.get(url, params={"per_page": n})
                if resp.status_code != 200:
                    return f"GitHub コミット取得エラー (HTTP {resp.status_code}): {resp.text[:160]}"
                commits = resp.json()
                lines = [
                    f"- `{c['sha'][:7]}` {c['commit']['message'].splitlines()[0]} ({c['commit']['author']['name']})"
                    for c in commits[:n]
                ]
                return f"【GitHub 最新コミット ({target_repo})】\n" + "\n".join(lines)

            # issues（PRを除外して純粋なIssueのみ返す）
            url = f"https://api.github.com/repos/{target_repo}/issues"
            resp = await client.get(url, params={"state": "open", "per_page": n * 2})
            if resp.status_code != 200:
                return f"GitHub Issue取得エラー (HTTP {resp.status_code}): {resp.text[:160]}"
            issues = [it for it in resp.json() if "pull_request" not in it][:n]
            if not issues:
                return f"{target_repo} にオープンな Issue はありません。"
            lines = [
                f"- #{it['number']} {it['title']} ({it['html_url']})"
                for it in issues
            ]
            return f"【GitHub Open Issues ({target_repo})】\n" + "\n".join(lines)

    @staticmethod
    def _parse_ics_datetime(raw: str) -> datetime.datetime | None:
        val = (raw or "").strip()
        try:
            if val.endswith("Z") and len(val) >= 16:
                dt = datetime.datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
                return dt.astimezone(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
            if "T" in val and len(val) >= 15:
                return datetime.datetime.strptime(val[:15], "%Y%m%dT%H%M%S")
            if len(val) == 8 and val.isdigit():
                return datetime.datetime.strptime(val, "%Y%m%d")
        except Exception:
            return None
        return None

    async def query_calendar(self, days: int = 7) -> str:
        """iCal (.ics) URL または Google Calendar OAuth を用いて今後の予定を取得する。"""
        days = max(1, min(30, int(days or 7)))
        now = datetime.datetime.now()
        until = now + datetime.timedelta(days=days)

        # 1. Google Calendar OAuth トークンがある場合は公式 API を利用
        token_path = os.path.join(DATA_DIR, "google_token.json")
        if os.path.exists(token_path):
            try:
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build

                creds = Credentials.from_authorized_user_file(token_path)
                service = build("calendar", "v3", credentials=creds)
                events_res = (
                    service.events()
                    .list(
                        calendarId="primary",
                        timeMin=now.isoformat() + "+09:00",
                        timeMax=until.isoformat() + "+09:00",
                        maxResults=15,
                        singleEvents=True,
                        orderBy="startTime",
                    )
                    .execute()
                )
                items = events_res.get("items", [])
                if not items:
                    return f"今後{days}日間の Google カレンダー予定はありません。"
                lines = []
                for ev in items:
                    st = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date") or ""
                    lines.append(f"- {st[:16]} | {ev.get('summary', '（無題）')}")
                return f"【Google カレンダー予定（今後{days}日間）】\n" + "\n".join(lines)
            except Exception as exc:
                return f"Google Calendar API 読込エラー: {exc}"

        # 2. iCal (.ics) URL（OAuth不要でGoogle/Outlookカレンダーの限定公開URLを読める）
        ics_url = (config.google_ics_url or "").strip()
        if not ics_url:
            return (
                "カレンダー連携が未設定です。統合設定の「🤖 自律操作・RAG・MCP」タブに"
                " Google カレンダーの「iCal形式の非公開URL（.ics）」または `data/google_token.json` を設定してください。"
            )

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(ics_url)
                if resp.status_code != 200:
                    return f"iCal (.ics) 取得失敗 (HTTP {resp.status_code})"
                ics_text = re.sub(r"\r?\n[ \t]", "", resp.text)

            events: list[tuple[datetime.datetime, str]] = []
            for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ics_text, flags=re.DOTALL):
                m_dt = re.search(r"DTSTART[^:]*:([^\r\n]+)", block)
                m_sum = re.search(r"SUMMARY[^:]*:([^\r\n]+)", block)
                if not m_dt or not m_sum:
                    continue
                dt = self._parse_ics_datetime(m_dt.group(1))
                if dt and (now - datetime.timedelta(hours=2)) <= dt <= until:
                    summary = m_sum.group(1).replace("\\,", ",").replace("\\n", " ").strip()
                    events.append((dt, summary))

            events.sort(key=lambda x: x[0])
            if not events:
                return f"今後{days}日間のカレンダー予定は見つかりませんでした。"
            lines = [f"- {dt.strftime('%m/%d %H:%M')} | {summary}" for dt, summary in events[:15]]
            return f"【カレンダー予定（今後{days}日間）】\n" + "\n".join(lines)
        except Exception as exc:
            return f"iCal 解析エラー: {exc}"

    async def query_gmail(self, max_results: int = 5) -> str:
        """認証済み `data/google_token.json` を用いて未読/最新メール要約を取得する。"""
        token_path = os.path.join(DATA_DIR, "google_token.json")
        if not os.path.exists(token_path):
            return (
                "Gmail 連携用の認証ファイル (`data/google_token.json`) が未配置です。"
                "Google Cloud Console の OAuth クライアントを認証して `data/google_token.json` を配置すると未読メールを要約できます。"
            )
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(token_path)
            service = build("gmail", "v1", credentials=creds)
            res = service.users().messages().list(userId="me", q="is:unread", maxResults=max_results).execute()
            msgs = res.get("messages", [])
            if not msgs:
                return "現在未読メールはありません。"
            lines = []
            for m in msgs[:max_results]:
                detail = service.users().messages().get(userId="me", id=m["id"], format="metadata").execute()
                hdrs = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
                subj = hdrs.get("Subject", "（件名なし）")
                sender = hdrs.get("From", "不明")
                snippet = html.unescape(detail.get("snippet", ""))[:90]
                lines.append(f"- [{sender}] {subj} — {snippet}")
            return "【Gmail 未読メール一覧】\n" + "\n".join(lines)
        except Exception as exc:
            return f"Gmail 取得エラー: {exc}"


class WindowsControlManager:
    """Windows のメディア・音量操作、および Action Center トースト通知 DB 読み取りを行う。"""

    VK_CODES = {
        "volume_mute": 0xAD,
        "volume_down": 0xAE,
        "volume_up": 0xAF,
        "next_track": 0xB0,
        "prev_track": 0xB1,
        "media_stop": 0xB2,
        "play_pause": 0xB3,
    }

    def __init__(self) -> None:
        self.last_seen_notification_id: int = 0
        self._deferred_power_actions: list[str] = []

    def media_action(self, action: str, steps: int = 1) -> str:
        """Windows 仮想キーイベントでメディア再生制御・音量調整を行う。"""
        if os.name != "nt":
            return "Windows環境専用機能です。"
        vk = self.VK_CODES.get((action or "").strip().lower())
        if not vk:
            return f"未対応の操作です: {action}（対応: {', '.join(self.VK_CODES.keys())}）"
        try:
            import ctypes

            count = max(1, min(15, int(steps or 1)))
            if action in ("play_pause", "next_track", "prev_track", "media_stop", "volume_mute"):
                count = 1
            for _ in range(count):
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
                ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
            return f"【Windows環境制御】`{action}` を実行しました（回数: {count}）。"
        except Exception as exc:
            return f"Windows環境制御エラー: {exc}"

    @staticmethod
    def _do_lock() -> bool:
        """Win32 API `LockWorkStation` で画面をロックする（失敗時は rundll32 で再試行）。"""
        import ctypes
        import subprocess

        try:
            if ctypes.windll.user32.LockWorkStation():
                return True
        except Exception:
            pass
        try:
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
            return True
        except Exception:
            return False

    @staticmethod
    def _do_sleep() -> bool:
        """Win32 API `SetSuspendState` で PC をスリープ（休止ではなく Suspend）へ移行させる。"""
        import ctypes
        from ctypes import wintypes

        powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
        powrprof.SetSuspendState.argtypes = [wintypes.BOOL, wintypes.BOOL, wintypes.BOOL]
        powrprof.SetSuspendState.restype = wintypes.BOOLEAN
        return bool(powrprof.SetSuspendState(False, False, False))

    def power_action(
        self, action: str, delay_sec: float | None = None, defer_until_response: bool = True
    ) -> str:
        """
        確認バナーなしで PC ロック (`lock_pc`) またはスリープ (`sleep_pc`) を実行予約する。
        返答生成や Discord 送信の完了後に発動するよう既定ではキューへ積み、応答完了直後に実行する。
        """
        if os.name != "nt":
            return "Windows環境専用機能です。"
        if not config.windows_power_control_enabled:
            return "PCロック・スリープの即時実行は現在設定（windows_power_control_enabled）で無効化されています。"

        act = (action or "").strip().lower()
        if act in ("lock_pc", "lock"):
            norm = "lock_pc"
            label = "PC画面ロック"
            default_wait = 0.8
        elif act in ("sleep_pc", "sleep", "suspend"):
            norm = "sleep_pc"
            label = "PCスリープ移行"
            default_wait = 1.8
        else:
            return f"未対応の電源制御アクションです: {action}（対応: lock_pc, sleep_pc）"

        if defer_until_response and delay_sec is None:
            if norm not in self._deferred_power_actions:
                self._deferred_power_actions.append(norm)
            return f"【Windows電源制御】{label}をセットしました（この返答の送信完了直後に実行されます）。"

        wait_s = default_wait if delay_sec is None else max(0.0, float(delay_sec))
        fn = self._do_lock if norm == "lock_pc" else self._do_sleep
        if wait_s <= 0:
            ok = fn()
            return f"【Windows電源制御】{label}を実行しました。" if ok else f"{label}の実行に失敗しました。"
        t = threading.Timer(wait_s, fn)
        t.daemon = True
        t.start()
        return f"【Windows電源制御】{label}を実行予約しました（{wait_s:.1f}秒後に実行）。"

    def flush_deferred_power_actions(self) -> list[str]:
        """応答完了（WebSocket/Discord送信完了）後に予約されていた電源制御タイマーを起動する。"""
        if not self._deferred_power_actions:
            return []
        actions = list(self._deferred_power_actions)
        self._deferred_power_actions.clear()
        for norm in actions:
            if norm == "lock_pc":
                t = threading.Timer(0.8, self._do_lock)
                t.daemon = True
                t.start()
            elif norm == "sleep_pc":
                t = threading.Timer(1.8, self._do_sleep)
                t.daemon = True
                t.start()
        return actions

    @staticmethod
    def _parse_toast_xml(payload_bytes: bytes | str) -> tuple[str, str]:
        """Windows トースト通知 XML Payload からタイトルと本文テキストを抽出する。"""
        try:
            xml_str = payload_bytes.decode("utf-8", errors="ignore") if isinstance(payload_bytes, bytes) else str(payload_bytes)
            root = ET.fromstring(xml_str)
            texts = [el.text.strip() for el in root.iter("text") if el.text and el.text.strip()]
            if not texts:
                return "", ""
            title = texts[0]
            body = " / ".join(texts[1:3])
            return title, body
        except Exception:
            return "", ""

    def read_notifications(self, limit: int = 8) -> list[dict[str, Any]]:
        """Windows Action Center DB (wpndatabase.db) を安全に一時コピーして最新トースト通知を取得する。"""
        if os.name != "nt":
            return []
        local_app = os.environ.get("LOCALAPPDATA", "")
        db_src = os.path.join(local_app, "Microsoft", "Windows", "Notifications", "wpndatabase.db")
        if not os.path.exists(db_src):
            return []

        fd, tmp_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            shutil.copy2(db_src, tmp_db)
            conn = sqlite3.connect(tmp_db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT n.Id, h.PrimaryId AS app_id, n.Payload, n.ArrivalTime
                FROM Notification n
                LEFT JOIN NotificationHandler h ON n.HandlerId = h.RecordId
                WHERE n.Payload IS NOT NULL
                ORDER BY n.Id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            conn.close()

            results = []
            for r in rows:
                title, body = self._parse_toast_xml(r["Payload"])
                if not title and not body:
                    continue
                results.append(
                    {
                        "id": int(r["Id"]),
                        "app_id": str(r["app_id"] or "Windows"),
                        "title": title,
                        "body": body,
                    }
                )
            return results
        except Exception:
            return []
        finally:
            if os.path.exists(tmp_db):
                try:
                    os.remove(tmp_db)
                except Exception:
                    pass

    def poll_new_notifications(self) -> list[dict[str, Any]]:
        """前回チェック以降の未通知 Windows トーストのみを返す。"""
        items = self.read_notifications(limit=6)
        if not items:
            return []
        max_id = max(it["id"] for it in items)
        if self.last_seen_notification_id == 0:
            self.last_seen_notification_id = max_id
            return []
        fresh = [it for it in items if it["id"] > self.last_seen_notification_id]
        self.last_seen_notification_id = max_id
        return list(reversed(fresh))


async def compute_url_content_hash(url: str) -> tuple[bool, str, str]:
    """指定URLから本文テキストを取得し、(成功, SHA256先頭16桁, 先頭テキスト抜粋) を返す。"""
    target = (url or "").strip()
    if not target.startswith(("http://", "https://")):
        return False, "", "無効なURLです。"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(target)
            if resp.status_code != 200:
                return False, "", f"HTTP {resp.status_code}"
            txt = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", resp.text, flags=re.DOTALL | re.IGNORECASE)
            txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
            txt = re.sub(r"\s+", " ", txt).strip()
            digest = hashlib.sha256(txt.encode("utf-8")).hexdigest()[:16]
            return True, digest, txt[:1600]
    except Exception as exc:
        return False, "", str(exc)


def save_research_report(topic: str, markdown_body: str) -> str:
    """深掘りリサーチ結果のMarkdownレポートを保存し、ファイルパスを返す。"""
    os.makedirs(RESEARCH_REPORT_DIR, exist_ok=True)
    safe_slug = re.sub(r"[^A-Za-z0-9\u3040-\u30FF\u4E00-\u9FFF_\-]+", "_", topic)[:40].strip("_") or "report"
    fname = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_slug}.md"
    fpath = os.path.join(RESEARCH_REPORT_DIR, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(markdown_body)
    return fpath


approval_manager = ApprovalManager()
browser_manager = BrowserAutomationManager()
mcp_manager = MCPClientManager()
external_service_manager = ExternalServiceManager()
windows_control_manager = WindowsControlManager()
