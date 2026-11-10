@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo AI DEV DIRECTOR 콘솔을 끕니다...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-companion.ps1"
if errorlevel 1 (
    echo [오류] 콘솔을 끄지 못했습니다. 8081 포트를 다른 프로그램이 쓰고 있을 수 있습니다.
    pause
    exit /b 1
)
echo 완료. 작업 기록과 작업본은 그대로 남아 있습니다.
timeout /t 3 >nul
