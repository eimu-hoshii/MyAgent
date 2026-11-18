"""
黒いコマンドプロンプト画面（cmd）を一切表示せずに、
NIAバックエンドサーバーおよびネイティブ透過デスクトップマスコットを起動・再起動・停止するサイレントランチャー。
"""

import ctypes
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
PID_FILE = os.path.join(DATA_DIR, "nia_processes.pid")
LOG_FILE = os.path.join(DATA_DIR, "server.log")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_PY_DIR = os.path.dirname(sys.executable)
PYTHON_EXE = os.path.join(_PY_DIR, "python.exe") if os.path.exists(os.path.join(_PY_DIR, "python.exe")) else sys.executable
PYTHONW_EXE = os.path.join(_PY_DIR, "pythonw.exe") if os.path.exists(os.path.join(_PY_DIR, "pythonw.exe")) else sys.executable


def is_port_in_use(port: int = 8000) -> bool:
    """指定ポートが既に使用中（サーバー稼働中）か判定する。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_aivisspeech_silent() -> None:
    """ポート10101が未稼働かつAivisSpeech-Engineがインストール済みなら、ウィンドウ無しで裏起動する（二重起動防止付き）。"""
    if is_port_in_use(10101):
        return
    local_app = os.environ.get("LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local"))
    prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for engine_exe in (
        os.path.join(local_app, "Programs", "AivisSpeech", "AivisSpeech-Engine", "run.exe"),
        os.path.join(prog_files, "AivisSpeech", "AivisSpeech-Engine", "run.exe"),
    ):
        if os.path.isfile(engine_exe):
            try:
                subprocess.Popen(
                    [engine_exe, "--use_gpu", "--host", "127.0.0.1", "--port", "10101"],
                    cwd=os.path.dirname(engine_exe),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception:
                pass
            return


def stop_existing_processes() -> None:
    """稼働中のNIAバックエンドサーバー（ポート8000）およびデスクトップマスコットをクリーン停止する。"""
    if os.name == "nt":
        # 1. ポート8000をリッスンしているプロセスを特定して停止
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                creationflags=CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                encoding="cp932",
                errors="ignore",
            )
            for line in out.splitlines():
                if "127.0.0.1:8000" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = int(parts[-1])
                    if pid > 0 and pid != os.getpid():
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(pid)],
                            creationflags=CREATE_NO_WINDOW,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
        except Exception:
            pass

        # 2. PIDファイルに記録されているプロセスがあれば停止
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.isdigit():
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", line],
                                creationflags=CREATE_NO_WINDOW,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                os.remove(PID_FILE)
            except Exception:
                pass


def start_backend_silent() -> int:
    """黒いcmd画面を出さずにFastAPIバックエンドサーバーを起動し、PIDを返す。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    log_fp = open(LOG_FILE, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [
            PYTHON_EXE,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=BASE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=log_fp,
        creationflags=CREATE_NO_WINDOW,
    )
    return proc.pid


def start_mascot_silent() -> int:
    """黒いcmd画面を出さずにネイティブ透過デスクトップマスコットを起動し、PIDを返す。"""
    mascot_script = os.path.join(BASE_DIR, "scripts", "desktop_mascot.py")
    proc = subprocess.Popen(
        [PYTHONW_EXE, mascot_script],
        cwd=BASE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


def wait_until_server_ready(timeout_sec: float = 8.0) -> bool:
    """バックエンドサーバーがHTTP応答可能になるまで待機する。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            req = urllib.request.urlopen("http://127.0.0.1:8000/api/status", timeout=1.0)
            if req.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.35)
    return False


def save_pids(pids: list[int]) -> None:
    """起動したプロセスのPIDを記録する。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PID_FILE, "w", encoding="utf-8") as f:
        for p in pids:
            if p:
                f.write(f"{p}\n")


def main() -> None:
    mode = "full"
    for arg in sys.argv[1:]:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1].strip()

    if mode == "stop":
        stop_existing_processes()
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(
                0,
                "ニア (NIA) のバックグラウンドサーバーおよび常駐マスコットを停止しました。",
                "NIA 統合管制システム",
                0x40,
            )
        return

    if mode == "mascot":
        ensure_aivisspeech_silent()
        pids = []
        if not is_port_in_use(8000):
            pids.append(start_backend_silent())
            wait_until_server_ready(6.0)
        pids.append(start_mascot_silent())
        save_pids(pids)
        return

    # デフォルト ("full"): 既存プロセスをクリーン再起動し、AivisSpeech裏起動確認＋サーバー起動後にブラウザを開く
    stop_existing_processes()
    ensure_aivisspeech_silent()
    time.sleep(0.4)
    srv_pid = start_backend_silent()
    save_pids([srv_pid])
    wait_until_server_ready(7.0)
    webbrowser.open("http://127.0.0.1:8000")


if __name__ == "__main__":
    main()
