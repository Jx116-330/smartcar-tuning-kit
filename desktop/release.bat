@echo off
setlocal

set VERSION=1.0
set RELEASE_DIR=release\SmartCarTuningTool-v%VERSION%
set ZIP_NAME=release\SmartCarTuningTool-v%VERSION%.zip

echo ============================================
echo   SmartCar Tuning Tool - Release Package
echo ============================================
echo.

:: Build exe
echo [1/3] Building exe ...
call build.bat >nul 2>&1
if not exist "dist\SmartCarTuningTool.exe" (
    echo ERROR: Build failed. Run build.bat manually to see errors.
    pause & exit /b 1
)

:: Assemble release folder
echo [2/3] Assembling release folder ...
if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%"

copy dist\SmartCarTuningTool.exe "%RELEASE_DIR%\SmartCarTuningTool.exe" >nul
copy config.json                 "%RELEASE_DIR%\config.json" >nul
copy config_yaw.json             "%RELEASE_DIR%\config_yaw.json" >nul

:: Write a plain-text quickstart note
(
echo SmartCar Tuning Tool - Quick Start
echo ====================================
echo.
echo 1. Double-click SmartCarTuningTool.exe to launch.
echo 2. The Dashboard link in the app opens in your browser.
echo 3. Edit config.json to customize channels, metrics, and commands.
echo    - For yaw tuning: copy config_yaw.json over config.json and restart.
echo 4. Connect your device and press [Start] in the control panel.
echo.
echo Network defaults: TCP port 8080  /  Dashboard port 9898
echo.
echo GitHub: https://github.com/Jx116-330/smartcar-tuning-kit
) > "%RELEASE_DIR%\README.txt"

:: Zip it
echo [3/3] Creating ZIP ...
powershell -NoProfile -Command "Compress-Archive -Path '%RELEASE_DIR%\*' -DestinationPath '%ZIP_NAME%' -Force"
if not exist "%ZIP_NAME%" (
    echo WARNING: ZIP creation failed ^(PowerShell required^).
    echo Release folder is ready at: %RELEASE_DIR%
) else (
    echo.
    echo ============================================
    echo   Done!
    echo   %ZIP_NAME%
    echo ============================================
)
pause
