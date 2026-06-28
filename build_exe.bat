@echo off
setlocal enabledelayedexpansion

echo ========================================
echo  Building NIMbus Standalone Executable
echo ========================================

set PROJECT_DIR=%~dp0
set VENV_PYTHON=%PROJECT_DIR%venv\Scripts\python.exe

:: Step 1: Install PyInstaller in venv
echo [1/5] Installing PyInstaller in venv...
call "%VENV_PYTHON%" -m pip install pyinstaller -q
if %errorlevel% neq 0 (
    echo ERROR: Failed to install PyInstaller
    exit /b 1
)
echo  OK

:: Step 2: Pre-download tiktoken encoding data with correct SHA1 cache naming
echo [2/5] Downloading tiktoken encoding data...
if not exist "%PROJECT_DIR%build_resources\tiktoken_cache" mkdir "%PROJECT_DIR%build_resources\tiktoken_cache"
if not exist "%PROJECT_DIR%build_resources\tiktoken_cache\9b5ad71b2ce5302211f9c61530b329a4922fc6a4" (
    call "%VENV_PYTHON%" "%PROJECT_DIR%build_exe_download_tiktoken.py"
)
echo  OK

:: Step 3: Create PyInstaller hidden import hook
echo [3/5] Creating PyInstaller hooks...
if not exist "%PROJECT_DIR%build_hooks" mkdir "%PROJECT_DIR%build_hooks"
echo  OK

:: Step 3.5: Pre-download Playwright browsers for bundling
echo [3.5/5] Pre-downloading Playwright browsers for portable build...
call "%VENV_PYTHON%" -m playwright install chromium
if %errorlevel% neq 0 (
    echo WARNING: Failed to pre-download Playwright Chromium (will fall back to runtime download)
)

:: Copy browser binaries to build_resources for PyInstaller bundling
echo [3.6/5] Copying Playwright browsers to build_resources...
if exist "%PROJECT_DIR%build_resources\ms-playwright" rmdir /s /q "%PROJECT_DIR%build_resources\ms-playwright"
mkdir "%PROJECT_DIR%build_resources\ms-playwright"
xcopy "%USERPROFILE%\AppData\Local\ms-playwright\chromium-1223" "%PROJECT_DIR%build_resources\ms-playwright\chromium-1223\" /E /I /Q >nul
xcopy "%USERPROFILE%\AppData\Local\ms-playwright\chromium_headless_shell-1223" "%PROJECT_DIR%build_resources\ms-playwright\chromium_headless_shell-1223\" /E /I /Q >nul
xcopy "%USERPROFILE%\AppData\Local\ms-playwright\ffmpeg-1011" "%PROJECT_DIR%build_resources\ms-playwright\ffmpeg-1011\" /E /I /Q >nul
xcopy "%USERPROFILE%\AppData\Local\ms-playwright\winldd-1007" "%PROJECT_DIR%build_resources\ms-playwright\winldd-1007\" /E /I /Q >nul
xcopy "%USERPROFILE%\AppData\Local\ms-playwright\.links" "%PROJECT_DIR%build_resources\ms-playwright\.links\" /E /I /Q >nul
if exist "%PROJECT_DIR%build_resources\ms-playwright\chromium-1223\chrome-win64\chrome.exe" (
    echo  OK - Playwright browsers copied
) else (
    echo  WARNING: Could not copy chromium, will use runtime download fallback
)

:: Step 4: Clean previous builds
echo [4/5] Cleaning previous builds...
:: Kill any stale nimbus processes first
taskkill /F /IM nimbus.exe >nul 2>&1
timeout /t 1 /nobreak >nul
if exist "%PROJECT_DIR%build_exe" rmdir /s /q "%PROJECT_DIR%build_exe"
:: Regenerate spec file from scratch to pick up code changes
if exist "%PROJECT_DIR%nimbus.spec" del "%PROJECT_DIR%nimbus.spec"
:: Delete old exe (keep other files like .env, overrides.json)
if exist "%PROJECT_DIR%dist_exe\nimbus.exe" del "%PROJECT_DIR%dist_exe\nimbus.exe"
echo  OK

:: Step 5: Build with PyInstaller
echo [5/5] Building executable...
cd /d "%PROJECT_DIR%"

