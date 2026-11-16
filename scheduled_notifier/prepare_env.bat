@echo off
chcp 65001 >nul
set "NOTIFIER_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%NOTIFIER_PYTHON%" (
    py -3 -m venv "%~dp0.venv"
    if errorlevel 1 (
        python -m venv "%~dp0.venv"
        if errorlevel 1 (
            echo 请先安装 Python 3.9 或以上版本，再运行本脚本。
            exit /b 1
        )
    )
)
"%NOTIFIER_PYTHON%" -c "import PyQt5, requests" >nul 2>&1
if errorlevel 1 (
    "%NOTIFIER_PYTHON%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 exit /b 1
)
exit /b 0
