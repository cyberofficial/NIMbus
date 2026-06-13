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

echo.
echo ========================================
echo  Build complete!
echo  Executable at: dist_exe\nimbus.exe
echo ========================================
echo.
echo  Place the .exe anywhere, run it, and it will
echo  auto-create a .env file for you on first run.
echo.

endlocal