call "%VENV_PYTHON%" -m PyInstaller ^
    --onefile ^
    --console ^
    --noconfirm ^
    --name nimbus ^
    --distpath dist_exe ^
    --workpath build_exe ^
    --specpath . ^
    --add-data "build_resources\tiktoken_cache\9b5ad71b2ce5302211f9c61530b329a4922fc6a4;tiktoken_cache" ^
    --add-data "build_resources\ms-playwright\chromium-1223;ms-playwright/chromium-1223" ^
    --add-data "build_resources\ms-playwright\chromium_headless_shell-1223;ms-playwright/chromium_headless_shell-1223" ^
    --add-data "build_resources\ms-playwright\ffmpeg-1011;ms-playwright/ffmpeg-1011" ^
    --add-data "build_resources\ms-playwright\winldd-1007;ms-playwright/winldd-1007" ^
    --add-data "build_resources\ms-playwright\.links;ms-playwright/.links" ^
    --additional-hooks-dir build_hooks ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.loops.asyncio ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.http.h11_impl ^
    --hidden-import uvicorn.protocols.http.httptools_impl ^
    --hidden-import uvicorn.protocols.http.flow_control ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.protocols.websockets.websockets_impl ^
    --hidden-import uvicorn.middleware.asgi2 ^
    --hidden-import uvicorn.middleware.wsgi ^
    --hidden-import uvicorn.middleware.proxy_headers ^
    --hidden-import tiktoken_ext.openai_public ^
    --add-data "venv\Lib\site-packages\playwright;playwright" ^
    --add-data "venv\Lib\site-packages\playwright_stealth;playwright_stealth" ^
    --add-data "venv\Lib\site-packages\greenlet;greenlet" ^
    --add-data "venv\Lib\site-packages\pyee;pyee" ^
    --hidden-import playwright ^
    --hidden-import playwright.async_api ^
    --hidden-import playwright._impl._browser_type ^
    --hidden-import playwright_stealth ^
    --hidden-import greenlet ^
    --hidden-import greenlet._greenlet ^
    --hidden-import pyee ^
    --hidden-import pyee.asyncio ^
    --hidden-import config ^
    --hidden-import config.settings ^
    --hidden-import config.nim ^
    --hidden-import config.logging_config ^
    --hidden-import api ^
    --hidden-import api.app ^
    --hidden-import api.routes ^
    --hidden-import api.dependencies ^
    --hidden-import api.middleware ^
    --hidden-import api.models ^
    --hidden-import api.models.anthropic ^
    --hidden-import api.models.responses ^
    --hidden-import api.bot_protection ^
    --hidden-import api.optimization_handlers ^
    --hidden-import api.request_utils ^
    --hidden-import providers ^
    --hidden-import providers.provider ^
    --hidden-import providers.base ^
    --hidden-import providers.text ^
    --hidden-import providers.rate_limit ^
    --hidden-import providers.request_queue ^
    --hidden-import providers.logging_utils ^
    --hidden-import providers.error_mapping ^
    --hidden-import providers.sse_builder ^
    --hidden-import providers.header_capture ^
    --hidden-import providers.heuristic_tool_parser ^
    --hidden-import providers.request ^
    --hidden-import providers.tiktoken_cache ^
    --hidden-import cli ^
    --hidden-import cli.manager ^
    --hidden-import cli.session ^
    --hidden-import cli.process_registry ^
    --hidden-import discord_bot ^
    --hidden-import discord_bot.bot ^
    --hidden-import discord_bot.cog ^
    --hidden-import discord_bot.conversation ^
    --hidden-import discord_bot.persistence ^
    --hidden-import discord_bot.rate_limit ^
    --hidden-import discord_bot.views ^
    --hidden-import discord_bot.user_blocking ^
    --hidden-import discord_bot.tools ^
    --hidden-import discord_bot.tools.web_search ^
    --hidden-import websearch ^
    --hidden-import websearch.duckduckgo_html ^
    --hidden-import api.swapper ^
    --hidden-import api.swapper.parser ^
    --hidden-import api.swapper.validator ^
    --add-data "venv\Lib\site-packages\playwright\driver\node.exe;playwright/driver" ^
    --add-data "venv\Lib\site-packages\playwright\driver\package;playwright/driver/package" ^
    --runtime-hook build_hooks/runtime-playwright.py ^
    --exclude-module pytest ^
    --exclude-module unittest ^
    --exclude-module IPython ^
    --exclude-module jupyter ^
    --exclude-module sentry_sdk ^
    --exclude-module pygments ^
    --exclude-module rich ^
    --exclude-module jinja2 ^
    --exclude-module PyYAML ^
    --exclude-module email_validator ^
    --exclude-module torch ^
    --exclude-module grpcio ^
    --exclude-module PIL ^
    --exclude-module matplotlib ^
    --exclude-module tkinter ^
    --exclude-module PyNaCl ^
    --exclude-module PyQt5 ^
    --exclude-module PySide6 ^
    packaged_entry.py

if %errorlevel% neq 0 (
    echo.
    echo ERROR: Build failed
    exit /b 1
)

:: Step 6: Build complete (browsers are embedded inside exe via --add-data)
echo.
echo ========================================
echo  Build complete!
echo  Executable at: dist_exe\nimbus.exe
echo ========================================
echo.
echo  Browsers are embedded inside the .exe (extracted to temp at runtime).
echo  No external ms-playwright folder needed.
echo.

endlocal

pause