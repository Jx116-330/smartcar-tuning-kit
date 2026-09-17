@echo off
cd /d "%~dp0"

echo ============================================
echo   Yaw Tuning Tool - Build
echo ============================================
echo.

echo [0/3] Building web UI (web\dist) ...
where npm >nul 2>nul
if errorlevel 1 (
    echo WARNING: npm not found, skipping web build. Using existing web\dist if present.
) else (
    pushd web
    call npm run build
    if errorlevel 1 (
        popd
        echo.
        echo ERROR: web UI build failed. See output above.
        pause & exit /b 1
    )
    popd
)

echo.
echo [1/3] Installing dependencies ...
python -m pip install pyinstaller ttkbootstrap
if errorlevel 1 (
    echo.
    echo ERROR: pip install failed. Make sure Python is in PATH.
    pause & exit /b 1
)

echo.
echo [2/3] Building exe ...
python -m PyInstaller --clean smartcar.spec
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. See output above.
    pause & exit /b 1
)
rem 控制台构建(headless/服务场景,设计文档 2026-09-14)
python -m PyInstaller --clean smartcar_console.spec
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller console build failed. See output above.
    pause & exit /b 1
)

echo.
echo [3/3] Copying config ...
if not exist "dist\config.json" copy config.json dist\config.json >nul
if not exist "dist\control_schema.json" copy control_schema.json dist\control_schema.json >nul
if exist xfyun_credentials.py copy /Y xfyun_credentials.py dist\xfyun_credentials.py >nul
rem P1/P4: agent tool scripts ship alongside exe
if not exist "dist\score_profile.json" copy score_profile.json dist\score_profile.json >nul
rem 桥扩展属代码文件:无条件覆盖(旧 dist 升级拿到新扩展的关键;
rem 冻结验收会校验其内容与源码一致)
for %%F in (score_engine.py session_driver.py mcp_server.py bridge_ext.py) do copy /Y %%F dist\%%F >nul

rem profiles/:配置仅缺失时复制(保留用户修改,与根 config 同原则);
rem 代码/文档同理逐文件 if-not-exist(不用 robocopy,/IS 语义不等于仅缺失复制)
if not exist "dist\profiles\virtual" mkdir "dist\profiles\virtual" >nul 2>nul
for %%F in (config.json control_schema.json score_profile.json virtual_device.py README.md) do (
    if not exist "dist\profiles\virtual\%%F" copy "profiles\virtual\%%F" "dist\profiles\virtual\%%F" >nul
)

echo.
echo ============================================
echo   Build complete!
echo   Output: dist\YawTuningTool.exe (+ YawTuningToolConsole.exe)
echo   Config: dist\config.json (edit to customize)
echo.
echo   发布验收(必须):
echo     python tests\test_frozen_headless.py --require
echo ============================================
pause
