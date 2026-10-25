@echo off
setlocal
title TradingAgents - Docker launcher

rem === Chay tu thu muc chua file nay (click duoc tu bat cu dau) ===
cd /d "%~dp0"

echo =====================================================
echo   TradingAgents - Chay bang Docker (PostgreSQL + API + Web)
echo =====================================================
echo.
echo Lan dau chay se tu sinh mat khau PostgreSQL va luu vao
echo .env.docker (khong commit). Cac lan sau dung lai mat khau do.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [LOI] Khong tim thay .venv\Scripts\python.exe
    echo        Cai dat truoc: .venv\Scripts\python.exe -m pip install -e ".[dev,web]"
    pause
    exit /b 1
)

.venv\Scripts\python.exe scripts\docker_up.py %*
if errorlevel 1 (
    echo.
    echo [LOI] Docker stack khong khoi dong duoc - xem thong bao phia tren.
    pause
    exit /b 1
)

echo.
echo =====================================================
echo   Muon dung: .venv\Scripts\python.exe scripts\docker_up.py --down
echo   Xem log API: .venv\Scripts\python.exe scripts\docker_up.py --logs
echo   Xem mat khau: .venv\Scripts\python.exe scripts\docker_up.py --show
echo =====================================================
endlocal
