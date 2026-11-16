@echo off
chcp 65001 >nul
call "%~dp0prepare_env.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
cd /d "%~dp0.."
"%NOTIFIER_PYTHON%" -m scheduled_notifier
if errorlevel 1 pause
