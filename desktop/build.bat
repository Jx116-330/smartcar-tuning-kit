@echo off
cd /d "%~dp0"

echo ============================================
echo   SmartCar Tuning Tool - Build
echo ============================================
echo.

echo [0/2] Installing dependencies ...
python -m pip install pyinstaller ttkbootstrap
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. Make sure Python is in PATH.
    pause & exit /b 1
)

echo.
echo [1/2] Building exe ...
python -m PyInstaller --clean smartcar.spec
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. See output above.
    pause & exit /b 1
)

echo.
echo [2/2] Copying config template ...
if not exist "dist\config.json" copy config.json dist\config.json >nul

echo.
echo ============================================
echo   Build complete!
echo   Output: dist\SmartCarTuningTool.exe
echo   Config: dist\config.json (edit to customize)
echo ============================================
pause
