@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ============================================================================
echo   NIA（ニア） // AIエージェント ^& 自律型VTuber 統合システムを起動します
echo   - 管制コンソール UI: http://localhost:8000
echo   - OBS 透過オーバーレイ: http://localhost:8000/?mode=obs
echo ============================================================================

start "" "http://localhost:8000"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
pause
