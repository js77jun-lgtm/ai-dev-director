@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [오류] .venv 가상환경이 없습니다. 먼저 가상환경을 만들고 requirements.txt를 설치하세요.
    pause
    exit /b 1
)

rem 가상환경 python과 npm 전역 CLI(claude, codex)를 PATH 앞에 둔다
call ".venv\Scripts\activate.bat"
set "PATH=%APPDATA%\npm;%PATH%"

echo AI DEV DIRECTOR 콘솔을 켭니다...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-companion.ps1"
if errorlevel 1 (
    echo [오류] 콘솔을 켜지 못했습니다. data\companion.err 파일을 확인하세요.
    pause
    exit /b 1
)

rem 서버가 응답할 때까지 최대 15초 기다린 뒤 브라우저를 연다
powershell -NoProfile -Command "for ($i=0; $i -lt 15; $i++) { try { Invoke-WebRequest http://127.0.0.1:8081/ -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { Start-Sleep 1 } }; exit 1"
if errorlevel 1 (
    echo [경고] 15초 안에 콘솔이 응답하지 않았습니다. data\companion.err 파일을 확인하세요.
    pause
    exit /b 1
)

start "" http://127.0.0.1:8081
echo 콘솔이 열렸습니다: http://127.0.0.1:8081
timeout /t 3 >nul
endlocal
