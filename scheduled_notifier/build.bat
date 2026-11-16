@echo off
chcp 65001 >nul
call "%~dp0prepare_env.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
"%NOTIFIER_PYTHON%" -m pip install "pyinstaller>=6,<7"
if errorlevel 1 (
    pause
    exit /b 1
)
cd /d "%~dp0.."
"%NOTIFIER_PYTHON%" -m PyInstaller --clean --noconfirm --onefile --windowed --name WeComScheduledNotifier --distpath "%~dp0dist" --workpath "%~dp0build" --specpath "%~dp0build" --paths "%~dp0.." "%~dp0__main__.py"
if errorlevel 1 (
    echo 打包失败，请查看上方错误信息。
    pause
    exit /b 1
)
echo 已生成 %~dp0dist\WeComScheduledNotifier.exe
echo 将此 exe 复制到目标 Windows 电脑后即可启动。
pause
