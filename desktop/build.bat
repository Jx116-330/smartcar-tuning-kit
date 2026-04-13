@echo off
echo ============================================
echo   SmartCar Tuning Tool - Build
echo ============================================
echo.

pip install pyinstaller ttkbootstrap >nul 2>&1

echo [1/2] Building exe ...
pyinstaller --clean smartcar.spec

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
