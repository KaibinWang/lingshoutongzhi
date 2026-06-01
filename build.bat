@echo off
chcp 65001 >nul
echo ========================================
echo 零售通知自动分发工具 - 打包脚本
echo ========================================
echo.

REM 检查Python是否安装
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未找到Python，请先安装Python
    pause
    exit /b 1
)

REM 安装依赖
echo 正在安装依赖...
pip install -r requirements.txt
if errorlevel 1 (
    echo 错误: 安装依赖失败
    pause
    exit /b 1
)

echo.
echo 正在打包为exe...

REM 使用PyInstaller打包
pyinstaller --onefile ^
    --windowed ^
    --name "零售通知工具" ^
    --add-data "data;data" ^
    --hidden-import PyQt5 ^
    --hidden-import requests ^
    --hidden-import openpyxl ^
    main.py

if errorlevel 1 (
    echo 错误: 打包失败
    pause
    exit /b 1

)

echo.
echo ========================================
echo 打包完成！
echo 生成的exe文件位于: dist\零售通知工具.exe
echo ========================================
echo.

REM 复制数据文件到dist目录
if not exist "dist\data" mkdir "dist\data"
copy /Y "data\groups.json" "dist\data\" >nul
copy /Y "data\templates.json" "dist\data\" >nul

echo 已复制数据文件到dist目录
echo.

pause
