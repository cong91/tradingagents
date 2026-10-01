@echo off
setlocal
title TradingAgents Control Panel - Launcher

rem === Chay tu thu muc chua file nay (click duoc tu bat cu dau) ===
cd /d "%~dp0"

echo =====================================================
echo   TradingAgents Control Panel - Khoi dong he thong
echo =====================================================
echo.

rem === 1. Kiem tra moi truong Python ===
if not exist ".venv\Scripts\python.exe" (
    echo [LOI] Khong tim thay .venv\Scripts\python.exe
    echo        Cai dat truoc: .venv\Scripts\python.exe -m pip install -e ".[dev,web]"
    pause
    exit /b 1
)

rem === 2. Lan dau chay: cai thu vien frontend neu thieu ===
if not exist "web\node_modules" (
    echo [SETUP] Chua co web\node_modules - chay npm install -- lam 1-2 phut lan dau...
    pushd web
    call npm install
    if errorlevel 1 (
        popd
        echo [LOI] npm install that bai - kiem tra ket noi mang / Node.js.
        pause
        exit /b 1
    )
    popd
)

rem === 3. Backend: dung lai neu dang chay san, khong thi khoi dong moi ===
netstat -ano | findstr "LISTENING" | findstr ":8000" >nul
if errorlevel 1 (
    echo [i] Khoi dong backend FastAPI -- cua so moi: TradingAgents Backend...
    start "TradingAgents Backend" cmd /k ".venv\Scripts\python.exe -m uvicorn server.main:app --port 8000"
) else (
    echo [i] Backend dang chay san o port 8000 - dung lai instance hien co.
)

rem === 4. Frontend Next.js: dung lai neu dang chay san, khong thi khoi dong moi ===
netstat -ano | findstr "LISTENING" | findstr ":3000" >nul
if errorlevel 1 (
    echo [i] Khoi dong frontend Next.js -- cua so moi: TradingAgents Frontend...
    start "TradingAgents Frontend" cmd /k "cd /d %~dp0web && npm run dev"
) else (
    echo [i] Frontend dang chay san o port 3000 - dung lai instance hien co.
)

rem === 5. Cho backend san sang (toi da ~30 giay) roi mo trinh duyet ===
echo [i] Cho backend san sang...
set TRIES=0
:POLL
set /a TRIES+=1
curl -s -m 2 -o NUL http://127.0.0.1:8000/api/health
if not errorlevel 1 goto BACKEND_UP
if %TRIES% geq 15 (
    echo [CANH BAO] Backend chua tra loi sau 30 giay - van mo trinh duyet, xem log o cua so Backend.
    goto BROWSER
)
rem ping wait thay timeout: timeout khong chay duoc khi stdin bi redirect
ping -n 3 127.0.0.1 >nul
goto POLL

:BACKEND_UP
echo [i] Backend da san sang.

:BROWSER
echo [i] Cho frontend bien dich ~5 giay roi mo trinh duyet...
ping -n 6 127.0.0.1 >nul
start "" http://localhost:3000

echo.
echo =====================================================
echo   Da khoi dong:
echo     - Giao dien : http://localhost:3000
echo     - Backend   : http://127.0.0.1:8000/api/health
echo   Muon DUNG he thong: dong 2 cua so log
echo   (TradingAgents Backend / TradingAgents Frontend)
echo =====================================================
endlocal
